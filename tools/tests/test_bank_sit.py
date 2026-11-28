"""Tests for tools/bank/sit.py, the laptop's side of a bank sitting.

Nothing here plays a sound or reaches the Pi: ssh is a stand-in that
records its commands, the cue connection blocks until it is stopped as
`tail -F` does, and every player is a list that records the tones. An
autouse fixture makes the real player fail any test that reaches it, and
the one test of the real player's call puts a stand-in winsound module in
its place.
"""

from __future__ import annotations

import io
import queue
import struct
import subprocess
import sys
import time
import types
import wave
from pathlib import Path

import pytest

BANK_TOOLS = Path(__file__).resolve().parents[1] / "bank"
sys.path.insert(0, str(BANK_TOOLS))

import sit  # noqa: E402  a bank tool, imported by its bare name from tools/bank/


@pytest.fixture(autouse=True)
def _no_sound(monkeypatch):
    def refuse():
        raise AssertionError("a test reached the real player")
    monkeypatch.setattr(sit, "winsound_player", refuse)


def frames(wav: bytes) -> tuple[int, list[int]]:
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    return rate, list(struct.unpack(f"<{len(raw) // 2}h", raw))


def crossings(x: list[int]) -> int:
    return sum(1 for a, b in zip(x, x[1:]) if (a < 0) != (b < 0))


# ------------------------------------------------------------------ the tones

def test_the_tones_are_quiet_short_and_faded_over_5_ms():
    rate, tick = frames(sit.SOUNDS["count"])
    _, now = frames(sit.SOUNDS["now"])
    assert rate == sit.TONE_RATE
    assert len(tick) == rate * 40 // 1000 and len(now) == rate * 160 // 1000
    assert max(map(abs, tick)) <= 0.06 * 32767 + 1 and max(map(abs, now)) <= 0.10 * 32767 + 1
    assert max(map(abs, tick)) > 0.05 * 32767  # quiet, and still there
    fade = rate * 5 // 1000
    for x in (tick, now):  # each end rises and falls over the whole 5 ms
        peak = max(map(abs, x))
        assert max(map(abs, x[:fade // 2])) < 0.5 * peak and max(map(abs, x[-(fade // 2):])) < 0.5 * peak
        assert max(map(abs, x[fade // 2:fade])) >= 0.5 * peak  # and is up to half by 5 ms, not later
    assert crossings(now) / len(now) > crossings(tick) / len(tick)  # NOW is the higher tone


def test_the_warning_is_two_low_tones_with_a_gap():
    rate, x = frames(sit.SOUNDS["warning"])
    one = rate * 120 // 1000
    gap = rate * 80 // 1000
    assert len(x) == 2 * one + gap
    assert max(map(abs, x[one:one + gap])) == 0 and max(map(abs, x[:one])) > 0 and max(map(abs, x[one + gap:])) > 0
    _, tick = frames(sit.SOUNDS["count"])
    assert crossings(x[:one]) / one < crossings(tick) / len(tick)


def test_the_real_player_plays_from_memory_and_waits(monkeypatch):
    """SND_MEMORY with SND_ASYNC raises RuntimeError (the winsound docs), and
    without SND_MEMORY the bytes are refused: the call must be exactly this."""
    monkeypatch.undo()  # the real winsound_player; the module below stands in for winsound
    calls = []
    fake = types.SimpleNamespace(SND_MEMORY=4, SND_ASYNC=1, SND_FILENAME=0x20000,
                                 PlaySound=lambda sound, flags: calls.append((sound, flags)))
    monkeypatch.setitem(sys.modules, "winsound", fake)
    sit.winsound_player()(sit.SOUNDS["now"])
    assert calls == [(sit.SOUNDS["now"], 4)]


def test_the_player_says_what_to_use_without_winsound(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "winsound", None)
    with pytest.raises(SystemExit, match="only Windows has"):
        sit.winsound_player()


def test_the_test_tone_plays_the_four_once_and_says_to_wear_earbuds():
    played, slept, out = [], [], io.StringIO()
    sit.test_tone(played.append, sleep=slept.append, out=out)
    assert played == [sit.SOUNDS["count"]] * 3 + [sit.SOUNDS["now"]] and slept == [1.0, 1.0, 1.0]
    assert "earbuds or headphones" in out.getvalue() and "in the recording" in out.getvalue()


# ------------------------------------------------------------------ the cues

class Tail:
    """A stand-in for `ssh ... tail -n0 -F`: its lines block until there is
    one, and it ends only when stopped, or early when told to."""

    def __init__(self, args, lines=(), end_early=False, code=255, **kwargs):
        self.args, self.kwargs = list(args), kwargs
        self.q: queue.Queue = queue.Queue()
        for ln in lines:
            self.q.put(ln)
        if end_early:
            self.q.put(None)
        self.code = code if end_early else None
        self.calls: list[str] = []
        self.stdout = self._lines()

    def _lines(self):
        while True:
            item = self.q.get(timeout=10)
            if item is None:
                return
            yield item

    def poll(self):
        return self.code

    def terminate(self):
        self.calls.append("terminate")
        self.q.put(None)

    def kill(self):
        self.calls.append("kill")


class Prompter:
    """A stand-in for the prompter's `ssh -t`: it runs until `done()` holds,
    then exits with `code`. A wait past `limit` fails the test."""

    def __init__(self, args, done, code=0, limit=500, interrupts=0, **kwargs):
        self.args, self.kwargs = list(args), kwargs
        self.done, self.code, self.limit, self.interrupts = done, code, limit, interrupts
        self.waits = 0
        self.calls: list[str] = []

    def wait(self, timeout=None):
        self.waits += 1
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        if self.waits > self.limit:
            raise AssertionError("the prompter never ended")
        if self.done():
            return self.code
        time.sleep(0.01)
        raise subprocess.TimeoutExpired("ssh", timeout)

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


class Ssh:
    """The mkdir, a tail for each connection asked for, and the prompter."""

    def __init__(self, tails, done, made=0, code=0, interrupts=0):
        self.tails = list(tails)   # (lines, end_early) for each connection, in order
        self.done, self.made, self.code, self.interrupts = done, made, code, interrupts
        self.runs: list[tuple[list[str], dict]] = []
        self.tail_procs: list[Tail] = []
        self.prompter: Prompter | None = None

    def run(self, args, **kwargs):
        self.runs.append((list(args), kwargs))
        return types.SimpleNamespace(returncode=self.made)

    def popen(self, args, **kwargs):
        if "tail -n0 -F" in args[-1]:
            lines, early = self.tails.pop(0) if self.tails else ((), True)
            t = Tail(args, lines, end_early=early, **kwargs)
            self.tail_procs.append(t)
            return t
        self.prompter = Prompter(args, self.done, code=self.code, interrupts=self.interrupts, **kwargs)
        return self.prompter


def stamp():
    import datetime
    return datetime.datetime(2026, 11, 25, 9, 30, 0)


def cue_lines(due=100.0):
    return [f"count 3 v1s1-03 1 32000 due={due:.3f}\n", f"count 2 v1s1-03 1 48000 due={due + 1:.3f}\n",
            f"count 1 v1s1-03 1 64000 due={due + 2:.3f}\n", f"now v1s1-03 1 80000 due={due + 3:.3f}\n"]


def sitting(ssh, played, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = sit.sit("tools/bank/plans/v1-sitting-1.json", kw.pop("extra", ["--again", "v1s1-02-a-near-wake-1"]),
                   popen=ssh.popen, run=ssh.run, play=played.append, now=stamp, out=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def test_a_sitting_runs_the_prompter_with_the_terminal_and_sounds_its_cues_while_it_runs():
    played: list[bytes] = []
    ssh = Ssh(tails=[(cue_lines(), False)], done=lambda: len(played) == 4, code=7)
    began = time.monotonic()
    code, out, err = sitting(ssh, played, wall=lambda: 1000.0)
    assert code == 7 and time.monotonic() - began < 3  # the cue connection is stopped, then waited for
    cues = "~/voicebank/logs/cues-20261125-093000.txt"
    assert ssh.runs == [(["ssh", "-o", "BatchMode=yes", "emet-pi",
                          "mkdir -p \"$HOME\"/voicebank/logs && : >> \"$HOME\"/voicebank/logs/cues-20261125-093000.txt"],
                         {"stdin": subprocess.DEVNULL})]
    (tail,) = ssh.tail_procs
    assert tail.args == ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=5", "emet-pi",
                         'tail -n0 -F "$HOME"/voicebank/logs/cues-20261125-093000.txt']
    assert tail.kwargs == {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "text": True, "bufsize": 1}
    assert ssh.prompter.args == ["ssh", "-t", "emet-pi", sit.prompter_command(
        "tools/bank/plans/v1-sitting-1.json", "~/voicebank", cues, ["--again", "v1s1-02-a-near-wake-1"], "~/emet-ci/emet")]
    assert ssh.prompter.kwargs == {}  # the terminal is passed straight through
    assert tail.calls == ["terminate"] and ssh.prompter.calls == []
    assert played == [sit.SOUNDS["count"]] * 3 + [sit.SOUNDS["now"]]  # all four before the prompter ended
    assert out.startswith(sit.EARBUDS) and "4 cue(s) sounded, 0 dropped as late, 0 tone(s) failed" in out
    assert err == ""


def test_a_cue_that_comes_late_is_dropped_never_played_in_a_burst():
    """The first three arrive on time; the NOW comes 2 s after it was due,
    against the 0.05 s of the quickest, so it is dropped."""
    played: list[bytes] = []
    arrivals = iter([100.05, 101.05, 102.06, 105.0])
    state = sit.Cues(lambda: Tail(["ssh"], cue_lines(due=100.0)), played.append, io.StringIO(),
                     wall=lambda: next(arrivals))
    deadline = time.monotonic() + 5
    while state.played + state.late < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    state.stop()
    assert played == [sit.SOUNDS["count"]] * 3 and state.late == 1


def test_the_clocks_need_not_agree():
    """With the Pi's clock 40 s behind the laptop's, every cue is on time."""
    played: list[bytes] = []
    arrivals = iter([140.05, 141.06, 142.05, 143.07])
    state = sit.Cues(lambda: Tail(["ssh"], cue_lines(due=100.0)), played.append, io.StringIO(),
                     wall=lambda: next(arrivals))
    deadline = time.monotonic() + 5
    while state.played < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    state.stop()
    assert state.played == 4 and state.late == 0


def test_a_failed_tone_is_said_at_once_and_the_cues_go_on():
    played: list[bytes] = []
    failures = []

    def flaky(wav):
        if not failures:
            failures.append(1)
            raise RuntimeError("Failed to play sound")
        played.append(wav)

    err = io.StringIO()
    state = sit.Cues(lambda: Tail(["ssh"], cue_lines()), flaky, err, wall=lambda: 0.0)
    deadline = time.monotonic() + 5
    while state.played + state.failed < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "a tone failed to play (RuntimeError: Failed to play sound); the cues go on" in err.getvalue()
    state.stop()
    assert state.failed == 1 and state.played == 3 and played[-1] == sit.SOUNDS["now"]


def test_a_cue_connection_that_ends_is_started_again_with_a_warning():
    """The first connection gives one cue and ends while the prompter runs:
    the warning sounds, it is said on the terminal, and a second connection
    on the same file brings the rest."""
    played: list[bytes] = []
    first, rest = cue_lines()[:1], cue_lines()[1:]
    ssh = Ssh(tails=[(first, True), (rest, False)],
              done=lambda: played.count(sit.SOUNDS["count"]) + played.count(sit.SOUNDS["now"]) == 4)
    code, out, err = sitting(ssh, played, wall=lambda: 0.0)
    assert code == 0 and len(ssh.tail_procs) == 2 and ssh.tail_procs[0].args == ssh.tail_procs[1].args
    assert played == [sit.SOUNDS["count"], sit.SOUNDS["warning"]] + [sit.SOUNDS["count"]] * 2 + [sit.SOUNDS["now"]]
    assert "the cue connection ended (ssh exit 255); cues were lost, starting it again" in err
    assert "the cue connection started again 1 time(s)" in out


def test_a_cue_connection_that_keeps_ending_is_given_up_with_a_warning():
    played: list[bytes] = []
    ssh = Ssh(tails=[], done=lambda: played.count(sit.SOUNDS["warning"]) == sit.RESTARTS + 1)
    code, out, err = sitting(ssh, played, wall=lambda: 0.0)
    assert len(ssh.tail_procs) == sit.RESTARTS + 1
    assert err.count("starting it again") == sit.RESTARTS and "no more tones in this sitting" in err


def test_a_ctrl_c_on_the_laptop_never_ends_the_prompter():
    ssh = Ssh(tails=[((), False)], done=lambda: True, code=130, interrupts=2)
    code, _out, _err = sitting(ssh, [], extra=[])
    assert code == 130 and ssh.prompter.calls == [] and ssh.prompter.waits == 3


def test_no_prompter_starts_without_its_cue_file():
    ssh = Ssh(tails=[], done=lambda: True, made=255)
    code, _out, err = sitting(ssh, [], extra=[])
    assert code == 2 and ssh.prompter is None and ssh.tail_procs == [] and "could not make the cue file" in err


def test_a_dry_run_through_sit_is_refused():
    ssh = Ssh(tails=[], done=lambda: True)
    code, _out, err = sitting(ssh, [], extra=["--dry-run"])
    assert code == 2 and ssh.runs == [] and ssh.prompter is None and "--test-tone" in err


# ------------------------------------------------------------------ the command line

def test_the_prompter_command_keeps_home_and_quotes_the_rest():
    cmd = sit.prompter_command("tools/bank/plans/v1-sitting-1.json", "~/voicebank",
                               "~/voicebank/logs/cues-1.txt", ["--again", "v1s1-02-a-near-wake-1"], "~/emet-ci/emet")
    assert cmd == ('cd "$HOME"/emet-ci/emet && python3 tools/bank/record.py tools/bank/plans/v1-sitting-1.json '
                   "--bank '~/voicebank' --cues '~/voicebank/logs/cues-1.txt' --again v1s1-02-a-near-wake-1")
    assert sit.on_host("~/my bank/x") == "\"$HOME\"/'my bank/x'" and sit.on_host("/srv/b") == "/srv/b"


def test_main_plays_the_test_tone_and_passes_every_option_on(monkeypatch):
    played = []
    monkeypatch.setattr(sit, "winsound_player", lambda: played.append)
    monkeypatch.setattr(sit.time, "sleep", lambda s: None)
    assert sit.main(["--test-tone"]) == 0 and len(played) == 4
    seen = {}
    monkeypatch.setattr(sit, "sit", lambda plan, extra, **kw: seen.update(plan=plan, extra=extra, kw=kw) or 0)
    assert sit.main(["plans/x.json", "--again", "f-1", "--host", "pi2", "--tree", "~/t", "--bank", "~/rehearsal",
                     "--ssh", "/usr/bin/ssh", "--", "--plain"]) == 0
    assert seen == {"plan": "plans/x.json", "extra": ["--again", "f-1", "--plain"],
                    "kw": {"host": "pi2", "tree": "~/t", "bank": "~/rehearsal", "ssh": "/usr/bin/ssh"}}
