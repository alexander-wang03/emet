"""The real PortAudio path, through a virtual sound card that only CI has.

Every other audio test stands a wav file in for the microphone and the null
sink in for the speaker, because a suite must never open the hardware of the
machine it runs on. That leaves the PortAudio streams inside
`MicrophoneSource` and `Speaker` untested in CI. The `virtual sound card` job
in .github/workflows/ci.yml gives this file a card that is no hardware: a
PulseAudio null sink, whose monitor is a capture source carrying whatever
was played into the sink, named to ALSA in ~/.asoundrc as
`emet_virtual_sink` and `emet_virtual_source`. A tone played through the
speaker comes back through the microphone.

The job sets EMET_VIRTUAL_AUDIO=1, and nothing else does; without it every
test here skips. With it set, a missing piece fails instead of skipping,
since the job promised a card. Before anything opens, the PulseAudio server
the job started must list the null sink and PortAudio must list both
virtual devices by name, and the streams are opened by those names only, so
the variable set by mistake on a machine with real cards fails here having
opened nothing.

The card's setup in ci.yml is adapted from reachy_mini's
.github/actions/virtual-sound-card/action.yml (Pollen Robotics, Apache-2.0,
at commit 9fb2bfcbdf4c9800b0649acb8f0c18027673943f), with the names
changed; the tests in this file are Emet's own.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
from array import array

import pytest

if os.environ.get("EMET_VIRTUAL_AUDIO") != "1":
    pytest.skip(
        "the virtual sound card exists only in CI's `virtual sound card` job, which sets EMET_VIRTUAL_AUDIO=1",
        allow_module_level=True,
    )

from emet_hal.audio import AudioFormat, MicrophoneSource, Speaker, _check_rate, devices  # noqa: E402

#: The names the job's ~/.asoundrc gives the null sink and its monitor.
SINK = "emet_virtual_sink"
SOURCE = "emet_virtual_source"
#: The PulseAudio sink behind both.
NULL_SINK = "emet_null"

RATE = 16000
FMT = AudioFormat(sample_rate=RATE)
TONE_HZ = 440.0


def run(coro):
    return asyncio.run(coro)


def tone(seconds: float, *, level: float = 0.3) -> bytes:
    n = int(seconds * RATE)
    return array("h", (int(32767 * level * math.sin(2 * math.pi * TONE_HZ * i / RATE)) for i in range(n))).tobytes()


def rms(frame: bytes) -> float:
    samples = array("h", frame)
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def crossings(frame: bytes) -> int:
    samples = array("h", frame)
    return sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))


@pytest.fixture(scope="module", autouse=True)
def the_virtual_card_is_there():
    """The job's card, checked before any stream opens. Fails, never skips.

    PulseAudio is asked first, before PortAudio starts and lists the cards,
    so on a machine without the job's server nothing audio has run at all."""
    sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=30)
    assert sinks.returncode == 0, f"pactl could not reach the job's PulseAudio server: {sinks.stderr}"
    assert any(line.split("\t")[1:2] == [NULL_SINK] for line in sinks.stdout.splitlines()), sinks.stdout
    import sounddevice  # noqa: F401  (an ImportError or OSError here fails the job)

    outs = [d.name for d in devices(want_input=False)]
    ins = [d.name for d in devices(want_input=True)]
    assert SINK in outs, f"PortAudio lists no {SINK}: {outs}"
    assert SOURCE in ins, f"PortAudio lists no {SOURCE}: {ins}"


def test_both_ends_take_the_wake_rate_without_opening():
    """The format query a body runs at boot, against a card that converts."""
    _check_rate(next(d.index for d in devices(want_input=False) if d.name == SINK), RATE, 1, want_input=False)
    _check_rate(next(d.index for d in devices(want_input=True) if d.name == SOURCE), RATE, 1, want_input=True)


async def play_and_listen(level: float, gain_db: float = 0.0) -> tuple[list[bytes], list[bytes], Speaker, MicrophoneSource]:
    """Half a second of the quiet card, then a tone through the speaker and
    what the microphone heard while it played and just after."""
    mic = MicrophoneSource({"device": SOURCE, "channels": 1}, FMT)
    spk = Speaker({"device": SINK, "gain_db": gain_db}, FMT)
    await mic.start()
    await spk.start()
    try:
        quiet = [await asyncio.wait_for(mic.read(), 5.0) for _ in range(6)]
        playing = asyncio.create_task(spk.play(tone(1.0, level=level)))
        heard = [await asyncio.wait_for(mic.read(), 5.0) for _ in range(int(2.0 / (FMT.frame_ms / 1000)))]
        await asyncio.wait_for(playing, 10.0)
    finally:
        await spk.stop()
        await mic.stop()
    return quiet, heard, spk, mic


def test_a_tone_through_the_speaker_comes_back_through_the_microphone():
    quiet, heard, spk, mic = run(play_and_listen(0.3))
    assert max(rms(f) for f in quiet) < 100, "the card was not quiet before the tone"
    loudest = max(heard, key=rms)
    assert rms(loudest) > 1500, f"no tone came back: loudest frame {rms(loudest):.0f} RMS"
    # 440 Hz crosses zero 880 times a second: about 70 times in an 80 ms frame.
    assert 60 <= crossings(loudest) <= 81, f"{crossings(loudest)} zero crossings in the loudest frame"
    assert all(len(f) == FMT.frame_bytes for f in quiet + heard)
    assert spk.device_name == SINK and mic.device_name == SOURCE
    assert spk.stream_latency_s is not None and spk.stream_latency_s >= 0


def test_the_speakers_gain_reaches_the_card():
    """-6 dB is about half the amplitude, measured on what came back."""
    _, full, _, _ = run(play_and_listen(0.3))
    _, half, _, _ = run(play_and_listen(0.3, gain_db=-6.0))
    ratio = max(rms(f) for f in half) / max(rms(f) for f in full)
    assert 0.4 <= ratio <= 0.6, f"-6 dB came back at {ratio:.2f} of 0 dB"
