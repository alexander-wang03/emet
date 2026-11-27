"""The body runner's soak, budgets, nightly and network cut, which run with
nobody at the robot (`tools/body/run.py`, `tools/body/nightly.sh`,
`tools/body/netcut.sh`).

Nothing here opens ssh, a microphone or a speaker. The stand-in body of
`test_body_run.py` answers the runner's scripts; the scripts the body runs
itself (the feeder, the budget's driver, the manifests with the null sink,
the soak's shell, the nightly's and the network cut's functions) are run
here for real wherever this machine can run them, with stand-ins for the
programs only the body has.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

import test_body_run as base
from test_body_run import FakeBody, prepared, record, run

laptop = base.laptop  # the stand-in laptop's fixture, shared

BODY = Path(run.__file__).resolve().parent
NIGHTLY_SH = BODY / "nightly.sh"
NETCUT_SH = BODY / "netcut.sh"
SOAK_PREPARED = prepared(("soak/soak.yaml", run.MODELS), extra="wavs=13\n")


def make(tmp_path, laptop, body, *extra, **kwargs):
    return base.make(tmp_path, laptop, body, *extra, **kwargs)


# ------------------------------------------------------- canned soak files


def samples(n: int = 60, *, t0: int = 1_800_000_000, rss_mb=lambda i: 96.0, fds=lambda i: 14,
            threads=lambda i: 3, throttled=lambda i: "0x0", snd=lambda i: 0, mine=lambda i: 0,
            cpu=lambda i: 100 + 4 * i) -> str:
    """The soak script's sample lines, one a minute from t0. A reading of
    None is written `none`, as the script writes one it could not read."""
    def kb(i: int) -> str:
        mb = rss_mb(i)
        return "none" if mb is None else str(int(mb * 1024))

    def shown(value) -> str:
        return "none" if value is None else str(value)

    return "".join(
        f"t={t0 + 60 * (i + 1)} rss_kb={kb(i)} fds={fds(i)} threads={shown(threads(i))} snd={snd(i)} "
        f"cpu_ticks={cpu(i)} temp=48.3 throttled={throttled(i)} pcm={mine(i)} pcm_mine={mine(i)} listening=1\n"
        for i in range(n))


FOOTER = """\
source ended. heard it 270 time(s).

  frames        45000  (3600.0s of audio)
  wakes         270   turns 270
  per frame     mean 2.41 ms   p50 2.30   p95 3.10   max {max}
  budget        80 ms/frame, {over} frame(s) over
  realtime      0.0301  (33x faster than realtime)
  dropped       0
  stt final     mean 0 ms   max 1   (270 turn(s), endpoint to final transcript)
  clock         not measured: a file has no sound card, so wall time says nothing about drift
  verdict       kept up
"""
STATUS = "clk_tck=100\nlisten_exit=0\nfeed_exit=0\ndev_exit=0\nsoak_exit=0\nlock=released\n"
DEV_ERR = ("/home/alex/emet-ci/.venv/lib/python3.13/site-packages/x.py:3: DeprecationWarning: old call\n"
           "  old()\n"
           "Executing <Task pending name='Task-1' coro=<_run() running at cli.py:433>> took 2.613 seconds\n")


def soak_files(**overrides) -> dict[str, str]:
    files = {"status": STATUS, "samples": samples(), "pcm_before": "", "pcm_after": "", "audio": "",
             "listen": FOOTER.format(max="9.80", over=0), "dev_err": DEV_ERR}
    files.update(overrides)
    return files


def soak_read(files: dict[str, str]):
    """The stand-in body's answer to the soak-read script: each file under
    the fence the script names."""
    def answer(script: str) -> str:
        fence = re.search(r'echo "(=== [0-9a-f]{32} )status"', script).group(1)
        return "".join(f"{fence}{key}\n{text}" for key, text in files.items())
    return answer


def soaking_body(files: dict[str, str] | None = None, answers: dict | None = None, polls: list[str] | None = None):
    """A stand-in body that takes a soak: prepares it, starts it, says it is
    alive for each poll in `polls`, then that it is done."""
    left = list(polls if polls is not None else ["alive=yes\n"])

    def poll(script: str) -> str:
        return left.pop(0) if left else "done=yes\n"

    body = FakeBody({"soak-prepare": SOAK_PREPARED, "soak-start": "started=4242\n", "soak-poll": poll,
                     "soak-read": soak_read(files or soak_files()), **(answers or {})})
    return body


# ---------------------------------------------------------- the soak's run


def test_the_soak_runs_one_replay_of_the_bank_at_real_time_with_the_null_sink_and_no_microphone(tmp_path, laptop):
    body = soaking_body()
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 0
    said = record(tmp_path)
    step = next(s for s in said["steps"] if s["name"] == "soak")
    assert step["status"] == "passed", step["reason"]
    start = body.script("soak-start")
    assert "nohup setsid bash \"$S/soak.sh\" > \"$S/soak.log\" 2>&1 < /dev/null &" in start, \
        "detached, so a dropped SSH session does not end it"
    assert "SOAK_S=3600\n" in start and "SAMPLE_S=60\n" in start
    listeners = [line for line in run.SOAK_SH.splitlines() if '"$LISTEN_BIN"' in line]
    assert len(listeners) == 2
    for line in listeners:
        assert '--replay "$PIPE" --transcribe --stats --keys empty-keys.env' in line, \
            "a replay, so no microphone; the mock transcriber from the runner's manifest"
        assert "soak.yaml" in line
    assert '"$PY" feed.py "$PIPE" realtime "$SOAK_S"' in run.SOAK_SH
    assert '-X dev "$LISTEN_BIN"' in run.SOAK_SH and '"$PY" feed.py "$PIPE" once' in run.SOAK_SH
    asked = body.steps()
    assert asked.index("bank") < asked.index("budget-run") < asked.index("soak-prepare") < asked.index("soak-start")
    assert asked.count("soak-poll") == 2
    # The lock is the soak's while it runs and is taken again after it.
    assert asked[asked.index("soak-read") + 1] == "lock"
    soak = said["soak"]
    assert soak["flat"] is True and soak["reasons"] == []
    assert soak["minutes"] == 60 and soak["samples"] == 60 and soak["judged"] == 55
    assert soak["dev_warnings"] == 2 and soak["dev_warning_lines"][0].endswith("DeprecationWarning: old call")
    assert step["reason"].startswith("flat over 60 minute(s), 60.0 of audio read: ")


def test_the_soak_checks_the_sound_cards_before_and_after_and_its_listener_each_minute():
    """The PCM check the preflight uses, before the first listener and after
    the second, and each sample of either counts the listener's own files
    under /dev/snd and the PCMs whose owner is the listener."""
    script = run.SOAK_SH
    assert run.PCM_CHECK.format(root='"$ASOUND"') in script and "ASOUND=/proc/asound\n" in script
    assert script.index("pcm_open > pcm-before.txt") < script.index('"$LISTEN_BIN"')
    assert script.index("pcm_open > pcm-after.txt") > script.index('echo "dev_exit=$?"')
    assert script.index("> audio.txt") > script.index('echo "dev_exit=$?"'), "the -X dev pass is inside the window"
    assert "follow listen.out samples.txt" in script and "follow dev.out dev-samples.txt" in script
    assert "-lname '/dev/snd/*'" in script and 'grep -c "owner_pid : $LISTEN\\$"' in script


def test_the_soak_runs_last_and_spends_none_of_the_window(tmp_path, laptop):
    """A run of every step takes them in the order STEPS names: the speaker
    and the microphone before the network cut and the soak."""
    body = soaking_body(answers={"netcut": base.NETCUT_READY, "netcut-start": base.NETCUT_STARTED,
                                 "netcut-alive": "alive=yes\n", "netcut-after": base.NETCUT_AFTER})
    assert make(tmp_path, laptop, body, *base.window_args(), "--soak", "60", "--netcut", "30").run() == 0
    names = [s["name"] for s in record(tmp_path)["steps"] if s["name"] not in ("preflight", "collect")]
    assert list(dict.fromkeys(name.split("-")[0] for name in names)) == list(run.STEPS), names
    asked = body.steps()
    assert asked.index("live-stats") < asked.index("netcut") < asked.index("soak-start")


def test_the_soaks_manifest_names_the_null_sink_whatever_the_body_says(tmp_path):
    """NULL_MANIFEST_PY run here as the body runs it: a manifest whose
    output is the real speaker comes out with the null sink and the mock
    models, and nothing else of the body changes."""
    yaml = pytest.importorskip("yaml")
    pytest.importorskip("emet_engine.state")
    source = tmp_path / "pi-speakerphone.yaml"
    source.write_text(base.MANIFEST_YAML, encoding="utf-8")
    (tmp_path / "w.py").write_text(run.NULL_MANIFEST_PY, encoding="utf-8")
    done = subprocess.run([sys.executable, str(tmp_path / "w.py"), str(source), str(tmp_path / "soak.yaml"),
                           json.dumps(run.MODELS)], capture_output=True, timeout=60,
                          env=dict(os.environ, XDG_STATE_HOME=str(tmp_path / "xdg")))
    assert done.returncode == 0, done.stdout.decode() + done.stderr.decode()
    said = run.Result(0, done.stdout.decode()).values()
    entry = json.loads(said["written"][0])
    assert entry["sink"] == "null" and entry["models"] == run.MODELS
    original, mine = yaml.safe_load(base.MANIFEST_YAML), yaml.safe_load((tmp_path / "soak.yaml").read_text())
    assert mine["audio"]["output"]["sink"] == "null"
    assert original["audio"]["output"].get("sink") != "null", "the test's body has a sink of its own"
    for key in set(original) - {"models", "audio"}:
        assert mine[key] == original[key]
    assert {k: v for k, v in mine["audio"]["output"].items() if k != "sink"} == \
        {k: v for k, v in original["audio"]["output"].items() if k != "sink"}


def test_a_soak_manifest_without_the_null_sink_stops_the_soak_before_it_starts(tmp_path, laptop):
    body = soaking_body(answers={"soak-prepare": prepared(("soak/soak.yaml", run.MODELS), sink="speaker",
                                                          extra="wavs=13\n")})
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 1
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "failed" and "names the sink 'speaker', not null" in step["reason"]
    assert "soak-start" not in body.steps()


def test_a_soak_with_no_recordings_is_not_run(tmp_path, laptop):
    body = soaking_body(answers={"soak-prepare": "wavs=0\nmissing=no recording in /home/alex/voicebank/audio\n"})
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 0
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "not run" and "no recording" in step["reason"]
    assert "soak-start" not in body.steps() and "soak" not in record(tmp_path)


def test_a_run_finds_a_soak_in_progress_and_refuses(tmp_path, laptop):
    soak = ("soak=/home/alex/emet-ci/runs/20261127-031700/soak\nsoak_started=2026-11-27T03:17:00\n"
            "soak_minutes=60\nsoak_release=0.5.6\n")
    ps = base.QUIET_PS + "ps=5150 /home/alex/emet-ci/.venv/bin/python3 /home/alex/emet-ci/.venv/bin/emet-listen soak.yaml\n"
    body = FakeBody({"preflight": base.preflight_out(ps=ps, lock="held") + soak})
    assert make(tmp_path, laptop, body).run() == 2
    verdict = record(tmp_path)["verdict"]
    assert "a soak is in progress on the body, for 0.5.6, started 2026-11-27T03:17:00 for 60 minute(s)" in verdict
    assert "--soak-collect" in verdict and body.steps() == ["preflight"]


def test_the_preflight_names_a_soak_only_while_its_script_lives(tmp_path):
    """The preflight's soak lines through a real bash: a soak whose script
    is alive is named; one that wrote `done`, or whose process is gone, is
    not."""
    home = tmp_path / "home"
    folder = home / "emet-ci" / "runs" / "x" / "soak"
    folder.mkdir(parents=True)
    (home / "emet-ci" / "soak.current").write_text(base.shell_home(home) + "/emet-ci/runs/x/soak\n")
    (folder / "meta.txt").write_text("release=0.5.6\nstarted=2026-11-27T03:17:00\nminutes=60\n")
    script = run.BodyRun.PREFLIGHT[run.BodyRun.PREFLIGHT.index('soak=$(cat'):]
    shell = base.BashBody(home)
    alive = shell.run(f'sleep 20 & echo "$!" > "$CI/runs/x/soak/pid"\n{script}kill %1\n').values()
    assert alive.get("soak") and alive["soak_minutes"] == ["60"] and alive["soak_release"] == ["0.5.6"]
    (folder / "done").write_text("2026-11-27T04:20:00\n")
    assert "soak" not in shell.run(f'sleep 20 & echo "$!" > "$CI/runs/x/soak/pid"\n{script}kill %1\n').values()
    (folder / "done").unlink()
    assert "soak" not in shell.run(f'echo 999999 > "$CI/runs/x/soak/pid"\n{script}').values()


def test_the_lock_is_left_to_a_soak_that_outlives_the_laptops_run(tmp_path, laptop):
    """The laptop's run ends while the soak runs: its lock stays for the
    soak, which frees it when it ends. Through a real bash."""
    home = tmp_path / "home"
    folder = home / "emet-ci" / "runs" / "x" / "soak"
    folder.mkdir(parents=True)
    runner = make(tmp_path, laptop, base.BashBody(home))
    runner.home = base.shell_home(home)
    assert runner.take_lock() is None
    runner.soak_dir = f"{runner.home}/emet-ci/runs/x/soak"
    lock = home / "emet-ci" / "lock"

    class Soaking(base.BashBody):
        def run(self, script, *, timeout=120.0):
            return super().run(f'sleep 20 & echo "$!" > "{runner.soak_dir}/pid"\n{script}kill %1\n', timeout=timeout)

    runner.remote = Soaking(home)
    runner.release_lock()
    assert lock.read_text().startswith(f"token {runner.token}\n"), "kept for the soak"
    runner.remote = base.BashBody(home)
    (folder / "done").write_text("now\n")
    runner.locked = True  # a run whose soak has ended holds the lock again
    runner.release_lock()
    assert not lock.exists()


def shell_path(path: Path) -> str:
    """A path as the POSIX shell spells it: Git's bash on Windows reads
    `/c/...` where Windows writes `C:/...`, whose colon would split PATH."""
    text = path.as_posix()
    if re.match(r"^[A-Za-z]:/", text):
        return "/" + text[0].lower() + text[2:]
    return text


#: Stand-ins for the soak's programs on a machine that is not the body. The
#: listener ignores Ctrl-C and never ends, as a hung one would; the feeder
#: blocks, as one writing to a listener that stopped reading does; the -X
#: dev pass ends at once.
HUNG_LISTEN = "#!/bin/sh\ntrap '' INT\nwhile :; do sleep 1; done\n"
STAND_IN_PY = '#!/bin/sh\ncase "$1" in\n  feed.py) exec sleep 30 ;;\n  -X) exit 0 ;;\nesac\nexit 9\n'


def soak_shell(tmp_path: Path, *, token: str, lock: str | None, cards: dict[str, str] | None = None,
               listen: str = HUNG_LISTEN, seconds: int = 1) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """SOAK_SH through a real bash, its sound cards read from a stand-in for
    /proc/asound, its programs the stand-ins above, a sample a second and
    every bound a second or two. Returns the run, the soak's folder and the
    lock's path. Whatever the script started is killed afterwards."""
    home = tmp_path / "home"
    ci = home / "emet-ci"
    folder = ci / "runs" / "x" / "soak"
    (ci / "tmp").mkdir(parents=True)
    folder.mkdir(parents=True)
    asound = tmp_path / "asound"
    for name, text in (cards or {"card2/pcm0p/sub0/status": "closed\n"}).items():
        (asound / name).parent.mkdir(parents=True, exist_ok=True)
        (asound / name).write_text(text, encoding="utf-8", newline="\n")
    programs = tmp_path / "programs"
    programs.mkdir()
    for name, text in (("emet-listen", listen), ("python", STAND_IN_PY)):
        (programs / name).write_text(text, encoding="utf-8", newline="\n")
        (programs / name).chmod(0o755)
    if lock is not None:
        (ci / "lock").write_text(lock, encoding="utf-8", newline="\n")
    params = "".join(f"{k}={shlex.quote(v)}\n" for k, v in (
        ("SOAK", shell_path(folder)), ("CI", shell_path(ci)), ("PY", shell_path(programs / "python")),
        ("LISTEN_BIN", shell_path(programs / "emet-listen")), ("SOUL", "soul.yaml"), ("WAVS", shell_path(tmp_path)),
        ("SOAK_S", str(seconds)), ("OVERRUN_S", "1"), ("DEV_S", "2"), ("SAMPLE_S", "1"), ("STAMP", "x"),
        ("TOKEN", token)))
    assert run.SOAK_SH.count("ASOUND=/proc/asound\n") == 1
    script = run.SOAK_SH.replace("ASOUND=/proc/asound\n", f"ASOUND={shlex.quote(shell_path(asound))}\n")
    (folder / "soak.sh").write_text(params + script, encoding="utf-8", newline="\n")
    try:
        done = subprocess.run([base.posix_bash(), str(folder / "soak.sh")], env=dict(os.environ, HOME=str(home)),
                              capture_output=True, timeout=90)
    finally:
        pids = " ".join((folder / name).read_text().strip() for name in ("pid", "listen.pid", "dev.pid")
                        if (folder / name).exists())
        if pids:
            subprocess.run([base.posix_bash(), "-c", f"kill -KILL {pids} 2>/dev/null; true"], timeout=30)
    return done, folder, ci / "lock"


OPEN_CARD = {"card2/pcm0p/sub0/status": "closed\n", "card3/pcm0c/sub0/status": "state: RUNNING\nowner_pid   : 4242\n"}


@pytest.mark.parametrize("lock, kept", [
    ("token " + "a" * 32 + "\nby tools/body/run.py\n", True),
    ("token " + "f" * 32 + "\nby tools/body/run.py\n", False),
], ids=["another-runs-lock", "its-own-runs-lock"])
def test_the_soaks_script_refuses_an_open_card_and_frees_only_its_own_runs_lock(tmp_path, lock, kept):
    """A card open when the soak is to start: no listener starts, the script
    exits 3, and the lock is freed only when it holds the soak's own token."""
    done, folder, path = soak_shell(tmp_path, token="f" * 32, lock=lock, cards=OPEN_CARD)
    status = run.key_values((folder / "status").read_text())
    assert done.returncode == 3 and status["soak_exit"] == "3", (done.stderr.decode(), status)
    assert status["refused"] == "a sound card was open when the soak was to start"
    assert "RUNNING, owner_pid : 4242" in (folder / "pcm-before.txt").read_text()
    assert not (folder / "listen.pid").exists(), "no listener was started"
    assert (folder / "done").exists()
    assert path.exists() is kept and status["lock"] == ("not ours" if kept else "released")
    if kept:
        assert path.read_text() == lock


def test_the_soaks_script_stops_a_listener_that_outlives_its_length(tmp_path):
    """A listener that ignores Ctrl-C and never ends, beside a feeder that
    blocks: past the soak's length and its overrun the script stops it, two
    samples later kills it, and ends; the soak is not flat."""
    start = time.monotonic()
    done, folder, path = soak_shell(tmp_path, token="f" * 32, lock="token " + "f" * 32 + "\n")
    assert done.returncode == 0, done.stderr.decode()
    assert time.monotonic() - start < 60, "the script ended by itself"
    status = run.key_values((folder / "status").read_text())
    assert status["listen_stopped"] == "it was still running 2 s after it started", status
    assert "listen_killed" in status and status["listen_exit"] != "0" and status["lock"] == "released"
    assert (folder / "done").exists() and not path.exists()
    files = {"status": (folder / "status").read_text(), "samples": (folder / "samples.txt").read_text(),
             "listen": "", "pcm_after": (folder / "pcm-after.txt").read_text()}
    soak = run.summarize_soak(1, files)
    assert soak["flat"] is False
    assert any(r.startswith("the listener was stopped by the soak's script: it was still running") for r in
               soak["reasons"]), soak["reasons"]


def test_the_laptop_waits_for_the_soak_through_a_body_that_stops_answering_for_a_while(tmp_path, laptop):
    """A poll lost to the network is tried again; the soak is read once it
    says it is done."""
    left = ["alive=yes\n", run.Unreachable("no route"), run.Unreachable("no route"), "alive=yes\n"]

    def poll(script):
        answer = left.pop(0) if left else "done=yes\n"
        if isinstance(answer, Exception):
            raise answer
        return answer

    body = soaking_body(answers={"soak-poll": poll})
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 0
    assert next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")["status"] == "passed"


def test_a_poll_ssh_gave_up_on_is_no_answer_and_never_the_soaks_end(tmp_path, laptop):
    """ssh's own timeout gives a result with code 124 and nothing printed:
    the laptop asks again, and reads the soak only once it says it is
    done."""
    left = [run.Result(124, "", "no answer within 60 s", 60.0)] * 3 + ["alive=yes\n"]

    def poll(script):
        return left.pop(0) if left else "done=yes\n"

    body = soaking_body(answers={"soak-poll": poll})
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 0
    asked = body.steps()
    assert asked.count("soak-poll") == 5 and asked.index("soak-read") > len(asked) - asked[::-1].index("soak-poll") - 1
    assert next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")["status"] == "passed"


def test_a_soak_whose_polls_ssh_keeps_giving_up_on_is_left_to_soak_collect(tmp_path, laptop):
    clock = {"now": base.NOW}

    def poll(script):
        clock["now"] += dt.timedelta(seconds=run.POLL_S)
        return run.Result(124, "", "no answer within 60 s", 60.0)

    body = soaking_body(answers={"soak-poll": poll})
    make(tmp_path, laptop, body, "--soak", "60", clock=lambda: clock["now"]).run()
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "not run" and "--soak-collect" in step["reason"]
    assert "soak-read" not in body.steps(), "the soak's files are never read while it may be running"
    assert "lock=kept for the soak" in body.script("unlock"), "the lock stays with the soak"


def test_a_soak_is_refused_while_the_robot_is_in_use(tmp_path, laptop):
    busy = base.preflight_out(ps=base.QUIET_PS + base.LISTENING_PS, lock="held")
    body = soaking_body(answers={"busy": busy})
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 1
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "refused" and step["reason"].startswith("the robot is in use")
    assert "soak-prepare" not in body.steps() and "soak-start" not in body.steps()


def test_a_soak_that_outlasts_its_length_by_40_minutes_is_left_to_soak_collect(tmp_path, laptop):
    """The body answers each poll that the soak is still alive: past its 60
    minutes and 40 more the laptop stops waiting, and the soak goes on."""
    clock = {"now": base.NOW}
    polls = {"n": 0}

    def poll(script):
        polls["n"] += 1
        assert polls["n"] < 500, "the laptop never stopped waiting"
        clock["now"] += dt.timedelta(seconds=run.POLL_S)
        return "alive=yes\n"

    body = soaking_body(answers={"soak-poll": poll})
    make(tmp_path, laptop, body, "--soak", "60", clock=lambda: clock["now"]).run()
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "not run" and "had not ended by" in step["reason"] and "--soak-collect" in step["reason"]
    assert polls["n"] == 101, "60 minutes and 40 more, at a poll a minute"
    assert "soak-read" not in body.steps()


def test_the_soaks_script_ends_inside_the_40_minutes_the_laptop_waits():
    """Its timed listener may run SOAK_OVERRUN_S past the soak's length and
    the -X dev pass DEV_PASS_S, each stopped at the first sample past its
    bound and killed two samples later."""
    worst = run.SOAK_OVERRUN_S + run.DEV_PASS_S + 6 * run.SAMPLE_S
    assert worst <= 40 * 60 - run.SAMPLE_S, worst
    assert run.DEV_PASS_S + 600 == 40 * 60, "the laptop's deadline past the soak's length"


def test_a_lock_taken_while_the_soak_ended_is_named_in_the_soaks_step(tmp_path, laptop):
    """The soak frees the lock when it ends, and the laptop takes it again
    for the collect; one someone took in between is named, never dropped."""
    taken = iter(["lock=taken\n", "lock=held\nlock_line=token nightly-ab\nlock_line=by tools/body/nightly.sh\n"])
    body = soaking_body(answers={"lock": lambda script: next(taken)})
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 0
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "passed"
    assert step["reason"].endswith("; the run did not take ~/emet-ci/lock again after the soak: ~/emet-ci/lock is "
                                   "held: token nightly-ab / by tools/body/nightly.sh")
    assert step["details"]["lock"].startswith("~/emet-ci/lock is held")
    assert body.steps()[-1] != "unlock", "a lock the run does not hold is left alone"


def test_a_soak_that_is_not_flat_fails_its_step_and_the_run(tmp_path, laptop):
    files = soak_files(samples=samples(rss_mb=lambda i: 96.0 + i * 0.02))
    body = soaking_body(files)
    assert make(tmp_path, laptop, body, "--soak", "60").run() == 1
    said = record(tmp_path)
    step = next(s for s in said["steps"] if s["name"] == "soak")
    assert step["status"] == "failed" and step["reason"].startswith("not flat: resident memory grew 1.20 MB an hour")
    assert said["soak"]["flat"] is False and ", 1 failed or refused," in said["verdict"]


def test_soak_collect_fails_a_soak_that_is_not_flat(tmp_path, laptop):
    find = ("home=/home/alex\nsoak=/home/alex/emet-ci/runs/20261127-031700/soak\ndone=yes\nrelease=0.5.6\n"
            f"minutes=60\ntoken={'e' * 32}\nstarted=2026-11-27T03:17:00\nrecord={{}}\n")
    tar = base.tar_of({"./samples.txt": samples().encode()})
    files = soak_files(listen=FOOTER.format(max="81.20", over=1))
    body = FakeBody({"soak-find": find, "soak-read": soak_read(files), "soak-files": run.Result(0, "", raw=tar),
                     "soak-unlock": "lock=released\n"})
    args = run.parse_args(["--release", "0.5.6", "--soak-collect", "--out", str(tmp_path / "out")])
    runner = run.BodyRun(args, remote=body, laptop=laptop, clock=lambda: base.NOW, sleep=lambda s: None,
                         top=tmp_path / "top")
    assert runner.run() == 1
    said = record(tmp_path)
    assert said["steps"][0]["status"] == "failed" and "1 frame(s) took over 80 ms" in said["steps"][0]["reason"]
    assert said["verdict"].startswith("soak collected: not flat")


def test_a_soak_the_laptop_lost_is_left_to_soak_collect(tmp_path, laptop):
    clock = {"now": base.NOW}

    def poll(script):
        clock["now"] += dt.timedelta(seconds=run.POLL_S)
        raise run.Unreachable("no route")

    body = soaking_body(answers={"soak-poll": poll})
    runner = make(tmp_path, laptop, body, "--soak", "60", clock=lambda: clock["now"])
    runner.run()
    step = next(s for s in record(tmp_path)["steps"] if s["name"] == "soak")
    assert step["status"] == "not run" and "--soak-collect" in step["reason"]
    assert "soak-read" not in body.steps()
    unlock = body.script("unlock")
    assert "lock=kept for the soak" in unlock and "/soak" in unlock, "the lock stays with the soak"


def test_soak_collect_brings_home_a_soak_that_ended_after_the_laptop_left(tmp_path, laptop):
    record_json = json.dumps({"source": {"kind": "pushed commit", "head": "ab" * 20}, "body": {"manifest": "m"}})
    find = ("home=/home/alex\nsoak=/home/alex/emet-ci/runs/20261127-031700/soak\ndone=yes\nrelease=0.5.6\n"
            f"minutes=60\ntoken={'e' * 32}\nstarted=2026-11-27T03:17:00\nrecord={record_json}\n")
    tar = base.tar_of({"./samples.txt": samples().encode(), "./listen.out": FOOTER.format(max="9.8", over=0).encode()})
    body = FakeBody({"soak-find": find, "soak-read": soak_read(soak_files()), "soak-files": run.Result(0, "", raw=tar),
                     "soak-unlock": "lock=released\n"})
    args = run.parse_args(["--release", "0.5.6", "--soak-collect", "--out", str(tmp_path / "out")])
    runner = run.BodyRun(args, remote=body, laptop=laptop, clock=lambda: base.NOW, sleep=lambda s: None,
                         top=tmp_path / "top")
    assert runner.run() == 0
    said = record(tmp_path)
    assert said["soak"]["flat"] is True and said["source"]["head"] == "ab" * 20
    assert [s["name"] for s in said["steps"]] == ["soak", "collect"]
    assert (tmp_path / "out" / "soak" / "samples.txt").is_file()
    assert f'grep -qx "token {"e" * 32}"' in body.script("soak-unlock")


@pytest.mark.parametrize("find, says", [
    ("home=/home/alex\nsoak=\n", "no soak has been started"),
    ("home=/home/alex\nsoak=/s\nalive=yes\nrelease=0.5.6\nminutes=60\nstarted=2026-11-27T03:17:00\n", "still running"),
    ("home=/home/alex\nsoak=/s\ndone=yes\nrelease=0.5.5\nminutes=60\n", "collect it with --release 0.5.5"),
], ids=["none", "still-running", "another-release"])
def test_soak_collect_says_why_it_brought_nothing_home(tmp_path, laptop, find, says):
    body = FakeBody({"soak-find": find})
    args = run.parse_args(["--release", "0.5.6", "--soak-collect", "--out", str(tmp_path / "out")])
    assert run.BodyRun(args, remote=body, laptop=laptop, top=tmp_path / "top").run() == 2
    assert says in record(tmp_path)["verdict"]


# ---------------------------------------------------------- the summary


def judged(**overrides) -> dict:
    return run.summarize_soak(60, soak_files(**overrides))


def test_a_flat_soak_names_its_numbers():
    soak = judged()
    assert soak["flat"] is True and soak["reasons"] == []
    assert soak["rss_growth_mb_per_h"] == 0.0 and soak["rss_start_mb"] == soak["rss_end_mb"] == 96.0
    assert soak["open_files"] == {"first": 14, "last": 14, "max": 14}
    assert soak["threads"] == {"first": 3, "last": 3, "max": 3}
    assert soak["frames"] == 45000 and soak["frames_over_80ms"] == 0 and soak["throttle_flags"] == []
    assert soak["cpu_pct"] == 0.07, "4 ticks a minute at 100 a second: 0.04 s of CPU a minute"


@pytest.mark.parametrize("overrides, says", [
    ({"samples": samples(rss_mb=lambda i: 96.0 + i * 0.02)}, "resident memory grew 1.20 MB an hour, at or over 1"),
    ({"samples": samples(threads=lambda i: 3 + i // 20)}, "threads were not constant: 3 after the warm-up, 5 at the end"),
    ({"samples": samples(fds=lambda i: 14 + (i == 30))}, "open files were not constant: 14 after the warm-up, 14 at the end, 15 at most"),
    ({"listen": FOOTER.format(max="81.20", over=1)}, "1 frame(s) took over 80 ms"),
    ({"samples": samples(throttled=lambda i: "0x50005" if i == 40 else "0x0")},
     "the board raised a throttle flag: under-voltage now, throttled now, under-voltage has occurred"),
    ({"samples": samples(snd=lambda i: 1 if i == 7 else 0)}, "the soak's listener opened a sound device"),
    ({"samples": samples(mine=lambda i: 1 if i == 7 else 0)}, "the soak's listener opened a sound device"),
    # Inside the five-minute warm-up: a device counts at any sample.
    ({"samples": samples(snd=lambda i: 1 if i == 2 else 0)}, "the soak's listener opened a sound device"),
    ({"samples": samples(mine=lambda i: 1 if i == 2 else 0)}, "the soak's listener opened a sound device"),
    ({"dev_samples": samples(n=3, snd=lambda i: 1 if i == 1 else 0)}, "the soak's listener opened a sound device"),
    ({"pcm_after": "pcm=/proc/asound/card3/pcm0c/sub0/status: state: RUNNING, owner_pid : 4321\n"},
     "a sound card was open when the soak ended: /proc/asound/card3/pcm0c/sub0/status"),
    ({"samples": samples(n=7)}, "2 sample(s) after the 5-minute warm-up are too few to judge"),
    ({"samples": samples(rss_mb=lambda i: None)}, "resident memory was read in 0 of the 55 sample(s)"),
    ({"samples": samples(threads=lambda i: None)}, "threads were read in none of the 55 sample(s)"),
    ({"listen": "stopped.\n"}, "the listener printed no footer"),
    ({"listen": FOOTER.format(max="81.00", over=0).replace("80 ms/frame", "100 ms/frame")},
     "the longest frame took 81 ms, over 80 ms"),
    ({"status": STATUS.replace("listen_exit=0", "listen_exit=1")}, "the listener exited 1"),
    ({"status": STATUS.replace("dev_exit=0", "dev_exit=124")}, "the -X dev pass exited 124"),
    # A soak cut short: the feeder died at minute 12, and the listener ended
    # on its pipe's end and exited 0.
    ({"status": STATUS.replace("feed_exit=0", "feed_exit=1"), "samples": samples(n=13),
      "listen": FOOTER.format(max="9.80", over=0).replace("45000  (3600.0s", "9000  (720.0s")},
     "the listener read 12.0 minute(s) of audio of the 60 asked"),
    ({"status": STATUS.replace("feed_exit=0", "feed_exit=1")}, "the feeder exited 1"),
    # The script stopped by TERM at minute 20: finish() wrote its exit and no
    # other.
    ({"status": "clk_tck=100\nsoak_exit=130\nlock=released\n", "samples": samples(n=20),
      "listen": FOOTER.format(max="9.80", over=0).replace("45000  (3600.0s", "15000  (1200.0s")},
     "the listener's exit was not recorded"),
    ({"status": "clk_tck=100\nsoak_exit=130\nlock=released\n"}, "the soak's script exited 130"),
    ({"status": STATUS.replace("dev_exit=0\n", "")}, "the -X dev pass did not run"),
    ({"status": STATUS.replace("dev_exit=0\n", "dev_refused=no pipe could be made in /tmp\n")},
     "the -X dev pass did not run: no pipe could be made in /tmp"),
    ({"status": STATUS + "listen_stopped=it was still running 3780 s after it started\n"},
     "the listener was stopped by the soak's script: it was still running 3780 s after it started"),
    ({"audio": "/home/alex/emet-ci/x.wav\n"}, "audio was written during the soak"),
    ({"status": "refused=a sound card was open when the soak was to start\nsoak_exit=3\n", "samples": "", "listen": ""},
     "the soak did not start: a sound card was open"),
], ids=["memory-growing", "a-thread-leak", "a-file-left-open", "a-frame-over-80-ms", "a-throttle-flag",
        "a-device-file", "a-pcm-of-its-own", "a-device-file-in-the-warm-up", "a-pcm-of-its-own-in-the-warm-up",
        "a-device-under-x-dev", "a-card-open-at-the-end", "too-short", "memory-never-read", "threads-never-read",
        "no-footer", "a-frame-over-80-ms-at-another-budget", "listener-failed", "dev-pass-failed",
        "the-feeder-died", "feeder-failed", "stopped-by-term", "script-failed", "no-dev-pass", "no-pipe-for-dev",
        "a-hung-listener", "audio-written", "refused-at-the-start"])
def test_a_soak_that_is_not_flat_says_why(overrides, says):
    soak = judged(**overrides)
    assert soak["flat"] is False
    assert any(reason.startswith(says) for reason in soak["reasons"]), soak["reasons"]


def test_a_soak_whose_dev_pass_did_not_run_counts_no_warnings():
    soak = judged(status=STATUS.replace("dev_exit=0\n", ""))
    assert soak["dev_warnings"] is None and soak["dev_exit"] is None


def test_the_frames_are_held_to_80_ms_whatever_the_engines_own_budget():
    """The footer's count is against the engine's frame length: at a 40 ms
    frame it counts frames over 40, none of them over 80."""
    at_40 = FOOTER.format(max="61.00", over=5).replace("80 ms/frame", "40 ms/frame")
    soak = judged(listen=at_40)
    assert soak["flat"] is True and soak["frames_over_80ms"] == 0, soak["reasons"]
    assert judged(listen=FOOTER.format(max="9.80", over=0))["frames_over_80ms"] == 0


def test_memory_that_grows_just_under_the_line_is_flat_and_just_over_is_not():
    under = judged(samples=samples(rss_mb=lambda i: 96.0 + i * (0.99 / 60)))
    over = judged(samples=samples(rss_mb=lambda i: 96.0 + i * (1.01 / 60)))
    assert under["flat"] is True and 0.98 < under["rss_growth_mb_per_h"] < 1.0
    assert over["flat"] is False and over["rss_growth_mb_per_h"] >= 1.0


def test_memory_and_threads_of_the_boot_are_left_out():
    """The first five minutes are the boot: memory and threads may rise
    there and the soak is still flat."""
    soak = judged(samples=samples(rss_mb=lambda i: 60.0 + 9 * min(i, 4), threads=lambda i: 2 + (i > 3)))
    assert soak["flat"] is True, soak["reasons"]
    assert soak["rss_start_mb"] == 60.0 and soak["rss_end_mb"] == 96.0


def test_a_throttle_flag_from_before_the_soak_is_none_of_its_doing():
    """The "has occurred" bits stay set from whatever happened since the
    boot, so only one raised during the soak counts."""
    assert judged(samples=samples(throttled=lambda i: "0x50000"))["throttle_flags"] == []
    assert judged(samples=samples(throttled=lambda i: "0x50000" if i < 30 else "0xd0000"))["throttle_flags"] == [
        "soft temperature limit has occurred"]


def test_the_thresholds_are_stage_4s():
    assert (run.RSS_GROWTH_MB_PER_H, run.FRAME_BUDGET_MS, run.WARMUP_S, run.SAMPLE_S) == (1.0, 80.0, 300, 60)
    assert run.SOAK_MINUTES == {"patch": 60, "minor": 480, "major": 4320}


def test_the_longest_soak_fits_the_one_wav_stream_its_listener_reads():
    """The feeder's header names the largest data a wav holds; a soak past
    it would end its listener early, so --soak stops short of it."""
    data = 0xFFFFFFFE - 36
    assert run.SOAK_MAX * 60 * 16000 * 2 <= data < (run.SOAK_MAX + 120) * 60 * 16000 * 2
    assert "DATA = 0xFFFFFFFE - 36" in run.FEEDER_PY
    assert run.parse_args(["--release", "0.5.6", "--tree", "--soak", str(run.SOAK_MAX)]).soak == run.SOAK_MAX
    with pytest.raises(SystemExit):
        run.parse_args(["--release", "0.5.6", "--tree", "--soak", str(run.SOAK_MAX + 1)])


# -------------------------------------------------- the soak's own shell


STAND_IN_LISTEN = '''#!/usr/bin/env python3
"""A stand-in for emet-listen: it reads the wav source the soak gives it
through --replay, as the wav source does, and prints the lines the soak
reads. Under -X dev it raises one warning only development mode shows."""
import sys, time, warnings, wave
args = sys.argv[1:]
path = args[args.index("--replay") + 1]
print("emet-listen 0.5.6, a stand-in")
print("listening for 'hey emet'", flush=True)
warnings.warn("a stand-in's pending change", PendingDeprecationWarning)
frames = 0
with wave.open(path, "rb") as source:
    while len(source.readframes(1280)) == 2560:
        frames += 1
print("\\nsource ended. heard it 0 time(s).\\n")
print("  frames        %d  (%.1fs of audio)" % (frames, frames * 0.08))
print("  per frame     mean 0.01 ms   p50 0.01   p95 0.02   max 0.03")
print("  budget        80 ms/frame, 0 frame(s) over")
print("  verdict       kept up")
'''


@pytest.mark.skipif(sys.platform == "win32" or not Path("/proc/self/status").exists() or not shutil.which("mkfifo"),
                    reason="the soak's script needs Linux: /proc, mkfifo and a pipe Python can open")
def test_the_soaks_script_feeds_samples_and_ends_with_its_markers(tmp_path):
    """SOAK_SH, the feeder and a stand-in listener through a real bash on
    Linux: six seconds of a recording at real time through a pipe, a sample
    a second, the -X dev pass, the lock freed and the pipe gone."""
    home = tmp_path / "home"
    ci = home / "emet-ci"
    folder = ci / "runs" / "x" / "soak"
    (ci / "tmp").mkdir(parents=True)
    folder.mkdir(parents=True)
    wavs = tmp_path / "audio"
    base_wav = wavs / "bank-take.wav"
    wavs.mkdir()
    with wave.open(str(base_wav), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(struct.pack("<16000h", *([300, -300] * 8000)))
    listen = tmp_path / "emet-listen"
    listen.write_text(STAND_IN_LISTEN.replace("#!/usr/bin/env python3", f"#!{sys.executable}"), encoding="utf-8")
    listen.chmod(0o755)
    (folder / "feed.py").write_text(run.FEEDER_PY, encoding="utf-8")
    (folder / "soak.yaml").write_text("{}\n", encoding="utf-8")
    token = "f" * 32
    (ci / "lock").write_text(f"token {token}\nby tools/body/run.py\n")
    params = "".join(f"{k}={v}\n" for k, v in (
        ("SOAK", folder), ("CI", ci), ("PY", sys.executable), ("LISTEN_BIN", listen), ("SOUL", "soul.yaml"),
        ("WAVS", wavs), ("SOAK_S", 6), ("OVERRUN_S", 60), ("DEV_S", 60), ("SAMPLE_S", 1), ("STAMP", "x"),
        ("TOKEN", token)))
    (folder / "soak.sh").write_text(params + run.SOAK_SH, encoding="utf-8")
    start = time.monotonic()
    done = subprocess.run(["bash", str(folder / "soak.sh")], env=dict(os.environ, HOME=str(home)),
                          capture_output=True, timeout=120)
    took = time.monotonic() - start
    assert done.returncode == 0, done.stderr.decode()
    files = {key: (folder / name).read_text() if (folder / name).exists() else ""
             for key, name in (("status", "status"), ("samples", "samples.txt"), ("listen", "listen.out"),
                               ("dev_err", "dev.err"), ("audio", "audio.txt"), ("pcm_before", "pcm-before.txt"),
                               ("pcm_after", "pcm-after.txt"))}
    status = run.key_values(files["status"])
    assert (status["listen_exit"], status["feed_exit"], status["dev_exit"], status["lock"]) == \
        ("0", "0", "0", "released"), files["status"]
    assert took >= 5.5, "the feeder paced six seconds of audio at real time"
    assert (folder / "done").exists() and not (ci / "lock").exists() and not list((ci / "tmp").iterdir())
    rows = run.parse_samples(files["samples"])
    assert len(rows) >= 3 and all(row["rss_kb"].isdigit() and row["threads"].isdigit() for row in rows)
    assert all(row["snd"] == "0" and row["pcm_mine"] == "0" and row["listening"] == "1" for row in rows)
    assert "fed_s=6.0" in (folder / "feed.txt").read_text()
    soak = run.summarize_soak(1, files)
    assert soak["frames"] == 75, "six seconds at 80 ms a frame"
    assert soak["dev_warnings"] == 1 and "PendingDeprecationWarning" in soak["dev_warning_lines"][0]
    assert soak["devices_opened"] == [] and soak["flat"] is False
    assert soak["reasons"] == ["0 sample(s) after the 5-minute warm-up are too few to judge; 3 are needed"]


# ------------------------------------------------- the feeder and the pipe


class OneWay:
    """A file Python cannot seek or tell, as a named pipe is."""

    def __init__(self, raw):
        self.raw = raw

    def read(self, n=-1):
        return self.raw.read(n)

    def tell(self):
        raise OSError("a pipe")

    def seek(self, *args):
        raise OSError("a pipe")


def test_the_feeders_stream_is_a_wav_python_reads_without_seeking(tmp_path, make_wav, make_tone):
    """`once` into a plain file: the header names the largest wav, and wave
    reads every frame from a stream it can neither seek nor tell, which is
    how the wav source reads the pipe. A recording at another rate is
    named and skipped."""
    make_wav(tmp_path / "a.wav", make_tone(1.0))
    make_wav(tmp_path / "b.wav", make_tone(0.5))
    make_wav(tmp_path / "c.wav", make_tone(0.5, rate=48000), rate=48000)
    (tmp_path / "feed.py").write_text(run.FEEDER_PY, encoding="utf-8")
    out = tmp_path / "stream.bin"
    done = subprocess.run([sys.executable, str(tmp_path / "feed.py"), str(out), "once", "0", str(tmp_path)],
                          capture_output=True, timeout=60)
    said = done.stdout.decode()
    assert done.returncode == 0 and "files=2" in said and "skipped=c.wav: 48000 Hz, 1 channel(s), 16-bit" in said
    with out.open("rb") as raw, wave.open(OneWay(raw)) as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        assert source.getnframes() == (0xFFFFFFFE - 36) // 2
        frames = 0
        while len(source.readframes(1280)) == 2560:
            frames += 1
    assert frames == 12 + 6, "1.0 s and 0.5 s, each to its last whole frame"


def test_the_feeder_paces_at_real_time(tmp_path):
    (tmp_path / "feed.py").write_text(run.FEEDER_PY, encoding="utf-8")
    start = time.monotonic()
    done = subprocess.run([sys.executable, str(tmp_path / "feed.py"), str(tmp_path / "q.bin"), "quiet", "0.8", "."],
                          capture_output=True, timeout=60)
    assert done.returncode == 0 and "fed_s=0.8" in done.stdout.decode()
    assert time.monotonic() - start >= 0.7
    data = (tmp_path / "q.bin").read_bytes()[44:]
    assert len(data) == 10 * 2560 and max(abs(v) for v in struct.unpack(f"<{len(data) // 2}h", data)) <= 32


# ---------------------------------------------------------- the budgets


def earlier(top: Path, version: str, day: str, budgets: list | None = None, *, host: str = "emet-pi",
            kernel: str = "Linux 6.18.39+rpt-rpi-2712 aarch64", failed: bool = False, refused: bool = False,
            started: str | None = None, temp: str = "temp=43.3'C") -> Path:
    """A run.json of an earlier release under `top`'s releases/: its suites
    step passed, failed or (as a hardware step outside its window is)
    refused."""
    path = top / "releases" / f"v{version}" / "body" / day / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "failed" if failed else "refused" if refused else "passed"
    doc = {"format": "emet-body-run/2", "started": started or f"{day}T07:33:05", "host": host,
           "preflight": {"kernel": kernel, "temp": temp, "throttled": "throttled=0x0", "throttled_means": ["none"],
                         "loadavg": "0.08 0.02 0.01 1/316 9086"},
           "steps": [{"name": "suites", "status": status}],
           "verdict": "body run done: 6 step(s) passed"}
    if budgets is not None:
        doc["budgets"] = budgets
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def last_row(measure: str, value, spread=None) -> dict:
    return {"measure": measure, "value": value, "spread_pct": spread}


def budget_run(tmp_path, laptop, *extra, budget=None, **answers):
    body = FakeBody({"budget-run": budget or base.budget_run(), **answers})
    code = make(tmp_path, laptop, body, *extra, release="0.5.6").run()
    said = record(tmp_path)
    return code, {r["measure"]: r for r in said["budgets"]}, next(s for s in said["steps"] if s["name"] == "budgets")


def test_with_no_earlier_value_nothing_is_judged_and_nothing_fails(tmp_path, laptop):
    code, rows, step = budget_run(tmp_path, laptop)
    assert code == 0 and step["status"] == "passed"
    assert [r["measure"] for r in record(tmp_path)["budgets"]] == [label for label, _ in run.MEASURES]
    boot = rows["Boot to first frame"]
    assert boot == {"measure": "Boot to first frame", "value": 1800.0, "unit": "ms", "last": None, "last_file": None,
                    "change_pct": None, "bound_pct": 25.0, "within": None, "spread_pct": None, "values": [1800.0]}
    assert rows["Idle CPU"]["value"] == 3.5 and rows["First chunk at the null sink after the final, local path"][
        "value"] == 620.0
    assert rows["Resident memory at the end"]["value"] is None, "only a soak measures it"
    assert all(set(r) >= {"measure", "value", "unit", "last", "last_file", "change_pct", "bound_pct", "within"}
               for r in rows.values())
    assert "7 with no earlier value" in step["reason"] and step["details"]["bank_version"] == "2026-11-25"


def test_a_measure_within_its_bound_passes_and_one_past_it_fails_the_step(tmp_path, laptop):
    top = tmp_path / "top"
    earlier(top, "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1500.0), last_row("Per-frame mean", 2.0)])
    code, rows, step = budget_run(tmp_path, laptop)
    assert rows["Boot to first frame"]["change_pct"] == 20.0 and rows["Boot to first frame"]["within"] is True
    assert rows["Per-frame mean"]["change_pct"] == 20.0 and rows["Per-frame mean"]["within"] is True
    assert code == 0 and step["status"] == "passed"
    assert rows["Boot to first frame"]["last_file"] == "releases/v0.5.5/body/2026-11-25/run.json"
    earlier(tmp_path / "b" / "top", "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1500.0)])
    code, rows, step = budget_run(tmp_path / "b", laptop, budget=base.budget_run(boot_ms=1900.0))
    assert rows["Boot to first frame"]["change_pct"] == 26.7 and rows["Boot to first frame"]["within"] is False
    assert code == 1 and step["status"] == "failed" and "out of bounds: Boot to first frame 1900 ms against 1500 ms" \
        in step["reason"]


def test_a_measure_that_fell_is_within(tmp_path, laptop):
    earlier(tmp_path / "top", "0.5.5", "2026-11-25", [last_row("Per-frame mean", 4.8)])
    code, rows, _ = budget_run(tmp_path, laptop)
    assert rows["Per-frame mean"]["change_pct"] == -50.0 and rows["Per-frame mean"]["within"] is True and code == 0


def test_the_spread_of_repeated_runs_widens_the_bound(tmp_path, laptop):
    """Three runs of the boot at 1500, 1800 and 2100 ms lie 33.3% apart:
    a rise of 28% is then within."""
    earlier(tmp_path / "top", "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1406.0)])
    results = "".join(base.budget_run(1, boot_ms=ms).replace("-1 ", f"-{i} ") for i, ms in
                      enumerate((1500.0, 1800.0, 2100.0), 1))
    code, rows, step = budget_run(tmp_path, laptop, "--repeat", "3", budget=results)
    boot = rows["Boot to first frame"]
    assert boot["values"] == [1500.0, 1800.0, 2100.0] and boot["value"] == 1800.0
    assert boot["spread_pct"] == 33.3 and boot["bound_pct"] == 33.3
    assert boot["change_pct"] == 28.0 and boot["within"] is True and code == 0
    assert step["details"]["repeat"] == 3


def test_a_later_run_of_the_release_takes_the_spread_its_first_run_measured(tmp_path, laptop):
    top = tmp_path / "top"
    earlier(top, "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1406.0)])
    earlier(top, "0.5.6", "2026-11-27", [last_row("Boot to first frame", 1800.0, spread=33.3)])
    code, rows, _ = budget_run(tmp_path, laptop)
    assert rows["Boot to first frame"]["bound_pct"] == 33.3 and rows["Boot to first frame"]["last"] == 1406.0
    assert rows["Boot to first frame"]["within"] is True and code == 0


@pytest.mark.parametrize("other", [
    {"host": "emet-pi-2"},
    {"kernel": "Linux 6.8.0-49-generic x86_64"},
    {"failed": True},
    {"refused": True},
], ids=["another-body", "another-platform", "a-run-that-failed", "a-run-with-a-refused-step"])
def test_a_budget_is_never_held_to_another_bodys_or_a_failed_runs_number(tmp_path, laptop, other):
    """The newest run of the earlier release is another body's, another
    platform's or one that failed; the comparison takes the older run of
    this body that passed."""
    top = tmp_path / "top"
    earlier(top, "0.5.5", "2026-10-28", [last_row("Boot to first frame", 1700.0)])
    earlier(top, "0.5.5", "2026-11-25", [last_row("Boot to first frame", 100.0)], **other)
    code, rows, _ = budget_run(tmp_path, laptop)
    assert rows["Boot to first frame"]["last"] == 1700.0 and rows["Boot to first frame"]["within"] is True
    assert rows["Boot to first frame"]["last_file"] == "releases/v0.5.5/body/2026-10-28/run.json" and code == 0


def test_the_newest_passing_run_of_the_last_release_is_the_last_value(tmp_path, laptop):
    top = tmp_path / "top"
    earlier(top, "0.5.5", "2026-10-28", [last_row("Boot to first frame", 1000.0)])
    earlier(top, "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1700.0)])
    earlier(top, "0.5.5", "2026-11-01", [last_row("Boot to first frame", 1300.0)])
    _, rows, _ = budget_run(tmp_path, laptop)
    assert rows["Boot to first frame"]["last"] == 1700.0
    assert rows["Boot to first frame"]["last_file"] == "releases/v0.5.5/body/2026-11-25/run.json"


def test_a_measure_whose_last_value_was_0_is_held_to_it(tmp_path, laptop):
    """Frames over 80 ms is 0 on every healthy run, so a rise from 0 has no
    percentage: 0 again is within, and any frame over is out of bounds and
    named against the 0."""
    earlier(tmp_path / "top", "0.5.5", "2026-11-25", [last_row("Frames over 80 ms", 0.0)])
    code, rows, step = budget_run(tmp_path, laptop)
    over = rows["Frames over 80 ms"]
    assert (over["value"], over["last"], over["change_pct"], over["within"]) == (0.0, 0.0, 0.0, True)
    assert code == 0 and step["status"] == "passed"
    earlier(tmp_path / "b" / "top", "0.5.5", "2026-11-25", [last_row("Frames over 80 ms", 0.0)])
    code, rows, step = budget_run(tmp_path / "b", laptop, budget=base.budget_run(over=3))
    over = rows["Frames over 80 ms"]
    assert (over["value"], over["last"], over["change_pct"], over["within"]) == (3.0, 0.0, None, False)
    assert code == 1 and step["status"] == "failed"
    assert "out of bounds: Frames over 80 ms 3 against 0, a rise from 0, which no bound allows" in step["reason"]


def test_the_highest_earlier_release_with_the_measure_is_the_last(tmp_path, laptop):
    top = tmp_path / "top"
    earlier(top, "0.5.4", "2026-10-07", [last_row("Per-frame mean", 2.35), last_row("Boot to first frame", 1700.0)])
    earlier(top, "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1750.0)])
    earlier(top, "0.6.0", "2026-12-20", [last_row("Boot to first frame", 1.0)])
    _, rows, _ = budget_run(tmp_path, laptop)
    assert rows["Boot to first frame"]["last"] == 1750.0, "0.6.0 is later than 0.5.6 and never read"
    assert rows["Per-frame mean"]["last"] == 2.35 and rows["Per-frame mean"]["last_file"].startswith("releases/v0.5.4/")


def test_a_format_1_run_gives_the_board_lines_from_its_preflight(tmp_path, laptop):
    """0.5.5's runs recorded no budget, and their preflight holds the board
    lines: that is the first row 0.5.5 gives the table."""
    earlier(tmp_path / "top", "0.5.5", "2026-11-25")
    _, rows, _ = budget_run(tmp_path, laptop)
    board = rows["Board lines (temperature, throttle, load)"]
    assert board["last"] == "43.3'C, throttled 0x0 (none), load 0.08 0.02 0.01"
    assert board["value"].startswith("49.9'C, throttled 0x50000") and board["within"] is None


def test_a_run_of_the_budget_that_failed_fails_the_step(tmp_path, laptop):
    broken = base.budget_run().replace("ran=speak-1 0", "ran=speak-1 1").replace('"exit": 0', '"exit": 1', 2)
    code, rows, step = budget_run(tmp_path, laptop, budget=broken)
    assert code == 1 and step["status"] == "failed" and "runs that failed: speak-1 exit 1" in step["reason"]
    # A run that did not exit 0 gives no number: the speak run's first chunk
    # is not this release's value.
    first = rows["First chunk at the null sink after the final, local path"]
    assert first["values"] == [] and first["value"] is None


def test_frames_over_80_ms_are_the_engines_count_only_at_an_80_ms_frame():
    at_80 = base.driver("frames", over=2)
    at_80["budget_ms"] = 80.0
    at_40 = dict(at_80, budget_ms=40.0)
    assert run.budget_values({"frames-1": at_80})["Frames over 80 ms"] == [2.0]
    assert run.budget_values({"frames-1": at_40})["Frames over 80 ms"] == [], "frames over 40 ms are another measure"


def test_the_soaks_end_joins_the_budget(tmp_path, laptop):
    earlier(tmp_path / "top", "0.5.5", "2026-11-25", [last_row("Resident memory at the end", 90.0)])
    body = soaking_body()
    body.answers["budget-run"] = base.budget_run()
    code = make(tmp_path, laptop, body, "--soak", "60", release="0.5.6").run()
    rows = {r["measure"]: r for r in record(tmp_path)["budgets"]}
    assert rows["Resident memory at the end"]["value"] == 96.0
    assert rows["Resident memory at the end"]["change_pct"] == 6.7 and code == 0


def test_the_budget_runs_hold_to_the_null_sink_and_the_wrapped_engine(tmp_path, laptop):
    body = FakeBody({"budget-prepare": prepared(("budget/listen.yaml", run.MODELS),
                                                ("budget/speak.yaml", base.SPEAK_MODELS), sink="portaudio")})
    assert make(tmp_path, laptop, body).run() == 1
    assert "budget-run" not in body.steps()
    body = FakeBody()
    make(tmp_path / "b", laptop, body).run()
    sent = body.script("budget-run")
    assert run.BUDGET_PY in sent and sent.count('"$PY" "$B/driver.py"') == 3
    assert "--replay /home/alex/voicebank/audio/soak-10min.wav --transcribe --stats" in sent
    assert "--replay /home/alex/voicebank/audio/one-breath-0.5.2.wav --speak --stats" in sent
    assert '--speak --stats' in sent and '--replay "$PIPE" --stats' in sent


def test_the_budgets_driver_reads_the_real_engines_numbers(tmp_path, make_wav):
    """BUDGET_PY around the engine itself, on a recording of quiet noise:
    the wrapped methods are still the ones the engine calls, so the first
    frame, the 99th percentile and the frames come back."""
    pytest.importorskip("pocketsphinx")
    pytest.importorskip("emet_engine.cli")
    import random

    import replay

    noise = random.Random(1)
    wav = make_wav(tmp_path / "quiet.wav", [noise.randint(-40, 40) for _ in range(16000 * 3)])
    (tmp_path / "body.yaml").write_text(replay.body_yaml(None, "1e-15", tmp_path / "turns", None, None),
                                        encoding="utf-8")
    (tmp_path / "soul.yaml").write_text(replay.SOUL_YAML, encoding="utf-8")
    (tmp_path / "keys.env").write_text("", encoding="utf-8")
    (tmp_path / "driver.py").write_text(run.BUDGET_PY, encoding="utf-8")
    start = time.time_ns()
    done = subprocess.run([sys.executable, str(tmp_path / "driver.py"), str(start), str(tmp_path / "r.json"),
                           str(tmp_path / "body.yaml"), str(tmp_path / "soul.yaml"), "--replay", str(wav),
                           "--transcribe", "--stats", "--keys", str(tmp_path / "keys.env"),
                           "--state", str(tmp_path / "state")], capture_output=True, timeout=300,
                          env={k: v for k, v in os.environ.items() if not k.startswith("EMET_")})
    result = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert done.returncode == 0 and result["exit"] == 0, done.stdout.decode()[-800:] + done.stderr.decode()[-800:]
    assert result["frames"] == 37 and result["first_ns"] > start
    # The engine's own count, read through the wrapped method. How many
    # frames go over depends on how busy this machine is, so only that it
    # is a count is held here; keeping up is the soak's question.
    assert isinstance(result["over"], int) and 0 <= result["over"] <= result["frames"]
    assert result["budget_ms"] == 80.0, "pocketsphinx's 1280 samples at 16 kHz"
    assert 0 < result["mean_ms"] <= result["p99_ms"] <= result["max_ms"]
    values = run.budget_values({"frames-1": result})
    assert values["Per-frame 99th percentile"] == [result["p99_ms"]] and len(values["Boot to first frame"]) == 1
    assert "frames        37" in done.stdout.decode(), "the engine's own footer still prints"


# ------------------------------------------------------- the table


def test_the_budget_table_is_the_plans_with_the_platform_commit_and_bank(tmp_path, laptop):
    earlier(tmp_path / "top", "0.5.5", "2026-11-25", [last_row("Boot to first frame", 1500.0),
                                                     last_row("Per-frame mean", 2.0)])
    budget_run(tmp_path, laptop)
    text = run.budget_table(record(tmp_path), "releases/v0.5.6/body/2026-11-27/run.json")
    lines = text.splitlines()
    assert lines[0] == "| Measure | Last release | This release | Change | Bound | File |"
    assert lines[2] == ("| Boot to first frame | 1500 ms (0.5.5) | 1800 ms | +20.0% | 25% | "
                        "`releases/v0.5.6/body/2026-11-27/run.json`; last `releases/v0.5.5/body/2026-11-25/run.json` |")
    assert lines[3].startswith("| First chunk at the null sink after the final, local path | none recorded | 620 ms |")
    assert [line.split(" | ")[0].removeprefix("| ") for line in lines[2:11]] == [label for label, _ in run.MEASURES]
    assert lines[-1].startswith("Platform linux-aarch64 (emet-pi, Linux 6.18 aarch64), commit eb55748 "
                                "(working tree), bank 2026-11-25, repeats 1. Judged 2: 2 within, 0 out of bounds; "
                                "5 with no earlier value.")


def test_the_measures_are_the_plan_templates_rows():
    """MEASURES is a copy of the budget table in planning/templates/plan.md
    §3, which lives beside the repository and not in it, so the test skips
    where the template is not there (CI)."""
    template = run.TOP / "planning" / "templates" / "plan.md"
    if not template.is_file():
        pytest.skip(f"no plan template at {template}")
    sys.path.insert(0, str(BODY.parent))
    try:
        import plan_file
    finally:
        sys.path.remove(str(BODY.parent))
    tables = [t for t in plan_file.tables_under(template.read_text(encoding="utf-8"), "3. Budgets")
              if t.header and t.header[0] == "Measure"]
    assert len(tables) == 1, "one budget table under §3"
    assert [cells[0] for _, cells in tables[0].rows] == [label for label, _ in run.MEASURES]


def test_budget_table_prints_from_the_newest_run_with_budgets(tmp_path, laptop, capsys):
    budget_run(tmp_path, laptop)
    top = tmp_path / "top"
    target = top / "releases" / "v0.5.6" / "body" / "2026-11-27" / "run.json"
    target.parent.mkdir(parents=True)
    shutil.copy(tmp_path / "out" / "run.json", target)
    earlier(top, "0.5.6", "2026-11-26", started="2026-11-26T01:00:00")
    capsys.readouterr()  # the run's own report
    args = run.parse_args(["--release", "0.5.6", "--budget-table"])
    assert run.print_budget_table(args, top) == 0
    out = capsys.readouterr().out
    assert "`releases/v0.5.6/body/2026-11-27/run.json`" in out and out.startswith("| Measure |")
    args = run.parse_args(["--release", "0.5.7", "--budget-table"])
    assert run.print_budget_table(args, top) == 1


# --------------------------------------------------------- the nightly


NIGHTLY_REPORT = """\
format=emet-nightly/1
started=2026-11-27T03:17:01-08:00
head=8efab3a0000000000000000000000000000000ab
subject=0.5.6: Run every release by one playbook
installed=no
suite_emet-sdk=code 0; 226 passed in 2.10s
suite_emet-hal=code 0; 115 passed, 1 skipped in 18.5s
checks=tools/ci.py layering exit 0: ci: all green, 1 job, 1 not run here (layering/zizmor)
bank=code 0; held 13; total: 15 replayed on linux-aarch64 from the carried mean, 13 held to a golden
pcm_after=
verdict=passed
ended=2026-11-27T03:21:44-08:00
"""


def test_the_nightly_report_is_read_into_its_fields():
    report = run.parse_nightly(NIGHTLY_REPORT)
    assert report["verdict"] == "passed" and report["installed"] == "no" and report["head"].startswith("8efab3a")
    assert report["suites"] == {"emet-sdk": "code 0; 226 passed in 2.10s",
                                "emet-hal": "code 0; 115 passed, 1 skipped in 18.5s"}
    assert report["bank"].startswith("code 0; held 13")


class NightlyBody:
    def __init__(self, listed: list[str], text: str | None):
        self.listed, self.text = listed, text

    def run(self, script, *, timeout=60.0):
        fence = re.search(r'echo "(=== [0-9a-f]{32} )\$newest"', script).group(1)
        out = "".join(f"listed={name}\n" for name in self.listed)
        if self.text is not None:
            out += f"{fence}{self.listed[-1]}\n{self.text}"
        return run.Result(0, out)


@pytest.mark.parametrize("text, now, code, says", [
    (NIGHTLY_REPORT, dt.datetime(2026, 11, 27, 9, 0), 0, "verdict    passed"),
    (NIGHTLY_REPORT.replace("verdict=passed", "verdict=failed"), dt.datetime(2026, 11, 27, 9, 0), 1, "verdict    failed"),
    (NIGHTLY_REPORT, dt.datetime(2026, 11, 30, 9, 0), 1,
     "the newest nightly report started 2026-11-27T03:17:01-08:00, 78 h ago, past 48 h: the schedule has stopped"),
    (NIGHTLY_REPORT.replace("started=2026-11-27T03:17:01-08:00\n", ""), dt.datetime(2026, 11, 27, 9, 0), 1,
     "the newest nightly report, 2026-11-27.txt, has no start time this could read; open it at releases/v0.5.6/"),
    (NIGHTLY_REPORT.replace("verdict=passed", "verdict=gave way\nreason=the robot is in use"),
     dt.datetime(2026, 11, 27, 9, 0), 1, "reason     the robot is in use"),
    (NIGHTLY_REPORT.replace("pcm_after=\n", "note=this nightly.sh is not master's tools/body/nightly.sh\n"
                            "pcm_after=pcm=/proc/asound/card3/pcm0c/sub0/status: state: RUNNING\nlater=a new line\n"),
     dt.datetime(2026, 11, 27, 9, 0), 0, "note       this nightly.sh is not master's tools/body/nightly.sh\n"
     "  verdict    passed\n  later      a new line"),
], ids=["passed", "failed", "stale", "no-start-time", "gave-way", "a-note-and-a-line-it-does-not-know"])
def test_the_nightly_report_says_whether_the_last_night_passed_and_when(tmp_path, capsys, text, now, code, says):
    args = run.parse_args(["--release", "0.5.6", "--nightly-report"])
    body = NightlyBody(["2026-11-26.txt", "2026-11-27.txt"], text)
    assert run.nightly_report(args, body, top=tmp_path, now=lambda: now) == code
    out = capsys.readouterr().out
    assert says in out and "nightly reports on the body: 2, from 2026-11-26.txt to 2026-11-27.txt" in out
    assert (tmp_path / "releases" / "v0.5.6" / "body" / "nightly" / "2026-11-27.txt").read_text() == text


def test_no_nightly_report_is_a_failure_to_read(tmp_path, capsys):
    args = run.parse_args(["--release", "0.5.6", "--nightly-report"])
    assert run.nightly_report(args, NightlyBody([], None), top=tmp_path) == 1
    assert "no nightly report on the body" in capsys.readouterr().out


def nightly(home: Path, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """nightly.sh's functions, sourced into a real bash with HOME a folder of
    the test's and `env` added to its environment, and then `script`."""
    bash = base.posix_bash()
    source = NIGHTLY_SH.read_text(encoding="utf-8").replace("\r\n", "\n")
    (home / "nightly.sh").parent.mkdir(parents=True, exist_ok=True)
    (home / "nightly.sh").write_text(source, encoding="utf-8", newline="\n")
    env = dict(os.environ, HOME=str(home), NIGHTLY_SOURCE_ONLY="1", **(env or {}))
    return subprocess.run([bash, "-c", f'source "$HOME/nightly.sh"\n{script}'], env=env, capture_output=True,
                          timeout=60)


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
    ("4252 python3 -m emet_engine.clinic", False),
])
def test_the_nightly_gives_way_to_the_same_programs_as_the_runner(tmp_path, line, busy):
    assert bool(run.busy_lines(line)) is busy, "the runner's own pattern, as the cases say"
    done = nightly(tmp_path / "home", f"printf '%s\\n' {json.dumps(line)} | busy_lines")
    assert bool(done.stdout.decode().strip()) is busy, done.stderr.decode()


def test_the_nightly_installs_with_the_runners_line():
    text = NIGHTLY_SH.read_text(encoding="utf-8")
    assert f'EXTRAS="{" ".join(run.INSTALL)}"' in text, "the same line gives the same install key"
    assert f'PACKAGES="{" ".join(run.PACKAGES)}"' in text
    assert f'UPSTREAM="{run.UPSTREAM}"' in text


def test_the_nightly_gives_way_while_the_lock_is_held_and_writes_why(tmp_path):
    home = tmp_path / "home"
    (home / "emet-ci").mkdir(parents=True)
    (home / "emet-ci" / "lock").write_bytes(b"token someone\nby the maintainer\n")
    done = nightly(home, 'gate')
    assert done.stdout.decode().startswith("~/emet-ci/lock is held: token someone by the maintainer"), \
        done.stdout.decode() + done.stderr.decode()
    (home / "emet-ci" / "lock").unlink()
    done = nightly(home, 'take_lock && echo "taken $TOKEN" && cat "$CI/lock" && release_lock && '
                         '[ ! -e "$CI/lock" ] && echo freed')
    out = done.stdout.decode()
    assert re.search(r"^taken nightly-[0-9a-f]{32}$", out, re.M) and out.strip().endswith("freed"), out
    (home / "emet-ci" / "lock").write_bytes(b"token someone\n")
    done = nightly(home, 'TOKEN=nightly-x; release_lock; cat "$CI/lock"')
    assert done.stdout.decode() == "token someone\n", "another's lock is left where it is"


def test_the_nightly_keeps_its_newest_thirty_reports_and_writes_one_the_runner_reads(tmp_path):
    home = tmp_path / "home"
    folder = home / "emet-ci" / "nightly"
    folder.mkdir(parents=True)
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(33)]
    for day in days:
        (folder / f"{day}.txt").write_text("verdict=passed\n")
        (folder / str(day)).mkdir()
    # A night that gave way is a report too, and sorts before its day's.
    for day in (days[0], days[5]):
        (folder / f"{day}.gave-way.txt").write_text("verdict=gave way\n")
    strays = ("README.txt", f"{days[1]}.txt.bak", f".{days[2]}.txt.part")
    for name in strays:
        (folder / name).write_text("not a report\n")
    done = nightly(home, 'REPORT="$NIGHTLY/2026-11-03.txt"; PART="$NIGHTLY/.part"; say format emet-nightly/1; '
                         'say started 2026-11-03T03:17:01-08:00; say suite_emet-sdk "code 0; 226 passed"; '
                         'finish passed ""')
    assert done.returncode == 0, done.stderr.decode()
    kept = sorted(p.name for p in folder.glob("*.txt") if p.name != "README.txt")
    assert len(kept) == 30 and kept[0] == f"{days[5]}.gave-way.txt" and kept[-1] == "2026-11-03.txt", kept
    assert not (folder / f"{days[0]}.gave-way.txt").exists() and not (folder / f"{days[4]}.txt").exists()
    assert not (folder / str(days[0])).exists() and (folder / str(days[5])).exists()
    assert all((folder / name).exists() for name in strays), "a file that is no report stays"
    report = run.parse_nightly((folder / "2026-11-03.txt").read_text())
    assert report["verdict"] == "passed" and report["suites"] == {"emet-sdk": "code 0; 226 passed"}
    assert report["format"] == "emet-nightly/1" and "ended" in report


def test_the_nightly_takes_its_bank_step_as_the_runner_does():
    text = NIGHTLY_SH.read_text(encoding="utf-8")
    bank = text[text.index("bank() {"):text.index("main() {")]
    assert '[ "$held" != "0" ]' in bank and '[ "$held" != "unknown" ]' in bank, "a replay that held nothing fails"
    assert "--search $BANK/audio" in bank and "credential.helper=" in text, "anonymous: no credential is used"


#: The runner's venv on a stand-in body for the nightly: its Python writes
#: down each EMET_ variable it was given, and answers pytest and the checks
#: with the codes the test names.
NIGHTLY_PYTHON = '''#!/bin/sh
env | grep '^EMET_' >> "$HOME/emet-seen.txt"
case "$1 $2" in "-m pytest") exit "${SUITE_CODE:-0}" ;; esac
case "$1" in tools/check_*) exit "${CHECK_CODE:-0}" ;; esac
exit 0
'''


def nightly_main(home: Path, tmp_path: Path, *, ps: str = "1 /sbin/init\n", cards: dict[str, str] | None = None,
                 env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, Path]:
    """nightly.sh's main() through a real bash: `ps` and the sound cards are
    stand-ins, and so are the fetch and the install, which would reach the
    network. Everything else, the gate, the lock, the EMET_ unset, the
    suites, the checks and the verdict, is the script's own."""
    ci = home / "emet-ci"
    (ci / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (ci / ".venv" / "bin" / "python").write_text(NIGHTLY_PYTHON, encoding="utf-8", newline="\n")
    (ci / ".venv" / "bin" / "python").chmod(0o755)
    for package in run.PACKAGES:
        (ci / "emet" / package).mkdir(parents=True, exist_ok=True)
    (ci / "emet" / "tools").mkdir(exist_ok=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "ps.txt").write_text(ps, encoding="utf-8", newline="\n")
    (stubs / "ps").write_text(f'#!/bin/sh\ncat "{shell_path(stubs / "ps.txt")}"\n', encoding="utf-8", newline="\n")
    (stubs / "ps").chmod(0o755)
    asound = tmp_path / "asound"
    for name, text in (cards or {"card2/pcm0p/sub0/status": "closed\n"}).items():
        (asound / name).parent.mkdir(parents=True, exist_ok=True)
        (asound / name).write_text(text, encoding="utf-8", newline="\n")
    script = (f'PATH={shlex.quote(shell_path(stubs))}:"$PATH"\nASOUND={shlex.quote(shell_path(asound))}\n'
              'sync_master() { say head 8efab3a; say subject "0.5.6: a stand-in"; }\n'
              'install_tree() { say installed no; }\n'
              'main; echo "main=$?"\n')
    done = nightly(home, script, env=env)
    return done, home / "emet-ci" / "nightly"


def today() -> str:
    return dt.date.today().isoformat()


@pytest.mark.parametrize("ps, cards, says", [
    ("1 /sbin/init\n4242 /home/alex/Documents/emet/.venv/bin/python3 /home/alex/Documents/emet/.venv/bin/emet-listen "
     "a b\n", None, "the robot is in use: 4242 "),
    ("1 /sbin/init\n", OPEN_CARD, "a sound card is open: "),
], ids=["a-listener-running", "a-card-open"])
def test_the_nightlys_main_gives_way_without_touching_the_days_run(tmp_path, ps, cards, says):
    """It gives way to a program that means the robot is in use and to an
    open card, as the runner does, in a report of its own name: the day's
    report and folder, a run cron left, are as they were."""
    home = tmp_path / "home"
    folder = home / "emet-ci" / "nightly"
    (folder / today()).mkdir(parents=True)
    (folder / today() / "suite-emet-sdk.txt").write_text("226 passed\n")
    (folder / f"{today()}.txt").write_text("format=emet-nightly/1\nverdict=passed\n")
    done, folder = nightly_main(home, tmp_path, ps=ps, cards=cards)
    assert "main=0" in done.stdout.decode(), done.stderr.decode()
    gave = run.parse_nightly((folder / f"{today()}.gave-way.txt").read_text())
    assert gave["verdict"] == "gave way" and gave["reason"].startswith(says), gave
    assert (folder / f"{today()}.txt").read_text() == "format=emet-nightly/1\nverdict=passed\n"
    assert (folder / today() / "suite-emet-sdk.txt").exists(), "the day's folder is left as it was"
    assert not (home / "emet-ci" / "lock").exists() and not (home / "emet-seen.txt").exists()


def test_the_nightlys_main_leaves_a_held_lock_and_the_days_run_alone(tmp_path):
    home = tmp_path / "home"
    folder = home / "emet-ci" / "nightly"
    (folder / today()).mkdir(parents=True)
    (folder / today() / "bank-report.json").write_text("{}\n")
    (folder / f"{today()}.txt").write_text("format=emet-nightly/1\nverdict=passed\n")
    (home / "emet-ci" / "lock").write_text("token run-x\nby tools/body/run.py\n", newline="\n")
    done, folder = nightly_main(home, tmp_path)
    assert "main=0" in done.stdout.decode(), done.stderr.decode()
    gave = run.parse_nightly((folder / f"{today()}.gave-way.txt").read_text())
    assert gave["reason"] == "~/emet-ci/lock is held: token run-x by tools/body/run.py "
    assert (folder / f"{today()}.txt").read_text() == "format=emet-nightly/1\nverdict=passed\n"
    assert (folder / today() / "bank-report.json").exists()
    assert (home / "emet-ci" / "lock").read_text() == "token run-x\nby tools/body/run.py\n"


@pytest.mark.parametrize("codes, verdict", [
    ({}, "passed"),
    ({"SUITE_CODE": "1"}, "failed"),
    ({"CHECK_CODE": "1"}, "failed"),
], ids=["all-passed", "a-suite-failed", "a-check-failed"])
def test_the_nightlys_main_runs_with_no_emet_variable_and_reports_its_verdict(tmp_path, codes, verdict):
    home = tmp_path / "home"
    env = {"EMET_LIVE_TESTS": "1", "EMET_ANTHROPIC_API_KEY": "not-a-key", **codes}
    done, folder = nightly_main(home, tmp_path, env=env)
    out = done.stdout.decode()
    assert f"main={0 if verdict == 'passed' else 1}" in out, out + done.stderr.decode()
    report = run.parse_nightly((folder / f"{today()}.txt").read_text())
    assert report["verdict"] == verdict, report
    assert report["head"] == "8efab3a" and set(report["suites"]) == set(run.PACKAGES)
    assert (home / "emet-seen.txt").read_text() == "", "no EMET_ variable reached a suite or a check"
    assert not (home / "emet-ci" / "lock").exists(), "the nightly freed its own lock"


# ---------------------------------------------------- the network cut's script


def netcut(*args: str, script: Path = NETCUT_SH) -> subprocess.CompletedProcess:
    return subprocess.run([base.posix_bash(), str(script), *args], capture_output=True, timeout=60)


@pytest.mark.parametrize("args", [[], ["30", "x"], ["4"], ["601"], ["0030"], ["30;id"], ["$(id)"], ["-1"], [""],
                                  ["--help"], ["1e2"]])
def test_emet_netcut_takes_one_whole_number_within_its_bounds_and_nothing_else(args):
    done = netcut(*args)
    assert done.returncode == 2 and done.stderr.decode().startswith("usage: emet-netcut SECONDS"), done.stderr


def test_emet_netcut_needs_root_and_says_so_before_it_touches_anything():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("this test runs as root")
    done = netcut("30")
    assert done.returncode == 3 and "run it as root" in done.stderr.decode()


def test_emet_netcut_always_restores_and_its_rule_lapses_by_itself():
    """Read from the script: the trap that deletes the table on every way
    out, and the element's timeout that ends the cut if the script is killed
    outright; the element goes in last."""
    text = NETCUT_SH.read_text(encoding="utf-8")
    assert "trap restore EXIT" in text and "trap 'exit 143' TERM" in text and "trap 'exit 130' INT" in text
    assert 'nft delete table inet "$TABLE"' in text[text.index("restore() {"):text.index("trap restore EXIT")]
    assert "flags timeout;" in text and '"{ $PORT timeout $((seconds + SPARE_S))s }"' in text
    lines = [line for line in text.splitlines() if line.startswith("nft add")]
    assert lines[-1].startswith("nft add element"), "the cut starts with the last rule"
    assert "readonly MAX_S=600" in text and "readonly PORT=443" in text
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in text, "sudo's caller chooses no program it runs"


def stubbed_netcut(tmp_path: Path) -> tuple[Path, Path]:
    """A copy of netcut.sh whose PATH line names a folder of stand-ins: an
    `id` that says root and an `nft` that writes down what it was asked."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "nft.log"
    (stubs / "id").write_text("#!/bin/sh\necho 0\n", encoding="utf-8", newline="\n")
    (stubs / "nft").write_text(f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\n', encoding="utf-8", newline="\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    text = NETCUT_SH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.count("PATH=/usr/sbin:/usr/bin:/sbin:/bin\n") == 1
    copy = tmp_path / "netcut-copy.sh"
    copy.write_text(text.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin\n", f"PATH={shell_path(stubs)}:$PATH\n"),
                    encoding="utf-8", newline="\n")
    return copy, log


def test_emet_netcut_cuts_waits_and_restores(tmp_path):
    copy, log = stubbed_netcut(tmp_path)
    start = time.monotonic()
    done = netcut("5", script=copy)
    assert done.returncode == 0, done.stderr.decode()
    assert time.monotonic() - start >= 4.5
    asked = log.read_text().splitlines()
    assert asked[0] == "delete table inet emet_netcut"
    assert asked[-2] == "add element inet emet_netcut cut { 443 timeout 10s }"
    assert asked[-1] == "delete table inet emet_netcut", "the trap restored it"
    assert "add rule inet emet_netcut output tcp dport @cut drop" in asked
    assert "add rule inet emet_netcut output udp dport @cut drop" in asked, "UDP to 443 too, as its cut: line says"
    out = done.stdout.decode().splitlines()
    assert out[0].startswith("cut: outbound TCP and UDP to port 443") and out[-1].startswith("restored: ")


def test_emet_netcut_restores_when_it_is_stopped(tmp_path):
    """TERM during the wait: the trap runs at once and deletes the table."""
    copy, log = stubbed_netcut(tmp_path)
    script = (f'bash "{copy.as_posix()}" 60 > "{(tmp_path / "out").as_posix()}" 2>&1 & pid=$!\n'
              f'for i in $(seq 1 50); do grep -q "^cut:" "{(tmp_path / "out").as_posix()}" 2>/dev/null && break; '
              'sleep 0.1; done\nkill -TERM "$pid"; wait "$pid"; echo "code=$?"\n')
    start = time.monotonic()
    done = subprocess.run([base.posix_bash(), "-c", script], capture_output=True, timeout=60)
    assert time.monotonic() - start < 20, "the wait ended with the signal, not after 60 s"
    assert "code=143" in done.stdout.decode(), done.stdout.decode() + done.stderr.decode()
    assert log.read_text().splitlines()[-1] == "delete table inet emet_netcut"
    assert (tmp_path / "out").read_text().splitlines()[-1].startswith("restored: ")


# ------------------------------------------------------ the shell scripts


@pytest.mark.parametrize("script", [NIGHTLY_SH, NETCUT_SH], ids=["nightly.sh", "netcut.sh"])
def test_the_shell_scripts_parse(script):
    done = subprocess.run([base.posix_bash(), "-n", str(script)], capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not on this machine")
@pytest.mark.parametrize("script", [NIGHTLY_SH, NETCUT_SH], ids=["nightly.sh", "netcut.sh"])
def test_shellcheck_finds_nothing(script):
    done = subprocess.run(["shellcheck", "-S", "warning", str(script)], capture_output=True, timeout=120)
    assert done.returncode == 0, done.stdout.decode()
