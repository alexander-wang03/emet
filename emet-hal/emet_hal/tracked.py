"""Tracked drive: treads instead of wheels.

Shares its arithmetic with differential drive entirely, and differs in one
physical fact: **tracks scrub when they turn.** A wheel turning follows an arc
it rolls along; a track turning has to drag sideways against the ground the
whole way. The commanded rotation is therefore not the rotation you get.

Emet models that with a single slip factor rather than anything cleverer,
because anything cleverer needs surface-specific parameters that a builder
cannot measure and would end up guessing. One honest fudge factor, tunable per
robot, beats a model that looks rigorous and is wrong on carpet.

The existence of this file is the argument for the locomotion seam in
miniature. Wheels and treads are close enough to share every line of maths and
different enough that hardcoding either into the engine would have been wrong.
"""

from __future__ import annotations

from typing import Any, Mapping

from emet_sdk.types import LocomotionDescriptor

from emet_hal.differential import DifferentialDrive

__all__ = ["TrackedDrive"]


class TrackedDrive(DifferentialDrive):
    """Differential steering, with the scrub that treads actually have."""

    kinematics = "tracked"

    #: Fraction of a commanded turn that survives the scrub. Empirical, and
    #: the honest default for a small robot on a hard floor. Override per body
    #: with `driver.params.slip_factor`.
    DEFAULT_SLIP = 0.85

    def __init__(self, capability: Mapping[str, Any]) -> None:
        super().__init__(capability)
        self.slip_factor = float(self.params.get("slip_factor", self.DEFAULT_SLIP))

    def describe(self) -> LocomotionDescriptor:
        base = super().describe()
        # Tracks turn in place happily, that is what they are good at, but
        # the achievable rate is lower than the geometry alone predicts, and
        # the self-model should not promise what the treads cannot deliver.
        return LocomotionDescriptor(
            kinematics=self.kinematics,
            max_linear_mps=base.max_linear_mps,
            max_angular_rps=base.max_angular_rps * self.slip_factor,
            can_turn_in_place=True,
            can_translate_and_rotate=True,
            holonomic=False,
        )
