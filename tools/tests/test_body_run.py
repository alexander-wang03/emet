"""The body runner, `tools/body/run.py`, against a stand-in body.

Nothing here opens ssh, a microphone or a speaker. `FakeBody` answers each
script the runner sends by the step named in its first line and keeps every
script, so a test can show both what was asked of the body and what never
was. `FakeLaptop` answers the read-only git questions and fails a test that
asks anything else.
"""

from __future__ import annotations

import datetime as dt
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


class FakeBody:
    """The body, as the runner sees it: one answer per step."""

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
            "checks": "".join(f"{c}=0\n{c}_said=ok\n" for c in ("check_layering", "release_check", "check_style")),
            "bank": "missing=tools/bank/replay.py is not in the synced tree\n",
            "prepare": (f"body=pi_speakerphone\nstate={HOME}/.local/state/emet/pi_speakerphone.json\n"
                        f"models={json.dumps(run.MODELS)}\n"),
            "collect": run.Result(0, "", raw=tar_of({"./suite-emet-sdk.txt": b"226 passed\n"})),
            "history": "",
        }
        for package, tests in (("emet-sdk", 226), ("emet-hal", 117), ("emet-providers", 167), ("emet-engine", 949)):
            self.answers[f"suite {package}"] = (f"code=0\nms=4000\nsummary={tests} passed in 3.5s\n--- junit\n"
                                                + junit(tests, skipped=1 if package == "emet-hal" else 0))
        for name in ("speaker-echo", "speaker-speak", "speaker-talk", "live-plain", "live-stats"):
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


def make(tmp_path, laptop, body, *extra: str, clock=lambda: NOW):
    args = run.parse_args(["--release", "0.5.5", "--tree", "--out", str(tmp_path / "out"), *extra])
    return run.BodyRun(args, remote=body, laptop=laptop, clock=clock,
                       log_check=tmp_path / "no-log-check.py", history=[])


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


def test_every_suite_and_check_runs_from_the_runners_own_tree(tmp_path, laptop):
    body = FakeBody()
    make(tmp_path, laptop, body).run()
    for script in body.scripts:
        assert "Documents/emet" not in script, "the maintainer's checkout is never touched"
    for step in ("suite emet-sdk", "checks", "install"):
        assert '"$CI/emet' in body.script(step)


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
    hardware = ("speaker-echo", "speaker-speak", "speaker-talk", "live-plain", "live-stats")
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
    assert "timeout -s INT -k 30 --preserve-status 620" in steps["live-stats"]["details"]["command"]
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
    assert all(steps[n]["status"] == "refused" for n in ("speaker-echo", "live-plain", "live-stats"))
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
    for name in ("speaker-echo", "speaker-speak", "speaker-talk", "live-plain", "live-stats"):
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
    runner = run.BodyRun(args, remote=body, laptop=laptop, clock=lambda: NOW,
                         log_check=tmp_path / "none.py", history=[])
    assert runner.run() == 0
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
