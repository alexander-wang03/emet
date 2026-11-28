"""Seeded properties of chain resolution (RP8, stage 1).

Each test states one property, quotes the sentence of `DESIGN.md` or of a
docstring it comes from, and runs over the seeds `_generators.seeds()`
names: a generated body manifest, the SDK's shipped chains with a generated
chain set laid over them (later wins, as `--chains` does), and the
descriptors `descriptors_from_manifest` projects from the manifest.

Whether a rung's selector selects a part is judged by `fits()` below,
written from the documents, so that no property takes the code under test
as its oracle.

A failure prints its seed and the line that reruns it. `-k "seed_17]"`
picks seed 17 out of the default range (the closing bracket keeps out
seed_170 to seed_179); `EMET_PROPERTY_SEED=17` runs any seed alone, and
`EMET_PROPERTY_SEEDS=2000` widens the range. A seed listed in `KNOWN` is a
case where the code does not do what the sentence says: it is expected to
fail, strictly, until the finding it names is fixed, and the finding is in
`releases/v0.5.6/reports/B7.md`, outside the repository. A wider range
reaches seeds past those lists, where the same findings fail as plain
failures.
"""

from __future__ import annotations

import dataclasses
import random
from functools import lru_cache

import pytest

from _generators import chain_set, manifest, registry, rerun, rng_for, schema_valid_variant, seed_params
from emet_sdk.chains import ActuatorSelector, Chain, parse_chain_set
from emet_sdk.resolve import (
    BindingTable,
    descriptors_from_manifest,
    load_chains,
    resolve,
    unused_reasons,
)
from emet_sdk.types import CapabilityDescriptor
from emet_sdk.validate import validate_manifest

FILE = "tests/test_properties_resolver.py"

#: The types `resolve.py` names as inputs, which no chain drives.
INPUTS = frozenset({"camera", "sensor"})

#: The types `resolve.py` names as the ones a chain can bind to.
BINDABLE = frozenset({"joint_group", "drive", "display", "light"})

#: Seeds of the default range on which a property is refuted, by finding.
KNOWN: dict[str, dict[str, tuple[int, ...]]] = {
    "B7-F2": {
        "inputs": (4, 9, 18, 20, 34, 36, 43, 50, 58, 66, 87, 91, 108, 153, 159, 161, 180, 183),
        "binds": (4, 8, 9, 18, 20, 34, 36, 43, 48, 50, 58, 61, 66, 87, 91, 108, 112, 118, 124, 126, 127, 138, 153,
                  159, 161, 169, 171, 180, 181, 183),
        "order": (4, 77, 153),
    },
}

REASONS = {
    "B7-F2": "B7-F2: a part of a type no chain may bind (a camera, a sensor, a reserved type) whose role "
             "or type a rung selects is bound like an actuator",
}


def seeds_for(test: str) -> list:
    return seed_params(test, KNOWN, REASONS)


def fits(selector: ActuatorSelector, part: CapabilityDescriptor) -> bool:
    """Whether a rung's selector selects a part, written from the documents
    rather than taken from `ActuatorSelector.matches`. DESIGN.md section 6:
    "bind the first rung whose actuator selector matches something in the
    manifest", where a selector gives a role, a type or an axis
    (`{role: head, axis: pitch}`, `{role: ambient, type: light}`);
    `ActuatorSelector.matches`: a part that reported unhealthy is fallen
    through; resolve.py, `_BINDABLE_TYPES`: "Capability types a chain can
    bind to", the four of `BINDABLE`, and cameras and sensors "are never
    candidates for a rung"."""
    if selector.voice or not part.healthy or part.capability_type not in BINDABLE:
        return False
    return (
        (selector.role is None or part.role == selector.role)
        and (selector.type is None or part.capability_type == selector.type)
        and (selector.axis is None or selector.axis in part.axes)
    )


@lru_cache(maxsize=1)
def shipped() -> dict[str, Chain]:
    return load_chains()


@lru_cache(maxsize=None)
def generated(seed: int) -> tuple[dict, dict[str, Chain], tuple[CapabilityDescriptor, ...]]:
    """One generated body, its chains and its descriptors, made once a seed
    and never changed: a test that edits one edits a copy."""
    rng = random.Random(seed)
    doc = manifest(rng)
    chains = {**shipped(), **parse_chain_set(chain_set(rng))}
    return doc, chains, tuple(descriptors_from_manifest(doc))


def case(seed: int, test: str) -> tuple[random.Random, dict, dict[str, Chain], list[CapabilityDescriptor]]:
    doc, chains, caps = generated(seed)
    return rng_for(seed, test), doc, chains, list(caps)


def shape(table: BindingTable) -> dict[str, tuple]:
    """What a binding table says each intent does, without the reasons."""
    return {
        name: (b.rung_index, b.is_voice, b.capability_id, b.action, dict(b.params), tuple(s.index for s in b.skipped))
        for name, b in table.bindings.items()
    }


@pytest.mark.parametrize("seed", seeds_for("binds"))
def test_every_intent_binds_to_a_part_the_body_has_or_to_its_voice_rung(seed):
    """DESIGN.md section 2, principle 2: "Every intent is always satisfiable.
    Because the hardware floor is a microphone and a speaker, every
    expressive fallback chain terminates in a voice rung. No intent may fail
    for lack of hardware; it degrades."
    """
    _, _, chains, caps = case(seed, "binds")
    table = resolve(chains, caps)
    where = rerun(seed, FILE, "test_every_intent_binds")
    assert set(table.bindings) == set(chains), f"{where}: intents left unbound: {sorted(set(chains) - set(table.bindings))}"
    by_id = {c.capability_id: c for c in caps}
    for name, binding in table.bindings.items():
        rung = chains[name].rungs[binding.rung_index]
        assert (binding.action, dict(binding.params)) == (rung.action, dict(rung.params)), f"{where}: {name}"
        if binding.is_voice:
            assert rung.is_voice and binding.capability_id is None, f"{where}: {name} is voice on a hardware rung"
        else:
            part = by_id.get(binding.capability_id)
            assert part is not None, f"{where}: {name} bound to {binding.capability_id!r}, which the body lacks"
            assert fits(rung.actuator, part), (
                f"{where}: {name} bound to {part.capability_id!r} ({part.capability_type}, role {part.role!r}, "
                f"axes {sorted(part.axes)}), which its rung {rung.actuator} does not select"
            )


@pytest.mark.parametrize("seed", seeds_for("order"))
def test_resolution_is_the_same_whatever_order_the_parts_and_chains_come_in(seed):
    """resolve.py: "(chains, capability descriptors) -> binding table",
    "a pure function over contract data (no state, no I/O, no
    personality)"; DESIGN.md section 6: "walk each chain top to bottom, bind
    the first rung whose actuator selector matches something in the
    manifest."

    "Something in the manifest" is a question about the set of parts, so
    the rung an intent binds to, and what it does there, cannot depend on
    the order the parts are declared in. Which part performs it can, when
    two parts both fit the rung: DESIGN.md section 6 says the part declared
    first wins it (since 0.5.7, ledger L14), and the code takes the first
    declared. This holds the part fixed only where exactly one fits.
    """
    rng, _, chains, caps = case(seed, "order")
    where = rerun(seed, FILE, "test_resolution_is_the_same")
    first = resolve(chains, caps)
    assert shape(resolve(chains, caps)) == shape(first), f"{where}: two runs on the same inputs differ"

    shuffled = caps[:]
    rng.shuffle(shuffled)
    names = list(chains)
    rng.shuffle(names)
    again = resolve({name: chains[name] for name in names}, shuffled)
    for name, binding in first.bindings.items():
        other = again[name]
        same = (other.rung_index, other.is_voice, other.action, dict(other.params))
        assert same == (binding.rung_index, binding.is_voice, binding.action, dict(binding.params)), (
            f"{where}: {name} binds differently once the parts are reordered"
        )
        rung = chains[name].rungs[binding.rung_index]
        fitting = [c.capability_id for c in caps if fits(rung.actuator, c)]
        if len(fitting) == 1:
            assert other.capability_id == binding.capability_id, f"{where}: {name}"
        elif fitting:
            assert other.capability_id in fitting, f"{where}: {name} bound to a part that does not fit"


@pytest.mark.parametrize("seed", seeds_for("removed"))
def test_removing_a_part_never_binds_an_intent_to_it_or_to_a_higher_rung(seed):
    """resolve.py: "Walk each chain top to bottom, bind the first rung whose
    selector matches something the body actually has"; DESIGN.md section 2,
    principle 2: "No intent may fail for lack of hardware; it degrades."

    A body with a part fewer has fewer matches, so every intent binds to the
    same rung or a lower one, and none to the part that is gone.
    """
    rng, _, chains, caps = case(seed, "removed")
    if not caps:
        caps = descriptors_from_manifest({"capabilities": [{"id": "head", "type": "joint_group", "role": "head",
                                                            "joints": [{"id": "pan", "axis": "yaw"}]}]})
    where = rerun(seed, FILE, "test_removing_a_part")
    before = resolve(chains, caps)
    gone = rng.choice(caps).capability_id
    after = resolve(chains, [c for c in caps if c.capability_id != gone])
    for name, binding in after.bindings.items():
        assert binding.capability_id != gone, f"{where}: {name} still bound to the removed {gone!r}"
        assert binding.rung_index >= before[name].rung_index, (
            f"{where}: {name} rose from rung {before[name].rung_index} to {binding.rung_index} "
            f"when {gone!r} was removed"
        )


@pytest.mark.parametrize("seed", seeds_for("unhealthy"))
def test_a_part_that_reported_unhealthy_is_bound_past_as_if_it_were_absent(seed):
    """chains.py, `ActuatorSelector.matches`: "Matched against what the
    plugin *reported*, not against the YAML: a servo group that failed to
    initialise reports a narrower descriptor and the chain falls through
    rather than binding to something dead."
    """
    rng, _, chains, caps = case(seed, "unhealthy")
    where = rerun(seed, FILE, "test_a_part_that_reported_unhealthy")
    dead = {c.capability_id for c in caps if rng.random() < 0.4}
    reported = [dataclasses.replace(c, healthy=c.capability_id not in dead) for c in caps]
    table = resolve(chains, reported)
    absent = resolve(chains, [c for c in caps if c.capability_id not in dead])
    for name, binding in table.bindings.items():
        assert binding.capability_id not in dead, f"{where}: {name} bound to the dead part {binding.capability_id!r}"
        other = absent[name]
        assert (binding.rung_index, binding.capability_id) == (other.rung_index, other.capability_id), (
            f"{where}: {name} binds differently to a dead part than to no part"
        )
        # resolve.py: the table "shows curiosity bound to the light ring
        # because the head group reported unhealthy". A rung asking for a
        # role whose parts are all dead says so.
        for skip in binding.skipped:
            role = chains[name].rungs[skip.index].actuator.role
            holders = [c for c in reported if role is not None and c.role == role]
            if holders and not any(c.healthy for c in holders):
                assert skip.reason == f"{role!r} present but reported unhealthy", f"{where}: {name}: {skip}"


@pytest.mark.parametrize("seed", seeds_for("first"))
def test_each_binding_is_the_first_rung_that_fits_and_names_every_rung_it_skipped(seed):
    """resolve.py: "Walk each chain top to bottom, bind the first rung whose
    selector matches something the body actually has, and record the
    result", and "Every binding carries the rungs it skipped and why";
    `Binding.degraded`: "True if a more expressive rung existed but did not
    fit this body."
    """
    _, _, chains, caps = case(seed, "first")
    where = rerun(seed, FILE, "test_each_binding_is_the_first_rung")
    for name, binding in resolve(chains, caps).bindings.items():
        for rung in chains[name].rungs[: binding.rung_index]:
            assert not any(fits(rung.actuator, c) for c in caps), f"{where}: {name} skipped a rung that fits"
        assert [s.index for s in binding.skipped] == list(range(binding.rung_index)), f"{where}: {name}"
        assert all(s.reason for s in binding.skipped), f"{where}: {name} skipped a rung without a reason"
        assert binding.degraded == (binding.rung_index > 0), f"{where}: {name}"


@pytest.mark.parametrize("seed", seeds_for("bare"))
def test_a_body_with_no_parts_binds_every_intent_to_its_voice_rung(seed):
    """`resolve()`: "the worst case is that everything binds to the speaker,
    which is exactly what should happen on a body with no actuators."
    """
    _, _, chains, _ = case(seed, "bare")
    where = rerun(seed, FILE, "test_a_body_with_no_parts")
    table = resolve(chains, [])
    assert not table.hardware_bound, f"{where}: a bare body bound {table.used_capabilities}"
    for name, binding in table.bindings.items():
        assert binding.rung_index == len(chains[name].rungs) - 1, f"{where}: {name}"


@pytest.mark.parametrize("seed", seeds_for("inputs"))
def test_no_intent_binds_to_a_camera_or_a_sensor(seed):
    """resolve.py, `_BINDABLE_TYPES`: "Cameras and sensors are inputs (the
    engine consumes what they report; no intent drives them), so they are
    never candidates for a rung and never 'unused'."
    """
    _, _, chains, caps = case(seed, "inputs")
    where = rerun(seed, FILE, "test_no_intent_binds_to_a_camera")
    inputs = {c.capability_id: c.capability_type for c in caps if c.capability_type in INPUTS}
    for name, binding in resolve(chains, caps).bindings.items():
        assert binding.capability_id not in inputs, (
            f"{where}: {name} bound to {binding.capability_id!r}, a {inputs.get(binding.capability_id)}"
        )


@pytest.mark.parametrize("seed", seeds_for("unused"))
def test_the_unused_parts_are_the_actuators_no_intent_binds(seed):
    """`BindingTable.unused_capabilities`: "Actuators that no intent binds
    to. Cameras and sensors are excluded"; `unused_reasons`: "Work out *why*
    each unbound actuator went unused", telling "Nothing asks for it" from
    "Something better always wins."

    `emet explain` prints each reason, so the reason is held to the case:
    no rung before the voice rung selects the part; a chain bound a higher
    rung; or every chain that selects it bound that same rung to another
    part.
    """
    _, _, chains, caps = case(seed, "unused")
    where = rerun(seed, FILE, "test_the_unused_parts")
    table = resolve(chains, caps)
    bound = {b.capability_id for b in table.hardware_bound}
    expected = sorted(c.capability_id for c in caps if c.capability_type in BINDABLE and c.capability_id not in bound)
    assert table.unused_capabilities() == expected, f"{where}"
    reasons = unused_reasons(chains, caps, table)
    assert sorted(reasons) == expected, f"{where}: a reason for each, and no other"
    for cap in (c for c in caps if c.capability_id in reasons):
        # The chains that select this part, each with the first rung that does.
        eligible: dict[str, int] = {}
        for name, chain in chains.items():
            for index, rung in enumerate(chain.rungs):
                if rung.is_voice:
                    break
                if fits(rung.actuator, cap):
                    eligible[name] = index
                    break
        higher = [name for name, index in eligible.items() if not table[name].is_voice and table[name].rung_index < index]
        reason = reasons[cap.capability_id]
        said = f"{where}: {cap.capability_id!r} given {reason!r}"
        if not eligible:
            assert reason.startswith(f"no chain selects a {cap.capability_type!r} with role {cap.role!r}"), said
        elif higher:
            assert reason.startswith(f"could serve {len(eligible)} intent(s), but ") and " sits higher in " in reason, said
        else:
            assert reason == f"eligible for {len(eligible)} intent(s), bound by none", said


@pytest.mark.parametrize("seed", seeds_for("never-fails"))
def test_resolution_never_fails_on_a_manifest_the_validator_accepts(seed):
    """`resolve()`: "Never fails."; `descriptors_from_manifest`: "The static
    projection exists so that `emet explain` works on a laptop, with no
    robot attached and no drivers installed."

    The manifest is changed only where the schema leaves its shape open
    (the fields of a reserved part, the contents of a `params` block), and
    used only when `validate_manifest` reports no error for it. The
    validator raises on none of these variants since finding B7-F3 was
    fixed, and `test_validation_raises_nothing_but_its_own_error_on_any_input`
    holds it to that on wider inputs.
    """
    rng, doc, chains, _ = case(seed, "never-fails")
    where = rerun(seed, FILE, "test_resolution_never_fails")
    for _ in range(5):
        variant = schema_valid_variant(rng, doc)
        if validate_manifest(variant, registry=registry()).ok:
            doc = variant
            break
    try:
        caps = descriptors_from_manifest(doc)
        table = resolve(chains, caps)
        unused_reasons(chains, caps, table)
    except Exception as exc:  # noqa: BLE001 - the property is that nothing is raised
        raise AssertionError(f"{where}: {type(exc).__name__}: {exc}; capabilities {doc.get('capabilities')!r}") from exc


@pytest.mark.parametrize("seed", seeds_for("projected"))
def test_the_projection_keeps_each_declared_part_as_the_manifest_states_it(seed):
    """`descriptors_from_manifest`: "Project a manifest into descriptors
    *without instantiating anything*. This is what a manifest **claims**",
    and "the optimistic case: the binding table you get if every declared
    part works."

    Every property above takes its body from this projection, so it is held
    on its own: one descriptor a declared part, in order, with the id, type
    and role the manifest gives it, the ids and axes of its joints, and
    healthy.
    """
    doc, _, caps = generated(seed)
    where = rerun(seed, FILE, "test_the_projection_keeps")
    parts = doc.get("capabilities") or []
    assert [c.capability_id for c in caps] == [p["id"] for p in parts], where
    for part, cap in zip(parts, caps):
        joints = part.get("joints") or []
        said = f"{where}: {part['id']!r}"
        assert (cap.capability_type, cap.role) == (part["type"], part.get("role")), said
        assert cap.joints == tuple(j["id"] for j in joints), said
        assert cap.axes == frozenset(j["axis"] for j in joints), said
        assert cap.healthy, said
