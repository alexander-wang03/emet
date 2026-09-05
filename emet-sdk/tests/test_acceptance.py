"""The 0.1 acceptance criteria, as executable tests.

0.1 is done when `emet validate` accepts the example
manifests, rejects a chain whose final rung is not a voice rung, and rejects
`kinematics: legged` with a *missing plugin* error rather than a schema error.

The last two tests in this file are the ones that matter most: they are the
evidence for claims the whole design rests on — that principle 2 is enforced
mechanically, and that reserving schema now really is free later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from emet_sdk import chains, intents
from emet_sdk.discovery import PluginRegistry
from emet_sdk.validate import (
    load_yaml,
    validate_chain_document,
    validate_manifest,
    validate_motion_pack,
    validate_pairing,
    validate_soul,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CHAINS = Path(__file__).resolve().parent.parent / "emet_sdk" / "chains"


def codes(report) -> set[str]:
    return {f.code for f in report.errors}


def warnings(report) -> set[str]:
    return {f.code for f in report.warnings}


# ---------------------------------------------------------------- valid


def test_bodiless_manifest_is_valid():
    """The milestone-one manifest. Must stay valid forever."""
    report = validate_manifest(load_yaml(EXAMPLES / "bodiless.yaml"))
    assert report.ok, report.errors


def test_scout_manifest_is_valid():
    report = validate_manifest(load_yaml(EXAMPLES / "scout-01.yaml"))
    assert report.ok, report.errors


def test_reference_soul_with_every_rsv_field_is_valid():
    """The reserved-now-free-later claim, demonstrated rather than asserted."""
    report = validate_soul(load_yaml(EXAMPLES / "emet-soul.yaml"))
    assert report.ok, report.errors


def test_shipped_chain_files_are_valid():
    for path in sorted(CHAINS.glob("*.yaml")):
        report = validate_chain_document(load_yaml(path))
        assert report.ok, f"{path.name}: {report.errors}"


# -------------------------------------------------------------- invalid


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("no-audio.yaml", "schema"),
        ("duplicate-id.yaml", "duplicate_id"),
        ("home-out-of-range.yaml", "home_out_of_range"),
        ("two-drives.yaml", "multiple_drives"),
        ("unknown-mount.yaml", "unknown_reference"),
    ],
)
def test_invalid_manifests_are_rejected(fixture: str, expected: str):
    report = validate_manifest(load_yaml(EXAMPLES / "invalid" / fixture))
    assert not report.ok
    assert expected in codes(report), report.errors


def test_a_soul_may_answer_to_any_phrase():
    """This used to be rejected, and the rule was wrong rather than strict.

    P0 assumed wake detection meant a pretrained model, so only a fixed set of
    names could be heard and `wake_word: barnaby` was an error. The shipped
    default is a phonetic keyword spotter, which takes any phrase and a
    pronunciation. The field is free text, and whether a particular engine can
    hear a particular phrase is answered at boot against a live descriptor —
    not by a list in the validator, which would make a soul valid or invalid
    depending on which machine linted it.
    """
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    doc["identity"]["name"] = "Barnaby"
    doc["identity"]["wake_word"] = "hey barnaby"
    report = validate_soul(doc)
    assert report.ok, report.errors


def test_an_uninstalled_wake_engine_is_an_error():
    """Not a warning, unlike a missing driver. `audio.wake.engine` names an
    implementation that has to exist rather than hardware you might not have
    wired yet, and wake is the one thing with no rung beneath it."""
    report = validate_manifest(load_yaml(EXAMPLES / "invalid" / "unknown-wake-engine.yaml"))
    assert not report.ok
    assert "missing_plugin" in codes(report)


def test_an_uninstalled_driver_is_still_only_a_warning():
    """The other half of that distinction, pinned so the two do not drift
    together. Describing a body you have not finished building is normal."""
    doc = load_yaml(EXAMPLES / "scout-01.yaml")
    doc["capabilities"][0]["driver"]["plugin"] = "nobody.ships.this"
    report = validate_manifest(doc)
    assert report.ok, report.errors
    assert "driver_not_installed" in warnings(report)


def test_every_invalid_fixture_is_actually_rejected():
    """The contract of `examples/invalid/`: plain `emet validate` rejects all
    of it.

    That rule lived only in the CI workflow, so a fixture that merely warned
    could be added, pass every test here, and turn the pipeline red afterwards.
    Which is exactly what happened. Sweeping the directory keeps the rule and
    the fixtures in the same place.
    """
    from emet_sdk.cli import _validate_path

    registry = PluginRegistry.discover()
    fixtures = sorted((EXAMPLES / "invalid").glob("*.yaml"))
    assert fixtures, "no invalid fixtures found; the glob or the path is wrong"
    for path in fixtures:
        kind, report = _validate_path(path, registry)
        assert not report.ok, f"{path.name} validated clean as {kind!r}"


# ------------------------------------------- the two that carry the design


def test_chain_without_voice_rung_is_rejected():
    """Principle 2, enforced mechanically.

    Every intent is always satisfiable because every chain terminates in a
    rung that cannot fail to bind. Nothing in the engine checks this at
    runtime — it cannot get past the validator.
    """
    report = validate_chain_document(
        load_yaml(EXAMPLES / "invalid" / "unterminated-chain.yaml")
    )
    assert not report.ok
    assert "unterminated_chain" in codes(report)


def test_legged_is_a_missing_plugin_not_a_schema_error():
    """`kinematics` is an open enum, and the RSV `legs` block validates clean.

    If this ever produces a schema error, the enum has closed and bipeds have
    become unexpressible without a migration across every robot in the field.
    """
    report = validate_manifest(load_yaml(EXAMPLES / "invalid" / "legged-no-plugin.yaml"))
    assert not report.ok
    assert codes(report) == {"missing_plugin"}, report.errors
    assert "schema" not in codes(report)


def test_legs_block_is_accepted_wherever_the_plugin_exists():
    """The RSV legs structure is legal on its own terms."""
    doc = load_yaml(EXAMPLES / "invalid" / "legged-no-plugin.yaml")
    doc["capabilities"][0]["kinematics"] = "tracked"
    report = validate_manifest(doc)
    assert report.ok, report.errors


# ----------------------------------------------------------- cross-document


def test_barge_in_requires_echo_cancellation():
    manifest = load_yaml(EXAMPLES / "bodiless.yaml")
    manifest["audio"]["input"]["aec"] = "none"
    soul = load_yaml(EXAMPLES / "emet-soul.yaml")
    report = validate_pairing(manifest, soul)
    assert not report.ok
    assert "barge_in_without_aec" in codes(report)


def test_valid_pairing_is_clean():
    report = validate_pairing(
        load_yaml(EXAMPLES / "scout-01.yaml"),
        load_yaml(EXAMPLES / "emet-soul.yaml"),
    )
    assert report.ok, report.errors


# ------------------------------------------------------------------ intents


def test_intent_vocabulary_is_closed():
    with pytest.raises(intents.UnknownIntentError):
        intents.make("emote", "wistful")
    with pytest.raises(intents.UnknownIntentError):
        intents.make("express", "wistful")


def test_reserved_intents_are_legal_to_emit():
    """A soul written today may reach for an intent that lands in 2033.

    Because the vocabulary is closed, an unreserved name would make the whole
    bundle *invalid* rather than merely ineffective — and bundles are the
    artifact strangers publish and keep for years.
    """
    for kind in ("manipulate", "navigate", "gesture", "attend_joint"):
        assert intents.is_reserved(kind)
    intent = intents.make("manipulate", "grasp")
    assert intent.name == "manipulate.grasp"


def test_every_p0_intent_has_a_chain():
    """No P0 intent may be left without a degradation ladder."""
    shipped: dict = {}
    for path in sorted(CHAINS.glob("*.yaml")):
        shipped.update(chains.parse_chain_set(load_yaml(path)))

    missing = []
    for kind, arguments in intents.P0_INTENTS.items():
        if arguments is intents.FREE_ARGUMENT or not arguments:
            if kind not in shipped:
                missing.append(kind)
        else:
            missing.extend(
                f"{kind}.{arg}" for arg in arguments if f"{kind}.{arg}" not in shipped
            )
    assert not missing, f"P0 intents with no fallback chain: {sorted(missing)}"


def test_every_shipped_chain_terminates_in_voice():
    for path in sorted(CHAINS.glob("*.yaml")):
        for name, chain in chains.parse_chain_set(load_yaml(path)).items():
            assert chain.voice_rung.is_voice, f"{path.name}: {name}"


# ------------------------------------------------------------- motion packs


def test_motion_pack_round_trips():
    pack = {
        "pack_version": "0.1",
        "name": "scout default",
        "requires": [{"role": "head", "joints": ["pan", "tilt"]}],
        "clips": {
            "curious_tilt": {
                "intent": "express.curiosity",
                "easing": "ease_in_out",
                "keyframes": [
                    {"t_ms": 0, "joints": {"pan": 0, "tilt": 0}},
                    {"t_ms": 300, "joints": {"pan": -8, "tilt": 12}},
                    {"t_ms": 1000, "joints": {"pan": -8, "tilt": 12}},
                    {"t_ms": 1400, "joints": {"pan": 0, "tilt": 0}},
                ],
            }
        },
    }
    assert validate_motion_pack(pack).ok


def test_motion_pack_rejects_unknown_intent():
    pack = {
        "pack_version": "0.1",
        "name": "bad",
        "clips": {
            "shrug": {
                "intent": "express.wistful",
                "keyframes": [
                    {"t_ms": 0, "joints": {"pan": 0}},
                    {"t_ms": 100, "joints": {"pan": 5}},
                ],
            }
        },
    }
    report = validate_motion_pack(pack)
    assert not report.ok
    assert "unknown_intent" in codes(report)


# ----------------------------------------------------------------- version


def test_the_reported_version_matches_the_installed_distribution():
    """One declaration, in `pyproject.toml`, read back at import time.

    This test exists because the two used to be written separately and drifted:
    the installed metadata said 0.1.0 while the source said 0.2.0, and
    nothing noticed because nothing compared them. A version that is quietly
    wrong is worse than no version, because it gets reported in bug reports as
    fact.
    """
    from importlib.metadata import version

    import emet_sdk

    assert emet_sdk.__version__ == version("emet-sdk")
    assert emet_sdk.__version__ != "0+unknown", "package is not installed"
