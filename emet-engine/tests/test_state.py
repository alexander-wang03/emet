"""Body-local state: what one body learned about itself, kept beside it."""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from types import MappingProxyType

import pytest

import emet_engine
from emet_engine import state
from emet_engine.state import MACHINE_STATE_DIR, STATE_VERSION, BodyState, state_path
from emet_sdk.resolve import Binding, BindingTable, load_chains, resolve
from emet_sdk.types import Health

#: The function as shipped. This module is imported when pytest collects it,
#: before the guard in conftest.py replaces `state.default_dirs` for a test.
SHIPPED_DEFAULT_DIRS = state.default_dirs


@pytest.fixture
def machine(tmp_path) -> Path:
    """The machine place, as the guard in conftest.py points it."""
    return tmp_path / "machine-state"


@pytest.fixture
def person(tmp_path) -> Path:
    """The person's place, as the guard in conftest.py points it."""
    return tmp_path / "user-state"


def write_json(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


HEAD = {
    "id": "head",
    "type": "joint_group",
    "role": "head",
    "driver": {"plugin": "emet_hal.pca9685", "params": {"bus": 1}},
    "joints": [
        {"id": "pan", "axis": "yaw", "channel": 0, "range_deg": [-90, 90], "trim_deg": 0},
        {"id": "tilt", "axis": "pitch", "channel": 1, "range_deg": [-30, 45]},
    ],
}


# ------------------------------------------------------------ the guard


def test_the_suite_never_reads_or_writes_a_real_state_place(tmp_path):
    """The guard in conftest.py. Without it, the suite writes into a
    developer's ~/.local/state and reads whatever the last run left there."""
    dirs = state.default_dirs()

    assert dirs == [tmp_path / "machine-state", tmp_path / "user-state"]
    assert MACHINE_STATE_DIR not in dirs and state.user_state_dir() not in dirs

    saved = BodyState.load("guard_body").save()
    assert saved is not None and tmp_path in saved.parents


def test_the_default_places_are_the_machine_then_the_person():
    # The shipped function only builds paths and touches nothing, so calling
    # it leaves the guard, and every other guard, in place.
    assert SHIPPED_DEFAULT_DIRS is not state.default_dirs
    assert SHIPPED_DEFAULT_DIRS() == [MACHINE_STATE_DIR, state.user_state_dir()]
    assert MACHINE_STATE_DIR == Path("/etc/emet/state")


def test_the_persons_place_honours_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state.user_state_dir() == tmp_path / "xdg" / "emet"


def test_without_xdg_state_home_the_persons_place_is_under_the_home_directory(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert state.user_state_dir() == Path.home() / ".local" / "state" / "emet"


# ----------------------------------------------------- where the file is


def test_a_named_file_wins_over_one_already_in_a_default_place(tmp_path, machine):
    write_json(machine / "scout.json", {})
    named = tmp_path / "elsewhere.json"
    assert state_path("scout", named) == named
    assert state_path("scout", str(named)) == named


def test_a_named_directory_holds_the_file_named_by_the_body(tmp_path):
    folder = tmp_path / "states"
    folder.mkdir()
    assert state_path("scout", folder) == folder / "scout.json"


def test_an_existing_file_in_the_persons_place_wins_over_an_empty_machine_place(machine, person):
    machine.mkdir()
    write_json(person / "scout.json", {})
    assert state_path("scout") == person / "scout.json"


def test_the_machines_file_wins_when_both_places_hold_one(machine, person):
    write_json(machine / "scout.json", {})
    write_json(person / "scout.json", {})
    assert state_path("scout") == machine / "scout.json"


def test_with_no_file_yet_a_writable_machine_place_is_used(machine):
    machine.mkdir()
    assert state_path("scout") == machine / "scout.json"


def test_a_missing_machine_place_falls_to_the_person_and_is_never_created(machine, person):
    assert state_path("scout") == person / "scout.json"
    assert not machine.exists() and not person.exists()


def test_a_machine_place_that_cannot_be_written_falls_to_the_person(machine, person, monkeypatch):
    machine.mkdir()
    monkeypatch.setattr(state, "_writable", lambda directory: directory != machine)
    assert state_path("scout") == person / "scout.json"


def test_when_nothing_can_be_written_the_persons_place_is_still_the_answer(machine, person, monkeypatch):
    machine.mkdir()
    monkeypatch.setattr(state, "_writable", lambda directory: False)
    assert state_path("scout") == person / "scout.json"


def test_a_machine_place_this_user_cannot_look_into_falls_to_the_person(machine, person, monkeypatch):
    """A system install may keep /etc/emet/state private to its service's
    user. Asking about anything inside it then raises PermissionError, where
    a missing file would only say no, and a person running the robot by hand
    still gets a place of their own."""
    write_json(machine / "scout.json", {"trims": {"head": {"pan": 9.0}}})
    real_stat = os.stat

    def private(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)):
            target = Path(path)
            if target == machine or machine in target.parents:
                raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(state.os, "stat", private)
    monkeypatch.setattr(state, "_writable", lambda directory: directory != machine)

    body = BodyState.load("scout")
    assert body.enabled and body.path == person / "scout.json"
    assert body.warnings == [] and body.trims("head") == {}
    assert body.save() == person / "scout.json"


def test_a_user_with_no_home_directory_runs_without_state(tmp_path, monkeypatch):
    """A service account may have no home. `Path.home()` then raises
    RuntimeError, and loading still never raises."""

    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(state, "default_dirs", lambda: [tmp_path / "machine-state", state.user_state_dir()])
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(no_home))

    body = BodyState.load("scout")
    assert not body.enabled and "home directory" in body.warnings[0]
    assert body.save() is None


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", ".hidden", "", "with space"])
def test_a_body_id_that_would_leave_its_directory_is_refused(bad):
    with pytest.raises(ValueError, match="cannot name a state file"):
        state_path(bad)


# ------------------------------------------------------------- loading


def test_a_missing_file_is_an_empty_state_with_nothing_to_say(person):
    body = BodyState.load("scout")
    assert body.enabled and body.path == person / "scout.json"
    assert body.data == {} and body.warnings == [] and body.last_saved is None


def test_without_a_body_id_nothing_is_kept_and_nothing_is_written(tmp_path):
    body = BodyState.load(None)
    body.carry("wake.mock", {"x": 1})
    body.set_trim("head", "pan", 2.0)

    assert not body.enabled and body.path is None
    assert body.save() is None
    assert body.carried("wake.mock") == {"x": 1}, "in memory it still works"
    assert list(tmp_path.rglob("*.json")) == []


def test_naming_a_file_without_a_body_id_says_that_nothing_is_kept(tmp_path):
    body = BodyState.load("", tmp_path / "x.json")
    assert not body.enabled
    assert "no body.id" in body.warnings[0]


def test_an_unusable_body_id_turns_the_state_off_with_a_warning():
    body = BodyState.load("../../etc/passwd")
    assert not body.enabled and body.path is None
    assert "body-local state is off" in body.warnings[0]
    assert body.save() is None


#: Files a Pi can end up holding. A power cut on an SD card can leave a file
#: empty or full of zeros; Python's own json writer puts NaN in by default.
NOT_JSON = {
    "typo": b"{ this is not json",
    "empty": b"",
    "zeros": bytes(64),
    "not_utf8": bytes([0x80, 0x81, 0x82]),
    "nan": b'{"carry": {"wake.mock": {"cmninit": NaN}}}',
    "infinity": b'{"trims": {"head": {"pan": Infinity}}}',
}


@pytest.mark.parametrize("raw", list(NOT_JSON.values()), ids=list(NOT_JSON))
def test_a_file_that_is_not_json_is_set_aside_before_the_next_save(person, raw):
    path = person / "scout.json"
    person.mkdir()
    path.write_bytes(raw)

    body = BodyState.load("scout")
    assert body.data == {} and body.carried("wake.mock") == {}
    assert "is not a JSON object" in body.warnings[0] and "scout.json.bad" in body.warnings[0]
    assert not (person / "scout.json.bad").exists(), "nothing moves until a save"

    assert body.save() == path
    assert (person / "scout.json.bad").read_bytes() == raw
    assert read_json(path)["body_id"] == "scout"


def test_a_later_bad_file_never_writes_over_an_earlier_one(person):
    path = person / "scout.json"
    person.mkdir()
    path.write_bytes(b"first, holding the old calibration")
    BodyState.load("scout").save()

    path.write_bytes(b"second")
    body = BodyState.load("scout")
    assert f"{person / 'scout.json.bad.2'} before the next save" in body.warnings[0]
    assert body.save() == path

    assert (person / "scout.json.bad").read_bytes() == b"first, holding the old calibration"
    assert (person / "scout.json.bad.2").read_bytes() == b"second"


def test_a_bad_file_already_set_aside_is_not_copied_twice(person):
    """A save that failed after the copy leaves the same bad file for the
    next boot, which finds its bytes already set aside."""
    person.mkdir()
    (person / "scout.json").write_bytes(b"{ bad")
    (person / "scout.json.bad").write_bytes(b"{ bad")

    body = BodyState.load("scout")
    assert f"{person / 'scout.json.bad'} before the next save" in body.warnings[0]
    body.save()
    assert sorted(p.name for p in person.iterdir()) == ["scout.json", "scout.json.bad"]


def test_when_every_set_aside_name_is_taken_the_file_is_left_alone(person):
    person.mkdir()
    path = person / "scout.json"
    path.write_bytes(b"{ bad")
    taken = ["scout.json.bad"] + [f"scout.json.bad.{n}" for n in range(2, state._MAX_BAD + 1)]
    for name in taken:
        (person / name).write_bytes(b"older")

    body = BodyState.load("scout")
    assert "Move them somewhere else" in body.warnings[0]
    assert body.save() is None
    assert path.read_bytes() == b"{ bad"
    assert all((person / name).read_bytes() == b"older" for name in taken)


def test_a_file_that_is_json_but_not_an_object_is_set_aside_too(person):
    write_json(person / "scout.json", [1, 2, 3])
    body = BodyState.load("scout")
    assert body.data == {} and body.warnings
    body.save()
    assert read_json(person / "scout.json.bad") == [1, 2, 3]


def test_a_section_of_the_wrong_shape_is_set_aside_and_the_rest_is_kept(person):
    write_json(person / "scout.json", {"carry": ["not", "a", "mapping"], "trims": {"head": {"pan": 1.5}}})

    body = BodyState.load("scout")
    assert "carry" in body.warnings[0]
    assert body.trims("head") == {"pan": 1.5}
    assert body.carried("wake.mock") == {}

    body.save()
    assert read_json(person / "scout.json.bad")["carry"] == ["not", "a", "mapping"]
    saved = read_json(person / "scout.json")
    assert saved["carry"] == {} and saved["trims"] == {"head": {"pan": 1.5}}


def test_a_file_from_a_newer_engine_is_read_as_empty_and_never_overwritten(person):
    newer = {"state_version": "9.0", "body_id": "scout", "trims": {"head": {"pan": 3.0}}}
    path = write_json(person / "scout.json", newer)
    before = path.read_bytes()

    body = BodyState.load("scout")
    body.set_trim("head", "pan", 1.0)

    assert body.enabled and body.data != newer
    assert "newer engine" in body.warnings[0]
    assert body.save() is None
    assert path.read_bytes() == before
    assert not path.with_name("scout.json.tmp").exists()


def test_versions_compare_as_integers_part_by_part(person, monkeypatch):
    monkeypatch.setattr(state, "STATE_VERSION", "0.9")
    write_json(person / "scout.json", {"state_version": "0.10"})
    assert BodyState.load("scout").save() is None, "0.10 is newer than 0.9"

    write_json(person / "scout.json", {"state_version": "0.9.0"})
    assert BodyState.load("scout").save() is not None, "0.9.0 is 0.9"


def test_a_version_that_cannot_be_read_is_left_alone(person):
    path = write_json(person / "scout.json", {"state_version": "next", "carry": {}})
    body = BodyState.load("scout")
    assert "cannot read" in body.warnings[0]
    assert body.save() is None
    assert read_json(path)["state_version"] == "next"


def test_another_bodys_file_is_never_used_or_overwritten(person):
    lamp = {"state_version": STATE_VERSION, "body_id": "lamp", "trims": {"head": {"pan": 4.0}}}
    path = write_json(person / "scout.json", lamp)
    body = BodyState.load("scout")
    assert body.trims("head") == {}
    assert "belongs to body 'lamp'" in body.warnings[0]
    assert body.save() is None
    assert read_json(path)["body_id"] == "lamp"


def test_a_file_that_cannot_be_read_is_left_alone(person):
    (person / "scout.json").mkdir(parents=True)
    body = BodyState.load("scout")
    assert body.warnings and "could not be read" in body.warnings[0]
    assert body.save() is None
    assert (person / "scout.json").is_dir()


# -------------------------------------------------------------- saving


def test_a_save_writes_every_key_with_its_stamps(person):
    path = BodyState.load("pi_speakerphone").save()

    doc = read_json(path)
    assert list(doc) == [
        "state_version",
        "body_id",
        "updated",
        "engine",
        "carry",
        "trims",
        "devices",
        "health",
        "bindings",
        "doa_reference_deg",
        "camera_intrinsics",
        "odometry",
    ]
    assert doc["state_version"] == STATE_VERSION and doc["body_id"] == "pi_speakerphone"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", doc["updated"])
    assert doc["engine"] == emet_engine.__version__
    assert doc["devices"] == {"input": None, "output": None}
    assert doc["doa_reference_deg"] is None and doc["camera_intrinsics"] == {} and doc["odometry"] == {}


def test_without_installed_metadata_the_engine_is_stamped_unknown(monkeypatch):
    def missing(name):
        raise state.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(state.metadata, "version", missing)
    assert read_json(BodyState.load("scout").save())["engine"] == "0+unknown"


def test_keys_this_version_does_not_know_are_written_back(person):
    path = write_json(
        person / "scout.json",
        {
            "state_version": STATE_VERSION,
            "body_id": "scout",
            "from_the_future": {"x": [1, 2]},
            "devices": {"input": "old", "output": "old", "mixer": "hw:0"},
        },
    )
    body = BodyState.load("scout")
    body.record_devices("new in", "new out")
    body.save()

    doc = read_json(path)
    assert doc["from_the_future"] == {"x": [1, 2]}
    assert doc["devices"] == {"input": "new in", "output": "new out", "mixer": "hw:0"}


def test_a_save_leaves_no_temporary_file_behind(person):
    body = BodyState.load("scout")
    body.save()
    body.carry("wake.mock", {"cmninit": "40,3"})
    body.save()
    assert sorted(p.name for p in person.iterdir()) == ["scout.json"]


def test_a_save_that_fails_keeps_the_old_file_and_says_why(person, monkeypatch):
    body = BodyState.load("scout")
    body.set_trim("head", "pan", 1.0)
    path = body.save()
    before = path.read_bytes()

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(state.os, "replace", refuse)
    body.set_trim("head", "pan", 2.0)

    assert body.save() is None
    assert "was not saved" in body.warnings[-1] and "Permission denied" in body.warnings[-1]
    assert path.read_bytes() == before
    assert sorted(p.name for p in person.iterdir()) == ["scout.json"]


def test_a_value_that_cannot_be_written_as_json_fails_the_save_without_raising():
    body = BodyState.load("scout")
    body.data["from_a_caller"] = {1, 2, 3}
    assert body.save() is None
    assert "was not saved" in body.warnings[-1]


def test_the_persons_place_is_created_on_the_first_save(person):
    assert not person.exists()
    assert BodyState.load("scout").save() == person / "scout.json"


def test_the_machine_place_is_never_created_even_when_it_vanishes(machine):
    machine.mkdir()
    body = BodyState.load("scout")
    assert body.path == machine / "scout.json"
    machine.rmdir()

    assert body.save() is None
    assert "never creates it" in body.warnings[-1]
    assert not machine.exists()


def test_a_save_records_where_it_went_and_the_document_it_wrote():
    body = BodyState.load("scout")
    path = body.save()
    assert body.last_saved == path
    assert body.data["body_id"] == "scout" and "updated" in body.data


def test_a_named_file_is_saved_where_it_was_named(tmp_path):
    named = tmp_path / "deep" / "er" / "mine.json"
    assert BodyState.load("scout", named).save() == named
    assert read_json(named)["body_id"] == "scout"


# --------------------------------------------------------- carry-over


def test_a_carried_value_comes_back_at_the_next_boot():
    first = BodyState.load("scout")
    first.carry("wake.pocketsphinx", {"cmninit": "43.0,6.0,-1.2"})
    first.save()

    second = BodyState.load("scout")
    assert second.carried("wake.pocketsphinx") == {"cmninit": "43.0,6.0,-1.2"}
    assert second.carried("wake.other") == {}


def test_carrying_an_empty_mapping_forgets_the_key():
    body = BodyState.load("scout")
    body.carry("wake.mock", {"x": 1})
    body.carry("wake.mock", {})
    body.carry("never.there", {})
    assert body.carried("wake.mock") == {}
    assert "wake.mock" not in body.data["carry"]


def test_what_carried_returns_is_a_copy():
    body = BodyState.load("scout")
    params = {"nested": {"a": 1}}
    body.carry("wake.mock", params)
    params["nested"]["a"] = 2
    got = body.carried("wake.mock")
    got["nested"]["a"] = 3
    assert body.carried("wake.mock") == {"nested": {"a": 1}}


def test_a_carry_that_cannot_be_written_down_keeps_what_was_there():
    body = BodyState.load("scout")
    body.carry("wake.mock", {"x": 1})

    body.carry("wake.mock", {"bad": object()})
    body.carry("wake.mock", {"nan": float("nan")})
    body.carry("wake.mock", ["not", "a", "mapping"])  # type: ignore[arg-type]

    assert body.carried("wake.mock") == {"x": 1}
    assert len(body.warnings) == 3 and all("wake.mock" in w for w in body.warnings)


# -------------------------------------------------------------- trims


def test_a_trim_comes_back_at_the_next_boot():
    first = BodyState.load("scout")
    first.set_trim("head", "pan", 1.5)
    first.set_trim("head", "tilt", -2)
    first.save()

    assert BodyState.load("scout").trims("head") == {"pan": 1.5, "tilt": -2.0}
    assert BodyState.load("scout").trims("arm") == {}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_trim_that_is_not_a_finite_number_is_refused(bad):
    body = BodyState.load("scout")
    with pytest.raises(ValueError, match="finite"):
        body.set_trim("head", "pan", bad)
    assert body.trims("head") == {}


def test_a_trim_in_the_file_that_is_not_a_number_is_ignored(person):
    trims = {"head": {"pan": "left a bit", "tilt": 1, "roll": True, "yaw": 10**400}, "arm": 5}
    write_json(person / "scout.json", {"trims": trims})
    body = BodyState.load("scout")
    assert body.trims("head") == {"tilt": 1.0}
    assert body.trims("arm") == {}


def test_the_state_files_trim_wins_over_the_manifests():
    body = BodyState.load("scout")
    body.set_trim("head", "pan", 1.5)
    body.set_trim("head", "tilt", -0.5)
    body.set_trim("head", "not_in_the_manifest", 9.0)

    block = body.overlay_trims(HEAD)

    assert [j["trim_deg"] for j in block["joints"]] == [1.5, -0.5]
    assert [j["id"] for j in block["joints"]] == ["pan", "tilt"]


def test_overlaying_trims_never_changes_the_block_it_was_given():
    body = BodyState.load("scout")
    body.set_trim("head", "pan", 1.5)
    original = copy.deepcopy(HEAD)

    block = body.overlay_trims(HEAD)
    block["driver"]["params"]["bus"] = 99

    assert HEAD == original
    assert block["joints"] is not HEAD["joints"]


def test_other_blocks_and_untrimmed_groups_come_back_as_plain_copies():
    body = BodyState.load("scout")
    body.set_trim("head", "pan", 1.5)
    drive = {"id": "base", "type": "drive", "kinematics": "tracked", "limits": {"max_linear_mps": 0.35}}
    arm = dict(HEAD, id="arm", role="arm")

    assert body.overlay_trims(drive) == drive and body.overlay_trims(drive) is not drive
    assert body.overlay_trims(arm) == arm


def test_a_frozen_block_can_be_overlaid():
    body = BodyState.load("scout")
    body.set_trim("head", "pan", 1.5)
    frozen = MappingProxyType({**HEAD, "joints": tuple(MappingProxyType(j) for j in HEAD["joints"])})

    block = body.overlay_trims(frozen)

    assert isinstance(block, dict)
    assert block["joints"][0]["trim_deg"] == 1.5
    assert frozen["joints"][0]["trim_deg"] == 0


# ---------------------------------------------------- what a boot records


def test_the_binding_table_is_recorded_by_intent():
    table = BindingTable(
        {
            "express.curiosity": Binding(intent="express.curiosity", rung_index=3, action="inflect", is_voice=True),
            "attend.person": Binding(intent="attend.person", rung_index=0, action="look_at", capability_id="head"),
        }
    )
    body = BodyState.load("scout")
    body.data["bindings"] = {"stale.intent": {"target": "voice", "action": "utter", "rung": 0}}

    body.record_bindings(table)

    assert body.data["bindings"] == {
        "attend.person": {"target": "head", "action": "look_at", "rung": 0},
        "express.curiosity": {"target": "voice", "action": "inflect", "rung": 3},
    }


def test_a_real_binding_table_goes_round_a_save():
    table = resolve(load_chains())
    body = BodyState.load("scout")
    body.record_bindings(table)
    path = body.save()

    recorded = read_json(path)["bindings"]
    assert set(recorded) == set(table.bindings)
    assert all(entry["target"] == "voice" for entry in recorded.values())


def test_devices_are_recorded_and_a_device_without_a_name_is_kept_as_null():
    body = BodyState.load("scout")
    body.record_devices("USB PnP Sound Device: Audio (plughw:3,0)", None)
    path = body.save()

    assert read_json(path)["devices"] == {"input": "USB PnP Sound Device: Audio (plughw:3,0)", "output": None}


def test_health_is_recorded_by_capability_and_replaces_the_last_record():
    body = BodyState.load("scout")
    body.record_health({"old": Health()})
    body.record_health(
        {
            "head": Health(),
            "base": Health(ok=False, detail="left motor stalled", faults=("overcurrent", "timeout")),
        }
    )
    path = body.save()

    assert read_json(path)["health"] == {
        "base": {"ok": False, "detail": "left motor stalled", "faults": ["overcurrent", "timeout"]},
        "head": {"ok": True, "detail": None, "faults": []},
    }


def test_carry_says_whether_it_kept_the_params(tmp_path):
    """The engine reports a key as kept only when the state file took it."""
    state = BodyState.load("said", tmp_path / "said.json")
    assert state.carry("wake.mock", {"cmninit": "1,2"}) is True
    assert state.carry("capability.eyes", {"blob": b"bytes"}) is False
    assert state.carry("capability.head", {}) is False
    assert state.carried("wake.mock") == {"cmninit": "1,2"}
