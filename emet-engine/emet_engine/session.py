"""The listen loop: audio in, wake events out.

This is the first thing in Emet that is neither a contract nor a driver. It
takes a body manifest and a soul bundle, brings up the two pieces of hardware
the floor guarantees, and produces an event each time the robot hears its name.

**It imports `emet_sdk` and nothing else.** Microphones and wake detectors live
in `emet_hal`, and this module never names that package. Both arrive by name
through entry-point discovery, which is the only reason a layering rule that
forbids the import and an engine that needs a microphone can both be true.

**Order matters at start-up, and not the obvious way round.** The detector is
brought up first and asked what audio it needs, and the source is then
configured to match. Doing it the other way — opening the microphone at
whatever rate the manifest mentions and hoping the detector agrees — is how a
robot ends up running perfectly and hearing nothing, because feeding 48 kHz
audio to a 16 kHz model does not raise anything. It just stops working.

**The boot check that has no fallback.** Every other missing piece degrades: a
chain that cannot find a head falls through to a light ring, and one that finds
nothing still speaks. A robot that cannot hear its own name has no next rung,
so `start()` refuses rather than running deaf. That check is the entire reason
`WakeDescriptor.can_detect` exists.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Mapping

from emet_sdk.discovery import PluginRegistry
from emet_sdk.types import AudioFormat, AudioSink, AudioSource, WakeDescriptor, WakeEvent
from emet_sdk.validate import (
    DEFAULT_AUDIO_SINK,
    DEFAULT_AUDIO_SOURCE,
    DEFAULT_WAKE_ENGINE,
)

from emet_engine.metrics import SessionStats, Stopwatch
from emet_engine.turn import DEFAULT_PATIENCE_MS, Endpointer, Utterance

__all__ = ["EngineError", "ListenSession"]

log = logging.getLogger("emet_engine.session")


class EngineError(RuntimeError):
    """The engine cannot run, with a message naming what to fix."""


class ListenSession:
    """One run of the listen loop, for one body and one soul.

    Usage is two lines once it is started:

        async for event in session.wakes():
            ...

    The iterator ends when the source does, which a file always does and a
    microphone never should.
    """

    def __init__(
        self,
        manifest: Mapping[str, Any],
        soul: Mapping[str, Any],
        *,
        registry: PluginRegistry | None = None,
    ) -> None:
        self.manifest = manifest
        self.soul = soul
        self.registry = registry or PluginRegistry.discover()

        audio = manifest.get("audio") or {}
        self._input_block: Mapping[str, Any] = audio.get("input") or {}
        self._wake_block: Mapping[str, Any] = audio.get("wake") or {}
        self._output_block: Mapping[str, Any] = audio.get("output") or {}
        self.phrase = str((soul.get("identity") or {}).get("wake_word") or "")

        self.engine_name = str(self._wake_block.get("engine") or DEFAULT_WAKE_ENGINE)
        self.source_name = str(self._input_block.get("source") or DEFAULT_AUDIO_SOURCE)
        self.sink_name = str(self._output_block.get("sink") or DEFAULT_AUDIO_SINK)

        # Turn-taking is a persona trait, not engine tuning: a reflective soul
        # waits longer than an eager one, and that difference is the whole
        # reason the number lives on the soul rather than in this file.
        interaction = soul.get("interaction") or {}
        self.patience_ms = int(interaction.get("patience_ms") or DEFAULT_PATIENCE_MS)

        self._wake: Any = None
        self._audio: AudioSource | None = None
        self._sink: AudioSink | None = None
        self.descriptor: WakeDescriptor | None = None
        self.format: AudioFormat | None = None
        #: Whether the loop is keeping up. Populated as it runs; see
        #: `emet_engine.metrics` for why the real-time factor is the number
        #: that decides whether a body can run Emet at all.
        self.stats = SessionStats()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not self.phrase:
            raise EngineError(
                "the soul bundle has no `identity.wake_word`, so there is "
                "nothing to listen for."
            )

        self._wake = self._build_wake()
        await self._wake.start()

        self.descriptor = self._wake.describe()
        if not self.descriptor.can_detect(self.phrase):
            raise EngineError(self._deaf_message())

        # The detector states the contract; the source is made to fit it.
        self.format = AudioFormat(
            sample_rate=self.descriptor.sample_rate,
            frame_samples=self.descriptor.frame_samples,
        )

        self.stats.frame_ms = self.format.frame_ms

        source_cls = self.registry.load_audio(self.source_name)
        self._audio = source_cls(self._input_block, self.format)
        await self._audio.start()

        # The output rate is not the input rate and has no reason to be: one is
        # what the detector needs, the other is what synthesis produces.
        self.sink_format = AudioFormat(
            sample_rate=int(self._output_block.get("sample_rate") or 22050)
        )
        sink_cls = self.registry.load_audio_out(self.sink_name)
        self._sink = sink_cls(self._output_block, self.sink_format)
        await self._sink.start()

        log.info(
            "listening for %r via %s on %s at %d Hz",
            self.phrase,
            self.engine_name,
            self.source_name,
            self.format.sample_rate,
        )

    def _build_wake(self) -> Any:
        wake_cls = self.registry.load_wake(self.engine_name)
        return wake_cls(self._wake_block, self.phrase)

    def _deaf_message(self) -> str:
        """Say what is wrong in the terms the person can act on.

        A health detail from the plugin is more specific than anything this
        layer could invent, so prefer it and fall back to naming the phrase.
        """
        detail = ""
        health = self._wake.health()
        if not health.ok and health.detail:
            detail = f" {health.detail}"
        return (
            f"the wake engine {self.engine_name!r} cannot hear {self.phrase!r}, so this "
            f"robot would never answer to its own name. Unlike a missing head there is "
            f"nothing to fall back to, so it will not start.{detail}"
        )

    async def stop(self) -> None:
        """Safe to call twice, and safe if `start()` raised part way through."""
        if self._sink is not None:
            await self._sink.stop()
            self._sink = None
        if self._audio is not None:
            await self._audio.stop()
            self._audio = None
        if self._wake is not None:
            await self._wake.shutdown()
            self._wake = None

    async def __aenter__(self) -> "ListenSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ---------------------------------------------------------------- speech

    async def say(self, pcm: bytes) -> None:
        """Play mono int16 audio at the sink's rate.

        The whole of Emet's output side for now. 0.4 puts speech synthesis in
        front of it; nothing below this line needs to change when it does,
        which is the point of the sink being a discovered contract rather than
        a speaker this module imported.
        """
        if self._sink is None:
            raise EngineError("session was not started")
        await self._sink.play(pcm)

    async def hush(self) -> None:
        """Stop talking immediately, mid-word.

        Barge-in is built on this: `DESIGN.md` §13 requires that speech during
        playback interrupts rather than queues. Nothing calls it yet, and the
        sink contract carries it from the start so that adding barge-in later
        is engine work rather than a breaking change to every sink.
        """
        if self._sink is not None:
            await self._sink.cancel()

    # ----------------------------------------------------------------- loop

    async def wakes(self) -> AsyncIterator[WakeEvent]:
        """Yield an event each time the phrase is heard.

        The detector is reset after every event rather than before the next
        read: its hypothesis persists until the utterance is closed, so without
        this one wake becomes one event per frame for the rest of the session.
        """
        if self._audio is None or self._wake is None:
            raise EngineError("session was not started")

        while True:
            frame = await self._audio.read()
            if frame is None:
                return
            # Timed around the work only. The wait for audio above is not
            # work, and counting it would flatter every measurement.
            with Stopwatch() as watch:
                event = await self._wake.process(frame)
            self._record(watch.elapsed_ms)
            if event is not None:
                self.stats.wakes += 1
                yield event
                await self._wake.reset()

    async def turns(self) -> AsyncIterator[tuple[WakeEvent, Utterance]]:
        """Yield the wake and the speech that followed it, once per turn.

        While an utterance is being captured the wake detector is not fed. That
        is deliberate: the phrase often appears inside what somebody then says
        ("hey emet, what did you mean, hey emet is a silly name"), and a
        detector still listening would start a second turn inside the first.
        """
        if self._audio is None or self._wake is None or self.format is None:
            raise EngineError("session was not started")

        endpointer: Endpointer | None = None
        pending: WakeEvent | None = None

        while True:
            frame = await self._audio.read()
            if frame is None:
                if endpointer is not None and pending is not None:
                    # The source ran out mid-turn. Hand over what was caught
                    # rather than dropping it: a truncated question is still
                    # more useful than silence.
                    yield pending, endpointer.close()
                return

            if endpointer is None:
                with Stopwatch() as watch:
                    event = await self._wake.process(frame)
                self._record(watch.elapsed_ms)
                if event is not None:
                    self.stats.wakes += 1
                    await self._wake.reset()
                    pending = event
                    endpointer = Endpointer(self.format, patience_ms=self.patience_ms)
                continue

            with Stopwatch() as watch:
                utterance = endpointer.feed(frame)
            self._record(watch.elapsed_ms)
            if utterance is not None:
                assert pending is not None
                self.stats.turns += 1
                yield pending, utterance
                endpointer = None
                pending = None

    # -------------------------------------------------------------- honesty

    def _record(self, process_ms: float) -> None:
        self.stats.record_frame(process_ms)
        # Pulled from the source each frame rather than read once at the end:
        # a run that is killed part way through should still say what it lost.
        self.stats.dropped = self.dropped

    @property
    def dropped(self) -> int:
        """Frames the source discarded because this loop fell behind.

        Read defensively: `AudioSource` does not require it, and a file cannot
        drop anything. Non-zero means wake words were missed, and a robot that
        knows it missed something should be able to say so rather than let it
        pass as bad luck.
        """
        return int(getattr(self._audio, "dropped", 0) or 0)
