"""Core types shared by the engine, the HAL, and every plugin.

This module is contract, not logic. Anything here is something both sides of
the boundary must agree on, so additions are a minor version bump of the SDK
and changes are a major one.

The soul never names hardware (principle 1). Nothing in this module references
a servo channel, a GPIO pin, an I2C address, or a driver: intents and actions
describe *what should happen*, and plugins decide what that means for a body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "Priority",
    "TargetKind",
    "Target",
    "Intent",
    "Action",
    "Pose",
    "Twist",
    "CapabilityDescriptor",
    "LocomotionDescriptor",
    "WakeDescriptor",
    "WakeEvent",
    "Transcript",
    "TranscriberDescriptor",
    "ToolSpec",
    "ToolCall",
    "Message",
    "Prompt",
    "TextDelta",
    "ReplyDone",
    "ReplyEvent",
    "STOP_REASONS",
    "LanguageModelDescriptor",
    "VoiceDescriptor",
    "AudioFormat",
    "AudioSource",
    "AudioSink",
    "SAMPLE_BYTES",
    "Health",
    "Reading",
    "Sensitivity",
    "MemoryKind",
]


class Priority(IntEnum):
    """Arbitration order. Highest wins; ties break by recency.

    P0 is one intent per actuator: a higher-priority intent preempts, and the
    lower one is dropped rather than queued. Per-actuator blending (expressing
    curiosity with the eyes *while* attending with the head) is V1.
    """

    SAFETY = 100      # thermal, estop, joint limit, brownout recovery
    SIGNAL = 90       # system state is never suppressed by a personality
    SPEECH = 80       # speak, acknowledge
    ATTENTION = 60    # attend
    EXPRESSIVE = 40   # express
    LOCOMOTION = 30   # move
    IDLE = 10         # the idle loop


class TargetKind(StrEnum):
    NONE = "none"
    BEARING = "bearing"        # a direction, from DoA or vision
    PERSON = "person"          # a known person id
    CAPABILITY = "capability"  # one of the robot's own parts (V1: attend_joint)


@dataclass(frozen=True, slots=True)
class Target:
    """What an intent is directed at, if anything."""

    kind: TargetKind = TargetKind.NONE
    bearing_deg: float | None = None
    person_id: str | None = None
    capability_id: str | None = None

    def __post_init__(self) -> None:
        required = {
            TargetKind.BEARING: ("bearing_deg", self.bearing_deg),
            TargetKind.PERSON: ("person_id", self.person_id),
            TargetKind.CAPABILITY: ("capability_id", self.capability_id),
        }.get(self.kind)
        if required is not None and required[1] is None:
            raise ValueError(f"Target of kind {self.kind!r} requires {required[0]!r}")


@dataclass(frozen=True, slots=True)
class Intent:
    """What the soul wants to happen. The soul emits only these.

    `kind` and `argument` must come from the closed vocabulary in
    `emet_sdk.intents`; construct via `emet_sdk.intents.make` to have that
    checked.
    """

    kind: str
    argument: str | None = None
    intensity: float = 0.5
    target: Target | None = None
    duration_ms: int | None = None
    priority: Priority = Priority.EXPRESSIVE
    interruptible: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError(f"intensity must be in [0.0, 1.0], got {self.intensity}")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must not be negative")

    @property
    def name(self) -> str:
        """Dotted form, e.g. 'express.curiosity'. Keys motion pack clips."""
        return f"{self.kind}.{self.argument}" if self.argument else self.kind


@dataclass(frozen=True, slots=True)
class Action:
    """A concrete instruction to one capability, produced by resolving an
    intent against a bound fallback rung.

    Plugins receive these and nothing else. `apply()` must return promptly;
    long moves are driven by repeated calls from the choreographer.
    """

    capability_id: str
    name: str                                    # tilt, expression, pulse, inflect, ...
    params: Mapping[str, Any] = field(default_factory=dict)
    intensity: float = 0.5
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Pose:
    """Joint id to angle in degrees. Body-relative, never raw servo counts."""

    joints: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Twist:
    """A desired velocity, handed to a locomotion plugin.

    The engine says how fast to go and how fast to turn. Everything below this
    line (wheel arithmetic, gait phase, balance) belongs to the plugin.
    """

    linear_mps: float = 0.0
    angular_rps: float = 0.0

    @property
    def is_stop(self) -> bool:
        return self.linear_mps == 0.0 and self.angular_rps == 0.0


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """What a plugin instance can *actually* do, reported after init.

    Fallback chains bind against this rather than against the YAML, because
    the manifest states intent and the hardware states reality. A servo that
    failed to initialise reports a narrower descriptor, and the chain falls
    through to the next rung instead of binding to something dead.
    """

    capability_id: str
    capability_type: str
    role: str | None = None
    form: str | None = None
    actions: frozenset[str] = frozenset()
    axes: frozenset[str] = frozenset()
    joints: tuple[str, ...] = ()
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class LocomotionDescriptor:
    """What a locomotion plugin reports about how this body moves.

    `can_turn_in_place` matters to the self-model: a body that cannot must
    describe turning honestly, and `move.turn_to` realises differently.
    """

    kinematics: str
    max_linear_mps: float
    max_angular_rps: float
    can_turn_in_place: bool
    can_translate_and_rotate: bool
    holonomic: bool = False


@dataclass(frozen=True, slots=True)
class WakeDescriptor:
    """What a wake word engine can actually hear, reported after `start()`.

    No chain binds against this, because there is no wake chain, but the boot
    check does. A soul asking for a phrase this instance cannot detect is a
    robot that will never answer to its own name, and unlike a missing head
    there is nothing to degrade to. That has to fail at boot, loudly.
    """

    engine: str
    #: Phrases this instance loaded a model for. Empty is legal and honest for
    #: an engine that failed to load one; it is not the same as `healthy=False`,
    #: which means the engine itself is broken.
    phrases: frozenset[str] = frozenset()
    #: Advisory, and deliberately NOT consulted by `can_detect`. Whether this
    #: *engine* can be pointed at an arbitrary phrase given a pronunciation, as
    #: a phonetic spotter can and a trained classifier cannot. Tooling uses it
    #: to explain the options; it says nothing about what this instance is
    #: currently listening for, which is `phrases`.
    supports_custom_phrases: bool = False
    #: The audio contract. The engine feeds frames at this rate and size; a
    #: plugin that needs something else says so here rather than resampling
    #: quietly and drifting.
    sample_rate: int = 16000
    frame_samples: int = 1280
    healthy: bool = True

    def can_detect(self, phrase: str) -> bool:
        """Whether this instance, as configured and started, would hear `phrase`.

        Does not consult `supports_custom_phrases`. An engine that *could* be
        configured for any phrase is still only listening for what it actually
        loaded, and conflating the two would let a healthy engine claim it
        hears a name nobody ever gave it.
        """
        return self.healthy and phrase in self.phrases


@dataclass(frozen=True, slots=True)
class WakeEvent:
    """The robot heard its name."""

    phrase: str
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")


@dataclass(frozen=True, slots=True)
class Transcript:
    """What speech recognition heard, so far or in full.

    A streaming recogniser sends words back while the person is still talking,
    and revises them: "what time" becomes "what time is it" becomes "what time
    is it in Tokyo". Each revision arrives as a partial carrying the whole text
    heard so far in this utterance, and `final=False`. When the turn ends, one
    more arrives with `final=True`, and that is the text the engine acts on.

    Cumulative rather than incremental on purpose. A caller showing a live
    caption replaces the line; it does not have to splice fragments, and a
    provider that reorders or retracts a word cannot leave a stale fragment
    behind. A recogniser that does not stream sends no partials and one final.
    """

    text: str
    final: bool = False
    #: Advisory. Providers that report no calibrated score say 1.0, which is
    #: honesty about the absence of a number rather than a claim of certainty.
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {self.confidence}")


@dataclass(frozen=True, slots=True)
class TranscriberDescriptor:
    """What a speech recogniser can actually do, reported after `start()`.

    The boot check reads `healthy` and `sample_rate`. A recogniser is built
    with the audio format the wake engine already fixed, because one
    microphone feeds both, and it echoes back the rate it will actually run
    at. If the two differ the engine refuses to start rather than letting a
    16 kHz stream be heard at 8 kHz, which does not fail, it just produces
    nonsense. Same rule as the detector: the consumer states the format, and
    a mismatch is loud.
    """

    provider: str
    #: The model this instance loaded, if the provider has such a thing. What
    #: was asked for is in the config; this is what answered.
    model: str | None = None
    #: Whether partial transcripts arrive while the person is still speaking.
    #: A batch recogniser says False and sends one final per utterance.
    streaming: bool = False
    sample_rate: int = 16000
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A function the language model may ask to have run.

    Vendor-neutral: `parameters` is a JSON Schema object and each provider
    translates it to its own tool format. Tools are how side effects leave
    the model (a memory to write, a fact to look up); what is *said* comes
    back as text.
    """

    name: str
    description: str
    parameters: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asking for a tool to run, with parsed arguments.

    Streamed once the arguments are complete. The caller runs the tool and
    answers with a `Message` of role `tool` carrying this call's `id`.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


#: Roles a `Message` may carry. The system prompt is not a message: it sits
#: on the `Prompt`, because every provider treats it differently.
MESSAGE_ROLES: frozenset[str] = frozenset({"user", "assistant", "tool"})


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of the conversation as the model sees it.

    `user` and `assistant` carry text. An `assistant` turn that asked for
    tools carries the calls beside its text; a `tool` turn answers one call
    and names it. Providers that want tool results in a different wrapper
    (a user turn holding result blocks, say) do the wrapping themselves.
    """

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in MESSAGE_ROLES:
            raise ValueError(f"role must be one of {sorted(MESSAGE_ROLES)}, got {self.role!r}")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("a tool message must name the call it answers (tool_call_id)")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only an assistant message carries tool_calls")


@dataclass(frozen=True, slots=True)
class Prompt:
    """Everything one request to a language model needs.

    Assembled by the engine: the persona, and in later releases the
    self-model and the memories the sensitivity floor lets through, become
    `system`; the conversation so far and the words just heard become
    `messages`. Nothing about which vendor answers is in here.

    `max_tokens` bounds one reply. A reply is spoken aloud, so the engine
    sets this low on purpose; a provider that hits it reports `length`.
    """

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of the reply, as it is generated. Concatenate in order."""

    text: str


#: Why a reply ended, in words every provider maps onto. `end` is the
#: natural finish; `tool` means the model wants tool results before it goes
#: on; `length` means `max_tokens` cut it off; `refusal` means the provider
#: declined to answer; `error` means the reply is incomplete and `error`
#: says why.
STOP_REASONS: frozenset[str] = frozenset({"end", "tool", "length", "refusal", "error"})


@dataclass(frozen=True, slots=True)
class ReplyDone:
    """The last event of a reply: the whole text, why it stopped, what it cost.

    Always the final event, on success and on failure alike. A caller that
    only wants the text reads it here; a caller that streamed the deltas
    reads `stop_reason` and the token counts.
    """

    text: str
    stop_reason: str = "end"
    tool_calls: tuple[ToolCall, ...] = ()
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.stop_reason not in STOP_REASONS:
            raise ValueError(
                f"stop_reason must be one of {sorted(STOP_REASONS)}, got {self.stop_reason!r}"
            )


#: What `LanguageModelPlugin.reply()` yields: text as it arrives, a tool call
#: once its arguments are complete, and one `ReplyDone` last.
ReplyEvent = TextDelta | ToolCall | ReplyDone


@dataclass(frozen=True, slots=True)
class LanguageModelDescriptor:
    """What a language model plugin can actually do, reported after `start()`.

    `healthy` is what the boot check reads: a provider whose key is missing,
    rejected, or unreachable says so here and in `health().detail`, and the
    engine refuses to run a robot that would hear and never answer.
    """

    provider: str
    #: The model this instance will ask for. What answered is on each
    #: `ReplyDone`, because a provider may substitute one.
    model: str | None = None
    #: Whether `TextDelta`s arrive before `ReplyDone`. A provider that does
    #: not stream sends the whole text as one delta.
    streaming: bool = True
    #: Whether `Prompt.tools` will be honoured. A provider without tool use
    #: says False and the engine leaves tools out of its prompts.
    tools: bool = True
    healthy: bool = True


@dataclass(frozen=True, slots=True)
class VoiceDescriptor:
    """What a speech synthesis plugin can actually do, reported after `start()`.

    The third provider descriptor, and the one the output side is built
    from. `sample_rate` is the rate the audio from `speak()` will arrive at,
    and the engine opens the sink at that rate rather than asking the voice
    to match a card: a local voice produces one rate and only one, and
    resampling in the engine would be a second place for audio to go
    wrong. The same rule as the wake engine, the other way round: the
    detector states the format and the microphone conforms; the voice states
    the format and the speaker conforms.

    `healthy` is what the boot check reads. A voice whose model file is
    missing, whose key is rejected or whose library is not installed says so
    here and in `health().detail`, and the engine refuses to run a robot that
    would answer and never be heard.
    """

    provider: str
    #: The voice this instance loaded: a Piper model name, a vendor's voice
    #: id. What was asked for is in the config; this is what will speak.
    model: str | None = None
    #: The rate of the audio `speak()` yields. Mono int16, always.
    sample_rate: int = 22050
    #: Whether audio arrives in pieces before the sentence is finished. A
    #: provider that synthesises a whole sentence at once yields one chunk
    #: and says False; the engine cannot tell them apart except by latency.
    streaming: bool = False
    healthy: bool = True


#: Audio is int16 throughout. Two bytes a sample, everywhere.
SAMPLE_BYTES = 2


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """The shape of the audio crossing the boundary. Mono int16, always.

    Defaults are what the shipped wake engine asks for. A body with a
    four-capsule array downmixes before this point, so nothing above the HAL
    counts microphones.
    """

    sample_rate: int = 16000
    frame_samples: int = 1280

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * SAMPLE_BYTES

    @property
    def frame_ms(self) -> float:
        return 1000.0 * self.frame_samples / self.sample_rate


@runtime_checkable
class AudioSource(Protocol):
    """Something producing mono int16 frames at a known rate.

    Here rather than in `emet_hal` because it is the seam the engine sees. The
    engine may import `emet_sdk` and nothing else, so a microphone reaches it
    as this Protocol and never as a concrete class, which is the same reason
    a servo reaches it as `ActuatorPlugin`. Put this type in the HAL and the
    engine cannot describe its own input.

    `read()` returns exactly `format.frame_bytes` bytes, or None when the
    source has ended: a file always does, a microphone never should.

    **Construction.** Implementations discovered through the `emet.audio`
    entry-point group are built as `cls(config, fmt)`, where `config` is the
    manifest's `audio.input` block and `fmt` the format the consumer needs.
    Same shape as a plugin receiving its capability block, and for the same
    reason: the caller has a name and a mapping, never a class it imported.
    """

    format: AudioFormat

    async def start(self) -> None: ...

    async def read(self) -> bytes | None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class AudioSink(Protocol):
    """Somewhere mono int16 audio goes. The other half of the hardware floor.

    Deliberately shaped the opposite way round from `AudioSource`, and kept a
    separate group for that reason: audio is *pushed* into a sink and *pulled*
    from a source. Forcing both into one contract would give every microphone a
    `play` it cannot honour.

    Splitting them also lets the two be chosen independently, which is how a
    wake failure gets debugged: read from a recording, play to a real speaker,
    or read from a microphone and write what was said to a file.

    **Construction** matches sources exactly. Implementations discovered
    through the `emet.audio_out` entry-point group are built as
    `cls(config, fmt)`, where `config` is the manifest's `audio.output` block.
    """

    format: AudioFormat

    async def start(self) -> None: ...

    async def play(self, pcm: bytes) -> None:
        """Play a whole buffer, returning when it has finished."""
        ...

    async def cancel(self) -> None:
        """Stop immediately, mid-word if need be.

        Barge-in depends on this: `DESIGN.md` §13 requires that speech during
        playback stops the audio rather than queueing behind it. A sink that
        cannot be interrupted makes the robot talk over the person correcting
        it, which is the single rudest thing it could do.
        """
        ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Health:
    """RSV. Polled by the engine; feeds proprioceptive self-model updates so
    that "my left wheel isn't responding" can enter context and the character
    can mention it.
    """

    ok: bool = True
    detail: str | None = None
    faults: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Reading:
    """One typed observation from a sensor.

    Sensors invert the flow of the rest of the system: the engine polls them
    and consumes what comes back. A sensor never emits an intent: deciding
    what an observation *means* is the engine's job, and letting hardware
    push intents would put a driver author in charge of the personality.
    """

    capability_id: str
    kind: str                                    # imu, range, touch, light, temp
    values: Mapping[str, float] = field(default_factory=dict)
    stale: bool = False


class Sensitivity(IntEnum):
    """Memory disclosure levels.

    OPEN and PERSONAL are left to the character's judgement. PRIVATE and
    SEALED are enforced in the retrieval query: the soul never receives them,
    so it cannot disclose them regardless of what it is talked into. A hard
    floor on the catastrophic cases, personality everywhere above it.
    """

    OPEN = 0      # freely recalled with anyone present
    PERSONAL = 1  # recalled with the subject; otherwise gated on disclosure setting
    PRIVATE = 2   # only when the source person is the sole participant
    SEALED = 3    # never enters a prompt; retained for inspection and export only


class MemoryKind(StrEnum):
    FACT = "fact"
    EVENT = "event"
    PREFERENCE = "preference"
    PROMISE = "promise"
    FEELING = "feeling"
    SELF = "self"
