#!/usr/bin/env python3
"""Every CI job's steps, in one script that GitHub, the laptop and the body run.

    python tools/ci.py --list            every job: name, blocking or advisory, where it runs
    python tools/ci.py JOB [JOB ...]     run them; exit 1 when a blocking job fails
    python tools/ci.py --all             every job this machine can run, naming each it could not and why

    --install     run each job's `pip install` lines, with the running
                  interpreter, where the job places them: most come first,
                  and a line that only an advisory step needs comes just
                  before that step. GitHub passes it. The laptop leaves it
                  out and uses what is installed.
    --log FILE    write everything printed to FILE as well, making its folder

**Why this exists.** The steps of `ci.yml` used to be written twice: in the
workflow, and in a shell script written again for each release beside the
plan (`releases/v0.5.5/ci/local_ci_055.sh`, with three helper scripts). A
rule that lives only in a workflow is broken by the next change, because
nobody can run the workflow before pushing. Now a job is data plus steps in
this file. A workflow keeps the runner, the matrix and the machine setup, and
its one command is `python tools/ci.py <job> --install`. One script that CI
and a person both call is microduck's `scripts/board-test.sh`, which its
`board` job runs (Pollen Robotics, Apache-2.0): the idea taken, no code.

**Three kinds of job.** A *blocking* job turns its check red when a step
fails. Eight blocking checks are required by the master ruleset, and `--list`
marks them; `virtual sound card` is blocking and outside the ruleset, and
`release_audit.py --merge` holds a merge on it all the same, since the audit
reads every check. An *advisory* job never turns its check red: on GitHub
(`GITHUB_ACTIONS` set) it prints a `::warning`, writes the step summary and
exits 0. On the laptop it is named in the verdict line as failed advisory and
leaves `--all` green.

**Not run is never passed.** A script another change has not delivered yet,
or a tool this machine does not have, is `not run here: <reason>`, and the
verdict line names it. On GitHub nothing is allowed to be missing, so there a
step of a blocking job that could not run fails its job.

**What GitHub gives a job stays in the environment:** `BASE_SHA` and
`HEAD_SHA` (dco), `BASE_SHA` (mutmut), `PR` and `GH_TOKEN` (audit),
`EMET_VIRTUAL_AUDIO` (sound, with `PULSE_SERVER`, which the workflow's setup
step writes to `GITHUB_ENV`), `EMET_BANK_GOLDEN_OUT` (bank and bank-full:
where the goldens this platform lacks are written for the workflow to
upload), `GITHUB_STEP_SUMMARY` (every job's table). The variables that
change what a test run means never reach a job (`RUN_CHANGING`), and the
first lines say which were left out.

**Tools.** Standard library only, so the script runs on a bare Python
(`python -S -I tools/ci.py --list`, which the `layering` job checks). build,
zizmor, pip-audit and flake8 are used from the running interpreter when
they are installed there, then from the tools venv (`tools/.venv`), then
from `PATH`.
coverage and mutmut run the suites, so they are used only from the
interpreter that runs this script, which can import the four packages.
Under `--install` the pinned set comes from `tools/ci/requirements.txt`, by
hash.

**Time limits, on GitHub.** A workflow ends a job at its `timeout-minutes`,
and a job ended there does not pass, whatever its kind. So each job with an
advisory part carries its workflow's minutes in the table below, and on
GitHub that part stops `WORKFLOW_MARGIN` seconds before them: a command
still running then is ended with every process it started, and its step
fails, advisory. An advisory job then warns and exits 0. A blocking job's
blocking steps keep the workflow's own limit. Off GitHub nothing is timed.

**The tree is left as it was found.** Wheels, the fresh venv, the shadow
modules, pip's reports and coverage's data go to a folder under the system
temp folder, removed at the end.

The last line is the verdict: `ci: all green, N jobs, M not run here (names)`
or `ci: FAILED (names)`.

Usage:  python tools/ci.py --list | --all | JOB [JOB ...]  [--install] [--log FILE]
Exit:   0 green (an advisory failure included), 1 a blocking job failed, 2 a
        command line this script does not know.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

#: The modules behind the optional extras. A body that only ever wakes
#: installs none of them, so every test that needs one has to skip.
OPTIONAL_MODULES = ("pocketsphinx", "sounddevice", "websockets", "httpx", "piper")

#: The pinned, hashed set of CI-only tools (stream B2's file).
CI_TOOLS = "tools/ci/requirements.txt"

#: What the test jobs install, as `ci.yml` did. emet-hal and emet-providers
#: are installed too, and for more than their own tests: plugin discovery
#: reads entry points, so several SDK and engine tests are meaningless unless
#: something is registered to discover.
TEST_INSTALL = (
    "emet-sdk[dev]",
    "emet-hal[dev]",
    "emet-providers[dev,deepgram,anthropic,openai]",
    "emet-engine[dev]",
)

#: Files the SDK's wheel must carry. In a source checkout `schemas/` sits
#: beside the package and `force-include` moves it inside the wheel, so a
#: typo there leaves every suite green and breaks the first `pip install`.
#: `py.typed` is PEP 561's marker: without it in the wheel a type checker
#: ignores the SDK's annotations (review RB200).
WHEEL_DATA = (
    "emet_sdk/schemas/body-manifest.schema.json",
    "emet_sdk/schemas/soul-bundle.schema.json",
    "emet_sdk/schemas/motion-pack.schema.json",
    "emet_sdk/chains/core.yaml",
    "emet_sdk/chains/express.yaml",
    "emet_sdk/py.typed",
)

#: The engine's one data file, the --events schema (0.5.7 contract 2.2).
ENGINE_WHEEL_DATA = ("emet_engine/schemas/events-1.schema.json",)

#: Variables that change what a test run means, left out of every job's
#: environment (review RB275): snapshots rewritten in place of compared,
#: one seed or a wider range in place of the default seeds, and a live call
#: to a vendor, which no CI run makes.
RUN_CHANGING = ("EMET_UPDATE_SNAPSHOTS", "EMET_PROPERTY_SEED", "EMET_PROPERTY_SEEDS", "EMET_LIVE_TESTS",
                "EMET_LIVE_RECORD")

#: The bank tools' pinned list, which pins numpy, the bank builder's one
#: optional package, and the test that holds numpy's arithmetic to plain
#: Python's (review RC124).
BANK_REQUIREMENTS = "tools/bank/requirements.txt"
NUMPY_TEST = "tools/tests/test_bank_generate.py::test_numpy_gives_the_same_whole_numbers_as_plain_python"

#: What must be discovered from the installed wheels: one name per group.
DISCOVERED = (
    ("locomotion", "differential"), ("locomotion", "tracked"), ("driver", "emet_hal.mock"),
    ("wake", "pocketsphinx"), ("audio", "microphone"), ("audio_out", "speaker"),
    ("stt", "mock"), ("llm", "mock"), ("tts", "mock"),
)

PASSED, FAILED, NOT_RUN = "passed", "failed", "not run"

#: `git commit -s` writes this line; the DCO check reads it, any case.
SIGN_OFF = re.compile(r"^Signed-off-by: .+ <.+@.+>", re.IGNORECASE | re.MULTILINE)

#: 0.5.7-D10 (b): a line of a commit message that names a co-author or the
#: tool that wrote it, read case-insensitively. CLAUDE.md rule 1 allows no
#: trailer beyond the sign-off, and 0.4.1's merged body carried such a line.
ATTRIBUTION = re.compile(r"^co-authored-by:|generated with|noreply@anthropic\.com", re.IGNORECASE)

#: mutmut joins a class and its method with this letter in a mutant's name
#: (mutmut's `format_utils.py`, read 2026-11-26).
MUTMUT_CLASS_SEPARATOR = "\u01c1"

#: What a workflow's own steps around `tools/ci.py` may take inside its
#: `timeout-minutes`, in seconds: the checkout, setup-python, the sound
#: card's apt-get and PulseAudio lines, an upload. On GitHub an advisory part
#: of a job stops this long before the workflow would end the job.
WORKFLOW_MARGIN = 180


# --------------------------------------------------------------- processes


@dataclass(frozen=True)
class Done:
    """A finished command: its exit code and what it printed, both streams.
    `timed_out` says it was ended at its time limit, or never started
    because the limit had passed."""

    code: int
    out: str = ""
    timed_out: bool = False


def _decode(raw: bytes) -> str:
    """A child's line as text. Most children write UTF-8; a Python child on
    Windows writes the locale's encoding into a pipe."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if os.name == "nt" else "utf-8", errors="replace")


def _end_tree(process: subprocess.Popen) -> None:
    """End a command and every process it started. On POSIX the command
    leads a process group of its own, and the group is killed. On Windows
    `taskkill /T` ends the tree from the command's process id; a venv's
    `python.exe` there is a launcher whose child holds the pipe, so killing
    the launcher alone would leave the read waiting."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            process.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass


def run_command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                echo: Callable[[str], None] | None = None, timeout: float | None = None) -> Done:
    """Start one command and wait for it. Every job's command goes through
    here, so the tests replace this one function.

    With `timeout`, the command and every process it started are ended once
    that many seconds have passed, and the Done says so. A timed command
    leads a process group of its own on POSIX, where it no longer hears the
    terminal's Ctrl-C, so an interrupt of this script ends it too."""
    own_group = timeout is not None and os.name != "nt"
    try:
        process = subprocess.Popen([str(a) for a in argv], cwd=str(cwd) if cwd else None, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=own_group)
    except OSError as error:
        text = f"{argv[0]}: {error}\n"
        if echo:
            echo(text)
        return Done(127, text)
    expired = threading.Event()
    timer = None
    if timeout is not None:
        def expire() -> None:
            expired.set()
            _end_tree(process)

        timer = threading.Timer(max(timeout, 0.0), expire)
        timer.daemon = True
        timer.start()
    lines: list[str] = []
    try:
        with process:                   # closes the pipe and waits, whatever happens while reading
            assert process.stdout is not None
            for raw in process.stdout:
                text = _decode(raw).replace("\r\n", "\n")
                lines.append(text)
                if echo:
                    echo(text)
    except BaseException:
        if timeout is not None:
            _end_tree(process)
        raise
    finally:
        if timer is not None:
            timer.cancel()
    if expired.is_set():
        text = f"ci: ended after {timeout:.0f} s, its time limit\n"
        lines.append(text)
        if echo:
            echo(text)
        return Done(process.returncode or 124, "".join(lines), timed_out=True)
    return Done(process.returncode, "".join(lines))


class Output:
    """Where everything is printed: the console, and the `--log` file,
    whose folder is made when it does not exist yet (a new release's
    `releases/vX.Y.Z/ci/` has none until the first run writes there)."""

    def __init__(self, log: Path | None = None) -> None:
        if log:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log, "w", encoding="utf-8", newline="\n") if log else None

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        if self._log:
            self._log.write(text)
            self._log.flush()

    def close(self) -> None:
        if self._log:
            self._log.close()


def _has_module(name: str) -> bool:
    """Whether the running interpreter can import it, read afresh: under
    `--install` a module may have arrived since this process started."""
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str = ""


def passed(reason: str = "") -> Outcome:
    return Outcome(PASSED, reason)


def failed(reason: str) -> Outcome:
    return Outcome(FAILED, reason)


def not_run(reason: str) -> Outcome:
    """This machine could not run the step. Never a pass."""
    return Outcome(NOT_RUN, reason)


@dataclass(frozen=True)
class Step:
    """One step of a job.

    `install` marks a `pip install` line, run only under `--install`.
    `always` runs the step even after an earlier step of its job failed.
    `advisory` makes its failure a warning. `gate` marks a step the later
    ones stand on: when it failed or could not run, they are not reached,
    and an advisory gate's failure that leaves a blocking step unreached is
    the job's failure.
    """

    name: str
    key: str
    run: Callable[["Context"], Outcome]
    install: bool = False
    always: bool = False
    advisory: bool = False
    gate: bool = False


@dataclass(frozen=True)
class Job:
    key: str
    checks: tuple[str, ...]              # the GitHub check names, exactly
    blocking: bool                       # False is advisory
    ruleset: bool                        # one of the eight the master ruleset requires
    where: str                           # for --list
    steps: Callable[["Context"], list[Step]]
    cannot: Callable[["Context"], str] = lambda ctx: ""   # why it cannot run here, or ""
    minutes: int | None = None           # the workflow's timeout-minutes, for a job with an advisory part

    @property
    def kind(self) -> str:
        return "blocking" if self.blocking else "advisory"

    @property
    def limit(self) -> float | None:
        """Seconds the job's advisory part may run on GitHub, from its start."""
        return self.minutes * 60.0 - WORKFLOW_MARGIN if self.minutes else None


@dataclass
class StepResult:
    step: Step
    outcome: Outcome
    seconds: float


@dataclass
class JobResult:
    job: Job
    status: str
    reason: str = ""
    steps: list[StepResult] = field(default_factory=list)
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def parts_not_run(self) -> list[str]:
        """Steps this machine could not run, inside a job that otherwise ran."""
        if self.status == NOT_RUN:
            return []
        return [f"{self.job.key}/{r.step.key}" for r in self.steps if r.outcome.status == NOT_RUN]

    @property
    def parts_failed_advisory(self) -> list[str]:
        return [f"{self.job.key}/{r.step.key}" for r in self.steps
                if r.outcome.status == FAILED and r.step.advisory]


# ------------------------------------------------------------------ context


@dataclass
class Context:
    """What a job needs to know about the machine. The tests build their own."""

    root: Path
    env: dict[str, str]
    out: Any
    runner: Callable[..., Done] = run_command
    install: bool = False
    python: str = sys.executable
    version: tuple[int, int] = (sys.version_info[0], sys.version_info[1])
    os_name: str = sys.platform
    machine: str = platform.machine().lower()
    temp: Path | None = None
    has_module: Callable[[str], bool] = _has_module
    which: Callable[..., str | None] = shutil.which
    has_fork: bool = hasattr(os, "fork")
    clock: Callable[[], float] = time.monotonic
    state: dict[str, Any] = field(default_factory=dict)      # one job's, emptied before each
    notes: list[str] = field(default_factory=list)           # one job's lines for the summary
    deadline: float | None = None       # on `clock`, when the running step's commands must end; None is no limit
    ran_out: bool = False               # a command of the running step met the deadline
    _scratch: Path | None = None

    @property
    def github(self) -> bool:
        return bool(self.env.get("GITHUB_ACTIONS"))

    @property
    def windows(self) -> bool:
        return self.os_name == "win32"

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def note(self, text: str) -> None:
        """A line for the job's summary, printed now as well."""
        self.say(text)
        self.notes.append(text)

    def run(self, argv: list[Any], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            quiet: bool = False) -> Done:
        """Run a command in the repository (or `cwd`), with `env` added to
        the job's environment. `quiet` keeps its output off the console.
        Under a deadline the command gets what is left of it, and one asked
        for after the deadline is not started."""
        argv = [str(a) for a in argv]
        if not quiet:
            self.say("+ " + shlex.join(argv))
        how: dict[str, float] = {}
        if self.deadline is not None:
            left = self.deadline - self.clock()
            if left <= 0:
                self.ran_out = True
                text = "ci: not started, the time limit has passed\n"
                if not quiet:
                    self.out.write(text)
                return Done(124, text, timed_out=True)
            how["timeout"] = left
        done = self.runner(argv, cwd=cwd or self.root, env={**self.env, **(env or {})},
                           echo=None if quiet else self.out.write, **how)
        if done.timed_out:
            self.ran_out = True
        return done

    def check(self, argv: list[Any], **how: Any) -> Outcome:
        done = self.run(argv, **how)
        return passed() if done.code == 0 else failed(f"exit {done.code}")

    def scratch(self, name: str) -> Path:
        """A folder under the system temp folder, outside the repository."""
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="emet-ci-", dir=str(self.temp) if self.temp else None))
        folder = self._scratch / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def close(self) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            if self._scratch.exists():
                self.say(f"left in the temp folder, in use by another process: {self._scratch}")
            self._scratch = None

    def exe(self, name: str) -> str:
        return name + ".exe" if self.windows else name

    def in_venv(self, venv: Path, name: str) -> Path:
        return venv / ("Scripts" if self.windows else "bin") / self.exe(name)

    def console(self, name: str) -> str:
        """An installed console script, looked for beside the interpreter and then on PATH."""
        beside = Path(self.python).parent / self.exe(name)
        if beside.is_file():
            return str(beside)
        return self.which(name, path=self.env.get("PATH")) or name

    def tool(self, name: str, module: str | None = None) -> list[str] | None:
        """The command for a CI-only tool, or None when this machine has none:
        the running interpreter first, then the tools venv, then PATH."""
        if module and self.has_module(module):
            return [self.python, "-m", module]
        beside = Path(self.python).parent / self.exe(name)
        if beside.is_file():
            return [str(beside)]
        venv = self.root / "tools" / ".venv"
        if self.in_venv(venv, name).is_file():
            return [str(self.in_venv(venv, name))]
        if module and self.in_venv(venv, "python").is_file() and (
                list(venv.glob(f"Lib/site-packages/{module}")) or list(venv.glob(f"lib/python*/site-packages/{module}"))):
            return [str(self.in_venv(venv, "python")), "-m", module]
        found = self.which(name, path=self.env.get("PATH"))
        return [found] if found else None


# ------------------------------------------------------------- shared steps


def pip_step(name: str, *args: str, advisory: bool = False) -> Step:
    """A `pip install` line, with the running interpreter, under `--install`."""

    def run(ctx: Context) -> Outcome:
        for i, arg in enumerate(args):
            if arg == "-r" and not (ctx.root / args[i + 1]).is_file():
                return not_run(f"{args[i + 1]} is missing")
        return ctx.check([ctx.python, "-m", "pip", "install", *args])

    return Step(name, "install", run, install=True, gate=True, advisory=advisory)


def package_installs(extras: tuple[str, ...]) -> list[Step]:
    return [pip_step(f"Install {spec}", "-e", spec) for spec in extras]


def ci_tools_step(why: str, advisory: bool = False) -> Step:
    """The CI-only tools, every one pinned by hash in `tools/ci/requirements.txt`
    (CI3), where `ci.yml` used to run `pip install --upgrade build`. A job
    that installed them says so in its state, for `ci_tools_licences`."""
    step = pip_step(f"Install the CI tools, pinned by hash ({why})", "--require-hashes", "-r", CI_TOOLS,
                    advisory=advisory)

    def run(ctx: Context) -> Outcome:
        outcome = step.run(ctx)
        if outcome.status == PASSED:
            ctx.state["ci-tools"] = True
        return outcome

    return replace(step, run=run)


def ci_tools_licences(ctx: Context) -> Outcome:
    """BVP1 for the CI lock, contract §5's `--requirements` line: each pin of
    `tools/ci/requirements.txt` held to D9's rule with MPL-2.0 allowed, its
    licence read from the interpreter this job installed the lock into. A
    Dependabot bump of the lock reaches this step. It runs only where the
    job installed the lock, since a pin's licence is known once pip has
    resolved it. Advisory until it passes on master's tree (RELEASE_PROCESS.md
    §11)."""
    if not ctx.state.get("ci-tools"):
        return not_run(f"needs {CI_TOOLS} installed by this job (--install)")
    return script(ctx, "tools/check_licences.py", "--requirements", CI_TOOLS, "--python", ctx.python)


def script(ctx: Context, path: str, *args: str, env: dict[str, str] | None = None) -> Outcome:
    """Run one of the repository's own check scripts. A script that is not
    in the tree is not run here, and says so."""
    if not (ctx.root / path).is_file():
        return not_run(f"{path} is missing")
    return ctx.check([ctx.python, path, *args], env=env)


def script_step(name: str, key: str, path: str, *args: str, **how: Any) -> Step:
    return Step(name, key, lambda ctx: script(ctx, path, *args), **how)


def pytest_line(ctx: Context, *, dev: bool = False) -> list[str]:
    """`python -m pytest -q`, as `ci.yml` ran it. The cache plugin is off so
    a run leaves no `.pytest_cache` behind; nothing in CI reads the cache."""
    return [ctx.python, *(("-X", "dev") if dev else ()), "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def suite_steps(*, dev: bool = False, always: bool = False, shadowed: bool = False) -> list[Step]:
    """The four suites, each in its own package's folder.

    emet-providers runs with no network and no key: the mocks need nothing,
    every real provider is tested against a stand-in server, and the live
    tests skip unless a key and EMET_LIVE_TESTS=1 are set. emet-engine runs
    without a microphone, an acoustic model or a network: the loop is
    exercised through a wav file, the mock detector and the mock transcriber,
    reached by the same entry-point discovery the real ones use.
    """

    def one(package: str) -> Step:
        def run(ctx: Context) -> Outcome:
            env = ctx.state.get("shadow") if shadowed else None
            return ctx.check(pytest_line(ctx, dev=dev), cwd=ctx.root / package, env=env)

        return Step(f"Test {package}" + (" under -X dev" if dev else ""), package, run, always=always)

    return [one(package) for package in PACKAGES]


# ----------------------------------------------------------------- layering


def zizmor(ctx: Context) -> Outcome:
    """zizmor over `.github` (CI3): the workflows and the Dependabot file. It
    finds an unpinned action, a persisted credential, an expression inside a
    `run:` line. Offline, so the same audits run on GitHub and on the laptop.
    A finding the project accepts is listed with its reason in zizmor's
    configuration, `tools/ci/zizmor.yml` when that file exists, otherwise the
    one zizmor finds by itself. zizmor is MIT (PyPI, verified 2026-11-26);
    the version is `tools/ci/requirements.in`'s, and these options were run
    on 1.32.0 that day."""
    tool = ctx.tool("zizmor")
    if tool is None:
        return not_run("needs zizmor")
    if not (ctx.root / ".github" / "workflows").is_dir():
        return not_run(".github/workflows is missing")
    argv = [*tool, "--offline", "--no-progress", "--color", "never"]
    for name in ("tools/ci/zizmor.yml", "tools/ci/zizmor.yaml"):
        if (ctx.root / name).is_file():
            argv += ["--config", name]
            break
    return ctx.check([*argv, ".github"])


def standard_library_only(ctx: Context) -> Outcome:
    """The body runner and this script start on a bare Python: `-S` leaves
    site-packages off the path, so an import from outside the standard
    library fails here."""
    bad = []
    for path, arg in (("tools/body/run.py", "--help"), ("tools/ci.py", "--list")):
        if not (ctx.root / path).is_file():
            continue
        done = ctx.run([ctx.python, "-S", "-I", path, arg], quiet=True)
        ctx.say(f"{path} {arg} under -S -I: " + ("ok" if done.code == 0 else f"exit {done.code}"))
        if done.code != 0:
            ctx.say(done.out.rstrip()[-2000:])
            bad.append(path)
    return failed("needs more than the standard library: " + ", ".join(bad)) if bad else passed()


#: 0.5.7-D14: flake8 holds only the four codes the code's `# noqa` markers
#: already name, over the four packages and `tools/`. `--extend-exclude`
#: keeps flake8's own exclude, `.svn,CVS,.bzr,.hg,.git,__pycache__,.tox,
#: .nox,.eggs,*.egg` (flake8 7.5.0's `defaults.py`, read 2026-11-27 in the
#: pinned wheel), and adds the venvs inside the scanned folders
#: (`emet-sdk/.venv`, `tools/.venv`) and mutmut's `mutants/`.
FLAKE8_SELECT = ("E402", "E731", "F401", "F821")
FLAKE8_EXCLUDE = (".venv", "mutants")
FLAKE8_PATHS = (*PACKAGES, "tools")

#: Its first run, 2026-11-27, found 8 sites no marker covers (4 F401, 4
#: F821), so the step warned until stream A12's sweep fixed them that day.
#: The sweep set this True, and the step blocks `layering`.
FLAKE8_BLOCKS = True


def flake8(ctx: Context) -> Outcome:
    """D14's linter. flake8 7.5.0 with pyflakes 4.1.0, pycodestyle 2.15.0 and
    mccabe 0.7.0, the PyCQA's, each MIT (PyPI's JSON API, read 2026-11-27:
    flake8 and pycodestyle declare `MIT` with no classifier, pyflakes `MIT`
    with the MIT classifier, mccabe `Expat license` with the MIT
    classifier), pinned by hash in `tools/ci/requirements.txt`."""
    tool = ctx.tool("flake8", "flake8")
    if tool is None:
        return not_run("needs flake8")
    paths = [p for p in FLAKE8_PATHS if (ctx.root / p).is_dir()]
    return ctx.check([*tool, "--select=" + ",".join(FLAKE8_SELECT), "--extend-exclude=" + ",".join(FLAKE8_EXCLUDE),
                      *paths])


def pr_body(ctx: Context) -> Path | None:
    """The release's pull request body beside the repository,
    `../releases/vX.Y.Z/pr-body.md` for the version `emet-sdk` declares, when
    this machine has it. The laptop does; GitHub's checkout does not."""
    try:
        project = tomllib.loads((ctx.root / "emet-sdk" / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None
    body = ctx.root.parent / "releases" / f"v{project.get('version', '')}" / "pr-body.md"
    return body if body.is_file() else None


def house_style(ctx: Context) -> Outcome:
    """`check_style.py` over the tree, and over the release's pull request
    body with `--file` where it is beside the repository (0.5.7-D10 (c)),
    since the body is published on GitHub as surely as a file is."""
    body = pr_body(ctx)
    return script(ctx, "tools/check_style.py", ".", *(("--file", str(body)) if body else ()))


def layering_steps(ctx: Context) -> list[Step]:
    """Layering, release invariants, house style, code health, the workflows.

    The layering check exists because the closed-engine plan used to enforce
    that boundary structurally, and an open monorepo does not. The release
    invariants are what no single suite can see: the four packages agreeing
    about the version, every discovery group having something behind it, no
    document advertising a version the code is not. House style is mechanised
    because 0.3 removed 178 em-dashes by hand after they had passed review
    one at a time. `check_health.py` exits 1 only when the SDK's contract
    modules pass 4,000 lines (E8); a budgeted number that rose is a warning
    it prints (GV5 stage 1). After zizmor, `tools/ci/check_workflows.py`
    holds the workflows to the house rules zizmor does not hold (stream
    B2's), so a broken workflow fails the job that checks the workflows.
    Last, flake8 with the four codes the `# noqa` markers name (D14),
    blocking while `FLAKE8_BLOCKS` is True, as the sweep set it.
    """
    return [
        ci_tools_step("zizmor, flake8, and vulture for the health report"),
        script_step("Check package layering", "layering", "tools/check_layering.py", "."),
        script_step("Check release invariants", "release", "tools/release_check.py", "."),
        Step("Check house style", "style", house_style),
        script_step("Check code health and the SDK's line budget", "health", "tools/check_health.py", "."),
        Step("Check the workflows with zizmor", "zizmor", zizmor),
        script_step("Check the workflows' house rules", "workflows", "tools/ci/check_workflows.py", "."),
        Step("The tools that start on a bare Python need only the standard library", "stdlib",
             standard_library_only),
        Step("Lint with flake8, the four codes the noqa markers name", "flake8", flake8,
             advisory=not FLAKE8_BLOCKS),
    ]


# ---------------------------------------------------------------------- dco


def branch_commits(ctx: Context) -> tuple[str, list[str]] | Outcome:
    """The pull request's range and its commits, or the outcome that says
    why they cannot be read. GitHub gives the range as BASE_SHA and
    HEAD_SHA; the laptop reads origin/master..HEAD."""
    base, head = ctx.env.get("BASE_SHA", ""), ctx.env.get("HEAD_SHA", "")
    if not (base and head):
        if ctx.github:
            return failed("BASE_SHA and HEAD_SHA are not set: this job is for pull requests")
        base, head = "origin/master", "HEAD"
    listed = ctx.run(["git", "rev-list", f"{base}..{head}"], quiet=True)
    if listed.code != 0:
        return not_run(f"needs the history of {base}..{head}: " + (listed.out.strip().splitlines() or ["git failed"])[-1])
    return f"{base}..{head}", listed.out.split()


def dco(ctx: Context) -> Outcome:
    """Every commit of the pull request carries Signed-off-by."""
    found = branch_commits(ctx)
    if isinstance(found, Outcome):
        return found
    span, commits = found
    missing = 0
    for sha in commits:
        message = ctx.run(["git", "log", "-1", "--format=%B", sha], quiet=True).out
        if not SIGN_OFF.search(message):
            subject = (message.strip().splitlines() or [""])[0]
            ctx.say(f"missing sign-off: {sha}  {subject}")
            missing += 1
    if missing:
        ctx.say()
        ctx.say("Emet uses a DCO. Sign your commits with 'git commit -s',")
        ctx.say("or add sign-off to existing work with 'git rebase --signoff <base>'.")
        ctx.say("See CONTRIBUTING.md.")
        return failed(f"{missing} of {len(commits)} commits without Signed-off-by")
    ctx.say(f"all commits signed off ({len(commits)} in {span})")
    return passed()


def attribution_lines(message: str) -> list[str]:
    """The lines of a commit message that name a co-author or a tool."""
    return [line.strip() for line in message.splitlines() if ATTRIBUTION.search(line.strip())]


def no_attribution(ctx: Context) -> Outcome:
    """0.5.7-D10 (b): no commit of the pull request carries a co-author or a
    tool's line. A commit is public once pushed, and the squash on master
    keeps its title only, so the branch's own commits are read here, each
    of them, before the merge can make one part of master's history."""
    found = branch_commits(ctx)
    if isinstance(found, Outcome):
        return found
    span, commits = found
    carrying = 0
    for sha in commits:
        message = ctx.run(["git", "log", "-1", "--format=%B", sha], quiet=True).out
        lines = attribution_lines(message)
        if lines:
            carrying += 1
            title = (message.strip().splitlines() or [""])[0]
            for line in lines:
                ctx.say(f'commit {sha[:7]} "{title}" carries an attribution line: {line}. Emet\'s commits carry no '
                        f"co-author or tool line; the sign-off names the author. Rewrite the message and push again.")
    if carrying:
        return failed(f"{carrying} of {len(commits)} commits carry an attribution line")
    ctx.say(f"no commit carries an attribution line ({len(commits)} in {span})")
    return passed()


def dco_steps(ctx: Context) -> list[Step]:
    return [Step("Every commit carries Signed-off-by", "sign-off", dco),
            Step("No commit carries a co-author or a tool's line", "attribution", no_attribution, always=True)]


# -------------------------------------------------------------------- wheel


def normalise(name: str) -> str:
    """A distribution's name as the index compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def first_party(name: str) -> bool:
    """An Emet name: `emet`, or anything starting `emet-`. Emet publishes
    none of them to an index, so one that resolves from an index is another
    party's project."""
    name = normalise(name)
    return name == "emet" or name.startswith("emet-")


def requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return match.group(1) if match else ""


def wheel_requirements(wheel: Path) -> list[str]:
    """Every `Requires-Dist` of a built wheel, markers dropped."""
    with zipfile.ZipFile(wheel) as archive:
        name = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
        text = archive.read(name).decode("utf-8") if name else ""
    found = []
    for line in text.splitlines():
        if not line.strip():
            break                                    # the headers end at the first blank line
        if line.startswith("Requires-Dist:"):
            found.append(line.partition(":")[2].partition(";")[0].strip())
    return found


def first_party_requirements(requirements: list[str]) -> list[str]:
    return [r for r in requirements if first_party(requirement_name(r))]


def missing_wheel_data(names: set[str]) -> list[str]:
    return [required for required in WHEEL_DATA if required not in names]


def compare_builds(first: dict[str, str], second: dict[str, str]) -> list[str]:
    """BVP3. Two builds of one commit, by file name and SHA-256: every way
    they differ. A build is reproducible when the same source, environment
    and instructions give the same bytes (Reproducible Builds' definition,
    reproducible-builds.org/docs/definition, an idea)."""
    if not first:
        return ["there is no wheel to compare"]
    problems = []
    for name in sorted(set(first) | set(second)):
        if name not in second:
            problems.append(f"{name}: built the first time and not the second")
        elif name not in first:
            problems.append(f"{name}: built the second time and not the first")
        elif first[name] != second[name]:
            problems.append(f"{name}: sha256 {first[name][:12]} the first time, {second[name][:12]} the second")
    return problems


#: pip saying it could not reach the index, as against the index answering
#: that it has nothing.
_UNREACHED = re.compile(r"Retrying \(Retry|connection broken|ConnectionError|ProxyError|SSLError|timed out|"
                        r"Temporary failure in name resolution|getaddrinfo failed", re.IGNORECASE)
_NO_MATCH = re.compile(r"No matching distribution found for\s+([A-Za-z0-9][A-Za-z0-9._-]*)")
_FROM_VERSIONS = re.compile(r"\(from versions: ([^)]*)\)")


def index_verdict(code: int, output: str, report: str, *, alone: bool) -> tuple[bool, str]:
    """CI1's decision on one `pip install --dry-run --report`, run against the
    index with no sibling wheel offered: (ok, what to print).

    The hole it closes: `emet-sdk` on PyPI is another party's project (FUSE's,
    0.1.0 to 0.1.3, installing a module named `emet_sdk`; pip listed those
    versions on 2026-11-26). `emet-providers` once declared `emet-sdk>=0.1`,
    so installing its wheel alone fetched that project with no error. With
    the floor at the release's own version the index holds nothing that
    satisfies it, and pip's "No matching distribution found" for an Emet name
    is the passing result. Offering the sibling wheels through `--find-links`
    cannot make this check: pip takes the highest version that satisfies, so
    Emet's own wheel hides the hole.

    `alone` is one wheel by itself, which may fail to resolve in that one
    way. The four together must resolve, every Emet name from a local file.
    """
    if code == 0:
        try:
            items = json.loads(report)["install"]
            if not (isinstance(items, list) and all(isinstance(item, dict) for item in items)):
                raise TypeError("the report's install list has another shape")
        except (ValueError, KeyError, TypeError):
            return False, "pip exited 0 and wrote no report this script can read, so nothing was decided"
        fetched = []
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            url = item.get("download_info", {}).get("url", "")
            if first_party(name) and not url.startswith("file:"):
                fetched.append(f"{name} {item.get('metadata', {}).get('version', '?')} would come from {url}")
        if fetched:
            return False, "an Emet name resolves from the index: " + "; ".join(fetched)
        return True, f"resolves, {len(items)} distributions, every Emet name from a local file"
    unmet = [name for name in _NO_MATCH.findall(output) if first_party(name)]
    if _UNREACHED.search(output):
        return False, "pip could not reach the index, so nothing was decided"
    if unmet and alone:
        versions = _FROM_VERSIONS.search(output)
        seen = f" (the index offers {versions.group(1).strip()})" if versions else ""
        return True, f"the index holds nothing that satisfies {', '.join(sorted(set(unmet)))}{seen}"
    last = (output.strip().splitlines() or [f"exit {code}"])[-1]
    return False, f"pip failed: {last}"


def source_date_epoch(ctx: Context) -> dict[str, str]:
    """SOURCE_DATE_EPOCH from the head commit, which hatchling uses for every
    build timestamp. With no commit to read, hatchling's own fixed default
    applies (hatch.pypa.io/latest/config/build, verified 2026-11-25 in the
    survey), so the two builds still agree."""
    done = ctx.run(["git", "log", "-1", "--format=%ct", "HEAD"], quiet=True)
    stamp = done.out.strip()
    if done.code == 0 and stamp.isdigit():
        ctx.say(f"SOURCE_DATE_EPOCH={stamp}, the head commit's time")
        return {"SOURCE_DATE_EPOCH": stamp}
    ctx.say("SOURCE_DATE_EPOCH: no commit to read here, so the build backend's fixed default applies")
    return {}


def _build_into(ctx: Context, tool: list[str], folder: Path) -> Outcome:
    for package in PACKAGES:
        done = ctx.run([*tool, "--wheel", "--outdir", folder, ctx.root / package], env=ctx.state["epoch"])
        if done.code != 0:
            return failed(f"{package} did not build (exit {done.code})")
    return passed()


def _wheels(folder: Path) -> dict[str, Path]:
    """The wheel of each package in a folder; a package with none is absent."""
    found = {}
    for package in PACKAGES:
        files = sorted(folder.glob(package.replace("-", "_") + "-*.whl"))
        if files:
            found[package] = files[-1]
    return found


def _hashes(folder: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(folder.glob("*.whl"))}


def build_wheels(ctx: Context) -> Outcome:
    """Build real wheels, outside the tree. An editable install keeps the
    source layout, so every other job exercises only the path no user of the
    project ever takes."""
    tool = ctx.tool("pyproject-build", module="build")
    if tool is None:
        return not_run("needs build")
    ctx.state["build"] = tool
    ctx.state["epoch"] = source_date_epoch(ctx)
    dist = ctx.scratch("dist-1")
    outcome = _build_into(ctx, tool, dist)
    if outcome.status != PASSED:
        return outcome
    wheels = _wheels(dist)
    lacking = [p for p in PACKAGES if p not in wheels]
    if lacking:
        return failed("the build wrote no wheel for " + ", ".join(lacking))
    ctx.state["dist"], ctx.state["wheels"] = dist, wheels
    for path in wheels.values():
        ctx.say(path.name)
    return passed()


def rebuild_and_compare(ctx: Context) -> Outcome:
    """BVP3: build each wheel a second time, in a second folder, and fail
    when a hash differs."""
    again = ctx.scratch("dist-2")
    outcome = _build_into(ctx, ctx.state["build"], again)
    if outcome.status != PASSED:
        return outcome
    problems = compare_builds(_hashes(ctx.state["dist"]), _hashes(again))
    for problem in problems:
        ctx.say(f"  x {problem}")
    if problems:
        return failed(f"{len(problems)} wheel(s) differ between two builds of one commit")
    for name, digest in _hashes(again).items():
        ctx.say(f"{digest}  {name}")
    ctx.say("both builds gave the same bytes")
    return passed()


def wheel_data(ctx: Context) -> Outcome:
    wheel = ctx.state["wheels"]["emet-sdk"]
    with zipfile.ZipFile(wheel) as archive:
        missing = missing_wheel_data(set(archive.namelist()))
    if missing:
        ctx.say(f"{wheel.name} is missing packaged data files:")
        for name in missing:
            ctx.say(f"  - {name}")
        return failed(f"{len(missing)} data file(s) missing from the SDK's wheel")
    engine = ctx.state["wheels"]["emet-engine"]
    with zipfile.ZipFile(engine) as archive:
        lacking = [name for name in ENGINE_WHEEL_DATA if name not in archive.namelist()]
    if lacking:
        return failed(f"{engine.name} is missing {', '.join(lacking)}")
    ctx.say(f"all {len(WHEEL_DATA)} data files present in {wheel.name}")
    return passed()


def fresh_venv(ctx: Context) -> Outcome:
    """A venv that has never seen the source tree, for the wheels."""
    venv = ctx.scratch("venv")
    ctx.state["venv"] = venv
    return ctx.check([ctx.python, "-m", "venv", "--clear", venv])


def index_dry_run(ctx: Context) -> Outcome:
    """CI1: no install of the four wheels may resolve an Emet name from the
    index. See `index_verdict` for the hole."""
    wheels: dict[str, Path] = ctx.state["wheels"]
    pip = [ctx.in_venv(ctx.state["venv"], "python"), "-m", "pip", "install", "--disable-pip-version-check",
           "--dry-run", "--ignore-installed"]
    bad = 0

    # The four are a closed set: each Emet requirement is met by a wheel
    # built here, with the index out of reach.
    wanted: dict[str, list[str]] = {}
    for package, wheel in wheels.items():
        for requirement in first_party_requirements(wheel_requirements(wheel)):
            wanted.setdefault(requirement, []).append(package)
    for requirement, by in wanted.items():
        done = ctx.run([*pip, "--no-index", "--no-deps", "--find-links", ctx.state["dist"], requirement], quiet=True)
        ok = done.code == 0
        bad += not ok
        ctx.say(f"{'ok ' if ok else 'x  '} {requirement} (asked by {', '.join(by)}): "
                + ("a wheel built here satisfies it" if ok else "no wheel built here satisfies it"))

    # Each wheel alone against the index, no sibling offered, then the four.
    reports = ctx.scratch("reports")
    runs = [(package, [wheel], True) for package, wheel in wheels.items()]
    runs.append(("the four together", list(wheels.values()), False))
    for label, files, alone in runs:
        report = reports / (label.replace(" ", "-") + ".json")
        done = ctx.run([*pip, "--quiet", "--report", report, *files], quiet=True)
        text = report.read_text(encoding="utf-8") if report.is_file() else ""
        ok, line = index_verdict(done.code, done.out, text, alone=alone)
        bad += not ok
        ctx.say(f"{'ok ' if ok else 'x  '} {label}: {line}")
    return failed(f"{bad} resolution(s) wrong") if bad else passed()


def install_wheels(ctx: Context) -> Outcome:
    python = ctx.in_venv(ctx.state["venv"], "python")
    return ctx.check([python, "-m", "pip", "install", "--disable-pip-version-check",
                      *ctx.state["wheels"].values()])


def cli_outside(ctx: Context) -> Outcome:
    """From a folder outside the checkout the source tree is not on the
    path, so anything that resolves comes out of the installed packages."""
    venv, outside = ctx.state["venv"], ctx.scratch("outside")
    examples = ctx.root / "emet-sdk" / "examples"
    emet = ctx.in_venv(venv, "emet")
    lines = [
        ([emet, "--version"], False),
        ([ctx.in_venv(venv, "emet-listen"), "--help"], True),
        ([ctx.in_venv(venv, "emet-talk"), "--help"], True),
        ([ctx.in_venv(venv, "emet-say"), "--help"], True),
        ([emet, "validate", examples / "bodiless.yaml"], False),
        ([emet, "validate", examples / "mock-scout.yaml", "--verify-drivers"], False),
        ([emet, "explain", examples / "mock-scout.yaml", "--why"], False),
    ]
    for argv, quiet in lines:
        done = ctx.run(argv, cwd=outside, quiet=quiet)
        if done.code != 0:
            return failed(f"{Path(str(argv[0])).stem} {' '.join(str(a) for a in argv[1:2])} exited {done.code}")
    return passed()


def discovery_from_wheel(ctx: Context) -> Outcome:
    """Entry points and versions, read back inside the fresh venv by this
    script's own `--probe discovery`."""
    return ctx.check([ctx.in_venv(ctx.state["venv"], "python"), ctx.root / "tools" / "ci.py", "--probe", "discovery"],
                     cwd=ctx.scratch("outside"))


def wheel_steps(ctx: Context) -> list[Step]:
    return [
        ci_tools_step("build"),
        Step("Build wheels", "build", build_wheels, gate=True),
        Step("Build them again and compare the hashes", "rebuild", rebuild_and_compare),
        Step("Data files are actually inside the wheel", "data", wheel_data),
        Step("A fresh environment for the wheels", "venv", fresh_venv, gate=True),
        Step("No Emet name resolves from the index", "index", index_dry_run),
        Step("Install the wheels, not the source", "install-wheels", install_wheels, gate=True),
        Step("CLI works from outside the source tree", "cli", cli_outside),
        Step("Plugin discovery works from an installed wheel", "discovery", discovery_from_wheel),
    ]


# --------------------------------------------------------------------- test


def emet_line(ctx: Context, *args: Any, quiet: bool = False) -> Done:
    return ctx.run([ctx.console("emet"), *args], cwd=ctx.root / "emet-sdk", quiet=quiet)


def valid_examples(ctx: Context) -> Outcome:
    """Against the editable install. The `wheel` job checks the packaged
    layout."""
    done = emet_line(ctx, "validate", "examples/bodiless.yaml", "examples/scout-01.yaml", "examples/emet-soul.yaml")
    return passed() if done.code == 0 else failed(f"exit {done.code}")


def boot_strictness(ctx: Context) -> Outcome:
    """A manifest wired entirely to installed plugins must pass the strict
    check the engine applies at boot."""
    done = emet_line(ctx, "validate", "examples/mock-scout.yaml", "--verify-drivers")
    return passed() if done.code == 0 else failed(f"exit {done.code}")


def binding_tables(ctx: Context) -> Outcome:
    for args in (("explain", "examples/bodiless.yaml"), ("explain", "examples/mock-scout.yaml", "--why")):
        done = emet_line(ctx, *args)
        if done.code != 0:
            return failed(f"emet {' '.join(args)} exited {done.code}")
    return passed()


def invalid_examples(ctx: Context) -> Outcome:
    fixtures = sorted((ctx.root / "emet-sdk" / "examples" / "invalid").glob("*.yaml"))
    if not fixtures:
        return failed("there is no invalid fixture to reject")
    clean = []
    for fixture in fixtures:
        if emet_line(ctx, "validate", f"examples/invalid/{fixture.name}", quiet=True).code == 0:
            ctx.say(f"FAIL: examples/invalid/{fixture.name} validated clean but should not have")
            clean.append(fixture.name)
    if clean:
        return failed(f"{len(clean)} invalid fixture(s) validated clean")
    ctx.say(f"all {len(fixtures)} invalid fixtures rejected")
    return passed()


def tools_tests(dev: bool, shadowed: bool = False) -> Step:
    """The tools' own tests: the bank's tools, the body runner, the audit,
    this script. They import no package beyond the four, so one Python is
    enough, and 3.13 is the one the laptop and the reference body run. Tests
    that need the bank's audio skip without it. `shadowed` runs them with
    the `bare` job's shadow folder first on the path."""

    def run(ctx: Context) -> Outcome:
        env = ctx.state.get("shadow") if shadowed else None
        return ctx.check([*pytest_line(ctx, dev=dev), "tools/tests"], env=env)

    name = "Test tools" + (" under -X dev" if dev else "") + (" with every optional extra absent" if shadowed else "")
    return Step(name, "tools", run)


#: `pytest --collect-only -q` ends on "1010 tests collected in 2.10s".
#: `planning/check_planning.py` reads the same line.
_COLLECTED = re.compile(r"(\d+) tests? collected")


def collected_count(text: str) -> int | None:
    """The count on the last line `pytest --collect-only -q` printed, or
    None when that line names none."""
    lines = text.strip().splitlines()
    match = _COLLECTED.match(lines[-1].strip()) if lines else None
    return int(match.group(1)) if match else None


def collected_counts(ctx: Context) -> Outcome:
    """How many tests each suite collects: the numbers the planning
    documents quote and `planning/check_planning.py` holds them to.
    `releases/v0.5.5/ci/local_ci_055.sh` printed each package's count
    before its suite. Printed, never held to a number: a count that moves
    is the release's own news."""
    counts: list[tuple[str, int]] = []
    unread = []
    suites = [*((package, ctx.root / package, ()) for package in PACKAGES), ("tools/tests", ctx.root, ("tools/tests",))]
    for name, cwd, args in suites:
        done = ctx.run([*pytest_line(ctx), "--collect-only", *args], cwd=cwd, quiet=True)
        count = collected_count(done.out)
        if done.code != 0 or count is None:
            unread.append(name)
            ctx.say(f"{name}: collection exited {done.code}: " + (done.out.strip().splitlines() or ["no output"])[-1])
        else:
            counts.append((name, count))
    line = "tests collected: " + (", ".join(f"{name} {count}" for name, count in counts) or "none read")
    in_packages = [count for name, count in counts if name in PACKAGES]
    if len(in_packages) == len(PACKAGES):
        line += f"; {sum(in_packages)} in the four packages"
    ctx.note(line)
    return failed("no count read for " + ", ".join(unread)) if unread else passed()


def coverage_numbers(ctx: Context) -> Outcome:
    """N76: line coverage of each package by its own suite, printed, with no
    floor. A second run of the suites, so the measurement can never change
    what the blocking run above saw. coverage is Apache-2.0 (PyPI, verified
    2026-11-26); the version is `tools/ci/requirements.in`'s."""
    if not ctx.has_module("coverage"):
        return not_run("needs coverage in the interpreter that runs the suites")
    data, numbers, broke = ctx.scratch("coverage"), [], []
    for package in PACKAGES:
        file = data / f"{package}.coverage"
        ran = ctx.run([ctx.python, "-m", "coverage", "run", f"--data-file={file}",
                       f"--source={package.replace('-', '_')}", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                      cwd=ctx.root / package, quiet=True)
        ctx.say((ran.out.strip().splitlines() or ["no output"])[-1])
        if ran.code != 0:
            broke.append(package)
        total = ctx.run([ctx.python, "-m", "coverage", "report", f"--data-file={file}", "--format=total"],
                        cwd=ctx.root / package, quiet=True)
        figure = (total.out.strip().splitlines() or ["?"])[-1].strip()
        numbers.append(f"{package} {figure}%" if total.code == 0 and re.fullmatch(r"\d+(\.\d+)?", figure)
                       else f"{package} unread")
    ctx.note("coverage, lines, no floor (N76): " + ", ".join(numbers))
    if broke:
        return failed("the suite failed under coverage, so its number is not to be trusted: " + ", ".join(broke))
    return passed()


def numpy_pin(text: str) -> str | None:
    """The `numpy==X` line of the bank tools' list, or None when it pins none."""
    for line in text.splitlines():
        line = line.partition("#")[0].strip()
        if re.fullmatch(r"numpy==[0-9][0-9A-Za-z.]*", line):
            return line
    return None


def numpy_install(ctx: Context) -> Outcome:
    """numpy at the bank tools' pin, a wheel and no dependency (numpy has
    none), into a folder of its own under the temp folder. The job's
    interpreter never holds it, so the licence steps never read it: numpy's
    0BSD, Zlib and CC0-1.0 are outside the test jobs' allowed list (stream
    B6's F4, the maintainer's to answer). The pin is a version, not a hash:
    `tools/bank/requirements.txt` holds no hashes."""
    path = ctx.root / BANK_REQUIREMENTS
    pin = numpy_pin(path.read_text(encoding="utf-8")) if path.is_file() else None
    if pin is None:
        return not_run(f"{BANK_REQUIREMENTS} pins no numpy")
    folder = ctx.scratch("numpy")
    outcome = ctx.check([ctx.python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps",
                         "--only-binary", ":all:", "--target", folder, pin])
    if outcome.status == PASSED:
        ctx.state["numpy"] = (folder, pin)
    return outcome


def numpy_arithmetic(ctx: Context) -> Outcome:
    """RC124: the bank builder's numpy arithmetic gives the same whole
    numbers as its plain Python, which the tests build with. The test skips
    wherever numpy is absent, the suites above included, so here it runs
    with numpy's folder first on the path, and a skip is no pass."""
    if "numpy" not in ctx.state:
        return not_run("needs numpy, which this job installs into a folder of its own only under --install")
    folder, pin = ctx.state["numpy"]
    env = {"PYTHONPATH": os.pathsep.join(p for p in (str(folder), ctx.env.get("PYTHONPATH", "")) if p)}
    done = ctx.run([*pytest_line(ctx), "-rs", NUMPY_TEST], env=env)
    last = (done.out.strip().splitlines() or ["no output"])[-1].strip()
    if done.code != 0:
        return failed(f"exit {done.code}: {last}")
    if not re.fullmatch(r"1 passed in [0-9.]+s", last.strip("= ")):
        return failed(f"the test did not run against {pin}: {last}")
    return passed()


#: Ledger L20: the extras no test job installs, put in a venv of their own
#: and held to the licence gate. `piper` brings piper-tts, GPL-3.0-or-later
#: by its exception in `tools/licences.json`, and onnxruntime; `audio`
#: brings sounddevice and cffi. Local paths, so the SDK is this tree's and
#: never another project's of that name on the index.
EXTRAS_GATED = ("./emet-sdk", "./emet-hal[audio]", "./emet-providers[piper]")


def extras_install(ctx: Context) -> Outcome:
    """A venv under the temp folder holding the SDK, `emet-hal[audio]` and
    `emet-providers[piper]` and nothing else of this job's."""
    venv = ctx.scratch("extras-venv")
    made = ctx.check([ctx.python, "-m", "venv", venv])
    if made.status != PASSED:
        return made
    python = ctx.in_venv(venv, "python")
    outcome = ctx.check([python, "-m", "pip", "install", "--disable-pip-version-check", *EXTRAS_GATED])
    if outcome.status == PASSED:
        ctx.state["extras-venv"] = python
    return outcome


def extras_licences(ctx: Context) -> Outcome:
    """BVP1 over the extras venv: every distribution there, the piper-tts
    exception read for `emet-providers[piper]` alone. The script is standard
    library, so the venv's own interpreter runs it and reads what it holds.
    Blocking: it passed on the tree of 2026-11-27, on the laptop (16
    distributions, piper-tts by its exception)."""
    python = ctx.state.get("extras-venv")
    if python is None:
        return not_run("needs the extras venv, which this job makes only under --install")
    return ctx.check([python, ctx.root / "tools" / "check_licences.py"])


def test_steps(ctx: Context) -> list[Step]:
    """One Python; the workflow's matrix starts the job three times.

    The wake engine is an optional extra. pocketsphinx 5.1.0 and 5.1.1, the
    newest, publish cp311 and cp313 wheels and no cp312 one (PyPI, verified
    2026-11-26; 5.0.3 and 5.0.4 had cp312 wheels), so under emet-hal's
    `pocketsphinx>=5.0` pip on 3.12 takes 5.1.1's source and the job would
    compile a speech decoder. The wake tests
    `importorskip`, so 3.12 runs the rest and skips them. On 3.13 the suites
    run under Python's development mode (BVP7), where an unclosed file, a
    coroutine never awaited or a deprecated call is a warning, and each
    package's `filterwarnings = error` makes that warning a failure. After
    the blocking steps, 3.13 runs the bank builder's numpy test against
    numpy in a folder of its own (RC124), then the CI tools' licences and
    coverage. Before those, 3.13 puts the `piper` and `audio` extras in a
    venv of their own and runs the licence gate over it (ledger L20).
    """
    newest = ctx.version == (3, 13)
    steps = package_installs(TEST_INSTALL)
    if ctx.version != (3, 12):
        steps.append(pip_step("Install the wake engine where a wheel exists", "-e", "emet-hal[wake]"))
    steps.append(script_step("Every installed dependency's licence is on the allowed list", "licences",
                             "tools/check_licences.py"))
    if newest:
        steps.append(Step("Count the tests each suite collects", "counts", collected_counts, advisory=True))
    steps += suite_steps(dev=newest)
    if newest:
        steps.append(tools_tests(dev=True))
    steps += [
        Step("CLI accepts the valid examples", "valid-examples", valid_examples),
        Step("Fully-mocked manifest passes the boot-strictness check", "boot-strictness", boot_strictness),
        Step("Binding tables resolve", "explain", binding_tables),
        Step("CLI rejects the invalid examples", "invalid-examples", invalid_examples),
    ]
    if newest:
        steps.append(Step("Install the piper and audio extras into a venv of their own", "extras-install",
                          extras_install, install=True, gate=True))
        steps.append(Step("Every licence the piper and audio extras bring is on the allowed list", "extras-licences",
                          extras_licences))
        # After every blocking step, so the suites above ran in an
        # environment that held the four packages and nothing else. numpy
        # goes to a folder outside the interpreter, so it changes nothing
        # the steps after it read.
        steps.append(Step("Install numpy, the bank tools' pin, into a folder of its own", "install",
                          numpy_install, install=True, advisory=True))
        steps.append(Step("The bank builder's numpy arithmetic gives plain Python's whole numbers", "numpy",
                          numpy_arithmetic, advisory=True))
        steps.append(ci_tools_step("coverage, and the CI tools' licences", advisory=True))
        steps.append(Step("Every CI tool's licence is on the tools' allowed list", "tools-licences",
                          ci_tools_licences, advisory=True))
        steps.append(Step("Coverage of each package by its own suite, no floor", "coverage", coverage_numbers,
                          advisory=True))
    return steps


# --------------------------------------------------------------------- bare


def shadow_source(module: str) -> str:
    return f'raise ModuleNotFoundError("shadowed for the no-extras run: {module}", name="{module}")\n'


def extras_absent(ctx: Context) -> Outcome:
    """Every optional extra is really absent. GitHub's job installs none and
    checks that none leaked in. A machine that has them installed, the
    laptop, puts a folder of modules that raise ModuleNotFoundError first on
    the path, which is how the rule was kept by hand before it was a job."""
    present = [m for m in OPTIONAL_MODULES if ctx.has_module(m)]
    if not present:
        ctx.say("bare: no optional extras installed")
        return passed()
    if ctx.install:
        ctx.say("optional extras leaked into the bare job: " + ", ".join(present))
        return failed("optional extras are installed: " + ", ".join(present))
    shadow = ctx.scratch("shadow")
    for module in OPTIONAL_MODULES:
        (shadow / f"{module}.py").write_text(shadow_source(module), encoding="utf-8")
    env = {"PYTHONPATH": os.pathsep.join(p for p in (str(shadow), ctx.env.get("PYTHONPATH", "")) if p)}
    done = ctx.run([ctx.python, "tools/ci.py", "--probe", "shadowed"], env=env)
    if done.code != 0:
        return failed("a shadowed module still imports")
    ctx.state["shadow"] = env
    ctx.say(f"installed here: {', '.join(present)}. All five are shadowed for the suites below.")
    return passed()


def bare_steps(ctx: Context) -> list[Step]:
    """The four suites with every optional extra absent: no pocketsphinx, no
    sounddevice, no websockets, no httpx, no piper-tts. On 3.13, the Python
    the `test` job runs them on, the tools' tests follow with the same five
    absent, as `releases/v0.5.5/ci/local_ci_055.sh` ran them on the laptop
    ("no extras: test tools"), which GitHub's job never did."""
    steps = [
        *package_installs(("emet-sdk[dev]", "emet-hal[dev]", "emet-providers[dev]", "emet-engine[dev]")),
        Step("Every optional extra is really absent", "absent", extras_absent, gate=True),
        *suite_steps(shadowed=True),
    ]
    if ctx.version == (3, 13):
        steps.append(tools_tests(dev=False, shadowed=True))
    return steps


# -------------------------------------------------------------------- arm64


def arm64_cannot(ctx: Context) -> str:
    if ctx.os_name == "linux" and ctx.machine in ("aarch64", "arm64"):
        return ""
    return "needs Linux on arm64: GitHub's ubuntu-24.04-arm, or the Pi"


def arm64_steps(ctx: Context) -> list[Step]:
    """The four suites on the reference body's architecture, on GitHub's
    arm64 runner, free on public repositories (verified 2026-10-27, GitHub
    Docs). Running arm64 work on `ubuntu-24.04-arm` is microduck's CI (Pollen
    Robotics, Apache-2.0): the idea taken, no code. Advisory by the
    maintainer's decision of 2026-10-27, so every suite runs whatever the one
    before it did, and the warning names each that failed. The licence check
    runs here too, since an aarch64 wheel can carry other bundled libraries
    (BVP1)."""
    return [
        *package_installs(("emet-sdk[dev]", "emet-hal[dev,wake]",
                           "emet-providers[dev,deepgram,anthropic,openai]", "emet-engine[dev]")),
        script_step("Every installed dependency's licence is on the allowed list", "licences",
                    "tools/check_licences.py", always=True),
        *suite_steps(always=True),
    ]


# -------------------------------------------------------------------- sound


def sound_cannot(ctx: Context) -> str:
    if ctx.os_name == "linux" and ctx.env.get("EMET_VIRTUAL_AUDIO") == "1":
        return ""
    return ("needs the virtual sound card: Linux, a PulseAudio null sink named to ALSA, and "
            "EMET_VIRTUAL_AUDIO=1, which GitHub's job sets up")


#: The virtual card's two ends as the workflow's ~/.asoundrc names them to
#: ALSA, and the PulseAudio null sink behind both, as
#: `emet-hal/tests/test_audio_virtual.py` names them. The live run opens
#: nothing by any other name.
VIRTUAL_SINK = "emet_virtual_sink"
VIRTUAL_SOURCE = "emet_virtual_source"
NULL_SINK = "emet_null"

#: Stream C3's clip: LJ Speech's public-domain voice saying "Hey emet." once,
#: after 5.5 s of quiet pink noise, with the record of what made it beside it.
LIVE_CLIP = "emet-hal/tests/clips/hey-emet-ljspeech.wav"

#: The reference soul. The live run's body takes every model stage over with
#: the mock, so no key and no vendor is reached.
LIVE_SOUL = "emet-sdk/examples/emet-soul.yaml"

#: How long `emet-listen` listens (CI6's "about 30 s"), and how long after it
#: starts the clip begins: the name then comes 5 + 5.5 s in, after the
#: decoder has booted and heard the clip's own noise.
LIVE_SECONDS, LIVE_LEAD = 30, 5

#: What the live run adds to the sound job's install: the engine, the wake
#: engine and the providers, for `emet-listen` with the mock transcriber.
LIVE_INSTALL = ("emet-hal[wake]", "emet-providers", "emet-engine")

#: The live run as bash runs it. Every path and name arrives as an argument,
#: so nothing is pasted into the script's text. The player is PulseAudio's
#: `paplay`, into the null sink; the listener is the rest of the line, ended
#: as Ctrl-C ends it, by SIGINT from coreutils' `timeout`, which then exits
#: with the listener's own status (`--preserve-status`). Exit 3 is a player
#: that failed.
LIVE_SCRIPT = """\
set -u
seconds=$1 lead=$2 sink=$3 clip=$4
shift 4
( sleep "$lead" && paplay --device="$sink" "$clip" ) &
player=$!
timeout --preserve-status --kill-after=10 --signal=INT "$seconds" "$@"
listened=$?
wait "$player"
played=$?
echo "live run: the player exited $played, the listener $listened"
if [ "$played" -ne 0 ]; then
  exit 3
fi
exit "$listened"
"""

#: What `emet-listen` prints: each wake as it happens, the source in its
#: header, and the line a stopped run ends on (`emet_engine/cli.py`).
LIVE_HEARD = re.compile(r"^  heard '.*'  \(confidence [0-9.]+\)$", re.MULTILINE)
LIVE_SOURCE = re.compile(r"^  source   (\S+)", re.MULTILINE)
LIVE_STOPPED = re.compile(r"^stopped\. heard it (\d+) time\(s\)\.$", re.MULTILINE)


def live_manifest() -> str:
    """The body of the live run: the microphone is the virtual card's
    monitor source, the sink is null, the wake engine is the shipped
    pocketsphinx at its shipped threshold, and every model stage is the
    mock. JSON strings are YAML double-quoted strings."""
    q = json.dumps
    return "\n".join([
        "# Written by tools/ci.py for the sound job's live run. The microphone is the",
        "# virtual card's monitor source and the sink is null: no device of the",
        "# machine is named, and nothing plays through Emet.",
        'manifest_version: "0.1"',
        "body:",
        "  id: ci_virtual_card",
        '  name: "a CI runner with a virtual sound card"',
        "  scale: desk",
        "  power: plugged_in",
        '  description: "A hosted runner whose only sound card is a PulseAudio null sink and its monitor."',
        "audio:",
        f"  input: {{device: {q(VIRTUAL_SOURCE)}, sample_rate: 16000, channels: 1, aec: none, doa: false}}",
        f'  output: {{sink: "null", device: {q(VIRTUAL_SINK)}}}',
        "  wake:",
        "    engine: pocketsphinx",
        "models:",
        "  stt: {provider: mock}",
        "  chat: {provider: mock}",
        "  tts: {provider: mock}",
        "",
    ])


def live_command(listen: list[Any], clip: Path) -> list[str]:
    """`bash -c LIVE_SCRIPT` with the window, the lead, the null sink, the
    clip, then the listener's own command line."""
    return ["bash", "-c", LIVE_SCRIPT, "live-run", str(LIVE_SECONDS), str(LIVE_LEAD), NULL_SINK, str(clip),
            *(str(a) for a in listen)]


def live_problems(code: int, text: str) -> list[str]:
    """Why a live run did not show one wake on the live path: the clip says
    the name once, the header must name the microphone source (a replay
    reads `wav`), and the run must end on the stopped line, which says the
    listener heard the whole window and was stopped as Ctrl-C stops it."""
    problems = []
    if code != 0:
        problems.append(f"the run exited {code}")
    source = LIVE_SOURCE.search(text)
    if source is None or source.group(1) != "microphone":
        problems.append("emet-listen did not listen through the microphone source"
                        + (f" (its header says {source.group(1)})" if source else ""))
    heard = len(LIVE_HEARD.findall(text))
    if heard != 1:
        problems.append(f"it heard its name {heard} time(s); the clip says it once")
    if LIVE_STOPPED.findall(text) != [str(heard)]:
        problems.append("the run did not end on emet-listen's stopped line for that count")
    return problems


def live_run(ctx: Context) -> Outcome:
    """CI6: the live loop, off the body. While `emet-listen --transcribe
    --stats` listens on the virtual card's monitor source for 30 s, the
    clip plays into the card's null sink, and the run must hear its name
    once. It is the first run of emet-listen's loop on a live source off
    the body: the loop's clock and the wake path behind the PortAudio
    callback and queue that `test_audio_virtual.py` already opens. The
    card's own clock, the room and the Pi's temperature stay the body's,
    and a run here never counts as the body's ten minutes (RELEASING.md
    §6). Before anything plays, PulseAudio must
    list the null sink, as `test_audio_virtual.py` asks.

    Advisory until E12 (c)'s count graduates it: ten green runs, pull
    request runs and scheduled runs on master counted, at least two of them
    pull requests (RELEASE_PROCESS.md §11)."""
    clip = ctx.root / LIVE_CLIP
    if not clip.is_file():
        return not_run(f"{LIVE_CLIP} is missing")
    for tool in ("bash", "timeout", "pactl", "paplay"):
        if not ctx.which(tool, path=ctx.env.get("PATH")):
            return not_run(f"needs {tool}")
    sinks = ctx.run(["pactl", "list", "short", "sinks"], quiet=True)
    if sinks.code != 0 or not any(line.split("\t")[1:2] == [NULL_SINK] for line in sinks.out.splitlines()):
        return failed(f"PulseAudio lists no {NULL_SINK} sink, so nothing was played or opened")
    work = ctx.scratch("live")
    (work / "body.yaml").write_text(live_manifest(), encoding="utf-8")
    (work / "keys.env").write_text("", encoding="utf-8")
    listen = [ctx.console("emet-listen"), work / "body.yaml", ctx.root / LIVE_SOUL, "--transcribe", "--stats",
              "--keys", work / "keys.env", "--state", work]
    done = ctx.run(live_command(listen, clip))
    problems = live_problems(done.code, done.out)
    if problems:
        return failed("; ".join(problems))
    ctx.note(f"live run on the virtual card: one wake in {LIVE_SECONDS} s, the clip {Path(LIVE_CLIP).name} "
             "played into the null sink")
    return passed()


def sound_steps(ctx: Context) -> list[Step]:
    """The real PortAudio path, which every other job leaves out. A
    PulseAudio null sink is the speaker and its monitor the microphone, set
    up by the workflow, and `test_audio_virtual.py` plays a tone through
    `Speaker` and hears it through `MicrophoneSource`. A hosted runner has no
    sound card to reach. EMET_VIRTUAL_AUDIO is set in this job alone, and
    that file skips without it (the maintainer's decision of 2026-10-27).
    `test_audio.py` runs here too, so its tests that need sounddevice run
    against a real PortAudio once. After the tests, the live run (CI6), an
    advisory step with its own install lines, so the tests above ran with
    the audio extra alone."""
    return [
        *package_installs(("emet-sdk[dev]", "emet-hal[dev,audio]")),
        Step("List what PortAudio sees", "devices", lambda ctx: ctx.check([ctx.python, "-m", "sounddevice"])),
        Step("Test the PortAudio path through the virtual card", "virtual-card",
             lambda ctx: ctx.check([*pytest_line(ctx), "tests/test_audio_virtual.py", "tests/test_audio.py"],
                                   cwd=ctx.root / "emet-hal")),
        *(pip_step(f"Install -e {spec} for the live run", "-e", spec, advisory=True) for spec in LIVE_INSTALL),
        Step("Listen live on the virtual card while the clip plays: one wake", "live", live_run, advisory=True),
    ]


# -------------------------------------------------------------------- audit


def _with_gh(ctx: Context) -> dict[str, str] | None:
    """The environment `release_audit.py` needs to find `gh`, or None. Git
    Bash on the laptop leaves the GitHub CLI's folder off PATH."""
    if ctx.which("gh", path=ctx.env.get("PATH")):
        return {}
    folder = Path(ctx.env.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI"
    if ctx.windows and (folder / "gh.exe").is_file():
        return {"PATH": ctx.env.get("PATH", "") + os.pathsep + str(folder)}
    return None


def audit(*args: str) -> Callable[[Context], Outcome]:
    def run(ctx: Context) -> Outcome:
        env = _with_gh(ctx)
        if env is None:
            return not_run("needs gh, the GitHub CLI")
        return script(ctx, "tools/release_audit.py", *args, env=env)

    return run


def audit_steps(ctx: Context) -> list[Step]:
    """The release audit's two steps, read-only. The second runs even when
    the history failed, so a pull request learns about its own title in the
    same run. It runs where GitHub gives PR, the pull request's number."""
    steps = [Step("Every version since 0.4.1", "history", audit())]
    number = ctx.env.get("PR", "")
    if number:
        steps.append(Step("This pull request, as far as CI can tell", "pull-request",
                          audit("--pr", number, "--ci"), always=True))
    return steps


# ------------------------------------------------------- docs and api-diff


def docs(ctx: Context) -> Outcome:
    """RP9: the documents' commands run and their links resolve. The
    interpreter's own folder goes first on PATH, as an activated venv has
    it, since the documents name `emet` and `python` bare."""
    here = str(Path(ctx.python).parent)
    return script(ctx, "tools/check_docs.py", ".", env={"PATH": here + os.pathsep + ctx.env.get("PATH", "")})


def docs_steps(ctx: Context) -> list[Step]:
    return [*package_installs(TEST_INSTALL), Step("Run the documents' commands and check their links", "docs", docs)]


def api_diff(ctx: Context) -> Outcome:
    """RP10: what a builder or a plugin author would see change against
    master, printed, and copied into the job's summary."""
    if not (ctx.root / "tools" / "api_diff.py").is_file():
        return not_run("tools/api_diff.py is missing")
    done = ctx.run([ctx.python, "tools/api_diff.py", "--base", "origin/master"])
    if done.code != 0:
        return failed(f"exit {done.code}")
    ctx.notes.append("```\n" + done.out.strip()[-6000:] + "\n```")
    return passed()


def api_diff_steps(ctx: Context) -> list[Step]:
    return [Step("Print the public API and schema diff against master", "api-diff", api_diff)]


# ---------------------------------------------------------------- pip-audit


def pip_audit_requirements(pyproject: str) -> list[str]:
    """What a package asks of the index: its dependencies and every extra's.
    Emet's own names are left out: no index holds them at the release's
    floor, and PyPI's `emet-sdk` is another party's project (see
    `index_verdict`)."""
    project = tomllib.loads(pyproject).get("project", {})
    wanted = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        wanted += extra
    kept: list[str] = []
    for requirement in wanted:
        if not first_party(requirement_name(requirement)) and requirement not in kept:
            kept.append(requirement)
    return kept


def pip_audit_findings(report: str) -> list[dict[str, str]]:
    """pip-audit's JSON as rows of name, version, advisory id and fix versions."""
    rows = []
    for dependency in json.loads(report).get("dependencies", []):
        for vuln in dependency.get("vulns", []):
            rows.append({"name": dependency.get("name", "?"), "version": dependency.get("version", "?"),
                         "id": vuln.get("id", "?"),
                         "fix": ", ".join(vuln.get("fix_versions", [])) or "none published"})
    return rows


def pip_audit(ctx: Context) -> Outcome:
    """BVP2 stage 1: each package's resolved set, every extra included,
    against the PyPA's advisory database. A finding prints with its fix and
    becomes a ledger row; it never fails a pull request, since a third
    party's database should not block unrelated work. pip-audit is the
    PyPA's, 2.10.1, Apache-2.0 (PyPI, verified 2026-11-26)."""
    tool = ctx.tool("pip-audit", module="pip_audit")
    if tool is None:
        return not_run("needs pip-audit")
    work, rows, unread = ctx.scratch("pip-audit"), [], []
    for package in PACKAGES:
        wanted = pip_audit_requirements((ctx.root / package / "pyproject.toml").read_text(encoding="utf-8"))
        listed, report = work / f"{package}.txt", work / f"{package}.json"
        listed.write_text("\n".join(wanted) + "\n", encoding="utf-8")
        done = ctx.run([*tool, "-r", listed, "-f", "json", "-o", report, "--progress-spinner", "off",
                        "--desc", "off"], quiet=True)
        try:
            found = pip_audit_findings(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unread.append(package)
            ctx.say(f"{package}: pip-audit exited {done.code} and wrote no report")
            ctx.say(done.out.rstrip()[-1500:])
            continue
        ctx.say(f"{package}: {len(wanted)} requirements, {len(found)} finding(s)")
        rows += [{**row, "package": package} for row in found]
    for row in rows:
        ctx.note(f"pip-audit: {row['package']}: {row['name']} {row['version']} {row['id']}, fix: {row['fix']}")
    if unread:
        return failed("pip-audit gave no report for " + ", ".join(unread))
    if rows:
        return failed(f"{len(rows)} known vulnerability finding(s), each a ledger row")
    ctx.say("pip-audit: no known vulnerability in any package's resolved set")
    return passed()


def pip_audit_steps(ctx: Context) -> list[Step]:
    return [ci_tools_step("pip-audit"),
            Step("Audit each package's resolved set, every extra included", "pip-audit", pip_audit)]


# ------------------------------------------------------------------- mutmut


def read_targets(text: str) -> list[tuple[str, str]]:
    """`tools/ci/mutmut-targets.txt`: one `path::function` a line, `#` for a
    comment. A method is `path::Class.method`."""
    targets = []
    for line in text.splitlines():
        line = line.partition("#")[0].strip()
        if "::" in line:
            path, _, name = line.partition("::")
            targets.append((path.strip().replace("\\", "/"), name.strip()))
    return targets


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """The line ranges a `git diff -U0` touches, by path, on the new side."""
    changed: dict[str, list[tuple[int, int]]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = path[2:] if path.startswith("b/") else path
        elif line.startswith("@@") and path and path != "/dev/null":
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or "1")
                changed.setdefault(path, []).append((start, start + max(count, 1) - 1))
    return changed


def function_span(source: str, name: str) -> tuple[int, int] | None:
    """The first and last line of a function, or of `Class.method`."""
    body: list[ast.stmt] = ast.parse(source).body
    node: ast.AST | None = None
    for part in name.split("."):
        node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            return None
        body = node.body                                           # type: ignore[attr-defined]
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])      # type: ignore[union-attr]
    return first, node.end_lineno                                            # type: ignore[union-attr]


def changed_targets(targets: list[tuple[str, str]], changed: dict[str, list[tuple[int, int]]],
                    read: Callable[[str], str]) -> list[tuple[str, str]]:
    """The targets whose function a changed line falls inside."""
    hit = []
    for path, name in targets:
        if path not in changed:
            continue
        try:
            span = function_span(read(path), name)
        except (OSError, SyntaxError):
            span = None
        if span and any(start <= span[1] and end >= span[0] for start, end in changed[path]):
            hit.append((path, name))
    return hit


def mutant_pattern(path: str, name: str) -> str:
    """The pattern `mutmut run` takes for one function: its module, dotted
    from the project's folder, then mutmut's own mangling of the name."""
    module = path[:-3].replace("/", ".")
    module = module[4:] if module.startswith("src.") else module
    if "." in name:
        owner, _, method = name.rpartition(".")
        mangled = f"x{MUTMUT_CLASS_SEPARATOR}{owner}{MUTMUT_CLASS_SEPARATOR}{method}"
    else:
        mangled = f"x_{name}"
    return f"{module}.{mangled}__mutmut_*"


#: Statuses of `mutmut results` that are no survivor: a test failed, the
#: type checker refused the mutant, or a pragma kept the line unmutated.
NOT_SURVIVORS = ("killed", "caught by type check", "skipped")


def survivors(results: str, patterns: list[str]) -> list[str]:
    """The chosen mutants no test killed. `mutmut results` prints one
    `name: status` line for every mutant of every mutated file that was not
    killed, and a mutant `mutmut run` was not asked for is `not checked`, so
    only names that match a pattern given to the run are read. A chosen
    mutant still `not checked` did not run, and is named (mutmut 3.8.1's
    `__main__.py` and `stats.py`, read 2026-11-26)."""
    alive = []
    for line in results.splitlines():
        name, _, status = line.strip().rpartition(": ")
        if ("__mutmut_" in name and status not in ("", *NOT_SURVIVORS)
                and any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)):
            alive.append(f"{name}: {status}")
    return alive


def mutmut_cannot(ctx: Context) -> str:
    return "" if ctx.has_fork else "mutmut needs os.fork: run `python tools/ci.py mutmut` on Linux, in WSL or on the Pi"


#: The projects whose tests take the repository to be the folder above the
#: one they sit in, and import what they test by its bare name.
LIFTED = ("tools",)

#: The hardware guard every suite's conftest walks up to (0.5.7-D10 (a)).
GUARD = "tools/hardware_guard.py"


def mutmut_folder(tree: Path, project: str, guard: Path | None = None) -> tuple[Path, list[str]]:
    """Where `mutmut run` starts for one project in a copy of the tree, and
    the entries it copies into `mutants/` beside the mutated source.

    mutmut copies the source and the `also_copy` entries into `mutants/`
    inside the folder it starts in, puts `mutants/` first on the path, runs
    the tests from `mutants/tests/`, and names a module by its path under
    that folder (mutmut 3.8.1's `utils/file_utils.py` and
    `utils/format_utils.py`, read 2026-11-26). A package's tests need their
    package beside them, so mutmut starts in the package's folder.

    The tools' tests import a tool by its bare name (`import plan_file`)
    and take the repository to be the folder above the one they sit in
    (`REPO = TOOLS.parent`, or `parents[2]`). Started in `tools/`, that
    folder is `tools/` itself, so a test looked for `tools/tools/bank/` and
    collection failed (stream B5's F4). So a tools target starts at the
    copy's root, with each entry of `tools/` copied up beside `emet-sdk/`
    and the rest: the module is still `plan_file`, and the folder above
    `mutants/tests/` is a whole repository.

    Every suite's `conftest.py` walks up from `mutants/tests/` to the first
    `tools/hardware_guard.py` and fails the collection without one (C13). So
    the copy keeps the guard at `tools/hardware_guard.py`: `guard`, the
    tree's own, is copied in when the copy lacks it, and a copy that has
    none is refused here, naming the file.
    """
    folder = tree / project
    names = sorted(p.name for p in folder.iterdir() if p.name not in ("mutants", "setup.cfg"))
    start = folder
    if project in LIFTED:
        clash = [name for name in names if (tree / name).exists()]
        if clash:
            raise ValueError(f"{project}/ cannot be copied up to the root of the copy, which already holds "
                             + ", ".join(clash))
        for name in names:
            if (folder / name).is_dir():
                shutil.copytree(folder / name, tree / name)
            else:
                shutil.copy2(folder / name, tree / name)
        start = tree
    held = tree / GUARD
    if not held.is_file() and guard is not None and guard.is_file():
        held.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(guard, held)
    if not held.is_file():
        raise ValueError(f"the mutmut copy of {project} has no {GUARD}, which every suite's conftest.py loads, "
                         f"so its tests would not be collected")
    return start, names


def mutmut(ctx: Context) -> Outcome:
    """RP7 stage 1: mutate the gate functions this branch changes, and name
    every mutant no test killed. The targets are the functions a merge gate
    or a floor rests on (`tools/ci/mutmut-targets.txt`). It works on a copy
    of the tree under the temp folder, since mutmut writes a `mutants/`
    folder where it runs. mutmut 3.8.1, BSD-3-Clause (PyPI, verified
    2026-11-26); its command line and names were read from its README and
    source that day. This step ran once that day, in WSL on Python 3.12
    against a small git repository: the mutants of the two changed target
    functions ran, the unchanged target's were left alone, the survivors
    were named, and the repository was left as it was
    (`releases/v0.5.6/reports/B1-logs/mutmut-wsl.out`). Later that day it
    ran in WSL against a git copy of this repository with one line changed
    inside `tools/plan_file.py`'s `read_plan`, laid out by `mutmut_folder`:
    74 mutants, 73 killed, 1 named as surviving, in 362 s (WSL's
    `~/i1-mutmut/after2.out`).

    On GitHub the job's time limit ends a run that would outlast the
    workflow's, and the step then names every target it did not mutate."""
    listed = ctx.root / "tools" / "ci" / "mutmut-targets.txt"
    if not listed.is_file():
        return not_run("tools/ci/mutmut-targets.txt is missing")
    if not ctx.has_module("mutmut"):
        return not_run("needs mutmut in the interpreter that runs the suites")
    targets = read_targets(listed.read_text(encoding="utf-8"))
    base = ctx.env.get("BASE_SHA") or "origin/master"
    fork_point = ctx.run(["git", "merge-base", base, "HEAD"], quiet=True)
    if fork_point.code != 0:
        return not_run(f"needs {base} in the checkout, to see what the branch changed")
    diff = ctx.run(["git", "diff", "-U0", fork_point.out.strip(), "--", *sorted({p for p, _ in targets})], quiet=True)
    chosen = changed_targets(targets, changed_lines(diff.out),
                             lambda path: (ctx.root / path).read_text(encoding="utf-8"))
    if not chosen:
        ctx.say(f"none of the {len(targets)} target functions changed against {base}: nothing to mutate")
        return passed()

    files = ctx.run(["git", "ls-files", "-co", "--exclude-standard"], quiet=True).out.splitlines()
    projects: dict[str, list[tuple[str, str]]] = {}
    for path, name in chosen:
        project, _, inside = path.partition("/")
        projects.setdefault(project, []).append((inside, name))
    alive: list[str] = []
    order = list(projects.items())
    for at, (project, functions) in enumerate(order):
        # Each project mutates in its own copy, so one project's `mutants/`
        # folder never sits in the tree another project's tests read.
        tree = ctx.scratch(f"mutmut-{project}")
        for name in files:
            if (ctx.root / name).is_file():
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ctx.root / name, tree / name)
        try:
            folder, others = mutmut_folder(tree, project, ctx.root / GUARD)
        except ValueError as error:
            return failed(str(error))
        sources = sorted({inside.split("/")[0] + ("/" if "/" in inside else "") for inside, _ in functions})
        (folder / "setup.cfg").write_text(
            "[mutmut]\nsource_paths=\n" + "".join(f"    {s}\n" for s in sources)
            + "pytest_add_cli_args_test_selection=\n    tests/\nalso_copy=\n"
            + "".join(f"    {o}\n" for o in others), encoding="utf-8")
        patterns = [mutant_pattern(inside, name) for inside, name in functions]
        ran = ctx.run([ctx.python, "-m", "mutmut", "run", *patterns], cwd=folder)
        if ran.timed_out:
            left = [f"{p}/{inside}::{name}" for p, fs in order[at:] for inside, name in fs]
            for line in alive:
                ctx.note(f"mutmut: survived: {line}")
            return failed(f"mutmut run was ended in {project}, so {len(left)} target function(s) were not "
                          "mutated: " + ", ".join(left))
        if ran.code != 0:
            return failed(f"mutmut run exited {ran.code} in {project}")
        results = ctx.run([ctx.python, "-m", "mutmut", "results"], cwd=folder, quiet=True)
        if results.code != 0:
            last = (results.out.strip().splitlines() or ["no output"])[-1]
            return failed(f"mutmut results exited {results.code} in {project}, so no survivor was read: {last}")
        alive += survivors(results.out, patterns)
    for line in alive:
        ctx.note(f"mutmut: survived: {line}")
    ctx.say(f"mutmut: {len(chosen)} changed target function(s), {len(alive)} surviving mutant(s)")
    return failed(f"{len(alive)} mutant(s) survived the tests") if alive else passed()


def mutmut_steps(ctx: Context) -> list[Step]:
    return [*package_installs(TEST_INSTALL), ci_tools_step("mutmut"),
            Step("Mutate the gate functions this branch changes", "mutmut", mutmut)]


# ------------------------------------------------------- the generated bank

#: Where CI takes the generated bank from: each set's release asset and the
#: sha256 of that archive. A data file, so the pull request that publishes a
#: bank version changes two fields of it and no code, and both jobs read one
#: place, on GitHub and on the laptop. Its `note` says the rest.
BANK_STORE = "tools/bank/generated/store.json"
BANK_STORE_FORMAT = "emet-genbank-store/1"

#: The generated bank's goldens, a folder a platform (stream C3's).
BANK_GOLDEN = "tools/bank/golden/generated"

#: Why the bank jobs are not run while the store's fields are empty. The
#: maintainer named the store on 2026-11-26 (0.5.6-D4); the agent creates
#: it at the close, and a later pull request fills in the fields.
NO_STORE = "the generated bank's public store, emet-testaudio (0.5.6-D4), has published no set yet"

#: The bank jobs' install: the engine with the wake engine, and the
#: providers for the mock transcriber. No audio extra: `replay.py` makes
#: sounddevice unimportable in every replay all the same.
BANK_INSTALL = ("emet-sdk", "emet-hal[wake]", "emet-providers", "emet-engine")

#: Set by the workflow: where the goldens this platform lacks are written,
#: as `replay.py --update` lays them out, for the next step to upload.
GOLDEN_OUT = "EMET_BANK_GOLDEN_OUT"

_HEX64 = re.compile(r"[0-9a-f]{64}")


def store_entry(doc: Any, set_name: str) -> tuple[str, str]:
    """A set's (url, sha256) from the store file: ("", "") while the store
    has published no set, and ValueError for anything else that is not a
    whole entry.
    The archive's name must be the set's own, as `generate.py --pack` names
    it, so the full set's URL cannot stand in the small set's place."""
    if not isinstance(doc, dict) or doc.get("format") != BANK_STORE_FORMAT:
        raise ValueError(f"{BANK_STORE} is not an {BANK_STORE_FORMAT} file")
    entry = (doc.get("sets") or {}).get(set_name) if isinstance(doc.get("sets"), dict) else None
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("url", "sha256")):
        raise ValueError(f"{BANK_STORE} has no url and sha256 for the {set_name} set")
    url, digest = entry["url"].strip(), entry["sha256"].strip()
    if not url and not digest:
        return "", ""
    if not (url and digest):
        raise ValueError(f"{BANK_STORE}: the {set_name} set has a {'url' if url else 'sha256'} and no "
                         f"{'sha256' if url else 'url'}; fill both or neither")
    if not url.startswith(("https://", "file:")):
        raise ValueError(f"{BANK_STORE}: the {set_name} set's url is neither https:// nor a file URL: {url}")
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(rf"genbank-{re.escape(set_name)}-\d{{4}}-\d{{2}}-\d{{2}}\.tar\.gz", name):
        raise ValueError(f"{BANK_STORE}: the {set_name} set's url names {name}, where "
                         f"genbank-{set_name}-<version>.tar.gz was expected")
    if not _HEX64.fullmatch(digest):
        raise ValueError(f"{BANK_STORE}: the {set_name} set's sha256 is not 64 lower-case hex digits")
    return url, digest


def bank_store(ctx: Context, set_name: str) -> Outcome:
    """The store's entry for the set, kept for the fetch. Empty fields are
    not run, never passed: the bank was not replayed."""
    path = ctx.root / BANK_STORE
    if not path.is_file():
        return not_run(f"{BANK_STORE} is missing")
    try:
        url, digest = store_entry(json.loads(path.read_text(encoding="utf-8")), set_name)
    except (OSError, ValueError) as error:
        return failed(str(error))
    if not url:
        return not_run(NO_STORE)
    ctx.state["store"] = (url, digest)
    ctx.say(f"the {set_name} set: {url}, sha256 {digest}")
    return passed()


def fetch_bank(ctx: Context, set_name: str) -> Outcome:
    """`generate.py --fetch`: the archive is kept only when its sha256 is
    the store's, unpacked only when every member lies inside it, and the
    bank only when it passes `check.py` (stream C3's). It goes under the
    temp folder, never the tree."""
    if "store" not in ctx.state:
        return failed(f"the store's step gave no entry for the {set_name} set, so nothing was fetched")
    if not (ctx.root / "tools" / "bank" / "generate.py").is_file():
        return not_run("tools/bank/generate.py is missing")
    url, digest = ctx.state["store"]
    to = ctx.scratch(f"bank-{set_name}")
    done = ctx.run([ctx.python, "tools/bank/generate.py", "--fetch", url, "--sha256", digest, "--to", to])
    if done.code != 0:
        return failed(f"generate.py --fetch exited {done.code}, so no bank was kept")
    bank = to / url.rstrip("/").rsplit("/", 1)[-1][: -len(".tar.gz")]
    if not (bank / "bank.json").is_file():
        return failed(f"the fetch kept no bank at {bank.name}")
    ctx.state["bank"] = bank
    return passed()


def bank_platform(ctx: Context) -> str:
    """The golden's folder for this machine, as `replay.py` names it."""
    return f"{ctx.os_name}-{ctx.machine}"


def bank_ids(bank: Path) -> list[str]:
    """Every recording the bank lists, by id, in its order."""
    try:
        index = json.loads((bank / "bank.json").read_text(encoding="utf-8"))
        ids = [json.loads((bank / rel).read_text(encoding="utf-8"))["id"] for rel in index["files"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{bank.name}: its index or a manifest cannot be read ({error})") from None
    if not ids:
        raise ValueError(f"{bank.name} lists no recording")
    return ids


def replay_bank(ctx: Context) -> Outcome:
    """`replay.py` at the shipped threshold with the mock transcriber, each
    recording held to its golden for this platform. With the store's
    cassettes, in the bank's `cassettes/` folder, the transcriber answers
    from them and the audio figures are still held (CI5); without, it is
    the mock alone.

    A recording with no golden here fails the step, as `replay.py`'s NO
    GOLDEN does. Its golden is then written into `EMET_BANK_GOLDEN_OUT`
    when the workflow sets it, for the upload step: the first run on Linux
    x86-64 makes that platform's goldens, and they come back in a pull
    request. A step that held nothing to a golden is never green."""
    bank, plat = ctx.state["bank"], bank_platform(ctx)
    if not (ctx.root / "tools" / "bank" / "replay.py").is_file():
        return not_run("tools/bank/replay.py is missing")
    try:
        ids = bank_ids(bank)
    except ValueError as error:
        return failed(str(error))
    have = {path.stem for path in (ctx.root / BANK_GOLDEN / plat).glob("*.json")}
    held, lacking = [i for i in ids if i in have], [i for i in ids if i not in have]
    replay = [ctx.python, "tools/bank/replay.py", bank, "--platform", plat]
    problems = []
    if held:
        compare = [*replay, "--golden", BANK_GOLDEN]
        cassettes = bank / "cassettes"
        if cassettes.is_dir() and any(cassettes.glob("*.json")):
            compare += ["--cassette", cassettes]
            ctx.say(f"the store's cassettes: {cassettes.name}/ in {bank.name}")
        else:
            ctx.say("no cassettes in the store yet, so the transcriber is the mock alone")
        if lacking:
            compare += [word for rid in held for word in ("--only", rid)]
        done = ctx.run(compare)
        if done.code != 0:
            problems.append(f"replay.py exited {done.code} against the goldens for {plat}")
    else:
        ctx.say(f"none of the {len(ids)} recording(s) of {bank.name} has a golden for {plat}")
    if lacking:
        problems.append(f"{len(lacking)} of {len(ids)} recording(s) have no golden for {plat}")
        out = ctx.env.get(GOLDEN_OUT, "")
        if out:
            done = ctx.run([*replay, "--golden", out, "--update", *(w for rid in lacking for w in ("--only", rid))])
            if done.code == 0:
                ctx.note(f"bank: {len(lacking)} golden(s) for {plat} written to {out} for review; the run uploads "
                         f"them, and they come back in a pull request into {BANK_GOLDEN}/{plat}/")
            else:
                problems.append(f"replay.py --update exited {done.code}, so they were not written")
        else:
            # The bank fetched here is gone once the job ends, and an
            # --update without --only rewrites the goldens just compared.
            url, digest = ctx.state["store"]
            only = " ".join(f"--only {rid}" for rid in lacking)
            ctx.say(f"to write only these on a {plat} machine, fetch the set again and replay those: "
                    f"python tools/bank/generate.py --fetch {url} --sha256 {digest} --to DIR, then "
                    f"python tools/bank/replay.py DIR/{bank.name} --golden {BANK_GOLDEN} --update "
                    f"--platform {plat} {only}")
    if problems:
        return failed("; ".join(problems))
    return passed(f"{len(held)} recording(s) held to a golden")


def bank_steps(set_name: str) -> Callable[[Context], list[Step]]:
    """P10's job for one set. The store's step comes first and is a gate,
    so an empty store installs nothing and is not run, and a refused one
    installs nothing and is the job's failure, in the store's own words."""

    def steps(ctx: Context) -> list[Step]:
        return [
            Step("The generated bank's store names this set", "store", lambda ctx: bank_store(ctx, set_name),
                 advisory=True, gate=True),
            *package_installs(BANK_INSTALL),
            Step(f"Fetch the {set_name} set, held to its sha256", "fetch", lambda ctx: fetch_bank(ctx, set_name),
                 gate=True),
            Step("Replay it and hold it to this platform's goldens", "replay", replay_bank),
        ]

    return steps


# ------------------------------------------------------------------ the jobs

#: Every job, in the order `--all` runs them. A new job is one line here and
#: one function that returns its steps. `bank` is advisory until E12 (c)'s
#: count graduates it: ten green runs, pull-request runs and scheduled runs
#: on master counted, at least two of them pull requests (RELEASE_PROCESS.md
#: §11); a run whose bank step says not run counts as no green run.
#: `bank-full` never runs on a pull request, so it graduates on 0.5.7-D9 (a)'s
#: count instead: ten green scheduled runs.
#:
#: `minutes` is the `timeout-minutes` of the workflow job that runs the key,
#: for every job with an advisory part (`test_ci.py` holds the two equal).
JOBS: tuple[Job, ...] = (
    Job("layering", ("layering",), True, True, "anywhere", layering_steps),
    Job("dco", ("dco sign-off",), True, True, "a checkout with the branch's history", dco_steps),
    Job("wheel", ("wheel install",), True, True, "anywhere the package index is reached", wheel_steps),
    Job("test", ("test (py3.11)", "test (py3.12)", "test (py3.13)"), True, True,
        "anywhere, on the Python that runs this script", test_steps, minutes=20),
    Job("bare", ("test (no extras)",), True, True, "anywhere; installed extras are shadowed", bare_steps),
    Job("audit", ("release audit",), True, True, "a checkout with the whole history, and gh", audit_steps),
    Job("arm64", ("test (arm64, advisory)",), False, False,
        "Linux on arm64: GitHub's ubuntu-24.04-arm, or the Pi", arm64_steps, arm64_cannot, minutes=20),
    Job("sound", ("virtual sound card",), True, False,
        "Linux with the virtual sound card and EMET_VIRTUAL_AUDIO=1: GitHub", sound_steps, sound_cannot,
        minutes=15),
    Job("docs", ("docs (advisory)",), False, False, "anywhere", docs_steps, minutes=15),
    Job("api-diff", ("api-diff (advisory)",), False, False, "a checkout that has origin/master", api_diff_steps,
        minutes=10),
    Job("mutmut", ("mutmut (advisory)",), False, False, "where os.fork exists: Linux, WSL, the Pi",
        mutmut_steps, mutmut_cannot, minutes=30),
    Job("pip-audit", ("pip-audit (advisory)",), False, False, "anywhere the package index is reached",
        pip_audit_steps, minutes=15),
    Job("bank", ("bank (advisory)",), False, False,
        "anywhere the bank's store is reached: GitHub on every pull request; the small set", bank_steps("small"),
        minutes=15),
    Job("bank-full", ("bank-full (advisory)",), False, False,
        "anywhere the bank's store is reached: GitHub weekly, never on a pull request; the full set",
        bank_steps("full"), minutes=60),
)

BY_KEY = {job.key: job for job in JOBS}


def list_jobs() -> str:
    rows = [("job", "check on GitHub", "kind", "ruleset", "runs")]
    for job in JOBS:
        for check in job.checks:
            rows.append((job.key, check, job.kind, "required" if job.ruleset else "-", job.where))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = ["  ".join([*(cell.ljust(widths[i]) for i, cell in enumerate(row[:4])), row[4]]) for row in rows]
    required = sum(len(job.checks) for job in JOBS if job.ruleset)
    lines.append("")
    lines.append(f"{len(JOBS)} jobs, {sum(len(j.checks) for j in JOBS)} checks; {required} are required by the "
                 "master ruleset. A blocking job turns its check red; an advisory job warns and stays green.")
    return "\n".join(lines)


# ------------------------------------------------------------------ running


def annotation(title: str, message: str) -> str:
    """A GitHub warning annotation, escaped as workflow commands need."""

    def data(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    return f"::warning title={data(title).replace(':', '%3A').replace(',', '%2C')}::{data(message)}"


def header(ctx: Context, job: Job) -> str:
    """The job's first line: the check this run reports as (the test job's
    own Python), and its kind unless the check's name already says it."""
    own = f"test (py{ctx.version[0]}.{ctx.version[1]})"
    checks = [check for check in job.checks if check == own] or list(job.checks)
    named = ", ".join(checks)
    return f"\n=== {job.key}: {named}" + ("" if all("advisory" in c for c in checks) else f" ({job.kind})")


def run_job(ctx: Context, job: Job) -> JobResult:
    ctx.state, ctx.notes = {}, []
    started = ctx.clock()
    ctx.say(header(ctx, job))
    why = job.cannot(ctx)
    if why:
        ctx.say(f"... not run here: {why}")
        return JobResult(job, NOT_RUN, why)
    # On GitHub the advisory part of a job ends inside its workflow's limit.
    ends = started + job.limit if ctx.github and job.limit else None
    results: list[StepResult] = []
    stopped = ""
    gate: StepResult | None = None      # an advisory gate that failed and stopped the steps after it
    held: list[StepResult] = []         # such a gate, once a blocking step went unreached for it
    warnings: list[tuple[StepResult, str]] = []
    for step in job.steps(ctx):
        if step.install and not ctx.install:
            continue
        if stopped and not step.always:
            ctx.say(f"\n--- {job.key}: {step.name}\n... not reached: {stopped}")
            if gate is not None and not step.advisory and gate not in held:
                held.append(gate)
            continue
        ctx.say(f"\n--- {job.key}: {step.name}")
        began = ctx.clock()
        ctx.deadline = ends if ends is not None and (step.advisory or not job.blocking) else None
        ctx.ran_out = False
        try:
            outcome = step.run(ctx)
        except Exception as error:      # a fault in a step is a failure of it, never a pass
            ctx.say(traceback.format_exc().rstrip())
            outcome = failed(f"{type(error).__name__}: {error}")
        finally:
            ctx.deadline = None
        if ctx.ran_out:
            outcome = failed(f"out of time: on GitHub this part of the job stops {WORKFLOW_MARGIN} s before its "
                             f"workflow's {job.minutes} minutes: {outcome.reason or outcome.status}")
        if outcome.status == NOT_RUN and ctx.github and not step.advisory:
            outcome = failed(f"not run here: {outcome.reason}. On GitHub nothing may be missing")
        seconds = ctx.clock() - began
        result = StepResult(step, outcome, seconds)
        results.append(result)
        if outcome.status == PASSED:
            ctx.say(f"ok: {step.name} ({seconds:.1f} s)")
        elif outcome.status == NOT_RUN:
            ctx.say(f"... not run here: {outcome.reason}")
            warnings.append((result, f"not run here: {outcome.reason}"))
            if step.gate:
                stopped = f"{step.key} was not run"
        elif step.advisory:
            ctx.say(f"!!! failed, advisory: {step.name}: {outcome.reason}")
            warnings.append((result, outcome.reason))
            if step.gate and not stopped:
                stopped, gate = f"{step.key} failed", result
        else:
            ctx.say(f"!!! FAILED: {step.name}: {outcome.reason}")
            stopped = stopped or f"{step.key} failed"

    broken = [r for r in results if r.outcome.status == FAILED and not r.step.advisory] + held
    if broken:
        status, reason = FAILED, "; ".join(f"{r.step.key}: {r.outcome.reason}" for r in broken)
    elif any(r.outcome.status == PASSED for r in results):
        status, reason = PASSED, ""
    else:
        status = NOT_RUN
        reason = "; ".join(r.outcome.reason for r in results if r.outcome.status == NOT_RUN) or "no step ran"
    result = JobResult(job, status, reason, results, ctx.clock() - started, list(ctx.notes))
    ctx.say(f"\n{job.key}: {status}" + (f": {reason}" if reason else "") + f" ({result.seconds:.1f} s)")
    if ctx.github:
        for r, message in warnings:
            # An advisory job's own warning, below, names the steps its
            # reason holds, so they are not warned twice.
            covered = not job.blocking and status != PASSED and (
                r in broken or (status == NOT_RUN and r.outcome.status == NOT_RUN))
            if not covered:
                ctx.say(annotation(f"{job.key}: {r.step.key} (advisory)", message))
        if not job.blocking and status != PASSED:
            ctx.say(annotation(f"{job.key} (advisory)", f"{status}: {reason}"))
    write_summary(ctx, result)
    return result


def write_summary(ctx: Context, result: JobResult) -> None:
    """The job's table on GitHub's run page (`GITHUB_STEP_SUMMARY`)."""
    target = ctx.env.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    job = result.job
    lines = [f"### {job.key}: {result.status} ({job.kind}, {result.seconds:.1f} s)", ""]
    if result.reason:
        lines += [result.reason, ""]
    if result.steps:
        lines += ["| Step | Outcome | Seconds |", "|---|---|---|"]
        for r in result.steps:
            said = r.outcome.status + (f": {r.outcome.reason}" if r.outcome.reason else "")
            lines.append(f"| {r.step.name} | {said.replace('|', '/')} | {r.seconds:.1f} |")
        lines.append("")
    lines += [*result.notes, ""]
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def verdict(results: list[JobResult], strict: bool) -> tuple[str, int]:
    """The last line, and the exit code. `strict` is GitHub, where a blocking
    job that could not run is a failed one."""
    red = [r.job.key for r in results
           if r.job.blocking and (r.status == FAILED or (strict and r.status == NOT_RUN))]
    if red:
        return f"ci: FAILED ({', '.join(red)})", 1
    green = [r for r in results if r.status == PASSED]
    missing = [name for r in results for name in ([r.job.key] if r.status == NOT_RUN else r.parts_not_run)]
    advisory = [name for r in results
                for name in ([r.job.key] if r.status == FAILED else r.parts_failed_advisory)]
    line = f"ci: all green, {len(green)} job{'' if len(green) == 1 else 's'}, {len(missing)} not run here"
    if missing:
        line += f" ({', '.join(missing)})"
    if advisory:
        line += f", {len(advisory)} failed advisory ({', '.join(advisory)})"
    return line, 0


def run_jobs(ctx: Context, keys: list[str]) -> int:
    """Run these jobs in order, print the table and the verdict, clean up."""
    results = []
    try:
        for key in keys:
            results.append(run_job(ctx, BY_KEY[key]))
    finally:
        ctx.close()
    ctx.say("\n=== summary")
    width = max(len(r.job.key) for r in results)
    for r in results:
        said = r.status if r.job.blocking or r.status != FAILED else "failed advisory"
        said += f": {r.reason}" if r.reason else ""
        if r.parts_not_run:
            said += f" [not run here: {', '.join(r.parts_not_run)}]"
        if r.parts_failed_advisory:
            said += f" [failed advisory: {', '.join(r.parts_failed_advisory)}]"
        ctx.say(f"{r.job.key.ljust(width)}  {said}  ({r.seconds:.1f} s)")
    line, code = verdict(results, ctx.github)
    ctx.say(line)
    return code


# ------------------------------------------------------------------- probes


def probe_shadowed() -> int:
    """Run with the shadow folder on PYTHONPATH: every optional module must
    fail to import, as it does on a machine that never installed it."""
    still = []
    for module in OPTIONAL_MODULES:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as error:
            print(f"{module}: absent ({error})")
        else:
            print(f"{module}: STILL IMPORTS")
            still.append(module)
    return 1 if still else 0


def probe_discovery() -> int:
    """Run by the fresh venv's Python from outside the tree: every group
    discovers, and each package's version comes from installed metadata. The
    version is declared once, in pyproject.toml, and read back through
    importlib.metadata; from a wheel that path differs from an editable
    install's, so it is checked here as well as in the suites."""
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if not p or Path(p).resolve() != here]
    from importlib.metadata import version

    from emet_sdk.discovery import PluginRegistry       # the installed one: this script imports no Emet package

    registry = PluginRegistry.discover()
    problems = [f"{group}:{name} did not survive packaging" for group, name in DISCOVERED
                if not getattr(registry, f"has_{group}")(name)]
    print("discovered:", [f"{g}:{n}" for g, n in registry])
    prefix = Path(sys.prefix).resolve()
    for dist in PACKAGES:
        module = importlib.import_module(dist.replace("-", "_"))
        if module.__version__ != version(dist):
            problems.append(f"{dist} version drifted: {module.__version__} against {version(dist)}")
        if module.__version__ == "0+unknown":
            problems.append(f"{dist} reports no version")
        if prefix not in Path(module.__file__ or "").resolve().parents:
            problems.append(f"{dist} was imported from {module.__file__}, outside the environment the wheels are in")
    for problem in problems:
        print(f"  x {problem}")
    if not problems:
        print("versions:", version("emet-sdk"))
    return 1 if problems else 0


PROBES = {"shadowed": probe_shadowed, "discovery": probe_discovery}


def job_environment(environ: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The environment every job starts from, `RUN_CHANGING` left out, and
    the names that were left out."""
    dropped = [name for name in RUN_CHANGING if name in environ]
    return {k: v for k, v in environ.items() if k not in dropped}, dropped


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci.py", description="Every CI job's steps, for GitHub, the laptop and the body.",
        epilog="Jobs: " + ", ".join(BY_KEY) + ". `--list` says what each is.")
    parser.add_argument("jobs", nargs="*", metavar="JOB", help="a job to run")
    parser.add_argument("--list", action="store_true", help="every job: name, blocking or advisory, where it runs")
    parser.add_argument("--all", action="store_true", help="every job this machine can run")
    parser.add_argument("--install", action="store_true",
                        help="run each job's pip install lines, where the job places them, into the running "
                             "interpreter")
    parser.add_argument("--log", metavar="FILE", help="write everything printed to FILE as well")
    parser.add_argument("--probe", choices=sorted(PROBES), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.probe:
        return PROBES[args.probe]()
    if args.list:
        print(list_jobs())
        return 0
    unknown = [key for key in args.jobs if key not in BY_KEY]
    if unknown:
        parser.error(f"no job named {', '.join(unknown)}. The jobs: {', '.join(BY_KEY)}")
    if args.all and args.jobs:
        parser.error("--all runs every job; name jobs or pass --all")
    if not (args.all or args.jobs):
        parser.error("name a job, or pass --all or --list")

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    out = Output(Path(args.log) if args.log else None)
    env, dropped = job_environment(dict(os.environ))
    ctx = Context(root=Path(__file__).resolve().parents[1], env=env, out=out, install=args.install)
    try:
        ctx.say(f"ci: Python {platform.python_version()} at {sys.executable}")
        if dropped:
            ctx.say(f"ci: left out of every job, since each changes what a test run means: {', '.join(dropped)}")
        return run_jobs(ctx, list(BY_KEY) if args.all else args.jobs)
    finally:
        out.close()


if __name__ == "__main__":
    raise SystemExit(main())
