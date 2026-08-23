"""Differential drive — two independently driven wheels.

The oldest trick in mobile robotics: drive both wheels the same and you go
straight; drive them at different speeds and you turn; drive them opposite and
you spin on the spot.

This plugin ships in a release with no hardware drivers at all, which looks
inconsistent until you see what it actually is. A locomotion plugin is
*arithmetic* — it turns a desired velocity into per-wheel speeds and hands
those to whatever driver owns the motors. It touches no GPIO, needs no robot,
and is fully testable against the mock. Shipping it is how the seam from the
design spec gets proven rather than asserted: the engine says "go forward at
0.2 m/s while turning left", and something that is not the engine works out
what the wheels should do.

The maths, for anyone checking it:

    v_left  = (v - w * W/2) / r        (rad/s at the wheel)
    v_right = (v + w * W/2) / r

where `v` is linear m/s, `w` is angular rad/s, `W` is the track width, and `r`
is the wheel radius.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from emet_sdk.plugin import LocomotionPlugin, PluginError
from emet_sdk.types import LocomotionDescriptor, Twist

__all__ = ["DifferentialDrive", "WheelSpeeds"]

log = logging.getLogger("emet_hal.differential")


class WheelSpeeds(dict):
    """Left and right wheel angular velocity, rad/s. A dict so a driver can
    take it without importing anything from here."""

    def __init__(self, left: float, right: float) -> None:
        super().__init__(left=left, right=right)

    @property
    def left(self) -> float:
        return self["left"]

    @property
    def right(self) -> float:
        return self["right"]


class DifferentialDrive(LocomotionPlugin):
    """Two-wheel differential steering."""

    kinematics = "differential"
    capability_type = "drive"

    #: How much of a commanded turn is lost to the ground. Wheels barely
    #: scrub; tracks do, which is the only thing the tracked subclass changes.
    slip_factor: float = 1.0

    def __init__(self, capability: Mapping[str, Any]) -> None:
        super().__init__(capability)
        geometry = capability.get("geometry") or {}
        limits = capability.get("limits") or {}

        self.wheel_radius_m = float(geometry.get("wheel_radius_m") or 0.0)
        self.track_width_m = float(geometry.get("track_width_m") or 0.0)
        self.max_linear_mps = float(limits.get("max_linear_mps") or 0.0)
        self.max_angular_rps = float(limits.get("max_angular_rps") or 0.0)

        self.last: WheelSpeeds | None = None

    async def start(self) -> None:
        # Geometry is what makes the arithmetic meaningful. Without it every
        # command silently produces zero, which looks like a dead motor and
        # sends the builder hunting through wiring instead of the manifest.
        missing = [
            name
            for name, value in (
                ("geometry.wheel_radius_m", self.wheel_radius_m),
                ("geometry.track_width_m", self.track_width_m),
            )
            if value <= 0
        ]
        if missing:
            raise PluginError(
                f"{self.kinematics} drive {self.capability_id!r} needs "
                f"{' and '.join(missing)} to convert a velocity into wheel speeds"
            )

    def describe(self) -> LocomotionDescriptor:
        return LocomotionDescriptor(
            kinematics=self.kinematics,
            max_linear_mps=self.max_linear_mps,
            max_angular_rps=self.max_angular_rps,
            # Opposed wheels spin the body about its own centre.
            can_turn_in_place=True,
            can_translate_and_rotate=True,
            # Cannot strafe: no sideways motion without turning first.
            holonomic=False,
        )

    def solve(self, twist: Twist) -> WheelSpeeds:
        """Convert a body velocity into wheel angular velocities.

        Separate from `command()` so it can be tested, and read, without any
        hardware or async machinery in the way.
        """
        v = _clamp(twist.linear_mps, self.max_linear_mps)
        w = _clamp(twist.angular_rps, self.max_angular_rps) * self.slip_factor

        half_track = self.track_width_m / 2.0
        left = (v - w * half_track) / self.wheel_radius_m
        right = (v + w * half_track) / self.wheel_radius_m
        return WheelSpeeds(left=left, right=right)

    async def command(self, twist: Twist) -> None:
        self.last = self.solve(twist)
        log.debug(
            "%s %s: left=%.3f rad/s right=%.3f rad/s",
            self.kinematics,
            self.capability_id,
            self.last.left,
            self.last.right,
        )

    async def stop(self, hard: bool = False) -> None:
        await self.command(Twist())

    async def shutdown(self) -> None:
        await self.stop(hard=True)


def _clamp(value: float, limit: float) -> float:
    """Respect the declared limit. A limit of zero means unset, not immobile."""
    if limit <= 0:
        return value
    return max(-limit, min(limit, value))
