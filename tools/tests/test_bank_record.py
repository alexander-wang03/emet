"""Tests for tools/bank/record.py, the bank's prompter, and for the plans in
tools/bank/plans/.

No test opens a device: the recorder is a stub that writes a silent wav of
the length it was asked for, and the clock is virtual, so a 100 s file runs
in milliseconds. The plans are checked against the bank v1 table of
research/references-2026-10-26/verification-design.md 5.1, copied below,
so a plan edited away from the design fails here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

BANK_TOOLS = Path(__file__).resolve().parents[1] / "bank"
sys.path.insert(0, str(BANK_TOOLS))

import check  # noqa: E402
import record  # noqa: E402

PLANS = BANK_TOOLS / "plans"
TRIAL = PLANS / "trial-30s.json"
SITTINGS = [PLANS / "v1-sitting-1.json", PLANS / "v1-sitting-2.json"]


# ------------------------------------------------------------------ stubs


class FakeProc:
    """A recorder that writes a silent wav of its -d at once and records
    every signal anyone sends it."""

    launched: list["FakeProc"] = []

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.signals: list[object] = []
        self.waited = False
        seconds = int(self.cmd[self.cmd.index("-d") + 1])
        with wave.open(self.cmd[-1], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(bytes(2 * 16000 * seconds))
        FakeProc.launched.append(self)

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def poll(self):
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


class ListScreen:
    def __init__(self, on_show=None):
        self.shows: list[tuple[str, int, object, int, int]] = []
        self.notes: list[str] = []
        self.on_show = on_show

    def show(self, f, slot, entry, part, pos):
        self.shows.append((f.id, slot, entry, part, pos))
        if self.on_show:
            self.on_show(f, slot, entry, part, pos)

    def note(self, text):
        self.notes.append(text)


class InterruptAt(record.ScriptedKeys):
    """Ctrl-C while a given slot is on screen."""

    def __init__(self, at):
        super().__init__({})
        self.at = at

    def poll(self, file_id, slot):
        if slot == self.at:
            raise KeyboardInterrupt
        return None


@pytest.fixture(autouse=True)
def _fresh():
    FakeProc.launched = []


def make_bank(root: Path, rooms=("room-a", "room-b"), consent="test consent, 2026-10-27\n") -> Path:
    (root / "consent").mkdir(parents=True)
    (root / "consent" / "s01.txt").write_text(consent, encoding="utf-8")
    (root / "bank.json").write_text(json.dumps({
        "format": "emet-bank/1", "version": "2026-10-01", "files": [],
        "rooms": {r: {"description": "a test room"} for r in rooms},
        "speakers": {"s01": {"consent": "consent/s01.txt"}},
    }), encoding="utf-8")
    return root


def run(plan, bank, keys=None, screen=None, **kw):
    return record.run_sitting(plan, bank, clock=record.VirtualClock(), screen=screen or ListScreen(),
                              keys=keys or record.NoKeys(), popen=FakeProc, level=lambda d: None,
                              today="2026-10-27", **kw)


def manifest(bank: Path, fid: str) -> dict:
    return json.loads((bank / "manifests" / f"{fid}.json").read_text(encoding="utf-8"))


def minimal_plan(takes, every_s=None, phrases=None, **file_kw):
    doc = {
        "format": "emet-bank-plan/1", "id": "t", "speaker": "s01", "lead_s": 5, "retake_slots": 1,
        "positions": {"p": {"room": "room-a", "distance": "near"}},
        "phrases": phrases or {
            "a": {"kind": "one_breath", "text": "hey emet, what time is it", "question": "what time is it", "first_sound": "w"},
            "z": {"kind": "pause", "text": "hey emet, I want to ... go to the shops", "question": "I want to go to the shops",
                  "first_sound": "ay", "pause_s": 1.5},
        },
        "files": [dict({"id": "t-01", "at": "p", "takes": takes}, **file_kw)],
    }
    if every_s is not None:
        doc["files"][0]["every_s"] = every_s
    return doc


# ------------------------------------------------------------------ the plans against the design's table

#: verification-design.md 5.1, the bank v1 table, maintainer only: takes by
#: class and place. "Others" are 10 each of wake and one breath at 3 m
#: facing away, near with a TV, about 5 m, room B near and room B 3 m.
TABLE = {
    ("wake_pause", "a-near"): 20, ("wake_pause", "a-3m"): 15,
    ("one_breath", "a-near"): 80 + 11, ("one_breath", "a-3m"): 20,
    ("number", "a-near"): 24, ("number", "a-3m"): 6,
    ("short", "a-near"): 24, ("short", "a-3m"): 8,
    ("decoy", "a-near"): 18, ("decoy", "a-3m"): 9,
    ("pause", "a-near"): 10,
    ("long", "a-near"): 2,
}
OTHERS = ("a-3m-away", "a-near-tv", "a-5m", "b-near", "b-3m")
for _place in OTHERS:
    TABLE[("wake_pause", _place)] = 10
    TABLE[("one_breath", _place)] = 10
#: Seconds of file for talk and room tone, by place, across both sittings.
#: Room tone is recorded once a room in each sitting, so either stands alone.
TALK_S = {"a-near": 300, "a-3m": 120, "b-near": 120, "a-near-tv": 120}
#: Every English onset the table names, as ARPAbet the aligner prints.
ONSETS = {"ih", "aa", "ow", "eh", "ah", "hh", "f", "v", "th", "dh", "s", "z", "sh", "ch", "jh",
          "p", "b", "t", "d", "k", "g", "m", "n", "l", "r", "w", "y"}
WEAK = {"hh", "f", "th", "s"}


def plans():
    return [record.load_plan(p) for p in SITTINGS]


def test_the_two_sittings_hold_the_bank_v1_table():
    got: dict[tuple[str, str], int] = {}
    talk: dict[str, int] = {}
    for plan in plans():
        for f in plan.files:
            for t in f.takes:
                if t.kind == "talk":
                    talk[f.at] = talk.get(f.at, 0) + f.seconds
                elif t.kind not in ("room_tone", "room_response"):
                    got[(t.kind, f.at)] = got.get((t.kind, f.at), 0) + 1
    assert got == TABLE
    assert talk == TALK_S


def test_no_second_voice_without_consent():
    """D7: the second voice's class waits for written consent."""
    for plan in plans() + [record.load_plan(TRIAL)]:
        assert plan.speaker == "s01"


def test_one_breath_has_every_onset_and_the_weak_ones_three_times_near():
    per_text: dict[str, int] = {}
    sounds = set()
    for plan in plans():
        for f in plan.files:
            for t in f.takes:
                if t.kind == "one_breath" and f.at == "a-near":
                    per_text[t.text] = per_text.get(t.text, 0) + 1
                    sounds.add(t.first_sound)
    assert sounds == ONSETS
    assert len(per_text) == 40
    phrase = {t.text: t.first_sound for plan in plans() for f in plan.files for t in f.takes}
    for text, n in per_text.items():
        assert n == (3 if phrase[text] in WEAK else 2), text


def test_the_3m_one_breath_says_the_weak_sounds_first():
    files = [f for f in plans()[0].files if f.at == "a-3m" and f.takes[0].kind == "one_breath"]
    sounds = [t.first_sound for f in files for t in f.takes]
    n_weak = sum(1 for s in sounds if s in WEAK)
    assert len(files) == 2 and n_weak == 11 and all(s in WEAK for s in sounds[:n_weak])


def test_each_sitting_stands_alone():
    """Each room a sitting records in has its room tone in that sitting, so
    neither sitting needs the other."""
    for plan in plans():
        tone_rooms = {f.where["room"] for f in plan.files if f.kind == "room_tone"}
        speech = [f for f in plan.files if f.takes[0].kind != "room_tone"]
        assert {f.where["room"] for f in speech} <= tone_rooms, plan.id


def test_no_plan_asks_for_a_room_response():
    """The microphone's automatic gain control is on (amixer -c 3, read
    2026-10-28), and a swept-sine response assumes a gain that holds still
    through the sweep, so a sitting holds no take the robot cannot measure
    as asked."""
    for plan in plans() + [record.load_plan(TRIAL)]:
        assert all(t.kind != "room_response" for f in plan.files for t in f.takes), plan.id


def test_each_sitting_fits_in_about_45_minutes():
    """About 29 min of audio each; Enter between files and the moves bring a
    sitting to about 45 (D5)."""
    for plan in plans():
        assert 25 * 60 <= plan.seconds <= 35 * 60, (plan.id, plan.seconds)
        assert all(f.seconds <= 100 for f in plan.files)


def test_the_trial_is_30_seconds():
    assert record.load_plan(TRIAL).seconds == 30


# ------------------------------------------------------------------ the plan rules

@pytest.mark.parametrize("edit, says", [
    (lambda d: d.update(lead_s=3), "at least 5 s"),
    (lambda d: d["files"][0].update(every_s=7), "every 6 s"),
    (lambda d: d["files"][0].update(takes=["a"] * 16), "at most 100 s"),
    (lambda d: d["files"][0].update(takes=["z"], every_s=6), "every 8 s"),
    (lambda d: d.update(speaker="alex"), "never a name"),
    (lambda d: d["files"][0].update(takes=["nope"]), "not in the plan's phrases"),
    (lambda d: d["files"].append(dict(d["files"][0])), "two files have this id"),
    (lambda d: d["phrases"]["a"].update(kind="question"), "is not one of"),
    (lambda d: d["files"][0].update(lead_s=5.5), "whole number of seconds"),
])
def test_a_plan_that_breaks_a_rule_is_refused(edit, says):
    doc = minimal_plan(["a"])
    edit(doc)
    with pytest.raises(record.PlanError, match=says):
        record.parse_plan(doc)


def test_a_long_take_names_its_slot_and_sits_alone():
    phrases = {"tone": {"kind": "room_tone", "text": "nobody speaks"},
               "a": {"kind": "one_breath", "text": "hey emet, hi"}}
    with pytest.raises(record.PlanError, match="names its every_s"):
        record.parse_plan(minimal_plan(["tone"], phrases=phrases))
    with pytest.raises(record.PlanError, match="shares a file"):
        record.parse_plan(minimal_plan(["tone", "a"], every_s=30, phrases=phrases))
    plan = record.parse_plan(minimal_plan(["tone"], every_s=60, phrases=phrases, kind="room_tone"))
    assert plan.files[0].seconds == 66 and plan.files[0].retake_slots == 0


# ------------------------------------------------------------------ recording against the stub

def test_prompts_are_timed_from_the_first_sample_and_the_file_is_hashed(tmp_path):
    bank = make_bank(tmp_path)
    res = run(record.load_plan(TRIAL), bank)
    assert not res.problems and len(res.files) == 1
    doc = manifest(bank, "trial-30s-01-a-near")
    slots = [(t["slot"]["start"], t["slot"]["end"]) for t in doc["takes"]]
    assert slots == [(80000, 176000), (176000, 272000), (272000, 368000)]
    wav = bank / "audio" / "trial-30s-01-a-near.wav"
    assert doc["sha256"] == hashlib.sha256(wav.read_bytes()).hexdigest()
    assert doc["frames"] == 30 * 16000 and doc["duration_s"] == 30
    assert doc["lead_s"] == 5 and doc["prompt_every_s"] == 6


def test_the_recorder_has_a_fixed_duration_and_a_session_of_its_own(tmp_path):
    bank = make_bank(tmp_path)
    run(record.load_plan(TRIAL), bank)
    (proc,) = FakeProc.launched
    wav = str(bank / "audio" / "trial-30s-01-a-near.wav")
    assert proc.cmd == ["arecord", "-D", "plughw:3,0", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", "30", wav]
    if os.name == "posix":
        assert proc.kwargs.get("start_new_session") is True
    else:
        assert proc.kwargs.get("creationflags", 0) & subprocess.CREATE_NEW_PROCESS_GROUP


def test_ctrl_c_stops_the_prompts_and_waits_for_the_recorder_unsignalled(tmp_path):
    bank = make_bank(tmp_path)
    screen = ListScreen()
    res = run(record.load_plan(TRIAL), bank, keys=InterruptAt(1), screen=screen)
    (proc,) = FakeProc.launched
    assert res.stopped and proc.waited and proc.signals == []
    doc = manifest(bank, "trial-30s-01-a-near")
    assert len(doc["takes"]) == 2 and doc["sha256"]
    assert res.files[0].unprompted == 1
    assert check.check(bank, [], False)[2] == 0


def test_the_manifest_is_on_disk_before_each_prompt_shows(tmp_path):
    bank = make_bank(tmp_path)
    seen = []

    def look(f, slot, entry, part, pos):
        doc = manifest(bank, f.id)
        seen.append((len(doc["takes"]), doc["takes"][-1]["text"] if doc["takes"] else None))

    run(record.load_plan(TRIAL), bank, screen=ListScreen(look))
    assert seen[:3] == [(1, "hey emet, what time is it"), (2, "hey emet ... how far away is the moon"),
                        (3, "hey emma, are you still there")]


def test_r_marks_the_take_on_screen_and_asks_it_again_at_the_end(tmp_path):
    bank = make_bank(tmp_path)
    run(record.load_plan(TRIAL), bank, keys=record.ScriptedKeys({("trial-30s-01-a-near", 1): "r"}))
    takes = manifest(bank, "trial-30s-01-a-near")["takes"]
    assert takes[1]["exclude"] == {"reason": "marked a retake at the prompter", "date": "2026-10-27"}
    assert takes[3]["text"] == takes[1]["text"] and takes[3]["retake_of"] == takes[1]["id"]
    assert takes[3]["slot"]["start"] == 368000
    assert check.check(bank, [], False)[2] == 0


def test_p_marks_the_take_before(tmp_path):
    bank = make_bank(tmp_path)
    run(record.load_plan(TRIAL), bank, keys=record.ScriptedKeys({("trial-30s-01-a-near", 2): "p"}))
    takes = manifest(bank, "trial-30s-01-a-near")["takes"]
    assert [t["exclude"] is not None for t in takes] == [False, True, False, False]
    assert takes[3]["retake_of"] == takes[1]["id"]


def test_retakes_past_the_kept_slot_go_to_a_file_at_the_same_place(tmp_path):
    bank = make_bank(tmp_path)
    keys = record.ScriptedKeys({("trial-30s-01-a-near", 0): "r", ("trial-30s-01-a-near", 1): "r"})
    res = run(record.load_plan(TRIAL), bank, keys=keys)
    assert [r.id for r in res.files] == ["trial-30s-01-a-near", "trial-30s-01-a-near-retake1"]
    again = manifest(bank, "trial-30s-01-a-near-retake1")
    first = manifest(bank, "trial-30s-01-a-near")
    assert again["position"] == first["position"] and again["distance"] == first["distance"]
    assert [t["text"] for t in again["takes"]] == ["hey emet ... how far away is the moon"]
    assert "says again trial-30s-01-a-near/02" in again["made_with"]
    lines, good, bad = check.check(bank, [], False)
    assert (good, bad) == (2, 0), lines
    index = json.loads((bank / "bank.json").read_text(encoding="utf-8"))
    assert index["files"] == ["manifests/trial-30s-01-a-near.json", "manifests/trial-30s-01-a-near-retake1.json"]
    assert index["version"] == "2026-10-27"


def test_a_pause_take_shows_hold_and_the_rest_at_its_planned_samples(tmp_path):
    bank = make_bank(tmp_path)
    plan = record.parse_plan(minimal_plan(["z"]))
    screen = ListScreen()
    run(plan, bank, screen=screen)
    parts = [(part, pos) for _f, slot, entry, part, pos in screen.shows if slot == 0]
    assert parts == [(1, 80000), (2, 80000 + 24000), (3, 80000 + 48000)]
    words = [record.prompt_words(entry.take, part) for _f, slot, entry, part, _p in screen.shows if slot == 0]
    assert words == ["hey emet, I want to", "HOLD", "go to the shops"]
    assert manifest(bank, "t-01")["takes"][0]["text"] == "hey emet, I want to [hold 1.5 s] go to the shops"
    assert manifest(bank, "t-01")["prompt_every_s"] == 8


def test_no_cue_is_played_or_rung(tmp_path, capsys):
    """A cue would be in the recording: the screen writes no bell, and the
    only process the prompter starts is the recorder, once a file."""
    bank = make_bank(tmp_path)
    plan = record.load_plan(TRIAL)
    rec_screen = record.Screen()
    record.run_sitting(plan, bank, clock=record.VirtualClock(), screen=rec_screen,
                       keys=record.ScriptedKeys({("trial-30s-01-a-near", 1): "r"}), popen=FakeProc,
                       level=lambda d: None, today="2026-10-27")
    out = capsys.readouterr().out
    assert "█" in out and "\a" not in out
    assert [p.cmd[0] for p in FakeProc.launched] == ["arecord"]
    source = (BANK_TOOLS / "record.py").read_text(encoding="utf-8")
    for player in ("aplay", "paplay", "pw-play", "speaker-test", "sounddevice", "\\a", "\\x07", "\\007"):
        assert player not in source, player


def test_the_bank_is_append_only(tmp_path):
    bank = make_bank(tmp_path)
    plan = record.load_plan(TRIAL)
    run(plan, bank)
    before = (bank / "audio" / "trial-30s-01-a-near.wav").read_bytes()
    res = run(plan, bank)
    assert res.skipped == ["trial-30s-01-a-near"] and len(FakeProc.launched) == 1
    assert (bank / "audio" / "trial-30s-01-a-near.wav").read_bytes() == before
    (bank / "manifests" / "trial-30s-01-a-near.json").unlink()
    res = run(plan, bank)
    assert res.problems and "append-only" in res.problems[0] and len(FakeProc.launched) == 1


def test_consent_is_checked_before_a_real_run(tmp_path):
    plan = record.load_plan(TRIAL)
    assert "does not exist" in record.check_consent(plan, tmp_path / "none")[0]
    bank = make_bank(tmp_path / "ok")
    assert record.check_consent(plan, bank) == []
    (bank / "consent" / "s01.txt").write_text("  \n", encoding="utf-8")
    assert "missing or empty" in record.check_consent(plan, bank)[0]
    other = make_bank(tmp_path / "other")
    doc = json.loads((other / "bank.json").read_text(encoding="utf-8"))
    doc["speakers"] = {"s02": {"consent": "consent/s01.txt"}}
    (other / "bank.json").write_text(json.dumps(doc), encoding="utf-8")
    assert "not listed" in record.check_consent(plan, other)[0]


def test_a_manifest_left_open_is_closed_from_its_file(tmp_path):
    bank = make_bank(tmp_path)

    class Hung(FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("arecord", timeout)

    res = record.run_sitting(record.load_plan(TRIAL), bank, clock=record.VirtualClock(), screen=ListScreen(),
                             keys=record.NoKeys(), popen=Hung, level=lambda d: None, today="2026-10-27")
    assert "left alone" in res.problems[0]
    path = bank / "manifests" / "trial-30s-01-a-near.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["sha256"] is None
    doc["takes"][-1]["slot"]["end"] = None  # as a prompter killed in its last slot leaves it
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert check.check(bank, [], False)[2] == 0  # not in the index yet
    assert record.close_open(bank, "trial-30s-01-a-near", "2026-10-27") == []
    assert manifest(bank, "trial-30s-01-a-near")["takes"][-1]["slot"]["end"] == 30 * 16000
    lines, good, bad = check.check(bank, [], False)
    assert (good, bad) == (1, 0), lines


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert record.lock_path() == home / "emet-ci" / "lock"
    return home


RUNNER_LOCK = "token 0123abcd\nby tools/body/run.py\nstarted 2026-10-28T01:00:00-07:00\n"


def test_the_lock_is_the_runners_made_once_and_freed_only_with_its_token(fake_home):
    lock = record.lock_path()
    now = record.datetime.datetime(2026, 10, 28, 1, 2, 3).astimezone()
    token, held = record.take_lock(lock, now)
    lines = lock.read_text(encoding="utf-8").splitlines()
    assert token and held == [] and lines == [f"token {token}", "by tools/bank/record.py", f"started {now.isoformat(timespec='seconds')}"]
    again, held = record.take_lock(lock, now)
    assert again is None and held == lines and lock.read_text(encoding="utf-8").splitlines() == lines
    assert record.release_lock(lock, "not-ours") is False and lock.exists()
    assert record.release_lock(lock, token) is True and not lock.exists()


def sitting_ready(monkeypatch, bank, seen):
    """main's real path with the terminal, arecord and the sitting stood in for."""

    class Keys:
        def __enter__(self):
            return record.NoKeys()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(record.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(record, "TerminalKeys", Keys)
    monkeypatch.setattr(record.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(record.signal, "signal", lambda *a: None)

    def sitting(plan, bank_, **kw):
        seen.append(record.lock_path().read_text(encoding="utf-8"))
        return record.SittingResult()

    monkeypatch.setattr(record, "run_sitting", sitting)


def test_a_sitting_holds_the_lock_and_frees_it(fake_home, tmp_path, monkeypatch):
    bank = make_bank(tmp_path / "bank")
    seen: list[str] = []
    sitting_ready(monkeypatch, bank, seen)
    assert record.main([str(TRIAL), "--bank", str(bank)]) == 0
    assert len(seen) == 1 and "by tools/bank/record.py" in seen[0]
    assert not record.lock_path().exists()


def test_a_sitting_refuses_while_the_runner_holds_the_lock(fake_home, tmp_path, monkeypatch, capsys):
    bank = make_bank(tmp_path / "bank")
    seen: list[str] = []
    sitting_ready(monkeypatch, bank, seen)
    lock = record.lock_path()
    lock.parent.mkdir(parents=True)
    lock.write_text(RUNNER_LOCK, encoding="utf-8")
    assert record.main([str(TRIAL), "--bank", str(bank)]) == 2
    assert seen == [] and "is held: token 0123abcd / by tools/body/run.py" in capsys.readouterr().err
    assert lock.read_text(encoding="utf-8") == RUNNER_LOCK


def test_a_stopped_sitting_still_frees_the_lock(fake_home, tmp_path, monkeypatch):
    bank = make_bank(tmp_path / "bank")

    def stopped(plan, bank_, **kw):
        assert record.lock_path().exists()
        raise KeyboardInterrupt

    sitting_ready(monkeypatch, bank, [])
    monkeypatch.setattr(record, "run_sitting", stopped)
    with pytest.raises(KeyboardInterrupt):
        record.main([str(TRIAL), "--bank", str(bank)])
    assert not record.lock_path().exists()


def test_a_dropped_session_while_waiting_leaves_the_file_to_close(tmp_path):
    bank = make_bank(tmp_path)

    class Dropped(FakeProc):
        def wait(self, timeout=None):
            raise record.Stopped()

    res = record.run_sitting(record.load_plan(TRIAL), bank, clock=record.VirtualClock(), screen=ListScreen(),
                             keys=record.NoKeys(), popen=Dropped, level=lambda d: None, today="2026-10-27")
    assert res.stopped and "--close trial-30s-01-a-near" in res.problems[0]
    assert FakeProc.launched[0].signals == []


def test_the_start_estimate_comes_down_to_the_earliest_write(tmp_path):
    wav = tmp_path / "x.wav"
    est = record.StartEstimate(wav, launched=10.0)
    assert est.t0 == 10.0
    wav.write_bytes(bytes(44 + 2 * 1600))      # 0.1 s on disk at 10.25: started by 10.15
    est.update(10.25)
    assert est.t0 == pytest.approx(10.15)
    wav.write_bytes(bytes(44 + 2 * 3200))      # 0.2 s on disk at 10.32: started by 10.12
    est.update(10.32)
    assert est.t0 == pytest.approx(10.12)
    wav.write_bytes(bytes(44 + 2 * 16000 * 5))  # all of it at once, as the stub writes
    est.update(10.40)
    assert est.t0 == 10.0                       # never before the launch
    assert est.position(10.5) == 8000


def test_the_dry_run_prints_the_schedule_and_writes_the_manifests(tmp_path, capsys):
    bank = tmp_path / "dry"
    code = record.main([str(TRIAL), "--dry-run", "--bank", str(bank), "--dry-keys", "trial-30s-01-a-near:2:r"])
    out = capsys.readouterr().out
    assert code == 0
    assert "     5.0 s     80000  01.1  one_breath    hey emet, what time is it" in out
    assert "(again: trial-30s-01-a-near/02)" in out
    doc = manifest(bank, "trial-30s-01-a-near")
    assert len(doc["takes"]) == 4 and doc["sha256"]


def test_big_text_wraps_at_words_and_draws_seven_rows():
    rows = record.big("hey emet, what time is it", 60)
    assert len(rows) == 7 * 3 + 2  # three lines of ten glyphs, a blank row between
    assert all(len(r) <= 60 for r in rows)
    assert record.big("~", 60) == [""] * 7
