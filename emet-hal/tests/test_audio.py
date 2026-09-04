"""The hardware floor.

Nothing here touches a microphone or a speaker. Two reasons, and the second
matters more than the first: audio hardware is not present in CI, and a test
suite that records from whatever microphone is attached to the machine running
it is a rude thing to ship.

So the device-dependent paths are tested through PortAudio's *format query*,
which asks whether a device would accept a format without opening it, and
everything else runs over wav files generated in a temp directory. The fixtures
are written by the tests rather than committed, so the repository stays free of
binary blobs whose provenance nobody can check.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from emet_hal.audio import (
    AudioError,
    AudioFormat,
    AudioSink,
    AudioSource,
    NullSink,
    Speaker,
    WavSink,
    MicrophoneSource,
    WavSource,
    _downmix,
    resolve_device,
)

try:  # the `audio` extra is optional, exactly like `wake`
    import sounddevice  # noqa: F401

    HAVE_SOUNDDEVICE = True
except (ImportError, OSError):  # pragma: no cover - depends on the environment
    # OSError too: the wheel is pure Python and loads PortAudio from the
    # system, so an installed sounddevice with no libportaudio2 raises from the
    # dynamic loader rather than from the import machinery.
    HAVE_SOUNDDEVICE = False

needs_sounddevice = pytest.mark.skipif(
    not HAVE_SOUNDDEVICE, reason="emet-hal[audio] is not installed"
)


def run(coro):
    return asyncio.run(coro)


def write_wav(path: Path, pcm: bytes, *, rate: int = 16000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def silence(frames: int, channels: int = 1) -> bytes:
    return b"\x00\x00" * frames * channels


# ----------------------------------------------------------------- format


def test_the_format_states_what_a_frame_is():
    fmt = AudioFormat()
    assert fmt.sample_rate == 16000
    assert fmt.frame_bytes == 2560
    assert fmt.frame_ms == pytest.approx(80.0)


def test_the_audio_contract_lives_in_the_sdk():
    """`emet_engine` may import `emet_sdk` and nothing else, so the Protocol
    describing the engine's own input cannot live here. This module re-exports
    it for convenience; it does not own it."""
    from emet_sdk.types import AudioFormat as SdkFormat
    from emet_sdk.types import AudioSource as SdkSource

    assert AudioSource is SdkSource
    assert AudioFormat is SdkFormat


def test_the_default_format_is_what_the_wake_engine_asks_for():
    """These two travelling apart is how a detector ends up hearing nothing."""
    from emet_hal.pocketsphinx_wake import FRAME_SAMPLES, SAMPLE_RATE

    fmt = AudioFormat()
    assert (fmt.sample_rate, fmt.frame_samples) == (SAMPLE_RATE, FRAME_SAMPLES)


# ---------------------------------------------------------------- downmix


def test_mono_passes_through_untouched():
    raw = b"\x01\x02\x03\x04"
    assert _downmix(raw, 1, mix="first") is raw


def test_taking_the_first_channel_of_a_stereo_frame():
    # interleaved: L0 R0 L1 R1, as int16 little-endian
    raw = b"\x01\x00" + b"\x63\x00" + b"\x02\x00" + b"\x64\x00"
    assert _downmix(raw, 2, mix="first") == b"\x01\x00\x02\x00"


def test_averaging_across_channels():
    raw = b"\x0a\x00" + b"\x14\x00"  # 10 and 20 in one stereo frame
    assert _downmix(raw, 2, mix="average") == b"\x0f\x00"  # 15


def test_a_four_capsule_array_arrives_downstream_as_mono():
    """scout-01 declares `channels: 4`. Nothing above this counts capsules."""
    frame = b"".join(bytes([c, 0]) for c in (1, 2, 3, 4))
    assert _downmix(frame * 3, 4, mix="first") == b"\x01\x00" * 3


# ------------------------------------------------------------- wav source


def test_a_file_can_stand_in_for_a_microphone(tmp_path):
    src = WavSource.from_path(str(write_wav(tmp_path / "a.wav", silence(1280 * 3))))
    assert isinstance(src, AudioSource)

    async def scenario():
        await src.start()
        frames = []
        while (f := await src.read()) is not None:
            frames.append(f)
        await src.stop()
        return frames

    frames = run(scenario())
    assert len(frames) == 3
    assert all(len(f) == 2560 for f in frames)


def test_a_partial_trailing_frame_ends_the_stream(tmp_path):
    """Half a frame is not a frame. Padding it would hand the detector
    silence that was never recorded."""
    src = WavSource.from_path(str(write_wav(tmp_path / "b.wav", silence(1280 * 2 + 400))))

    async def scenario():
        await src.start()
        n = 0
        while await src.read() is not None:
            n += 1
        await src.stop()
        return n

    assert run(scenario()) == 2


def test_a_stereo_file_is_downmixed(tmp_path):
    path = write_wav(tmp_path / "c.wav", silence(1280, channels=2), channels=2)
    src = WavSource.from_path(str(path))

    async def scenario():
        await src.start()
        frame = await src.read()
        await src.stop()
        return frame

    assert len(run(scenario())) == 2560


def test_a_file_at_the_wrong_rate_is_refused_by_name(tmp_path):
    """The failure this module exists to prevent, in its cheapest form."""
    src = WavSource.from_path(str(write_wav(tmp_path / "d.wav", silence(1280), rate=44100)))
    with pytest.raises(AudioError) as exc:
        run(src.start())
    assert "44100" in str(exc.value)
    assert "16000" in str(exc.value)


def test_a_wav_source_can_be_built_the_way_the_engine_builds_it(tmp_path):
    """`cls(config, fmt)` with the manifest's `audio.input` block, which is all
    the engine has: it holds a name and a mapping, never an imported class."""
    path = write_wav(tmp_path / "cfg.wav", silence(1280))
    src = WavSource({"source": "wav", "params": {"path": str(path)}})

    async def scenario():
        await src.start()
        frame = await src.read()
        await src.stop()
        return frame

    assert len(run(scenario())) == 2560


def test_a_wav_source_with_no_path_says_so(tmp_path):
    src = WavSource({"source": "wav"})
    with pytest.raises(AudioError, match="params.path"):
        run(src.start())


def test_a_missing_file_says_which_one(tmp_path):
    src = WavSource.from_path(str(tmp_path / "nope.wav"))
    with pytest.raises(AudioError, match="nope.wav"):
        run(src.start())


def test_eight_bit_audio_is_refused(tmp_path):
    path = tmp_path / "e.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(16000)
        w.writeframes(b"\x80" * 1280)
    with pytest.raises(AudioError, match="8-bit"):
        run(WavSource.from_path(str(path)).start())


# ------------------------------------------------- the whole path, no mic


def test_the_wake_path_runs_over_a_file(tmp_path):
    """Source to detector with no hardware anywhere.

    Silence, so nothing should fire. What this proves is the plumbing: frame
    sizes line up, the detector accepts what the source produces, and the loop
    terminates.
    """
    pytest.importorskip("pocketsphinx", reason="emet-hal[wake] is not installed")
    from emet_hal.pocketsphinx_wake import PocketSphinxWake

    src = WavSource.from_path(str(write_wav(tmp_path / "quiet.wav", silence(1280 * 20))))
    wake = PocketSphinxWake({"engine": "pocketsphinx", "params": {}}, "hey emet")

    async def scenario():
        await src.start()
        await wake.start()
        assert wake.describe().healthy, wake.health().detail
        assert wake.describe().sample_rate == src.format.sample_rate
        seen, events = 0, []
        while (frame := await src.read()) is not None:
            seen += 1
            if (event := await wake.process(frame)) is not None:
                events.append(event)
        await wake.shutdown()
        await src.stop()
        return seen, events

    seen, events = run(scenario())
    assert seen == 20
    assert events == []


def test_a_mock_engine_wakes_from_a_file(tmp_path):
    """The positive half, without needing anyone to speak.

    MockWake fires on a frame containing the phrase, so a file whose samples
    happen to spell it exercises the event actually propagating from source to
    detector to caller.
    """
    from emet_hal.mock import MockWake

    phrase = b"hey emet"
    pcm = silence(1280) + phrase + silence(1280) * 2
    src = WavSource.from_path(str(write_wav(tmp_path / "spelled.wav", pcm)))
    wake = MockWake({"engine": "mock", "params": {}}, "hey emet")

    async def scenario():
        await src.start()
        await wake.start()
        events = []
        while (frame := await src.read()) is not None:
            if (event := await wake.process(frame)) is not None:
                events.append(event)
                await wake.reset()
        await src.stop()
        return events

    events = run(scenario())
    assert len(events) == 1
    assert events[0].phrase == "hey emet"


# ------------------------------------------------------- device resolution


def test_the_default_device_needs_no_lookup():
    """Must not touch PortAudio: it is the answer when nothing is configured."""
    assert resolve_device(None, want_input=True) is None
    assert resolve_device("", want_input=True) is None
    assert resolve_device("default", want_input=False) is None


def test_a_numeric_device_is_an_index():
    assert resolve_device(3, want_input=True) == 3
    assert resolve_device("3", want_input=True) == 3


@needs_sounddevice
def test_an_alsa_name_on_the_wrong_machine_explains_itself():
    """`plughw:1,0` is right on the Pi the manifest describes and meaningless
    here. The message has to say that, not just 'not found'."""
    import sys

    if sys.platform.startswith("linux"):  # pragma: no cover - platform dependent
        pytest.skip("plughw may genuinely resolve on Linux")
    with pytest.raises(AudioError) as exc:
        resolve_device("plughw:1,0", want_input=True)
    message = str(exc.value)
    assert "ALSA" in message
    assert "Available:" in message


@needs_sounddevice
def test_an_unknown_device_lists_what_there_is():
    with pytest.raises(AudioError) as exc:
        resolve_device("no such microphone anywhere", want_input=True)
    assert "Available:" in str(exc.value)


@needs_sounddevice
def test_devices_can_be_listed_without_opening_any():
    from emet_hal.audio import devices

    ins = devices(want_input=True)
    outs = devices(want_input=False)
    assert all(d.inputs for d in ins)
    assert all(d.outputs for d in outs)
    # Both lists come from one enumeration, so a machine with neither is the
    # only way this is empty, and that is worth knowing rather than asserting.
    assert isinstance(ins, list) and isinstance(outs, list)


@needs_sounddevice
def test_an_impossible_rate_is_refused_before_the_device_opens():
    """A format query, not a capture. Nothing is recorded to run this."""
    from emet_hal.audio import _check_rate

    with pytest.raises(AudioError) as exc:
        _check_rate(None, 999_999, 1, want_input=True)
    assert "999999" in str(exc.value)


def test_a_microphone_source_carries_the_manifest_block():
    """Construction must not touch hardware; only `start()` may."""
    src = MicrophoneSource({"device": "plughw:1,0", "channels": 4})
    assert isinstance(src, AudioSource)
    assert src._channels == 4
    assert src.dropped == 0
    assert src.format.sample_rate == 16000


def test_dropped_frames_are_counted_rather_than_queued():
    """A backlog would make the robot answer a question from a minute ago, so
    the oldest frame goes and the loss is recorded."""

    async def scenario():
        src = MicrophoneSource({"channels": 1})
        src._queue = asyncio.Queue(maxsize=2)
        src._loop = asyncio.get_running_loop()
        for i in range(5):
            src._offer(bytes([i, 0]) * 1280)
        return src

    src = run(scenario())
    assert src.dropped == 3
    assert src._queue.qsize() == 2


# ------------------------------------------------------------------- sinks


def test_the_null_sink_swallows_audio_and_counts_it():
    """How the loop runs at midnight, and how a test avoids playing sound
    through whatever is plugged into the machine running it."""
    sink = NullSink()

    async def scenario():
        await sink.start()
        await sink.play(silence(100))
        await sink.play(silence(50))
        await sink.cancel()
        await sink.stop()

    run(scenario())
    assert sink.written == 300
    assert sink.cancelled == 1


def test_the_wav_sink_writes_a_readable_file(tmp_path):
    path = tmp_path / "said.wav"
    sink = WavSink({"sink": "wav", "params": {"path": str(path)}}, AudioFormat(sample_rate=22050))

    async def scenario():
        await sink.start()
        await sink.play(silence(1000))
        await sink.stop()

    run(scenario())
    with wave.open(str(path)) as w:
        assert w.getframerate() == 22050
        assert w.getnchannels() == 1
        assert w.getnframes() == 1000


def test_the_wav_sink_needs_a_destination():
    with pytest.raises(AudioError, match="params.path"):
        run(WavSink({"sink": "wav"}).start())


def test_a_speaker_can_be_built_without_touching_hardware():
    """Construction must be inert; only `start()` may open a device. A test
    that constructs one must not make a sound."""
    speaker = Speaker({"device": "plughw:1,0"}, AudioFormat(sample_rate=22050))
    assert speaker.sample_rate == 22050
    assert isinstance(speaker, AudioSink)


def test_output_defaults_to_a_synthesis_rate_not_the_input_rate():
    """16 kHz is what the detector needs. Speech generated at 16 kHz sounds
    like a telephone, and the two directions have no reason to agree."""
    assert NullSink().format.sample_rate == 22050
    assert AudioFormat().sample_rate == 16000
