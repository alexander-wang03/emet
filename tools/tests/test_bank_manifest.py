"""tools/bank/manifest.py and tools/bank/check.py: the bank's files judged
the same way by every tool, and a bank refused for anything wrong in it."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import check
import manifest as bank_files
from conftest import real_bank_search


def a_manifest(sha: str = "0" * 64, frames: int = 32000) -> dict:
    return {
        "format": "emet-bank/1", "id": "t-0.1", "path": "../rec/t-0.1.wav", "sha256": sha,
        "sample_rate": 16000, "channels": 1, "bits": 16, "frames": frames, "duration_s": frames / 16000,
        "recorded": "2026-10-27", "start_time": None, "device": None, "capture_level": None,
        "room": "unrecorded", "position": None, "distance": "near", "distance_m": None, "facing": None,
        "speaker": "s01", "noise": None, "kind": "takes", "lead_s": 0.25, "prompt_every_s": 0.75,
        "made_with": "a test",
        "takes": [
            {"id": "t-0.1/01", "kind": "one_breath", "text": "hey emet, stop", "question": "stop",
             "first_sound": "s", "slot": {"start": 4000, "end": 16000}, "expect_wake": True,
             "retake_of": None, "exclude": None},
            {"id": "t-0.1/02", "kind": "short", "text": "hey emet, no", "question": "no",
             "first_sound": "n", "slot": {"start": 16000, "end": 28000}, "expect_wake": True,
             "retake_of": "t-0.1/01", "exclude": {"reason": "a cough over the name", "date": "2026-10-27"}},
        ],
    }


def a_bank() -> dict:
    return {
        "format": "emet-bank/1", "version": "2026-10-27", "files": ["manifests/t-0.1.json"],
        "rooms": {"unrecorded": {"description": "no record names the room"}},
        "speakers": {"s01": {"consent": "consent/s01.txt"}},
    }


def build_bank(tmp_path: Path, make_wav, make_tone) -> Path:
    """A bank of one recording at tmp/bank, its audio at tmp/rec."""
    wav = make_wav(tmp_path / "rec" / "t-0.1.wav", make_tone(2.0))
    root = tmp_path / "bank"
    doc = a_manifest(bank_files.sha256_file(wav))
    bank_files.write(root / "manifests" / "t-0.1.json", doc, "manifest")
    bank_files.write(root / "bank.json", a_bank(), "bank")
    (root / "consent").mkdir()
    (root / "consent" / "s01.txt").write_text("own recordings, private test use\n", encoding="utf-8")
    return root


# ------------------------------------------------------------ the formats


def test_a_sound_manifest_and_bank_have_no_problems():
    assert bank_files.problems(a_manifest(), "manifest") == []
    assert bank_files.problems(a_bank(), "bank") == []


@pytest.mark.parametrize("change, expect", [
    (lambda d: d.update(format="emet-bank/2"), "format is 'emet-bank/2'"),
    (lambda d: d.update(frams=1), "unknown field 'frams'"),
    (lambda d: d.pop("speaker"), "no 'speaker'"),
    (lambda d: d.update(speaker="alex"), "speaker is an id like s01"),
    (lambda d: d.update(sha256="ABC"), "sha256 is 64 lowercase hex"),
    (lambda d: d.update(duration_s=3.0), "duration_s 3.0 is not frames / sample_rate"),
    (lambda d: d.update(kind="podcast"), "kind is one of"),
    (lambda d: d.update(path="C:/rec/t.wav"), "path is relative"),
    (lambda d: d["takes"][0].update(kind="question"), "kind is one of"),
    (lambda d: d["takes"][1].update(id="other/02"), "id is the recording's id"),
    (lambda d: d["takes"][1].update(id="t-0.1/01"), "the id is used twice"),
    (lambda d: d["takes"][0]["slot"].update(end=40000), "past the recording's 32000 samples"),
    (lambda d: d["takes"][0]["slot"].update(start=16000), "ends after it starts"),
    (lambda d: d["takes"][1]["slot"].update(start=15999), "inside the slot before it"),
    (lambda d: d["takes"][1].update(retake_of="t-0.1/09"), "retake_of names an earlier take"),
    (lambda d: d["takes"][1].update(exclude={"reason": "", "date": "2026-10-27"}), "exclude is null or"),
    (lambda d: d["takes"][0].update(expect_wake="yes"), "expect_wake is true or false"),
])
def test_a_broken_manifest_is_refused_naming_its_problem(change, expect):
    doc = a_manifest()
    change(doc)
    found = bank_files.problems(doc, "manifest")
    assert any(expect in p for p in found), found


@pytest.mark.parametrize("change, expect", [
    (lambda d: d.update(version="27 October"), "version is the date"),
    (lambda d: d.update(files=["manifests/a.json", "manifests/a.json"]), "listed twice"),
    (lambda d: d.update(speakers={"Alex": {"consent": "consent/a.txt"}}), "never a name"),
    (lambda d: d.update(speakers={"s01": {"consent": "consent/s01.txt", "name": "x"}}), "names its consent file"),
    (lambda d: d.update(rooms={}), "rooms names at least one room"),
])
def test_a_broken_index_is_refused_naming_its_problem(change, expect):
    doc = a_bank()
    change(doc)
    found = bank_files.problems(doc, "bank")
    assert any(expect in p for p in found), found


def test_every_problem_is_listed_not_only_the_first():
    doc = a_manifest()
    doc.update(speaker="alex", kind="podcast", frams=1)
    assert len(bank_files.problems(doc, "manifest")) == 3


def test_write_refuses_an_unsound_document_and_writes_nothing(tmp_path):
    doc = a_manifest()
    doc["speaker"] = "alex"
    with pytest.raises(bank_files.BankError):
        bank_files.write(tmp_path / "m.json", doc, "manifest")
    assert not (tmp_path / "m.json").exists()


def test_a_file_written_twice_is_the_same_bytes(tmp_path):
    p = bank_files.write(tmp_path / "m.json", a_manifest(), "manifest")
    first = p.read_bytes()
    bank_files.write(p, bank_files.read(p, "manifest"), "manifest")
    assert p.read_bytes() == first and first.endswith(b"}\n") and b"\r" not in first


def test_read_refuses_another_format_and_bad_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({**a_manifest(), "format": "emet-golden/1"}), encoding="utf-8")
    with pytest.raises(bank_files.BankError, match="this reads 'emet-bank/1'"):
        bank_files.read(p, "manifest")
    p.write_text("{", encoding="utf-8")
    with pytest.raises(bank_files.BankError, match="not readable JSON"):
        bank_files.read(p, "manifest")


def a_cassette_turn() -> dict:
    return {
        "request": {"provider": "deepgram", "model": "nova-3", "sample_rate": 16000,
                    "query": {"punctuate": "true", "smart_format": "false"}},
        "fed_sha256": "a" * 64, "fed_bytes": 64000,
        "partials": [{"text": "Where", "after_bytes": 10240, "prefix_sha256": "b" * 64}],
        "final": {"text": "Where are my keys?", "confidence": 0.999, "error": None},
        "model_version": "2025-04-17.21547", "recorded": "2026-10-05", "ledger_entry": 9,
        "source": "releases/v0.5.3/harness/runs/dg09/deepgram.json turns[0]",
    }


def test_a_cassette_is_checked_down_to_its_partials():
    good = {"format": "emet-cassette/1", "turns": [a_cassette_turn()]}
    assert bank_files.problems(good, "cassette") == []
    bad = copy.deepcopy(good)
    bad["turns"][0]["partials"][0]["after_bytes"] = 70000
    assert any("inside the bytes fed" in p for p in bank_files.problems(bad, "cassette"))
    keyed = copy.deepcopy(good)
    keyed["turns"][0]["request"]["query"]["api_key"] = "x"
    assert any("credential" in p for p in bank_files.problems(keyed, "cassette"))


# --------------------------------------------------------- finding audio


def test_find_audio_by_path_then_by_name_then_by_hash(tmp_path, make_wav, make_tone):
    root = build_bank(tmp_path, make_wav, make_tone)
    doc = bank_files.read(root / "manifests" / "t-0.1.json", "manifest")
    assert bank_files.find_audio(doc, root) == root / "../rec/t-0.1.wav"
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (tmp_path / "rec" / "t-0.1.wav").rename(moved / "renamed.wav")
    assert bank_files.find_audio(doc, root, [moved]) is None
    assert bank_files.find_audio(doc, root, [moved], by_hash=True) == moved / "renamed.wav"
    (moved / "renamed.wav").rename(moved / "t-0.1.wav")
    assert bank_files.find_audio(doc, root, [moved]) == moved / "t-0.1.wav"


def test_find_audio_never_returns_a_file_whose_hash_differs(tmp_path, make_wav, make_tone):
    root = build_bank(tmp_path, make_wav, make_tone)
    doc = bank_files.read(root / "manifests" / "t-0.1.json", "manifest")
    make_wav(tmp_path / "rec" / "t-0.1.wav", make_tone(2.0, hz=441.0))
    assert bank_files.find_audio(doc, root, [tmp_path / "rec"], by_hash=True) is None


def test_take_at_finds_the_slot_and_nothing_outside_the_slots():
    doc = a_manifest()
    assert bank_files.take_at(doc, 4000)["id"] == "t-0.1/01"
    assert bank_files.take_at(doc, 15999)["id"] == "t-0.1/01"
    assert bank_files.take_at(doc, 16000)["id"] == "t-0.1/02"
    assert bank_files.take_at(doc, 3999) is None and bank_files.take_at(doc, 28000) is None


# ------------------------------------------------------------- check.py


def test_check_passes_a_sound_bank(tmp_path, make_wav, make_tone, capsys):
    root = build_bank(tmp_path, make_wav, make_tone)
    assert check.main([str(root)]) == 0
    out = capsys.readouterr().out
    assert "t-0.1: ok, 2 take(s)" in out and "total: 1 of 1 recording(s) sound" in out


@pytest.mark.parametrize("breakage, expect", [
    ("audio changed", "no file with sha256"),
    ("audio gone", "no file with sha256"),
    ("consent gone", "no consent file"),
    ("consent empty", "is empty"),
    ("room unknown", "room 'kitchen' is not in bank.json"),
    ("id not file name", "is not the file's name"),
    ("header differs", "frames is 32000 in the file and 31999 in the manifest"),
])
def test_check_fails_a_broken_bank(tmp_path, make_wav, make_tone, capsys, breakage, expect):
    root = build_bank(tmp_path, make_wav, make_tone)
    mpath = root / "manifests" / "t-0.1.json"
    doc = bank_files.read(mpath, "manifest")
    if breakage == "audio changed":
        make_wav(tmp_path / "rec" / "t-0.1.wav", make_tone(2.0, hz=441.0))
    elif breakage == "audio gone":
        (tmp_path / "rec" / "t-0.1.wav").unlink()
    elif breakage == "consent gone":
        (root / "consent" / "s01.txt").unlink()
    elif breakage == "consent empty":
        (root / "consent" / "s01.txt").write_text("\n", encoding="utf-8")
    elif breakage == "room unknown":
        doc["room"] = "kitchen"
        bank_files.write(mpath, doc, "manifest")
    elif breakage == "id not file name":
        mpath.rename(root / "manifests" / "other.json")
        bank = bank_files.read(root / "bank.json", "bank")
        bank["files"] = ["manifests/other.json"]
        bank_files.write(root / "bank.json", bank, "bank")
    elif breakage == "header differs":
        doc["frames"], doc["duration_s"] = 31999, 31999 / 16000
        bank_files.write(mpath, doc, "manifest")
    assert check.main([str(root)]) == 1
    assert expect in capsys.readouterr().out


def test_check_finds_a_renamed_copy_only_by_hash(tmp_path, make_wav, make_tone, capsys):
    root = build_bank(tmp_path, make_wav, make_tone)
    away = tmp_path / "pi-home"
    away.mkdir()
    (tmp_path / "rec" / "t-0.1.wav").rename(away / "one-breath.wav")
    assert check.main([str(root), "--search", str(away)]) == 1
    assert check.main([str(root), "--search", str(away), "--by-hash"]) == 0
    assert "found at" in capsys.readouterr().out


# ------------------------------------------------------------- bank v0


def test_bank_v0_is_the_thirteen_recordings_as_the_records_give_them(real_bank):
    """Bank v0's figures (verification-design.md 5.1): 13 files, 193
    phrase slots plus 5 minutes of talk, 30.3 minutes; and the six of
    2026-10-06 are the bytes recordings/pi-0.5.4.sha256 hashed on the Pi."""
    docs = [doc for _, doc in bank_files.manifests(real_bank)]
    assert len(docs) == 13
    takes = [t for d in docs for t in d["takes"]]
    talk = [t for t in takes if t["kind"] == "talk"]
    assert len(takes) - len(talk) == 193 and len(talk) == 1
    assert sum(d["frames"] for d in docs if d["kind"] == "talk") == 300 * 16000
    assert round(sum(d["duration_s"] for d in docs) / 60, 1) == 30.3
    assert sum(1 for t in takes if t["kind"] == "decoy") == 9
    assert sum(1 for d in docs if d["distance"] == "about 3 m") == 3
    hashes = real_bank.parent / "recordings" / "pi-0.5.4.sha256"
    if hashes.is_file():
        pi = dict(reversed(line.split()) for line in hashes.read_text(encoding="utf-8").splitlines() if line.strip())
        mine = {Path(d["path"]).name: d["sha256"] for d in docs}
        assert pi and all(mine[name] == sha for name, sha in pi.items())


def test_bank_v0_checks_clean_with_its_audio(real_bank, capsys):
    search = real_bank_search()
    args = [str(real_bank)] + [a for d in search for a in ("--search", str(d))]
    assert check.main(args) == 0, capsys.readouterr().out
