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
import os
import threading
import sys
import types
import wave
from array import array
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
    apply_gain,
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


def test_a_frame_arriving_after_stop_is_discarded():
    """PortAudio's thread can schedule one last `_offer` before the stream
    closes, and the loop runs it after `stop()` has cleared the queue. Ctrl-C
    on a live run used to end with an AssertionError traceback from exactly
    that. Before `start()` the queue is equally absent, so this is the same
    branch without a microphone."""
    src = MicrophoneSource({"channels": 1})
    src._offer(bytes(2 * 1280))
    assert src.dropped == 0


# --------------------------------------------------------------------------
# PortAudio configuration
# --------------------------------------------------------------------------


def _fake_sounddevice(monkeypatch):
    """Stand in for the real module so `_sd()` returns without touching
    PortAudio, and so these tests pass with the `audio` extra absent."""
    monkeypatch.setitem(sys.modules, "sounddevice", types.ModuleType("sounddevice"))


def _unset(monkeypatch, name):
    """Remove `name` from the environment for this test, restoring the
    original state afterwards even when it was absent to begin with."""
    monkeypatch.setenv(name, "placeholder")
    monkeypatch.delenv(name)


def test_on_linux_portaudio_is_told_to_use_plughw(monkeypatch):
    """`hw:` devices convert nothing, so a 48 kHz card refuses 16 kHz. The HAL
    asks PortAudio for `plughw:` before it starts, which also makes the
    `plughw:1,0` names in the manifests match what PortAudio lists."""
    from emet_hal import audio as audio_module

    monkeypatch.setattr(sys, "platform", "linux")
    _unset(monkeypatch, "PA_ALSA_PLUGHW")
    _fake_sounddevice(monkeypatch)
    audio_module._sd()
    assert os.environ["PA_ALSA_PLUGHW"] == "1"


def test_an_explicit_plughw_setting_is_respected(monkeypatch):
    from emet_hal import audio as audio_module

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PA_ALSA_PLUGHW", "0")
    _fake_sounddevice(monkeypatch)
    audio_module._sd()
    assert os.environ["PA_ALSA_PLUGHW"] == "0"


def test_off_linux_the_environment_is_left_alone(monkeypatch):
    from emet_hal import audio as audio_module

    monkeypatch.setattr(sys, "platform", "win32")
    _unset(monkeypatch, "PA_ALSA_PLUGHW")
    _fake_sounddevice(monkeypatch)
    audio_module._sd()
    assert "PA_ALSA_PLUGHW" not in os.environ


# --------------------------------------------------------------------------
# Speaker playback, against a fake PortAudio
# --------------------------------------------------------------------------


class FakeOutputStream:
    """Stands in for a `RawOutputStream` opened with a callback.

    Records how it was opened, and `pump()` plays PortAudio's thread: it asks
    the callback for one block and keeps whatever came out, zeros included,
    so a test can see exactly what the card would have played.
    """

    instances: list["FakeOutputStream"] = []

    latency = 0.085

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.blocksize = int(kwargs.get("blocksize") or 64)
        self.out = bytearray()
        self.started = self.stopped = self.closed = False
        FakeOutputStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, ignore_errors=True):
        self.stopped = True

    def close(self, ignore_errors=True):
        self.closed = True

    def pump(self, status=None) -> bytes:
        block = bytearray(2 * self.blocksize)
        self.callback(block, self.blocksize, None, status)
        self.out += block
        return bytes(block)


def speaker_with_fake_portaudio(rate: int = 16000, *, gain_db: float | None = None, **params) -> Speaker:
    FakeOutputStream.instances.clear()
    block = {"device": "USB", "params": params}
    if gain_db is not None:
        block["gain_db"] = gain_db
    spk = Speaker(block, AudioFormat(sample_rate=rate))
    spk._sd = types.SimpleNamespace(RawOutputStream=FakeOutputStream)
    spk._device = 2
    return spk


async def started(spk: Speaker) -> Speaker:
    """The half of `start()` that needs no hardware: the loop and the lock."""
    spk._loop = asyncio.get_running_loop()
    spk._opening = asyncio.Lock()
    return spk


async def pumped(spk: Speaker, coro, *, blocks: int = 500):
    """Run `coro` while pumping the stream the way PortAudio's thread would,
    one block per turn of the loop."""
    task = asyncio.ensure_future(coro)
    for _ in range(blocks):
        await asyncio.sleep(0.001)
        if task.done():
            break
        # Only once something is queued. The worker thread assigns the stream
        # a moment before `play()` resumes and queues, and a block pumped in
        # that moment puts silence at the start of what the card heard. The
        # speaker did nothing wrong, and the test failed on it now and then
        # (seen on the development laptop, 2026-09-23).
        if spk._stream is not None and spk.queued_bytes:
            spk._stream.pump()
    return await task


def drained(spk: Speaker) -> bytes:
    """Pump until the queue is empty, and return what the card was given
    with the trailing silence trimmed."""
    stream = spk._stream
    while spk.queued_bytes:
        stream.pump()
    return bytes(stream.out).rstrip(b"\x00")


def test_playback_goes_through_one_stream_that_stays_open():
    """0.4 opened a stream per buffer, which clicked between chunks, so the
    engine gathered each sentence before playing it and the cloud voice cost
    1.5 s before the first sound. One stream, opened on the first play and
    kept, is what lets a sentence be heard a chunk at a time."""
    spk = speaker_with_fake_portaudio()
    first = bytes(range(1, 256)) * 20
    second = bytes(range(255, 0, -1)) * 20

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(first))
        await pumped(spk, spk.play(second))
        return drained(spk)

    heard = run(scenario())
    (stream,) = FakeOutputStream.instances
    assert heard == first + second
    assert stream.started and not stream.stopped and not stream.closed
    assert stream.kwargs["samplerate"] == 16000 and stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "int16" and stream.kwargs["device"] == 2
    assert stream.kwargs["blocksize"] == 320, "20 ms at 16 kHz"
    assert "latency" not in stream.kwargs


def test_play_returns_with_the_last_block_still_in_hand():
    """So that the caller's next chunk lands before the queue runs dry."""
    spk = speaker_with_fake_portaudio()
    pcm = bytes(range(1, 256)) * 8  # 2040 bytes, a little over three blocks

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(pcm))
        return spk.queued_bytes

    left = run(scenario())
    assert 0 < left <= spk.block_bytes


def test_idle_blocks_are_silence_and_a_late_callback_is_counted():
    spk = speaker_with_fake_portaudio()

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(b"\x01\x02"))
        stream = spk._stream
        first = stream.pump()
        quiet = stream.pump()
        late = stream.pump(status=types.SimpleNamespace(output_underflow=True))
        return first, quiet, late

    first, quiet, late = run(scenario())
    assert first == b"\x01\x02" + bytes(638)
    assert quiet == bytes(640) and late == bytes(640)
    assert spk.underflows == 1


def test_cancel_empties_the_queue_and_frees_the_waiting_play():
    spk = speaker_with_fake_portaudio()
    long_pcm = bytes(range(1, 256)) * 100
    after_pcm = b"\x07\x07" * 40

    async def scenario():
        await started(spk)
        task = asyncio.ensure_future(spk.play(long_pcm))
        while spk._stream is None:
            await asyncio.sleep(0.001)
        spk._stream.pump()
        await spk.cancel()
        await asyncio.wait_for(task, 1.0)
        silence = spk._stream.pump()
        await pumped(spk, spk.play(after_pcm))
        return silence, drained(spk)

    silence, heard = run(scenario())
    (stream,) = FakeOutputStream.instances
    assert silence == bytes(640), "what was queued is gone"
    assert heard.endswith(after_pcm), "and the next play goes through the same stream"
    assert not stream.closed


def test_stop_closes_the_stream_and_a_later_play_is_refused():
    spk = speaker_with_fake_portaudio()

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(bytes(range(1, 256)) * 4))
        await spk.stop()
        (stream,) = FakeOutputStream.instances
        assert stream.stopped and stream.closed
        with pytest.raises(AudioError, match="not started"):
            await spk.play(b"\x01\x02")

    run(scenario())


def test_the_block_and_the_latency_come_from_params():
    spk = speaker_with_fake_portaudio(rate=22050, block_ms=10, latency="low")

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(bytes(range(1, 256)) * 4))

    run(scenario())
    (stream,) = FakeOutputStream.instances
    assert stream.kwargs["blocksize"] == 220
    assert stream.kwargs["latency"] == "low"


def test_a_speaker_that_cannot_open_says_so():
    spk = speaker_with_fake_portaudio()

    class Refuses:
        def __init__(self, **kwargs):
            raise RuntimeError("device busy")

    spk._sd = types.SimpleNamespace(RawOutputStream=Refuses)

    async def scenario():
        await started(spk)
        with pytest.raises(AudioError, match="could not open the speaker"):
            await spk.play(b"\x01\x02")

    run(scenario())


def test_playback_refuses_a_half_sample():
    spk = speaker_with_fake_portaudio()

    async def scenario():
        await started(spk)
        with pytest.raises(AudioError):
            await spk.play(bytes(3))

    run(scenario())


def test_play_before_start_is_refused():
    spk = Speaker({"device": "USB"}, AudioFormat(sample_rate=16000))
    with pytest.raises(AudioError, match="not started"):
        run(spk.play(b"\x01\x02"))


def test_card_overflows_are_counted_from_the_callback():
    """PortAudio flags an input overflow when its own buffer filled before the
    callback ran. That audio is gone before the queue exists, so it is counted
    apart from `dropped`, and it is the first thing to check when a live run's
    clock skew looks like drift."""
    src = MicrophoneSource({"channels": 1})
    src._on_audio(bytes(2 * 1280), 1280, None, types.SimpleNamespace(input_overflow=True))
    src._on_audio(bytes(2 * 1280), 1280, None, types.SimpleNamespace(input_overflow=False))
    src._on_audio(bytes(2 * 1280), 1280, None, None)
    assert src.overflows == 1
    assert src.dropped == 0


# --------------------------------------------------------------------------
# What an adversarial review of the persistent stream found
# --------------------------------------------------------------------------


def test_a_stream_that_opens_and_will_not_start_is_closed():
    """0.4 closed the stream in a `finally`. Keeping one open across calls
    lost that, and a stream nobody holds a reference to keeps the card until
    the process exits: sounddevice has no finaliser that would give it back.
    `Mouth` swallows the error and tries the next sentence, so it leaked once
    a sentence."""
    spk = speaker_with_fake_portaudio()

    class WillNotStart(FakeOutputStream):
        def start(self):
            raise RuntimeError("Error starting stream")

    spk._sd = types.SimpleNamespace(RawOutputStream=WillNotStart)

    async def scenario():
        await started(spk)
        with pytest.raises(AudioError, match="could not open the speaker"):
            await spk.play(b"\x01\x02")

    run(scenario())
    (stream,) = FakeOutputStream.instances
    assert stream.closed, "the card would stay claimed for the rest of the run"
    assert spk._stream is None


def test_a_stop_while_the_device_is_opening_closes_the_stream_it_finds_later():
    """`stop()` looks for a stream and finds none, because the worker thread
    has not assigned it yet. Without a hand-off that started stream is owned
    by nobody and holds the card, playing silence, until the process exits."""
    spk = speaker_with_fake_portaudio(stall_grace_s=0.05)
    gate = threading.Event()

    class SlowToOpen(FakeOutputStream):
        def start(self):
            gate.wait(2.0)
            super().start()

    spk._sd = types.SimpleNamespace(RawOutputStream=SlowToOpen)

    async def scenario():
        await started(spk)
        playing = asyncio.ensure_future(spk.play(bytes(640)))
        while not FakeOutputStream.instances:
            await asyncio.sleep(0.001)
        await spk.stop()
        gate.set()
        with pytest.raises(AudioError, match="stopped"):
            await asyncio.wait_for(playing, 2.0)
        for _ in range(100):
            if FakeOutputStream.instances[0].closed:
                break
            await asyncio.sleep(0.01)

    run(scenario())
    (stream,) = FakeOutputStream.instances
    assert stream.started and stream.closed, "the orphan was never given back"


def test_stop_plays_out_what_play_left_in_hand():
    """`play()` returns with a block still queued so the caller's next chunk
    lands behind it. At the end of a reply there is no next chunk, and
    `cancel()` would throw away the end of the last word."""
    spk = speaker_with_fake_portaudio()
    pcm = bytes(range(1, 256)) * 8

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(pcm))
        assert spk.queued_bytes, "the cushion is what this test is about"
        stream = spk._stream

        async def keep_pumping():
            while True:
                stream.pump()
                await asyncio.sleep(0.001)

        pump = asyncio.ensure_future(keep_pumping())
        try:
            await spk.stop()
        finally:
            pump.cancel()
        return bytes(stream.out).rstrip(b"\x00")

    heard = run(scenario())
    assert heard == pcm, "the tail of the last word was cut off"


def test_stop_closes_the_stream_it_drained():
    spk = speaker_with_fake_portaudio()

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(bytes(range(1, 256)) * 4))
        stream = spk._stream

        async def keep_pumping():
            while True:
                stream.pump()
                await asyncio.sleep(0.001)

        pump = asyncio.ensure_future(keep_pumping())
        try:
            await spk.stop()
        finally:
            pump.cancel()
        assert stream.stopped and stream.closed
        with pytest.raises(AudioError, match="not started"):
            await spk.play(b"\x01\x02")

    run(scenario())


def test_play_gives_up_when_the_card_stops_asking_for_audio():
    """In callback mode a stream PortAudio has stopped calling raises
    nothing at all, so the reply would sit silent and wait forever. 0.4
    raised from the write that failed."""
    spk = speaker_with_fake_portaudio(stall_grace_s=0.05)

    async def scenario():
        await started(spk)
        with pytest.raises(AudioError, match="stopped taking audio"):
            await spk.play(bytes(range(1, 256)) * 8)
        return list(spk._waiters)

    waiters = run(scenario())
    assert not waiters, "a waiter left behind would wake the next play early"


def test_small_chunks_still_get_a_whole_block_of_cushion():
    """A cushion of the chunk rather than the block bounds the queue at twice
    a small chunk, and then the callback pads every block with silence
    however promptly the caller comes back."""
    spk = speaker_with_fake_portaudio(rate=24000)
    tiny = b"\x01\x02" * 32  # 64 bytes against a 960-byte block

    async def scenario():
        await started(spk)
        # Nothing is pumped, so nothing is played: each of these has to
        # return on the cushion alone or wait for a card that never asks.
        for _ in range(14):
            await asyncio.wait_for(spk.play(tiny), 1.0)
        return spk.queued_bytes

    left = run(scenario())
    assert left == 14 * len(b"\x01\x02" * 32)
    assert left <= spk.block_bytes


def test_blocks_filled_from_an_empty_queue_are_counted():
    """PortAudio reports nothing when the queue runs dry: it was served on
    time, with silence. Somebody has to count that, or a gap in the middle
    of a word leaves no trace anywhere."""
    spk = speaker_with_fake_portaudio()

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(b"\x01\x02"))
        before = spk.padded
        spk._stream.pump()
        spk._stream.pump()
        return before, spk.padded

    before, after = run(scenario())
    assert after - before == 2


def test_the_stream_latency_is_recorded_when_it_opens():
    """It sits in front of every sound and is in no other number."""
    spk = speaker_with_fake_portaudio()

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(b"\x01\x02"))

    run(scenario())
    assert spk.stream_latency_s == pytest.approx(0.085)


# --------------------------------------------------------------------------
# Gain: `audio.output.gain_db`, which no sink read before 0.5
# --------------------------------------------------------------------------


def pcm_of(*samples: int) -> bytes:
    return array("h", samples).tobytes()


def samples_of(pcm: bytes) -> list[int]:
    out = array("h")
    out.frombytes(pcm)
    return list(out)


#: Samples between 12000 and 24000. -6 dB brings them to 6000 to 12000 and
#: +6 dB pushes the upper half past full scale. None is zero after either,
#: so trimming a drained stream's trailing silence cannot eat into them.
LOUD = array("h", (12000 + (i * 97) % 12000 for i in range(2000))).tobytes()


def test_minus_six_db_halves_every_sample():
    """10 ** (-6 / 20) is 0.5012, so half to within rounding. Worked by hand:
    32767 * 0.5012 is 16422.4, and -32768 * 0.5012 is -16422.9."""
    heard = samples_of(apply_gain(pcm_of(1000, -1000, 32767, -32768, 1, 0), -6.0))
    assert heard == [501, -501, 16422, -16423, 1, 0]


def test_a_six_db_boost_clips_at_the_rails_rather_than_wrapping():
    """+6 dB is 1.9953. 16000 doubles to 31924 and fits; 20000 would be
    39905, which wrapped round is a loud click, so it stops at full scale."""
    heard = samples_of(apply_gain(pcm_of(1000, 16000, 20000, -20000, -16500), 6.0))
    assert heard == [1995, 31924, 32767, -32768, -32768]


def test_zero_db_leaves_the_audio_untouched():
    pcm = pcm_of(1, -2, 3)
    assert apply_gain(pcm, 0.0) is pcm
    assert apply_gain(pcm, 0) is pcm


def test_the_gain_comes_from_the_manifests_output_block():
    """`gain_db` sits beside `device` in `audio.output`, outside `params`.
    Every example manifest sets it to -6.0, and until 0.5 no sink read it."""
    assert Speaker({"device": "USB", "gain_db": -6.0}).gain_db == -6.0
    assert Speaker({"device": "USB", "gain_db": None}).gain_db == 0.0
    assert Speaker({"device": "USB"}).gain_db == 0.0


@pytest.mark.parametrize("gain_db", [float("inf"), float("-inf"), float("nan"), 7000.0, "loud"])
def test_a_gain_that_is_not_a_number_of_decibels_is_refused_at_construction(gain_db):
    """Refused before the first sentence rather than by it. YAML spells
    infinity `.inf`, and 7000 dB is a number the schema accepts whose factor
    does not fit in a float, so `play()` would raise on every sentence."""
    with pytest.raises(AudioError, match="gain_db"):
        Speaker({"device": "USB", "gain_db": gain_db})


@pytest.mark.parametrize("gain_db", [-6.0, 0.0, 6.0])
def test_the_card_is_given_the_gained_samples(gain_db):
    spk = speaker_with_fake_portaudio(gain_db=gain_db)

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(LOUD))
        return drained(spk)

    heard = run(scenario())
    assert heard == apply_gain(LOUD, gain_db)
    assert (heard == LOUD) == (gain_db == 0.0)


def test_a_long_chunk_is_scaled_and_queued_a_block_at_a_time(monkeypatch):
    """A local voice hands over a whole sentence, seconds of audio, as one
    chunk. Scaled whole before any of it was queued, it could outlast the
    block left in hand on a Pi, and the card would play silence inside the
    reply. So each block is in the queue before the next one is scaled."""
    from emet_hal import audio as audio_module

    spk = speaker_with_fake_portaudio(gain_db=-6.0)
    real = audio_module.apply_gain
    seen: list[tuple[int, int]] = []

    def watched(pcm, gain_db):
        seen.append((len(pcm), spk.queued_bytes))
        return real(pcm, gain_db)

    monkeypatch.setattr(audio_module, "apply_gain", watched)

    async def scenario():
        await started(spk)
        await pumped(spk, spk.play(LOUD))
        return drained(spk)

    heard = run(scenario())
    assert heard == real(LOUD, -6.0)
    assert len(seen) == 7, "4000 bytes in 640-byte blocks"
    assert all(size <= spk.block_bytes for size, _ in seen)
    assert [queued for _, queued in seen] == [i * spk.block_bytes for i in range(7)]


def test_a_wav_sink_records_what_the_voice_made_whatever_the_gain(tmp_path):
    """The gain is how loud one speaker plays. Engine tests read the mock
    voice's words back out of this file, so it holds what the voice made."""
    path = tmp_path / "said.wav"
    block = {"sink": "wav", "gain_db": -6.0, "params": {"path": str(path)}}
    sink = WavSink(block, AudioFormat(sample_rate=16000))

    async def scenario():
        await sink.start()
        await sink.play(LOUD)
        await sink.stop()

    run(scenario())
    with wave.open(str(path)) as w:
        assert w.readframes(w.getnframes()) == LOUD


# --------------------------------------------------------------------------
# Device names, for the body's state file
# --------------------------------------------------------------------------


class FakeInputStream:
    """Stands in for a `RawInputStream`. Opens nothing and never calls back."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        pass

    def close(self):
        self.closed = True


#: Two one-way USB cards, named the way PortAudio names ALSA devices under
#: `plughw:`.
CARDS = [
    {
        "name": "USB PnP Sound Device: Audio (plughw:3,0)",
        "hostapi": 0,
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "name": "USB Audio Device: - (plughw:2,0)",
        "hostapi": 0,
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
]


def fake_portaudio(monkeypatch, cards=CARDS, *, default_input=0, default_output=1, nameless=False):
    """A `sounddevice` that lists `cards`, accepts every format and opens
    nothing. `nameless` makes every lookup of a single device fail, the way
    a card unplugged between listing and naming would."""

    def query_devices(device=None, kind=None):
        if device is None and kind is None:
            return list(cards)
        if nameless:
            raise RuntimeError("Error querying device")
        if device is None:
            device = default_input if kind == "input" else default_output
        return cards[device]

    sd = types.ModuleType("sounddevice")
    sd.query_devices = query_devices
    sd.query_hostapis = lambda: [{"name": "ALSA"}]
    sd.check_input_settings = lambda **kwargs: None
    sd.check_output_settings = lambda **kwargs: None
    sd.RawInputStream = FakeInputStream
    sd.RawOutputStream = FakeOutputStream
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    # `_sd()` sets this on Linux. Setting it here first means the test leaves
    # the environment as it found it.
    monkeypatch.setenv("PA_ALSA_PLUGHW", "1")
    return sd


async def named(device):
    """Start `device`, read its name, and stop it again."""
    await device.start()
    try:
        return device.device_name
    finally:
        await device.stop()


def test_nothing_is_named_before_start():
    assert MicrophoneSource({"device": "USB"}).device_name is None
    assert Speaker({"device": "USB"}).device_name is None


def test_a_named_device_is_recorded_by_the_name_portaudio_gave_it(monkeypatch):
    """"USB" in the manifest matched one card on this body and could match
    another on the next. The state file keeps which."""
    fake_portaudio(monkeypatch)
    assert run(named(MicrophoneSource({"device": "USB", "channels": 1}))) == CARDS[0]["name"]
    assert run(named(Speaker({"device": "USB"}))) == CARDS[1]["name"]


def test_the_default_device_is_recorded_with_the_card_it_stood_for(monkeypatch):
    """The default moves when a card is plugged in, so "default" alone would
    not say which card a run heard through."""
    fake_portaudio(monkeypatch)
    assert run(named(MicrophoneSource({}))) == f"default ({CARDS[0]['name']})"
    assert run(named(Speaker({"device": "default"}))) == f"default ({CARDS[1]['name']})"


def test_a_default_that_is_itself_called_default_is_named_once(monkeypatch):
    """ALSA's own default device is listed as "default"."""
    alsa = [{**CARDS[0], "name": "default", "max_input_channels": 32, "max_output_channels": 32}]
    fake_portaudio(monkeypatch, alsa, default_input=0, default_output=0)
    assert run(named(MicrophoneSource({}))) == "default"
    assert run(named(Speaker({}))) == "default"


def test_a_device_portaudio_cannot_name_still_starts(monkeypatch):
    """The name is a record. A boot that failed over a label would be the
    wrong trade, so the name is None and the device runs."""
    fake_portaudio(monkeypatch, nameless=True)

    async def scenario():
        mic = MicrophoneSource({"device": "USB"})
        await mic.start()
        opened = mic._stream.started
        await mic.stop()
        return mic.device_name, opened

    assert run(scenario()) == (None, True)
    assert run(named(Speaker({}))) is None
