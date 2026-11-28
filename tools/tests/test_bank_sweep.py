"""tools/bank/sweep.py, the sweep a phone plays for the room responses, and
the block of sitting 2's plan with the gain control off that plays it
(E13 (a); 0.5.7 ledger L39)."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import wave

import pytest

import record
import sweep
from conftest import BANK_TOOLS

PLANS = BANK_TOOLS / "plans"
SITTING_2 = PLANS / "v1-sitting-2.json"
#: The five talker positions of verification-design.md 5.1's room response.
TALKERS = ["a-3m", "a-5m", "a-near", "b-3m", "b-near"]


@pytest.fixture(scope="module")
def pcm():
    return sweep.samples()


def test_the_sweep_is_ten_seconds_with_half_a_second_of_silence_each_side(pcm):
    lead, tail = int(sweep.LEAD_S * sweep.RATE), int(sweep.TAIL_S * sweep.RATE)
    assert len(pcm) == int((sweep.LEAD_S + sweep.SWEEP_S + sweep.TAIL_S) * sweep.RATE) == 528000
    assert not any(pcm[:lead]) and not any(pcm[-tail:])
    peak = max(abs(x) for x in pcm) / 32767
    assert 20 * math.log10(peak) == pytest.approx(-6.0, abs=0.05)
    # Faded in and out, so neither end of the sweep is a click.
    body = pcm[lead:len(pcm) - tail]
    assert max(abs(x) for x in body[:48]) < 100 and max(abs(x) for x in body[-48:]) < 100


def test_its_frequency_rises_from_100_hz_to_8000_hz_at_a_constant_rate_in_octaves(pcm):
    """Counted by zero crossings in 50 ms windows, the frequency is `f1 * (f2 / f1) ** (t / T)`."""
    lead = int(sweep.LEAD_S * sweep.RATE)
    for t in (1.0, 3.0, 5.0, 7.0, 9.0):
        a = lead + int(t * sweep.RATE)
        w = pcm[a:a + int(0.05 * sweep.RATE)]
        crossings = sum(1 for x, y in zip(w, w[1:]) if (x < 0) != (y < 0))
        expected = sweep.F1_HZ * (sweep.F2_HZ / sweep.F1_HZ) ** ((t + 0.025) / sweep.SWEEP_S)
        assert crossings / 2 / 0.05 == pytest.approx(expected, rel=0.06), t


def test_the_file_and_its_parameters_are_written_and_the_same_each_time(tmp_path, capsys):
    out = tmp_path / "sweeps" / sweep.NAME
    assert sweep.main([str(out)]) == 0
    first = out.read_bytes()
    doc = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    with wave.open(str(out), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 48000, 528000)
    assert doc["sha256"] == hashlib.sha256(first).hexdigest()
    assert (doc["format"], doc["f1_hz"], doc["f2_hz"], doc["sweep_s"], doc["duration_s"]) == (
        "emet-sweep/1", 100.0, 8000.0, 10.0, 11.0)
    assert capsys.readouterr().out.startswith(f"wrote: {out}\n")
    assert sweep.main([str(out)]) == 0
    assert out.read_bytes() == first
    assert capsys.readouterr().out.startswith(f"there already, the same bytes: {out}\n")


def test_a_path_inside_the_repository_is_refused_and_nothing_is_written(tmp_path, monkeypatch, capsys):
    """Held against a repository of the test's own, so a broken rule writes
    into the temporary folder and never into the tree."""
    assert sweep.inside_repository(sweep.REPO / "tools" / "bank" / sweep.NAME)
    assert not sweep.inside_repository(sweep.REPO.parent / "voicebank" / "sweeps" / sweep.NAME)
    monkeypatch.setattr(sweep, "REPO", tmp_path / "repo")
    inside = tmp_path / "repo" / "tools" / "bank" / sweep.NAME
    assert sweep.main([str(inside)]) == 2
    assert not (tmp_path / "repo").exists()
    assert "is inside the repository; the sweep is a file for the phone and stays outside it" in capsys.readouterr().err


def test_a_sweep_already_there_is_never_replaced_and_a_name_must_end_wav(tmp_path, capsys):
    out = tmp_path / sweep.NAME
    out.write_bytes(b"the sweep an earlier sitting played")
    assert sweep.main([str(out)]) == 2
    assert out.read_bytes() == b"the sweep an earlier sitting played"
    assert "is there already and holds other bytes" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.tmp"))
    assert sweep.main([str(tmp_path / "sweep.mp3")]) == 2
    assert "does not end .wav" in capsys.readouterr().err


def test_it_runs_on_a_bare_python(tmp_path):
    done = subprocess.run([sys.executable, "-S", "-I", str(BANK_TOOLS / "sweep.py"), str(tmp_path / "s.wav")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "s.json").is_file()


# ------------------------------------------------------------ the block in sitting 2


def plan2() -> record.Plan:
    return record.load_plan(SITTING_2)


def test_the_block_with_the_gain_control_off_is_one_unbroken_stretch():
    files = plan2().files
    off = [i for i, f in enumerate(files) if f.id.endswith("-gain-off")]
    assert off == list(range(off[0], off[-1] + 1)) and len(off) == 9
    # It begins after the last file of room A recorded as the body runs, and
    # room B's files as the body runs come after it.
    assert files[off[0] - 1].where["room"] == "room-a" and files[off[-1] + 1].where["room"] == "room-b"


def test_the_room_responses_are_two_sweeps_at_each_talker_position_inside_the_block():
    responses = [f for f in plan2().files if any(t.kind == "room_response" for t in f.takes)]
    assert sorted(f.at for f in responses) == TALKERS
    assert all(f.id.endswith("-gain-off") for f in responses)
    assert all([t.kind for t in f.takes] == ["room_response", "room_response"] for f in responses)
    for other in ("v1-sitting-1.json", "trial-30s.json"):
        plan = record.load_plan(PLANS / other)
        assert all(t.kind != "room_response" for f in plan.files for t in f.takes), other


def test_each_slot_holds_the_whole_sweep_with_time_to_press_play():
    played = sweep.LEAD_S + sweep.SWEEP_S + sweep.TAIL_S
    for f in plan2().files:
        if f.takes[0].kind == "room_response":
            assert f.every_s >= played + 3, f.id


def test_the_block_says_where_the_gain_control_goes_off_and_on_and_names_the_sweep():
    files = [f for f in plan2().files if f.id.endswith("-gain-off")]
    assert "Before Enter, the agent turns the microphone's Auto Gain Control off" in files[0].say
    assert sweep.NAME in files[0].say
    assert "After it, the agent turns Auto Gain Control back on" in files[-1].say
    note = json.loads(SITTING_2.read_text(encoding="utf-8"))["note"]
    assert "tools/bank/sweep.py" in note and sweep.NAME in note


#: Each file of the block that holds speech, and the file recorded with the
#: gain control on that holds the same slots at the same place (AF2 stage 2).
TWINS = {"v1s2-17-a-3m-numbers-short-2-gain-off": "v1s2-10-a-3m-numbers-short-1",
         "v1s2-19-a-near-numbers-2-gain-off": "v1s2-02-a-near-numbers-1",
         "v1s2-20-a-near-short-2-gain-off": "v1s2-03-a-near-short-1",
         "v1s2-21-a-near-pauses-2-gain-off": "v1s2-04-a-near-pauses-1"}


def test_the_block_holds_the_same_slots_near_and_at_3m_as_files_with_the_gain_control_on():
    by_id = {f.id: f for f in plan2().files}
    speech = {fid for fid, f in by_id.items() if fid.endswith("-gain-off") and f.takes[0].kind != "room_response"}
    assert speech == set(TWINS)
    note = json.loads(SITTING_2.read_text(encoding="utf-8"))["note"]
    for off, on in TWINS.items():
        assert by_id[off].at == by_id[on].at and by_id[off].at in ("a-near", "a-3m")
        assert {t.phrase for t in by_id[off].takes} == {t.phrase for t in by_id[on].takes}, off
        assert not by_id[on].id.endswith("-gain-off")
        assert f"{on[:7]} and {off[:7]}" in note
        assert on[:7] in (by_id[off].say or "")
