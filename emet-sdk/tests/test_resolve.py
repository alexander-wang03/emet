"""The 0.2 acceptance criteria, as executable tests.

0.2 is done when `emet explain` prints a correct binding table for a bodiless
manifest and for a fully-loaded one, and `kinematics: legged` still fails with
a missing-plugin error — now from real entry-point discovery rather than a
hardcoded set.

The tests that carry the most weight here are the ones about *falling
through*: an intent binding past a part that exists but is broken is the
behaviour the whole fallback design is for, and it is invisible until
something is deliberately broken to check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emet_sdk.chains import parse_chain_set
from emet_sdk.discovery import PluginRegistry
from emet_sdk.resolve import descriptors_from_manifest, resolve
from emet_sdk.types import CapabilityDescriptor
from emet_sdk.validate import MissingPluginError, load_yaml, validate_manifest

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

    express.affection wants a roll tilt first, so it must bind to the eyes —
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
    from emet_sdk.resolve import unused_reasons

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
