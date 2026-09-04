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
    lower one is dropped rather than queued. Per-actuator blending — expressing
    curiosity with the eyes *while* attending with the head — is V1.
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
    line — wheel arithmetic, gait phase, balance — belongs to the plugin.
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

    No chain binds against this, because there is no wake chain — but the boot
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
    as this Protocol and never as a concrete class — which is the same reason
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
    and consumes what comes back. A sensor never emits an intent — deciding
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
    SEALED are enforced in the retrieval query — the soul never receives them,
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
