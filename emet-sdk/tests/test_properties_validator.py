"""Seeded properties of validation (RP8, stage 1).

Each test states one property, quotes the sentence of `DESIGN.md`, a
docstring or the roadmap it comes from, and runs over the seeds
`_generators.seeds()` names: a generated body manifest or chain document,
one broken rule in a copy of it, or several stacked with values no schema
allows. The registry is `_generators.registry()`, so no property depends on
what is installed.

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

import copy
import json
import os
import random
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from _generators import (
    DEFAULT_SEEDS,
    MUTATIONS,
    PLUGIN_SITES,
    chain_set,
    manifest,
    mutate,
    mutate_chains,
    registry,
    rerun,
    rng_for,
    seed_params,
    stacked,
    stacked_chains,
    unknown_driver,
)
from emet_sdk.chains import UnterminatedChainError, parse_chain_set
from emet_sdk.validate import (
    MissingPluginError,
    ValidationError,
    ValidationReport,
    validate_chain_document,
    validate_manifest,
)

HERE = Path(__file__).resolve().parent
FILE = "tests/test_properties_validator.py"

#: Seeds of the default range on which a property is refuted, by finding.
#: Empty since B7-F3 and B7-F5 were fixed; a finding the properties refute
#: again is added here with its reason, as `test_properties_resolver.py` does.
KNOWN: dict[str, dict[str, tuple[int, ...]]] = {}

REASONS: dict[str, str] = {}


def seeds_for(test: str) -> list:
    return seed_params(test, KNOWN, REASONS)


@lru_cache(maxsize=None)
def generated(seed: int) -> dict:
    """A manifest, generated once a seed and never changed: every test
    mutates a copy."""
    return manifest(random.Random(seed))


@lru_cache(maxsize=None)
def generated_chains(seed: int) -> dict:
    return chain_set(random.Random(seed))


def codes(report: ValidationReport) -> set[str]:
    return {f.code for f in report.errors}


@pytest.mark.parametrize("seed", seeds_for("valid"))
def test_every_generated_manifest_validates_with_no_finding(seed):
    """DESIGN.md section 4.3, "Validation rules": "`audio.input` and
    `audio.output` are **required**. There is no valid manifest without
    them."; "Capability `id` values are unique and match `[a-z][a-z0-9_]*`.";
    "Every joint's `home_deg` lies within `range_deg`."; "At most one `drive`
    block (`P0`)."; "`audio.wake.engine`, `audio.input.source` and
    `audio.output.sink` resolve to installed plugins, or validation fails."
    validate.py adds a rule section 4.3 does not list: "a capability id
    referenced by a camera mount" has to be a part the manifest declares. A
    driver not installed only warns outside `--verify-drivers` (section
    4.3), and the generator names installed ones. A manifest the generator
    builds keeps every one of these, so the validator has nothing to say
    about it.
    """
    report = validate_manifest(generated(seed), registry=registry())
    assert not report.findings, f"{rerun(seed, FILE, 'test_every_generated_manifest')}: {[str(f) for f in report.findings]}"


@pytest.mark.parametrize("seed", seeds_for("kind"))
def test_each_broken_rule_is_reported_as_its_own_kind_and_no_other(seed):
    """validate.py: "a missing plugin is never reported as a schema error",
    and "Semantic rules assume a well-shaped document; running them on a
    broken one produces noise that buries the real error." DESIGN.md
    section 4.3: an unrecognised `kinematics` "is a *missing plugin* error,
    not a *schema* error. This is what keeps the enum open."

    The seed picks the rule in turn (`mutate(..., pick=seed)`), so the
    default range breaks every rule, and names a missing plugin at every
    site, kinematics among them.
    """
    mutation = mutate(rng_for(seed, "kind"), generated(seed), pick=seed)
    where = f"{rerun(seed, FILE, 'test_each_broken_rule')}: {mutation.name}"
    report = validate_manifest(mutation.doc, registry=registry())
    assert codes(report) == {mutation.code}, f"{where}: expected {mutation.code}, got {[str(f) for f in report.errors]}"
    try:
        report.raise_for_status()
    except ValidationError as raised:
        assert isinstance(raised, MissingPluginError) == (mutation.code == "missing_plugin"), where
    else:
        pytest.fail(f"{where}: raise_for_status() raised nothing")


def test_the_default_seeds_break_every_rule_and_name_every_missing_plugin_site():
    """What the property above claims for the default range, held on the
    generator: every code a mutation names, and a missing plugin at each of
    `PLUGIN_SITES`. A range drawn at random once left kinematics, the audio
    source and chat out of seeds 0 to 99."""
    drawn: set[str] = set()
    sites: set[str] = set()
    for seed in range(DEFAULT_SEEDS):
        mutation = mutate(rng_for(seed, "kind"), generated(seed), pick=seed)
        drawn.add(mutation.code)
        if mutation.code == "missing_plugin":
            sites.add(mutation.name.split(" ", 1)[0])
    assert drawn == {code for code, _ in MUTATIONS}
    assert sites == set(PLUGIN_SITES)


@pytest.mark.parametrize("seed", seeds_for("driver"))
def test_a_driver_not_installed_warns_and_fails_only_when_drivers_are_verified(seed):
    """DESIGN.md section 4.3: "`driver.plugin` may name hardware not yet
    wired and so only warns outside `--verify-drivers`"; validate.py: "an
    unrecognised driver name is a warning here by default", and with
    `verify_drivers=True` "makes it an error."
    """
    doc = unknown_driver(rng_for(seed, "driver"), generated(seed))
    where = rerun(seed, FILE, "test_a_driver_not_installed")
    lenient = validate_manifest(doc, registry=registry())
    assert not lenient.errors, f"{where}: {[str(f) for f in lenient.errors]}"
    assert [f.code for f in lenient.warnings] == ["driver_not_installed"], f"{where}"
    strict = validate_manifest(doc, registry=registry(verify_drivers=True))
    assert codes(strict) == {"missing_plugin"} and not strict.warnings, f"{where}: {[str(f) for f in strict.findings]}"


@pytest.mark.parametrize("seed", seeds_for("raises"))
def test_validation_raises_nothing_but_its_own_error_on_any_input(seed):
    """The roadmap's CI11 (`research/sota-2026-11-25/ci-cd.md`, outside the
    repository): "`validate` returns a schema error or a missing-plugin
    error and never raises"; `ValidationReport`: "Every problem found,
    rather than only the first."

    The input is two to six mutations stacked on a generated manifest, with
    values YAML can produce where no schema allows them, the fields of
    reserved parts among them. `raise_for_status` is the one way out, and it
    raises `ValidationError` or its `MissingPluginError`.
    """
    doc = stacked(rng_for(seed, "raises"), generated(seed))
    where = rerun(seed, FILE, "test_validation_raises_nothing")
    try:
        report = validate_manifest(doc, registry=registry())
    except Exception as exc:  # noqa: BLE001 - the property is that nothing is raised
        raise AssertionError(f"{where}: {type(exc).__name__}: {exc}; input {doc!r:.600}") from exc
    assert isinstance(report, ValidationReport), where
    try:
        report.raise_for_status()
    except ValidationError:
        pass


def twice_input(seed: int) -> dict:
    """The input of the two properties below: two to four broken rules of
    one kind, so that none hides another (a schema error silences the
    semantic rules), schema rules on odd seeds and semantic ones on even
    seeds, where two or three absent drivers are named as well."""
    rng = rng_for(seed, "twice")
    doc = copy.deepcopy(generated(seed))
    schema = seed % 2 == 1
    pool = [m for m in MUTATIONS if (m[0] == "schema") == schema]
    for _ in range(rng.randint(2, 4)):
        try:
            rng.choice(pool)[1](doc, rng)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            break  # an earlier mutation took away what this one edits, as in `stacked`
    if not schema:
        doc = unknown_driver(rng, doc, rng.randint(2, 3))
    return doc


def findings_in_order(seed: int) -> list[str]:
    return [str(f) for f in validate_manifest(twice_input(seed), registry=registry()).findings]


@pytest.mark.parametrize("seed", seeds_for("twice"))
def test_validating_twice_gives_the_same_findings_in_the_same_order(seed):
    """DESIGN.md section 3: "deterministic pure functions over contract data
    belong in the SDK"; the CLI prints `report.findings` in order, so the
    order is part of what a builder reads.

    With one finding or none, no order could differ; 186 of the 200
    default seeds give two findings or more.
    """
    first = findings_in_order(seed)
    second = findings_in_order(seed)
    assert first == second, f"{rerun(seed, FILE, 'test_validating_twice')}"


#: Seeds the property below checks in other interpreters.
HASHED = range(0, 200, 4)


def test_the_findings_come_in_the_same_order_in_an_interpreter_with_another_hash_seed():
    """The property above across processes. Each `emet validate` is a new
    interpreter with its own string-hash seed, and an order that follows a
    set or a hash repeats within one process and changes between two. Two
    other interpreters, with `PYTHONHASHSEED` 0 and 12345, validate the same
    inputs, and every seed's findings come in the order this one gives."""
    here = {seed: findings_in_order(seed) for seed in HASHED}
    assert sum(len(f) > 1 for f in here.values()) >= len(HASHED) * 3 // 4
    program = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from test_properties_validator import findings_in_order\n"
        "print(json.dumps([findings_in_order(int(s)) for s in sys.argv[2:]]))\n"
    )
    for hash_seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        done = subprocess.run([sys.executable, "-c", program, str(HERE), *map(str, HASHED)],
                              capture_output=True, text=True, encoding="utf-8", env=env, cwd=HERE, timeout=120)
        assert done.returncode == 0, done.stderr
        there = dict(zip(HASHED, json.loads(done.stdout)))
        for seed in HASHED:
            assert there[seed] == here[seed], f"{rerun(seed, FILE, 'test_the_findings_come_in_the_same_order')}: PYTHONHASHSEED={hash_seed}"


@pytest.mark.parametrize("seed", seeds_for("chains-valid"))
def test_every_generated_chain_document_validates_with_no_finding(seed):
    """DESIGN.md section 6: "**Validation:** the SDK **rejects** any chain
    whose final rung is not a voice rung"; section 6.1, as built in 0.5:
    "`emet_sdk.chains.VOICE_ACTIONS` names the six, and the validator
    rejects a voice rung naming anything else." A generated chain names
    intents of the closed vocabulary and ends in one of the six, with the
    words, tone or topic it needs.
    """
    doc = generated_chains(seed)
    where = rerun(seed, FILE, "test_every_generated_chain_document")
    report = validate_chain_document(doc)
    assert not report.findings, f"{where}: {[str(f) for f in report.findings]}"
    assert set(parse_chain_set(doc)) == set(doc), where


@pytest.mark.parametrize("seed", seeds_for("chains-kind"))
def test_each_broken_chain_rule_is_reported_as_its_own_kind(seed):
    """chains.py, `UnterminatedChainError`: "Its own exception type because
    this is worse than a typo: a chain that can fail to bind on a
    sufficiently bare body"; intents.py: "an unrecognised intent is a
    *validation failure*, not a no-op."
    """
    mutation = mutate_chains(rng_for(seed, "chains-kind"), generated_chains(seed))
    where = f"{rerun(seed, FILE, 'test_each_broken_chain_rule')}: {mutation.name}"
    report = validate_chain_document(mutation.doc)
    assert codes(report) == {mutation.code}, f"{where}: expected {mutation.code}, got {[str(f) for f in report.errors]}"
    if mutation.code == "unterminated_chain":
        try:
            parse_chain_set(mutation.doc)
        except UnterminatedChainError:
            pass
        else:
            pytest.fail(f"{where}: parse_chain_set raised no UnterminatedChainError")


@pytest.mark.parametrize("kind", ["express", "move", "attend", "idle", "manipulate", "signal"])
def test_a_kind_that_takes_an_argument_is_refused_without_one(kind):
    """intents.py: "an unrecognised intent is a *validation failure*, not a
    no-op"; `validate_intent_name` refuses a kind that "requires an
    argument" when it is given none, as a bare `express` is."""
    chain = {"rungs": [{"actuator": {"voice": True}, "action": "utter"}]}
    report = validate_chain_document({kind: chain})
    assert [(f.code, f.path) for f in report.errors] == [("unknown_intent", f"/{kind}")]
    assert f"intent {kind!r} requires an argument, one of: " in report.errors[0].message


@pytest.mark.parametrize("seed", seeds_for("chains-raises"))
def test_chain_validation_raises_nothing_on_any_input(seed):
    """validate.py, `validate_chain_document`: "Validate a chain file,
    including the terminal-voice-rung rule", which `emet validate`, `emet
    explain --chains` and the engine's `--chains` all call; CI11's
    "`validate` ... never raises" holds it to the manifest's standard.

    The input is two to six chain mutations stacked, with odd values and
    top-level keys YAML can produce (`1:`, `null:`, `yes:`).
    """
    doc = stacked_chains(rng_for(seed, "chains-raises"), generated_chains(seed))
    where = rerun(seed, FILE, "test_chain_validation_raises")
    try:
        report = validate_chain_document(doc)
    except Exception as exc:  # noqa: BLE001 - the property is that nothing is raised
        raise AssertionError(f"{where}: {type(exc).__name__}: {exc}") from exc
    assert isinstance(report, ValidationReport), where


# ------------------------------------- the cases the seeds above once refuted

#: The least a manifest holds, for the cases below to add a part to.
BARE = {
    "manifest_version": "0.1",
    "body": {"id": "bare", "scale": "desk", "power": "plugged_in"},
    "audio": {"input": {"device": "default"}, "output": {"device": "default"}},
}

#: A P0 part a reserved part may be mounted on.
LAMP = {"id": "lamp", "type": "light", "role": "status", "driver": {"plugin": "acme_leds"}}


def with_parts(*parts: dict) -> dict:
    return {**copy.deepcopy(BARE), "capabilities": [copy.deepcopy(p) for p in parts]}


@pytest.mark.parametrize("driver", [2.5, ["acme_servo"], "acme_servo", True, 0, None, {}])
def test_a_reserved_part_whose_driver_is_no_mapping_names_no_plugin(driver):
    """Seeds 88, 123 and 149 of the property above (finding B7-F3): the
    schema leaves a reserved part's fields open, so its `driver` may be a
    number, a list or a word. None of these names a plugin, so the part is
    passed over as one with no driver is, with no finding even when drivers
    are verified."""
    doc = with_parts({"id": "grip", "type": "manipulator", "driver": driver})
    for strict in (False, True):
        report = validate_manifest(doc, registry=registry(verify_drivers=strict))
        assert report.findings == [], (driver, strict, [str(f) for f in report.findings])


def test_a_reserved_part_whose_driver_is_a_mapping_still_has_its_plugin_checked():
    """validate.py: "an unrecognised driver name is a warning here by
    default", and `verify_drivers=True` "makes it an error", for a reserved
    part as for any other."""
    doc = with_parts({"id": "grip", "type": "manipulator", "driver": {"plugin": "acme_missing"}})
    lenient = validate_manifest(doc, registry=registry())
    assert [(f.severity, f.code, f.path) for f in lenient.findings] == [
        ("warning", "driver_not_installed", "/capabilities")]
    strict = validate_manifest(doc, registry=registry(verify_drivers=True))
    assert [(f.severity, f.code, f.path) for f in strict.findings] == [
        ("error", "missing_plugin", "/capabilities/0/driver/plugin")]
    installed = with_parts({"id": "grip", "type": "manipulator", "driver": {"plugin": "acme_servo"}})
    assert validate_manifest(installed, registry=registry(verify_drivers=True)).findings == []


@pytest.mark.parametrize("mount", [{"k": [None]}, ["lamp"], 7, 2.5, True])
def test_a_reserved_part_mounted_on_no_string_is_an_unknown_reference(mount):
    """Seeds 9, 30, 38, 50 and 119 of the property above (finding B7-F3):
    `mounted_on` names the part it sits on by its id, a string. A list or a
    mapping names no part, and is reported as a number always was, as a
    reference to no capability in the manifest."""
    report = validate_manifest(with_parts(LAMP, {"id": "grip", "type": "manipulator", "mounted_on": mount}),
                               registry=registry())
    assert [(f.code, f.path) for f in report.findings] == [("unknown_reference", "/capabilities/1/mounted_on")]
    assert report.errors[0].message == (
        f"'grip' is mounted_on {mount!r}, which is not a capability in this manifest. Known ids: grip, lamp")


@pytest.mark.parametrize("mount", ["lamp", "body", None])
def test_a_reserved_part_mounted_on_a_part_the_manifest_declares_is_no_finding(mount):
    """validate.py: "a capability id referenced by a camera mount" has to be
    a part the manifest declares, and `body` or no mount at all is always
    one."""
    report = validate_manifest(with_parts(LAMP, {"id": "grip", "type": "manipulator", "mounted_on": mount}),
                               registry=registry())
    assert report.findings == []


VOICE_ONLY = {"rungs": [{"actuator": {"voice": True}, "action": "inflect",
                         "params": {"preset": "rising", "filler": "hm?"}}]}


def test_a_chain_named_by_no_string_is_an_unknown_intent_and_the_rest_are_still_checked():
    """Seed 18 of the property above (finding B7-F5): YAML reads an
    unquoted `1:`, `null:` or `yes:` as a number, a null or a boolean, and
    a chain named so is outside the closed vocabulary. Each such name is
    reported where it stands, and the names after it are checked too."""
    doc = {1: VOICE_ONLY, None: VOICE_ONLY, 2.5: VOICE_ONLY, "express.glee": VOICE_ONLY,
           "express.curiosity": VOICE_ONLY}
    report = validate_chain_document(doc)
    assert [(f.code, f.path) for f in report.findings] == [
        ("unknown_intent", "/1"), ("unknown_intent", "/None"), ("unknown_intent", "/2.5"),
        ("unknown_intent", "/express.glee")]
    assert [f.message for f in report.errors[:3]] == [
        f"a chain is named {name!r} ({kind}), and an intent's name is a string such as "
        f"'express.curiosity'. YAML reads an unquoted 1, null or yes as a number, a null or a boolean."
        for name, kind in ((1, "int"), (None, "NoneType"), (2.5, "float"))]
    assert validate_chain_document({True: VOICE_ONLY}).errors[0].message.startswith(
        "a chain is named True (bool), and an intent's name is a string")


@pytest.mark.parametrize("doc", [None, [], ["express.curiosity"], "express.curiosity", 7])
def test_a_chain_file_that_is_no_mapping_is_one_schema_error(doc):
    """`validate_chain_document` reads "a mapping of intent name to chain",
    and a file of any other shape is one schema error saying so."""
    report = validate_chain_document(doc)
    assert [(f.severity, f.code, f.message, f.path) for f in report.findings] == [
        ("error", "schema", "a chain file must be a mapping of intent name to chain", "")]


def test_a_chain_in_mode_all_validates_with_one_warning_that_names_it():
    """`ChainMode.ALL`: "RSV. Bind every matching rung". The validator
    accepts it and warns, at the chain's `mode`, that P0 binds one rung."""
    report = validate_chain_document({"express.curiosity": {**VOICE_ONLY, "mode": "all"}})
    assert [(f.severity, f.code, f.message, f.path) for f in report.findings] == [
        ("warning", "reserved_mode",
         "chain 'express.curiosity' uses mode: all, which is RSV. The P0 engine binds the first matching rung only.",
         "/express.curiosity/mode")]


def test_a_chain_file_whose_yaml_key_is_no_string_is_refused_by_the_loader(tmp_path):
    """`load_chains`, which `emet explain --chains` and the engine's
    `--chains` call: an override "is held to `emet validate`'s rules before
    it is laid over anything", and a problem "raises `ChainError` naming the
    file". A file whose one key YAML reads as a null is refused so, where it
    raised `AttributeError` before."""
    from emet_sdk.chains import ChainError
    from emet_sdk.resolve import load_chains
    from emet_sdk.validate import load_yaml

    path = tmp_path / "null-key.yaml"
    path.write_text("null:\n  rungs:\n    - actuator: {voice: true}\n      action: utter\n"
                    "      params: {text: hm}\n", encoding="utf-8")
    assert list(load_yaml(path)) == [None]
    with pytest.raises(ChainError, match=r"null-key\.yaml: a chain is named None \(NoneType\)"):
        load_chains([path])


def test_every_rerun_line_selects_the_one_test_it_was_printed_for():
    """The brief's "one line reruns it": the command a failure prints sits
    between backticks, so the detail after it stays out of `-k`, and the
    `-k` it names selects exactly the property whose failure printed it, in
    both property files."""
    message = f"{rerun(17, FILE, 'test_each_broken_rule')}: missing_plugin: chat names 'ollama'"
    assert message.split("`")[1] == f"EMET_PROPERTY_SEED=17 python -m pytest {FILE} -k test_each_broken_rule"
    for path in (HERE / "test_properties_resolver.py", HERE / "test_properties_validator.py"):
        source = path.read_text(encoding="utf-8")
        tests = re.findall(r"^def (test_\w+)\(", source, re.M)
        for k in re.findall(r"rerun\(\d*\w*, FILE, ['\"](\w+)['\"]\)", source):
            assert [t for t in tests if k in t] == [t for t in tests if t.startswith(k)], (path.name, k)
            assert len([t for t in tests if k in t]) == 1, (path.name, k)
