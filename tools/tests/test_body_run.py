"""The body runner, `tools/body/run.py`, against a stand-in body.

Nothing here opens ssh, a microphone or a speaker. `FakeBody` answers each
script the runner sends by the step named in its first line and keeps every
script, so a test can show both what was asked of the body and what never
was. `FakeLaptop` answers the read-only git questions and fails a test that
asks anything else.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

RUN_PY = Path(__file__).resolve().parents[1] / "body" / "run.py"


def _load():
    spec = importlib.util.spec_from_file_location("emet_body_run", RUN_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run = _load()

#: A weekday afternoon, well inside the hours the speaker may be used.
NOW = dt.datetime(2026, 10, 28, 14, 0)
HOME = "/home/alex"

QUIET_PS = "ps=1 /sbin/init splash\nps=812 sshd: alex@notty\nps=990 /usr/bin/pipewire\n"
LISTENING_PS = ("ps=4242 /home/alex/Documents/emet/.venv/bin/python3 "
                "/home/alex/Documents/emet/.venv/bin/emet-listen /home/alex/pi-speakerphone.yaml soul.yaml\n")


def preflight_out(ps: str = QUIET_PS, lock: str = "free", pcm: str = "") -> str:
    return (f"home={HOME}\ndate=2026-10-28T14:00:00-07:00\nuptime= 14:00:00 up 2 days, load average: 0.03\n"
            "loadavg=0.03 0.06 0.07 1/515 1\ndisk=/dev/mmcblk0p2 58G 13G 44G 23% /\n"
            "temp=temp=49.9'C\nthrottled=throttled=0x50000\nkernel=Linux 6.18 aarch64\n"
            "python=Python 3.13.5\nemet_env=EMET_LIVE_TESTS \n" + ps + pcm + f"lock={lock}\n"
            + ("lock_line=token someone else\n" if lock == "held" else ""))


def junit(tests: int, skipped: int = 0, failures: int = 0) -> str:
    return (f'<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" failures="{failures}" '
            f'skipped="{skipped}" tests="{tests}" time="3.5"/></testsuites>')


def tar_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


HARDWARE_OK = "state_before=abc\ncode=0\nmixer=same\nstate_after=abc\nended=1\n"

#: What the stand-in body says its manifest and its state file hash to.
MANIFEST_SHA = "5e" * 32
STATE_SHA = "a7" * 32
STATE_FILE = f"{HOME}/.local/state/emet/pi_speakerphone.json"
FINGERPRINT_OK = f"manifest_hash={MANIFEST_SHA}\nstate={STATE_FILE}\nstate_hash={STATE_SHA}\ncode=0\n"

#: pip-audit, as the audit step's script reports it, with nothing found.
AUDIT_CLEAN = "version=pip-audit 2.10.1\ncode=0\ntail=No known vulnerabilities found\nparsed=yes\npackages=58\n"


def lock_part() -> tuple[str, str]:
    """pip-audit's part of the real CI lock, and its pin, as the runner reads them."""
    return run.audit_requirements(run.AUDIT_LOCK.read_text(encoding="utf-8"))


def lock_digest() -> str:
    """The sha256 of the lock's part, which the audit's venv records as installed."""
    return hashlib.sha256(lock_part()[0].encode("utf-8")).hexdigest()


def written(path: str, models: dict, sink: str = "null") -> str:
    return "written=" + json.dumps({"path": path, "sink": sink, "source": "microphone", "models": models},
                                   sort_keys=True) + "\n"


def prepared(*files: tuple[str, dict], sink: str = "null", extra: str = "") -> str:
    """What NULL_MANIFEST_PY prints on the stand-in body for these files."""
    return (f"manifest_hash={MANIFEST_SHA}\n" + "".join(written(p, m, sink) for p, m in files)
            + f"body=pi_speakerphone\nstate={STATE_FILE}\nmanifest_code=0\n" + extra)


SPEAK_MODELS = {**run.MODELS, "tts": dict(run.PIPER_TTS)}
BUDGET_PREPARED = prepared(("budget/listen.yaml", run.MODELS), ("budget/speak.yaml", SPEAK_MODELS),
                           extra="bank_version=2026-11-25\n")


def driver(kind: str, *, boot_ms: float = 1800.0, mean: float = 2.4, p99: float = 4.1, over: int = 0,
           rss_mb: float = 98.0, voice_ms: float = 620.0, cpu_pct: float = 3.5, code: int = 0,
           gap_ms: float = 2260.0) -> dict:
    """One BUDGET_PY result, as the engine's wrapped SessionStats left it.
    Only the speak run's turns make a sound, so only it has turn gaps: three
    turns whose median is `gap_ms`."""
    start = 1_000_000_000_000
    result = {"start_ns": start, "exit": code, "first_ns": start + int(boot_ms * 1e6),
              "rss_boot_kb": int(rss_mb * 1024), "frames": 7500, "mean_ms": mean, "p99_ms": p99, "over": over,
              "voice_first_ms": [], "cpu_start": 10.0, "wall_start": 100.0, "cpu_end": 10.0, "wall_end": 160.0,
              "gap_ms": []}
    if kind == "speak":
        result["voice_first_ms"] = [voice_ms, voice_ms]
        result["gap_ms"] = [gap_ms + 900.0, gap_ms - 300.0, gap_ms]
    if kind == "idle":
        result["cpu_end"] = 10.0 + cpu_pct / 100.0 * 60.0
    return result


def budget_run(repeat: int = 1, **numbers) -> str:
    lines = []
    for i in range(1, repeat + 1):
        for kind in ("frames", "speak", "idle"):
            lines.append(f"ran={kind}-{i} 0")
            lines.append(f"result={kind}-{i} " + json.dumps(driver(kind, **numbers)))
    return "\n".join(lines) + "\n"


class FakeBody:
    """The body as the runner sees it, with one answer for each step."""

    def __init__(self, answers: dict | None = None, *, unreachable: bool = False) -> None:
        self.scripts: list[str] = []
        self.sent: list[tuple[str, bytes]] = []
        self.unreachable = unreachable
        self.digest = ""
        self.answers = {
            "preflight": preflight_out(),
            "busy": preflight_out(lock="held"),
            "lock": "lock=taken\n",
            "unlock": "lock=released\n",
            "swap": lambda script: f"digest={self.digest}\nswapped=yes\n",
            "source": "{}",
            "install": "key=k\ninstalled=yes\nversions=0.5.4 0.5.4 0.5.4 0.5.4\n",
            "fingerprint": FINGERPRINT_OK,
            "audit": AUDIT_CLEAN,
            "budget-prepare": BUDGET_PREPARED,
            "budget-run": budget_run(),
            "checks": "".join(f"{c}=0\n{c}_said=ok\n" for c in ("check_layering", "release_check", "check_style")),
            "bank": "missing=tools/bank/replay.py is not in the synced tree\n",
            "prepare": (f"manifest_hash={MANIFEST_SHA}\nbody=pi_speakerphone\nstate={STATE_FILE}\n"
                        f"models={json.dumps(run.MODELS)}\n"),
            "collect": run.Result(0, "", raw=tar_of({"./suite-emet-sdk.txt": b"226 passed\n"})),
            "history": "",
        }
        for package, tests in (("emet-sdk", 226), ("emet-hal", 117), ("emet-providers", 167), ("emet-engine", 949)):
            self.answers[f"suite {package}"] = (f"code=0\nms=4000\nsummary={tests} passed in 3.5s\n--- junit\n"
                                                + junit(tests, skipped=1 if package == "emet-hal" else 0))
        for name in run.HARDWARE_SUBSTEPS:
            self.answers[name] = HARDWARE_OK
        self.answers.update(answers or {})

    @staticmethod
    def step_of(script: str) -> str:
        first = script.split("\n", 1)[0]
        assert first.startswith("# body-run: "), f"a script without its step named: {first!r}"
        return first.removeprefix("# body-run: ")

    def steps(self) -> list[str]:
        return [self.step_of(s) for s in self.scripts]

    def script(self, step: str) -> str:
        return next(s for s in self.scripts if self.step_of(s) == step)

    def run(self, script: str, *, timeout: float = 600.0):
        if self.unreachable:
            raise run.Unreachable("ssh: connect to host emet-pi.local port 22: No route to host")
        self.scripts.append(script)
        answer = self.answers[self.step_of(script)]
        if callable(answer):
            answer = answer(script)
        if isinstance(answer, run.Result):
            return answer
        return run.Result(0, answer, raw=answer.encode())

    def send(self, command: str, data: bytes, *, timeout: float = 600.0):
        if self.unreachable:
            raise run.Unreachable("no route")
        self.sent.append((command, data))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            entries = [(m.name, tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()]
        self.digest = run.tree_digest(entries)
        return run.Result(0, "")


class FakeLaptop:
    """The laptop's git, read-only. Any other git command fails the test."""

    READ_ONLY = {"ls-files", "rev-parse", "log", "ls-tree"}

    def __init__(self, repo: Path, files: dict[str, bytes], head: dict[str, bytes], *, autocrlf: bool = True):
        self.repo = repo
        for name, data in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_bytes(data)
        self.files = files
        self.head = head
        self.autocrlf = autocrlf

    def git(self, *args: str) -> bytes:
        assert args[0] in self.READ_ONLY, f"the runner asked git to {args[0]}"
        if args[0] == "ls-files":
            return "\0".join(list(self.files) + ["deleted-since.txt"]).encode() + b"\0"
        if args[0] == "rev-parse":
            return b"eb55748aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        if args[0] == "log":
            return b"0.5.4: Keep spoken numbers as words in the transcript\n"
        return b"".join(f"100644 blob {run.blob_id(data)}\t{name}\0".encode() for name, data in self.head.items())

    def config(self, key: str) -> str:
        return "true" if self.autocrlf and key == "core.autocrlf" else ""


@pytest.fixture
def laptop(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    files = {"emet-sdk/a.py": b"print('a')\r\n", "tools/new.py": b"x = 1\r\n", "data.bin": b"\0\r\n"}
    head = {"emet-sdk/a.py": b"print('a')\n", "gone.py": b"old\n", "data.bin": b"\0\r\n"}
    return FakeLaptop(repo, files, head)


def make(tmp_path, laptop, body, *extra: str, clock=lambda: NOW, release: str = "0.5.5"):
    """A runner against the stand-in body. Its releases/ is a folder of the
    test's own, so the budgets never read the laptop's real evidence, and its
    sleep returns at once."""
    args = run.parse_args(["--release", release, "--tree", "--out", str(tmp_path / "out"), *extra])
    return run.BodyRun(args, remote=body, laptop=laptop, clock=clock,
                       log_check=tmp_path / "no-log-check.py", history=[], sleep=lambda s: None,
                       top=tmp_path / "top")


def record(tmp_path) -> dict:
    return json.loads((tmp_path / "out" / "run.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ not done


def test_an_unreachable_body_is_a_run_not_done_and_never_a_pass(tmp_path, laptop, capsys):
    code = make(tmp_path, laptop, FakeBody(unreachable=True)).run()
    assert code == 2
    verdict = record(tmp_path)["verdict"]
    assert verdict.startswith("body run not done: the body did not answer over SSH")
    assert "passed" not in verdict
    assert "body run not done" in capsys.readouterr().out


def test_the_runner_refuses_while_the_robot_is_listening(tmp_path, laptop):
    body = FakeBody({"preflight": preflight_out(ps=QUIET_PS + LISTENING_PS)})
    code = make(tmp_path, laptop, body).run()
    assert code == 2
    assert "the robot is in use" in record(tmp_path)["verdict"]
    assert body.steps() == ["preflight"], "nothing after the preflight may run, not even the lock"


def test_the_runner_refuses_while_the_lock_is_held(tmp_path, laptop):
    body = FakeBody({"preflight": preflight_out(lock="held")})
    assert make(tmp_path, laptop, body).run() == 2
    assert "lock is held" in record(tmp_path)["verdict"]
    assert body.steps() == ["preflight"]


def test_a_lock_taken_after_the_preflight_still_stops_the_run(tmp_path, laptop):
    body = FakeBody({"lock": "lock=held\nlock_line=token the maintainer\n"})
    assert make(tmp_path, laptop, body).run() == 2
    assert body.steps() == ["preflight", "lock"]
    assert not body.sent, "no tree may be sent without the lock"


@pytest.mark.parametrize("line, busy", [
    ("4242 /home/alex/Documents/emet/.venv/bin/python3 /home/alex/Documents/emet/.venv/bin/emet-listen a b", True),
    ("4243 script -qa /home/alex/pi-0.5.4.log -c emet-talk /home/alex/pi-speakerphone.yaml soul.yaml", True),
    ("4244 /usr/bin/python3 -m emet_engine.cli a b --replay x.wav", True),
    ("4248 python3 tools/bank/record.py --plan plans/v1-a.json --out /home/alex/voicebank/audio", True),
    ("4249 arecord -D plughw:3,0 -f S16_LE -r 16000 -c 1 -d 100 /home/alex/voicebank/audio/x.wav", True),
    ("4250 /usr/bin/aplay -D plughw:2,0 sweep.wav", True),
    ("4245 grep -E (^|[ /])emet-(listen|talk)( |$)", False),
    ("4246 vim emet-listen.md", False),
    ("4251 less arecord.txt", False),
    ("4247 bash -s", False),
])
def test_busy_matches_the_programs_and_not_a_search_for_them(line, busy):
    assert bool(run.busy_lines(line)) is busy


def test_the_runner_refuses_while_a_sound_card_is_open(tmp_path, laptop):
    """A PCM opened by anything at all, a recording started by hand included."""
    pcm = "pcm=/proc/asound/card3/pcm0c/sub0/status: state: RUNNING, owner_pid : 4321\n"
    body = FakeBody({"preflight": preflight_out(pcm=pcm)})
    assert make(tmp_path, laptop, body).run() == 2
    assert "a sound card is open: /proc/asound/card3/pcm0c" in record(tmp_path)["verdict"]
    assert body.steps() == ["preflight"]
    assert run.PCM_CHECK.format(root="/proc/asound") in body.script("preflight")


def test_the_pcm_check_names_each_open_substream_and_no_closed_one(tmp_path):
    """The preflight's PCM lines, run by a real bash over a stand-in for
    /proc/asound: a closed substream says nothing, an open one says so
    whatever its state."""
    statuses = {"card2/pcm0p/sub0/status": "closed\n",
                "card3/pcm0c/sub0/status": "state: RUNNING\nowner_pid   : 4321\ntrigger_time: 1.0\n",
                "card3/pcm0c/sub1/status": "state: PREPARED\nowner_pid   : 99\n"}
    for name, text in statuses.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    done = subprocess.run([posix_bash(), "-s"], input=run.PCM_CHECK.format(root=".").encode(),
                          cwd=tmp_path, capture_output=True, timeout=60)
    found = run.Result(done.returncode, done.stdout.decode()).values().get("pcm", [])
    assert found == ["./card3/pcm0c/sub0/status: state: RUNNING, owner_pid : 4321",
                     "./card3/pcm0c/sub1/status: state: PREPARED, owner_pid : 99"]
    assert run.busy_reasons({"pcm": found})[0].startswith("a sound card is open: ./card3/pcm0c/sub0")


def test_the_lock_is_released_only_with_the_token_it_was_taken_with(tmp_path, laptop):
    body = FakeBody()
    make(tmp_path, laptop, body).run()
    taken, released = body.script("lock"), body.script("unlock")
    token = re.search(r"\b[0-9a-f]{32}\b", taken).group(0)
    assert "set -C" in taken, "the lock is created only when no file is there"
    assert f'grep -qx "token {token}"' in released
    assert body.steps()[-1] == "unlock"


class BashBody:
    """The body's shell for the steps that need only bash and a home folder:
    each script runs in a real bash, after the real prelude, with HOME a
    temporary folder."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def run(self, script: str, *, timeout: float = 120.0):
        env = dict(os.environ, HOME=str(self.home))
        done = subprocess.run([posix_bash(), "-s"], input=(run.PRELUDE + script).encode(), env=env,
                              capture_output=True, timeout=timeout)
        return run.Result(done.returncode, done.stdout.decode("utf-8", "replace"),
                          done.stderr.decode("utf-8", "replace"), raw=done.stdout)


def test_the_lock_scripts_leave_anyone_elses_lock_where_it_is(tmp_path, laptop):
    home = tmp_path / "home"
    lock = home / "emet-ci" / "lock"
    lock.parent.mkdir(parents=True)
    runner = make(tmp_path, laptop, BashBody(home))
    foreign = "token " + "f" * 32 + "\nby the maintainer\n"
    lock.write_text(foreign)
    assert "lock is held" in runner.take_lock()
    assert lock.read_text() == foreign, "taking the lock must never overwrite one"
    runner.locked = True  # even a runner that thinks it holds the lock leaves another's alone
    runner.release_lock()
    assert lock.read_text() == foreign
    lock.unlink()
    runner.locked = False
    assert runner.take_lock() is None and runner.locked
    assert lock.read_text().startswith(f"token {runner.token}\n")
    runner.release_lock()
    assert not lock.exists()


# ------------------------------------------------------- the whole run


def test_a_run_without_a_window_never_uses_the_microphone_or_the_speaker(tmp_path, laptop):
    body = FakeBody()
    code = make(tmp_path, laptop, body).run()
    assert code == 0
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    for name in ("speaker", "live"):
        assert steps[name]["status"] == "not run"
        assert "no --window" in steps[name]["reason"]
    asked = body.steps()
    assert "prepare" not in asked
    assert not any(s.startswith(("speaker-", "live-")) for s in asked)


def test_the_suites_are_counted_per_package(tmp_path, laptop):
    make(tmp_path, laptop, FakeBody()).run()
    suites = next(s for s in record(tmp_path)["steps"] if s["name"] == "suites")
    assert suites["status"] == "passed"
    assert suites["details"]["emet-hal"] | {} == {
        "code": 0, "summary": "117 passed in 3.5s", "wall_s": 4.0,
        "collected": 117, "passed": 116, "skipped": 1, "failed": 0, "pytest_s": 3.5}


def test_a_failing_suite_fails_the_run(tmp_path, laptop):
    body = FakeBody({"suite emet-hal": "code=1\nms=10\nsummary=1 failed\n--- junit\n" + junit(117, 1, 1)})
    assert make(tmp_path, laptop, body).run() == 1
    suites = next(s for s in record(tmp_path)["steps"] if s["name"] == "suites")
    assert suites["status"] == "failed" and "emet-hal" in suites["reason"]


def test_a_missing_replay_runner_is_not_run_and_not_failed(tmp_path, laptop):
    assert make(tmp_path, laptop, FakeBody()).run() == 0
    bank = next(s for s in record(tmp_path)["steps"] if s["name"] == "bank")
    assert bank["status"] == "not run" and "replay.py" in bank["reason"]


# -------------------------------------------------------- the bank step

V1_TAKES = ["trial-30s-01-a-near", "trial-30s-01-a-near-2", "v1s1-01-a-near-room-tone", "v1s1-02-a-near-wake-1"]
#: replay.py's last line on the Pi's bank once the four of bank v1 are named
#: and held to nothing (the run of 2026-11-25 had them as NO GOLDEN).
V1_TOTAL = ("total: 17 replayed on linux-aarch64 from the carried mean, 13 held to a golden, 0 changed or "
            "without a golden, 0 problem(s); 4 unreleased and held to nothing: " + ", ".join(V1_TAKES))


def bank_says(code: int, total: str, held: str | None, unreleased: list[str] = ()) -> str:
    return (f"code={code}\ntail=stop-3m-0.5.4: same as the golden, 0 wakes\ntail={total}\n"
            + (f"held={held}\n" if held is not None else "") + "".join(f"unreleased={u}\n" for u in unreleased))


def bank_step(tmp_path, laptop, answer: str) -> tuple[int, dict]:
    code = make(tmp_path, laptop, FakeBody({"bank": answer})).run()
    return code, next(s for s in record(tmp_path)["steps"] if s["name"] == "bank")


def test_the_bank_step_passes_on_a_bank_whose_newest_sitting_has_no_goldens_and_names_it(tmp_path, laptop):
    code, bank = bank_step(tmp_path, laptop, bank_says(0, V1_TOTAL, "13", V1_TAKES))
    assert code == 0 and bank["status"] == "passed"
    assert bank["details"]["replayed"] == 13 and bank["details"]["unreleased"] == V1_TAKES
    assert bank["details"]["code"] == "0" and bank["details"]["tail"][-1] == V1_TOTAL
    assert bank["reason"] == "replay.py exit 0: " + V1_TOTAL
    for take in V1_TAKES:
        assert take in bank["reason"]
    assert take in (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")


def test_the_bank_step_names_the_unreleased_even_when_replays_last_line_does_not(tmp_path, laptop):
    code, bank = bank_step(tmp_path, laptop, bank_says(0, "total: 17 replayed, all well", "13", V1_TAKES))
    assert code == 0 and bank["status"] == "passed"
    assert bank["reason"].endswith("; 4 unreleased and held to nothing: " + ", ".join(V1_TAKES))


def test_a_bank_step_with_every_recording_held_names_none(tmp_path, laptop):
    total = "total: 13 replayed on linux-aarch64 from the carried mean, 13 held to a golden, 0 changed or without a golden, 0 problem(s)"
    code, bank = bank_step(tmp_path, laptop, bank_says(0, total, "13"))
    assert code == 0 and bank["status"] == "passed" and bank["reason"] == "replay.py exit 0: " + total
    assert bank["details"]["replayed"] == 13 and bank["details"]["unreleased"] == []


@pytest.mark.parametrize("answer, replayed, says", [
    # A golden missing in a sitting that has goldens: replay.py exits 1, as in 0.5.5.
    (bank_says(1, "total: 17 replayed on linux-aarch64 from the carried mean, 13 held to a golden, 1 changed or "
                  "without a golden, 0 problem(s); 3 unreleased and held to nothing: a, b, c", "13", ["a", "b", "c"]),
     13, "replay.py exit 1"),
    # A replay that held no recording to a golden fails, whatever replay.py's exit code.
    (bank_says(0, "total: 4 replayed", "0", V1_TAKES), 0, "no recording was held to a golden"),
    # A replay.py from before 0.5.6 writes no count, and one that wrote no report gives none.
    (bank_says(0, "total: 13 replayed on linux-aarch64 from the carried mean, 0 changed or without a golden, 0 problem(s)",
               "unknown"), None, "gives no count of what it held to a golden"),
    (bank_says(0, "total: 13 replayed", None), None, "gives no count of what it held to a golden"),
    (bank_says(124, "soak-10min: same as the golden", "unknown"), None, "replay.py exit 124"),
], ids=["a-golden-missing", "nothing-held", "no-count-in-the-report", "no-report", "timed-out"])
def test_the_bank_step_fails_unless_replay_passed_and_held_a_recording_to_a_golden(
        tmp_path, laptop, answer, replayed, says):
    code, bank = bank_step(tmp_path, laptop, answer)
    assert code == 1 and bank["status"] == "failed"
    assert says in bank["reason"] and bank["details"]["replayed"] == replayed


STUB_REPLAY = '''\
"""A stand-in for tools/bank/replay.py: it writes the report it is told to
and prints a last line, as the real one does, and replays nothing."""
import json, sys

args = sys.argv[1:]
report = {"recordings": {}}
if "--old" not in args:
    report.update(held=["a", "b"], unreleased=["v1s1-01", "v1s1-02"])
with open(args[args.index("--report") + 1], "w", encoding="utf-8") as handle:
    json.dump(report, handle)
print("a: same as the golden")
print("total: 4 replayed, 2 held to a golden; 2 unreleased and held to nothing: v1s1-01, v1s1-02")
'''


@pytest.mark.parametrize("extra, status, replayed, unreleased", [
    ([], "passed", 2, ["v1s1-01", "v1s1-02"]),
    (["--bank-arg=--old"], "failed", None, []),
], ids=["a-report-with-counts", "a-report-from-before-0.5.6"])
def test_the_bank_step_reads_replays_report_in_a_real_shell(tmp_path, laptop, extra, status, replayed, unreleased):
    """The step's script as it is sent, through bash, with a stand-in
    replay.py in the synced tree: the counts come from the report it wrote."""
    home = tmp_path / "home"
    (home / "emet-ci" / "emet" / "tools" / "bank").mkdir(parents=True)
    (home / "emet-ci" / "emet" / "tools" / "bank" / "replay.py").write_text(STUB_REPLAY, encoding="utf-8")
    (home / "voicebank").mkdir()
    (home / "voicebank" / "bank.json").write_text("{}", encoding="utf-8")
    fake_venv(home)
    runner = make(tmp_path, laptop, BashBody(home), *extra)
    runner.home = shell_home(home)
    step = runner.bank()
    assert (step.status, step.details["replayed"], step.details["unreleased"]) == (status, replayed, unreleased), step.reason
    assert step.details["code"] == "0" and step.details["tail"][-1].startswith("total: 4 replayed")


def test_every_suite_and_check_runs_from_the_runners_own_tree(tmp_path, laptop):
    body = FakeBody()
    make(tmp_path, laptop, body).run()
    for script in body.scripts:
        assert "Documents/emet" not in script, "the maintainer's checkout is never touched"
    for step in ("suite emet-sdk", "checks", "install"):
        assert '"$CI/emet' in body.script(step)


# -------------------------------------------------------------- format 2

FORMAT_1_FIELDS = ("format", "started", "ended", "host", "release", "window", "allow_night", "preflight",
                   "source", "steps", "flags", "verdict")


def test_a_run_with_no_window_writes_format_2_with_the_body_and_no_flags_run(tmp_path, laptop):
    body = FakeBody()
    assert make(tmp_path, laptop, body).run() == 0
    said = record(tmp_path)
    assert said["format"] == "emet-body-run/2" == run.FORMAT
    assert [key for key in FORMAT_1_FIELDS if key not in said] == [], "every field of format 1 stays"
    assert set(said) - set(FORMAT_1_FIELDS) == {"body", "flags_run", "audit", "budgets"}, \
        "format 2's fields, and batch C's for the steps that ran; no soak was asked for"
    assert said["body"] == {
        "manifest": "~/pi-speakerphone.yaml", "manifest_sha256": MANIFEST_SHA,
        "state": "~/.local/state/emet/pi_speakerphone.json", "state_sha256": STATE_SHA, "note": ""}
    assert said["flags_run"] == []
    assert all(set(step) == {"name", "status", "reason", "seconds", "details"} for step in said["steps"])
    assert "fingerprint" not in [step["name"] for step in said["steps"]], "the fingerprint is a field, never a step"
    asked = body.steps()
    assert asked.count("fingerprint") == 1
    assert asked.index("install") < asked.index("fingerprint") < asked.index("suite emet-sdk")
    sent = body.script("fingerprint")
    assert f"- {HOME}/pi-speakerphone.yaml <<'EMET_PY'" in sent and run.FINGERPRINT_PY in sent
    report = (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")
    assert (f"  body     ~/pi-speakerphone.yaml sha256 {MANIFEST_SHA[:12]}; "
            f"state ~/.local/state/emet/pi_speakerphone.json sha256 {STATE_SHA[:12]}") in report


def test_a_run_that_was_not_done_still_carries_both_fields(tmp_path, laptop):
    assert make(tmp_path, laptop, FakeBody(unreachable=True)).run() == 2
    said = record(tmp_path)
    assert said["format"] == run.FORMAT and said["flags_run"] == []
    assert said["body"] == {"manifest": "~/pi-speakerphone.yaml", "manifest_sha256": None, "state": None,
                            "state_sha256": None,
                            "note": "not read: the run ended before the body's manifest was hashed"}


def test_a_collect_only_run_names_the_body_too(tmp_path, laptop):
    """The sitting's typescript comes home with the body it was sat at."""
    body = FakeBody({"fetch-file": "Script started on 2026-10-28 14:10:00-07:00 [COMMAND=\"emet-talk a b\"]\n"})
    args = run.parse_args(["--release", "0.5.5", "--collect-only", "--fetch", "~/sitting-0.5.5.log",
                           "--manifest", "~/pi-speakerphone-aura.yaml", "--out", str(tmp_path / "out")])
    runner = run.BodyRun(args, remote=body, laptop=laptop, clock=lambda: NOW,
                         log_check=tmp_path / "none.py", history=[])
    assert runner.run() == 0
    assert record(tmp_path)["body"]["manifest"] == "~/pi-speakerphone-aura.yaml"
    assert record(tmp_path)["body"]["manifest_sha256"] == MANIFEST_SHA
    assert f"- {HOME}/pi-speakerphone-aura.yaml <<'EMET_PY'" in body.script("fingerprint")
    assert record(tmp_path)["flags_run"] == []


@pytest.mark.parametrize("answer, manifest_sha, state, state_sha, note", [
    ("note=the manifest is not there\ncode=0\n", None, None, None, "the manifest is not there"),
    (f"manifest_hash={MANIFEST_SHA}\nstate={STATE_FILE}\nnote=there is no state file yet\ncode=0\n",
     MANIFEST_SHA, "~/.local/state/emet/pi_speakerphone.json", None, "there is no state file yet"),
    (f"manifest_hash={MANIFEST_SHA}\nstate=/etc/emet/state/pi_speakerphone.json\nstate_hash={STATE_SHA}\ncode=0\n",
     MANIFEST_SHA, "/etc/emet/state/pi_speakerphone.json", STATE_SHA, ""),
    ("manifest_hash=not-a-hash\nstate_hash=5e5e\nnote=one\nnote=two\ncode=0\n", None, None, None, "one; two"),
    ("code=1\n", None, None, None, "the fingerprint script exited 1"),
    ("", None, None, None, "the fingerprint script exited without a code"),
])
def test_what_the_fingerprint_could_not_read_is_null_with_a_note_and_fails_nothing(
        tmp_path, laptop, answer, manifest_sha, state, state_sha, note):
    assert make(tmp_path, laptop, FakeBody({"fingerprint": answer})).run() == 0
    said = record(tmp_path)
    assert said["body"] == {"manifest": "~/pi-speakerphone.yaml", "manifest_sha256": manifest_sha,
                            "state": state, "state_sha256": state_sha, "note": note}
    assert [s["name"] for s in said["steps"] if s["status"] in ("failed", "refused")] == []
    if note:
        assert f"           {note}" in (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")


# ------------------------------------- the fingerprint's own Python, run

MANIFEST_YAML = """\
# The reference body, as its owner keeps it.
manifest_version: "0.1"
body:
  id: pi_speakerphone   # keys the state file
  name: "a Raspberry Pi with a USB microphone and speaker"
audio:
  input: {device: "USB", sample_rate: 16000, channels: 1}
  output: {sink: "speaker", gain_db: -6.0}
  wake:
    engine: pocketsphinx
    params:
      threshold: 1.0e-15
capabilities: [speak, listen]
models:
  stt: {provider: deepgram, model: nova-3}
"""

#: The same body written another way: every mapping's keys in another order,
#: other comments, block style for flow style, other quotes, other spacing.
MANIFEST_REWRITTEN = """\
models:
  stt:
    model: 'nova-3'
    provider: deepgram
capabilities:
  - speak        # the voice
  - listen

audio:
  wake: {params: {threshold: 1.0e-15}, engine: pocketsphinx}
  output:
    gain_db: -6.0
    sink: speaker
  input:
    channels: 1
    sample_rate: 16000
    device: USB
body: {name: a Raspberry Pi with a USB microphone and speaker, id: pi_speakerphone}
manifest_version: '0.1'
# Edited by hand on the body.
"""


def fingerprint(tmp_path: Path, manifest: str | None, *, state: bytes | None = None, bare: bool = False):
    """`FINGERPRINT_PY` itself, run by this Python as the body's runs it:
    what it printed, by key, and everything it wrote to either stream. The
    state file's place is a temporary folder, through XDG_STATE_HOME. `bare`
    runs it with no site-packages, as a python3 without PyYAML is."""
    work = tmp_path / "fingerprint"
    xdg = work / "xdg"
    (xdg / "emet").mkdir(parents=True, exist_ok=True)
    (work / "fingerprint.py").write_text(run.FINGERPRINT_PY, encoding="utf-8")
    path = work / "pi-speakerphone.yaml"
    path.unlink(missing_ok=True)
    if manifest is not None:
        path.write_text(manifest, encoding="utf-8")
    place = xdg / "emet" / "pi_speakerphone.json"
    place.unlink(missing_ok=True)
    if state is not None:
        place.write_bytes(state)
    done = subprocess.run([sys.executable, *(["-S", "-I"] if bare else []), str(work / "fingerprint.py"), str(path)],
                          capture_output=True, env=dict(os.environ, XDG_STATE_HOME=str(xdg)), timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    out = done.stdout.decode("utf-8")
    return run.Result(0, out).values(), out + done.stderr.decode("utf-8", "replace"), place


def test_the_canonical_form_is_sorted_keys_in_compact_json():
    """The form the README states, pinned on a document small enough to
    write out by hand: comments and layout gone, keys sorted at every depth,
    a sequence in its own order, no spaces, ASCII."""
    import hashlib

    pytest.importorskip("yaml")
    text = "# a comment\nb: 1\na:\n  z: [x, 'y']   # and another\n  c: \"caf\\u00e9\"\n"
    scope: dict = {}
    exec(run.CANONICAL_PY, scope)
    import yaml

    want = b'{"a":{"c":"caf\\u00e9","z":["x","y"]},"b":1}'
    assert scope["canonical_sha256"](yaml.safe_load(text)) == hashlib.sha256(want).hexdigest()


def test_the_fingerprint_hashes_the_manifest_and_the_state_file(tmp_path):
    import hashlib

    pytest.importorskip("yaml")
    pytest.importorskip("emet_engine.state")
    carried = b'{\n  "state_version": "0.1",\n  "body_id": "pi_speakerphone"\n}\n'
    said, _, place = fingerprint(tmp_path, MANIFEST_YAML, state=carried)
    assert set(said) == {"manifest_hash", "state", "state_hash"}, "two hashes and a path, and no note"
    assert run.HEX64.fullmatch(said["manifest_hash"][0])
    assert Path(said["state"][0]) == place
    assert said["state_hash"] == [hashlib.sha256(carried).hexdigest()], "the state file as it is, byte for byte"


def test_reordered_keys_and_comments_keep_the_hash_and_a_changed_value_does_not(tmp_path):
    pytest.importorskip("yaml")
    first = fingerprint(tmp_path, MANIFEST_YAML)[0]["manifest_hash"]
    assert fingerprint(tmp_path, MANIFEST_REWRITTEN)[0]["manifest_hash"] == first
    assert MANIFEST_REWRITTEN.encode() != MANIFEST_YAML.encode()
    louder = MANIFEST_YAML.replace("gain_db: -6.0", "gain_db: -3.0")
    assert fingerprint(tmp_path, louder)[0]["manifest_hash"] != first
    swapped = MANIFEST_YAML.replace("[speak, listen]", "[listen, speak]")
    assert fingerprint(tmp_path, swapped)[0]["manifest_hash"] != first, "a sequence's order is part of the body"


def test_a_missing_manifest_and_a_missing_state_file_are_notes(tmp_path):
    pytest.importorskip("yaml")
    pytest.importorskip("emet_engine.state")
    said, _, _ = fingerprint(tmp_path, None)
    assert said == {"note": ["the manifest is not there"]}
    said, _, place = fingerprint(tmp_path, MANIFEST_YAML)
    assert said["note"] == ["there is no state file yet"] and "state_hash" not in said
    assert Path(said["state"][0]) == place and "manifest_hash" in said
    said, _, _ = fingerprint(tmp_path, "audio: {}\n")
    assert said["note"] == ["the manifest names no body.id, so no state file is named"] and "state" not in said


def test_a_manifest_that_does_not_parse_is_named_by_the_errors_kind_alone(tmp_path):
    """A YAML parser's message quotes the line it stopped at, so none of it
    is printed: nothing of the manifest but a hash leaves the body."""
    pytest.importorskip("yaml")
    said, everything, _ = fingerprint(tmp_path, "body: [unclosed\n  device: a-word-of-the-manifest\n")
    assert len(said["note"]) == 1 and said["note"][0].startswith("the manifest is not readable YAML (")
    assert "manifest_hash" not in said
    assert "a-word-of-the-manifest" not in everything and "unclosed" not in everything
    for text in (MANIFEST_YAML, MANIFEST_REWRITTEN):
        said, everything, _ = fingerprint(tmp_path, text)
        assert "Raspberry" not in everything and "deepgram" not in everything


def test_a_python_without_pyyaml_says_so_and_hashes_nothing(tmp_path):
    said, _, _ = fingerprint(tmp_path, MANIFEST_YAML, bare=True)
    assert said == {"note": ["the manifest was not hashed: PyYAML is not installed for the Python that read it"]}


def program(path: Path, *command: str) -> Path:
    """An executable at `path` that runs `command` with its own arguments
    after it, for a stand-in body whose shell is real."""
    path.parent.mkdir(parents=True, exist_ok=True)
    words = " ".join(f'"{word}"' for word in command)
    path.write_text(f'#!/bin/sh\nexec {words} "$@"\n', encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def fake_venv(home: Path) -> None:
    """The runner's venv on such a body: its `$CI/.venv/bin/python` runs the
    Python these tests run under."""
    program(home / "emet-ci" / ".venv" / "bin" / "python", Path(sys.executable).as_posix())


def shell_home(home: Path) -> str:
    """The home folder as the stand-in body's own shell spells it, which is
    what the preflight hands the runner."""
    return BashBody(home).run('echo "home=$HOME"\n').value("home")


def test_the_fingerprint_runs_in_a_real_shell_with_the_runners_venv(tmp_path, laptop, monkeypatch):
    """The script as it is sent, heredoc and all, through bash. With no venv
    yet it falls back to the body's python3, here one with no PyYAML, and
    gives a note; with the venv's Python it gives the hashes."""
    pytest.importorskip("yaml")
    pytest.importorskip("emet_engine.state")
    home = tmp_path / "home"
    home.mkdir()
    (home / "pi-speakerphone.yaml").write_text(MANIFEST_YAML, encoding="utf-8")
    carried = b'{"state_version": "0.1"}\n'
    place = tmp_path / "xdg" / "emet" / "pi_speakerphone.json"
    place.parent.mkdir(parents=True)
    place.write_bytes(carried)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    bare = program(tmp_path / "bin" / "python3", Path(sys.executable).as_posix(), "-S", "-I")
    monkeypatch.setenv("PATH", str(bare.parent) + os.pathsep + os.environ["PATH"])
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    runner.fingerprint()
    assert runner.body == {
        "manifest": "~/pi-speakerphone.yaml", "manifest_sha256": None, "state": None, "state_sha256": None,
        "note": "the manifest was not hashed: PyYAML is not installed for the Python that read it"}
    fake_venv(home)
    runner.fingerprint()
    assert runner.body["note"] == ""
    assert runner.body["manifest_sha256"] == fingerprint(tmp_path, MANIFEST_YAML)[0]["manifest_hash"][0]
    assert runner.body["state_sha256"] == hashlib.sha256(carried).hexdigest()
    assert Path(runner.body["state"]) == place


# ------------------------------------------------------------- the tree


def test_as_committed_follows_git_autocrlf():
    assert run.as_committed(b"a\r\nb\r\n") == b"a\nb\n"
    assert run.as_committed(b"a\nb\n") == b"a\nb\n"
    assert run.as_committed(b"a\rb\r\n") == b"a\rb\r\n", "a lone CR makes git call it binary"
    assert run.as_committed(b"\0\r\n") == b"\0\r\n", "a NUL makes git call it binary"


def test_the_tree_goes_over_as_git_would_commit_it(tmp_path, laptop):
    body = FakeBody()
    make(tmp_path, laptop, body).run()
    (command, data), = body.sent
    assert "tar -xzf -" in command and "Documents" not in command
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        got = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    assert got == {"emet-sdk/a.py": b"print('a')\n", "tools/new.py": b"x = 1\n", "data.bin": b"\0\r\n"}
    source = record(tmp_path)["source"]
    assert source["changed"] == [] and source["added"] == ["tools/new.py"] and source["removed"] == ["gone.py"]
    assert source["crlf_converted"] == 2
    assert source["digest"] == run.tree_digest(got.items())


def test_a_tree_the_body_unpacked_differently_is_not_swapped_in(tmp_path, laptop):
    body = FakeBody({"swap": "digest=0000\n"})
    assert make(tmp_path, laptop, body).run() == 2
    assert "did not reach the body" in record(tmp_path)["verdict"]
    assert "install" not in body.steps()


def posix_bash() -> str:
    """A bash with GNU tools beside it, or a skip. On Windows that is Git's;
    the one in System32 starts WSL, which this suite never uses."""
    found = shutil.which("bash")
    if found is None or "system32" in found.lower() or "windowsapps" in found.lower():
        pytest.skip("no POSIX bash on this machine")
    return found


def test_the_body_digest_command_matches_the_laptops(tmp_path):
    """The body's find | sort | sha256sum pipeline, run here where those
    tools exist, gives `tree_digest`'s answer for the same files."""
    entries = {"b/x.py": b"1\n", "a-b.txt": b"2\n", "a/b.txt": b"3\n", "z.md": b"4\n", "A.txt": b"5\n"}
    for name, data in entries.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    done = subprocess.run([posix_bash(), "-s"], input=run.REMOTE_DIGEST.encode() + b"\n", cwd=tmp_path,
                          capture_output=True, timeout=60)
    if done.returncode != 0:
        pytest.skip(f"bash could not run the pipeline: {done.stderr[:80]!r}")
    assert done.stdout.decode().strip() == run.tree_digest(entries.items())


# --------------------------------------------- windows and the night


WINDOW = (dt.datetime(2026, 10, 28, 13, 0), dt.datetime(2026, 10, 28, 15, 0))


@pytest.mark.parametrize("now, seconds, says", [
    (dt.datetime(2026, 10, 28, 12, 59), 60, "the window opens"),
    (dt.datetime(2026, 10, 28, 14, 55), 650, "after the window closes"),
    (dt.datetime(2026, 10, 28, 15, 1), 60, "after the window closes"),
])
def test_a_hardware_step_refuses_outside_its_window(now, seconds, says):
    assert says in run.window_refusal(now, seconds, WINDOW, allow_night=False)


def test_a_hardware_step_inside_its_window_may_start():
    assert run.window_refusal(dt.datetime(2026, 10, 28, 14, 0), 650, WINDOW, allow_night=False) is None


def test_no_window_is_a_refusal():
    assert "no --window" in run.window_refusal(NOW, 60, None, allow_night=False)


NIGHT_WINDOW = (dt.datetime(2026, 10, 28, 6, 0), dt.datetime(2026, 10, 29, 6, 0))


@pytest.mark.parametrize("now, seconds", [
    (dt.datetime(2026, 10, 28, 23, 0), 60),
    (dt.datetime(2026, 10, 28, 7, 59), 60),
    (dt.datetime(2026, 10, 28, 21, 55), 650),  # starts in the day, ends at night
    (dt.datetime(2026, 10, 28, 22, 0), 10),
])
def test_a_hardware_step_refuses_at_night_unless_allowed(now, seconds):
    assert "between 22:00 and 08:00" in run.window_refusal(now, seconds, NIGHT_WINDOW, allow_night=False)
    assert run.window_refusal(now, seconds, NIGHT_WINDOW, allow_night=True) is None


def test_the_morning_starts_at_eight():
    assert run.window_refusal(dt.datetime(2026, 10, 28, 8, 0), 650, NIGHT_WINDOW, allow_night=False) is None
    assert run.window_refusal(dt.datetime(2026, 10, 28, 21, 49), 650, NIGHT_WINDOW, allow_night=False) is None


def test_a_window_that_ends_before_it_starts_is_refused():
    with pytest.raises(SystemExit):
        run.parse_args(["--release", "0.5.5", "--tree", "--window", "2026-10-28T15:00", "2026-10-28T14:00"])


# --------------------------------------- the microphone and the speaker


def window_args(start="2026-10-28T13:00", end="2026-10-28T15:00"):
    return ("--window", start, end)


def test_inside_a_window_each_hardware_step_runs_with_mocks_an_empty_keys_file_and_a_state_copy(tmp_path, laptop):
    body = FakeBody()
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    hardware = ("speaker-echo", "speaker-speak", "speaker-talk", "live-plain", "live-talk", "live-stats")
    assert hardware == run.HARDWARE_SUBSTEPS
    assert [s["name"] for s in record(tmp_path)["steps"] if s["name"] in hardware] == list(hardware), "in this order"
    assert all(steps[name]["status"] == "passed" for name in hardware)
    run_dir = f"{HOME}/emet-ci/runs/20261028-140000"
    for name in hardware:
        command = steps[name]["details"]["command"]
        assert f"--keys {run_dir}/empty-keys.env" in command
        assert f"--state {run_dir}/state" in command
        assert f"{run_dir}/body.yaml" in command and "pi-speakerphone.yaml" not in command
        assert "--transcribe" not in command and "--reply " not in command
        assert ("--replay" in command) is name.startswith("speaker-")
        assert "Documents/emet" not in body.script(name)
        assert f'COPY="$RUN/state/pi_speakerphone.json"' in body.script(name)
    assert "--stats" in steps["live-stats"]["details"]["command"]
    assert "timeout --foreground -s INT -k 30 --preserve-status 620" in steps["live-stats"]["details"]["command"]
    call = next(line for line in body.script("prepare").splitlines() if "<<'EMET_PY'" in line)
    models = json.loads(shlex.split(call.split("<<'EMET_PY'")[0])[-1])
    assert models == run.MODELS
    run.check_models(models)


def test_a_hardware_step_is_refused_when_the_robot_gets_busy(tmp_path, laptop):
    body = FakeBody({"busy": preflight_out(ps=QUIET_PS + LISTENING_PS, lock="held")})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    assert steps["live-stats"]["status"] == "refused" and "in use" in steps["live-stats"]["reason"]
    assert not any(s.startswith(("speaker-", "live-")) for s in body.steps())


def test_a_hardware_step_outside_the_window_is_refused_and_never_sent(tmp_path, laptop):
    body = FakeBody()
    clock = lambda: dt.datetime(2026, 10, 28, 15, 30)  # noqa: E731
    assert make(tmp_path, laptop, body, *window_args(), clock=clock).run() == 1
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    assert all(steps[n]["status"] == "refused" for n in run.HARDWARE_SUBSTEPS)
    assert not any(s.startswith(("speaker-", "live-")) for s in body.steps())


def test_each_hardware_step_reads_the_clock_when_it_starts(tmp_path, laptop):
    """A run that starts inside its window and reaches the hardware after the
    window closed: the suites took the time, and each step is refused."""
    late = dt.datetime(2026, 10, 28, 15, 30)
    readings = iter([NOW])  # the run's start; every later reading is late
    body = FakeBody()
    assert make(tmp_path, laptop, body, *window_args(), clock=lambda: next(readings, late)).run() == 1
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    assert record(tmp_path)["started"] == "2026-10-28T14:00:00"
    for name in run.HARDWARE_SUBSTEPS:
        assert steps[name]["status"] == "refused" and "after the window closes" in steps[name]["reason"], name
    assert not any(s.startswith(("speaker-", "live-")) for s in body.steps())


def test_a_hardware_step_keeps_a_margin_before_the_window_closes(tmp_path, laptop):
    """100 s before the window closes, the plain run's 80 s fit and its 80 s
    and the 30 s margin do not, so it is refused."""
    near_end = dt.datetime(2026, 10, 28, 14, 58, 20)
    assert run.PLAIN_S + run.BOOT_S < 100 < run.PLAIN_S + run.BOOT_S + run.MARGIN_S
    body = FakeBody()
    make(tmp_path, laptop, body, *window_args(), clock=lambda: near_end).run()
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "live-plain")
    assert step["status"] == "refused" and "after the window closes" in step["reason"]
    assert "live-plain" not in body.steps()


def test_the_live_step_runs_emet_talk_with_no_flags_for_a_minute(tmp_path, laptop):
    """`live-talk`, between the plain run and the ten minutes: emet-talk with
    nothing but where the keys and the state are, stopped by SIGINT after 60 s
    of listening and 20 s to boot, on the runner's manifest of mocks."""
    body = FakeBody()
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "live-talk")
    assert step["status"] == "passed" and step["reason"] == "emet-talk with no flags, 60 s on the microphone"
    run_dir = f"{HOME}/emet-ci/runs/20261028-140000"
    assert (run.PLAIN_S, run.BOOT_S) == (60, 20)
    assert shlex.split(step["details"]["command"]) == [
        "timeout", "--foreground", "-s", "INT", "-k", "30", "--preserve-status", "80",
        f"{HOME}/emet-ci/.venv/bin/emet-talk", f"{run_dir}/body.yaml",
        f"{HOME}/emet-ci/emet/emet-sdk/examples/emet-soul.yaml",
        "--keys", f"{run_dir}/empty-keys.env", "--state", f"{run_dir}/state"]
    assert run.command_flags(step["details"]["command"]) == {"--keys", "--state"}
    asked = body.steps()
    assert asked.index("live-plain") < asked.index("live-talk") < asked.index("live-stats")
    sent = body.script("live-talk")
    assert 'script -qae "$RUN/live-talk.typescript" -c ' in sent and "< /dev/null" in sent
    assert "Documents/emet" not in sent and "pi-speakerphone.yaml" not in sent


def test_emet_talk_keeps_its_bound_and_the_margin_inside_the_window(tmp_path, laptop):
    """109 s before the window closes its 80 s and the 30 s margin do not
    fit; 111 s before, they do."""
    for seconds_left, status in ((109, "refused"), (111, "passed")):
        clock = lambda: dt.datetime(2026, 10, 28, 15, 0) - dt.timedelta(seconds=seconds_left)  # noqa: E731
        where = tmp_path / str(seconds_left)
        body = FakeBody()
        make(where, laptop, body, *window_args(), clock=clock).run()
        step = next(s for s in record(where)["steps"] if s["name"] == "live-talk")
        assert step["status"] == status, seconds_left
        assert ("live-talk" in body.steps()) is (status == "passed")
        if status == "refused":
            assert "after the window closes" in step["reason"]


NIGHT_ARGS = ("--window", "2026-10-28T20:00", "2026-10-29T02:00")


@pytest.mark.parametrize("now", [
    dt.datetime(2026, 10, 28, 23, 0),
    dt.datetime(2026, 10, 28, 21, 58, 30),   # 110 s later it is 22:00:20
    dt.datetime(2026, 10, 29, 1, 0),
])
def test_emet_talk_is_refused_at_night_without_the_separate_yes(tmp_path, laptop, now):
    body = FakeBody()
    assert make(tmp_path, laptop, body, *NIGHT_ARGS, clock=lambda: now).run() == 1
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "live-talk")
    assert step["status"] == "refused" and "between 22:00 and 08:00" in step["reason"]
    assert "live-talk" not in body.steps(), "a refused step is never sent to the body"
    allowed = FakeBody()
    make(tmp_path / "allowed", laptop, allowed, *NIGHT_ARGS, "--allow-night", clock=lambda: now).run()
    step = next(s for s in record(tmp_path / "allowed")["steps"] if s["name"] == "live-talk")
    assert step["status"] == "passed" and "live-talk" in allowed.steps()


def test_emet_talk_may_run_in_the_last_two_minutes_of_the_evening(tmp_path, laptop):
    """At 21:58:00 its 110 s end at 21:59:50, inside the day; the replays
    and the ten minutes, which would end after 22:00, are refused."""
    body = FakeBody()
    make(tmp_path, laptop, body, *NIGHT_ARGS, clock=lambda: dt.datetime(2026, 10, 28, 21, 58, 0)).run()
    steps = {s["name"]: s["status"] for s in record(tmp_path)["steps"]}
    assert (steps["live-plain"], steps["live-talk"]) == ("passed", "passed")
    assert (steps["speaker-echo"], steps["live-stats"]) == ("refused", "refused")


STAND_IN = '''\
"""A stand-in for emet-listen and emet-talk on a body with no sound card:
the lines the runner reads, and the engine's own way down. `asyncio.run`
answers SIGINT by cancelling the task; the loop catches that, as
`emet_engine.cli` and `emet_engine.talk` do, and prints the end line.
Given `--events PATH` it writes the file's header first, as
`emet_engine.events.EventWriter` does when it is made, since the runner
fails a step whose events file is empty or missing."""
import asyncio, sys

name, args = sys.argv[1], sys.argv[2:]
replay, stats = "--replay" in args, "--stats" in args
source = "wav" if replay else "microphone"
if "--events" in args:
    with open(args[args.index("--events") + 1], "w", encoding="ascii") as events:
        events.write('{"format": "emet-events", "version": "1.0", "program": "%s"}\\n' % name)


async def run() -> int:
    if name == "emet-talk":
        print("Emet is listening for 'hey emet'")
        print(f"  wake     pocketsphinx on {source}, 16000 Hz")
        print("  words    mock")
    else:
        print("listening for 'hey emet'")
        print("  engine   pocketsphinx")
        print(f"  source   {source}")
    print("", flush=True)
    stopped = False
    try:
        await asyncio.sleep(0.2 if replay else 3600)
    except asyncio.CancelledError:
        stopped = True
        asyncio.current_task().uncancel()
    count = "0 exchange(s)." if name == "emet-talk" else "heard it 0 time(s)."
    print(f"\\n{'stopped' if stopped else 'source ended'}. {count}")
    if stats:
        print("\\n  frames        7655  (612.4s of audio)")
    return 0


sys.exit(asyncio.run(run()))
'''


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("script") or not shutil.which("timeout"),
                    reason="needs util-linux's script and coreutils' timeout, as the body has them")
def test_the_hardware_steps_own_shell_stops_a_live_program_and_keeps_its_typescript(tmp_path, laptop, monkeypatch):
    """Every hardware step's script as it is sent, run by a real bash, script
    and timeout, with stand-ins for the two programs and seconds for minutes.
    A live step is stopped by timeout's SIGINT, exits 0 and leaves its end
    line; the typescripts script wrote give every flag row. No sound card is
    there to open.

    A live step's bound here is 3 s, and the stand-in must have started
    within it: a SIGINT that lands while Python is still importing ends the
    program with no end line (seen 2026-11-26, with imports slowed to 2 s).
    On the body the bound holds 20 s for the boot.

    It runs on the machine's own `timeout`: GNU's on the body and in WSL,
    uutils' on GitHub's ubuntu-latest (Ubuntu 26.04), where a live step
    exited 137 with no end line until the command took `--foreground`
    (2026-11-26)."""
    for constant, seconds in (("PLAIN_S", 2), ("STATS_S", 2), ("BOOT_S", 1), ("REPLAY_LIMIT_S", 20)):
        monkeypatch.setattr(run, constant, seconds)
    home = tmp_path / "home"
    (home / "emet-ci" / "emet").mkdir(parents=True)
    (home / "state.json").write_text("{}\n", encoding="utf-8")
    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    for name in ("emet-listen", "emet-talk"):
        program(home / "emet-ci" / ".venv" / "bin" / name, sys.executable, str(stand_in), name)
    runner = make(tmp_path, laptop, BashBody(home), *window_args())
    runner.home = shell_home(home)
    Path(runner.run_dir, "state").mkdir(parents=True)  # the prepare step makes it on the body
    passed = []
    for name, what, command, bound in runner.hardware_plan():
        step = runner.hardware_step(name, what, command, "pi_speakerphone", f"{runner.home}/state.json")
        assert step.status == "passed", (name, step.reason, step.details)
        assert step.details["code"] == "0" and step.details["mixer"] == "same"
        if name.startswith("live-"):
            assert bound == 3 and 2.0 <= step.seconds < 30, "stopped at its bound, by the signal"
        text = Path(runner.run_dir, f"{name}.typescript").read_text(encoding="utf-8", errors="replace")
        passed.append((name, text))
    for name in run.EVENTS_STEPS:
        written = Path(runner.run_dir, f"{name}{run.EVENTS_SUFFIX}").read_text(encoding="ascii")
        assert written.startswith('{"format": "emet-events"'), (name, written)
    talk_text = dict(passed)["live-talk"]
    assert "stopped. 0 exchange(s)." in talk_text and talk_text.startswith("Script started on ")
    assert run.flags_exercised(passed) == EVERY_FLAG


def test_the_window_arithmetic_in_the_documents_is_the_plans(tmp_path, laptop):
    runner = make(tmp_path, laptop, FakeBody(), *window_args())
    runner.home = HOME
    bounds = {name: bound for name, _what, _command, bound in runner.hardware_plan()}
    assert bounds == {"speaker-echo": 300, "speaker-speak": 300, "speaker-talk": 300,
                      "live-plain": 80, "live-talk": 80, "live-stats": 620}
    assert sum(bounds.values()) == 1680 and sum(bounds.values()) + run.MARGIN_S == 28.5 * 60
    readme = RUN_PY.with_name("README.md").read_text(encoding="utf-8")
    for text in (readme, run.__doc__):
        flowed = " ".join(text.split())  # a number may sit at a line's end
        assert "1,680 s" in flowed and "28 and a half minutes" in flowed
    for name, bound in bounds.items():
        assert re.search(rf"\| `{name}` \|.*\| {bound} s \|", readme), f"the README's table gives {name} {bound} s"
    # What comes before the hardware steps takes the window too, so the
    # README names each step that runs before them, and those after.
    window = " ".join(readme[readme.index("- **How long a window.**"):readme.index("- **No audio kept or sent.**")].split())
    first = run.STEPS[:run.STEPS.index("speaker")]
    assert all(f"the {name}" in window for name in first), (first, window)
    words = {"netcut": "network cut", "soak": "soak"}
    after = [words[s] for s in run.STEPS[run.STEPS.index("live") + 1:]]
    assert f"The {after[0]} and the {after[1]} run after the speaker and the microphone" in window


def test_a_manifest_that_changed_after_its_fingerprint_stops_every_hardware_step(tmp_path, laptop):
    other = "0c" * 32
    body = FakeBody({"prepare": f"manifest_hash={other}\nbody=pi_speakerphone\nstate={STATE_FILE}\n"
                                f"models={json.dumps(run.MODELS)}\n"})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    said = record(tmp_path)
    for step in said["steps"]:
        if step["name"] in run.HARDWARE_SUBSTEPS:
            assert step["status"] == "failed", step["name"]
            assert step["reason"] == ("the body's manifest changed during the run: its fingerprint is "
                                      f"{MANIFEST_SHA[:12]}, and the hardware steps read {other[:12]}")
    assert not any(s.startswith(("speaker-", "live-")) for s in body.steps())
    assert said["body"]["manifest_sha256"] == MANIFEST_SHA and said["flags_run"] == []


def test_a_fingerprint_that_could_not_be_read_does_not_stop_the_hardware_steps(tmp_path, laptop):
    body = FakeBody({"fingerprint": "note=the manifest was not hashed: PyYAML is not installed for the "
                                    "Python that read it\ncode=0\n"})
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    assert record(tmp_path)["body"]["manifest_sha256"] is None
    assert "live-talk" in body.steps()


def test_the_runners_manifest_is_hashed_before_its_models_are_replaced(tmp_path, laptop):
    """The Python the prepare step sends, run here on a manifest whose models
    name a vendor: the hash it prints is the fingerprint's of the file as its
    owner wrote it, and the manifest it writes has the mocks."""
    yaml = pytest.importorskip("yaml")
    pytest.importorskip("emet_engine.state")
    body = FakeBody()
    make(tmp_path, laptop, body, *window_args()).run()
    sent = body.script("prepare")
    python = sent.split("<<'EMET_PY'\n", 1)[1].rsplit("\nEMET_PY", 1)[0]
    assert python.index('print("manifest_hash=" + canonical_sha256(doc))') < python.index('doc["models"] =')
    work = tmp_path / "prepare"
    work.mkdir()
    (work / "prepare.py").write_text(python, encoding="utf-8")
    (work / "pi-speakerphone.yaml").write_text(MANIFEST_YAML, encoding="utf-8")
    done = subprocess.run([sys.executable, str(work / "prepare.py"), str(work / "pi-speakerphone.yaml"),
                           str(work / "body.yaml"), json.dumps(run.MODELS)], capture_output=True, timeout=60,
                          env=dict(os.environ, XDG_STATE_HOME=str(work / "xdg")))
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    said = run.Result(0, done.stdout.decode("utf-8"))
    as_written = fingerprint(tmp_path, MANIFEST_YAML)[0]["manifest_hash"][0]
    assert said.value("manifest_hash") == as_written
    written = yaml.safe_load((work / "body.yaml").read_text(encoding="utf-8"))
    assert written["models"] == run.MODELS and said.value("body") == "pi_speakerphone"
    # Whether this venv's engine has --events, as the hardware steps read it.
    assert said.value("events") == ("yes" if importlib.util.find_spec("emet_engine.events") else "no")
    assert {k: v for k, v in written.items() if k != "models"} == {
        k: v for k, v in yaml.safe_load(MANIFEST_YAML).items() if k != "models"}
    rewritten = fingerprint(tmp_path, (work / "body.yaml").read_text(encoding="utf-8"))[0]["manifest_hash"][0]
    assert rewritten != as_written, "the runner's own manifest is another body, and is never the one hashed"
    # A manifest that does not parse: the kind of the error, and none of its words.
    (work / "pi-speakerphone.yaml").write_text("audio: {device: a-word-of-the-manifest, gain: [unclosed\n",
                                               encoding="utf-8")
    done = subprocess.run([sys.executable, str(work / "prepare.py"), str(work / "pi-speakerphone.yaml"),
                           str(work / "body.yaml"), json.dumps(run.MODELS)], capture_output=True, timeout=60)
    assert done.returncode == 3 and done.stdout.decode("utf-8").startswith("problem=")
    assert b"a-word-of-the-manifest" not in done.stdout + done.stderr


@pytest.mark.parametrize("answer, reason", [
    # The Python said what kind of error stopped it; its stderr is left on the body.
    (run.Result(3, "problem=ParserError\n", "Traceback: ... : a-word-of-the-manifest, gain: [unclosed"),
     "the runner's manifest could not be written: ParserError"),
    # The shell failed before any Python ran, and says so itself.
    (run.Result(127, "", "bash: line 12: /home/alex/emet-ci/.venv/bin/python: No such file or directory\n"),
     "the runner's manifest could not be written: exit 127: bash: line 12: "
     "/home/alex/emet-ci/.venv/bin/python: No such file or directory"),
], ids=["the-python-said-its-kind", "the-shell-failed-first"])
def test_a_manifest_that_could_not_be_rewritten_is_reported_without_its_words(tmp_path, laptop, answer, reason):
    body = FakeBody({"prepare": answer})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    said = (tmp_path / "out" / "run.json").read_text(encoding="utf-8")
    assert "a-word-of-the-manifest" not in said
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    assert all(steps[name]["status"] == "failed" and steps[name]["reason"] == reason for name in run.HARDWARE_SUBSTEPS)
    assert not any(s.startswith(("speaker-", "live-")) for s in body.steps())


@pytest.mark.parametrize("extra, problem", [
    ("audio=/home/alex/emet-ci/runs/x/turn-0001.raw\n", "audio was written"),
    ("state_after=def\n", "carried state file changed"),
    ("ended=0\n", "no end line"),
    ("mixer=changed\n", "mixer settings changed"),
    ("code=130\n", "exited 130"),
])
def test_a_hardware_step_that_breaks_a_rule_fails(tmp_path, laptop, extra, problem):
    body = FakeBody({"live-plain": HARDWARE_OK + extra})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "live-plain")
    assert step["status"] == "failed" and problem in step["reason"]


def test_a_manifest_the_body_wrote_with_a_vendor_stops_every_hardware_step(tmp_path, laptop):
    vendor = dict(run.MODELS, stt={"provider": "deepgram", "model": "nova-3"})
    body = FakeBody({"prepare": f"body=pi_speakerphone\nstate={HOME}/s.json\nmodels={json.dumps(vendor)}\n"})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    assert steps["speaker-echo"]["status"] == "failed" and "deepgram" in steps["speaker-echo"]["reason"]
    assert not any(s.startswith(("speaker-", "live-")) for s in body.steps())


def test_the_models_guard():
    run.check_models(run.MODELS)
    run.check_models(dict(run.MODELS, tts=run.PIPER_TTS))
    for bad, says in [
        (None, "no models block"),
        (dict(run.MODELS, stt={"provider": "deepgram"}), "models.stt"),
        (dict(run.MODELS, chat={"provider": "openai"}), "models.chat"),
        (dict(run.MODELS, tts={"provider": "deepgram"}), "models.tts"),
        ({k: v for k, v in run.MODELS.items() if k != "chat"}, "models.chat"),
        (dict(run.MODELS, stt={"provider": "mock", "params": {"record_turns": "/tmp/t"}}), "record_turns"),
    ]:
        with pytest.raises(run.RunnerError, match=says):
            run.check_models(bad)


# ------------------------------------------------------------- collect


def test_collect_keeps_text_and_refuses_audio(tmp_path, laptop):
    files = {"./suite-emet-sdk.txt": b"226 passed\n", "./live-plain.typescript": b"x\n", "./turn.wav": b"RIFF"}
    body = FakeBody({"collect": run.Result(0, "", raw=tar_of(files))})
    assert make(tmp_path, laptop, body).run() == 1
    out = tmp_path / "out"
    assert (out / "suite-emet-sdk.txt").read_bytes() == b"226 passed\n"
    assert not (out / "turn.wav").exists()
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "collect")
    assert step["status"] == "failed" and "turn.wav is audio" in step["reason"]


def test_the_runners_manifests_stay_on_the_body(tmp_path, laptop):
    """The collect's tar through a real bash, over a run folder as the
    budgets, the soak and the hardware steps leave it: the runner's
    manifests are the maintainer's with two blocks changed, so none of them
    comes home, and neither do the state copies or the keys file."""
    home = tmp_path / "home"
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    folder = home / "emet-ci" / "runs" / runner.stamp
    for name in ("body.yaml", "budget/listen.yaml", "budget/speak.yaml", "soak/soak.yaml", "suite-emet-sdk.txt",
                 "soak/samples.txt", "empty-keys.env", "state/pi_speakerphone.json"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("audio: {gain_db: -6.0}\n" if name.endswith(".yaml") else "x\n", encoding="utf-8")
    kept, problems = runner.fetch_tar("collect", runner.run_dir, tmp_path / "out")
    assert problems == [] and sorted(kept) == ["soak/samples.txt", "suite-emet-sdk.txt"], kept
    assert not list((tmp_path / "out").rglob("*.yaml"))


@pytest.mark.parametrize("path", [
    "~/.config/emet/keys.env", "~/.ssh/id_ed25519", "/etc/emet/keys.env", "~/sitting-keys.log",
    "~/pi-token.log", "~/sitting-0.5.5.env.log", "~/notes.log", "~/emet-ci/runs/x/report.txt",
    "~/sitting.wav", "~/sitting-x.wav", "/etc/passwd", "~/../alex/pi-x.log", "~/sitting a.log", "~/pi-x;rm.log",
])
def test_a_fetch_takes_only_the_body_typescripts(tmp_path, laptop, path):
    body = FakeBody()
    runner = make(tmp_path, laptop, body)
    runner.home = HOME
    problems: list[str] = []
    assert runner.fetch(path, tmp_path, problems) is None
    assert problems and "fetch-file" not in body.steps()


@pytest.mark.parametrize("path", ["~/sitting-0.5.5.log", "~/pi-0.5.0-rerun.log"])
def test_a_fetched_typescript_comes_home(tmp_path, laptop, path):
    body = FakeBody({"fetch-file": "Script started on 2026-10-28 14:10:00-07:00 [COMMAND=\"emet-talk a b\"]\n"})
    assert make(tmp_path, laptop, body, "--fetch", path).run() == 0
    name = path.removeprefix("~/")
    assert (tmp_path / "out" / name).read_text().startswith("Script started")
    assert f"f={HOME}/{name}" in body.script("fetch-file")


@pytest.mark.parametrize("ref", ["x; rm -rf ~", "../master", "$(id)", "-oProxyCommand=x", "a b",
                                 "pull/12", "pull/12/head", "refs/pull/12/head", "pulls/3"])
def test_a_commit_that_is_not_a_plain_name_is_refused(ref):
    with pytest.raises(SystemExit):
        run.parse_args(["--release", "0.5.5", "--commit", ref])


def test_a_commit_only_a_pull_request_holds_is_refused(tmp_path, laptop, monkeypatch):
    """The fetch script, run by a real bash and git against a stand-in for
    the public repository with a branch and a pull request. The runner's
    clone already holds the pull request's commit, as an older clone that
    fetched pull requests would; the commit is still refused, and the
    branch's is taken. Both repositories are temporary folders."""
    git = shutil.which("git") or pytest.skip("no git on this machine")
    up = tmp_path / "up"

    def g(*args: str, cwd: Path = up) -> str:
        return subprocess.run([git, *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(text: str, parents: list[str]) -> str:
        # Bytes in, so no platform turns a line feed into CR LF on the way.
        blob = subprocess.run([git, "hash-object", "-w", "--stdin"], cwd=up, input=text.encode(), check=True,
                              capture_output=True).stdout.decode().strip()
        tree = subprocess.run([git, "mktree"], cwd=up, input=f"100644 blob {blob}\ta.txt\n".encode(), check=True,
                              capture_output=True).stdout.decode().strip()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        return subprocess.run([git, "commit-tree", tree, "-m", text, *[a for p in parents for a in ("-p", p)]],
                              cwd=up, env=env, check=True, capture_output=True, text=True).stdout.strip()

    up.mkdir()
    g("init", "-q", "--bare", str(up), cwd=tmp_path)
    branch = commit("main\n", [])
    pull = commit("a stranger's change\n", [branch])
    g("update-ref", "refs/heads/main", branch)
    g("update-ref", "refs/pull/1/head", pull)
    home = tmp_path / "home"
    clone = home / "emet-ci" / "repo.git"
    clone.parent.mkdir(parents=True)
    g("init", "-q", "--bare", str(clone), cwd=tmp_path)
    g("fetch", "-q", up.as_posix(), "+refs/pull/*/head:refs/remotes/origin/pull/*", cwd=clone)
    monkeypatch.setattr(run, "UPSTREAM", up.as_posix())
    for ref, taken in ((pull, False), (branch, True), ("main", True)):
        args = run.parse_args(["--release", "0.5.5", "--commit", ref, "--out", str(tmp_path / "out")])
        runner = run.BodyRun(args, remote=BashBody(home), laptop=laptop, clock=lambda: NOW,
                             log_check=tmp_path / "none.py", history=[])
        step = runner.sync_commit()
        assert (step.status == "passed") is taken, (ref, step.reason)
        if taken:
            assert runner.source["head"] == branch
            assert (home / "emet-ci" / "emet" / "a.txt").read_text() == "main\n"
        else:
            assert "pull requests are not run on the body" in step.reason


def test_a_pushed_commit_is_fetched_into_the_runners_own_clone(tmp_path, laptop):
    body = FakeBody({"fetch": "sha=eb55748\nsubject=0.5.4: Keep\ndigest=d1\n",
                     "swap": "digest=d1\nswapped=yes\n"})
    args = run.parse_args(["--release", "0.5.5", "--commit", "eb55748", "--out", str(tmp_path / "out")])
    # A releases/ of the test's own, as make() gives, so the budgets this
    # run takes never read the laptop's real evidence.
    runner = run.BodyRun(args, remote=body, laptop=laptop, clock=lambda: NOW,
                         log_check=tmp_path / "none.py", history=[], sleep=lambda s: None, top=tmp_path / "top")
    assert runner.run() == 0
    assert "budget-run" in body.steps() and runner.top == tmp_path / "top"
    script = body.script("fetch")
    assert run.UPSTREAM in script
    for line in script.splitlines():
        if "git " in line or "$GIT" in line:
            assert "repo.git" in line or "$GIT" in line, line
    assert not body.sent


# ------------------------------------------------------- the flag table


def session(when: str, command: str, lines: list[str]) -> str:
    """One `script -c` session, as util-linux 2.41 writes it, CR LF and all."""
    body = "\r\n".join(lines)
    return (f'Script started on {when}-07:00 [COMMAND="{command}" TERM="xterm-256color"]\n'
            f"{body}\r\n\r\nScript done on {when}-07:00 [COMMAND_EXIT_CODE=\"0\"]\n")


def listen(*extra: str, source: str = "microphone", audio: float | None = None, ended: bool = True) -> list[str]:
    lines = ["listening for 'hey emet'", "  engine   pocketsphinx", f"  source   {source}"]
    if "--transcribe" in extra or "--reply" in extra or "--speak" in extra:
        lines.append("  stt      mock, model none (streaming)")
    lines += ["  keys     none", "", "  heard 'hey emet'  (confidence 1.00)"]
    if ended:
        lines += ["stopped. heard it 1 time(s).", ""]
        if audio is not None:
            lines += [f"  frames        {int(audio * 12.5)}  ({audio:.1f}s of audio)",
                      "  stt final     mean 127 ms   max 127", "  verdict       kept up"]
    return lines


def talk(*extra: str, ended: bool = True) -> list[str]:
    lines = ["Emet is listening for 'hey emet'", "  wake     pocketsphinx on microphone, 16000 Hz",
             "  words    deepgram, nova-3", "", "  (heard 'hey emet')"]
    return lines + (["stopped. 1 exchange(s)."] if ended else [])


def test_the_flag_table_reads_each_flag_from_the_logs():
    log = "".join([
        session("2026-09-24 20:35:26", "emet-listen m s --stats", listen("--stats", audio=656.1)),
        session("2026-10-26 22:28:18", "emet-listen m s --stats", listen("--stats", audio=24.6)),
        session("2026-10-25 10:00:00", "emet-listen m s --echo", listen("--echo")),
        session("2026-10-27 14:05:00", "timeout -s INT 300 /x/.venv/bin/emet-listen m s --replay w.wav --echo --stats",
                listen("--echo", source="wav", audio=130.0)),
        session("2026-10-26 22:31:12", "emet-listen m s --speak --stats", listen("--speak", audio=8.9)),
        session("2026-10-27 09:00:00", "emet-talk m s --stats", talk()),
        session("2026-10-20 09:00:00", "emet-talk m s", talk()),
        session("2026-10-27 11:00:00", "emet-listen m s --keys k --state st", listen()),
        session("2026-10-28 11:00:00", "emet-listen m s", listen(ended=False)),
        session("2026-10-28 12:00:00", "emet-talk m s", talk(ended=False)),
    ])
    rows = {r["flag"]: r for r in run.flag_table(run.logged_runs(log, "x.log"))}
    assert (rows["a plain run"]["live"], rows["a plain run"]["replay"]) == ("2026-10-27", None)
    assert rows["--replay"]["replay"] == "2026-10-27" and rows["--replay"]["live"] is None
    assert rows["--stats, live, ten minutes"]["live"] == "2026-09-24"
    assert (rows["--echo"]["live"], rows["--echo"]["replay"]) == ("2026-10-25", "2026-10-27")
    for flag in ("--transcribe", "--reply", "--speak"):
        assert rows[flag]["live"] == "2026-10-26", flag
        assert rows[flag]["with"] == "stt mock", "the cited run's providers come from its header only"
    assert rows["emet-talk with no flags"]["live"] == "2026-10-20"
    assert rows["--echo"]["where"].startswith("x.log:")


def test_a_run_typed_at_a_prompt_takes_its_command_from_the_prompt():
    log = ("Script started on 2026-09-10 21:00:00-07:00 [TERM=\"xterm\"]\n"
           "alex@emet-pi:~/Documents/emet $ emet-listen m s --transcribe\n"
           + "\n".join(listen("--transcribe")) + "\n")
    rows = {r["flag"]: r for r in run.flag_table(run.logged_runs(log, "y.log"))}
    assert rows["--transcribe"]["live"] == "2026-09-10"
    assert rows["a plain run"]["live"] is None


def test_the_0_5_4_logs_dates_are_the_pr_bodys():
    """The body table in the 0.5.4 pull request, read back from its log."""
    log = run.TOP / "releases" / "v0.5.4" / "pi-0.5.4.log"
    if not log.is_file():
        pytest.skip("the 0.5.4 log is outside the repository, on the maintainer's laptop")
    rows = {r["flag"]: r for r in run.flag_table(run.logged_runs(log.read_text(encoding="utf-8", errors="replace"), "l"))}
    assert rows["a plain run"]["live"] == "2026-10-07"
    assert rows["--replay"]["replay"] == "2026-10-07"
    assert rows["--stats, live, ten minutes"]["live"] is None
    for flag in ("--echo", "--transcribe", "--reply", "--speak", "emet-talk with no flags"):
        assert rows[flag]["live"] == "2026-10-26", flag


# ------------------------------------------- the flags a run itself ran


def sent_command(script: str) -> str:
    """The command a hardware step's script hands to `script -c`."""
    line = next(line for line in script.splitlines() if line.startswith("script -qae "))
    words = shlex.split(line)
    return words[words.index("-c") + 1]


def typescript(command: str, stats_audio: float) -> str:
    """What the body's `script` would have kept of this command: the header
    with the command as it was sent, the boot header, and the end line."""
    replay = "--replay" in command.split()
    source = "wav" if replay else "microphone"
    if "/emet-talk " in command:
        lines = ["Emet is listening for 'hey emet'", f"  wake     pocketsphinx on {source}, 16000 Hz",
                 "  words    mock", "  answers  mock", "  voice    mock (16000 Hz)", "  patience 900 ms", "",
                 "source ended. 1 exchange(s)." if replay else "stopped. 0 exchange(s)."]
    else:
        flags = [flag for flag in ("--echo", "--speak", "--stats") if flag in command.split()]
        audio = (130.0 if replay else stats_audio) if "--stats" in flags else None
        lines = listen(*flags, source=source, audio=audio)
    return session("2026-10-28 14:05:00", command, lines)


def windowed_body(answers: dict | None = None, *, stats_audio: float = 612.4, lost: tuple[str, ...] = ()) -> FakeBody:
    """A stand-in body whose collect step brings home a typescript for each
    hardware sub-step it was sent, written from the command it was sent.
    `lost` names typescripts that do not come back."""
    body = FakeBody(answers)

    def collect(script: str):
        files = {"./suite-emet-sdk.txt": b"226 passed\n"}
        for name in run.HARDWARE_SUBSTEPS:
            if name in body.steps() and name not in lost:
                files[f"./{name}.typescript"] = typescript(sent_command(body.script(name)), stats_audio).encode()
        return run.Result(0, "", raw=tar_of(files))

    body.answers["collect"] = collect
    return body


EVERY_FLAG = [
    {"flag": "a plain run", "how": "live", "step": "live-plain"},
    {"flag": "--replay", "how": "replay", "step": "speaker-echo"},
    {"flag": "--stats, live, ten minutes", "how": "live", "step": "live-stats"},
    {"flag": "--echo", "how": "replay", "step": "speaker-echo"},
    {"flag": "--transcribe", "how": "replay", "step": "speaker-speak"},
    {"flag": "--reply", "how": "replay", "step": "speaker-speak"},
    {"flag": "--speak", "how": "replay", "step": "speaker-speak"},
    {"flag": "emet-talk with no flags", "how": "live", "step": "live-talk"},
]
TEN_MINUTES = EVERY_FLAG[2]


def test_one_windowed_run_exercises_every_flag_row(tmp_path, laptop):
    """`flags_run`, read from the typescripts of the commands the runner
    itself sent: every row of FLAG_ROWS, a live-only row as live, and each
    naming the sub-step, as it stands in `steps`, that showed it."""
    body = windowed_body()
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    said = record(tmp_path)
    assert said["flags_run"] == EVERY_FLAG
    assert [entry["flag"] for entry in said["flags_run"]] == [row.label for row in run.FLAG_ROWS]
    for row in run.FLAG_ROWS:
        how = [entry["how"] for entry in said["flags_run"] if entry["flag"] == row.label]
        assert how == (["live"] if row.live_only else ["replay"]), row.label
    passed = {step["name"] for step in said["steps"] if step["status"] == "passed"}
    assert {entry["step"] for entry in said["flags_run"]} <= passed & set(run.HARDWARE_SUBSTEPS)
    report = (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")
    assert "  flag this run exercised      how         step" in report
    assert "  emet-talk with no flags      live        live-talk" in report


def test_a_step_that_failed_adds_no_flag_though_its_typescript_came_home(tmp_path, laptop):
    body = windowed_body({"live-stats": HARDWARE_OK + "code=130\n"})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    assert (tmp_path / "out" / "live-stats.typescript").is_file()
    assert record(tmp_path)["flags_run"] == [entry for entry in EVERY_FLAG if entry is not TEN_MINUTES]


def test_ten_minutes_of_stats_that_read_less_audio_add_no_flag_though_the_step_passed(tmp_path, laptop):
    """The flag table counts the ten minutes only from a footer that read
    600 s, and `flags_run` counts as it does."""
    body = windowed_body(stats_audio=599.9)
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    said = record(tmp_path)
    assert {s["name"]: s["status"] for s in said["steps"]}["live-stats"] == "passed"
    assert said["flags_run"] == [entry for entry in EVERY_FLAG if entry is not TEN_MINUTES]


def test_a_run_whose_hardware_steps_were_refused_exercised_nothing(tmp_path, laptop):
    body = windowed_body()
    late = lambda: dt.datetime(2026, 10, 28, 15, 30)  # noqa: E731
    assert make(tmp_path, laptop, body, *window_args(), clock=late).run() == 1
    assert record(tmp_path)["flags_run"] == []


def test_a_run_whose_hardware_steps_all_failed_exercised_nothing(tmp_path, laptop):
    """Each ran on the body and left a whole typescript, and each broke a
    rule: the mixer changed. None is counted."""
    body = windowed_body({name: HARDWARE_OK + "mixer=changed\n" for name in run.HARDWARE_SUBSTEPS})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    said = record(tmp_path)
    assert all(s["status"] == "failed" for s in said["steps"] if s["name"] in run.HARDWARE_SUBSTEPS)
    assert sorted(p.name for p in (tmp_path / "out").glob("*.typescript")) == sorted(
        f"{name}.typescript" for name in run.HARDWARE_SUBSTEPS)
    assert said["flags_run"] == []


def test_a_typescript_this_run_did_not_bring_home_is_never_counted(tmp_path, laptop):
    """The plain run passed on the body and its typescript did not come
    back, while one from an earlier run sits in the folder `--out` names.
    The flag table of all history reads the old file; `flags_run` does not."""
    out = tmp_path / "out"
    out.mkdir()
    old = session("2026-10-20 09:00:00", "emet-listen b s --keys k --state st", listen())
    (out / "live-plain.typescript").write_text(old, encoding="utf-8")
    body = windowed_body(lost=("live-plain",))
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    said = record(tmp_path)
    assert {s["name"]: s["status"] for s in said["steps"]}["live-plain"] == "passed"
    assert {row["flag"]: row["live"] for row in said["flags"]}["a plain run"] == "2026-10-20"
    assert said["flags_run"] == EVERY_FLAG[1:]


def test_a_flag_shown_live_and_from_a_file_has_an_entry_for_each():
    replayed = session("2026-10-28 14:00:00", "emet-listen m s --replay w.wav --echo", listen("--echo", source="wav"))
    live = session("2026-10-28 14:05:00", "emet-listen m s --echo", listen("--echo"))
    assert run.flags_exercised([("speaker-echo", replayed), ("live-plain", live)]) == [
        {"flag": "--replay", "how": "replay", "step": "speaker-echo"},
        {"flag": "--echo", "how": "live", "step": "live-plain"},
        {"flag": "--echo", "how": "replay", "step": "speaker-echo"},
    ]
    unfinished = session("2026-10-28 14:10:00", "emet-talk m s", talk(ended=False))
    assert run.flags_exercised([("live-talk", unfinished)]) == []
    assert run.flags_exercised([]) == []


def test_emet_talk_from_a_file_is_no_run_of_emet_talk_with_no_flags():
    """`speaker-talk` replays a file under `--stats`, so it shows no row:
    the row is the live run alone, which is why the live step has one."""
    command = "timeout -s INT 300 /x/.venv/bin/emet-talk b s --replay w.wav --stats --keys k --state st"
    assert run.flags_exercised([("speaker-talk", typescript(command, 0.0))]) == []


# ------------------------------------------------------- the real ssh


def test_every_body_command_starts_by_unsetting_each_emet_variable(monkeypatch):
    seen = {}

    def fake_run(args, input, capture_output, timeout):
        seen["args"], seen["input"] = args, input
        return subprocess.CompletedProcess(args, 0, b"ok\n", b"")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run.Remote("emet-pi").run("echo hi\n")
    assert seen["args"][:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "emet-pi"]
    assert seen["input"].decode().startswith(run.PRELUDE)
    assert 'unset "$name"' in run.PRELUDE and "EMET_" in run.PRELUDE


def test_ssh_failing_to_connect_is_unreachable(monkeypatch):
    monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 255, b"", b"ssh: connect to host emet-pi.local port 22: Connection timed out\n"))
    with pytest.raises(run.Unreachable, match="timed out"):
        run.Remote("emet-pi").run("true\n")


def test_the_prelude_unsets_what_it_finds(tmp_path):
    """The prelude, run by a real bash with a key exported, leaves no EMET_
    variable for the script after it."""
    script = run.PRELUDE + 'echo "set=$EMET_SET"; env | grep -c "^EMET_" || true\n'
    env = dict(os.environ, HOME=str(tmp_path), EMET_DEEPGRAM_KEY="not-a-real-key", EMET_VIRTUAL_AUDIO="1")
    done = subprocess.run([posix_bash(), "-s"], input=script.encode(), capture_output=True, env=env, timeout=60)
    out = done.stdout.decode()
    assert "EMET_DEEPGRAM_KEY" in out and "EMET_VIRTUAL_AUDIO" in out
    assert out.strip().splitlines()[-1] == "0"
    assert "not-a-real-key" not in out


def test_the_prelude_sets_every_pip_configuration_file_aside(tmp_path):
    """The Pi's /etc/pip.conf adds piwheels as an extra index, so every pip
    the runner starts reads no configuration file and installs from PyPI
    alone (ledger L53). The prelude's value wins over the environment's,
    and it is the path pip compares with `os.devnull` on the body."""
    assert "\nexport PIP_CONFIG_FILE=/dev/null\n" in run.PRELUDE
    script = run.PRELUDE + 'echo "pip_config=$PIP_CONFIG_FILE"\n'
    env = dict(os.environ, HOME=str(tmp_path), PIP_CONFIG_FILE="/etc/pip.conf", MSYS_NO_PATHCONV="1")
    done = subprocess.run([posix_bash(), "-s"], input=script.encode(), capture_output=True, env=env, timeout=60)
    assert done.stdout.decode().strip().splitlines()[-1] == "pip_config=/dev/null"


def test_throttle_bits_are_named():
    assert run.throttle_meaning("throttled=0x0") == ["none"]
    assert run.throttle_meaning("throttled=0x50005") == [
        "under-voltage now", "throttled now", "under-voltage has occurred", "throttled has occurred"]


def test_the_throttle_names_are_the_footers():
    """The runner keeps its own copy of the names, and they are board.py's."""
    board = pytest.importorskip("emet_engine.board")
    assert run.THROTTLE_BITS == board.THROTTLE_BITS


def test_the_runner_needs_only_the_standard_library():
    """With site-packages off (`-S`), nothing but the standard library can be
    imported, and the runner still starts."""
    done = subprocess.run([sys.executable, "-S", "-I", str(RUN_PY), "--help"], capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-400:]
    assert b"--window" in done.stdout


def test_junit_counts_collected_passed_and_skipped():
    counts = run.junit_counts(junit(117, skipped=1))
    assert (counts.collected, counts.passed, counts.skipped, counts.failed) == (117, 116, 1, 0)
    assert run.junit_counts("not xml") is None


# ------------------------------------------- the install line (batch C)


def test_the_install_line_holds_the_provider_extras_and_piper(tmp_path, laptop):
    """The runner's venv is the maintainer's: the provider extras and piper,
    so its providers suite skips what the maintainer's skips. `--voice
    piper` asks for no other line, so it never reinstalls."""
    assert run.INSTALL[2] == "emet-providers[dev,deepgram,anthropic,openai,piper]"
    sent = []
    for voice in ("mock", "piper"):
        body = FakeBody()
        make(tmp_path / voice, laptop, body, "--voice", voice).run()
        sent.append(body.script("install"))
        step = next(s for s in record(tmp_path / voice)["steps"] if s["name"] == "install")
        assert step["details"]["extras"] == list(run.INSTALL)
    assert "-e 'emet-providers[dev,deepgram,anthropic,openai,piper]'" in sent[0]
    assert [re.sub(r"RUN=\S+", "", text) for text in sent][0] == re.sub(r"RUN=\S+", "", sent[1]), \
        "the same install, so the same key, whichever voice"


def test_no_new_step_can_reach_a_vendor(tmp_path, laptop):
    """Every program the budget and the soak start names an empty keys file
    the step made, and runs after the prelude that unsets each EMET_
    variable: no key reaches a run, so no vendor is called."""
    body = FakeBody({"soak-poll": "done=yes\n", "soak-read": "", "soak-start": "started=4242\n",
                     "soak-prepare": prepared(("soak/soak.yaml", run.MODELS), extra="wavs=13\n")})
    make(tmp_path, laptop, body, "--soak", "60").run()
    for step in ("budget-prepare", "soak-prepare"):
        assert ': > "$F/empty-keys.env"' in body.script(step)
    budget = body.script("budget-run")
    assert budget.count('--keys "$B/empty-keys.env"') == 3
    assert run.SOAK_SH.count("--keys empty-keys.env") == 2
    for line in budget.splitlines() + run.SOAK_SH.splitlines():
        assert "keys.env" not in line or "empty-keys.env" in line, line


# ------------------------------------------------------ the audit (BVP2)


@pytest.mark.parametrize("answer, status, says, findings", [
    (AUDIT_CLEAN, "ran", "no known vulnerability in 58 package(s) audited", []),
    ("version=pip-audit 2.10.1\ncode=1\ntail=Found 2 known vulnerabilities in 1 package\nparsed=yes\npackages=58\n"
     "finding=certifi|2026.7.22|PYSEC-2026-4320|2026.10.18\nfinding=certifi|2026.7.22|GHSA-xxxx|\n",
     "ran", "2 finding(s) in certifi 2026.7.22, of 58 package(s) audited",
     [{"name": "certifi", "version": "2026.7.22", "id": "PYSEC-2026-4320", "fix": "2026.10.18"},
      {"name": "certifi", "version": "2026.7.22", "id": "GHSA-xxxx", "fix": ""}]),
    ("state=no-install\ntail=ERROR: Could not find a version that satisfies the requirement pip-audit==2.10.1\n"
     "tail=WARNING: Retrying after connection broken by 'NewConnectionError'\n",
     "not run", "pip-audit==2.10.1 could not be installed in ~/emet-ci/audit-venv from the lock's hashes: "
                "no network", []),
    ("version=pip-audit 2.10.1\ncode=1\ntail=requests.exceptions.ConnectionError: Max retries exceeded\nparsed=no\n",
     "not run", "pip-audit did not finish: no network", []),
    ("version=pip-audit 2.10.1\ncode=2\ntail=usage: pip-audit\nparsed=no\n",
     "not run", "pip-audit did not finish (exit 2): usage: pip-audit", []),
    ("state=no-venv\n", "not run", "the audit's venv could not be made with python3 -m venv", []),
], ids=["clean", "findings", "no-network-for-the-install", "no-network-for-the-audit", "another-error", "no-venv"])
def test_the_audit_never_fails_the_run_and_says_which_state_it_is_in(tmp_path, laptop, answer, status, says,
                                                                    findings):
    assert make(tmp_path, laptop, FakeBody({"audit": answer})).run() == 0, "the audit leaves the verdict alone"
    said = record(tmp_path)
    assert said["audit"] == {"status": status, "reason": says, "tool": "pip-audit 2.10.1", "findings": findings}
    step = next(s for s in said["steps"] if s["name"] == "audit")
    assert step["status"] == ("passed" if status == "ran" else "not run") and step["reason"] == says
    assert " 0 failed or refused" in said["verdict"]
    report = (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")
    for finding in findings:
        assert finding["id"] in report


def test_the_audit_is_absent_from_a_run_that_did_not_ask_for_it(tmp_path, laptop):
    make(tmp_path, laptop, FakeBody(), "--steps", "sync,install,suites").run()
    said = record(tmp_path)
    assert "audit" not in said and "budgets" not in said and "soak" not in said


def test_the_audit_reads_pip_audits_own_json(tmp_path):
    """AUDIT_REPORT_PY over the JSON pip-audit 2.10.1 writes (its shape as
    the laptop's venv gave it on 2026-11-26): one line a finding, a finding
    listed twice counted once, and the editable packages named as skipped."""
    report = {"dependencies": [
        {"name": "certifi", "version": "2026.7.22", "vulns": [
            {"id": "PYSEC-2026-4320", "fix_versions": ["2026.10.18"], "aliases": ["GHSA-6q48-6c7w-9gw2"]},
            {"id": "PYSEC-2026-4320", "fix_versions": ["2026.10.18"], "aliases": ["GHSA-6q48-6c7w-9gw2"]}]},
        {"name": "anyio", "version": "4.15.1", "vulns": []},
        {"name": "emet-sdk", "skip_reason": "distribution marked as editable"}], "fixes": []}
    (tmp_path / "audit.json").write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "read.py").write_text(run.AUDIT_REPORT_PY, encoding="utf-8")
    done = subprocess.run([sys.executable, str(tmp_path / "read.py"), str(tmp_path / "audit.json")],
                          capture_output=True, timeout=60)
    values = run.Result(0, done.stdout.decode()).values()
    assert values == {"parsed": ["yes"], "packages": ["2"],
                      "finding": ["certifi|2026.7.22|PYSEC-2026-4320|2026.10.18"],
                      "skipped": ["emet-sdk|distribution marked as editable"]}
    (tmp_path / "audit.json").write_text("Found 2 known vulnerabilities", encoding="utf-8")
    done = subprocess.run([sys.executable, str(tmp_path / "read.py"), str(tmp_path / "audit.json")],
                          capture_output=True, timeout=60)
    assert done.stdout.decode().split() == ["parsed=no"]


STUB_AUDIT = '''#!/bin/sh
# A stand-in for pip-audit: --version, or the JSON of one finding.
if [ "$1" = "--version" ]; then echo "pip-audit 2.10.1"; exit 0; fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --path) echo "path=$2" >> "$(dirname "$0")/called" ;;
    -o) out=$2 ;;
  esac
  shift
done
printf '%s' '{"dependencies": [{"name": "certifi", "version": "2026.7.22", "vulns": [{"id": "PYSEC-2026-4320", "fix_versions": ["2026.10.18"]}]}], "fixes": []}' > "$out"
echo "Found 1 known vulnerability in 1 package"
exit 1
'''


def test_the_audit_runs_from_its_own_venv_over_the_runners_packages_in_a_real_shell(tmp_path, laptop):
    """The step's script through bash, with pip-audit already in the audit's
    venv: it audits the runner's site-packages with --path and installs
    nothing in the runner's venv."""
    home = tmp_path / "home"
    fake_venv(home)
    audit = home / "emet-ci" / "audit-venv"
    (audit / "bin").mkdir(parents=True)
    (audit / "bin" / "pip-audit").write_text(STUB_AUDIT, encoding="utf-8", newline="\n")
    (audit / "bin" / "pip-audit").chmod(0o755)
    program(audit / "bin" / "python", Path(sys.executable).as_posix())
    (audit / "installed").write_text(lock_digest() + "\n", encoding="utf-8")
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    step = runner.audit()
    assert step.status == "passed", step.reason
    assert runner.audit_result["findings"] == [
        {"name": "certifi", "version": "2026.7.22", "id": "PYSEC-2026-4320", "fix": "2026.10.18"}]
    import sysconfig

    called = (audit / "bin" / "called").read_text(encoding="utf-8")
    assert called == f"path={sysconfig.get_paths()['purelib']}\n", "the runner's packages, where its Python keeps them"


def test_the_audit_installs_pip_audit_in_its_own_venv_and_nothing_in_the_runners(tmp_path, laptop, monkeypatch):
    """The step's script through bash with no audit venv yet: `python3 -m
    venv` makes it, pip-audit goes into it, and the runner's own Python is
    asked where its packages are and nothing else. Both Pythons here are
    stand-ins that write down what they were asked; the audit's installs a
    stand-in pip-audit when asked to install, and neither reaches PyPI."""
    home = tmp_path / "home"
    log = tmp_path / "asked.log"
    real = Path(sys.executable).as_posix()
    runner_python = home / "emet-ci" / ".venv" / "bin" / "python"
    runner_python.parent.mkdir(parents=True)
    runner_python.write_text(f'#!/bin/sh\necho "runner $*" >> "{log.as_posix()}"\n'
                             f'case "$1" in -m) exit 0 ;; esac\nexec "{real}" "$@"\n', encoding="utf-8", newline="\n")
    runner_python.chmod(0o755)
    pip_audit = tmp_path / "pip-audit"
    pip_audit.write_text(STUB_AUDIT, encoding="utf-8", newline="\n")
    audit_python = (f'#!/bin/sh\necho "audit $*" >> "{log.as_posix()}"\n'
                    f'if [ "$1 $2 $3" = "-m pip install" ]; then cp "{pip_audit.as_posix()}" "$(dirname "$0")/pip-audit"; '
                    f'chmod 755 "$(dirname "$0")/pip-audit"; exit 0; fi\nexec "{real}" "$@"\n')
    (tmp_path / "audit-python").write_text(audit_python, encoding="utf-8", newline="\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text(
        f'#!/bin/sh\necho "python3 $*" >> "{log.as_posix()}"\n'
        f'[ "$1 $2" = "-m venv" ] || exit 9\nmkdir -p "$3/bin" && cp "{(tmp_path / "audit-python").as_posix()}" '
        f'"$3/bin/python" && chmod 755 "$3/bin/python"\n', encoding="utf-8", newline="\n")
    (bin_dir / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    step = runner.audit()
    assert step.status == "passed", step.reason
    asked = log.read_text(encoding="utf-8").splitlines()
    assert asked[0].startswith("python3 -m venv ") and asked[0].endswith("/emet-ci/audit-venv")
    venv = shell_home(home) + "/emet-ci/audit-venv"
    assert (f"audit -m pip install --disable-pip-version-check -q --require-hashes -r {venv}/requirements.txt"
            in asked), asked
    assert [line for line in asked if line.startswith("runner ")] == [
        "runner -c import sysconfig; print(sysconfig.get_paths()['purelib'])"], "the runner's venv is only read"
    audit = home / "emet-ci" / "audit-venv"
    assert (audit / "installed").read_text(encoding="utf-8").strip() == lock_digest()
    # What pip was given is the lock's part for pip-audit, byte for byte, as
    # the heredoc carried it: every line pinned and hashed.
    written = (audit / "requirements.txt").read_bytes().decode("utf-8")
    assert written == lock_part()[0]
    assert all(re.fullmatch(r"\S+==\S+( ; [^-]+)?( --hash=sha256:[0-9a-f]{64})+", line) for line in
               written.splitlines()), written
    assert step.details["pin"] == "pip-audit==2.10.1" and step.details["lock_sha256"] == lock_digest()


def test_an_audit_venv_installed_from_a_bare_pin_is_made_again_from_the_lock(tmp_path, laptop, monkeypatch):
    """Ledger L21. The body's audit venv from before 0.5.7 says it holds
    `pip-audit==2.10.1`, a pin with no hash; the runner's marker is now the
    lock part's sha256, so that venv is removed and made again from the
    lock's hashes."""
    home = tmp_path / "home"
    fake_venv(home)
    audit = home / "emet-ci" / "audit-venv"
    (audit / "bin").mkdir(parents=True)
    (audit / "bin" / "pip-audit").write_text(STUB_AUDIT, encoding="utf-8", newline="\n")
    (audit / "bin" / "pip-audit").chmod(0o755)
    (audit / "installed").write_text("pip-audit==2.10.1\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text('#!/bin/sh\necho "python3 $*" >> "$HOME/asked"\nexit 9\n', encoding="utf-8",
                                     newline="\n")
    (bin_dir / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    step = runner.audit()
    assert (home / "asked").read_text(encoding="utf-8").startswith("python3 -m venv "), "the venv is made again"
    assert not (audit / "bin" / "pip-audit").exists(), "the venv from the bare pin is gone"
    assert step.status == "not run" and step.reason == "the audit's venv could not be made with python3 -m venv"


def test_the_audit_takes_pip_audit_and_all_it_needs_from_the_ci_lock_and_nothing_else():
    """The real lock: pip-audit's part is pip-audit 2.10.1 and every pin the
    lock holds for it, however deep (filelock is there for cachecontrol,
    defusedxml for py-serializable, pip for pip-api), each with its hashes,
    and none of the other tools CI pins."""
    part, pin = lock_part()
    assert pin == "pip-audit==2.10.1"
    names = [run.pep503(re.split(r"[=; ]", line, maxsplit=1)[0]) for line in part.splitlines()]
    for needed in ("pip-audit", "cachecontrol", "filelock", "msgpack", "cyclonedx-python-lib", "py-serializable",
                   "defusedxml", "pip-api", "pip", "pip-requirements-parser", "platformdirs", "requests", "urllib3",
                   "rich", "markdown-it-py", "mdurl", "tomli", "tomli-w", "packaging"):
        assert needed in names, needed
    for other in ("mutmut", "zizmor", "vulture", "coverage", "build", "libcst", "textual", "pytest", "click",
                  "pyyaml", "pyyaml-ft", "colorama"):
        assert other not in names, other
    assert len(names) == len(set(names)) == 30
    assert all(" --hash=sha256:" in line for line in part.splitlines())


LOCK = """\
# This file was autogenerated by uv via the following command:
#    uv pip compile --generate-hashes requirements.in
alpha==1.0 \\
    --hash=sha256:aaaa \\
    --hash=sha256:bbbb
    # via
    #   other-tool
    #   pip-audit
beta==2.0 ; python_full_version < '3.11' \\
    --hash=sha256:cccc
    # via alpha
gamma==3.0 \\
    --hash=sha256:dddd
    # via other-tool
pip-audit==2.10.1 \\
    --hash=sha256:eeee
    # via -r requirements.in
"""


def test_the_lock_part_follows_via_however_deep_and_keeps_markers():
    part, pin = run.audit_requirements(LOCK)
    assert pin == "pip-audit==2.10.1"
    assert part.splitlines() == ["alpha==1.0 --hash=sha256:aaaa --hash=sha256:bbbb",
                                 "beta==2.0 ; python_full_version < '3.11' --hash=sha256:cccc",
                                 "pip-audit==2.10.1 --hash=sha256:eeee"]
    with pytest.raises(run.RunnerError, match="holds no pip-audit"):
        run.audit_requirements(LOCK.replace("pip-audit==2.10.1", "pip-tools==7.0"))
    with pytest.raises(run.RunnerError, match="pins beta with no hash"):
        run.audit_requirements(LOCK.replace("    --hash=sha256:cccc\n", ""))


def test_an_audit_with_no_lock_to_read_is_not_run(tmp_path, laptop, monkeypatch):
    monkeypatch.setattr(run, "AUDIT_LOCK", tmp_path / "no-requirements.txt")
    body = FakeBody()
    assert make(tmp_path, laptop, body).run() == 0, "the audit leaves the verdict alone"
    said = record(tmp_path)
    assert said["audit"]["status"] == "not run" and said["audit"]["reason"].startswith(
        "no hashed pin of pip-audit to install: [Errno 2]")
    assert "audit" not in body.steps(), "nothing went to the body"


# ------------------------------------------------- the install's pip (L27)


def test_the_runners_pip_is_held_at_26_2_or_later():
    """pip 26.2 fixes the six advisories of the runner's pip 25.1.1 (ledger
    L27), so the pin may move up and never below it."""
    name, _, version = run.RUNNER_PIP.partition("==")
    assert name == "pip" and tuple(int(part) for part in version.split(".")) >= (26, 2)


def test_the_install_upgrades_pip_first_and_a_new_pin_installs_again(tmp_path, laptop, monkeypatch):
    """The install step's script through bash, with a runner's Python that
    writes down what it is asked: pip is upgraded to RUNNER_PIP before the
    install line, the pip it then has is recorded, and the same tree with a
    new pin gives a new key, so it installs again."""
    home = tmp_path / "home"
    tree = home / "emet-ci" / "emet"
    for package in run.PACKAGES:
        (tree / package).mkdir(parents=True)
        (tree / package / "pyproject.toml").write_text(f'[project]\nname = "{package}"\n', encoding="utf-8")
    log = tmp_path / "asked.log"
    python = home / "emet-ci" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\n'
                      'case "$*" in "-m pip --version"*) echo "pip 26.2 from /x (python 3.13)" ;; '
                      '"-c "*) echo "versions=0.5.7 0.5.7 0.5.7 0.5.7" ;; esac\nexit 0\n',
                      encoding="utf-8", newline="\n")
    python.chmod(0o755)
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    step = runner.install()
    assert step.status == "passed", step.reason
    asked = log.read_text(encoding="utf-8").splitlines()
    installs = [line for line in asked if line.startswith("-m pip install")]
    assert installs[0] == f"-m pip install --disable-pip-version-check -q --upgrade {run.RUNNER_PIP}"
    assert installs[1].startswith("-m pip install --disable-pip-version-check -q -e emet-sdk[dev]")
    assert step.details["pip"] == "26.2" and step.reason.endswith("; pip 26.2")
    first = step.details["key"]
    log.write_text("", encoding="utf-8")
    assert runner.install().reason.startswith("kept"), "the same key: nothing is installed again"
    assert not [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("-m pip install")]
    monkeypatch.setattr(run, "RUNNER_PIP", "pip==26.3")
    again = runner.install()
    assert again.details["key"] != first and again.reason.startswith("installed")
    assert "--upgrade pip==26.3" in log.read_text(encoding="utf-8")


def test_a_pip_that_will_not_upgrade_fails_the_install_naming_it(tmp_path, laptop):
    answer = run.Result(6, "key=k\nupgrade=failed\ntail=ERROR: No matching distribution found for pip==26.2\n")
    body = FakeBody({"install": answer})
    assert make(tmp_path, laptop, body).run() == 2
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "install")
    assert step["reason"] == ("pip==26.2 could not be installed in the runner's venv (exit 6): "
                              "ERROR: No matching distribution found for pip==26.2")


# ------------------------------------------ the checks through tools/ci.py


def test_the_checks_run_ci_layering_when_the_tree_has_it(tmp_path, laptop):
    body = FakeBody({"checks": "ci=0\nci_line=ok: Check package layering (0.3 s)\n"
                               "ci_line=... not run here: needs zizmor\n"
                               "ci_said=ci: all green, 1 job, 1 not run here (layering/zizmor)\n"})
    assert make(tmp_path, laptop, body).run() == 0
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "checks")
    assert step["status"] == "passed" and step["details"]["via"] == "tools/ci.py layering"
    assert step["reason"] == "tools/ci.py layering exit 0: ci: all green, 1 job, 1 not run here (layering/zizmor)"
    body = FakeBody({"checks": "ci=1\nci_line=!!! FAILED: Check house style: exit 1\nci_said=ci: FAILED (layering)\n"})
    assert make(tmp_path / "b", laptop, body).run() == 1
    step = next(s for s in record(tmp_path / "b")["steps"] if s["name"] == "checks")
    assert step["status"] == "failed" and "Check house style: exit 1" in step["reason"]


@pytest.mark.parametrize("with_ci", [True, False], ids=["a-tree-with-ci.py", "master-before-0.5.6"])
def test_the_checks_script_takes_ci_py_or_the_three_scripts_in_a_real_shell(tmp_path, laptop, with_ci):
    home = tmp_path / "home"
    tools = home / "emet-ci" / "emet" / "tools"
    tools.mkdir(parents=True)
    fake_venv(home)
    for name in run.CHECKS:
        (tools / name).write_text(f"print('{name} ok')\n", encoding="utf-8")
    if with_ci:
        (tools / "ci.py").write_text("print('ok: Check package layering (0.1 s)')\nprint('ci: all green, 1 job')\n",
                                     encoding="utf-8")
    runner = make(tmp_path, laptop, BashBody(home))
    runner.home = shell_home(home)
    step = runner.checks()
    assert step.status == "passed", step.reason
    if with_ci:
        assert step.details["via"] == "tools/ci.py layering"
        assert step.details["lines"] == ["ok: Check package layering (0.1 s)"]
    else:
        assert step.reason == "layering ok; release_check ok; style ok"


# -------------------------------------------------- the network cut (P12)

NETCUT_SHA = "c4" * 32
#: What the network cut's first script, which only looks, prints on a body
#: ready for the cut; and what its second, which starts it, prints.
NETCUT_READY = f"owner=root 755\ninstalled={NETCUT_SHA}\ntree={NETCUT_SHA}\nbefore=open\n"
NETCUT_STARTED = "started=777\nduring=shut\n"
NETCUT_AFTER = ("after=open\nsaid=cut: outbound TCP and UDP to port 443 dropped from 2026-10-28T14:05:00-07:00 "
                "for 30 s\nsaid=restored: table inet emet_netcut deleted at 2026-10-28T14:05:30-07:00\n")


def netcut_run(tmp_path, laptop, answers: dict) -> tuple[int, dict, FakeBody]:
    body = FakeBody({"netcut-start": NETCUT_STARTED, "netcut-alive": "alive=yes\n", "netcut-after": NETCUT_AFTER,
                     **answers})
    code = make(tmp_path, laptop, body, "--netcut", "30").run()
    return code, next(s for s in record(tmp_path)["steps"] if s["name"] == "netcut"), body


@pytest.mark.parametrize("answer, says", [
    ("state=absent\n", "the sudo rule is not installed: /usr/local/sbin/emet-netcut is not there either"),
    (f"owner=root 755\ninstalled={NETCUT_SHA}\ntree={NETCUT_SHA}\nstate=no-rule\n", "the sudo rule is not installed"),
], ids=["nothing-installed", "the-script-without-its-rule"])
def test_the_network_cut_is_not_run_while_sudo_refuses(tmp_path, laptop, answer, says):
    """Today's state on the Pi, where `sudo -n` asks for a password."""
    code, step, body = netcut_run(tmp_path, laptop, {"netcut": answer})
    assert code == 0 and step["status"] == "not run" and step["reason"].startswith(says)
    assert "netcut-start" not in body.steps() and "netcut-alive" not in body.steps(), \
        "nothing starts, or waits on, a cut sudo would not run"
    look = body.script("netcut")
    assert 'sudo -n -l "$bin" 30' in look and "nohup" not in look, "the first script only looks"


def test_the_network_cut_runs_detached_and_passes_when_ssh_stayed_up_and_443_came_back(tmp_path, laptop):
    code, step, body = netcut_run(tmp_path, laptop, {"netcut": NETCUT_READY})
    assert code == 0 and step["status"] == "passed", step["reason"]
    assert step["details"]["ssh_during"] is True and step["details"]["after"] == "open"
    asked = body.steps()
    assert asked.index("netcut") < asked.index("netcut-start") < asked.index("netcut-alive") < asked.index("netcut-after")
    start = body.script("netcut-start")
    assert 'nohup setsid sudo -n "$bin" 30 > "$RUN/netcut.txt" 2>&1 < /dev/null &' in start, \
        "detached, its output in a file (the 0.4 row)"
    assert f"= 'root 755' ] && [" in start and f"= {NETCUT_SHA} ] ||" in start, \
        "it runs the script only while the owner, the mode and the hash are the ones judged"


@pytest.mark.parametrize("answers, says", [
    ({"netcut": NETCUT_READY, "netcut-start": NETCUT_STARTED.replace("during=shut", "during=open")},
     "port 443 was still reached during the cut"),
    ({"netcut": NETCUT_READY, "netcut-alive": ""}, "SSH did not answer during the cut"),
    ({"netcut": NETCUT_READY, "netcut-after": NETCUT_AFTER.replace("after=open", "after=shut")},
     "port 443 was not reached again after the cut"),
    ({"netcut": NETCUT_READY, "netcut-after": "after=open\n"}, "emet-netcut did not say it cut"),
    ({"netcut": NETCUT_READY, "netcut-after": NETCUT_AFTER.split("said=restored:")[0]},
     "emet-netcut did not say it restored"),
], ids=["not-cut", "ssh-lost", "not-restored", "no-word-from-the-script", "no-word-of-the-restore"])
def test_a_network_cut_that_broke_a_rule_fails(tmp_path, laptop, answers, says):
    code, step, _ = netcut_run(tmp_path, laptop, answers)
    assert code == 1 and step["status"] == "failed" and says in step["reason"]
    if says.endswith("restored"):
        assert step["reason"] == says, "it said it cut, and only the restore is missing"


@pytest.mark.parametrize("answer, says", [
    (NETCUT_READY.replace("owner=root 755", "owner=alex 755"), "it must be root's"),
    (NETCUT_READY.replace("owner=root 755", "owner=root 777"), "it must be root's"),
    (NETCUT_READY.replace("owner=root 755", "owner=root 775"), "it must be root's"),
    (NETCUT_READY.replace(f"tree={NETCUT_SHA}", "tree=" + "d5" * 32), "is not the synced tree's tools/body/netcut.sh"),
    (NETCUT_READY.replace("before=open", "before=shut"), "reached nothing on port 443 before the cut"),
], ids=["not-roots", "writable-by-others", "writable-by-its-group", "another-script", "no-network-before"])
def test_the_network_cut_asks_sudo_for_nothing_but_the_trees_root_owned_script(tmp_path, laptop, answer, says):
    code, step, body = netcut_run(tmp_path, laptop, {"netcut": answer})
    assert code == 0 and step["status"] == "not run" and says in step["reason"]
    assert "netcut-start" not in body.steps(), "the script that asks sudo to run it is never sent"
    assert "netcut-alive" not in body.steps()


def test_a_script_that_changed_after_the_look_is_not_run(tmp_path, laptop):
    code, step, body = netcut_run(tmp_path, laptop, {"netcut": NETCUT_READY, "netcut-start": "state=changed\n"})
    assert code == 0 and step["status"] == "not run" and "sudo was asked nothing" in step["reason"]
    assert "netcut-alive" not in body.steps()


def test_the_network_cut_needs_the_runners_lock(tmp_path, laptop):
    body = FakeBody()
    runner = make(tmp_path, laptop, body, "--netcut", "30")
    runner.home = HOME
    step = runner.netcut()
    assert step.status == "refused" and step.reason == "the runner's lock is not held"
    assert body.steps() == [], "nothing is asked of the body"


SUDO_STAND_IN = '''#!/bin/sh
# A stand-in for sudo: it writes down what it was asked and runs nothing.
echo "$*" >> "$HOME/sudo.log"
exit 0
'''


@pytest.mark.parametrize("owner, same, asked", [
    ("root 755", True, True),
    ("alex 755", True, False),
    ("root 775", True, False),
    ("root 755", False, False),
], ids=["the-trees-root-owned-script", "not-roots", "writable-by-its-group", "another-script"])
def test_the_network_cut_asks_sudo_to_run_only_the_trees_root_owned_script_in_a_real_shell(
        tmp_path, laptop, monkeypatch, owner, same, asked):
    """The step's own scripts through bash, with stand-ins for sudo, for
    stat (it gives the owner and mode of the case) and for setsid: sudo is
    asked to run the script in the one case where every check holds, and
    otherwise only whether the rule is there."""
    home = tmp_path / "home"
    tree = home / "emet-ci" / "emet" / "tools" / "body"
    tree.mkdir(parents=True)
    script = RUN_PY.with_name("netcut.sh").read_bytes()
    (tree / "netcut.sh").write_bytes(script)
    installed = tmp_path / "emet-netcut"
    installed.write_bytes(script if same else script + b"# another script\n")
    stubs = tmp_path / "stubs"
    (stubs / "sudo").parent.mkdir()
    (stubs / "sudo").write_text(SUDO_STAND_IN, encoding="utf-8", newline="\n")
    (stubs / "stat").write_text(f'#!/bin/sh\necho "{owner}"\n', encoding="utf-8", newline="\n")
    (stubs / "setsid").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8", newline="\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(stubs) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(run, "NETCUT_BIN", installed.as_posix())
    monkeypatch.setattr(run, "PROBE", "echo open")
    runner = make(tmp_path, laptop, BashBody(home), "--netcut", "30")
    runner.home = shell_home(home)
    runner.locked = True
    runner.check_busy = lambda: None
    step = runner.netcut()
    calls = (home / "sudo.log").read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"-n -l {installed.as_posix()} 30", "the first script asks only whether the rule is there"
    assert (f"-n {installed.as_posix()} 30" in calls) is asked, (calls, step.reason)
    if not asked:
        assert step.status == "not run" and len(calls) == 1, step.reason


@pytest.mark.parametrize("argv", [["--netcut", "29"], ["--netcut", "601"], ["--netcut", "30;id"],
                                  ["--steps", "netcut"], ["--steps", "soak"], ["--soak", "9"]])
def test_the_lengths_are_whole_numbers_within_their_bounds(argv):
    with pytest.raises(SystemExit):
        run.parse_args(["--release", "0.5.6", "--tree", *argv])


# ------------------------------------------- the events files (BVP12)


def events_text(gaps: list[float | None], *, header: dict | None = None) -> str:
    """An events file as `--events` writes it (contract section 2.2), with
    only the keys the runner reads: a header, a wake and a turn record for
    each gap, and a footer."""
    header = header or {"format": "emet-events", "version": "1.0", "program": "emet-talk",
                        "packages": {"emet-sdk": "0.5.7", "emet-hal": "0.5.7", "emet-providers": "0.5.7",
                                     "emet-engine": "0.5.7"}, "platform": "linux-aarch64", "python": "3.13.5"}
    lines = [json.dumps(header)]
    for n, gap in enumerate(gaps, 1):
        lines.append(json.dumps({"kind": "wake", "wake": n, "at_s": n * 4.0, "confidence": 1.0}))
        lines.append(json.dumps({"kind": "turn", "turn": n, "at_s": n * 4.0 + 2, "outcome": "answered",
                                 "gap_ms": gap, "tail_ms": 960}))
    lines.append(json.dumps({"kind": "footer", "at_s": 99.0, "frames": 7655}))
    return "\n".join(lines) + "\n"


def events_body(files: dict[str, str], answers: dict | None = None) -> FakeBody:
    """A stand-in body whose collect brings home these files beside a suite's."""
    body = FakeBody(answers)
    raw = {"./suite-emet-sdk.txt": b"226 passed\n", **{f"./{n}": text.encode() for n, text in files.items()}}
    body.answers["collect"] = run.Result(0, "", raw=tar_of(raw))
    return body


def test_the_talk_replay_and_the_ten_minutes_write_an_events_file_and_the_others_none(tmp_path, laptop):
    body = FakeBody()
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    run_dir = f"{HOME}/emet-ci/runs/20261028-140000"
    assert run.EVENTS_STEPS == ("speaker-talk", "live-stats")
    for name in run.HARDWARE_SUBSTEPS:
        command = steps[name]["details"]["command"]
        wanted = f"--stats --events {run_dir}/{name}.events.jsonl"
        assert (wanted in command) is (name in run.EVENTS_STEPS), (name, command)
        assert ("--events" in command) is (name in run.EVENTS_STEPS)
        look = f'[ -s {run_dir}/{name}.events.jsonl ] || echo "events=missing"'
        assert (look in body.script(name)) is (name in run.EVENTS_STEPS)
    assert "--events" not in run.BEHAVIOUR, "the file is where the numbers go, never how the robot behaves"


def test_an_engine_with_no_events_module_runs_both_steps_as_before(tmp_path, laptop):
    body = FakeBody({"prepare": (f"manifest_hash={MANIFEST_SHA}\nbody=pi_speakerphone\nstate={STATE_FILE}\n"
                                 f"models={json.dumps(run.MODELS)}\nevents=no\n")})
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    for step in record(tmp_path)["steps"]:
        if step["name"] in run.HARDWARE_SUBSTEPS:
            assert "--events" not in step["details"]["command"], step["name"]
            assert "events=missing" not in body.script(step["name"])


def test_a_step_that_wrote_no_events_file_fails(tmp_path, laptop):
    body = FakeBody({"speaker-talk": HARDWARE_OK + "events=missing\n"})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "speaker-talk")
    assert step["status"] == "failed" and step["reason"] == "the events file speaker-talk.events.jsonl was not written"


def test_the_events_files_come_home_and_each_step_says_its_turns_and_gap(tmp_path, laptop):
    body = events_body({"speaker-talk.events.jsonl": events_text([2000, 2600, 1800, None]),
                        "live-stats.events.jsonl": events_text([])})
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    steps = {s["name"]: s for s in record(tmp_path)["steps"]}
    assert steps["speaker-talk"]["details"]["events"] == {"file": "speaker-talk.events.jsonl", "turns": 4,
                                                          "gap_ms_median": 2000}
    assert steps["live-stats"]["details"]["events"] == {"file": "live-stats.events.jsonl", "turns": 0,
                                                        "gap_ms_median": None}
    assert "events" not in steps["speaker-echo"]["details"]
    assert (tmp_path / "out" / "speaker-talk.events.jsonl").is_file()
    report = (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")
    assert "  events   speaker-talk: 4 turn(s), turn gap median 2000 ms (speaker-talk.events.jsonl)" in report
    assert "  events   live-stats: 0 turn(s), turn gap median not measured (live-stats.events.jsonl)" in report


@pytest.mark.parametrize("text, says", [
    (events_text([2000], header={"format": "emet-events", "version": "2.0"}),
     "is format 'emet-events' version '2.0'; this reader knows emet-events 1.x"),
    ('{"format": "emet-events", "version": "1.0"}\nnot json\n', ":2: not a JSON object"),
], ids=["another-major", "a-broken-line"])
def test_an_events_file_the_reader_refuses_fails_the_collect_naming_it(tmp_path, laptop, text, says):
    body = events_body({"speaker-talk.events.jsonl": text})
    assert make(tmp_path, laptop, body, *window_args()).run() == 1
    collect = next(s for s in record(tmp_path)["steps"] if s["name"] == "collect")
    assert collect["status"] == "failed"
    assert "speaker-talk.events.jsonl could not be read: " in collect["reason"] and says in collect["reason"]


def test_a_run_with_events_still_counts_for_every_flag_row(tmp_path, laptop):
    """`--events` beside `--stats` leaves the ten live minutes a `--stats`
    run: the typescripts carry the commands as sent, --events and all."""
    body = windowed_body()
    assert make(tmp_path, laptop, body, *window_args()).run() == 0
    said = record(tmp_path)
    live_stats = next(s for s in said["steps"] if s["name"] == "live-stats")
    assert "--events" in live_stats["details"]["command"]
    assert said["flags_run"] == EVERY_FLAG


@pytest.mark.parametrize("path", ["~/sitting-0.5.7.events.jsonl", "~/pi-0.5.7.events.jsonl"])
def test_a_fetched_events_file_comes_home(tmp_path, laptop, path):
    body = FakeBody({"fetch-file": events_text([2000])})
    assert make(tmp_path, laptop, body, "--fetch", path).run() == 0
    assert (tmp_path / "out" / path.removeprefix("~/")).read_text().startswith('{"format": "emet-events"')


@pytest.mark.parametrize("path", ["~/sitting-0.5.7.events.json", "~/sitting-0.5.7.jsonl", "~/notes.events.jsonl",
                                  "~/sitting-key.events.jsonl", "~/.config/emet/x.events.jsonl",
                                  "~/sitting-0.5.7.events.jsonl.wav"])
def test_a_fetch_of_anything_else_ending_jsonl_is_refused(tmp_path, laptop, path):
    body = FakeBody()
    runner = make(tmp_path, laptop, body)
    runner.home = HOME
    problems: list[str] = []
    assert runner.fetch(path, tmp_path, problems) is None
    assert problems and "fetch-file" not in body.steps()


# ------------------------------------------- the release sitting (EV4)


SITTING_MD = RUN_PY.with_name("sitting.md")


def filled_sheet(rows: dict[int, str] | None = None, *, before: str = "Copied from sitting.md.\n\n") -> str:
    """A sheet with the maintainer's answers written in. `rows` replaces a
    row's line, by its number."""
    lines = ["| " + " | ".join(run.SHEET_COLUMNS) + " |", "|" + "---|" * len(run.SHEET_COLUMNS)]
    for n in run.SHEET_ROWS:
        held = "[x]" if n == 4 else ""
        body = "[ ]" if n == 5 else ""
        notes = "it took two tries | from the door" if n == 6 else ""
        line = f"| {n} | [x] | [x] | {held} | [x] | {2 + n % 4} | {2 if n == 6 else 1} | {body} | {notes} |"
        lines.append((rows or {}).get(n, line))
    return before + "\n".join(lines) + "\n\nAnything else:\n\n1. the voice was quiet\n"


def sitting_run(tmp_path, laptop, *, events: str | None, sheet: str | None = None):
    """`--collect-only` after a sitting: the log and, when given, the events
    file fetched, and the filled sheet named with --sheet."""
    log = "Script started on 2026-11-30 14:10:00-08:00 [COMMAND=\"emet-talk a b --stats\"]\n"

    def fetched(script: str) -> str:
        return events if "events.jsonl" in script else log

    body = FakeBody({"fetch-file": fetched})
    (tmp_path / "sheet.md").write_text(sheet if sheet is not None else filled_sheet(), encoding="utf-8")
    fetches = ["--fetch", "~/sitting-0.5.7.log"] + (["--fetch", "~/sitting-0.5.7.events.jsonl"] if events else [])
    args = run.parse_args(["--release", "0.5.7", "--collect-only", *fetches, "--sheet", str(tmp_path / "sheet.md"),
                           "--out", str(tmp_path / "out")])
    runner = run.BodyRun(args, remote=body, laptop=laptop, clock=lambda: NOW, log_check=tmp_path / "none.py",
                         history=[], top=tmp_path / "top")
    code = runner.run()
    sitting = tmp_path / "out" / "sitting.json"
    return code, json.loads(sitting.read_text(encoding="utf-8")) if sitting.is_file() else None, body


def test_the_sitting_sheet_is_the_one_the_runner_reads():
    """sitting.md's sheet has the columns read_sheet reads, "Soon" is gone,
    the anchors are written out, and its blank rows read as unticked."""
    text = SITTING_MD.read_text(encoding="utf-8")
    assert "| " + " | ".join(run.SHEET_COLUMNS) + " |" in text.splitlines()
    assert "Soon" not in text
    flowed = " ".join(text.split())
    assert "1, I would stop talking to it; 3, acceptable; 5, as quick as a person" in flowed
    assert "**Tries:**" in text and "1 when it woke the first time" in flowed
    rows = run.read_sheet(text)
    assert [r["turn"] for r in rows] == list(run.SHEET_ROWS)
    assert all(r["asked"] is False and r["wait"] is None and r["tries"] is None for r in rows)
    assert [r["turn"] for r in rows if r["held"] is not None] == [4]
    assert [r["turn"] for r in rows if r["body"] is not None] == [5]


def test_the_sitting_runs_turns_3_to_10_with_events_and_collects_them_with_the_sheet():
    text = SITTING_MD.read_text(encoding="utf-8")
    blocks = re.findall(r"```sh\n(.*?)\n```", text, re.S)
    plain = next(b for b in blocks if "emet-talk" in b and "--stats" not in b)
    stats = next(b for b in blocks if "emet-talk" in b and "--stats" in b)
    assert plain.rstrip().endswith('emet-sdk/examples/emet-soul.yaml"'), "turns 1 and 2 keep no flags"
    assert stats.rstrip().endswith('--stats --events ~/sitting-X.Y.Z.events.jsonl"')
    collect = next(b for b in blocks if "run.py" in b)
    assert ("--collect-only --fetch '~/sitting-X.Y.Z.log' --fetch '~/sitting-X.Y.Z.events.jsonl' "
            "--sheet ../releases/vX.Y.Z/sitting-sheet.md") in collect
    assert list(run.JOINED_ROWS) == [3, 4, 5, 6, 7, 8, 9, 10]
    for path in ("~/sitting-X.Y.Z.log", "~/sitting-X.Y.Z.events.jsonl"):
        assert run.FETCHABLE.match(path.replace("X.Y.Z", "0.5.7")) and not run.SECRET.search(path)


def test_the_sitting_talks_through_the_reference_manifest_the_release_ships():
    """Both of the sitting's runs name the branch's reference manifest, so
    the turns are judged and timed on the model settings the release ships
    (N25's reasoning effort), where the maintainer's ~/pi-speakerphone.yaml
    has no models block (0.5.7's review, batch critic#1)."""
    import yaml

    text = SITTING_MD.read_text(encoding="utf-8")
    talks = [b for b in re.findall(r"```sh\n(.*?)\n```", text, re.S) if "emet-talk" in b]
    assert len(talks) == 2
    for block in talks:
        assert '-c "emet-talk emet-sdk/examples/pi-speakerphone.yaml emet-sdk/examples/emet-soul.yaml' in block, block
        assert "~/pi-speakerphone.yaml" not in block, block
    manifest = yaml.safe_load((RUN_PY.parents[2] / "emet-sdk" / "examples" / "pi-speakerphone.yaml").read_text(encoding="utf-8"))
    soul = yaml.safe_load((RUN_PY.parents[2] / "emet-sdk" / "examples" / "emet-soul.yaml").read_text(encoding="utf-8"))
    assert soul["models"]["chat"]["provider"] == "openai" and "provider" not in manifest["models"]["chat"]
    assert manifest["models"]["chat"]["params"]["reasoning_effort"] == "low"


def test_the_real_vendor_replay_is_one_fixed_command_whose_files_come_home(tmp_path):
    """The done-when's body run (0.5.7-D6) has a command of its own in the
    README, since every runner step runs on mocks (0.5.7's review, batch
    critic#2): one-breath through the soul's real stages with the voice
    into the null sink, its typescript and events file fetched home, the
    owner's state file left as it was. Its manifest edit, run here, gives
    the branch's reference manifest with the null sink and nothing else
    changed."""
    import yaml

    readme = RUN_PY.with_name("README.md").read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```sh\n(.*?)\n```", readme, re.S) if "pi-done-when" in b]
    assert len(blocks) == 1, "one block holds the replay and its collect"
    replay, collect = blocks[0].splitlines()
    for flag in ("--replay ~/one-breath.wav", "--transcribe", "--reply", "--speak", "--stats",
                 "--events ~/pi-done-when-X.Y.Z.events.jsonl", "--state ~/pi-done-when-state.json"):
        assert flag in replay, flag
    assert "--keys" not in replay and "--echo" not in replay
    assert "script -qa ~/pi-done-when-X.Y.Z.log" in replay
    for path in ("~/pi-done-when-X.Y.Z.log", "~/pi-done-when-X.Y.Z.events.jsonl"):
        assert f"--fetch '{path}'" in collect
        assert run.FETCHABLE.match(path.replace("X.Y.Z", "0.5.7")) and not run.SECRET.search(path)
    edit = re.search(r"(sed \".*?\") emet-sdk/examples/pi-speakerphone\.yaml > ~/pi-done-when-body\.yaml", replay)
    assert edit, replay
    source = RUN_PY.parents[2] / "emet-sdk" / "examples" / "pi-speakerphone.yaml"
    (tmp_path / "in.yaml").write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    done = subprocess.run([posix_bash(), "-c", f'{edit.group(1)} in.yaml > out.yaml'], cwd=tmp_path,
                          capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()
    before = yaml.safe_load((tmp_path / "in.yaml").read_text(encoding="utf-8"))
    after = yaml.safe_load((tmp_path / "out.yaml").read_text(encoding="utf-8"))
    assert after["audio"]["output"].pop("sink") == "null" and after == before


def test_a_filled_sheet_reads_each_box_and_number():
    rows = run.read_sheet(filled_sheet())
    assert rows[0] == {"turn": 1, "asked": True, "whole": True, "held": None, "voice": True, "wait": 3, "tries": 1,
                       "body": None, "notes": None}
    assert rows[3]["held"] is True and rows[4]["body"] is False
    assert rows[5]["tries"] == 2 and rows[5]["notes"] == "it took two tries | from the door"
    assert [r["wait"] for r in rows] == [3, 4, 5, 2, 3, 4, 5, 2, 3, 4]


@pytest.mark.parametrize("rows, says", [
    ({4: "| 4 | [x] | [x] | [x] | [x] | 6 | 1 | | |"}, "row 4, Wait: '6' is not a rating from 1 to 5"),
    ({4: "| 4 | [x] | [x] | [x] | [x] | four | 1 | | |"}, "row 4, Wait: 'four' is not a rating from 1 to 5"),
    ({2: "| 2 | [x] | [x] | | [x] | 3 | 0 | | |"}, "row 2, Tries: '0' is not a whole number from 1"),
    ({2: "| 2 | yes | [x] | | [x] | 3 | 1 | | |"}, "row 2, Asked: 'yes' is not [x], [ ] or empty"),
    ({3: "| 4 | [x] | [x] | | [x] | 3 | 1 | | |"}, "row 3 is numbered '4'"),
    ({10: "| 10 | [x] | [x] | | [x] | 3 |"}, "has 6 cells, and the sheet has 9 columns"),
    ({10: "not a row"}, "the sheet has 9 rows, and a sitting has 10"),
], ids=["wait-6", "wait-a-word", "tries-0", "a-box-in-words", "a-row-misnumbered", "a-short-row", "nine-rows"])
def test_a_sheet_the_runner_cannot_read_names_the_row_and_column(rows, says):
    with pytest.raises(run.SheetError, match=re.escape(says)):
        run.read_sheet(filled_sheet(rows))


def test_a_sheet_with_the_old_columns_is_no_sheet():
    old = filled_sheet().replace("| Wait (1 to 5) | Tries |", "| Soon |")
    with pytest.raises(run.SheetError, match="no table has the sheet's columns"):
        run.read_sheet(old)


def test_a_sheet_is_refused_before_the_body_is_reached(tmp_path, capsys):
    (tmp_path / "sheet.md").write_text(filled_sheet({4: "| 4 | [x] | [x] | [x] | [x] | 9 | 1 | | |"}),
                                       encoding="utf-8")
    for argv, says in ([["--collect-only", "--sheet", str(tmp_path / "sheet.md")], "row 4, Wait: '9'"],
                       [["--collect-only", "--sheet", str(tmp_path / "none.md")], "could not be read"],
                       [["--tree", "--sheet", str(tmp_path / "sheet.md")], "--sheet goes with --collect-only"]):
        with pytest.raises(SystemExit) as stopped:
            run.parse_args(["--release", "0.5.7", *argv])
        assert stopped.value.code == 2 and says in capsys.readouterr().err


def test_the_sitting_joins_each_rating_to_its_turns_timings(tmp_path, laptop):
    code, sitting, body = sitting_run(tmp_path, laptop, events=events_text([2100 + 10 * n for n in range(8)]))
    assert code == 0
    assert sitting["format"] == "emet-sitting/1" and sitting["release"] == "0.5.7" and sitting["joined"] == "joined"
    turns = sitting["turns"]
    assert [t["turn"] for t in turns] == list(run.SHEET_ROWS)
    assert turns[0]["record"] is None and turns[1]["record"] is None, "turns 1 and 2 ran with no flags"
    assert [t["record"]["turn"] for t in turns[2:]] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert turns[2]["record"]["gap_ms"] == 2100 and turns[9]["record"]["gap_ms"] == 2170
    assert turns[2]["wait"] == 5 and turns[2]["record"]["kind"] == "turn"
    assert set(turns[0]) == {"turn", "asked", "whole", "held", "voice", "wait", "tries", "body", "notes", "record"}
    collect = next(s for s in record(tmp_path)["steps"] if s["name"] == "collect")
    assert collect["details"]["sitting"] == {"file": "sitting.json", "events": "sitting-0.5.7.events.jsonl",
                                             "joined": "joined", "rated": 10}
    report = (tmp_path / "out" / "report.txt").read_text(encoding="utf-8")
    assert "  sitting  sitting.json: 10 of 10 turn(s) rated; joined" in report
    assert f"f={HOME}/sitting-0.5.7.events.jsonl" in "".join(body.scripts)


@pytest.mark.parametrize("gaps, joined", [
    ([2000] * 9, "not joined: 9 turn records for 8 rows"),
    ([2000] * 5, "not joined: 5 turn records for 8 rows"),
], ids=["a-false-wake", "a-run-started-again"])
def test_a_sitting_with_other_than_eight_turns_keeps_its_ratings_unjoined(tmp_path, laptop, gaps, joined):
    code, sitting, _ = sitting_run(tmp_path, laptop, events=events_text(gaps))
    assert code == 0 and sitting["joined"] == joined
    assert all(t["record"] is None for t in sitting["turns"])
    assert [t["wait"] for t in sitting["turns"]] == [r["wait"] for r in run.read_sheet(filled_sheet())]


def test_a_sitting_with_no_events_file_says_which_to_fetch(tmp_path, laptop):
    code, sitting, _ = sitting_run(tmp_path, laptop, events=None)
    assert code == 0
    assert sitting["joined"] == "not joined: no events file was fetched (--fetch '~/sitting-0.5.7.events.jsonl')"
    assert all(t["record"] is None for t in sitting["turns"]) and sitting["turns"][0]["asked"] is True


def test_a_sitting_whose_events_file_is_refused_fails_the_collect_and_keeps_the_ratings(tmp_path, laptop):
    code, sitting, _ = sitting_run(tmp_path, laptop, events='{"format": "emet-events", "version": "9.0"}\n')
    assert code == 1
    assert sitting["joined"].startswith("not joined: sitting-0.5.7.events.jsonl could not be read: ")
    assert sitting["turns"][3]["held"] is True
    collect = next(s for s in record(tmp_path)["steps"] if s["name"] == "collect")
    assert collect["status"] == "failed" and "sitting-0.5.7.events.jsonl could not be read" in collect["reason"]
