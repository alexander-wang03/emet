"""Fallback chains: the mechanism that makes one soul run any body.

A chain maps an intent to an ordered list of rungs. At boot the engine walks
each chain top to bottom and binds the first rung whose actuator selector
matches something in the manifest. A body with a pan/tilt head tilts it; a
body with only eyes squints; a body with neither says "hm?".

Chains are data, live in the SDK, and are overridable per-soul. Degradation is
declared, not improvised (principle 5): there is no `if hasattr(...)`
anywhere in the engine.

**The rule this module exists to enforce:** every chain's final rung must be a
voice rung. Because the hardware floor is a microphone and a speaker, a chain
ending in voice can never fail to bind, which is what makes principle 2,
every intent is always satisfiable, mechanical rather than aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from emet_sdk.types import CapabilityDescriptor

__all__ = [
    "ActuatorSelector",
    "Rung",
    "Chain",
    "ChainMode",
    "ChainError",
    "UnterminatedChainError",
    "parse_chain_set",
]


class ChainError(ValueError):
    """A malformed chain."""


class UnterminatedChainError(ChainError):
    """A chain whose final rung is not a voice rung.

    Its own exception type because this is worse than a typo: a chain that
    can fail to bind on a sufficiently bare body, which breaks the guarantee
    the whole abstraction rests on.
    """


class ChainMode:
    #: P0. Bind the first matching rung and stop.
    FIRST = "first"
    #: RSV. Bind every matching rung, so a well-equipped body tilts its head
    #: *and* squints *and* pulses. Needs per-actuator arbitration, so it waits.
    ALL = "all"

    VALUES = frozenset({FIRST, ALL})


@dataclass(frozen=True, slots=True)
class ActuatorSelector:
    """Which part of a body a rung wants.

    Selectors name roles and axes (`head`, `pitch`, `eyes`), never drivers or
    channels. This is the seam that keeps principle 1 intact while still
    letting a chain be specific about what it needs.
    """

    role: str | None = None
    type: str | None = None
    axis: str | None = None
    voice: bool = False

    def __post_init__(self) -> None:
        if self.voice and (self.role or self.type or self.axis):
            raise ChainError(
                "a voice rung selector takes no role, type, or axis: "
                "voice is the floor, and it is unconditional"
            )
        if not self.voice and not (self.role or self.type or self.axis):
            raise ChainError("selector must constrain at least one of role, type, axis")

    def __str__(self) -> str:
        """Render the way it is written in a chain file, for error messages."""
        if self.voice:
            return "{voice: true}"
        parts = [
            f"{k}: {v}"
            for k, v in (("role", self.role), ("type", self.type), ("axis", self.axis))
            if v is not None
        ]
        return "{" + ", ".join(parts) + "}"

    def matches(self, cap: CapabilityDescriptor) -> bool:
        """Whether this selector binds to a capability instance.

        Matched against what the plugin *reported*, not against the YAML: a
        servo group that failed to initialise reports a narrower descriptor
        and the chain falls through rather than binding to something dead.
        """
        if self.voice:
            return False  # voice is not a manifest capability; the engine binds it directly
        if not cap.healthy:
            return False
        if self.role is not None and cap.role != self.role:
            return False
        if self.type is not None and cap.capability_type != self.type:
            return False
        if self.axis is not None and self.axis not in cap.axes:
            return False
        return True


@dataclass(frozen=True, slots=True)
class Rung:
    actuator: ActuatorSelector
    action: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_voice(self) -> bool:
        return self.actuator.voice


@dataclass(frozen=True, slots=True)
class Chain:
    """An intent's degradation ladder, most expressive first."""

    intent: str
    rungs: tuple[Rung, ...]
    mode: str = ChainMode.FIRST

    def __post_init__(self) -> None:
        if not self.rungs:
            raise ChainError(f"chain {self.intent!r} has no rungs")
        if self.mode not in ChainMode.VALUES:
            raise ChainError(
                f"chain {self.intent!r}: unknown mode {self.mode!r}, "
                f"expected one of {', '.join(sorted(ChainMode.VALUES))}"
            )
        if not self.rungs[-1].is_voice:
            raise UnterminatedChainError(
                f"chain {self.intent!r} does not terminate in a voice rung. "
                f"Its last rung selects {self.rungs[-1].actuator}, which a bare body "
                f"may not have. Every chain must end in `actuator: {{voice: true}}` so "
                f"that no intent can fail for lack of hardware (principle 2)."
            )
        for i, rung in enumerate(self.rungs[:-1]):
            if rung.is_voice:
                raise ChainError(
                    f"chain {self.intent!r}: rung {i} is a voice rung but is not last. "
                    f"Voice always binds, so every rung after it would be unreachable."
                )

    @property
    def voice_rung(self) -> Rung:
        return self.rungs[-1]


def _parse_selector(raw: Any, *, where: str) -> ActuatorSelector:
    if not isinstance(raw, Mapping):
        raise ChainError(f"{where}: 'actuator' must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - {"role", "type", "axis", "voice"}
    if unknown:
        raise ChainError(f"{where}: unknown selector keys {sorted(unknown)}")
    try:
        return ActuatorSelector(
            role=raw.get("role"),
            type=raw.get("type"),
            axis=raw.get("axis"),
            voice=bool(raw.get("voice", False)),
        )
    except ChainError as exc:
        raise ChainError(f"{where}: {exc}") from None


def _parse_rung(raw: Any, *, where: str) -> Rung:
    if not isinstance(raw, Mapping):
        raise ChainError(f"{where}: rung must be a mapping, got {type(raw).__name__}")
    if "action" not in raw:
        raise ChainError(f"{where}: rung is missing 'action'")
    if "actuator" not in raw:
        raise ChainError(f"{where}: rung is missing 'actuator'")
    params = raw.get("params", {})
    if not isinstance(params, Mapping):
        raise ChainError(f"{where}: 'params' must be a mapping")
    return Rung(
        actuator=_parse_selector(raw["actuator"], where=where),
        action=str(raw["action"]),
        params=dict(params),
    )


def parse_chain_set(raw: Mapping[str, Any]) -> dict[str, Chain]:
    """Parse a loaded chain file into Chain objects, validating as it goes.

    Returns a mapping of dotted intent name to chain. Raises `ChainError` (or
    `UnterminatedChainError`) on the first problem, naming the chain.
    """
    chains: dict[str, Chain] = {}
    for intent, body in raw.items():
        where = f"chain {intent!r}"
        if not isinstance(body, Mapping):
            raise ChainError(f"{where}: expected a mapping with 'rungs'")
        rungs_raw = body.get("rungs")
        if not isinstance(rungs_raw, Sequence) or isinstance(rungs_raw, (str, bytes)):
            raise ChainError(f"{where}: 'rungs' must be a list")
        rungs = tuple(
            _parse_rung(r, where=f"{where} rung {i}") for i, r in enumerate(rungs_raw)
        )
        chains[intent] = Chain(
            intent=intent,
            rungs=rungs,
            mode=str(body.get("mode", ChainMode.FIRST)),
        )
    return chains


def unterminated(chains: Iterable[Chain]) -> list[str]:
    """Names of chains that do not end in voice.

    Constructing a `Chain` already rejects these, so this is for reporting
    over sets assembled by other means.
    """
    return [c.intent for c in chains if not c.rungs[-1].is_voice]
