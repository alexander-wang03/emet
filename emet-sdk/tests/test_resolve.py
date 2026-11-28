"""The 0.2 acceptance criteria, as executable tests.

0.2 is done when `emet explain` prints a correct binding table for a bodiless
manifest and for a fully-loaded one, and `kinematics: legged` still fails with
a missing-plugin error, now from real entry-point discovery rather than a
hardcoded set.

The tests that carry the most weight here are the ones about *falling
through*: an intent binding past a part that exists but is broken is the
behaviour the whole fallback design is for, and it is invisible until
something is deliberately broken to check.

The last two sections came later: every field a binding records, and how a
reserved part whose fields the schema leaves open is projected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _generators import registry
from emet_sdk.chains import parse_chain_set
from emet_sdk.discovery import PluginRegistry
from emet_sdk.resolve import Binding, SkippedRung, descriptors_from_manifest, load_chains, resolve, unused_reasons
from emet_sdk.types import CapabilityDescriptor
from emet_sdk.validate import load_yaml, validate_manifest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CHAINS = Path(__file__).resolve().parent.parent / "emet_sdk" / "chains"


@pytest.fixture(scope="module")
def chains() -> dict:
    merged: dict = {}
    for path in sorted(CHAINS.glob("*.yaml")):
        merged.update(parse_chain_set(load_yaml(path)))
    return merged


def table_for(fixture: str, chains: dict):
    manifest = load_yaml(EXAMPLES / fixture)
    return resolve(chains, descriptors_from_manifest(manifest))


# ------------------------------------------------- the acceptance criteria


def test_bodiless_body_binds_everything_to_voice(chains):
    """The proof that the abstraction is real.

    A Pi with a speakerphone and nothing else still performs every intent in
    the vocabulary. Nothing is unsatisfiable; it is all just audible.
    """
    table = table_for("bodiless.yaml", chains)
    assert len(table) == len(chains)
    assert len(table.hardware_bound) == 0
    assert len(table.voice_bound) == len(chains)


def test_loaded_body_binds_curiosity_to_the_head(chains):
    table = table_for("scout-01.yaml", chains)
    curiosity = table["express.curiosity"]
    assert curiosity.capability_id == "head"
    assert curiosity.action == "tilt"
    assert not curiosity.is_voice
    assert not curiosity.degraded          # the head is its first choice


def test_legged_is_a_missing_plugin_from_real_discovery():
    """`kinematics` stayed open when discovery became real.

    The registry now reads installed entry points. `legged` must still be a
    missing *plugin* rather than a schema violation, or bipeds have quietly
    become inexpressible.
    """
    manifest = load_yaml(EXAMPLES / "invalid" / "legged-no-plugin.yaml")
    report = validate_manifest(manifest, registry=PluginRegistry.discover())
    assert not report.ok
    assert {f.code for f in report.errors} == {"missing_plugin"}


def test_shipped_locomotion_is_discovered():
    registry = PluginRegistry.discover()
    assert registry.has_locomotion("differential")
    assert registry.has_locomotion("tracked")
    assert not registry.has_locomotion("legged")
    assert registry.has_driver("emet_hal.mock")


# --------------------------------------------------------- falling through


def test_missing_axis_falls_through_to_the_next_rung(chains):
    """scout-01's head has yaw and pitch but no roll.

    express.affection wants a roll tilt first, so it must bind to the eyes,
    and the table must say why, because "my robot won't tilt affectionately"
    is otherwise an unanswerable bug report.
    """
    table = table_for("scout-01.yaml", chains)
    affection = table["express.affection"]
    assert affection.capability_id == "eyes"
    assert affection.degraded
    assert "roll" in affection.skipped[0].reason


def test_a_body_with_the_axis_binds_the_better_rung(chains):
    """The same chain, a body with a roll joint, a different answer."""
    table = table_for("mock-scout.yaml", chains)
    affection = table["express.affection"]
    assert affection.capability_id == "head"
    assert not affection.degraded


def test_role_not_part_is_what_a_chain_asks_for(chains):
    """scout-01's ring is `ambient`; mock-scout's is `status`.

    Identical hardware, one word different, and the signal chains land
    somewhere else entirely. This is the portability mechanism in one assert.
    """
    scout = table_for("scout-01.yaml", chains)
    mock = table_for("mock-scout.yaml", chains)
    assert scout["signal.booting"].capability_id == "eyes"
    assert mock["signal.booting"].capability_id == "ring"


def test_of_two_parts_that_fit_a_rung_the_first_declared_wins_it():
    """DESIGN.md section 6 (ledger L14): the order of the parts decides which
    part performs an intent, never which rung binds."""
    chains = load_chains()
    a = CapabilityDescriptor("lamp_a", "light", role="status")
    b = CapabilityDescriptor("lamp_b", "light", role="status")
    first, second = resolve(chains, (a, b)), resolve(chains, (b, a))
    one, two = first["signal.offline"], second["signal.offline"]
    assert (one.rung_index, one.action) == (two.rung_index, two.action) == (0, "pulse")
    assert (one.capability_id, two.capability_id) == ("lamp_a", "lamp_b")


def test_unhealthy_capability_is_bound_past(chains):
    """A part that exists but reported broken must not capture a rung.

    Binding to dead hardware means the robot silently does nothing, which is
    strictly worse than pulsing a light instead.
    """
    healthy = CapabilityDescriptor(
        capability_id="head", capability_type="joint_group",
        role="head", axes=frozenset({"yaw", "pitch"}), healthy=True,
    )
    eyes = CapabilityDescriptor(
        capability_id="eyes", capability_type="display", role="eyes", healthy=True,
    )

    assert resolve(chains, [healthy, eyes])["express.curiosity"].capability_id == "head"

    broken = CapabilityDescriptor(
        capability_id="head", capability_type="joint_group",
        role="head", axes=frozenset({"yaw", "pitch"}), healthy=False,
    )
    degraded = resolve(chains, [broken, eyes])["express.curiosity"]
    assert degraded.capability_id == "eyes"
    assert "unhealthy" in degraded.skipped[0].reason


def test_every_intent_resolves_on_every_example_body(chains):
    """Principle 2, checked against real manifests rather than argued for."""
    for fixture in ("bodiless.yaml", "scout-01.yaml", "mock-scout.yaml"):
        table = table_for(fixture, chains)
        assert len(table) == len(chains), fixture


# ------------------------------------------------------------- descriptors


def test_descriptors_read_axes_and_joints_from_the_manifest():
    caps = descriptors_from_manifest(load_yaml(EXAMPLES / "scout-01.yaml"))
    head = next(c for c in caps if c.capability_id == "head")
    assert head.role == "head"
    assert head.axes == frozenset({"yaw", "pitch"})
    assert head.joints == ("pan", "tilt")


def test_inputs_are_never_reported_as_unused(chains):
    """Cameras and sensors are consumed, not driven.

    They can never appear in a binding table, so listing them as unbound
    actuators would be a permanent false positive on every manifest that has
    a camera.
    """
    table = table_for("scout-01.yaml", chains)
    unused = table.unused_capabilities()
    assert "front_cam" not in unused
    assert "imu" not in unused
    assert "ring" in unused      # a real actuator, outranked everywhere


# ---------------------------------------------- diagnosing unused actuators


def test_outranked_actuator_is_distinguished_from_an_unasked_one(chains):
    """Two very different situations look identical in a binding table.

    A part nobody asks for is a manifest mistake. A part that something better
    always beats is fine, and `mode: all` would use it. Telling a builder the
    wrong one wastes an evening.
    """
    manifest = load_yaml(EXAMPLES / "mock-scout.yaml")
    caps = descriptors_from_manifest(manifest)
    table = resolve(chains, caps)

    # mock-scout's head has all three axes, so the eyes never win a rung.
    outranked = unused_reasons(chains, caps, table)["eyes"]
    assert "sits higher" in outranked
    assert "mode: all" in outranked

    # Give the ring a role no chain mentions and the diagnosis must change.
    for cap in manifest["capabilities"]:
        if cap["id"] == "ring":
            cap["role"] = "decorative"
    caps = descriptors_from_manifest(manifest)
    unasked = unused_reasons(chains, caps, resolve(chains, caps))["ring"]
    assert "no chain selects" in unasked
    assert "decorative" in unasked


# ------------------------------------------------- what a binding records
#
# `emet explain` prints every field of a `Binding` and the engine performs
# it, so each field `resolve` fills is held whole here: the intent, the
# rung's index, action and parameters, whether it is the voice, and each
# rung passed over with its index, its selector as a chain file writes it,
# and why. mutmut's survivors of `resolve` (`tools/ci/mutmut-targets.txt`)
# were changes to these fields that no test caught.

#: A roll tilt no test body below can give, then a voice rung with words.
TILT_THEN_VOICE = {
    "express.curiosity": {"rungs": [
        {"actuator": {"role": "head", "axis": "roll"}, "action": "tilt", "params": {"deg": 12}},
        {"actuator": {"voice": True}, "action": "inflect", "params": {"preset": "rising", "filler": "hm?"}},
    ]},
}

#: The same tilt, then the eyes, then the voice.
TILT_THEN_EYES = {
    "express.affection": {"rungs": [
        {"actuator": {"role": "head", "axis": "roll"}, "action": "tilt", "params": {"deg": 12}},
        {"actuator": {"role": "eyes"}, "action": "expression", "params": {"preset": "soft"}},
        {"actuator": {"voice": True}, "action": "utter", "params": {"text": "aw."}},
    ]},
}

EYES = CapabilityDescriptor(capability_id="eyes", capability_type="display", role="eyes")


def test_a_voice_binding_records_its_intent_its_rung_its_words_and_the_rung_it_skipped():
    """resolve.py: "Every binding carries the rungs it skipped and why." On a
    body with nothing, the tilt is skipped and the voice rung binds, with
    its own parameters, as rung 1 of `express.curiosity`."""
    binding = resolve(parse_chain_set(TILT_THEN_VOICE), [])["express.curiosity"]
    skip = SkippedRung(0, "{role: head, axis: roll}", "body has no capabilities")
    assert binding == Binding(
        intent="express.curiosity",
        rung_index=1,
        action="inflect",
        params={"preset": "rising", "filler": "hm?"},
        capability_id=None,
        is_voice=True,
        skipped=(skip,),
    )
    assert binding.intent == "express.curiosity"
    assert binding.rung_index == 1
    assert binding.params == {"preset": "rising", "filler": "hm?"}
    assert type(binding.skipped) is tuple and binding.skipped == (skip,)
    assert binding.skipped[0].index == 0
    assert binding.skipped[0].selector == "{role: head, axis: roll}"
    assert str(binding.skipped[0]) == "rung 0 {role: head, axis: roll}: body has no capabilities"
    assert binding.is_voice is True and binding.target == "voice" and binding.degraded


def test_a_voice_rung_that_binds_first_records_no_skipped_rung():
    """`Binding.degraded`: "True if a more expressive rung existed but did
    not fit this body." A chain of one voice rung skips nothing."""
    chains = parse_chain_set({"acknowledge": {"rungs": [
        {"actuator": {"voice": True}, "action": "backchannel", "params": {"text": "mm."}},
    ]}})
    binding = resolve(chains, [EYES])["acknowledge"]
    assert binding == Binding(intent="acknowledge", rung_index=0, action="backchannel",
                              params={"text": "mm."}, is_voice=True)
    assert type(binding.skipped) is tuple and binding.skipped == ()
    assert not binding.degraded


def test_a_hardware_binding_records_its_intent_its_part_and_that_it_is_not_the_voice():
    """DESIGN.md section 6: "bind the first rung whose actuator selector
    matches something in the manifest". The tilt finds no head, so the eyes
    rung binds to the eyes, and the binding says it is no voice binding:
    `is_voice` is False itself, which `voice_bound` and `hardware_bound`
    read."""
    table = resolve(parse_chain_set(TILT_THEN_EYES), [EYES])
    binding = table["express.affection"]
    assert binding == Binding(
        intent="express.affection",
        rung_index=1,
        action="expression",
        params={"preset": "soft"},
        capability_id="eyes",
        is_voice=False,
        skipped=(SkippedRung(0, "{role: head, axis: roll}", "no capability has role 'head'; body has eyes"),),
    )
    assert binding.intent == "express.affection"
    assert binding.is_voice is False
    assert table.hardware_bound == [binding] and table.voice_bound == []
    assert binding.target == "eyes"


# ------------------------------------- reserved parts the schema leaves open


#: A body the validator accepts: the schema leaves a reserved type's fields
#: open, so each reserved part below gives a field a shape no P0 part may.
RESERVED_ODDLY = {
    "manifest_version": "0.1",
    "body": {"id": "odd", "scale": "desk", "power": "plugged_in"},
    "audio": {"input": {"device": "default"}, "output": {"device": "default"}},
    "capabilities": [
        {"id": "grip", "type": "manipulator", "role": 2.5, "form": {"k": [None]}, "joints": True},
        {"id": "lamp", "type": "projector", "role": ["wall"], "form": 3,
         "joints": [{"id": "pan", "axis": {"k": 1}}, {"id": "tilt", "axis": "pitch"}, "loose", {"axis": "yaw"}]},
        {"id": "heat", "type": "thermal", "role": {"k": 1}, "joints": "wrist"},
        {"id": "eyes", "type": "display", "role": "eyes", "form": "dual_round", "driver": {"plugin": "emet_hal.mock"}},
    ],
}


def test_a_reserved_part_is_projected_with_only_the_fields_of_the_shape_a_descriptor_holds():
    """`descriptors_from_manifest`: "This is what a manifest **claims**".
    A `role` or `form` that is no string claims no role or form, `joints`
    that are no list claim no joints, and a joint whose `axis` is no string
    claims no axis; a P0 part is projected as it was."""
    assert validate_manifest(RESERVED_ODDLY, registry=registry()).findings == []
    assert descriptors_from_manifest(RESERVED_ODDLY) == [
        CapabilityDescriptor(capability_id="grip", capability_type="manipulator"),
        CapabilityDescriptor(capability_id="lamp", capability_type="projector",
                             axes=frozenset({"pitch", "yaw"}), joints=("pan", "tilt")),
        CapabilityDescriptor(capability_id="heat", capability_type="thermal"),
        CapabilityDescriptor(capability_id="eyes", capability_type="display", role="eyes", form="dual_round"),
    ]


def test_a_part_with_no_id_or_type_is_projected_with_empty_strings():
    """The projection takes what it is given and never fails on it. A part
    the schema would refuse for having no `id` and no `type` is still one
    descriptor, its id and type the empty string, as the engine's
    `body.py` reads a missing id."""
    assert descriptors_from_manifest({"capabilities": [{"role": "eyes"}]}) == [
        CapabilityDescriptor(capability_id="", capability_type="", role="eyes"),
    ]


def test_resolving_a_body_with_oddly_shaped_reserved_parts_raises_nothing_and_names_only_roles():
    """`resolve()`: "Never fails." A rung asking for a role the body lacks
    names the roles it has, and a reserved part's role of another shape is
    none of them; `unused_reasons` has a reason for the one actuator."""
    chains = parse_chain_set(TILT_THEN_VOICE)
    caps = descriptors_from_manifest(RESERVED_ODDLY)
    table = resolve(chains, caps)
    assert table["express.curiosity"].skipped == (
        SkippedRung(0, "{role: head, axis: roll}", "no capability has role 'head'; body has eyes"),
    )
    assert unused_reasons(chains, caps, table) == {
        "eyes": "no chain selects a 'display' with role 'eyes'; check the role against what the chains ask for",
    }
