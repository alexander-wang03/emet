"""The listen loop: audio in, wake events out, and since 0.4 the words after.

This is the first thing in Emet that is neither a contract nor a driver. It
takes a body manifest and a soul bundle, brings up the two pieces of hardware
the floor guarantees, and produces an event each time the robot hears its name.
Asked to, it also hands the speech that follows to a recogniser and returns
what was said, hands what was said to a language model and streams what the
robot would answer, and hands the answer, a sentence at a time as it
streams, to a voice and out through the speaker.

**It imports `emet_sdk` and nothing else.** Microphones and wake detectors live
in `emet_hal`, transcribers, language models and voices in `emet_providers`,
and this module never names either package. All of them arrive by name
through entry-point discovery, which is the only reason a layering rule that
forbids the import and an engine that needs a microphone can both be true.

**Order matters at start-up, and not the obvious way round.** The detector is
brought up first and asked what audio it needs, and the source is then
configured to match. Doing it the other way, opening the microphone at
whatever rate the manifest mentions and hoping the detector agrees, is how a
robot ends up running perfectly and hearing nothing, because feeding 48 kHz
audio to a 16 kHz model does not raise anything. It just stops working. The
transcriber is built between the two: it is handed the detector's format,
reports the rate it will actually run at, and a mismatch is refused before the
microphone is ever opened. The output side runs the same rule the other way
round: the voice states the rate its audio arrives at, and the speaker is
opened to match, so a local voice that produces one rate and only one is
never resampled by the engine.

**The boot check that has no fallback.** Every other missing piece degrades: a
chain that cannot find a head falls through to a light ring, and one that finds
nothing still speaks. A robot that cannot hear its own name has no next rung,
so `start()` refuses rather than running deaf. That check is the entire reason
`WakeDescriptor.can_detect` exists. A transcriber that cannot start is refused
on the same terms: what people say would go nowhere, and P0 fails loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Callable, Mapping

from emet_sdk.discovery import PluginRegistry
from emet_sdk.models import chat_selection, stt_selection, tts_selection
from emet_sdk.types import (
    AudioFormat,
    AudioSink,
    AudioSource,
    Intent,
    LanguageModelDescriptor,
    ReplyDone,
    ReplyEvent,
    TextDelta,
    Transcript,
    TranscriberDescriptor,
    VoiceDescriptor,
    WakeDescriptor,
    WakeEvent,
)
from emet_sdk.validate import (
    DEFAULT_AUDIO_SINK,
    DEFAULT_AUDIO_SOURCE,
    DEFAULT_WAKE_ENGINE,
)

from emet_engine.intent_tags import IntentTags
from emet_engine.metrics import SessionStats, Stopwatch
from emet_engine.prompting import DEFAULT_MAX_TOKENS, Conversation, persona_lines, system_prompt
from emet_engine.speech import Mouth, Sentences, SpokenSentence
from emet_engine.turn import DEFAULT_PATIENCE_MS, Endpointer, Utterance, looks_incomplete

__all__ = ["EngineError", "ListenSession", "Exchange"]

log = logging.getLogger("emet_engine.session")


class EngineError(RuntimeError):
    """The engine cannot run, with a message naming what to fix."""


@dataclass(frozen=True, slots=True)
class Exchange:
    """One turn of `talk()`: what was heard, what was answered, what was said.

    `reply` is None for a turn with no words in it. `line` is a fallback
    from the soul's `persona.lines` that was spoken (after a refusal, a
    failure, or a false wake), or None. `intents` are the tags the model
    put in its text, lifted out before the words were spoken.
    """

    wake: WakeEvent
    utterance: Utterance
    reply: ReplyDone | None
    spoken: tuple[SpokenSentence, ...]
    line: str | None = None
    intents: tuple[Intent, ...] = ()

    @property
    def heard(self) -> str:
        """The final transcript, or an empty string."""
        return self.utterance.transcript.text.strip() if self.utterance.transcript else ""


class ListenSession:
    """One run of the listen loop, for one body and one soul.

    Usage is two lines once it is started:

        async for event in session.wakes():
            ...

    The iterator ends when the source does, which a file always does and a
    microphone never should.

    `turns()` yields once per turn, after the person has stopped talking. Two
    callbacks let a caller show something while the turn is still going:
    `on_wake` fires the moment the name is heard, and `on_partial` fires with
    each partial transcript while a transcriber is listening.

    `answer(text)` is the next step, taken by the caller after a turn: the
    words go to the language model with the persona and the conversation so
    far, and the reply streams back as events. The caller decides whether to
    print it, speak it, or both. `answer_aloud(text)` is both: the same
    events, and each sentence spoken through the voice as it completes.
    """

    def __init__(
        self,
        manifest: Mapping[str, Any],
        soul: Mapping[str, Any],
        *,
        registry: PluginRegistry | None = None,
        transcribe: bool = False,
        reply: bool = False,
        speak: bool = False,
        on_wake: Callable[[WakeEvent], None] | None = None,
        on_partial: Callable[[Transcript], None] | None = None,
        on_extended: Callable[[str], None] | None = None,
        on_intent: Callable[[Intent], None] | None = None,
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

        #: The provider reference the soul and the body agree on, or None when
        #: neither names one. Resolved here, unconditionally, so that a caller
        #: can report what *would* run without asking for it to run.
        self.stt = stt_selection(manifest, soul)
        self.stt_name: str | None = self.stt["provider"] if self.stt else None
        #: Whether to build the transcriber and feed it. Off by default: the
        #: 0.3 loop, unchanged, for anyone who only wants to know that the
        #: robot heard its name.
        self.transcribe = transcribe
        self.on_wake = on_wake
        self.on_partial = on_partial
        #: Fires with the words so far when a turn's silence window is
        #: extended because they looked unfinished.
        self.on_extended = on_extended
        #: Fires with each intent the model tagged into its reply.
        self.on_intent = on_intent

        #: The language model the soul and the body agree on, and whether to
        #: bring it up. The persona becomes the system prompt here, once; the
        #: conversation accumulates across turns for the length of the run.
        self.chat = chat_selection(manifest, soul)
        self.chat_name: str | None = self.chat["provider"] if self.chat else None
        self.reply = reply
        self.system_prompt = system_prompt(soul)
        self.conversation = Conversation()

        #: The voice the soul and the body agree on, and whether to bring it
        #: up. The soul's `voice` block (its speaking rate) reaches the
        #: plugin beside the provider reference.
        self.tts = tts_selection(manifest, soul)
        self.tts_name: str | None = self.tts["provider"] if self.tts else None
        self.speak = speak
        self._voice_block: Mapping[str, Any] = soul.get("voice") or {}

        # Turn-taking is a persona trait, not engine tuning: a reflective soul
        # waits longer than an eager one, and that difference is the whole
        # reason the number lives on the soul rather than in this file.
        interaction = soul.get("interaction") or {}
        self.patience_ms = int(interaction.get("patience_ms") or DEFAULT_PATIENCE_MS)
        #: The trailing clause: wait one more window when the words so far
        #: look unfinished. Needs a transcriber to judge by; without one it
        #: is off whatever the soul says.
        self.extend_on_incomplete = bool(interaction.get("extend_on_incomplete"))
        #: The soul's words for the moments the model has none.
        self.lines = persona_lines(soul)
        self._latest_partial = ""
        #: The intents the model tagged into its most recent spoken reply.
        self.last_intents: list[Intent] = []

        self._wake: Any = None
        self._stt: Any = None
        self._llm: Any = None
        self._tts: Any = None
        self._mouth: Mouth | None = None
        self._audio: AudioSource | None = None
        self._sink: AudioSink | None = None
        self.descriptor: WakeDescriptor | None = None
        self.stt_descriptor: TranscriberDescriptor | None = None
        self.llm_descriptor: LanguageModelDescriptor | None = None
        self.tts_descriptor: VoiceDescriptor | None = None
        self.format: AudioFormat | None = None
        self.sink_format: AudioFormat | None = None
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

        # Before the microphone: a provider that cannot start should never
        # have caused a device to open.
        if self.transcribe:
            await self._start_stt()
        if self.reply:
            await self._start_llm()
        if self.speak:
            await self._start_tts()

        source_cls = self.registry.load_audio(self.source_name)
        self._audio = source_cls(self._input_block, self.format)
        await self._audio.start()

        # The output rate is not the input rate and has no reason to be: one is
        # what the detector needs, the other is what synthesis produces. With
        # a voice running, the voice states it and the sink is opened to match;
        # without one, the manifest's figure, or a synthesis rate.
        if self.tts_descriptor is not None:
            sink_rate = self.tts_descriptor.sample_rate
        else:
            sink_rate = int(self._output_block.get("sample_rate") or 22050)
        self.sink_format = AudioFormat(sample_rate=sink_rate)
        sink_cls = self.registry.load_audio_out(self.sink_name)
        self._sink = sink_cls(self._output_block, self.sink_format)
        await self._sink.start()
        if self._tts is not None:
            self._mouth = Mouth(self._tts, self._sink)

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

    async def _start_stt(self) -> None:
        """Bring up speech recognition, or say precisely why not.

        Three refusals, each in the terms the person can act on: no provider
        named anywhere, a provider that would not start (its own health detail
        says why: a missing key, a dead network), and a provider that would run
        at a rate other than the one the wake engine fixed.
        """
        assert self.format is not None
        if self.stt is None or self.stt_name is None:
            installed = ", ".join(self.registry.stt_names) or "(none)"
            raise EngineError(
                "speech recognition was asked for and no provider is configured. "
                "Name one under `models.stt.provider` in the soul, or take it over "
                f"with `audio.stt.provider` in the body. Installed: {installed}."
            )
        stt_cls = self.registry.load_stt(self.stt_name)
        self._stt = stt_cls(self.stt, self.format)
        await self._stt.start()

        self.stt_descriptor = self._stt.describe()
        if not self.stt_descriptor.healthy:
            raise EngineError(self._mute_message())
        if self.stt_descriptor.sample_rate != self.format.sample_rate:
            raise EngineError(
                f"the speech recognition provider {self.stt_name!r} would run at "
                f"{self.stt_descriptor.sample_rate} Hz and the wake engine fixed "
                f"{self.format.sample_rate} Hz. One microphone feeds both, so they "
                f"have to agree; a transcriber hearing speech at the wrong speed "
                f"does not fail, it produces nonsense. It will not start."
            )

    async def _start_llm(self) -> None:
        """Bring up the language model, or say precisely why not."""
        if self.chat is None or self.chat_name is None:
            installed = ", ".join(self.registry.llm_names) or "(none)"
            raise EngineError(
                "a reply was asked for and no language model is configured. Name "
                "one under `models.chat.provider` in the soul, or take it over "
                f"with `models.chat.provider` in the body. Installed: {installed}."
            )
        llm_cls = self.registry.load_llm(self.chat_name)
        self._llm = llm_cls(self.chat)
        await self._llm.start()

        self.llm_descriptor = self._llm.describe()
        if not self.llm_descriptor.healthy:
            detail = ""
            health = self._llm.health()
            if not health.ok and health.detail:
                detail = f" {health.detail}"
            raise EngineError(
                f"the language model {self.chat_name!r} cannot start, so the robot "
                f"would hear questions it can never answer. It will not run.{detail}"
            )

    async def _start_tts(self) -> None:
        """Bring up the voice, or say precisely why not.

        The plugin's own reason is quoted, because it is the one that names
        the fix: a download command for a missing model, a variable for a
        missing key, an extra for a missing library.
        """
        if self.tts is None or self.tts_name is None:
            installed = ", ".join(self.registry.tts_names) or "(none)"
            raise EngineError(
                "speech was asked for and no voice is configured. Name one under "
                "`models.tts.provider` in the soul, or take it over with "
                f"`models.tts.provider` in the body. Installed: {installed}."
            )
        tts_cls = self.registry.load_tts(self.tts_name)
        self._tts = tts_cls(self.tts, self._voice_block)
        await self._tts.start()

        self.tts_descriptor = self._tts.describe()
        if not self.tts_descriptor.healthy:
            detail = ""
            health = self._tts.health()
            if not health.ok and health.detail:
                detail = f" {health.detail}"
            raise EngineError(
                f"the voice {self.tts_name!r} cannot start, so the robot would "
                f"answer and never be heard. It will not run.{detail}"
            )

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

    def _mute_message(self) -> str:
        detail = ""
        health = self._stt.health()
        if not health.ok and health.detail:
            detail = f" {health.detail}"
        return (
            f"the speech recognition provider {self.stt_name!r} cannot start, so "
            f"what people say would go nowhere. It will not run half-deaf.{detail}"
        )

    async def stop(self) -> None:
        """Safe to call twice, and safe if `start()` raised part way through."""
        if self._mouth is not None:
            await self._mouth.hush()
            self._mouth = None
        if self._sink is not None:
            await self._sink.stop()
            self._sink = None
        if self._tts is not None:
            await self._tts.shutdown()
            self._tts = None
        if self._audio is not None:
            await self._audio.stop()
            self._audio = None
        if self._stt is not None:
            await self._stt.shutdown()
            self._stt = None
        if self._llm is not None:
            await self._llm.shutdown()
            self._llm = None
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
        before = self.dropped
        try:
            await self._sink.play(pcm)
        finally:
            self._charge_busy(before)

    async def hush(self) -> None:
        """Stop talking immediately, mid-word, and drop what was queued.

        Barge-in is built on this: `DESIGN.md` §13 requires that speech during
        playback interrupts rather than queues. Nothing calls it yet, and the
        sink contract carries it from the start so that adding barge-in later
        is engine work rather than a breaking change to every sink.
        """
        if self._mouth is not None:
            await self._mouth.hush()
        elif self._sink is not None:
            await self._sink.cancel()

    async def speak_text(self, text: str) -> list[SpokenSentence]:
        """Say a fixed piece of text, sentence by sentence, and wait for it.

        The voice's path without the language model in front of it: a
        greeting, a test line, a robot reading something out. Returns what
        happened to each sentence.
        """
        if self._mouth is None:
            raise EngineError("the voice was not started; construct the session with speak=True")
        before = self.dropped
        sentences = Sentences()
        self._mouth.open()
        try:
            for sentence in sentences.feed(text):
                await self._mouth.say(sentence)
            rest = sentences.flush()
            if rest:
                await self._mouth.say(rest)
            return await self._mouth.finish()
        finally:
            self._charge_busy(before)

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

        The transcriber, when there is one, is fed every frame after the wake,
        lead-in silence included. So a quiet speaker the energy detector missed
        is still transcribed, and a false wake costs a provider a few seconds
        of silence. Its final transcript rides on the utterance.
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
                    closed = endpointer.close()
                    if closed.extended:
                        self.stats.extended += 1
                    yield pending, await self._transcribed(closed)
                return

            if endpointer is None:
                with Stopwatch() as watch:
                    event = await self._wake.process(frame)
                self._record(watch.elapsed_ms)
                if event is not None:
                    self.stats.wakes += 1
                    await self._wake.reset()
                    pending = event
                    self._latest_partial = ""
                    endpointer = Endpointer(
                        self.format,
                        patience_ms=self.patience_ms,
                        extend_if=self._extend_if if self.extend_on_incomplete and self._stt is not None else None,
                    )
                    if self.on_wake is not None:
                        self.on_wake(event)
                continue

            partial: Transcript | None = None
            with Stopwatch() as watch:
                utterance = endpointer.feed(frame)
                if self._stt is not None:
                    # Inside the timing on purpose. A provider that blocks
                    # here on the network is loop cost, and the budget line
                    # should say so rather than flatter it.
                    partial = await self._stt.feed(frame)
            self._record(watch.elapsed_ms)
            if partial is not None:
                self._latest_partial = partial.text
                if self.on_partial is not None:
                    self.on_partial(partial)
            if utterance is not None:
                assert pending is not None
                self.stats.turns += 1
                if utterance.extended:
                    self.stats.extended += 1
                yield pending, await self._transcribed(utterance)
                endpointer = None
                pending = None

    def _extend_if(self) -> bool:
        """The endpointer's question: do the words so far look unfinished?"""
        if not looks_incomplete(self._latest_partial):
            return False
        if self.on_extended is not None:
            self.on_extended(self._latest_partial)
        return True

    async def _transcribed(self, utterance: Utterance) -> Utterance:
        """Close the transcriber's utterance and attach what it heard.

        Timed, and kept apart from the per-frame budget: this is the wait
        between the person stopping and the words existing, which is the
        first latency 0.4 has to answer for.

        The microphone is not read while this waits, the same as during a
        reply, so frames dropped meanwhile are the loop's own choice and are
        charged as such. A provider that took five seconds to send a final
        on the reference body cost 37 frames, and the verdict blamed the
        listening loop for them.
        """
        if self._stt is None:
            return utterance
        before = self.dropped
        try:
            with Stopwatch() as watch:
                final = await self._stt.finish()
        finally:
            self._charge_busy(before)
        self.stats.record_final(watch.elapsed_ms)
        return replace(utterance, transcript=final)

    # --------------------------------------------------------------- answer

    async def answer(self, text: str) -> AsyncIterator[ReplyEvent]:
        """Hand what was said to the language model and stream the reply.

        The caller's step after a turn, on purpose: `turns()` ends when the
        person stops talking, and what happens next is the caller's to show
        or to speak. The words join the conversation, the persona and the
        conversation become the prompt, and the events come back as the
        model produces them, `ReplyDone` last. A reply that ended well joins
        the conversation too; one that ended in error does not, so the model
        is never shown its own failure as something it said.

        The microphone is not read while this runs, the same as `say()`.
        Frames dropped meanwhile are the loop's own doing and are reported
        as such; barge-in, in 1.0, is what changes it.
        """
        if self._llm is None:
            raise EngineError("the language model was not started; construct the session with reply=True")
        self.conversation.add_user(text)
        prompt = self.conversation.prompt(self.system_prompt, max_tokens=DEFAULT_MAX_TOKENS)

        before = self.dropped
        first_ms: float | None = None
        try:
            with Stopwatch() as watch:
                async for event in self._llm.reply(prompt):
                    if isinstance(event, TextDelta) and first_ms is None:
                        first_ms = watch.peek_ms()
                    if isinstance(event, ReplyDone):
                        if event.stop_reason != "error" and event.text.strip():
                            self.conversation.add_assistant(event.text.strip())
                    yield event
            self.stats.record_reply(first_ms, watch.elapsed_ms)
        finally:
            self._charge_busy(before)

    async def answer_aloud(self, text: str) -> AsyncIterator[ReplyEvent]:
        """`answer()`, spoken: the same events, and every sentence said through
        the voice as soon as it is complete.

        The reply's text deltas are watched for sentence ends as they pass
        through. Each complete sentence goes to the voice at once, and its
        audio plays while the model is still writing the next, which is the
        whole reason the reply streams. After `ReplyDone` the tail (a model
        that stops without a full stop) is spoken too, and this waits for the
        last sound before returning, so the caller knows the floor is free.

        A sentence the voice could not say is logged, counted, and skipped;
        the rest of the reply is still heard. `stats` gets the two numbers
        a person feels: transcript to first sound, transcript to last.
        """
        if self._mouth is None:
            raise EngineError("the voice was not started; construct the session with speak=True")
        sentences = Sentences()
        tags = IntentTags()
        self.last_intents = []
        mouth = self._mouth
        first_ms: float | None = None
        watch = Stopwatch()

        def mark_first() -> None:
            nonlocal first_ms
            if first_ms is None:
                first_ms = watch.peek_ms()

        mouth.on_first_audio = mark_first
        mouth.open()
        with watch:
            try:
                async for event in self.answer(text):
                    if isinstance(event, TextDelta):
                        # Tags are lifted out before the words reach the voice
                        # or the caller; the intents they name are reported.
                        clean, intents = tags.feed(event.text)
                        for intent in intents:
                            self.last_intents.append(intent)
                            if self.on_intent is not None:
                                self.on_intent(intent)
                        if not clean:
                            continue
                        for sentence in sentences.feed(clean):
                            await mouth.say(sentence)
                        yield TextDelta(clean)
                        continue
                    yield event
                # `answer()` charged the frames dropped while the model wrote;
                # what follows is the voice's, and is charged below.
                after_reply = self.dropped
                held = tags.flush()
                if held:
                    sentences.feed(held)
                rest = sentences.flush()
                if rest:
                    await mouth.say(rest)
                spoken = await mouth.finish()
            except BaseException:
                await mouth.hush()
                raise
        self.stats.record_speech(
            first_ms,
            watch.elapsed_ms,
            spoken=sum(1 for s in spoken if s.ok and s.audio_bytes),
            lost=sum(1 for s in spoken if not s.ok),
        )
        self._charge_busy(after_reply)

    @property
    def last_spoken(self) -> list[SpokenSentence]:
        """What happened to each sentence of the most recent spoken reply."""
        return list(self._mouth.spoken) if self._mouth is not None else []

    # ----------------------------------------------------------------- talk

    async def talk(
        self,
        *,
        on_said: Callable[[str], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> AsyncIterator[Exchange]:
        """The whole loop: wake, words, answer, voice, speaker, one exchange
        at a time, until the source ends.

        Every stage has to be up, which `start()` guarantees when the session
        was built with `transcribe`, `reply` and `speak` all on. A turn with
        no words in it (a false wake, a cough) gets the soul's
        `nothing_heard` line, or silence. A reply the provider declined or
        the network broke gets the soul's `declined` or `failed` line after
        whatever was heard, so the robot fails loudly and in its own voice.

        `on_said` fires with the final transcript before the reply begins;
        `on_delta` with each piece of the reply's text as it streams, tags
        already lifted out.
        """
        if not (self.transcribe and self.reply and self.speak):
            raise EngineError(
                "talk() needs every stage: construct the session with "
                "transcribe=True, reply=True and speak=True."
            )
        async for wake, utterance in self.turns():
            text = utterance.transcript.text.strip() if utterance.transcript else ""
            if not text:
                line = self.lines.get("nothing_heard")
                spoken = await self.speak_text(line) if line else []
                yield Exchange(wake, utterance, None, tuple(spoken), line, ())
                continue
            if on_said is not None:
                on_said(text)
            done: ReplyDone | None = None
            async for event in self.answer_aloud(text):
                if isinstance(event, TextDelta) and on_delta is not None:
                    on_delta(event.text)
                elif isinstance(event, ReplyDone):
                    done = event
            spoken = list(self.last_spoken)
            intents = tuple(self.last_intents)
            line: str | None = None
            if done is not None and done.stop_reason == "refusal":
                line = self.lines.get("declined")
            elif done is not None and done.stop_reason == "error":
                line = self.lines.get("failed")
            if line:
                spoken += await self.speak_text(line)
            yield Exchange(wake, utterance, done, tuple(spoken), line, intents)

    def _charge_busy(self, dropped_before: int) -> None:
        """Attribute frames dropped during `say()` or `answer()` to the loop's
        own choice not to read, so the verdict judges listening alone."""
        lost = self.dropped - dropped_before
        if lost > 0:
            self.stats.dropped_busy += lost
        self.stats.dropped = self.dropped

    # -------------------------------------------------------------- honesty

    def _record(self, process_ms: float) -> None:
        self.stats.record_frame(process_ms)
        # Pulled from the source each frame rather than read once at the end:
        # a run that is killed part way through should still say what it lost.
        self.stats.dropped = self.dropped
        self.stats.overflows = self.overflows

    def warm_start_hint(self) -> str | None:
        """Whatever the wake plugin wants remembered for the next boot.

        Duck-typed on purpose: the engine names no engine. A plugin that has
        nothing to say, or no such method, yields None.
        """
        hint = getattr(self._wake, "warm_start_hint", None)
        if not callable(hint):
            return None
        text = hint()
        return str(text) if text else None

    @property
    def overflows(self) -> int:
        """Frames the sound card lost before the loop saw them. Read
        defensively, like `dropped`: a file has no card to overflow."""
        return int(getattr(self._audio, "overflows", 0) or 0)

    @property
    def dropped(self) -> int:
        """Frames the source discarded because this loop fell behind.

        Read defensively: `AudioSource` does not require it, and a file cannot
        drop anything. Non-zero means wake words were missed, and a robot that
        knows it missed something should be able to say so rather than let it
        pass as bad luck.
        """
        return int(getattr(self._audio, "dropped", 0) or 0)
