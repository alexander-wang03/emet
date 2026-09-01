"""The plugin contract. Breaking it is a major version bump.

Four categories, and the split is not arbitrary:

* **Actuators** receive `Action`s and do something physical. The engine tells
  them what should happen; how is theirs.
* **Sensors** invert the flow — the engine polls, they return `Reading`s. A
  sensor never emits an intent, because deciding what an observation *means*
  belongs to the engine. Letting a driver push intents would put its author in
  charge of the personality.
* **Locomotion** plugins receive a `Twist` and decide what it means for a body.
  Wheels solve it with arithmetic, treads with the same arithmetic and
  different slip assumptions, and a legged plugin with a gait generator. The
  engine above them does not know or care.
* **Wake** plugins listen for the robot's name and nothing else. They are the
  only category configured jointly by a body and a soul, and the only one with
  no fallback beneath it.

**Why there is no VAD category.** Voice activity detection looks like it
belongs beside wake, and does not. It is not a swap point: there is one real
answer (Silero), it is MIT licensed so it carries none of the rug-pull risk
that made wake a category, and its output feeds turn-taking — `patience_ms`,
the trailing-clause heuristic — which is personality rather than hardware. A
seam there would decouple nothing. The asymmetry settles it: adding a category
later is a minor version bump, removing one is a major bump, so under
uncertainty the cheap direction is to leave it out.

Everything here is hardware-agnostic on purpose. A plugin may talk to a servo
board over I2C; nothing in this module knows that, and nothing in the soul
layer ever will.

**On `describe()` and `start()`.** A descriptor reports what an instance can
*actually* do, which is only knowable after initialisation — so `start()`
exists, and `describe()` is defined to be called after it. A joint group whose
servo board failed to answer returns a narrower descriptor (or `healthy=False`),
and fallback chains bind past it to the next rung. That is the difference
between "the manifest says there is a head" and "there is a working head", and
it is why chains bind against descriptors rather than against the YAML.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping

from emet_sdk.types import (
    Action,
    CapabilityDescriptor,
    Health,
    LocomotionDescriptor,
    Reading,
    Twist,
    WakeDescriptor,
    WakeEvent,
)

__all__ = [
    "Plugin",
    "CapabilityPlugin",
    "ActuatorPlugin",
    "SensorPlugin",
    "LocomotionPlugin",
    "WakePlugin",
    "PluginError",
]


class PluginError(RuntimeError):
    """A plugin failed in a way the engine should hear about.

    Raising this from `start()` is the supported way to say "this hardware is
    not present or not working". The engine records it, marks the capability
    unhealthy, and lets chains fall through — it does not crash, because a
    robot with a dead servo is still a robot that can talk.
    """


class Plugin(ABC):
    """Lifecycle, and only lifecycle.

    Every category starts, shuts down, and reports health the same way. What a
    plugin is *constructed from* differs: three categories are built from one
    entry in a manifest's `capabilities` list, and wake is built from
    `audio.wake` plus a phrase the soul supplies. Construction therefore
    belongs to the subclasses, so that no category inherits a signature it has
    to contradict.
    """

    async def start(self) -> None:
        """Bring the hardware up. Raise `PluginError` if it is not there.

        Default is a no-op, so a plugin with nothing to initialise says nothing.
        """

    async def shutdown(self) -> None:
        """Called on SIGTERM and on fault. Must leave hardware safe.

        "Safe" means what it means for this device: servos relaxed or held,
        motors stopped, LEDs off. This runs on the way down from a crash as
        well as a clean exit, so it must not assume `start()` succeeded.
        """

    def health(self) -> Health:
        """`RSV`. Polled by the engine; feeds proprioceptive self-model updates,
        so that "my left wheel isn't responding" can enter the character's
        context and be mentioned out loud.
        """
        return Health()


class CapabilityPlugin(Plugin):
    """A plugin built from one entry in the manifest's `capabilities` list.

    The common case, and the reason `capability_type`, `capability_id` and
    `params` live here rather than on `Plugin`: wake has none of them.
    """

    #: The manifest `type` this plugin implements — joint_group, drive,
    #: display, light, camera, sensor. Locomotion plugins leave this empty and
    #: set `kinematics` instead.
    capability_type: ClassVar[str] = ""

    def __init__(self, capability: Mapping[str, Any]) -> None:
        """Receive the whole capability block from the manifest.

        Not just `driver.params`: a plugin needs `role`, `joints`, `form` and
        the rest to answer `describe()` honestly. What it does with the
        wiring-specific `params` is entirely its own business — Emet passes
        them through without looking at them.
        """
        self.capability: Mapping[str, Any] = capability
        self.capability_id: str = str(capability.get("id", ""))
        self.params: Mapping[str, Any] = dict(
            (capability.get("driver") or {}).get("params") or {}
        )


class ActuatorPlugin(CapabilityPlugin):
    """Something the robot can act with: joints, displays, lights."""

    @abstractmethod
    def describe(self) -> CapabilityDescriptor:
        """Report what this instance can actually do, after `start()`.

        The engine binds fallback chains against this, not against the YAML.
        Report narrowly and honestly: claiming an axis you cannot drive means
        a chain binds to you and the robot silently does nothing, which is
        worse than falling through to a light ring.
        """

    @abstractmethod
    async def apply(self, action: Action) -> None:
        """Execute one action.

        **Must return promptly.** Long moves are driven by repeated `apply()`
        calls from the choreographer at 50Hz — a plugin that sleeps for the
        duration of a gesture blocks the loop that would let a higher-priority
        intent preempt it.
        """

    async def home(self) -> None:
        """Return to the resting pose declared in the manifest."""


class SensorPlugin(CapabilityPlugin):
    """Something the robot observes with. Never emits intents."""

    @abstractmethod
    def describe(self) -> CapabilityDescriptor:
        """Report what this sensor provides, after `start()`."""

    @abstractmethod
    async def poll(self) -> Reading:
        """Return the latest observation.

        Called by the engine on its own schedule. If the value is old, say so
        with `Reading(stale=True)` rather than returning a stale number as if
        it were fresh — a robot acting confidently on a dead sensor is worse
        than one that knows it cannot see.
        """


class LocomotionPlugin(CapabilityPlugin):
    """How a body moves. The engine asks; it does not know.

    This is the seam that keeps `drive.kinematics` an open enum: the engine
    contains no wheels-and-treads logic, so `legged` is not a special case to
    be added later, it is a plugin nobody has written yet.
    """

    #: The string matched against `drive.kinematics` in the manifest, and the
    #: entry-point name this plugin registers under.
    kinematics: ClassVar[str] = ""

    @abstractmethod
    def describe(self) -> LocomotionDescriptor:
        """Report the achievable velocity envelope and what turning means here.

        `can_turn_in_place` matters beyond motion planning: it feeds the
        self-model, so a body that must arc around says so honestly rather
        than promising to turn and then not.
        """

    @abstractmethod
    async def command(self, twist: Twist) -> None:
        """Accept a desired linear and angular velocity.

        Everything below this line belongs to the plugin: wheel arithmetic,
        gait phase, balance, slip compensation. The engine has said what it
        wants; how is not its business.
        """

    async def stop(self, hard: bool = False) -> None:
        """Come to rest. `hard=True` means brake now, safety is preempting."""
        await self.command(Twist())


class WakePlugin(Plugin):
    """What listens for the robot's name.

    Wake sits oddly among the categories, on purpose. It is not a manifest
    capability, for the same reason voice is not: the hardware floor already
    guarantees a microphone, so there is nothing to declare. What varies is
    which detector runs on that microphone, and that is a deployment fact, so
    it is configured from `audio.wake` on the body.

    The *phrase* arrives from the other side. `identity.wake_word` is a soul
    field, which makes this the one place where a soul-declared value reaches
    a plugin constructor. That does not breach principle 1: the soul says
    which name it answers to, never which engine hears it, on what device, at
    what threshold.

    **Why this is a plugin category rather than a dependency.** Picovoice
    disabled every free Porcupine access key on 30 June 2026. Anything that
    had wired a single wake word engine in directly stopped waking that day,
    and could not be fixed by its owner. The seam is the whole defence, and it
    is the same seam as `drive.kinematics`: the engine holds no list of known
    detectors, so a better one is a package nobody has published yet.
    """

    #: The string matched against `audio.wake.engine` in the manifest, and the
    #: entry-point name this plugin registers under.
    engine: ClassVar[str] = ""

    def __init__(self, config: Mapping[str, Any], phrase: str) -> None:
        """Receive the `audio.wake` block and the phrase to listen for.

        Not a `CapabilityPlugin`, which is why this signature is free to
        differ rather than contradict one: wake declares no capability, so
        there is no `driver.params` to unwrap and no capability id to carry.

        Taking `phrase` here rather than through a later `configure()` means
        an instance cannot exist without knowing what it listens for.
        """
        self.config: Mapping[str, Any] = config
        self.params: Mapping[str, Any] = dict(config.get("params") or {})
        #: The phrase from the soul's `identity.wake_word`.
        self.phrase = phrase

    @abstractmethod
    def describe(self) -> WakeDescriptor:
        """Report what this instance can actually hear, after `start()`.

        Report narrowly. An engine that could not load a model for
        `self.phrase` must leave it out of `phrases` rather than claim it,
        because there is no next rung here. A chain that cannot find a head
        falls through to a light ring; a robot that cannot hear its name just
        never answers, and looks broken rather than limited.
        """

    @abstractmethod
    async def process(self, frame: bytes) -> WakeEvent | None:
        """Consume one frame of audio. Return an event if the phrase was heard.

        Audio is pushed in rather than pulled out so that the engine keeps sole
        ownership of the microphone: one capture loop, one buffer, and a
        detector that never competes with speech recognition for the device.

        Frames arrive at the rate and size the descriptor asked for. **Must
        return promptly** — this runs on every frame of the capture path, so
        blocking here drops audio and delays the wake it is meant to catch.
        """

    async def reset(self) -> None:
        """Drop accumulated audio state. Called once after a wake fires.

        Default is a no-op. Streaming detectors holding a rolling buffer
        override it so that one utterance cannot trigger twice.
        """
