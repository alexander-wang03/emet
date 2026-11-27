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
import io
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
        self.nexts: list[tuple[int, int, object]] = []      # (slot, part, the take shown as next)
        self.counts: list[tuple[str, int, int, int]] = []   # (file, slot, n, pos)
        self.events: list[tuple[int, str]] = []             # (pos, what) in the order shown
        self.notes: list[str] = []
        self.on_show = on_show

    def waiting(self, f, upcoming, pos):
        self.events.append((pos, "waiting"))

    def countdown(self, f, slot, n, upcoming, pos):
        self.counts.append((f.id, slot, n, pos))
        self.events.append((pos, f"count {n} for {slot}"))

    def show(self, f, slot, entry, part, pos, upcoming=None):
        self.shows.append((f.id, slot, entry, part, pos))
        self.nexts.append((slot, part, upcoming))
        self.events.append((pos, f"show {slot}.{part}"))
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


def test_every_take_with_a_pause_has_8_seconds():
    """verification-design.md 5.1: "prompts every 6 s (8 s for pause
    takes)". The first sitting gave wake_pause 6 s and lost the text. Every
    plan says 8 s in its own file, for both kinds, beyond what is derived."""
    for plan in plans() + [record.load_plan(TRIAL)]:
        for f in plan.files:
            kinds = {t.kind for t in f.takes}
            if kinds & {"wake_pause", "pause"}:
                assert f.every_s == 8, f.id
            elif not kinds & record.LONG_KINDS:
                assert f.every_s == 6, f.id
    seen = set()
    for path in SITTINGS + [TRIAL]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for f in doc["files"]:
            kinds = {doc["phrases"][k]["kind"] for k in f["takes"]} & {"wake_pause", "pause"}
            if kinds:
                assert f.get("every_s") == 8, (path.name, f["id"])
                seen |= kinds
    assert seen == {"wake_pause", "pause"}


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


def test_the_trial_is_38_seconds():
    """Three takes, one a wake_pause, so every 8 s, and a slot kept for a retake."""
    plan = record.load_plan(TRIAL)
    assert plan.seconds == 38 and plan.files[0].every_s == 8


# ------------------------------------------------------------------ the plan rules

@pytest.mark.parametrize("edit, says", [
    (lambda d: d.update(lead_s=3), "at least 5 s"),
    (lambda d: d["files"][0].update(every_s=7), "every 6 s"),
    (lambda d: d["files"][0].update(takes=["a"] * 16), "at most 100 s"),
    (lambda d: d["files"][0].update(takes=["z"], every_s=6), "every 8 s"),
    (lambda d: (d["phrases"].update(w={"kind": "wake_pause", "text": "hey emet ... hi", "question": "hi"}),
                d["files"][0].update(takes=["w"], every_s=6)), "every 8 s"),
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
    assert slots == [(80000, 208000), (208000, 336000), (336000, 464000)]
    wav = bank / "audio" / "trial-30s-01-a-near.wav"
    assert doc["sha256"] == hashlib.sha256(wav.read_bytes()).hexdigest()
    assert doc["frames"] == 38 * 16000 and doc["duration_s"] == 38
    assert doc["lead_s"] == 5 and doc["prompt_every_s"] == 8


def test_the_recorder_has_a_fixed_duration_and_a_session_of_its_own(tmp_path):
    bank = make_bank(tmp_path)
    run(record.load_plan(TRIAL), bank)
    (proc,) = FakeProc.launched
    wav = str(bank / "audio" / "trial-30s-01-a-near.wav")
    assert proc.cmd == ["arecord", "-D", "plughw:3,0", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", "38", wav]
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
    assert takes[3]["slot"]["start"] == 464000
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
    """A cue would be in the recording: by default the screen writes no
    bell, and the only process the prompter starts is the recorder, once a
    file, so nothing ever plays on the Pi."""
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


def test_the_bell_rings_in_the_terminal_at_each_now_and_nowhere_else(tmp_path, capsys):
    """--bell writes BEL once at each NOW, just before that prompt's screen,
    to the SSH terminal; the recorder stays the one process started."""
    bank = make_bank(tmp_path)
    record.run_sitting(record.load_plan(TRIAL), bank, clock=record.VirtualClock(),
                       screen=record.Screen(bell=True, width=100, height=60),
                       keys=record.ScriptedKeys({("trial-30s-01-a-near", 1): "r"}), popen=FakeProc,
                       level=lambda d: None, today="2026-11-24")
    out = capsys.readouterr().out
    rings = out.split("\a")[1:]
    assert len(rings) == 4  # three takes and the retake, none for a count-in
    for r in rings:  # each bell comes just before a NOW screen
        head, instruction = r.split("\n\n", 2)[:2]
        assert head.startswith(record.Screen.CLEAR + "trial-30s-01-a-near   prompt") and instruction.startswith("NOW: ")
    assert [p.cmd[0] for p in FakeProc.launched] == ["arecord"]


def test_the_bell_and_its_warning_come_only_with_the_flag(tmp_path, capsys):
    code = record.main([str(TRIAL), "--dry-run", "--bank", str(tmp_path / "dry")])
    plain = capsys.readouterr().out
    code_bell = record.main([str(TRIAL), "--dry-run", "--bank", str(tmp_path / "dry2"), "--bell"])
    rung = capsys.readouterr().out
    assert code == code_bell == 0
    assert "(bell)" not in plain and "earbuds or headphones" not in plain
    assert rung.count("(bell)") == 3 and "earbuds or headphones on the laptop" in rung


@pytest.mark.parametrize("cued", [False, True])
def test_each_prompt_is_counted_in_and_shows_the_next_take(tmp_path, cued):
    """3, 2, 1 a second apart before each prompt, the prompt at its own
    sample, and the take after it read ahead; a retake marked in slot 2 is
    counted in for the kept slot and read ahead from slot 3. The cue file
    changes nothing the screen is given."""
    bank = make_bank(tmp_path)
    inner = ListScreen()
    screen = record.CueScreen(inner, tmp_path / "cues.txt") if cued else inner
    run(record.load_plan(TRIAL), bank, screen=screen, keys=record.ScriptedKeys({("trial-30s-01-a-near", 1): "r"}))
    s = 16000
    assert inner.counts == [("trial-30s-01-a-near", k, n, (5 + 8 * k - n) * s) for k in range(4) for n in (3, 2, 1)]
    assert [pos for _f, _k, _e, part, pos in inner.shows if part == 1] == [5 * s, 13 * s, 21 * s, 29 * s]
    assert inner.events == sorted(inner.events, key=lambda e: e[0]) and inner.events[0] == (0, "waiting")
    texts = [None if e is None else e.take.text for _k, _p, e in inner.nexts]
    assert texts == ["hey emet ... how far away is the moon", "hey emma, are you still there",
                     "hey emet ... how far away is the moon", None]
    nums = [e.number for _f, _k, e, _p, _pos in inner.shows]
    assert nums == [1, 2, 3, 2]
    assert any(n.startswith("marked trial-30s-01-a-near/02") for n in inner.notes)


@pytest.mark.parametrize("cued", [False, True])
def test_the_take_stays_on_screen_with_the_count_in_below_it(capsys, tmp_path, cued):
    """From each NOW to the next, and from the last to the end, that take's
    whole text stays on screen; the count-in is a line under it, never in
    its place, and "next:" is under that (the first sitting, 2026-11-25:
    the text of the wake_pause takes went too soon). The lead keeps its own
    line through its count-in. The cue file changes nothing on screen."""
    bank = make_bank(tmp_path)
    screen = record.Screen(width=100, height=60)
    if cued:
        screen = record.CueScreen(screen, tmp_path / "cues.txt")
    record.run_sitting(record.load_plan(TRIAL), bank, clock=record.VirtualClock(), screen=screen,
                       keys=record.NoKeys(), popen=FakeProc, level=lambda d: None, today="2026-11-24")
    screens = capsys.readouterr().out.split(record.Screen.CLEAR)[1:]
    nows = [i for i, sc in enumerate(screens) if "\n\nNOW: " in sc]
    takes = record.load_plan(TRIAL).files[0].takes
    assert len(nows) == 3
    rest_after = ["next (sheet 2): hey emet ... how far away is the moon",
                  "next (sheet 3): hey emma, are you still there", "next: nothing more in this file"]
    get_ready = ["the name, a short pause, then the sentence", "say only this. It must not wake the robot", None]
    for k, at in enumerate(nows):
        upto = nows[k + 1] if k + 1 < len(nows) else len(screens)
        text = "\n".join(record.big(takes[k].text, 100))
        assert f"prompt {k + 1} of 4 (sheet {k + 1})" in screens[at] and "NOW: " + record.INSTRUCTIONS[takes[k].kind] in screens[at]
        counts = []
        for sc in screens[at:upto]:
            if not sc.startswith(f"trial-30s-01-a-near   prompt {k + 1} of 4"):
                break  # the kept slot's screen, or the file's closing notes
            assert text in sc, (k, sc[:200])
            lines = sc.partition(text)[2].strip("\n").splitlines()
            if lines[0].startswith("next prompt in"):
                counts.append(lines.pop(0))
            assert lines[0] == rest_after[k]
        want = [] if get_ready[k] is None else [f"next prompt in {n} s: {get_ready[k]}" for n in (3, 2, 1)]
        assert counts == want, k
    lead = screens[:nows[0]]
    assert [sc.count("next prompt in") for sc in lead] == [0, 1, 1, 1]
    assert all("the room first: the first prompt comes at 5 s" in sc for sc in lead)
    assert all("next (sheet 1): hey emet, what time is it" in sc for sc in lead)


def test_the_cue_file_has_a_line_at_each_count_and_each_now(tmp_path):
    cues = tmp_path / "logs" / "cues.txt"
    code = record.main([str(TRIAL), "--dry-run", "--bank", str(tmp_path / "dry"), "--cues", str(cues),
                        "--dry-keys", "trial-30s-01-a-near:2:r"])
    assert code == 0
    lines = cues.read_text(encoding="utf-8").splitlines()
    want = []
    for k in range(4):
        want += [f"count {n} trial-30s-01-a-near {k + 1} {(5 + 8 * k - n) * 16000}" for n in (3, 2, 1)]
        want.append(f"now trial-30s-01-a-near {k + 1} {(5 + 8 * k) * 16000}")
    assert [ln.rsplit(" due=", 1)[0] for ln in lines] == want
    dues = [float(ln.rsplit(" due=", 1)[1]) for ln in lines]
    assert dues == sorted(dues)


def test_each_cue_carries_the_time_it_was_due(tmp_path):
    clock = iter([100.0, 101.0, 102.0, 103.0])
    screen = record.CueScreen(ListScreen(), tmp_path / "cues.txt", wall=lambda: next(clock))
    f = record.load_plan(TRIAL).files[0]
    entry = record.Entry(f.takes[0], number=1)
    for n in (3, 2, 1):
        screen.countdown(f, 0, n, entry, 80000 - n * 16000)
    screen.show(f, 0, entry, 1, 80000)
    assert (tmp_path / "cues.txt").read_text(encoding="utf-8").splitlines() == [
        "count 3 trial-30s-01-a-near 1 32000 due=100.000", "count 2 trial-30s-01-a-near 1 48000 due=101.000",
        "count 1 trial-30s-01-a-near 1 64000 due=102.000", "now trial-30s-01-a-near 1 80000 due=103.000"]


def test_a_pause_take_cues_one_now_for_its_three_screens(tmp_path):
    """The NOW cue of a pause take comes at its prompt, never at HOLD or at
    the rest of its sentence."""
    bank = make_bank(tmp_path)
    cues = tmp_path / "cues.txt"
    screen = record.CueScreen(ListScreen(), cues, wall=lambda: 0.0)
    run(record.parse_plan(minimal_plan(["z"])), bank, screen=screen)
    assert cues.read_text(encoding="utf-8").splitlines() == [
        "count 3 t-01 1 32000 due=0.000", "count 2 t-01 1 48000 due=0.000", "count 1 t-01 1 64000 due=0.000",
        "now t-01 1 80000 due=0.000"]


def test_a_cue_that_cannot_be_written_never_stops_the_recording(tmp_path):
    bank = make_bank(tmp_path)
    blocked = tmp_path / "cues-dir"
    blocked.mkdir()
    screen = record.CueScreen(ListScreen(), blocked)  # a directory: every write fails
    res = record.run_sitting(record.load_plan(TRIAL), bank, clock=record.VirtualClock(), screen=screen,
                             keys=record.NoKeys(), popen=FakeProc, level=lambda d: None, today="2026-11-25")
    assert not res.problems and manifest(bank, "trial-30s-01-a-near")["sha256"]


def screens_of(plan, width, height):
    """Every screen the prompter draws for a plan's takes, as text."""
    out = io.StringIO()
    screen = record.Screen(out=out, width=width, height=height)
    for f in plan.files:
        entries = [record.Entry(t, number=f.first_number + i) for i, t in enumerate(f.takes)]
        screen.waiting(f, entries[0], 0)
        for k, e in enumerate(entries):
            nxt = entries[k + 1] if k + 1 < len(entries) else None
            for n in (3, 2, 1):
                screen.countdown(f, k, n, e, 0)
            for part in ((1, 2, 3) if e.take.kind == "pause" else (1,)):
                screen.show(f, k, e, part, 0, upcoming=nxt)
                yield f, e, part, out.getvalue().split(record.Screen.CLEAR)[-1]
                for n in (3, 2, 1):
                    screen.countdown(f, k + 1, n, nxt, 0)
                    yield f, e, part, out.getvalue().split(record.Screen.CLEAR)[-1]


@pytest.mark.parametrize("width, height", [(100, 30), (80, 24)])
def test_every_screen_fits_the_terminal_with_its_head_and_first_words(width, height):
    """At 100 by 30, 354 of 1035 count-in screens were taller than the
    terminal (the review of 2026-11-25). Now every screen fits, and its
    first lines are the head, the instruction and the take's first words."""
    count = 0
    for plan in plans() + [record.load_plan(TRIAL)]:
        for f, e, part, sc in screens_of(plan, width, height):
            count += 1
            lines = sc.rstrip("\n").split("\n")
            assert record.screen_rows(lines, width) <= height - 1, (f.id, len(lines))
            real = [ln for ln in lines if ln]
            assert real[0].startswith(f.id + "   prompt") and real[1].removeprefix("NOW: ").startswith(record.INSTRUCTIONS[e.take.kind][:10])
            words = record.prompt_words(e.take, part)
            first = record.big(words.split()[0], width)[0]
            assert real[2] == words[:len(real[2])] or real[2].startswith(first), (f.id, real[2][:60])
    assert count > 1000


def test_a_short_terminal_drops_spacing_then_letters_never_the_head():
    plan = record.load_plan(SITTINGS[0])
    tall = [sc for _f, _e, _p, sc in screens_of(plan, 100, 60)]
    short = [sc for _f, _e, _p, sc in screens_of(plan, 100, 30)]
    tiny = [sc for _f, _e, _p, sc in screens_of(plan, 100, 12)]
    assert any(record.KEYS_LINE in sc for sc in tall) and any(record.KEYS_LINE not in sc for sc in short)
    assert any("█" in sc for sc in short) and any("█" not in sc for sc in tiny)
    assert all(record.screen_rows(sc.rstrip("\n").split("\n"), 100) <= 11 for sc in tiny)
    assert all(sc.split("\n")[0].startswith("v1s1-") for sc in tiny)


def test_a_screen_too_tall_even_in_plain_words_is_cut_at_the_bottom():
    """A paragraph on a terminal six rows high: the head, the instruction and
    the paragraph's first words stay; what does not fit goes from the end."""
    plan = record.load_plan(SITTINGS[1])
    f = next(f for f in plan.files if f.takes[0].kind == "long")
    out = io.StringIO()
    screen = record.Screen(out=out, width=100, height=6)
    entry = record.Entry(f.takes[0], number=f.first_number)
    screen.show(f, 0, entry, 1, 0, upcoming=record.Entry(f.takes[1], number=f.first_number + 1))
    screen.countdown(f, 1, 3, record.Entry(f.takes[1]), 0)
    for sc in out.getvalue().split(record.Screen.CLEAR)[1:]:
        lines = sc.rstrip("\n").split("\n")
        assert record.screen_rows(lines, 100) <= 5
        assert lines[0].startswith(f.id + "   prompt") and lines[1].endswith("the name, then read the paragraph in one go")
        assert lines[2].startswith(f.takes[0].text[:40])


def test_a_real_sitting_writes_its_cues_where_home_is(fake_home, tmp_path, monkeypatch):
    """sit.py sends --cues '~/...' quoted, so record.py must expand the ~
    itself, and a real sitting's screen must be the one that writes cues."""
    bank = make_bank(tmp_path / "bank")
    seen: list[str] = []
    sitting_ready(monkeypatch, bank, seen)
    assert record.main([str(TRIAL), "--bank", str(bank), "--cues", "~/v/cues.txt"]) == 0
    screen = SITTING_KW[-1]["screen"]
    assert isinstance(screen, record.CueScreen) and screen.path == fake_home / "v" / "cues.txt"
    assert isinstance(screen.inner, record.Screen)


def test_a_dry_run_bank_named_with_a_tilde_is_in_home(fake_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert record.main([str(TRIAL), "--dry-run", "--bank", "~/drybank"]) == 0
    assert (fake_home / "drybank" / "manifests" / "trial-30s-01-a-near.json").is_file()
    assert not (tmp_path / "~").exists()


def test_again_never_takes_another_plan_file_for_a_recording_of_it(tmp_path):
    """A plan whose files are t-01 and t-01-2: --again t-01 replaces t-01,
    never the other file, and records t-01-3."""
    phrases = {"a": {"kind": "one_breath", "text": "hey emet, hi", "question": "hi", "first_sound": "hh"}}
    doc = minimal_plan(["a"], phrases=phrases)
    doc["files"].append({"id": "t-01-2", "at": "p", "takes": ["a"]})
    plan = record.parse_plan(doc)
    bank = make_bank(tmp_path)
    run(plan, bank)
    record.run_again(plan, bank, "t-01", clock=record.VirtualClock(), screen=ListScreen(), keys=record.NoKeys(),
                     popen=FakeProc, level=lambda d: None, today="2026-11-25")
    assert manifest(bank, "t-01-3")["made_with"].endswith("; again for t-01")
    assert all(t["exclude"] for t in manifest(bank, "t-01")["takes"])
    assert all(t["exclude"] is None for t in manifest(bank, "t-01-2")["takes"])
    new, replaced = record.plan_again(plan, bank, "t-01")
    assert (new.id, replaced) == ("t-01-4", "t-01-3")


def test_again_never_takes_a_file_no_again_made_for_a_recording_of_it(tmp_path):
    """A manifest named t-01-2 that --again did not make (another plan's, or
    one copied in) is passed over: the replaced recording is still t-01."""
    phrases = {"a": {"kind": "one_breath", "text": "hey emet, hi", "question": "hi", "first_sound": "hh"}}
    plan = record.parse_plan(minimal_plan(["a"], phrases=phrases))
    bank = make_bank(tmp_path)
    run(plan, bank)
    other = manifest(bank, "t-01")
    other.update(id="t-01-2", path="audio/t-01-2.wav", made_with="tools/bank/record.py, plan another")
    for take in other["takes"]:
        take["id"] = take["id"].replace("t-01/", "t-01-2/")
    (bank / "manifests" / "t-01-2.json").write_text(json.dumps(other), encoding="utf-8")
    new, replaced = record.plan_again(plan, bank, "t-01")
    assert (new.id, replaced) == ("t-01-3", "t-01")


def test_the_sheet_numbers_the_takes_through_the_sitting(capsys):
    plan = record.load_plan(SITTINGS[0])
    assert record.main([str(SITTINGS[0]), "--check"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln[:6].strip().isdigit()]
    assert [int(ln.split()[0]) for ln in lines] == list(range(1, 181))
    second = plan.files[1]
    assert second.first_number == len(plan.files[0].takes) + 1
    assert lines[second.first_number - 1].split(None, 1)[1] == record.manifest_text(second.takes[0])


# ------------------------------------------------------------------ --again

def recorded_trial(tmp_path) -> tuple[Path, object]:
    bank = make_bank(tmp_path)
    plan = record.load_plan(TRIAL)
    run(plan, bank)
    return bank, plan


def test_again_records_the_file_under_the_next_id_and_excludes_the_old_takes(tmp_path):
    bank, plan = recorded_trial(tmp_path)
    old_wav = (bank / "audio" / "trial-30s-01-a-near.wav").read_bytes()
    res = record.run_again(plan, bank, "trial-30s-01-a-near", clock=record.VirtualClock(), screen=ListScreen(),
                           keys=record.NoKeys(), popen=FakeProc, level=lambda d: None, today="2026-11-24")
    assert [r.id for r in res.files] == ["trial-30s-01-a-near-2"] and not res.problems
    new = manifest(bank, "trial-30s-01-a-near-2")
    assert new["made_with"] == "tools/bank/record.py, plan trial-30s; again for trial-30s-01-a-near"
    assert [t["text"] for t in new["takes"]] == [t.text for t in plan.files[0].takes]
    old = manifest(bank, "trial-30s-01-a-near")
    assert all(t["exclude"] == {"reason": "the maintainer asked for this file again; recorded again as "
                                          "trial-30s-01-a-near-2 (record.py --again)", "date": "2026-11-24"}
               for t in old["takes"])
    assert (bank / "audio" / "trial-30s-01-a-near.wav").read_bytes() == old_wav
    index = json.loads((bank / "bank.json").read_text(encoding="utf-8"))
    assert index["files"] == ["manifests/trial-30s-01-a-near.json", "manifests/trial-30s-01-a-near-2.json"]
    lines, good, bad = check.check(bank, [], False)
    assert (good, bad) == (2, 0), lines


def test_again_twice_replaces_the_latest_and_keeps_earlier_reasons(tmp_path):
    bank, plan = recorded_trial(tmp_path)
    kw = dict(clock=record.VirtualClock(), screen=ListScreen(), keys=record.NoKeys(), popen=FakeProc,
              level=lambda d: None)
    record.run_again(plan, bank, "trial-30s-01-a-near", today="2026-11-24", **kw)
    record.run_again(plan, bank, "trial-30s-01-a-near", today="2026-11-25", **kw)
    assert manifest(bank, "trial-30s-01-a-near-3")["made_with"].endswith("again for trial-30s-01-a-near-2")
    assert {t["exclude"]["date"] for t in manifest(bank, "trial-30s-01-a-near")["takes"]} == {"2026-11-24"}
    assert {t["exclude"]["date"] for t in manifest(bank, "trial-30s-01-a-near-2")["takes"]} == {"2026-11-25"}
    assert all(t["exclude"] is None for t in manifest(bank, "trial-30s-01-a-near-3")["takes"])


def test_again_keeps_the_reason_of_a_take_excluded_already(tmp_path):
    """A take marked for a retake keeps that reason; only the takes still
    open are excluded for the new recording."""
    bank = make_bank(tmp_path)
    plan = record.load_plan(TRIAL)
    run(plan, bank, keys=record.ScriptedKeys({("trial-30s-01-a-near", 1): "r"}))
    record.run_again(plan, bank, "trial-30s-01-a-near", clock=record.VirtualClock(), screen=ListScreen(),
                     keys=record.NoKeys(), popen=FakeProc, level=lambda d: None, today="2026-11-24")
    reasons = [t["exclude"]["reason"] for t in manifest(bank, "trial-30s-01-a-near")["takes"]]
    assert reasons[1] == "marked a retake at the prompter"
    assert [r.startswith("the maintainer asked for this file again") for r in reasons] == [True, False, True, True]


def test_again_leaves_the_old_takes_when_the_new_file_does_not_close(tmp_path):
    bank, plan = recorded_trial(tmp_path)

    class Hung(FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("arecord", timeout)

    screen = ListScreen()
    record.run_again(plan, bank, "trial-30s-01-a-near", clock=record.VirtualClock(), screen=screen,
                     keys=record.NoKeys(), popen=Hung, level=lambda d: None, today="2026-11-24")
    assert all(t["exclude"] is None for t in manifest(bank, "trial-30s-01-a-near")["takes"])
    assert "trial-30s-01-a-near is left as it was: trial-30s-01-a-near-2 did not close" in screen.notes


def test_again_refuses_a_file_the_plan_lacks_or_never_recorded(tmp_path, capsys):
    bank = make_bank(tmp_path)
    plan = record.load_plan(TRIAL)
    with pytest.raises(record.AgainError, match="is not a file of plan trial-30s"):
        record.plan_again(plan, bank, "no-such-file")
    with pytest.raises(record.AgainError, match="was never recorded"):
        record.plan_again(plan, bank, "trial-30s-01-a-near")
    assert record.main([str(TRIAL), "--dry-run", "--again", "trial-30s-01-a-near", "--bank", str(bank)]) == 2
    assert "was never recorded" in capsys.readouterr().err


def test_a_real_again_is_refused_before_anything_records(fake_home, tmp_path, monkeypatch, capsys):
    bank = make_bank(tmp_path / "bank")
    seen: list[str] = []
    sitting_ready(monkeypatch, bank, seen)
    assert record.main([str(TRIAL), "--bank", str(bank), "--again", "trial-30s-01-a-near"]) == 2
    assert seen == [] and "was never recorded" in capsys.readouterr().err
    assert not record.lock_path().exists()


def test_a_dry_run_of_again_says_what_it_would_do_and_changes_nothing(tmp_path, capsys):
    bank, _plan = recorded_trial(tmp_path)
    before = {p: p.read_bytes() for p in bank.rglob("*") if p.is_file()}
    capsys.readouterr()
    assert record.main([str(TRIAL), "--dry-run", "--again", "trial-30s-01-a-near", "--bank", str(bank)]) == 0
    out = capsys.readouterr().out
    assert "would record the plan's trial-30s-01-a-near again as trial-30s-01-a-near-2" in out
    assert "would exclude 3 take(s) of trial-30s-01-a-near" in out
    assert "     5.0 s     80000  01.1  one_breath    hey emet, what time is it" in out
    after = {p: p.read_bytes() for p in bank.rglob("*") if p.is_file()}
    assert after == before


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


def test_a_generated_voice_is_never_recorded(tmp_path):
    """A sitting records a person. A plan that names a generated voice
    (`g` and digits, FORMAT.md) is refused in words that say so, where the
    loader used to call the id a name; and a plan built without the loader
    is refused by the consent check, which used to raise KeyError on the
    voice record a generated bank lists in place of a consent file."""
    doc = minimal_plan(["a"])
    doc["speaker"] = "g01"
    with pytest.raises(record.PlanError, match="a generated voice is never recorded") as refused:
        record.parse_plan(doc)
    assert "never a name" not in str(refused.value)

    bank = make_bank(tmp_path / "generated")
    index = json.loads((bank / "bank.json").read_text(encoding="utf-8"))
    index["speakers"] = {"g01": {"voice": "voices/g01.json"}}
    (bank / "bank.json").write_text(json.dumps(index), encoding="utf-8")
    plan = record.load_plan(TRIAL)
    plan.speaker = "g01"
    problems = record.check_consent(plan, bank)
    assert len(problems) == 1 and "a generated voice is never recorded" in problems[0]
    assert "consent file" not in problems[0]


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
    assert manifest(bank, "trial-30s-01-a-near")["takes"][-1]["slot"]["end"] == 38 * 16000
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


SITTING_KW: list[dict] = []


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
        SITTING_KW.append(kw)
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
