"""The hardware floor: a microphone and a speaker.

Every fallback chain terminates in a voice rung, which is only a guarantee if
audio actually works. This module is what makes that true, and it is the one
piece of hardware a body cannot decline to have.

**Why this is not a plugin category.** Wake is one because engines get
discontinued: Picovoice disabled every free Porcupine access key on 30 June
2026. Audio devices do not work that way: PortAudio already abstracts ALSA,
WASAPI, and CoreAudio behind one interface, so the swap point that would
justify a category is already inside the dependency. `AudioSource` is a plain
Protocol instead, which is enough for a wav file to stand in for a microphone
without anything above noticing.

**On sample rates, which are the thing that will bite you.** The wake engine
needs 16 kHz mono. Almost no sound card runs at 16 kHz; they run at 44.1 or
48, so something must convert. Who does the converting varies:

* On Linux, an ALSA `plughw:` device converts for you. That is precisely what
  the `plug` layer is for, and it is why the manifests in this repository say
  `plughw:1,0` rather than `hw:1,0`. PortAudio lists and opens cards as `hw:`
  unless `PA_ALSA_PLUGHW=1` is in its environment when it starts, so `_sd()`
  below sets that on Linux before the first import. Cards are then listed as
  "(plughw:1,0)", which is also what makes those manifest names resolve.
* On Windows, shared-mode host APIs (MME, DirectSound, WASAPI) convert, and
  exclusive-mode WDM-KS refuses anything but the native rate.

So this module asks PortAudio whether a device will accept the rate, before
opening it, and refuses to start when the answer is no. It does not quietly
fall back to 48 kHz and feed that to a detector expecting 16 kHz: that path
does not fail, it just stops hearing you, which is the worst kind of bug in a
thing whose whole job is to listen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Any

from emet_sdk.types import SAMPLE_BYTES, AudioFormat, AudioSink, AudioSource

__all__ = [
    "AudioError",
    "AudioFormat",
    "AudioSource",
    "AudioSink",
    "Device",
    "MicrophoneSource",
    "NullSink",
    "Speaker",
    "WavSink",
    "WavSource",
    "devices",
    "resolve_device",
]

log = logging.getLogger("emet_hal.audio")


class AudioError(RuntimeError):
    """Audio could not be brought up, with a message a person can act on."""


@dataclass(frozen=True, slots=True)
class Device:
    index: int
    name: str
    inputs: int
    outputs: int
    default_rate: float
    host_api: str

    def __str__(self) -> str:
        kind = "in" if self.inputs else "out"
        return f"[{self.index}] {self.name} ({kind}, {self.host_api}, {self.default_rate:.0f} Hz)"


# --------------------------------------------------------------------------
# Finding a device
# --------------------------------------------------------------------------


def _sd() -> Any:
    # PortAudio's ALSA backend lists and opens each card as `hw:`, which
    # converts nothing, so a 48 kHz microphone refuses the detector's 16 kHz.
    # With PA_ALSA_PLUGHW=1 in its environment at initialisation it uses
    # `plughw:` throughout: ALSA's plug layer converts, and the names it
    # reports become "(plughw:1,0)", which is what the manifests in this
    # repository say. It has to be set before the first import, because
    # sounddevice initialises PortAudio on import. An explicit value already
    # in the environment wins. (PortAudio, src/hostapi/alsa/pa_linux_alsa.c,
    # BuildDeviceList.)
    if sys.platform.startswith("linux"):
        os.environ.setdefault("PA_ALSA_PLUGHW", "1")
    try:
        import sounddevice
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise AudioError(
            "sounddevice is not installed. It is an optional dependency: "
            "install `emet-hal[audio]`."
        ) from exc
    except OSError as exc:  # pragma: no cover - depends on the environment
        # sounddevice ships as a pure-Python wheel and loads PortAudio from the
        # system, so on Linux the import succeeds only if the shared library is
        # there. This is the common Raspberry Pi first-run failure.
        raise AudioError(
            f"sounddevice is installed but the PortAudio library it needs is not. "
            f"On Debian and Raspberry Pi OS: `sudo apt install libportaudio2`. "
            f"The loader said: {exc}"
        ) from exc
    return sounddevice


def devices(*, want_input: bool | None = None) -> list[Device]:
    """Every audio device PortAudio can see.

    `want_input=True` keeps only capture devices, `False` only playback, and
    `None` keeps both. Used by error messages, so it must never raise for a
    merely unusual device.
    """
    sd = _sd()
    apis = sd.query_hostapis()
    out: list[Device] = []
    for i, d in enumerate(sd.query_devices()):
        ins, outs = int(d["max_input_channels"]), int(d["max_output_channels"])
        if want_input is True and not ins:
            continue
        if want_input is False and not outs:
            continue
        if not ins and not outs:
            continue
        api = apis[d["hostapi"]]["name"] if d["hostapi"] < len(apis) else "?"
        out.append(
            Device(
                index=i,
                name=str(d["name"]),
                inputs=ins,
                outputs=outs,
                default_rate=float(d["default_samplerate"]),
                host_api=str(api),
            )
        )
    return out


def resolve_device(spec: str | int | None, *, want_input: bool) -> int | None:
    """Turn a manifest `device` string into a PortAudio index.

    Returns None for the system default, which is what an absent or "default"
    spec means.

    A manifest names a device on the body it was written for. `plughw:1,0` is
    correct on the Pi that manifest describes and means nothing on a laptop,
    so failing here is normal rather than exceptional and the message lists
    what this machine actually has.
    """
    if spec is None or spec == "" or spec == "default":
        return None
    if isinstance(spec, int):
        return spec
    text = str(spec).strip()
    if text.isdigit():
        return int(text)

    available = devices(want_input=want_input)
    for d in available:
        if d.name == text:
            return d.index
    hits = [d for d in available if text.lower() in d.name.lower()]
    if len(hits) == 1:
        return hits[0].index
    if len(hits) > 1:
        listing = "\n  ".join(str(d) for d in hits)
        raise AudioError(f"device {text!r} matches more than one device:\n  {listing}")

    listing = "\n  ".join(str(d) for d in available) or "  (none)"
    hint = ""
    if ":" in text or text.startswith(("hw", "plughw")):
        hint = (
            f"\n{text!r} is an ALSA name, so this manifest was written for a Linux "
            f"body. The manifest is fine; this is the wrong machine "
            f"for it. Set the device to one below, or run this on the body it "
            f"describes."
        )
    raise AudioError(
        f"no {'input' if want_input else 'output'} device matches {text!r}.{hint}\n"
        f"Available:\n  {listing}"
    )


def _check_rate(device: int | None, rate: int, channels: int, *, want_input: bool) -> None:
    """Ask PortAudio whether it will accept this format, without opening it.

    Deliberately separate from opening the stream: a format query does not
    touch the microphone, so a body can be diagnosed without recording anyone.
    """
    sd = _sd()
    check = sd.check_input_settings if want_input else sd.check_output_settings
    try:
        check(device=device, samplerate=rate, channels=channels, dtype="int16")
    except Exception as exc:
        name = "default" if device is None else str(device)
        native = ""
        try:
            info = sd.query_devices(device if device is not None else None)
            native = f" Its native rate is {float(info['default_samplerate']):.0f} Hz."
        except Exception:  # pragma: no cover - only when the device vanished
            pass
        raise AudioError(
            f"device {name} will not accept {rate} Hz, {channels}-channel int16.{native} "
            f"Some host APIs convert rates and some refuse: exclusive-mode drivers "
            f"(WDM-KS on Windows, `hw:` on Linux) give you the native rate only, while "
            f"shared-mode ones (MME, DirectSound, WASAPI, ALSA `plughw:`) convert. "
            f"Pick a device that converts rather than running the detector at the "
            f"wrong rate, which does not fail, it just stops hearing you. "
            f"PortAudio said: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def _downmix(raw: bytes, channels: int, *, mix: str) -> bytes:
    """Interleaved N-channel int16 to mono int16."""
    if channels == 1:
        return raw
    samples = array("h")
    samples.frombytes(raw)
    if mix == "average":
        out = array("h", [0]) * (len(samples) // channels)
        for i in range(len(out)):
            block = samples[i * channels : (i + 1) * channels]
            out[i] = sum(block) // channels
        return out.tobytes()
    return samples[0::channels].tobytes()


class WavSource:
    """A wav file pretending to be a microphone.

    Not only for tests, though it is good for those. It is how you reproduce a
    wake failure someone reports: they send the recording, you run the same
    detector over the same audio and get the same answer, with no hardware
    involved on either end.

    Refuses to resample or reinterpret. A file at the wrong rate is a wrong
    file, and saying so is more useful than silently feeding 44.1 kHz audio to
    a detector expecting 16.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        fmt: AudioFormat | None = None,
        *,
        mix: str = "first",
    ) -> None:
        """`config` is the manifest's `audio.input` block; the file is
        `params.path`. Same signature as `MicrophoneSource` because both are
        built by name through `emet.audio`, and the engine that builds them
        knows only the name."""
        self.config = dict(config or {})
        self.path = str((self.config.get("params") or {}).get("path") or "")
        self.format = fmt or AudioFormat()
        self._mix = mix
        self._wav: wave.Wave_read | None = None

    @classmethod
    def from_path(cls, path: str, fmt: AudioFormat | None = None, *, mix: str = "first"):
        """Build one directly. For tests and for anything holding a real path
        rather than a manifest."""
        return cls({"source": "wav", "params": {"path": path}}, fmt, mix=mix)

    async def start(self) -> None:
        if not self.path:
            raise AudioError(
                "the wav source needs a file: set `audio.input.params.path` to a "
                "16-bit mono PCM wav at the rate the consumer asks for."
            )
        try:
            wav = wave.open(self.path, "rb")
        except Exception as exc:
            raise AudioError(f"could not open {self.path!r}: {exc}") from exc
        if wav.getsampwidth() != SAMPLE_BYTES:
            wav.close()
            raise AudioError(
                f"{self.path!r} is {wav.getsampwidth() * 8}-bit; this expects 16-bit PCM"
            )
        if wav.getframerate() != self.format.sample_rate:
            rate = wav.getframerate()
            wav.close()
            raise AudioError(
                f"{self.path!r} is {rate} Hz and this expects "
                f"{self.format.sample_rate} Hz. Convert the file rather than "
                f"having the detector hear it at the wrong speed."
            )
        self._wav = wav

    async def read(self) -> bytes | None:
        if self._wav is None:
            return None
        channels = self._wav.getnchannels()
        raw = self._wav.readframes(self.format.frame_samples)
        if len(raw) < self.format.frame_samples * channels * SAMPLE_BYTES:
            return None  # a partial trailing frame ends the stream
        return _downmix(raw, channels, mix=self._mix)

    async def stop(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


class MicrophoneSource:
    """Live capture, downmixed to mono and handed over one frame at a time.

    PortAudio calls back on its own thread; frames cross into asyncio through a
    bounded queue. When the consumer falls behind the oldest frame is dropped
    and counted, because the alternative is an ever-growing backlog and a robot
    that answers a question from a minute ago. `dropped` matters:
    a non-zero value means wake words were missed, and the engine should say so
    rather than let it pass as bad luck.
    """

    #: Roughly two seconds at the default frame size. Long enough to ride out a
    #: garbage collection, short enough that a real stall is visible.
    QUEUE_FRAMES = 25

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        fmt: AudioFormat | None = None,
        *,
        mix: str = "first",
    ) -> None:
        """`config` is the manifest's `audio.input` block."""
        self.config = dict(config or {})
        self.format = fmt or AudioFormat()
        self.dropped = 0
        #: Frames the card lost before this code saw them: PortAudio reported
        #: an input overflow. A different cause from `dropped`, the same loss.
        self.overflows = 0
        # A mic array is downmixed here so nothing downstream counts capsules.
        # Channel 0 by default rather than an average: on a ReSpeaker-style
        # array that channel carries the hardware-processed output, and
        # averaging beamformed audio with raw capsules is worse than either.
        self._mix = mix
        self._channels = int(self.config.get("channels") or 1)
        self._stream: Any = None
        self._queue: asyncio.Queue[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        sd = _sd()
        device = resolve_device(self.config.get("device"), want_input=True)
        _check_rate(device, self.format.sample_rate, self._channels, want_input=True)

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.QUEUE_FRAMES)

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.format.sample_rate,
                blocksize=self.format.frame_samples,
                device=device,
                channels=self._channels,
                dtype="int16",
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioError(f"could not open the microphone: {exc}") from exc

    def _on_audio(self, indata, frames, time_info, status) -> None:
        """PortAudio's thread. Hands the frame to the event loop and counts
        what the card says it lost.

        `input_overflow` means PortAudio's own buffer filled before this
        callback ran, so audio was gone before the queue ever saw it. It is
        counted apart from `dropped` because the cause differs: a stalled
        thread rather than a slow loop. Either way the audio clock falls
        behind the wall clock, so a live run's skew has to be read against
        this number before it is called drift.
        """
        if status:
            log.debug("audio input status: %s", status)
            if getattr(status, "input_overflow", False):
                self.overflows += 1
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, bytes(indata))

    def _offer(self, raw: bytes) -> None:
        """On the event loop thread. Never blocks; drops the oldest instead.

        A frame can arrive after `stop()`: PortAudio's thread schedules this
        call before the stream closes and the loop runs it afterwards. That
        frame belongs to nobody, so it is discarded rather than asserted on.
        """
        if self._queue is None:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - races with a reader
                pass
        self._queue.put_nowait(_downmix(raw, self._channels, mix=self._mix))

    async def read(self) -> bytes | None:
        if self._queue is None:
            return None
        return await self._queue.get()

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._queue = None


def _wake(fut: "asyncio.Future[None]") -> None:
    """Resolve a `play()`'s future once. Runs on the event loop's thread."""
    if not fut.done():
        fut.set_result(None)


class Speaker:
    """Playback through a real sound card, so a voice rung is a sound rather
    than a promise.

    **One stream, kept open.** `start()` checks the device and the rate; the
    first `play()` opens one PortAudio output stream, and it stays open until
    `stop()`. PortAudio pulls from a queue through a callback on its own
    thread: whatever `play()` has queued, then zeros when the queue is empty.
    A stream that never closes cannot click between buffers, and a stream fed
    zeros rather than starved never underruns, so a sentence can be handed
    over a chunk at a time as a voice produces it and a gap between chunks is
    silence. That is what lets the engine play a cloud voice's first chunk
    before its last has arrived. 0.4 opened a stream per buffer, which
    clicked between chunks, so the engine gathered each sentence first.

    `play()` returns once no more than one block of audio is left unplayed,
    which is the old promise less one block and the card's own output
    latency. The block left in hand is what the caller's next chunk lands
    behind, so a sentence handed over in pieces is one continuous sound. The
    cushion is a whole block whatever the chunk size: a cushion of the chunk
    instead would cap the queue at twice a small chunk and have the callback
    pad every block with silence.

    `cancel()` empties the queue, so playback stops within a block plus that
    latency, and the stream stays open. That is barge-in. `stop()` is not:
    it waits out what is queued before closing, because the block `play()`
    left in hand is the end of the last word.

    Deliberately small otherwise. It takes int16 PCM at the format it was
    given and plays it; how that audio came to exist is the business of
    whatever synthesises speech.
    """

    #: Milliseconds of audio the callback asks for at a time. Smaller starts
    #: a sentence sooner and costs a Python call more often; 20 ms is fifty
    #: calls a second, which a Pi 5 does not notice. `params.block_ms`
    #: overrides it.
    BLOCK_MS = 20.0

    #: Seconds of slack on top of the audio already queued before `play()`
    #: decides the card has stopped taking audio. The queue drains in real
    #: time, so anything beyond this is a stream PortAudio is no longer
    #: calling. Without a bound the reply goes silent and waits forever;
    #: 0.4 raised from the write that failed instead.
    STALL_GRACE_S = 5.0

    def __init__(self, config: dict[str, Any] | None = None, fmt: AudioFormat | None = None) -> None:
        """`config` is the manifest's `audio.output` block."""
        self.config = dict(config or {})
        # 22.05 kHz rather than the 16 kHz of the input side: this is a
        # synthesis rate, and speech generated at 16 kHz sounds like a
        # telephone. The two directions are unrelated and need not agree.
        self.format = fmt or AudioFormat(sample_rate=22050)
        params = self.config.get("params") or {}
        self.block_ms = float(params.get("block_ms") or self.BLOCK_MS)
        #: PortAudio's stream latency: "low", "high", or seconds. None takes
        #: sounddevice's default, which is the device's high latency. A knob
        #: rather than a measured choice; `params.latency` sets it.
        self.latency: Any = params.get("latency")
        #: Seconds of slack before `play()` calls the stream dead.
        #: `params.stall_grace_s` overrides it.
        self.stall_grace_s = float(params.get("stall_grace_s") or self.STALL_GRACE_S)
        self._device: int | None = None
        self._sd: Any = None
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._opening: asyncio.Lock | None = None
        # The queue and its bookkeeping are shared with PortAudio's thread.
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._queued = 0
        self._played = 0
        self._closed = False
        self._waiters: deque[tuple[int, asyncio.Future[None]]] = deque()
        #: Callbacks PortAudio reported late: it had to insert a gap because
        #: this process did not service the card in time. Zero on a machine
        #: that keeps up. It says nothing about whether the queue ran dry,
        #: which is what `padded` counts.
        self.underflows = 0
        #: Blocks the callback had to fill with silence because the queue was
        #: empty. Normal between replies, when nobody is speaking. Inside a
        #: sentence it is a gap a person hears, and the engine reads this
        #: before and after a sentence to say so.
        self.padded = 0
        #: The stream's own output latency, in seconds, as PortAudio reported
        #: it when the stream opened. None until then. It sits in front of
        #: every sound and is not in any other number.
        self.stream_latency_s: float | None = None

    @property
    def sample_rate(self) -> int:
        return self.format.sample_rate

    @property
    def block_samples(self) -> int:
        return max(1, int(self.sample_rate * self.block_ms / 1000.0))

    @property
    def block_bytes(self) -> int:
        return self.block_samples * SAMPLE_BYTES

    @property
    def queued_bytes(self) -> int:
        """Audio handed over and not yet given to the card."""
        with self._lock:
            return len(self._pending)

    def _seconds(self, nbytes: int) -> float:
        return nbytes / float(self.sample_rate * SAMPLE_BYTES)

    async def start(self) -> None:
        self._sd = _sd()
        self._device = resolve_device(self.config.get("device"), want_input=False)
        _check_rate(self._device, self.sample_rate, 1, want_input=False)
        self._loop = asyncio.get_running_loop()
        self._opening = asyncio.Lock()
        with self._lock:
            self._closed = False

    def _open(self) -> None:
        """Open and start the one stream. Blocking, so it runs off the loop.

        Two ways this ends badly, both of which leave a card held open until
        the process exits, because sounddevice has no finaliser that closes a
        stream nobody references. A stream that opens and will not start is
        closed here. A stream that starts after `stop()` has already run is
        closed here too: `stop()` looked for a stream and found none, so this
        thread owns the one it just made.
        """
        extra: dict[str, Any] = {}
        if self.latency is not None:
            extra["latency"] = self.latency
        stream = None
        try:
            stream = self._sd.RawOutputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_samples,
                channels=1,
                dtype="int16",
                device=self._device,
                callback=self._on_output,
                **extra,
            )
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:  # pragma: no cover - already broken
                    pass
            raise AudioError(f"could not open the speaker: {exc}") from exc

        with self._lock:
            orphan = stream if self._closed else None
            if orphan is None:
                self._stream = stream
        if orphan is not None:
            _shut(orphan)
            return
        try:
            self.stream_latency_s = float(stream.latency)
        except Exception:  # pragma: no cover - a backend that reports none
            self.stream_latency_s = None
        log.debug(
            "speaker: one stream at %d Hz, %.0f ms blocks, %s s of output latency",
            self.sample_rate,
            self.block_ms,
            self.stream_latency_s,
        )

    def _on_output(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        """PortAudio's thread. Fill the block from the queue, zeros after.

        Never raises: an exception here aborts the stream, and a stream that
        has to be reopened is the click this design exists to remove.
        """
        try:
            if status and getattr(status, "output_underflow", False):
                self.underflows += 1
            want = len(outdata)
            ready: list[asyncio.Future[None]] = []
            with self._lock:
                take = bytes(self._pending[:want])
                del self._pending[: len(take)]
                self._played += len(take)
                if len(take) < want:
                    self.padded += 1
                while self._waiters and self._waiters[0][0] <= self._played:
                    ready.append(self._waiters.popleft()[1])
            outdata[: len(take)] = take
            if len(take) < want:
                outdata[len(take) :] = bytes(want - len(take))
            loop = self._loop
            if loop is not None:
                for fut in ready:
                    try:
                        loop.call_soon_threadsafe(_wake, fut)
                    except RuntimeError:  # pragma: no cover - the loop is closing
                        pass
        except Exception:  # pragma: no cover - a block that cannot be filled
            log.exception("speaker callback failed")

    async def play(self, pcm: bytes) -> None:
        """Queue mono int16 PCM behind whatever is already queued, and return
        once no more than one block of it is left unplayed.

        That block is left in hand on purpose: the caller's next `play()`
        lands behind it, so a sentence handed over a chunk at a time is one
        continuous sound. What a person hears ends one block plus the card's
        output latency after this returns.
        """
        if self._sd is None or self._loop is None or self._opening is None:
            raise AudioError("speaker was not started")
        if len(pcm) % SAMPLE_BYTES:
            raise AudioError("int16 audio has an even number of bytes")
        if not pcm:
            return
        async with self._opening:
            if self._stream is None:
                await asyncio.to_thread(self._open)
        with self._lock:
            if self._closed:
                # `stop()` ran while this call was opening the device.
                raise AudioError("speaker was stopped")
            self._pending += pcm
            self._queued += len(pcm)
            # A whole block of cushion whatever the chunk size. Cushioning by
            # the chunk instead would bound the queue at twice a small chunk,
            # and the callback would pad every block with silence however
            # promptly the caller came back.
            target = self._queued - self.block_bytes
            ahead = len(self._pending)
            if target <= self._played:
                return
            fut: asyncio.Future[None] = self._loop.create_future()
            self._waiters.append((target, fut))
        patience = self._seconds(ahead) + self.stall_grace_s
        try:
            await asyncio.wait_for(fut, timeout=patience)
        except asyncio.TimeoutError:
            with self._lock:
                self._waiters = deque(w for w in self._waiters if w[1] is not fut)
            raise AudioError(
                f"the speaker stopped taking audio: {self.queued_bytes} byte(s) still "
                f"queued after {patience:.1f} s. The stream is open and PortAudio is no "
                f"longer calling for audio."
            ) from None

    async def cancel(self) -> None:
        """Stop within a block: empty the queue and free every waiting
        `play()`. The stream stays open, playing silence, so the next
        sentence starts without a click.

        What PortAudio already holds, one block plus the card's output
        latency, is still heard. That is the floor for barge-in.
        """
        with self._lock:
            dropped = len(self._pending)
            self._pending.clear()
            self._played += dropped
            waiting = list(self._waiters)
            self._waiters.clear()
        for _, fut in waiting:
            _wake(fut)

    async def drain(self) -> None:
        """Wait out the audio still queued, and give up rather than hang.

        The queue plays in real time, so the wait is the length of what is in
        it plus a block. A callback that has stopped running leaves this
        returning late rather than never.
        """
        left = self.queued_bytes
        if left:
            await asyncio.sleep(self._seconds(left) + self.block_ms / 1000.0)

    async def stop(self) -> None:
        """Let what is queued finish, then close the one stream.

        Not `cancel()`. `play()` returns with a block still in hand so the
        next chunk lands behind it, and at the end of a reply there is no
        next chunk: cancelling here would cut the end of the last word.
        """
        with self._lock:
            self._closed = True
            stream, self._stream = self._stream, None
        if stream is not None:
            await self.drain()
            await asyncio.to_thread(_shut, stream)
        await self.cancel()
        self._sd = None
        self._opening = None


def _shut(stream: Any) -> None:
    """Stop a PortAudio stream and close it, whatever `stop()` does.

    `stream.stop()` waits for PortAudio's own buffers to play out; `close()`
    is what actually gives the card back, and it has to happen even when the
    stop fails, or the device stays claimed until the process exits.
    """
    try:
        stream.stop()
    except Exception:  # pragma: no cover - a stream already gone
        pass
    finally:
        try:
            stream.close()
        except Exception:  # pragma: no cover - a stream already gone
            pass

class WavSink:
    """Writes what the robot said to a file instead of playing it.

    The counterpart to `WavSource`, and useful for the same reason: a bug
    report about what the robot *said* is reproducible if the audio still
    exists. It is also the only sink that can be asserted on in a test.
    """

    def __init__(self, config: dict[str, Any] | None = None, fmt: AudioFormat | None = None) -> None:
        self.config = dict(config or {})
        self.format = fmt or AudioFormat(sample_rate=22050)
        self.path = str((self.config.get("params") or {}).get("path") or "")
        self._wav: wave.Wave_write | None = None

    async def start(self) -> None:
        if not self.path:
            raise AudioError(
                "the wav sink needs a destination: set `audio.output.params.path`."
            )
        try:
            wav = wave.open(self.path, "wb")
        except Exception as exc:
            raise AudioError(f"could not open {self.path!r} for writing: {exc}") from exc
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_BYTES)
        wav.setframerate(self.format.sample_rate)
        self._wav = wav

    async def play(self, pcm: bytes) -> None:
        if self._wav is None:
            raise AudioError("wav sink was not started")
        self._wav.writeframes(pcm)

    async def cancel(self) -> None:
        """Nothing to interrupt: a file is written as fast as it is handed
        over, so there is never playback in progress to stop."""

    async def stop(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


class NullSink:
    """Accepts audio and discards it, counting what it was given.

    Useful beyond tests, though essential there: a suite that plays sound
    through whatever happens to be plugged in is as rude as one that records.
    It is also how you run the loop on a laptop at midnight, and how a body
    with no speaker attached still satisfies the contract that every chain
    terminates in a voice rung.
    """

    def __init__(self, config: dict[str, Any] | None = None, fmt: AudioFormat | None = None) -> None:
        self.config = dict(config or {})
        self.format = fmt or AudioFormat(sample_rate=22050)
        #: Bytes discarded. The only evidence this sink leaves behind.
        self.written = 0
        self.cancelled = 0

    async def start(self) -> None:
        pass

    async def play(self, pcm: bytes) -> None:
        self.written += len(pcm)

    async def cancel(self) -> None:
        self.cancelled += 1

    async def stop(self) -> None:
        pass
