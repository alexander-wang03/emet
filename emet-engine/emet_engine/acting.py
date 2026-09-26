"""Acting on an intent through its binding.

The binding table says what each intent means on this body; this module does
it. A hardware binding becomes an `Action` handed to the part that won the
chain. A voice binding becomes one of the six things the voice rung can do
(`DESIGN.md` section 6.1):

    utter        the words: every sentence of a reply is a `speak` intent
    inflect      a filler in the reply's voice: `[express.curiosity]` is "hm?"
    tone         a system tone: the rising pair at boot, the click on a wake
    explain      why the body cannot comply, in the self-model's own words
    backchannel  the thinking-gap filler; a placeholder that fires nothing until 1.0
    silence      binds, makes no sound, and is a satisfied intent

**Silence is success.** An idle intent on a body with no actuators binds to
`silence` and is done. Nothing here reports it as a failure, and nothing
downstream should: a chain ending in silence resolved, bound and executed,
and producing no sound was the correct output (section 6.1).

**Reserved intents are dropped here too.** `manipulate`, `navigate`,
`gesture` and `attend_joint` are legal to emit and have no behaviour until
the release that implements them (section 5.2), so an actor asked to perform
one logs it at debug and returns. The tag reader drops them earlier, before
they are reported; this is the second of two doors.

**The voice is borrowed.** The actor does not own the mouth or the sink. The
session hands it two callables: `say`, which puts words into the reply being
spoken (or speaks them on their own and waits, outside a reply), and `play`,
which plays a buffer through the sink. Without a voice (`emet-listen` with no
`--speak`), the words and the tones are recorded as `unvoiced` and the
hardware rungs still act.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from emet_sdk import intents as _intents
from emet_sdk.resolve import Binding
from emet_sdk.types import Action, Intent, SelfModel

from emet_engine import tones
from emet_engine.body import Body
from emet_engine.speech import SpokenSentence

__all__ = ["Actor", "Performed", "OUTCOMES", "contour", "signal", "speak", "explain_topics"]

log = logging.getLogger("emet_engine.acting")

#: What can become of an intent. The first three are success.
OUTCOMES = frozenset({"done", "silent", "placeholder", "unvoiced", "dropped", "failed", "unknown"})

#: `say` puts words into the voice and returns what was heard when it waited
#: for them, or nothing when they joined a reply still being spoken. Its
#: `from_reply` keyword is False for a filler or an explanation.
Say = Callable[..., Awaitable[Sequence[SpokenSentence]]]
#: `play` plays a buffer of mono int16 at the sink's rate.
Play = Callable[[bytes], Awaitable[None]]


def contour(text: str) -> str:
    """A prosody hint read off a sentence's end mark: a question rises, an
    exclamation is bright, anything else is level.

    Recorded on every `speak` action and in what `perform()` returns. No
    voice receives it: `VoicePlugin.speak()` takes text alone, so reaching a
    voice is a contract change for a later release, and until then the hint
    is unheard, like `voice.pitch_shift_semitones`.
    """
    end = text.rstrip().rstrip("\"')]")[-1:]
    return {"?": "rising", "!": "bright"}.get(end, "level")


@dataclass(frozen=True, slots=True)
class Performed:
    """What became of one intent."""

    intent: Intent
    #: One of `OUTCOMES`.
    outcome: str
    binding: Binding | None = None
    action: Action | None = None
    #: The words handed to the voice, for utter, inflect and explain.
    said: str | None = None
    #: What was heard, when the words were spoken on their own and waited for.
    spoken: tuple[SpokenSentence, ...] = ()
    #: How long a tone lasts, silences included.
    audio_ms: float = 0.0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the intent was satisfied. Silence counts."""
        return self.outcome in {"done", "silent", "placeholder"}

    @property
    def voiced(self) -> bool:
        """Whether the voice rung made a sound for it."""
        return self.outcome == "done" and self.binding is not None and self.binding.is_voice and (
            self.said is not None or self.audio_ms > 0
        )


@dataclass
class Actor:
    """Performs intents through a body's binding table."""

    body: Body
    self_model: SelfModel
    #: Words into the voice; None when no voice is running.
    say: Say | None = None
    #: A buffer through the sink; None when there is no voice to play tones in.
    play: Play | None = None
    #: The sink's rate, which tones are rendered at.
    sample_rate: int | None = None
    #: Called with every outcome, after it happened.
    on_performed: Callable[[Performed], None] | None = None
    #: The most recent outcomes, newest last.
    history: deque = field(default_factory=lambda: deque(maxlen=64))
    _fillers: dict[str, int] = field(default_factory=dict)

    def binding_for(self, intent: Intent) -> Binding | None:
        """The binding an intent resolves to. A chain is keyed by the dotted
        name, or by the kind alone for an intent whose argument is free text:
        `speak` carries its sentence as the argument and binds as `speak`."""
        table = self.body.table
        if intent.name in table:
            return table[intent.name]
        if intent.kind in table:
            return table[intent.kind]
        return None

    async def perform(self, intent: Intent) -> Performed:
        outcome = await self._perform(intent)
        self.history.append(outcome)
        if outcome.outcome not in {"done", "silent", "placeholder"}:
            log.debug("acting: %s %s (%s)", intent.name, outcome.outcome, outcome.detail)
        if self.on_performed is not None:
            self.on_performed(outcome)
        return outcome

    async def _perform(self, intent: Intent) -> Performed:
        if _intents.is_reserved(intent.kind):
            log.debug("acting: %s is reserved; dropped until the release that implements it", intent.name)
            return Performed(intent, "dropped", detail="reserved")
        binding = self.binding_for(intent)
        if binding is None:
            return Performed(intent, "dropped", detail="no chain for this intent")

        params: dict[str, Any] = dict(binding.params)
        if intent.kind == "speak" and intent.argument:
            params.update(text=intent.argument, contour=contour(intent.argument))
        action = Action(
            capability_id=binding.capability_id or "voice",
            name=binding.action,
            params=params,
            intensity=intent.intensity,
            duration_ms=intent.duration_ms,
        )

        if not binding.is_voice:
            try:
                await self.body.apply(binding, action)
            except Exception as exc:  # noqa: BLE001 - a broken part loses the gesture and the turn goes on
                log.warning("acting: %s on %s failed: %s", action.name, binding.capability_id, exc)
                return Performed(intent, "failed", binding, action, detail=f"{type(exc).__name__}: {exc}")
            return Performed(intent, "done", binding, action)
        return await self._voice(intent, binding, action)

    async def _voice(self, intent: Intent, binding: Binding, action: Action) -> Performed:
        name, params = action.name, action.params
        if name == "silence":
            return Performed(intent, "silent", binding, action)
        if name == "backchannel":
            # The thinking-gap filler belongs to the local micro-model, which
            # arrives in 1.0 with barge-in. Until then it binds and is quiet.
            return Performed(intent, "placeholder", binding, action, detail="backchannel arrives in 1.0")
        if name == "tone":
            return await self._tone(intent, binding, action)

        if name == "utter":
            words = str(params.get("text") or intent.argument or "").strip() or None
        elif name == "inflect":
            words = self._filler(intent.name, params.get("filler"))
        elif name == "explain":
            words = self.self_model.explanations.get(str(params.get("topic") or ""))
        else:
            log.warning("acting: the voice rung for %s names %r, which no engine performs", intent.name, name)
            return Performed(intent, "unknown", binding, action, detail=f"unknown voice action {name!r}")

        if not words:
            return Performed(intent, "silent", binding, action, detail="nothing to say")
        if self.say is None:
            return Performed(intent, "unvoiced", binding, action, said=words, detail="no voice is running")
        spoken = await self.say(words, from_reply=name == "utter")
        return Performed(intent, "done", binding, action, said=words, spoken=tuple(spoken))

    async def _tone(self, intent: Intent, binding: Binding, action: Action) -> Performed:
        preset = str(action.params.get("preset") or "")
        if self.play is None or not self.sample_rate:
            return Performed(intent, "unvoiced", binding, action, detail="no voice is running")
        try:
            pcm = tones.render(preset, self.sample_rate)
        except tones.UnknownTone as exc:
            log.warning("acting: %s", exc)
            return Performed(intent, "unknown", binding, action, detail=str(exc))
        try:
            await self.play(pcm)
        except Exception as exc:  # noqa: BLE001 - a tone the sink refused is lost and the turn goes on
            log.warning("acting: the %s tone did not play: %s", preset, exc)
            return Performed(intent, "failed", binding, action, detail=f"{type(exc).__name__}: {exc}")
        return Performed(intent, "done", binding, action, audio_ms=tones.duration_ms(preset))

    def _filler(self, key: str, fillers: Any) -> str | None:
        """The next filler for this intent, in turn, so a robot curious twice
        in a row says "hm?" and then "hmm." rather than the same sound."""
        if isinstance(fillers, str):
            fillers = [fillers]
        # Words only: the validator refuses anything else in a chain file,
        # and a null that got past it would be spoken as "None".
        options = [f for f in (fillers or []) if isinstance(f, str) and f.strip()]
        if not options:
            return None
        turn = self._fillers.get(key, 0)
        self._fillers[key] = turn + 1
        return options[turn % len(options)]


def signal(name: str) -> Intent:
    """A signal intent from the engine's own state: booting, listening,
    thinking, speaking, muted, offline or error. System state, never
    emotion, and never from the model (`emet_engine.intent_tags` drops a
    tagged one)."""
    return _intents.make("signal", name)


def speak(sentence: str) -> Intent:
    """One sentence of a reply, as the intent it travels as."""
    return _intents.make("speak", sentence)


def explain_topics(bindings: Mapping[str, Binding]) -> set[str]:
    """The explain topics a body's bindings can reach: the things this body
    may have to say it cannot do."""
    return {
        str(b.params.get("topic"))
        for b in bindings.values()
        if b.is_voice and b.action == "explain" and b.params.get("topic")
    }
