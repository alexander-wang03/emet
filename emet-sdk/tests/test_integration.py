"""End-to-end: a manifest becomes running plugins becomes actions.

Every other test in this repository checks one layer. This one closes the
loop, because the seams between layers are where the design either works or
quietly does not:

    manifest  →  discovery  →  plugins  →  describe()  →  resolve  →  apply()

Nothing above this exists yet — there is no engine and no choreographer — so
this is the first and only place the whole path runs. Until 0.3 builds a real
loop, it is the test that says the architecture actually fits together.

The other thing checked here is **agreement**. `emet explain` resolves against
a static projection of the manifest, because it has to work on a laptop with
no robot attached. The engine resolves against what live plugins report. If
those two ever disagree, `emet explain` is lying to the person using it to
debug, which is worse than not having it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from emet_sdk.chains import parse_chain_set
from emet_sdk.discovery import PluginRegistry
from emet_sdk.plugin import ActuatorPlugin, LocomotionPlugin, SensorPlugin
from emet_sdk.resolve import descriptors_from_manifest, resolve
from emet_sdk.types import Action, Twist
from emet_sdk.validate import load_yaml, validate_manifest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CHAINS = Path(__file__).resolve().parent.parent / "emet_sdk" / "chains"


@pytest.fixture(scope="module")
def chains() -> dict:
    merged: dict = {}
    for path in sorted(CHAINS.glob("*.yaml")):
        merged.update(parse_chain_set(load_yaml(path)))
    return merged


@pytest.fixture(scope="module")
def manifest() -> dict:
    return load_yaml(EXAMPLES / "mock-scout.yaml")


async def _bring_up(manifest: dict) -> tuple[dict, dict]:
    """Do what the engine will do at boot: instantiate and start everything.

    Returns (drivers by capability id, locomotion by capability id).
    """
    registry = PluginRegistry.discover()
    drivers: dict[str, ActuatorPlugin | SensorPlugin] = {}
    locomotion: dict[str, LocomotionPlugin] = {}

    for cap in manifest["capabilities"]:
        plugin_cls = registry.load_driver(cap["driver"]["plugin"])
        plugin = plugin_cls(cap)
        await plugin.start()
        drivers[cap["id"]] = plugin

        # A drive has two plugins: one that talks to the motors, and one that
        # knows what a velocity means for this body. Keeping them separate is
        # what lets `legged` arrive without touching the motor driver.
        if cap.get("type") == "drive":
            loco_cls = registry.load_locomotion(cap["kinematics"])
            loco = loco_cls(cap)
            await loco.start()
            locomotion[cap["id"]] = loco

    return drivers, locomotion


def test_manifest_becomes_plugins_becomes_actions(manifest, chains):
    """The whole path, once, for real."""

    async def scenario():
        assert validate_manifest(manifest).ok

        drivers, locomotion = await _bring_up(manifest)
        assert set(drivers) == {"head", "base", "eyes", "ring", "imu"}
        assert set(locomotion) == {"base"}

        # Resolve against what the plugins REPORT, not against the YAML.
        live = [p.describe() for p in drivers.values()]
        table = resolve(chains, live)

        # Perform an expressive intent.
        binding = table["express.curiosity"]
        assert binding.capability_id == "head"
        head = drivers[binding.capability_id]
        await head.apply(
            Action(
                capability_id=binding.capability_id,
                name=binding.action,
                params=dict(binding.params),
            )
        )
        assert [a.name for a in head.applied] == ["tilt"]
        assert head.applied[0].params["angle_deg"] == 12

        # And a locomotion intent, which goes through the other plugin type.
        move = table["move.approach"]
        assert move.capability_id == "base"
        await locomotion["base"].command(Twist(linear_mps=0.2))
        assert locomotion["base"].last.left > 0
        assert locomotion["base"].last.right > 0

        # Shutdown must be safe to call on everything, in any state.
        for plugin in (*drivers.values(), *locomotion.values()):
            await plugin.shutdown()
        assert locomotion["base"].last.left == 0.0

    asyncio.run(scenario())


def test_live_descriptors_agree_with_the_static_projection(manifest, chains):
    """`emet explain` must not lie.

    It resolves against a projection of the manifest so that it works with no
    hardware attached. The engine resolves against live plugins. For a plugin
    that reports honestly, those must produce the same binding table — or the
    tool people reach for when debugging shows them something the robot will
    not do.
    """

    async def scenario():
        drivers, _ = await _bring_up(manifest)
        live = sorted(
            (p.describe() for p in drivers.values()), key=lambda d: d.capability_id
        )
        static = sorted(descriptors_from_manifest(manifest), key=lambda d: d.capability_id)

        assert [d.capability_id for d in live] == [d.capability_id for d in static]
        for a, b in zip(live, static):
            assert a.capability_type == b.capability_type, a.capability_id
            assert a.role == b.role, a.capability_id
            assert a.axes == b.axes, a.capability_id
            assert a.joints == b.joints, a.capability_id
            assert a.healthy == b.healthy, a.capability_id

        # And therefore the same answer.
        assert resolve(chains, live).bindings == resolve(chains, static).bindings

    asyncio.run(scenario())


def test_a_broken_part_changes_the_answer_at_boot(manifest, chains):
    """The one thing the static projection CANNOT know.

    `emet explain` is optimistic by construction — it assumes every declared
    part works. A servo board that does not answer is exactly the case where
    the engine's table diverges, and it must diverge in the safe direction:
    past the dead part, not into it.
    """

    async def scenario():
        broken = {
            **manifest,
            "capabilities": [
                {**cap, "driver": {**cap["driver"], "params": {"fail_on_start": True}}}
                if cap["id"] == "head"
                else cap
                for cap in manifest["capabilities"]
            ],
        }
        drivers, _ = await _bring_up(broken)
        live = [p.describe() for p in drivers.values()]

        head = next(d for d in live if d.capability_id == "head")
        assert not head.healthy

        table = resolve(chains, live)
        curiosity = table["express.curiosity"]
        assert curiosity.capability_id == "eyes"        # fell past the dead head
        assert curiosity.degraded
        assert "unhealthy" in curiosity.skipped[0].reason

        # Optimistic projection still claims the head. That difference is the
        # entire reason the CLI prints a caveat above its table.
        assert resolve(chains, descriptors_from_manifest(broken))[
            "express.curiosity"
        ].capability_id == "head"

    asyncio.run(scenario())


def test_every_example_body_brings_up_and_resolves(chains):
    """Nothing in the shipped examples fails on the way up."""

    async def scenario():
        for fixture in ("bodiless.yaml", "mock-scout.yaml"):
            doc = load_yaml(EXAMPLES / fixture)
            drivers, _ = await _bring_up(doc)
            table = resolve(chains, [p.describe() for p in drivers.values()])
            assert len(table) == len(chains), fixture
            for plugin in drivers.values():
                await plugin.shutdown()

    asyncio.run(scenario())


# ------------------------------------------------------- bad input handling


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("key: [unclosed\n", "read_error"),         # malformed YAML
        ("", "unknown_document"),                    # empty file
        ("just a string\n", "unknown_document"),     # a scalar, not a mapping
        ("bundle_version: '0.1'\n", "unknown_document"),  # right family, wrong document
    ],
)
def test_explain_reports_bad_input_instead_of_raising(tmp_path, content, expected_code, capsys):
    """`emet explain` must fail like `emet validate` does.

    Someone points this at the wrong file eventually. A stack trace tells them
    nothing about which file or why, and the two subcommands behaving
    differently on the same mistake is its own small betrayal.
    """
    from emet_sdk.cli import main

    path = tmp_path / "thing.yaml"
    path.write_text(content, encoding="utf-8")

    assert main(["explain", str(path)]) == 1
    out = capsys.readouterr().out
    assert expected_code in out
    assert "Traceback" not in out
