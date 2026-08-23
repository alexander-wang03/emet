"""Chain resolution — deciding what each intent means on a particular body.

    (chains, capability descriptors)  ->  binding table

Run **once at boot**, not per intent. Walk each chain top to bottom, bind the
first rung whose selector matches something the body actually has, and record
the result. At runtime, performing an intent is a dictionary lookup.

That is principle 5 made concrete: degradation is declared in data and resolved
once, rather than improvised with `if hasattr(...)` scattered through the
engine. On a robot with a pan/tilt head, curiosity is a head tilt. On one with
only eyes, a squint. On a bare speakerphone, "hm?". Same soul, same chains,
three different bodies, zero conditionals.

This lives in the SDK rather than the engine because it is a pure function over
contract data — no state, no I/O, no personality — and because a HAL
contributor needs to see where their driver binds without running an engine.
The choreographer, which is stateful and runs at 50Hz, does not.

**Why the table records failures too.** Every binding carries the rungs it
skipped and why. "Why doesn't my robot tilt its head?" is the question this
answers: the table shows curiosity bound to the light ring because the head
group reported unhealthy, which is a different problem from a missing chain or
a bad selector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from emet_sdk.chains import Chain, Rung
from emet_sdk.types import CapabilityDescriptor

__all__ = [
    "SkippedRung",
    "unused_reasons",
    "Binding",
    "BindingTable",
    "resolve",
    "descriptors_from_manifest",
]


#: Axes a joint may declare, mirrored from the manifest schema.
_AXES = frozenset({"yaw", "pitch", "roll", "linear"})

#: Capability types a chain can bind to. Cameras and sensors are inputs — the
#: engine consumes what they report; no intent drives them — so they are never
#: candidates for a rung and never "unused".
_BINDABLE_TYPES = frozenset({"joint_group", "drive", "display", "light"})


@dataclass(frozen=True, slots=True)
class SkippedRung:
    """A rung that was passed over, and the reason."""

    index: int
    selector: str
    reason: str

    def __str__(self) -> str:
        return f"rung {self.index} {self.selector}: {self.reason}"


@dataclass(frozen=True, slots=True)
class Binding:
    """What one intent resolves to on this body."""

    intent: str
    rung_index: int
    action: str
    params: Mapping[str, Any] = field(default_factory=dict)
    capability_id: str | None = None      # None for a voice rung
    is_voice: bool = False
    skipped: tuple[SkippedRung, ...] = ()

    @property
    def target(self) -> str:
        return "voice" if self.is_voice else (self.capability_id or "?")

    @property
    def degraded(self) -> bool:
        """True if a more expressive rung existed but did not fit this body."""
        return bool(self.skipped)


@dataclass(frozen=True, slots=True)
class BindingTable:
    """The resolved answer for a whole body. Print it with `emet explain`."""

    bindings: Mapping[str, Binding]
    capabilities: tuple[CapabilityDescriptor, ...] = ()

    def __getitem__(self, intent: str) -> Binding:
        return self.bindings[intent]

    def __contains__(self, intent: str) -> bool:
        return intent in self.bindings

    def __len__(self) -> int:
        return len(self.bindings)

    def __iter__(self):
        return iter(sorted(self.bindings))

    @property
    def voice_bound(self) -> list[Binding]:
        """Intents that fell all the way through to the speaker."""
        return [b for _, b in sorted(self.bindings.items()) if b.is_voice]

    @property
    def hardware_bound(self) -> list[Binding]:
        return [b for _, b in sorted(self.bindings.items()) if not b.is_voice]

    @property
    def used_capabilities(self) -> list[str]:
        return sorted({b.capability_id for b in self.hardware_bound if b.capability_id})

    def unused_capabilities(self) -> list[str]:
        """Actuators that no intent binds to.

        Cameras and sensors are excluded: they are *inputs*, consumed by the
        engine rather than driven by chains, so they never appear in a binding
        table and their absence here means nothing.

        An actuator showing up is worth a look but is not automatically a
        mistake. It usually means the part is outranked everywhere — a light
        ring sits below the head and eyes in every chain that mentions it, so
        on a body with both it never binds. `mode: all` would light it; P0
        binds one rung per intent.
        """
        used = set(self.used_capabilities)
        return sorted(
            c.capability_id
            for c in self.capabilities
            if c.capability_id not in used and c.capability_type in _BINDABLE_TYPES
        )


def _why_not(rung: Rung, capabilities: Sequence[CapabilityDescriptor]) -> str:
    """Explain, in one line, why no capability satisfied this rung."""
    sel = rung.actuator
    if not capabilities:
        return "body has no capabilities"

    if sel.role is not None:
        by_role = [c for c in capabilities if c.role == sel.role]
        if not by_role:
            roles = sorted({c.role for c in capabilities if c.role}) or ["(none)"]
            return f"no capability has role {sel.role!r}; body has {', '.join(roles)}"
        unhealthy = [c for c in by_role if not c.healthy]
        if len(unhealthy) == len(by_role):
            return f"{sel.role!r} present but reported unhealthy"
        if sel.axis is not None:
            axes = sorted({a for c in by_role for a in c.axes}) or ["(none)"]
            return f"{sel.role!r} has no {sel.axis!r} axis; it has {', '.join(axes)}"
        if sel.type is not None:
            return f"{sel.role!r} is not of type {sel.type!r}"

    if sel.type is not None:
        by_type = [c for c in capabilities if c.capability_type == sel.type]
        if not by_type:
            return f"body has no {sel.type!r} capability"
        return f"{sel.type!r} present but reported unhealthy"

    if sel.axis is not None:
        return f"no capability provides the {sel.axis!r} axis"

    return "no match"


def resolve(
    chains: Mapping[str, Chain],
    capabilities: Sequence[CapabilityDescriptor] = (),
) -> BindingTable:
    """Bind every chain against a body.

    Never fails. Every chain is guaranteed to terminate in a voice rung — the
    validator refuses to load one that does not — so the worst case is that
    everything binds to the speaker, which is exactly what should happen on a
    body with no actuators.
    """
    bindings: dict[str, Binding] = {}

    for name, chain in chains.items():
        skipped: list[SkippedRung] = []

        for index, rung in enumerate(chain.rungs):
            if rung.is_voice:
                bindings[name] = Binding(
                    intent=name,
                    rung_index=index,
                    action=rung.action,
                    params=dict(rung.params),
                    capability_id=None,
                    is_voice=True,
                    skipped=tuple(skipped),
                )
                break

            match = next((c for c in capabilities if rung.actuator.matches(c)), None)
            if match is None:
                skipped.append(
                    SkippedRung(index, str(rung.actuator), _why_not(rung, capabilities))
                )
                continue

            bindings[name] = Binding(
                intent=name,
                rung_index=index,
                action=rung.action,
                params=dict(rung.params),
                capability_id=match.capability_id,
                is_voice=False,
                skipped=tuple(skipped),
            )
            break

    return BindingTable(bindings=bindings, capabilities=tuple(capabilities))


def unused_reasons(
    chains: Mapping[str, Chain],
    capabilities: Sequence[CapabilityDescriptor],
    table: "BindingTable",
) -> dict[str, str]:
    """Work out *why* each unbound actuator went unused.

    Two very different situations look identical in a binding table, and a
    builder needs to tell them apart:

    * **Nothing asks for it.** A light declared `role: decorative` when every
      chain wants `ambient` or `status` — a manifest mistake, and a part that
      will never do anything.
    * **Something better always wins.** A body whose head has all three axes
      never reaches the eyes rung of any express chain. Nothing is wrong; the
      part is simply outranked, and `mode: all` would light it up.

    Computed on demand rather than during resolution, because only the CLI
    wants it and it costs a second pass over every chain.
    """
    unused = set(table.unused_capabilities())
    reasons: dict[str, str] = {}

    for cap in capabilities:
        if cap.capability_id not in unused:
            continue

        eligible = 0
        outranked: dict[str, int] = {}

        for name, chain in chains.items():
            bound = table.bindings.get(name)
            for index, rung in enumerate(chain.rungs):
                if rung.is_voice:
                    break
                if not rung.actuator.matches(cap):
                    continue
                eligible += 1
                if bound is not None and not bound.is_voice and bound.rung_index < index:
                    winner = bound.capability_id or "?"
                    outranked[winner] = outranked.get(winner, 0) + 1
                break

        if eligible == 0:
            reasons[cap.capability_id] = (
                f"no chain selects a {cap.capability_type!r} with role "
                f"{cap.role!r} — check the role against what the chains ask for"
            )
        elif outranked:
            winner, count = max(outranked.items(), key=lambda kv: kv[1])
            reasons[cap.capability_id] = (
                f"could serve {eligible} intent(s), but {winner!r} sits higher "
                f"in {count} of them. Not a fault: `mode: all` would use both"
            )
        else:
            reasons[cap.capability_id] = f"eligible for {eligible} intent(s), bound by none"

    return reasons


def descriptors_from_manifest(
    manifest: Mapping[str, Any],
) -> list[CapabilityDescriptor]:
    """Project a manifest into descriptors *without instantiating anything*.

    This is what a manifest **claims**, not what hardware **reports**. The
    engine builds descriptors the real way — start each plugin, ask it — and a
    joint group whose servo board did not answer will describe itself more
    narrowly than the YAML does.

    The static projection exists so that `emet explain` works on a laptop, with
    no robot attached and no drivers installed. Anything it shows is the
    optimistic case: the binding table you get if every declared part works.
    """
    out: list[CapabilityDescriptor] = []

    for cap in manifest.get("capabilities") or []:
        cap_type = cap.get("type")
        joints = tuple(
            str(j["id"]) for j in (cap.get("joints") or []) if isinstance(j, Mapping) and "id" in j
        )
        axes = frozenset(
            str(j["axis"])
            for j in (cap.get("joints") or [])
            if isinstance(j, Mapping) and j.get("axis") in _AXES
        )
        out.append(
            CapabilityDescriptor(
                capability_id=str(cap.get("id", "")),
                capability_type=str(cap_type or ""),
                role=cap.get("role"),
                form=cap.get("form"),
                actions=frozenset(),   # unknowable without asking the plugin
                axes=axes,
                joints=joints,
                healthy=True,          # optimistic: nothing has been started
            )
        )

    return out


def load_chain_files(paths: Iterable[Any]) -> dict[str, Chain]:
    """Parse and merge several chain files, later files overriding earlier.

    Ordering is how per-soul overrides work: the SDK's defaults load first, a
    soul bundle's chains load after and win.
    """
    from emet_sdk.chains import parse_chain_set
    from emet_sdk.validate import load_yaml

    merged: dict[str, Chain] = {}
    for path in paths:
        merged.update(parse_chain_set(load_yaml(path)))
    return merged
