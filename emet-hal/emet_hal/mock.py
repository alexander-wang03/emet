"""Plugins that pretend.

`MockActuator` accepts any action and records it instead of moving anything.
That sounds like a testing convenience, and it is, but it is also the way most
development on Emet will actually happen: the whole stack — chain resolution,
arbitration, the choreographer, an entire conversation — can be exercised on a
laptop with no robot attached.

It matters for contributors too. Someone writing a driver for a servo board
they own still needs to see what the choreographer sends and when. Running the
mock alongside answers that without a second set of hardware.

The mock deliberately describes itself from the manifest rather than inventing
capabilities: point it at a `joint_group` block and it reports that group's
joints and axes, so chains bind exactly as they would against the real thing.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from emet_sdk.plugin import ActuatorPlugin, SensorPlugin
from emet_sdk.types import Action, CapabilityDescriptor, Health, Reading

__all__ = ["MockActuator", "MockSensor"]

log = logging.getLogger("emet_hal.mock")

#: What each capability type can be asked to do. Used only so that the mock
#: reports a plausible `actions` set; the mock itself accepts anything.
_ACTIONS_BY_TYPE: Mapping[str, frozenset[str]] = {
    "joint_group": frozenset(
        {"tilt", "bob", "shake", "nod", "droop", "drift", "sweep", "jitter", "look_to", "home"}
    ),
    "display": frozenset({"expression", "gaze", "saccade"}),
    "light": frozenset({"pulse", "fade", "flash", "solid", "rotate", "modulate"}),
    "drive": frozenset({"drive_to", "turn_to", "wander", "stop"}),
    "camera": frozenset({"capture"}),
}

_AXES = frozenset({"yaw", "pitch", "roll", "linear"})


class MockActuator(ActuatorPlugin):
    """An actuator that logs what it was asked to do and does nothing.

    Set `params.fail_on_start: true` to simulate hardware that is wired but
    dead. That path is worth exercising deliberately — it is how you check
    that a chain falls through to its next rung instead of binding to
    something that will never move.
    """

    def __init__(self, capability: Mapping[str, Any]) -> None:
        super().__init__(capability)
        self.applied: list[Action] = []
        self.started = False
        self._fault: str | None = None

    async def start(self) -> None:
        if self.params.get("fail_on_start"):
            # Not an exception: a dead actuator should narrow the descriptor,
            # not take down a robot that can still talk.
            self._fault = str(self.params.get("fault_detail") or "simulated hardware fault")
            log.warning("mock %s: %s", self.capability_id, self._fault)
            return
        self.started = True

    def describe(self) -> CapabilityDescriptor:
        cap_type = str(self.capability.get("type") or "")
        joints = tuple(
            str(j["id"])
            for j in (self.capability.get("joints") or [])
            if isinstance(j, Mapping) and "id" in j
        )
        axes = frozenset(
            str(j["axis"])
            for j in (self.capability.get("joints") or [])
            if isinstance(j, Mapping) and j.get("axis") in _AXES
        )
        return CapabilityDescriptor(
            capability_id=self.capability_id,
            capability_type=cap_type,
            role=self.capability.get("role"),
            form=self.capability.get("form"),
            actions=_ACTIONS_BY_TYPE.get(cap_type, frozenset()),
            axes=axes,
            joints=joints,
            healthy=self._fault is None,
        )

    async def apply(self, action: Action) -> None:
        self.applied.append(action)
        log.info(
            "mock %s: %s(%s)",
            self.capability_id,
            action.name,
            ", ".join(f"{k}={v}" for k, v in sorted(action.params.items())),
        )

    async def home(self) -> None:
        await self.apply(Action(capability_id=self.capability_id, name="home"))

    async def shutdown(self) -> None:
        self.started = False

    def health(self) -> Health:
        if self._fault:
            return Health(ok=False, detail=self._fault, faults=("start_failed",))
        return Health()


class MockSensor(SensorPlugin):
    """A sensor that returns whatever `params.values` says, forever.

    Enough to exercise the polling path and the `stale` flag without owning an
    IMU.
    """

    def __init__(self, capability: Mapping[str, Any]) -> None:
        super().__init__(capability)
        self.polls = 0

    def describe(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id=self.capability_id,
            capability_type="sensor",
            role=self.capability.get("role"),
            actions=frozenset(),
            healthy=True,
        )

    async def poll(self) -> Reading:
        self.polls += 1
        raw = self.params.get("values") or {}
        return Reading(
            capability_id=self.capability_id,
            kind=str(self.capability.get("sensor_kind") or "unknown"),
            values={str(k): float(v) for k, v in raw.items()},
            stale=bool(self.params.get("stale", False)),
        )
