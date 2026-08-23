"""The canonical intent vocabulary.

A **closed set**. The soul may emit only these, and adding one is a minor
version bump of the SDK.

Closed also means an unrecognised intent is a *validation failure*, not a
no-op — which is why the intents that arrive in later releases are reserved
here from P0. A soul bundle written today that reaches for `manipulate` is
merely ineffective; without reservation it would fail to load once the name
was finally added. Bundles are exactly the artifact strangers publish, copy,
and keep for years, so that distinction is worth four entries in a table.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from emet_sdk.types import Intent, Priority, Target

__all__ = [
    "P0_INTENTS",
    "RESERVED_INTENTS",
    "ALL_INTENTS",
    "DEFAULT_PRIORITY",
    "FREE_ARGUMENT",
    "UnknownIntentError",
    "is_reserved",
    "validate_intent_name",
    "make",
]


#: Sentinel for intents whose argument is arbitrary text rather than a
#: member of a fixed set — `speak` carries an utterance, not a keyword.
FREE_ARGUMENT = frozenset({"*"})


#: Implemented in Prima Materia. kind -> legal arguments.
P0_INTENTS: Mapping[str, frozenset[str]] = MappingProxyType({
    # Always available: the terminal rung of every fallback chain.
    "speak": FREE_ARGUMENT,

    # Backchannel. Must fire within 300ms of the user's endpoint, which is
    # why it is served by a local micro-model rather than the cloud.
    "acknowledge": frozenset(),

    "attend": frozenset({"speaker", "bearing", "person", "none"}),

    # The core expressive set. Deliberately small — a body renders a handful
    # of states legibly, and a longer list would mostly collapse onto them.
    "express": frozenset({
        "curiosity", "delight", "confusion", "concern",
        "amusement", "boredom", "surprise", "affection", "thinking",
    }),

    # System state, not emotion. Distinct from `express` so that a soul
    # cannot suppress it: a muted robot must look muted regardless of taste.
    "signal": frozenset({
        "booting", "listening", "thinking", "speaking",
        "muted", "offline", "error",
    }),

    "idle": frozenset({"settle", "look_around", "fidget", "doze"}),

    # Only binds if a drive capability exists; otherwise it degrades to voice
    # like everything else.
    "move": frozenset({"approach", "retreat", "turn_to", "wander", "stop"}),
})


#: Reserved from P0: legal to emit, logged at debug level and dropped by the
#: engine until the release that implements them.
RESERVED_INTENTS: Mapping[str, frozenset[str]] = MappingProxyType({
    "manipulate": frozenset({"grasp", "release", "offer"}),
    "navigate": FREE_ARGUMENT,                        # a named place
    "gesture": FREE_ARGUMENT,                         # a clip from a motion pack
    "attend_joint": FREE_ARGUMENT,                    # look at your own hand
})


ALL_INTENTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {**P0_INTENTS, **RESERVED_INTENTS}
)


#: Arbitration defaults. Fixed priority: highest wins, ties break by recency.
DEFAULT_PRIORITY: Mapping[str, Priority] = MappingProxyType({
    "speak": Priority.SPEECH,
    "acknowledge": Priority.SPEECH,
    "signal": Priority.SIGNAL,
    "attend": Priority.ATTENTION,
    "express": Priority.EXPRESSIVE,
    "move": Priority.LOCOMOTION,
    "idle": Priority.IDLE,
    # Reserved intents inherit sensible homes so that arbitration does not
    # need changing when their behaviour lands.
    "manipulate": Priority.LOCOMOTION,
    "navigate": Priority.LOCOMOTION,
    "gesture": Priority.EXPRESSIVE,
    "attend_joint": Priority.ATTENTION,
})


class UnknownIntentError(ValueError):
    """Raised for an intent outside the closed vocabulary.

    Distinct from a schema error and from a missing plugin: this means the
    soul asked for something that does not exist, which is a bug in the soul.
    """


def is_reserved(kind: str) -> bool:
    """True if `kind` is reserved — accepted, but dropped by the P0 engine."""
    return kind in RESERVED_INTENTS


def validate_intent_name(kind: str, argument: str | None = None) -> None:
    """Check a kind/argument pair against the closed vocabulary.

    Raises `UnknownIntentError` with the legal alternatives listed, because an
    error that does not tell you what *was* allowed costs support hours.
    """
    if kind not in ALL_INTENTS:
        known = ", ".join(sorted(ALL_INTENTS))
        raise UnknownIntentError(
            f"unknown intent kind {kind!r}. The vocabulary is closed; known kinds are: {known}"
        )

    allowed = ALL_INTENTS[kind]

    if allowed is FREE_ARGUMENT:
        return

    if not allowed:
        if argument is not None:
            raise UnknownIntentError(f"intent {kind!r} takes no argument, got {argument!r}")
        return

    if argument is None:
        raise UnknownIntentError(
            f"intent {kind!r} requires an argument, one of: {', '.join(sorted(allowed))}"
        )

    if argument not in allowed:
        raise UnknownIntentError(
            f"unknown argument {argument!r} for intent {kind!r}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )


def parse(name: str) -> tuple[str, str | None]:
    """Split a dotted intent name ('express.curiosity') into its parts."""
    kind, _, argument = name.partition(".")
    return kind, (argument or None)


def make(
    kind: str,
    argument: str | None = None,
    *,
    intensity: float = 0.5,
    target: Target | None = None,
    duration_ms: int | None = None,
    priority: Priority | None = None,
    interruptible: bool = True,
) -> Intent:
    """Construct an Intent, checking it against the closed vocabulary.

    Prefer this over calling `Intent(...)` directly — the dataclass stays
    permissive so that the engine can round-trip intents it did not create,
    but anything the soul emits should come through here.
    """
    validate_intent_name(kind, argument)
    return Intent(
        kind=kind,
        argument=argument,
        intensity=intensity,
        target=target,
        duration_ms=duration_ms,
        priority=priority if priority is not None else DEFAULT_PRIORITY.get(kind, Priority.EXPRESSIVE),
        interruptible=interruptible,
    )
