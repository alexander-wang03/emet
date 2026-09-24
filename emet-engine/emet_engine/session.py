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

**The body, since 0.5.** Before the microphone opens, every part the manifest
declares is started and described (a reserved type has no plugin yet and is
only listed), and every chain is resolved once against what the parts
reported (`emet_engine.body`). The self-model is compiled from the same
manifest and the same descriptors (`emet_engine.self_model`), and joins the
persona in the system prompt with the tags this body can act on. From then
on an intent is performed through its binding (`emet_engine.acting`): the
reply's sentences as `speak` intents, the tags the model wrote at the place
it wrote them, and the engine's own state as `signal` intents, `booting`
when it is up, `listening` on a wake, `thinking` while the model works,
`speaking` from the first sentence, `offline` when, in `talk()`, a
provider's words or reply do not arrive, `error` when a declared part is not
working, and `muted` when it stops listening for good.

**Body-local state.** With a `body.id` in the manifest, what is true of this
hardware and meaningless on the next is kept in the body's state file
(`emet_engine.state`): the binding table, the audio devices that answered,
each part's health, joint trims, and whatever a plugin asks to carry over,
which for the shipped wake engine is its adapted cepstral mean.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from emet_sdk.discovery import PluginRegistry
from emet_sdk.models import chat_selection, stt_selection, tts_selection
from emet_sdk.resolve import BindingTable, load_chains
from emet_sdk.types import (
    AudioFormat,
    AudioSink,
    AudioSource,
    Intent,
    LanguageModelDescriptor,
    ReplyDone,
    ReplyEvent,
    SelfModel,
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

from emet_engine.acting import Actor, Performed, signal, speak
from emet_engine.body import Body
from emet_engine.intent_tags import IntentTags
from emet_engine.metrics import SessionStats, Stopwatch
from emet_engine.prompting import (
    DEFAULT_MAX_TOKENS,
    Conversation,
    offered_tags,
    persona_lines,
    system_prompt,
)
from emet_engine.self_model import compile_self_model
from emet_engine.speech import Mouth, Sentences, SpokenSentence, split_sentences
from emet_engine.state import BodyState
from emet_engine.turn import DEFAULT_PATIENCE_MS, Endpointer, Utterance, looks_incomplete

__all__ = ["EngineError", "ListenSession", "Exchange", "sentences_of"]

log = logging.getLogger("emet_engine.session")

#: The output latency assumed for a sink that plays into the room and has not
#: said what its own is. The reference body's figure is unmeasured (the
#: header prints the speaker's once its stream is open); this errs long,
#: because a long guess costs a few frames the endpointer cannot start on,
#: and a short one lets the robot hear its own click as somebody speaking.
DEFAULT_OUTPUT_LATENCY_MS = 150.0


def sentences_of(text: str) -> list[str]:
    """A fixed piece of text as the sentences a voice is handed, in order."""
    sentences = Sentences()
    out = sentences.feed(text)
    rest = sentences.flush()
    return out + ([rest] if rest else [])


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
        on_performed: Callable[[Performed], None] | None = None,
        chains: Sequence[str | Path] = (),
        state: str | Path | None = None,
        carry: bool | None = None,
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

        body = manifest.get("body") or {}
        #: The key to this body's state file, and its human label. `body.id`
        #: is never a memory namespace; its one job is the state file.
        self.body_id: str | None = str(body.get("id")) if body.get("id") else None
        self.body_name: str | None = str(body.get("name")) if body.get("name") else None
        #: Chain files laid over the SDK's, later winning, as `emet explain
        #: --chains` does.
        self.chain_files: list[str | Path] = list(chains)
        #: An explicit state file, or None for the default place for this body.
        self.state_arg = state
        #: Whether plugins carry what they learned into and out of the state
        #: file: the wake engine, every driver and the locomotion plugin. Off
        #: by default for a recording, since a replay exists to reproduce a
        #: run exactly and a mean adapted to somebody else's room is somebody
        #: else's calibration. Trims still apply, and bindings, health and
        #: devices are still recorded.
        self.carry = (self.source_name != "wav") if carry is None else bool(carry)
        self.body_state: BodyState | None = None
        #: The started parts and the binding table, after `start()`.
        self.body: Body | None = None
        #: What the robot is told about its body, after `start()`.
        self.self_model: SelfModel | None = None
        #: The tags the system prompt offers on this body, after `start()`.
        self.tags: list[str] = []
        #: Fires with every intent performed, after it was.
        self.on_performed = on_performed
        self._actor: Actor | None = None
        #: Where the wake engine's carried params live in the state file.
        self._wake_key = f"wake.{self.engine_name}"
        #: Names of the params the wake engine was handed from the state file.
        self.wake_carried: list[str] = []
        #: State-file keys `remember()` last wrote carried params under.
        self.kept: list[str] = []

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
        #: bring it up. The persona is the system prompt until `start()` adds
        #: the body to it, once; the conversation accumulates across turns
        #: for the length of the run.
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

        # First, because the wake engine is built from it: what the last run
        # on this body learned. Never raises; a bad file is a warning.
        self.body_state = BodyState.load(self.body_id, self.state_arg)

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

        # The body, before the microphone too: a driver that is not installed
        # stops the boot here, and a part that will not start is recorded
        # and bound past.
        await self._start_body()

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

        assert self.body is not None and self.self_model is not None
        # Without a voice the words and the tones have nowhere to go, and the
        # hardware rungs still act: `emet-listen` without `--speak` is quiet.
        voiced = self._mouth is not None
        self._actor = Actor(
            self.body,
            self.self_model,
            say=self._voice_say if voiced else None,
            play=self.say if voiced else None,
            sample_rate=self.sink_format.sample_rate,
            on_performed=self.on_performed,
        )

        # Booted, and said so, before the microphone opens: the rising pair is
        # not in the first frames the wake engine hears, and no backlog of
        # frames builds up while it plays to be counted as audio by the clock.
        await self._signal("booting")
        if self.body.faults:
            await self._signal("error")

        source_cls = self.registry.load_audio(self.source_name)
        self._audio = source_cls(self._input_block, self.format)
        await self._audio.start()
        self._record_state()

        log.info(
            "listening for %r via %s on %s at %d Hz",
            self.phrase,
            self.engine_name,
            self.source_name,
            self.format.sample_rate,
        )

    def _build_wake(self) -> Any:
        """The wake engine, with whatever it carried over on this body laid
        over the manifest's params. The state file wins: it holds what the
        last run learned, and the manifest's value is the builder's guess."""
        wake_cls = self.registry.load_wake(self.engine_name)
        block = dict(self._wake_block)
        carried = self.body_state.carried(self._wake_key) if self.body_state is not None and self.carry else {}
        if carried:
            block["params"] = {**dict(block.get("params") or {}), **carried}
            self.wake_carried = sorted(carried)
        return wake_cls(block, self.phrase)

    async def _start_body(self) -> None:
        """Start every declared part, resolve every chain once, compile the
        self-model from the same answers, and put it in the system prompt."""
        try:
            chains = load_chains(self.chain_files)
        except Exception as exc:  # noqa: BLE001 - a YAML error and a bad chain read the same to the person
            raise EngineError(f"the chains could not be loaded: {type(exc).__name__}: {exc}") from exc
        self.body = Body(self.manifest, self.registry, chains=chains, state=self.body_state, carry=self.carry)
        await self.body.start()
        self.self_model = compile_self_model(
            self.manifest,
            capabilities=self.body.capabilities,
            locomotion=self.body.locomotion,
            lines=self.lines,
        )
        self.tags = offered_tags(self.body.table)
        self.system_prompt = system_prompt(self.soul, self_model=self.self_model, tags=self.tags)

    def _record_state(self) -> None:
        """Write what boot learned about this body: its bindings, its audio
        devices, each part's health. A replay names no device, so a device
        already on file is kept rather than overwritten with nothing."""
        state = self.body_state
        if state is None or not state.enabled or self.body is None:
            return
        state.record_bindings(self.body.table)
        known = state.data.get("devices") or {}
        state.record_devices(
            getattr(self._audio, "device_name", None) or known.get("input"),
            getattr(self._sink, "device_name", None) or known.get("output"),
        )
        state.record_health(self.body.health)
        state.save()

    @property
    def binding_table(self) -> BindingTable | None:
        """What each intent means on this body, resolved at boot."""
        return self.body.table if self.body is not None else None

    @property
    def performed(self) -> list[Performed]:
        """The most recent intents performed, oldest first."""
        return list(self._actor.history) if self._actor is not None else []

    def remember(self) -> Path | None:
        """Keep what the plugins learned about this body for its next boot.

        Asks the wake engine and every part for `carry_over()`, while they
        are still running, and writes the state file. Returns where it was
        written, or None when there is nothing to keep it in (no `body.id`,
        or a file that could not be written; `body_state.warnings` says
        which). An empty answer keeps what was carried before: a run that
        learned nothing leaves the last lesson in place.
        """
        state = self.body_state
        if state is None or not state.enabled or not self.carry:
            return None
        kept: dict[str, dict[str, Any]] = {}
        if self._wake is not None:
            try:
                kept[self._wake_key] = dict(self._wake.carry_over() or {})
            except Exception as exc:  # noqa: BLE001 - a plugin that cannot say keeps its old notes
                log.warning("wake: carry_over failed: %s", exc)
        if self.body is not None:
            kept.update(self.body.carry_over())
        # A key the state file refused (a value JSON cannot hold) is not
        # reported as kept; the file's warning says why.
        self.kept = sorted(key for key, params in kept.items() if params and state.carry(key, params))
        if not self.kept:
            return None
        return state.save()

    async def _signal(self, name: str) -> Performed | None:
        """Perform one of the engine's own states through its binding."""
        if self._actor is None:
            return None
        return await self._actor.perform(signal(name))

    async def _voice_say(self, text: str, *, from_reply: bool = True) -> list[SpokenSentence]:
        """The actor's way into the voice. Inside a reply the words join it,
        in order; outside one they are spoken on their own and waited for.
        `from_reply` is False for words the body adds (a filler, an
        explanation), which are heard and not counted as the reply's."""
        if self._mouth is None:
            return []
        if self._mouth.is_open:
            for sentence in sentences_of(text):
                await self._mouth.say(sentence, from_reply=from_reply)
            return []
        before = self.dropped
        self._mouth.open()
        try:
            for sentence in sentences_of(text):
                await self._mouth.say(sentence, from_reply=from_reply)
            return await self._mouth.finish()
        except BaseException:
            # Interrupted mid-line: stop the words now, so nothing said on
            # the way down (the muted tone) queues behind the rest of them.
            await self._mouth.hush()
            raise
        finally:
            self._charge_busy(before)

    async def _utter(self, sentence: str) -> None:
        """One sentence, as the `speak` intent it travels as."""
        if self._actor is not None:
            await self._actor.perform(speak(sentence))
        elif self._mouth is not None:
            await self._mouth.say(sentence)

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
                f"with `models.stt.provider` in the body. Installed: {installed}."
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
        """Safe to call twice, and safe if `start()` raised part way through.

        First it keeps what its plugins learned, while they still know it and
        before anything below can fail. Then it stops whatever it was saying,
        and says it has stopped listening (`signal.muted`, the falling pair on
        a body with nothing else to show it with), bounded so that a speaker
        that has stopped taking audio cannot hold the way down. Then every
        piece is shut down, each on its own, so one that raises cannot leave
        the parts after it running; the first error is raised at the end.
        A second Ctrl-C or SIGTERM on the way down is held the same way: the
        rest still stop, and the cancellation is raised once they have.
        """
        try:
            self.remember()
        except Exception as exc:  # noqa: BLE001 - notes that cannot be kept must not strand the hardware
            log.warning("stop: the body's state could not be kept: %s", exc)

        failures: list[BaseException] = []
        cancelled = False

        async def step(name: str, action: Callable[[], Any], *, counts: bool = True) -> None:
            nonlocal cancelled
            try:
                await action()
            except asyncio.CancelledError:
                log.warning("stop: cancelled while %s stopped; stopping the rest first", name)
                cancelled = True
            except Exception as exc:  # noqa: BLE001 - logged, and the next piece still stops
                log.warning("stop: %s failed: %s", name, exc)
                if counts:
                    failures.append(exc)

        if self._mouth is not None:
            await step("the mouth", self._mouth.hush, counts=False)
        if self._actor is not None and self._sink is not None:
            await step("the muted signal", self._muted, counts=False)
        self._actor = None
        self._mouth = None

        if self._sink is not None:
            await step("the sink", self._sink.stop)
            self._sink = None
        if self._tts is not None:
            await step("the voice", self._tts.shutdown)
            self._tts = None
        if self._audio is not None:
            await step("the source", self._audio.stop)
            self._audio = None
        if self._stt is not None:
            await step("the transcriber", self._stt.shutdown)
            self._stt = None
        if self._llm is not None:
            await step("the language model", self._llm.shutdown)
            self._llm = None
        if self._wake is not None:
            await step("the wake engine", self._wake.shutdown)
            self._wake = None
        if self.body is not None:
            await step("the body", self.body.shutdown)
        if cancelled:
            raise asyncio.CancelledError()
        if failures:
            raise failures[0]

    async def _muted(self) -> None:
        """`signal.muted`, bounded at two seconds.

        Skipped when the sink still holds audio it never played: that is a
        speaker that stopped asking for audio, and a tone queued behind it
        would wait out the whole backlog and then the stall grace."""
        queued = int(getattr(self._sink, "queued_bytes", 0) or 0)
        if queued > int(getattr(self._sink, "block_bytes", 0) or 0):
            log.warning("stop: the sink still holds unplayed audio; no muted tone")
            return
        try:
            await asyncio.wait_for(self._signal("muted"), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("stop: the muted signal took longer than two seconds; going on without it")
        except Exception as exc:  # noqa: BLE001 - going down anyway; say why and keep going
            log.warning("stop: the muted signal failed: %s", exc)

    async def __aenter__(self) -> "ListenSession":
        try:
            await self.start()
        except BaseException:
            # A boot that failed part way has started parts a person may be
            # able to touch (a servo, from 1.0) and a microphone. `__aexit__`
            # never runs for a failed `__aenter__`, so they are put safe here,
            # and the reason the boot failed is the one that is raised.
            try:
                await self.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("stop after a failed start: %s", exc)
            raise
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
        except BaseException:
            # Cut off part way (Ctrl-C during the boot chime): drop the rest,
            # so the sink holds no tail that `stop()` would take for a
            # speaker that stopped asking for audio.
            await self._sink.cancel()
            raise
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
        self._mouth.open()
        try:
            for sentence in sentences_of(text):
                await self._utter(sentence)
            return await self._mouth.finish()
        except BaseException:
            await self._mouth.hush()
            raise
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
                    await self._listening(endpointer)
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

    async def _listening(self, endpointer: Endpointer) -> None:
        """Show that the robot is listening, and do not mistake the showing
        for the person.

        `signal.listening` on a body with no status light and no eyes is the
        voice rung's soft click. Played into a room by a body that declares
        no echo cancellation (the reference body says `aec: none`), the
        click reaches its own microphone a moment later, and the energy
        detector would take it for the start of speech: the person would
        then have only one patience window to begin, where the lead-in gives
        them two and a half seconds. So the endpointer is deaf for the
        click's length plus the sink's output latency and one frame. Speech
        that starts inside that window is still captured, the room's level
        is still learned from it, and the transcriber hears every frame
        regardless. A sink that plays into no room (`null`, `wav`) reports
        no latency, and a recording never heard the click, so neither
        deafens anything.
        """
        performed = await self._signal("listening")
        if performed is None or not performed.audio_ms or self.format is None:
            return
        if self.source_name == "wav" or str(self._input_block.get("aec") or "none") != "none":
            return
        latency = getattr(self._sink, "stream_latency_s", None)
        if not hasattr(self._sink, "stream_latency_s"):
            return
        latency_ms = float(latency) * 1000.0 if latency else DEFAULT_OUTPUT_LATENCY_MS
        endpointer.deafen(performed.audio_ms + latency_ms + self.format.frame_ms)

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
        await self._signal("thinking")
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

    async def _lifted(
        self,
        text: str,
        *,
        words: Callable[[str], Awaitable[None]] | None = None,
        before_tag: Callable[[Intent], Awaitable[None]] | None = None,
    ) -> AsyncIterator[ReplyEvent]:
        """`answer()`, with every tag lifted out of the text and acted on
        where it stood.

        Each piece of clean text goes to `words` in order, and each tag is
        reported (`last_intents`, `on_intent`), handed to `before_tag`, then
        performed through its binding. The caller gets the clean text as
        `TextDelta`s and the other events as they came, `ReplyDone` last. A
        bracket still open when the reply ends is released as text before
        `ReplyDone`, unless it can only have been a tag cut short, so what
        is printed and what is heard stay the same words.
        """
        tags = IntentTags()
        self.last_intents = []
        async for event in self.answer(text):
            if isinstance(event, ReplyDone):
                held = tags.flush()
                if held:
                    if words is not None:
                        await words(held)
                    yield TextDelta(held)
                yield event
                continue
            if not isinstance(event, TextDelta):
                yield event
                continue
            clean: list[str] = []
            for piece in tags.pieces(event.text):
                if isinstance(piece, str):
                    clean.append(piece)
                    if words is not None:
                        await words(piece)
                    continue
                self.last_intents.append(piece)
                if self.on_intent is not None:
                    self.on_intent(piece)
                if before_tag is not None:
                    await before_tag(piece)
                if self._actor is not None:
                    await self._actor.perform(piece)
            if clean:
                yield TextDelta("".join(clean))

    async def answer_acted(self, text: str) -> AsyncIterator[ReplyEvent]:
        """`answer()` for a reply that is printed and not spoken
        (`emet-listen --reply`): the tags lifted out of the text, reported,
        and acted on. Without a voice the voice rungs are recorded as
        unvoiced, and the hardware rungs act."""
        async for event in self._lifted(text):
            yield event

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

        Every sentence travels as a `speak` intent through its binding, and
        every tag the model wrote is acted on where it stood. On a bodiless
        body `[express.curiosity]` is a filler in the reply's voice, queued
        with the sentences, so it is heard before the sentence the tag
        opened; a tag in the middle of a sentence is heard before that
        sentence, which is the unit of speech. A hardware rung acts when the
        model writes the tag, ahead of the words being heard, until the
        choreographer (1.0) can time it to them. The first sound is also
        `signal.speaking`.
        """
        if self._mouth is None:
            raise EngineError("the voice was not started; construct the session with speak=True")
        sentences = Sentences()
        mouth = self._mouth
        first_ms: float | None = None
        watch = Stopwatch()
        speaking = False

        def mark_first() -> None:
            nonlocal first_ms
            if first_ms is None:
                first_ms = watch.peek_ms()

        async def start_speaking() -> None:
            nonlocal speaking
            if not speaking:
                speaking = True
                await self._signal("speaking")

        async def utter(sentence: str) -> None:
            await start_speaking()
            await self._utter(sentence)

        async def words(piece: str) -> None:
            for sentence in sentences.feed(piece):
                await utter(sentence)

        async def before_tag(intent: Intent) -> None:
            # A sentence that has its end mark and is only waiting for the
            # next word to confirm it ("Well..." before a capital, "news!"
            # with the tag glued on) is said first: the tag opens the next.
            # The splitter decides, as if that next word had come, so "Dr."
            # and "3." wait for the rest of their sentence as they would
            # without a tag.
            closed, _ = split_sentences(sentences.pending.rstrip() + " X")
            if closed:
                rest = sentences.flush()
                if rest:
                    await utter(rest)
            binding = self._actor.binding_for(intent) if self._actor is not None else None
            if binding is not None and binding.is_voice and binding.action in ("inflect", "explain", "utter"):
                # A filler or an explanation is the first sound of the reply
                # as much as a sentence is.
                await start_speaking()

        mouth.on_first_audio = mark_first
        mouth.open()
        with watch:
            try:
                async for event in self._lifted(text, words=words, before_tag=before_tag):
                    yield event
                # `answer()` charged the frames dropped while the model wrote;
                # what follows is the voice's, and is charged below.
                after_reply = self.dropped
                rest = sentences.flush()
                if rest:
                    await utter(rest)
                spoken = await mouth.finish()
            except BaseException:
                await mouth.hush()
                raise
        heard = [s for s in spoken if s.ok and s.audio_bytes]
        self.stats.record_speech(
            # A reply nobody heard has no first sound. `on_first_audio` fires
            # before the sink accepts the chunk, so a reply whose every
            # sentence failed would otherwise contribute a `voice first` for
            # audio that never left the machine. A filler is a first sound.
            first_ms if heard else None,
            watch.elapsed_ms,
            # The reply's own sentences; a filler is not one of them.
            spoken=sum(1 for s in heard if s.from_reply),
            lost=sum(1 for s in spoken if not s.ok and s.from_reply),
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
        `nothing_heard` line, or silence; a turn whose words never arrived
        because the transcriber could not reach its service gets the `failed`
        line instead, because somebody spoke and deserves an answer. A reply
        the provider declined or the network broke gets the soul's `declined`
        or `failed` line after whatever was heard, so the robot fails loudly
        and in its own voice.

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
            transcript = utterance.transcript
            text = transcript.text.strip() if transcript else ""
            if not text:
                # A cough and a dead speech service both arrive here as no
                # words. They are different events to the person in the room:
                # one of them spoke. So a transcript that says why the words
                # are missing gets the failure line, and a plain empty one
                # keeps the old silence.
                broke = transcript is not None and transcript.error is not None
                if broke:
                    spoken, line = await self._say_failure()
                else:
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
                if line:
                    spoken += await self.speak_text(line)
            elif done is not None and done.stop_reason == "error":
                more, line = await self._say_failure()
                spoken += more
            yield Exchange(wake, utterance, done, tuple(spoken), line, intents)

    async def _say_failure(self) -> tuple[list[SpokenSentence], str | None]:
        """A provider's words or reply did not arrive: `signal.offline`, and
        the soul's `failed` line, said once.

        On a body with nothing else to show it with, the signal's voice rung
        is `explain`, whose words for `offline` are the soul's `failed`
        line, and saying it is the whole of both. Where a status light shows
        the state instead, the words are still owed to the person who asked,
        and are said after it. Returns what was heard and the line.
        """
        performed = await self._signal("offline")
        if performed is not None and performed.said:
            return list(performed.spoken), performed.said
        line = self.lines.get("failed")
        return (await self.speak_text(line) if line else []), line

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
    def underflows(self) -> int:
        """Times PortAudio reported that this process did not service the
        card in time. Read defensively, like `overflows`: only a real card
        can be late, and only a sink that keeps a stream open counts.

        It says nothing about whether the audio was continuous. A queue that
        ran dry is served on time with silence, and `starved` is that."""
        return int(getattr(self._sink, "underflows", 0) or 0)

    @property
    def starved(self) -> int:
        """Blocks of silence the sink played inside a sentence because the
        voice had not made the next chunk yet. A gap a person hears."""
        return int(getattr(self._mouth, "starved", 0) or 0)

    @property
    def dropped(self) -> int:
        """Frames the source discarded because this loop fell behind.

        Read defensively: `AudioSource` does not require it, and a file cannot
        drop anything. Non-zero means wake words were missed, and a robot that
        knows it missed something should be able to say so rather than let it
        pass as bad luck.
        """
        return int(getattr(self._audio, "dropped", 0) or 0)
