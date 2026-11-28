"""`tools/ci.py`, with the one function that starts a process replaced.

No test here builds a wheel, runs a suite or reaches an index. `Fake`
stands in for `run_command`: it keeps every command it was asked to run and
answers by the first rule whose text the command line holds, so a test can
show both what a job ran and what it never reached. A rule may also act, the
way a real command would: the stand-in for `python -m build` writes a small
wheel into the folder it was given, and the stand-in for pip's dry run
writes the report.

The tree the jobs see is a small one under `tmp_path`, and the temp folder
they are given is beside it, so a test can hold the tree unchanged.
Four tests start a real process: the script under `python -S -I`, the
shadow probe and `run_command` itself, each for under a second, and
`run_command`'s time limit, which ends a sleeping child and grandchild
after one second.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import pytest

CI_PY = Path(__file__).resolve().parents[1] / "ci.py"
REPO = CI_PY.parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("emet_ci", CI_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci = _load()

#: Contract §5: the check names GitHub shows, and which the ruleset requires.
CHECKS = {
    "layering": ("layering", "blocking", True),
    "dco sign-off": ("dco", "blocking", True),
    "wheel install": ("wheel", "blocking", True),
    "test (py3.11)": ("test", "blocking", True),
    "test (py3.12)": ("test", "blocking", True),
    "test (py3.13)": ("test", "blocking", True),
    "test (no extras)": ("bare", "blocking", True),
    "release audit": ("audit", "blocking", True),
    "test (arm64, advisory)": ("arm64", "advisory", False),
    "virtual sound card": ("sound", "blocking", False),
    "docs (advisory)": ("docs", "advisory", False),
    "api-diff (advisory)": ("api-diff", "advisory", False),
    "mutmut (advisory)": ("mutmut", "advisory", False),
    "pip-audit (advisory)": ("pip-audit", "advisory", False),
    "bank (advisory)": ("bank", "advisory", False),
    "bank-full (advisory)": ("bank-full", "advisory", False),
}

PYPROJECT = """[project]
name = "{name}"
version = "0.5.6"
dependencies = [{deps}]

[project.optional-dependencies]
dev = ["pytest>=9.2.0"]
net = ["httpx>=0.27", "certifi>=2026.10.18"]
"""

#: pip 24.3.1's own words, 2026-11-26, for one wheel alone against the index.
NO_MATCH = ("ERROR: Could not find a version that satisfies the requirement emet-sdk>=0.5.6 (from emet-hal) "
            "(from versions: 0.1.0, 0.1.1, 0.1.2, 0.1.3)\n"
            "ERROR: No matching distribution found for emet-sdk>=0.5.6\n")


def report(*items: tuple[str, str, str]) -> str:
    """A pip installation report holding these (name, version, url)."""
    return json.dumps({"version": "1", "pip_version": "24.3.1", "install": [
        {"metadata": {"name": name, "version": version}, "download_info": {"url": url}}
        for name, version, url in items]})


LOCAL = "file:///tmp/dist/"
INDEX = "https://files.pythonhosted.org/packages/91/41/"


class Said:
    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> None:
        self.text += text


class Fake:
    """Stands in for `ci.run_command`.

    A command line is kept with the test's own folder written as `TMP`:
    pytest names that folder after the test, so a test called "...zizmor..."
    would otherwise find its word in every path. A rule given `where`
    answers only a command started in a folder of that name. The time limit
    a command was given, if any, is kept in `timeouts`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path | None, dict[str, str]]] = []
        self.timeouts: list[float | None] = []
        self.rules: list[tuple[str, int, str, Callable[[list[str]], Any] | None, str | None]] = []
        self.base = ""

    def on(self, text: str, code: int = 0, out: str = "", do: Callable[[list[str]], Any] | None = None,
           where: str | None = None) -> "Fake":
        self.rules.append((text, code, out, do, where))
        return self

    def __call__(self, argv, *, cwd=None, env=None, echo=None, timeout=None):
        line = " ".join(str(a) for a in argv)
        line = line.replace(self.base, "TMP") if self.base else line
        self.calls.append((line, cwd, dict(env or {})))
        self.timeouts.append(timeout)
        for text, code, out, do, where in self.rules:
            if text in line and (where is None or (cwd is not None and Path(cwd).name == where)):
                answer = do(list(argv)) if do else None
                if isinstance(answer, ci.Done):
                    code, out = answer.code, answer.out
                    if answer.timed_out:
                        return answer
                if echo and out:
                    echo(out)
                return ci.Done(code, out)
        return ci.Done(0, "")

    @property
    def lines(self) -> list[str]:
        return [line for line, _, _ in self.calls]

    def ran(self, text: str) -> list[str]:
        return [line for line in self.lines if text in line]


def make_tree(tmp_path: Path, *scripts: str) -> Path:
    """A repository small enough to read: the four packages' pyproject
    files, the scripts every job expects, and whichever more are named."""
    root = tmp_path / "emet"
    for package in ci.PACKAGES:
        (root / package).mkdir(parents=True)
        deps = "" if package == "emet-sdk" else '"emet-sdk>=0.5.6"'
        (root / package / "pyproject.toml").write_text(
            PYPROJECT.format(name=package, deps=deps or '"pyyaml>=6.0.4"'), encoding="utf-8")
    present = ("tools/check_layering.py", "tools/release_check.py", "tools/check_style.py",
               "tools/ci/check_workflows.py", "tools/release_audit.py", "tools/ci.py", "tools/body/run.py",
               "tools/hardware_guard.py", *scripts)
    for name in present:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("", encoding="utf-8")
    invalid = root / "emet-sdk" / "examples" / "invalid"
    invalid.mkdir(parents=True)
    for name in ("no-voice.yaml", "two-heads.yaml"):
        (invalid / name).write_text("{}\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (tmp_path / "temp").mkdir()
    return root


def context(root: Path, fake: Fake, *, env: dict[str, str] | None = None, version=(3, 13), os_name="win32",
            machine="amd64", install=False, modules: tuple[str, ...] = (), on_path: tuple[str, ...] = (),
            fork=False, clock: Callable[[], float] | None = None):
    fake.base = str(root.parent)
    return ci.Context(
        root=root, env=dict(env or {}), out=Said(), runner=fake, install=install,
        python=str(root.parent / "py" / "python"), version=version, os_name=os_name, machine=machine,
        temp=root.parent / "temp", has_module=lambda name: name in modules,
        which=lambda name, path=None: f"/usr/bin/{name}" if name in on_path else None, has_fork=fork,
        **({"clock": clock} if clock else {}))


#: The temp folder a job was given, the tree, and the interpreter, as `Fake` writes them.
TEMP, TREE = os.path.join("TMP", "temp"), os.path.join("TMP", "emet")
PY = os.path.join("TMP", "py", "python")


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else b"<dir>")
            for p in sorted(root.rglob("*"))}


def last_line(ctx) -> str:
    return ctx.out.text.rstrip().splitlines()[-1]


# ------------------------------------------------------------- the job table


def test_list_names_every_job_with_the_check_names_github_shows(capsys):
    assert ci.main(["--list"]) == 0
    rows = [re.split(r"\s{2,}", line) for line in capsys.readouterr().out.splitlines()[1:] if "  " in line]
    listed = {row[1]: (row[0], row[2], row[3] == "required") for row in rows}
    assert listed == CHECKS
    assert sum(1 for _, _, required in listed.values() if required) == 8
    assert list(ci.BY_KEY) == ["layering", "dco", "wheel", "test", "bare", "audit", "arm64", "sound",
                               "docs", "api-diff", "mutmut", "pip-audit", "bank", "bank-full"]


def test_a_job_the_table_does_not_have_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as stopped:
        ci.main(["layering", "nosuch"])
    assert stopped.value.code == 2
    assert "no job named nosuch" in capsys.readouterr().err
    for argv in ([], ["--all", "layering"]):
        with pytest.raises(SystemExit) as stopped:
            ci.main(argv)
        assert stopped.value.code == 2


def test_every_job_a_workflow_names_is_in_the_table():
    """`python tools/ci.py NAME` in `.github/workflows/*.yml` must be a job."""
    called = []
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.search(r"python[0-9.]*[ \t]+tools/ci\.py((?:[ \t]+[^\s#|&;]+)*)", line)
            if match and "#" not in line[:match.start()]:
                called.append((workflow.name, match.group(1).split()))
    if not called:
        pytest.skip("no workflow calls tools/ci.py yet")
    flags = {"--install", "--all", "--list", "--log"}
    for name, words in called:
        jobs = [w for w in words if not w.startswith("-")]
        assert jobs or "--all" in words or "--list" in words, f"{name}: tools/ci.py is called with no job"
        for word in jobs:
            assert word in ci.BY_KEY, f"{name}: tools/ci.py {word}: no such job (write the job's name, no variable)"
        for word in words:
            assert not word.startswith("-") or word in flags, f"{name}: tools/ci.py {word}: no such option"


def test_the_script_starts_on_a_bare_python():
    done = subprocess.run([sys.executable, "-S", "-I", str(CI_PY), "--list"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "test (no extras)" in done.stdout


# ------------------------------------------------------ steps and their jobs


def test_a_blocking_step_that_fails_fails_the_job_and_stops_its_later_steps(tmp_path):
    root, fake = make_tree(tmp_path, "tools/check_health.py"), Fake().on("check_layering.py", code=1)
    ctx = context(root, fake)
    assert ci.run_jobs(ctx, ["layering"]) == 1
    assert fake.ran("check_layering.py") and not fake.ran("release_check.py") and not fake.ran("check_style.py")
    assert "not reached: layering failed" in ctx.out.text
    assert last_line(ctx) == "ci: FAILED (layering)"


@pytest.mark.parametrize("key, script", [
    ("layering", "check_layering.py"), ("release", "release_check.py"), ("style", "check_style.py"),
    ("health", "check_health.py"), ("zizmor", "/usr/bin/zizmor"), ("workflows", "check_workflows.py")])
def test_each_check_of_the_layering_job_fails_it_on_github_and_stops_the_rest(tmp_path, key, script):
    """The required `layering` check exists for these."""
    root = make_tree(tmp_path, "tools/check_health.py")
    fake = Fake().on(script, code=1)
    ctx = context(root, fake, env={"GITHUB_ACTIONS": "true"}, on_path=("zizmor",))
    assert ci.run_jobs(ctx, ["layering"]) == 1
    assert f"\nlayering: failed: {key}: exit 1 (" in ctx.out.text and last_line(ctx) == "ci: FAILED (layering)"
    keys = [step.key for step in ci.layering_steps(ctx) if not step.install]
    assert ctx.out.text.count(f"not reached: {key} failed") == len(keys) - keys.index(key) - 1


def test_a_tool_that_needs_more_than_the_standard_library_fails_the_layering_job(tmp_path):
    root = make_tree(tmp_path, "tools/check_health.py")
    fake = Fake().on("-S -I", code=1, out="ModuleNotFoundError: No module named 'yaml'\n")
    ctx = context(root, fake, on_path=("zizmor",))
    result = ci.run_job(ctx, ci.BY_KEY["layering"])
    assert result.status == ci.FAILED
    assert result.reason == "stdlib: needs more than the standard library: tools/body/run.py, tools/ci.py"
    assert "tools/ci.py --list under -S -I: exit 1" in ctx.out.text and "No module named 'yaml'" in ctx.out.text


def test_the_audits_second_step_runs_even_when_the_first_failed(tmp_path):
    root, fake = make_tree(tmp_path), Fake().on("release_audit.py", code=1)
    ctx = context(root, fake, env={"PR": "17", "GH_TOKEN": "t"}, on_path=("gh",))
    result = ci.run_job(ctx, ci.BY_KEY["audit"])
    assert [line.split("release_audit.py")[1].strip() for line in fake.lines] == ["", "--pr 17 --ci"]
    assert result.status == ci.FAILED and len(result.steps) == 2
    assert fake.calls[1][2]["GH_TOKEN"] == "t"


def test_the_audit_has_one_step_where_github_gives_no_pull_request(tmp_path):
    root, fake = make_tree(tmp_path), Fake()
    result = ci.run_job(context(root, fake, on_path=("gh",)), ci.BY_KEY["audit"])
    assert len(fake.lines) == 1 and fake.lines[0].endswith("tools/release_audit.py")
    assert result.status == ci.PASSED
    no_gh = ci.run_job(context(root, Fake(), os_name="linux"), ci.BY_KEY["audit"])
    assert (no_gh.status, no_gh.reason) == (ci.NOT_RUN, "needs gh, the GitHub CLI")


def test_a_step_that_raises_is_a_failure(tmp_path):
    def broken(ctx):
        raise RuntimeError("the step itself is at fault")

    job = ci.Job("x", ("x",), True, False, "anywhere", lambda ctx: [ci.Step("Break", "break", broken)])
    ctx = context(make_tree(tmp_path), Fake())
    result = ci.run_job(ctx, job)
    assert result.status == ci.FAILED and "RuntimeError" in result.reason


def test_without_install_no_job_runs_pip(tmp_path):
    root, fake = make_tree(tmp_path, "tools/check_licences.py"), Fake()
    ctx = context(root, fake, os_name="linux", machine="aarch64", env={"EMET_VIRTUAL_AUDIO": "1"}, fork=True)
    ci.run_jobs(ctx, list(ci.BY_KEY))
    assert fake.lines and not fake.ran("pip install")


# ----------------------------------------------------------------- advisory


def test_an_advisory_job_that_fails_exits_0_on_github_with_a_warning(tmp_path):
    root, fake = make_tree(tmp_path, "tools/check_docs.py"), Fake().on("check_docs.py", code=1)
    summary = tmp_path / "summary.md"
    ctx = context(root, fake, env={"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)})
    assert ci.run_jobs(ctx, ["docs"]) == 0
    assert "::warning title=docs (advisory)::failed: docs: exit 1" in ctx.out.text
    assert last_line(ctx) == "ci: all green, 0 jobs, 0 not run here, 1 failed advisory (docs)"
    written = summary.read_text(encoding="utf-8")
    assert "### docs: failed (advisory" in written and "| Run the documents' commands" in written


def test_an_advisory_job_that_fails_is_named_on_the_laptop_and_leaves_all_green(tmp_path):
    root, fake = make_tree(tmp_path, "tools/check_docs.py", "tools/check_health.py"), Fake().on("check_docs.py", code=1)
    ctx = context(root, fake, on_path=("zizmor", "flake8"))
    assert ci.run_jobs(ctx, ["layering", "docs"]) == 0
    assert "::warning" not in ctx.out.text
    assert last_line(ctx) == "ci: all green, 1 job, 0 not run here, 1 failed advisory (docs)"


def test_an_advisory_job_whose_script_is_missing_warns_on_github(tmp_path):
    ctx = context(make_tree(tmp_path), Fake(), env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["docs"]) == 0
    assert "::warning title=docs (advisory)::failed: docs: not run here: tools/check_docs.py is missing" in ctx.out.text


def test_a_warning_is_escaped_the_way_workflow_commands_need():
    assert ci.annotation("test: coverage, lines", "50% of\nit") == \
        "::warning title=test%3A coverage%2C lines::50%25 of%0Ait"


def test_arm64_runs_every_suite_whatever_the_one_before_did(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake().on("pytest", code=1)
    ctx = context(root, fake, os_name="linux", machine="aarch64", env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["arm64"]) == 0
    assert len(fake.ran("pytest")) == 4 and fake.ran("check_licences.py")
    warning = next(line for line in ctx.out.text.splitlines() if line.startswith("::warning title=arm64 (advisory)"))
    assert all(package in warning for package in ci.PACKAGES)


def test_arm64_installs_the_wake_engine_and_checks_the_licences_after_its_installs(tmp_path):
    """The reference body's architecture, pocketsphinx's aarch64 wheel included."""
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake()
    ctx = context(root, fake, os_name="linux", machine="aarch64", install=True)
    assert ci.run_job(ctx, ci.BY_KEY["arm64"]).status == ci.PASSED
    assert [line.split("pip install ")[1] for line in fake.ran("pip install")] == [
        "-e emet-sdk[dev]", "-e emet-hal[dev,wake]", "-e emet-providers[dev,deepgram,anthropic,openai]",
        "-e emet-engine[dev]"]
    assert fake.lines[4] == f"{PY} tools/check_licences.py" and len(fake.lines) == 9
    # The licence check runs whatever the installs did, and a refusal warns.
    failing = Fake().on("pip install", code=1).on("check_licences.py", code=1)
    ctx = context(root, failing, os_name="linux", machine="aarch64", install=True, env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["arm64"]) == 0
    assert failing.ran("check_licences.py") and len(failing.ran("-m pytest")) == 4
    warning = next(line for line in ctx.out.text.splitlines() if line.startswith("::warning title=arm64 (advisory)"))
    assert "licences: exit 1" in warning


# ------------------------------------------------- not run is never passed


def test_a_missing_script_and_a_missing_tool_are_not_run_here_and_named(tmp_path):
    root, fake = make_tree(tmp_path), Fake()                # no check_health.py, and no zizmor anywhere
    ctx = context(root, fake)
    result = ci.run_job(ctx, ci.BY_KEY["layering"])
    by_key = {r.step.key: r.outcome for r in result.steps}
    assert by_key["health"] == ci.Outcome(ci.NOT_RUN, "tools/check_health.py is missing")
    assert by_key["zizmor"] == ci.Outcome(ci.NOT_RUN, "needs zizmor")
    assert by_key["flake8"] == ci.Outcome(ci.NOT_RUN, "needs flake8")
    assert by_key["style"].status == ci.PASSED
    assert not fake.ran("check_health.py") and not fake.ran("zizmor")
    assert result.status == ci.PASSED
    assert result.parts_not_run == ["layering/health", "layering/zizmor", "layering/flake8"]
    line, code = ci.verdict([result], strict=False)
    assert (line, code) == ("ci: all green, 1 job, 3 not run here (layering/health, layering/zizmor, "
                            "layering/flake8)", 0)


def test_on_github_a_blocking_step_that_could_not_run_fails_its_job(tmp_path):
    ctx = context(make_tree(tmp_path), Fake(), env={"GITHUB_ACTIONS": "true"}, on_path=("zizmor",))
    assert ci.run_jobs(ctx, ["layering"]) == 1
    assert "not run here: tools/check_health.py is missing. On GitHub nothing may be missing" in ctx.out.text
    assert last_line(ctx) == "ci: FAILED (layering)"


def test_a_job_with_nothing_it_could_run_is_not_run(tmp_path):
    ctx = context(make_tree(tmp_path), Fake())
    assert ci.run_jobs(ctx, ["docs", "api-diff", "arm64", "sound", "mutmut"]) == 0
    assert last_line(ctx) == "ci: all green, 0 jobs, 5 not run here (docs, api-diff, arm64, sound, mutmut)"
    assert "needs Linux on arm64" in ctx.out.text and "mutmut needs os.fork" in ctx.out.text


def test_zizmor_runs_offline_over_the_workflows_with_its_configuration(tmp_path):
    root, fake = make_tree(tmp_path, "tools/check_health.py", "tools/ci/zizmor.yml"), Fake().on("zizmor", code=14)
    result = ci.run_job(context(root, fake, on_path=("zizmor",)), ci.BY_KEY["layering"])
    assert fake.ran("zizmor") == ["/usr/bin/zizmor --offline --no-progress --color never "
                                  "--config tools/ci/zizmor.yml .github"]
    assert result.status == ci.FAILED and "zizmor: exit 14" in result.reason


def test_the_layering_job_holds_the_workflows_to_their_house_rules_after_zizmor(tmp_path):
    """Stream B2's B2-2 found that `check_workflows.py` had no step of its own."""
    root = make_tree(tmp_path, "tools/check_health.py")
    fake = Fake().on("check_workflows.py", code=1)
    result = ci.run_job(context(root, fake, on_path=("zizmor",)), ci.BY_KEY["layering"])
    checks = [line for line in fake.lines if "zizmor" in line or "check_workflows.py" in line]
    assert len(checks) == 2 and checks[0].startswith("/usr/bin/zizmor")
    assert checks[1].endswith("tools/ci/check_workflows.py .")
    assert result.status == ci.FAILED and result.reason == "workflows: exit 1"
    assert any(step.key == "workflows" for step in ci.layering_steps(context(root, Fake())))
    assert (REPO / "tools" / "ci" / "check_workflows.py").is_file(), "the step names a script the repository lacks"


def test_a_tool_is_taken_from_the_interpreter_then_the_tools_venv_then_path(tmp_path):
    root = make_tree(tmp_path, "tools/.venv/Scripts/zizmor.exe")
    assert context(root, Fake(), modules=("build",)).tool("pyproject-build", module="build")[1:] == ["-m", "build"]
    assert context(root, Fake(), on_path=("zizmor",)).tool("zizmor") == [str(root / "tools/.venv/Scripts/zizmor.exe")]
    assert context(root, Fake(), on_path=("pip-audit",)).tool("pip-audit", module="pip_audit") == ["/usr/bin/pip-audit"]
    assert context(root, Fake()).tool("pip-audit", module="pip_audit") is None


def test_the_sound_card_job_needs_linux_and_the_variable(tmp_path):
    root = make_tree(tmp_path)
    for os_name, env in (("win32", {"EMET_VIRTUAL_AUDIO": "1"}), ("linux", {})):
        fake = Fake()
        result = ci.run_job(context(root, fake, os_name=os_name, env=env), ci.BY_KEY["sound"])
        assert result.status == ci.NOT_RUN and fake.lines == []
    fake = Fake()
    result = ci.run_job(context(root, fake, os_name="linux", env={"EMET_VIRTUAL_AUDIO": "1"}), ci.BY_KEY["sound"])
    assert result.status == ci.PASSED and "tests/test_audio_virtual.py tests/test_audio.py" in fake.lines[-1]
    assert fake.calls[-1][2]["EMET_VIRTUAL_AUDIO"] == "1" and fake.calls[-1][1] == root / "emet-hal"
    strict = context(root, Fake(), os_name="linux", env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(strict, ["sound"]) == 1 and last_line(strict) == "ci: FAILED (sound)"


# --------------------------------------------------------------- the verdict


def result(key: str, status: str, *steps: tuple[str, str, bool]):
    made = [ci.StepResult(ci.Step(k, k, lambda ctx: ci.passed(), advisory=advisory), ci.Outcome(s), 0.0)
            for k, s, advisory in steps]
    return ci.JobResult(ci.BY_KEY[key], status, "", made)


def test_the_verdict_line_in_each_case():
    green = [result("layering", ci.PASSED), result("test", ci.PASSED), result("bare", ci.PASSED)]
    assert ci.verdict(green, False) == ("ci: all green, 3 jobs, 0 not run here", 0)
    some = [*green, result("arm64", ci.NOT_RUN), result("wheel", ci.PASSED, ("build", ci.PASSED, False)),
            result("audit", ci.PASSED, ("history", ci.NOT_RUN, False))]
    assert ci.verdict(some, False) == ("ci: all green, 5 jobs, 2 not run here (arm64, audit/history)", 0)
    red = [*some, result("dco", ci.FAILED), result("docs", ci.FAILED)]
    assert ci.verdict(red, False) == ("ci: FAILED (dco)", 1)
    warned = [*green, result("docs", ci.FAILED), result("wheel", ci.PASSED, ("coverage", ci.FAILED, True))]
    assert ci.verdict(warned, False) == (
        "ci: all green, 4 jobs, 0 not run here, 2 failed advisory (docs, wheel/coverage)", 0)
    assert ci.verdict([result("sound", ci.NOT_RUN)], False) == ("ci: all green, 0 jobs, 1 not run here (sound)", 0)
    assert ci.verdict([result("sound", ci.NOT_RUN)], True) == ("ci: FAILED (sound)", 1)
    assert ci.verdict([result("arm64", ci.NOT_RUN)], True)[1] == 0


# --------------------------------------------------- the test job's versions


def run_test_job(tmp_path, version, modules=(), reject=True):
    """GitHub's `test` job on one Python. `reject` makes the stand-in `emet`
    refuse the invalid fixtures, as the real one does."""
    root, fake = make_tree(tmp_path, "tools/check_licences.py", ci.CI_TOOLS), Fake().on("coverage report", out="87\n")
    fake.on("--collect-only", out="7 tests collected in 0.01s\n")
    if reject:
        fake.on("examples/invalid", code=1)
    ctx = context(root, fake, version=version, install=True, modules=modules)
    return ci.run_job(ctx, ci.BY_KEY["test"]), fake, ctx


def test_python_3_11_installs_the_wake_engine_and_leaves_the_tools_tests(tmp_path):
    outcome, fake, _ = run_test_job(tmp_path, (3, 11))
    assert outcome.status == ci.PASSED
    assert [line.split("pip install ")[1] for line in fake.ran("pip install")] == [
        "-e emet-sdk[dev]", "-e emet-hal[dev]", "-e emet-providers[dev,deepgram,anthropic,openai]",
        "-e emet-engine[dev]", "-e emet-hal[wake]"]
    assert len(fake.ran("pytest")) == 4 and not fake.ran("tools/tests") and not fake.ran("-X dev")
    assert not fake.ran("coverage") and fake.ran("check_licences.py")


def test_python_3_12_has_no_wake_wheel_so_none_is_installed(tmp_path):
    outcome, fake, _ = run_test_job(tmp_path, (3, 12))
    assert outcome.status == ci.PASSED
    assert len(fake.ran("pip install")) == 4 and not fake.ran("emet-hal[wake]")
    assert len(fake.ran("pytest")) == 4 and not fake.ran("tools/tests") and not fake.ran("-X dev")


def test_python_3_13_runs_under_dev_mode_with_the_tools_tests_and_coverage(tmp_path):
    outcome, fake, ctx = run_test_job(tmp_path, (3, 13), modules=("coverage",))
    assert outcome.status == ci.PASSED and fake.ran("emet-hal[wake]")
    blocking = [line for line in fake.ran("-m pytest") if "coverage" not in line and "--collect-only" not in line]
    assert len(blocking) == 5 and all(" -X dev -m pytest -q" in line for line in blocking)
    assert blocking[-1].endswith("tools/tests")
    cwds = [cwd.name for line, cwd, _ in fake.calls if "-X dev" in line]
    assert cwds == ["emet-sdk", "emet-hal", "emet-providers", "emet-engine", "emet"]
    # The CI tools arrive after the last blocking step, for their licences and the measurement alone.
    tools_at = fake.lines.index(next(line for line in fake.lines if "--require-hashes" in line))
    after = fake.lines[tools_at + 1:]
    assert after[0] == f"{PY} tools/check_licences.py --requirements {ci.CI_TOOLS} --python {PY}"
    assert all("coverage" in line for line in after[1:])
    assert all("-X dev" not in line and "validate" not in line for line in fake.lines[tools_at:])
    assert len(fake.ran("coverage run")) == 4
    assert f"--data-file={TEMP}" in fake.ran("coverage run")[0]
    assert "coverage, lines, no floor (N76): emet-sdk 87%, emet-hal 87%" in ctx.out.text
    assert [note.split(":")[0] for note in outcome.notes] == ["tests collected", "coverage, lines, no floor (N76)"]


#: Every suite the `test` job runs, by Python: the four packages on each, the tools' tests on 3.13.
SUITES = [*((version, package) for version in ((3, 11), (3, 12), (3, 13)) for package in ci.PACKAGES),
          ((3, 13), "tools")]


def fail_suite(fake: Fake, key: str) -> Fake:
    """Make one suite fail, a package's in its own folder or the tools'."""
    if key == "tools":
        return fake.on("tools/tests", code=1, out="1 failed\n")
    return fake.on("-m pytest", code=1, out="1 failed\n", where=key)


@pytest.mark.parametrize("version, key", SUITES, ids=[f"py{v[0]}.{v[1]}-{k}" for v, k in SUITES])
def test_a_failing_suite_fails_the_test_job_on_github(tmp_path, version, key):
    """The suites are what the required `test (py3.x)` checks hold."""
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = fail_suite(Fake().on("--collect-only", out="7 tests collected in 0.01s\n").on("examples/invalid", code=1),
                      key)
    ctx = context(root, fake, version=version, env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["test"]) == 1
    assert f"\ntest: failed: {key}: exit 1 (" in ctx.out.text and last_line(ctx) == "ci: FAILED (test)"


@pytest.mark.parametrize("key", [*ci.PACKAGES, "tools"])
def test_a_failing_suite_fails_the_no_extras_job_on_github(tmp_path, key):
    """The same for the required `test (no extras)`, each suite with the five extras shadowed."""
    ctx = context(make_tree(tmp_path), fail_suite(Fake(), key), env={"GITHUB_ACTIONS": "true"}, modules=("httpx",))
    assert ci.run_jobs(ctx, ["bare"]) == 1
    assert f"\nbare: failed: {key}: exit 1 (" in ctx.out.text and last_line(ctx) == "ci: FAILED (bare)"


@pytest.mark.parametrize("version", [(3, 11), (3, 12), (3, 13)])
def test_the_licence_check_follows_every_package_install_and_comes_before_the_suites(tmp_path, version):
    """BVP1 judges what the job installed: run before the installs it would
    read setup-python's bare interpreter and pass."""
    outcome, fake, ctx = run_test_job(tmp_path, version)
    lines = fake.lines
    licence = lines.index(f"{PY} tools/check_licences.py")
    installs = [i for i, line in enumerate(lines) if "pip install -e " in line]
    suites = [i for i, line in enumerate(lines) if "-m pytest" in line and "--collect-only" not in line
              and "coverage" not in line]
    assert outcome.status == ci.PASSED and installs and max(installs) < licence < min(suites)
    assert f"=== test: test (py{version[0]}.{version[1]}) (blocking)\n" in ctx.out.text
    if version == (3, 13):
        lock = next(i for i, line in enumerate(lines) if "--require-hashes" in line)
        assert lines[lock + 1] == f"{PY} tools/check_licences.py --requirements {ci.CI_TOOLS} --python {PY}"
    else:
        assert not fake.ran("--requirements")


@pytest.mark.parametrize("version", [(3, 11), (3, 12), (3, 13)])
def test_a_refused_licence_fails_the_test_job_before_any_suite(tmp_path, version):
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake().on("check_licences.py", code=1, out="licences: 1 refused\n")
    ctx = context(root, fake, version=version, env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["test"]) == 1
    assert "\ntest: failed: licences: exit 1 (" in ctx.out.text and not fake.ran("-m pytest")


def test_the_ci_locks_licences_are_checked_where_the_job_installed_it_and_warn(tmp_path):
    """Contract §5's `--requirements` line, advisory: a refused CI tool warns
    and leaves the required check green."""
    root = make_tree(tmp_path, "tools/check_licences.py", ci.CI_TOOLS)
    fake = Fake().on("--requirements", code=1, out="licences: 1 refused\n").on("examples/invalid", code=1) \
        .on("--collect-only", out="7 tests collected in 0.01s\n").on("coverage report", out="87\n")
    ctx = context(root, fake, install=True, modules=("coverage",), env={"GITHUB_ACTIONS": "true"})
    result = ci.run_job(ctx, ci.BY_KEY["test"])
    assert result.status == ci.PASSED and result.parts_failed_advisory == ["test/tools-licences"]
    assert "::warning title=test%3A tools-licences (advisory)::exit 1" in ctx.out.text
    assert len(fake.ran("coverage run")) == 4
    # Only the advisory part is timed: a blocking step keeps the workflow's own limit.
    timed = {line: timeout for line, timeout in zip(fake.lines, fake.timeouts)}
    advisory = [line for line in timed if "coverage" in line or "--requirements" in line
                or "--require-hashes" in line or "--collect-only" in line]
    assert advisory and all(0 < timed[line] <= ci.BY_KEY["test"].limit for line in advisory)
    assert all(timed[line] is None for line in timed if line not in advisory)


def test_the_coverage_tools_install_is_advisory_inside_the_required_test_job(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py", ci.CI_TOOLS)
    fake = Fake().on("--require-hashes", code=1).on("examples/invalid", code=1) \
        .on("--collect-only", out="7 tests collected in 0.01s\n")
    ctx = context(root, fake, install=True, modules=("coverage",), env={"GITHUB_ACTIONS": "true"})
    result = ci.run_job(ctx, ci.BY_KEY["test"])
    assert result.status == ci.PASSED and result.parts_failed_advisory == ["test/install"]
    assert ci.verdict([result], strict=True)[1] == 0
    assert not fake.ran("coverage run") and not fake.ran("--requirements")


@pytest.mark.parametrize("text, reason", [
    ("examples/bodiless.yaml examples/scout-01.yaml", "valid-examples: exit 1"),
    ("--verify-drivers", "boot-strictness: exit 1"),
    ("explain examples/bodiless.yaml", "explain: emet explain examples/bodiless.yaml exited 1"),
], ids=["valid-examples", "boot-strictness", "explain"])
def test_a_cli_step_whose_command_fails_fails_the_test_job(tmp_path, text, reason):
    root = make_tree(tmp_path, "tools/check_licences.py")
    outcome = ci.run_job(context(root, Fake().on(text, code=1).on("examples/invalid", code=1), version=(3, 11)),
                         ci.BY_KEY["test"])
    assert outcome.status == ci.FAILED and outcome.reason == reason


def test_an_empty_folder_of_invalid_examples_fails_the_test_job(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py")
    for fixture in (root / "emet-sdk" / "examples" / "invalid").glob("*.yaml"):
        fixture.unlink()
    outcome = ci.run_job(context(root, Fake(), version=(3, 11)), ci.BY_KEY["test"])
    assert outcome.status == ci.FAILED and outcome.reason == "invalid-examples: there is no invalid fixture to reject"


def test_the_cli_steps_ask_for_each_example_and_reject_each_invalid_one(tmp_path):
    outcome, fake, ctx = run_test_job(tmp_path, (3, 11), reject=False)
    emet = [line for line in fake.lines if line.startswith("emet ")]
    assert emet == [
        "emet validate examples/bodiless.yaml examples/scout-01.yaml examples/emet-soul.yaml",
        "emet validate examples/mock-scout.yaml --verify-drivers",
        "emet explain examples/bodiless.yaml",
        "emet explain examples/mock-scout.yaml --why",
        "emet validate examples/invalid/no-voice.yaml",
        "emet validate examples/invalid/two-heads.yaml",
    ]
    # The stand-in exits 0 for every command, so both invalid fixtures "validated clean".
    assert outcome.status == ci.FAILED and "2 invalid fixture(s) validated clean" in outcome.reason


def test_coverage_that_is_not_installed_is_not_measured_and_says_so(tmp_path):
    root, fake = make_tree(tmp_path, "tools/check_licences.py"), Fake().on("examples/invalid", code=1)
    outcome = ci.run_job(context(root, fake), ci.BY_KEY["test"])
    assert outcome.status == ci.PASSED
    assert outcome.parts_not_run == ["test/extras-licences", "test/numpy", "test/tools-licences", "test/coverage"]
    assert not fake.ran("coverage") and not fake.ran("pip install") and not fake.ran("--requirements")
    reasons = {r.step.key: r.outcome.reason for r in outcome.steps}
    assert reasons["tools-licences"] == f"needs {ci.CI_TOOLS} installed by this job (--install)"


def test_a_suite_that_fails_under_coverage_is_a_warning_and_the_job_stays_green(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake().on("coverage run", code=1, out="1 failed\n").on("coverage report", out="80\n") \
        .on("examples/invalid", code=1).on("--collect-only", out="3 tests collected in 0.01s\n")
    outcome = ci.run_job(context(root, fake, modules=("coverage",)), ci.BY_KEY["test"])
    assert outcome.status == ci.PASSED and outcome.parts_failed_advisory == ["test/coverage"]


def test_python_3_13_prints_the_tests_each_suite_collects_before_the_suites(tmp_path):
    """Stream B1's F4: the counts `local_ci_055.sh` printed and the planning
    documents quote, one line in the job's summary."""
    root = make_tree(tmp_path, "tools/check_licences.py")
    said = iter(["tests/test_a.py::test_one\n\n226 tests collected in 1.20s\n", "116 tests collected in 0.40s\n",
                 "243 tests collected in 0.90s\n", "1010 tests collected in 3.10s\n", "1 test collected in 0.01s\n"])
    fake = Fake().on("--collect-only", do=lambda argv: ci.Done(0, next(said))).on("examples/invalid", code=1)
    outcome = ci.run_job(context(root, fake), ci.BY_KEY["test"])
    assert outcome.status == ci.PASSED and outcome.parts_failed_advisory == []
    assert ("tests collected: emet-sdk 226, emet-hal 116, emet-providers 243, emet-engine 1010, tools/tests 1; "
            "1595 in the four packages") in outcome.notes
    collects = [(i, line, cwd) for i, (line, cwd, _) in enumerate(fake.calls) if "--collect-only" in line]
    assert [cwd.name for _, _, cwd in collects] == [*ci.PACKAGES, "emet"]
    assert collects[-1][1].endswith("-m pytest -q -p no:cacheprovider --collect-only tools/tests")
    first_suite = next(i for i, (line, _, _) in enumerate(fake.calls) if "-X dev -m pytest" in line)
    assert all(i < first_suite for i, _, _ in collects)
    for version in ((3, 11), (3, 12)):
        other = Fake().on("examples/invalid", code=1)
        ci.run_job(context(root, other, version=version), ci.BY_KEY["test"])
        assert not other.ran("--collect-only")


def test_the_count_is_read_off_the_last_line_pytest_prints():
    assert ci.collected_count("a\n\n1010 tests collected in 2.10s\n") == 1010
    assert ci.collected_count("1 test collected in 0.01s") == 1
    assert ci.collected_count("5 tests collected, 1 error in 0.30s\n") == 5
    assert ci.collected_count("1010 tests collected in 2.10s\nno tests ran\n") is None
    assert ci.collected_count("") is None


def test_a_suite_that_will_not_collect_warns_and_the_job_goes_on(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake().on("--collect-only", code=2, out="5 tests collected, 1 error in 0.30s\n").on("examples/invalid", code=1)
    ctx = context(root, fake)
    outcome = ci.run_job(ctx, ci.BY_KEY["test"])
    assert outcome.status == ci.PASSED and outcome.parts_failed_advisory == ["test/counts"]
    assert "emet-sdk: collection exited 2: 5 tests collected, 1 error in 0.30s" in ctx.out.text
    assert outcome.notes[0] == "tests collected: none read"
    assert len([line for line in fake.lines if "-X dev -m pytest" in line]) == 5


# ------------------------------------------------- the bank builder's numpy


def numpy_job(tmp_path: Path, said: str, code: int = 0, **how: Any):
    """The 3.13 test job with the bank tools' list in the tree, the numpy
    test answering `said` with `code`."""
    root = make_tree(tmp_path, "tools/check_licences.py", ci.CI_TOOLS, ci.BANK_REQUIREMENTS)
    (root / ci.BANK_REQUIREMENTS).write_text("# the bank's tools\nfaster-whisper==1.2.1\nnumpy==2.5.4\n",
                                             encoding="utf-8")
    fake = Fake().on("examples/invalid", code=1).on("--collect-only", out="7 tests collected in 0.01s\n") \
        .on("coverage report", out="87\n").on(ci.NUMPY_TEST, code=code, out=said)
    ctx = context(root, fake, **how)
    return ci.run_job(ctx, ci.BY_KEY["test"]), fake, ctx


def test_the_numpy_test_runs_against_numpy_in_a_folder_of_its_own_after_the_blocking_steps(tmp_path):
    """Review RC124. The test skips wherever numpy is absent, CI's suites
    included, and numpy may not enter the job's interpreter, whose licence
    steps would refuse it. So 3.13 installs the bank tools' pin into a
    folder under the temp folder and runs that one test with it first on
    the path."""
    outcome, fake, ctx = numpy_job(tmp_path, "1 passed in 0.83s\n", install=True, modules=("coverage",),
                                   env={"GITHUB_ACTIONS": "true"})
    assert outcome.status == ci.PASSED and outcome.parts_failed_advisory == []
    assert "test/numpy" not in outcome.parts_not_run
    pip = [i for i, line in enumerate(fake.lines) if "--target" in line]
    assert len(pip) == 1
    line = fake.lines[pip[0]]
    assert line.startswith(f"{PY} -m pip install --disable-pip-version-check --no-deps --only-binary :all: "
                           f"--target {TEMP}") and line.endswith(" numpy==2.5.4")
    folder = line.split("--target ")[1].split()[0]
    test = next(i for i, (line, _, _) in enumerate(fake.calls) if ci.NUMPY_TEST in line)
    assert fake.lines[test] == f"{PY} -m pytest -q -p no:cacheprovider -rs {ci.NUMPY_TEST}"
    assert fake.calls[test][2]["PYTHONPATH"].split(os.pathsep)[0].replace(str(tmp_path), "TMP") == folder
    assert fake.calls[test][1] == ctx.root
    # After every blocking step and before the CI tools; nothing else is given numpy's folder.
    last_blocking = max(i for i, line in enumerate(fake.lines) if line.startswith("emet validate examples/invalid"))
    lock = next(i for i, line in enumerate(fake.lines) if "--require-hashes" in line)
    assert last_blocking < pip[0] < test < lock
    assert [i for i, line in enumerate(fake.lines) if "numpy" in line] == [pip[0], test]
    assert all("PYTHONPATH" not in env for line, _, env in fake.calls if ci.NUMPY_TEST not in line)
    # An advisory part: on GitHub both are timed inside the job's limit.
    assert all(0 < fake.timeouts[i] <= ci.BY_KEY["test"].limit for i in (pip[0], test))
    # 3.11 and 3.12 do not run it.
    for version in ((3, 11), (3, 12)):
        _, other, _ = numpy_job(tmp_path / f"py{version[1]}", "1 passed in 0.83s\n", install=True, version=version)
        assert not other.ran("--target") and not other.ran(ci.NUMPY_TEST)


@pytest.mark.parametrize("said, code, reason", [
    ("1 skipped in 0.20s\n", 0, "the test did not run against numpy==2.5.4: 1 skipped in 0.20s"),
    ("1 passed, 1 skipped in 0.20s\n", 0, "the test did not run against numpy==2.5.4: 1 passed, 1 skipped in 0.20s"),
    ("1 failed in 0.30s\n", 1, "exit 1: 1 failed in 0.30s"),
    ("no tests ran in 0.10s\n", 5, "exit 5: no tests ran in 0.10s"),
], ids=["skipped", "one-of-two-skipped", "failed", "none-ran"])
def test_a_numpy_test_that_skips_or_fails_warns_and_leaves_the_required_check_green(tmp_path, said, code, reason):
    outcome, fake, ctx = numpy_job(tmp_path, said, code, install=True, env={"GITHUB_ACTIONS": "true"})
    assert outcome.status == ci.PASSED and outcome.parts_failed_advisory == ["test/numpy"]
    assert {r.step.key: r.outcome.reason for r in outcome.steps}["numpy"] == reason
    assert "::warning title=test%3A numpy (advisory)::" in ctx.out.text and ci.verdict([outcome], strict=True)[1] == 0


def test_the_numpy_test_is_not_run_without_install_or_without_a_pin(tmp_path):
    needs = ci.Outcome(ci.NOT_RUN, "needs numpy, which this job installs into a folder of its own only under --install")
    outcome, fake, _ = numpy_job(tmp_path, "1 passed in 0.83s\n")
    assert not fake.ran("--target") and not fake.ran(ci.NUMPY_TEST)
    assert {r.step.key: r.outcome for r in outcome.steps}["numpy"] == needs
    root = make_tree(tmp_path / "no-pin", "tools/check_licences.py")
    unpinned = ci.run_job(context(root, Fake().on("examples/invalid", code=1), install=True), ci.BY_KEY["test"])
    installs = [r.outcome for r in unpinned.steps if r.step.name.startswith("Install numpy")]
    assert installs == [ci.Outcome(ci.NOT_RUN, f"{ci.BANK_REQUIREMENTS} pins no numpy")]


def test_a_numpy_install_that_pip_refuses_is_an_advisory_failure_and_nothing_runs_against_it(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py", ci.CI_TOOLS, ci.BANK_REQUIREMENTS)
    (root / ci.BANK_REQUIREMENTS).write_text("numpy==2.5.4\n", encoding="utf-8")
    fake = Fake().on("--target", code=1, out="ERROR: No matching distribution found for numpy==2.5.4\n") \
        .on("examples/invalid", code=1).on("--collect-only", out="7 tests collected in 0.01s\n") \
        .on("coverage report", out="87\n")
    outcome = ci.run_job(context(root, fake, install=True, modules=("coverage",)), ci.BY_KEY["test"])
    assert outcome.status == ci.PASSED and outcome.parts_failed_advisory == ["test/install"]
    assert not fake.ran(ci.NUMPY_TEST) and "test/numpy" in outcome.parts_not_run
    assert len(fake.ran("coverage run")) == 4                  # the steps after it still run


def test_the_numpy_pin_is_read_from_the_bank_tools_list_and_the_test_it_names_exists():
    assert ci.numpy_pin("numpyx==1\n# numpy==9\n  numpy==2.5.4  # the builder's\n") == "numpy==2.5.4"
    assert ci.numpy_pin("numpy>=2\nscipy==1.16.0\n") is None and ci.numpy_pin("") is None
    assert ci.numpy_pin((REPO / ci.BANK_REQUIREMENTS).read_text(encoding="utf-8")) is not None
    path, _, name = ci.NUMPY_TEST.partition("::")
    assert f"\ndef {name}(" in (REPO / path).read_text(encoding="utf-8")


# --------------------------------------------------------- the no-extras job


def test_the_laptop_shadows_the_five_optional_modules_outside_the_tree(tmp_path):
    root, fake = make_tree(tmp_path), Fake()
    before = snapshot(root)
    ctx = context(root, fake, modules=("httpx", "websockets"))
    outcome = ci.run_job(ctx, ci.BY_KEY["bare"])
    shadow = Path(fake.calls[0][2]["PYTHONPATH"])
    assert outcome.status == ci.PASSED and fake.lines[0].endswith("tools/ci.py --probe shadowed")
    assert shadow.parent.parent == tmp_path / "temp"
    assert sorted(p.name for p in shadow.iterdir()) == sorted(f"{m}.py" for m in ci.OPTIONAL_MODULES)
    suites = [(cwd.name, env.get("PYTHONPATH")) for line, cwd, env in fake.calls if "pytest" in line]
    # Stream B1's F4: the tools' tests follow the four, with the same five absent.
    assert suites == [*((package, str(shadow)) for package in ci.PACKAGES), ("emet", str(shadow))]
    assert fake.ran("pytest")[-1].endswith("-m pytest -q -p no:cacheprovider tools/tests")
    assert snapshot(root) == before
    ctx.close()
    assert list((tmp_path / "temp").iterdir()) == []


def test_the_bare_job_runs_the_tools_tests_on_3_13_only_as_the_test_job_does(tmp_path):
    root = make_tree(tmp_path)
    for version, runs in (((3, 11), False), ((3, 12), False), ((3, 13), True)):
        fake = Fake()
        outcome = ci.run_job(context(root, fake, version=version, modules=("httpx",)), ci.BY_KEY["bare"])
        assert outcome.status == ci.PASSED and len(fake.ran("pytest")) == (5 if runs else 4)
        assert bool(fake.ran("tools/tests")) is runs
    failing = Fake().on("tools/tests", code=1)
    outcome = ci.run_job(context(root, failing, modules=("httpx",)), ci.BY_KEY["bare"])
    assert outcome.status == ci.FAILED and outcome.reason == "tools: exit 1"


def test_a_shadowed_module_that_still_imports_stops_the_job(tmp_path):
    root, fake = make_tree(tmp_path), Fake().on("--probe shadowed", code=1, out="httpx: STILL IMPORTS\n")
    outcome = ci.run_job(context(root, fake, modules=("httpx",)), ci.BY_KEY["bare"])
    assert outcome.status == ci.FAILED and not fake.ran("pytest")


def test_githubs_no_extras_job_shadows_nothing_and_refuses_a_leak(tmp_path):
    root, fake = make_tree(tmp_path), Fake()
    outcome = ci.run_job(context(root, fake, install=True), ci.BY_KEY["bare"])
    assert outcome.status == ci.PASSED and not fake.ran("--probe")
    assert [line.split("-e ")[1] for line in fake.ran("pip install")] == [
        "emet-sdk[dev]", "emet-hal[dev]", "emet-providers[dev]", "emet-engine[dev]"]
    assert all("PYTHONPATH" not in env for _, _, env in fake.calls)
    leaky = context(root, Fake(), install=True, modules=("sounddevice",))
    leaked = ci.run_job(leaky, ci.BY_KEY["bare"])
    assert leaked.status == ci.FAILED and "sounddevice" in leaked.reason
    assert "optional extras leaked into the bare job: sounddevice\n" in leaky.out.text


def test_the_shadow_makes_each_module_fail_to_import_in_a_real_process(tmp_path):
    for module in ci.OPTIONAL_MODULES:
        (tmp_path / f"{module}.py").write_text(ci.shadow_source(module), encoding="utf-8")
    done = subprocess.run([sys.executable, str(CI_PY), "--probe", "shadowed"], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(tmp_path)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("absent (shadowed for the no-extras run") == 5


# ---------------------------------------------------------------------- dco


def dco_fake(messages: dict[str, str]) -> Fake:
    fake = Fake().on("git rev-list", out="\n".join(messages) + "\n")
    for sha, message in messages.items():
        fake.on(f"--format=%B {sha}", out=message)
    return fake


def test_dco_passes_when_every_commit_is_signed_off_in_any_case(tmp_path):
    fake = dco_fake({"a1": "Add it\n\nSigned-off-by: A Person <a@example.com>\n",
                     "b2": "Fix it\n\nsigned-off-by: B Person <b@example.com>\n"})
    ctx = context(make_tree(tmp_path), fake)
    assert ci.run_job(ctx, ci.BY_KEY["dco"]).status == ci.PASSED
    assert fake.lines[0] == "git rev-list origin/master..HEAD"
    assert "all commits signed off (2 in origin/master..HEAD)" in ctx.out.text


def test_dco_names_each_commit_without_a_sign_off(tmp_path):
    fake = dco_fake({"a1": "Add it\n\nSigned-off-by: A Person <a@example.com>\n", "b2": "Fix it\n",
                     "c3": "Mention it\n\nSigned-off-by: nobody\n"})
    ctx = context(make_tree(tmp_path), fake, env={"BASE_SHA": "base0", "HEAD_SHA": "head9", "GITHUB_ACTIONS": "1"})
    outcome = ci.run_job(ctx, ci.BY_KEY["dco"])
    assert fake.lines[0] == "git rev-list base0..head9"
    assert outcome.status == ci.FAILED and "2 of 3 commits" in outcome.reason
    assert "missing sign-off: b2  Fix it" in ctx.out.text and "missing sign-off: c3  Mention it" in ctx.out.text
    assert "git commit -s" in ctx.out.text and "a1  " not in ctx.out.text


def test_dco_without_its_range_fails_on_github_and_is_not_run_without_history(tmp_path):
    root = make_tree(tmp_path)
    on_github = ci.run_job(context(root, Fake(), env={"GITHUB_ACTIONS": "true"}), ci.BY_KEY["dco"])
    assert on_github.status == ci.FAILED and "BASE_SHA and HEAD_SHA are not set" in on_github.reason
    no_history = ci.run_job(context(root, Fake().on("git rev-list", code=128, out="fatal: not a git repository\n")),
                            ci.BY_KEY["dco"])
    assert no_history.status == ci.NOT_RUN and "fatal: not a git repository" in no_history.reason


#: Lines that carry an attribution, each put together from parts so this
#: file holds none whole (D10 (b)'s three patterns).
CO_AUTHOR = "Co-" + "Authored-By: A Helper <helper" + "@" + "example.com>"
TOOL_LINE = "Gener" + "ated with A Tool"
VENDOR_ADDRESS = "Thanks to noreply" + "@" + "anthropic.com"
SIGNED = "\n\nSigned-off-by: A Person <a@example.com>\n"


@pytest.mark.parametrize("line", [CO_AUTHOR, CO_AUTHOR.lower(), CO_AUTHOR.upper(), TOOL_LINE,
                                  "  " + TOOL_LINE.lower() + " today", VENDOR_ADDRESS])
def test_dco_refuses_a_commit_carrying_an_attribution_line(tmp_path, line):
    fake = dco_fake({"a1b2c3d4e5": "Add it" + SIGNED, "f6a7b8c9d0": f"Fix it\n\n{line}" + SIGNED})
    ctx = context(make_tree(tmp_path), fake, env={"BASE_SHA": "base0", "HEAD_SHA": "head9", "GITHUB_ACTIONS": "1"})
    result = ci.run_job(ctx, ci.BY_KEY["dco"])
    assert result.status == ci.FAILED
    assert [s.outcome.status for s in result.steps] == [ci.PASSED, ci.FAILED], "signed off, and carrying the line"
    assert result.steps[1].outcome.reason == "1 of 2 commits carry an attribution line"
    assert (f'commit f6a7b8c "Fix it" carries an attribution line: {line.strip()}. Emet\'s commits carry no '
            "co-author or tool line; the sign-off names the author. Rewrite the message and push again.") in ctx.out.text
    assert "a1b2c3d" not in ctx.out.text


def test_dco_passes_a_branch_with_no_attribution_line_and_names_its_range(tmp_path):
    fake = dco_fake({"a1": "Add it" + SIGNED, "b2": "Mention co-authors in CONTRIBUTING" + SIGNED})
    ctx = context(make_tree(tmp_path), fake)
    result = ci.run_job(ctx, ci.BY_KEY["dco"])
    assert result.status == ci.PASSED
    assert [s.step.key for s in result.steps] == ["sign-off", "attribution"]
    assert "no commit carries an attribution line (2 in origin/master..HEAD)" in ctx.out.text


def test_dco_reads_attribution_lines_even_when_a_sign_off_is_missing(tmp_path):
    fake = dco_fake({"a1": "Add it\n\n" + CO_AUTHOR + "\n"})
    ctx = context(make_tree(tmp_path), fake)
    result = ci.run_job(ctx, ci.BY_KEY["dco"])
    assert [s.outcome.status for s in result.steps] == [ci.FAILED, ci.FAILED]


# ------------------------------------------------ the pull request's body (D10 (c))


def test_house_style_reads_the_releases_pull_request_body_beside_the_repository(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake()
    ctx = context(root, fake, on_path=("zizmor", "flake8"))
    ci.run_job(ctx, ci.BY_KEY["layering"])
    assert fake.ran("check_style.py") == [f"{PY} tools/check_style.py ."], "no body, no --file"
    body = tmp_path / "releases" / "v0.5.6" / "pr-body.md"
    body.parent.mkdir(parents=True)
    body.write_text("A plain body.\n", encoding="utf-8")
    fake = Fake()
    ci.run_job(context(root, fake, on_path=("zizmor", "flake8")), ci.BY_KEY["layering"])
    assert fake.ran("check_style.py") == [f"{PY} tools/check_style.py . --file "
                                          f"{os.path.join('TMP', 'releases', 'v0.5.6', 'pr-body.md')}"]


# ------------------------------------------------------------- flake8 (D14)


def test_flake8_runs_the_four_codes_over_the_packages_and_tools_and_blocks_layering(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake().on("flake8", code=1, out="emet-sdk/x.py:1:1: F401 'os' imported but unused\n")
    ctx = context(root, fake, on_path=("zizmor", "flake8"))
    result = ci.run_job(ctx, ci.BY_KEY["layering"])
    (line,) = fake.ran("flake8")
    assert line == ("/usr/bin/flake8 --select=E402,E731,F401,F821 --extend-exclude=.venv,mutants "
                    "emet-sdk emet-hal emet-providers emet-engine tools")
    step = next(r for r in result.steps if r.step.key == "flake8")
    assert ci.FLAKE8_BLOCKS is True, "A12's sweep fixed the 8 sites and set it"
    assert step.outcome.status == ci.FAILED and step.step.advisory is False
    assert result.status == ci.FAILED
    assert result.steps[-1].step.key == "flake8"


def test_flake8_is_found_in_the_interpreter_first(tmp_path):
    fake = Fake()
    ctx = context(make_tree(tmp_path), fake, modules=("flake8",), on_path=("zizmor",))
    ci.run_job(ctx, ci.BY_KEY["layering"])
    assert fake.ran("flake8")[0].startswith(f"{PY} -m flake8 --select=")


def test_flake8_only_warns_with_the_switch_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "FLAKE8_BLOCKS", False)
    fake = Fake().on("flake8", code=1)
    ctx = context(make_tree(tmp_path), fake, on_path=("zizmor", "flake8"))
    result = ci.run_job(ctx, ci.BY_KEY["layering"])
    step = next(r for r in result.steps if r.step.key == "flake8")
    assert step.outcome.status == ci.FAILED and step.step.advisory is True
    assert result.status == ci.PASSED


FLAKE8_SITE = "import os\n\n\ndef f():\n    return 1\n"


def test_real_flake8_fails_an_unused_import_and_passes_the_same_under_a_venv(tmp_path):
    """D14's two breaks against the real tool: a module with an unused
    import fails the step, and the same module under a `.venv` folder of a
    scanned path, as `emet-sdk/.venv` is, does not. It needs flake8 in the
    interpreter, which the CI lock installs."""
    pytest.importorskip("flake8")
    root = tmp_path / "emet"
    for package in (*ci.PACKAGES, "tools"):
        (root / package).mkdir(parents=True)
    ctx = ci.Context(root=root, env=dict(os.environ), out=Said(), has_module=lambda name: name == "flake8")
    (root / "emet-sdk" / ".venv" / "Lib").mkdir(parents=True)
    (root / "emet-sdk" / ".venv" / "Lib" / "site.py").write_text(FLAKE8_SITE, encoding="utf-8")
    (root / "tools" / "mutants").mkdir()
    (root / "tools" / "mutants" / "copied.py").write_text(FLAKE8_SITE, encoding="utf-8")
    assert ci.flake8(ctx).status == ci.PASSED, ctx.out.text
    (root / "emet-sdk" / "unused.py").write_text(FLAKE8_SITE, encoding="utf-8")
    assert ci.flake8(ctx).status == ci.FAILED
    assert "unused.py:1:1: F401 'os' imported but unused" in ctx.out.text.replace("\\", "/")
    found = [line for line in ctx.out.text.splitlines() if "F401" in line and not line.startswith("+ ")]
    assert len(found) == 1 and ".venv" not in found[0] and "mutants" not in found[0], found


# ------------------------------------------------- the extras' licences (L20)


def test_the_extras_go_in_a_venv_of_their_own_and_their_licences_are_held(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake().on("examples/invalid", code=1)
    ctx = context(root, fake, install=True, os_name="linux")
    result = ci.run_job(ctx, ci.BY_KEY["test"])
    keys = [r.step.key for r in result.steps]
    assert keys.index("extras-install") < keys.index("extras-licences") < keys.index("numpy")
    (made,) = [line for line in fake.lines if "-m venv" in line and "extras-venv" in line]
    venv_python = os.path.join(made.split()[-1], "bin", "python")
    (install,) = [line for line in fake.lines if line.startswith(venv_python) and "pip install" in line]
    assert install.endswith("pip install --disable-pip-version-check ./emet-sdk ./emet-hal[audio] "
                            "./emet-providers[piper]")
    (gate,) = [line for line in fake.lines if line.startswith(venv_python) and "check_licences.py" in line]
    assert gate == f"{venv_python} {os.path.join(TREE, 'tools', 'check_licences.py')}"
    steps = {r.step.key: r for r in result.steps}
    assert not steps["extras-install"].step.advisory and not steps["extras-licences"].step.advisory
    assert steps["extras-licences"].outcome.status == ci.PASSED


def test_a_licence_the_extras_bring_that_is_refused_fails_the_test_job(tmp_path):
    root = make_tree(tmp_path, "tools/check_licences.py")
    fake = Fake().on("examples/invalid", code=1)
    fake.rules.insert(0, (os.path.join("extras-venv", "bin", "python") + " " + TREE, 1, "", None, None))
    ctx = context(root, fake, install=True, os_name="linux")
    result = ci.run_job(ctx, ci.BY_KEY["test"])
    steps = {r.step.key: r.outcome for r in result.steps}
    assert steps["extras-licences"].status == ci.FAILED
    assert result.status == ci.FAILED


def test_the_extras_are_gated_on_313_alone(tmp_path):
    for version in ((3, 11), (3, 12)):
        ctx = context(make_tree(tmp_path / f"v{version[1]}"), Fake(), version=version)
        assert "extras-licences" not in [s.key for s in ci.test_steps(ctx)]


def test_dco_on_github_without_the_history_of_its_range_fails(tmp_path):
    """A shallow checkout on GitHub must turn the required `dco sign-off` red."""
    fake = Fake().on("git rev-list", code=128, out="fatal: bad revision 'b..h'\n")
    ctx = context(make_tree(tmp_path), fake, env={"GITHUB_ACTIONS": "true", "BASE_SHA": "b", "HEAD_SHA": "h"})
    assert ci.run_jobs(ctx, ["dco"]) == 1
    assert last_line(ctx) == "ci: FAILED (dco)" and "all commits signed off" not in ctx.out.text
    assert "needs the history of b..h: fatal: bad revision 'b..h'" in ctx.out.text


# -------------------------------------------------------------------- wheel


def test_the_dry_run_refuses_an_emet_name_that_the_index_would_supply():
    old_floor = report(("emet-providers", "0.5.6", LOCAL + "emet_providers-0.5.6-py3-none-any.whl"),
                       ("emet-sdk", "0.1.3", INDEX + "emet_sdk-0.1.3-py3-none-any.whl"),
                       ("opentelemetry-api", "1.45.2", INDEX + "opentelemetry_api-1.45.2-py3-none-any.whl"))
    ok, line = ci.index_verdict(0, "", old_floor, alone=True)
    assert not ok
    assert line == ("an Emet name resolves from the index: emet-sdk 0.1.3 would come from "
                    + INDEX + "emet_sdk-0.1.3-py3-none-any.whl")
    spelled = report(("Emet_SDK", "9.0", INDEX + "x.whl"))
    assert ci.index_verdict(0, "", spelled, alone=False)[0] is False
    assert ci.index_verdict(0, "", report(("emet", "1.0", INDEX + "emet-1.0.tar.gz")), alone=True)[0] is False


def test_the_dry_run_passes_when_the_index_has_nothing_for_the_floor():
    ok, line = ci.index_verdict(1, NO_MATCH, "", alone=True)
    assert ok and line == ("the index holds nothing that satisfies emet-sdk "
                           "(the index offers 0.1.0, 0.1.1, 0.1.2, 0.1.3)")
    sdk_alone = report(("emet-sdk", "0.5.6", LOCAL + "emet_sdk-0.5.6-py3-none-any.whl"),
                       ("jsonschema", "4.26.0", INDEX + "jsonschema-4.26.0-py3-none-any.whl"),
                       ("emetic", "1.0", INDEX + "emetic-1.0-py3-none-any.whl"))
    assert ci.index_verdict(0, "", sdk_alone, alone=True) == (
        True, "resolves, 3 distributions, every Emet name from a local file")


def test_the_dry_run_decides_nothing_it_did_not_see():
    # The four together must resolve: the one way a wheel alone may fail is no pass here.
    assert ci.index_verdict(1, NO_MATCH, "", alone=False)[0] is False
    unreachable = ("WARNING: Retrying (Retry(total=4, connect=None)) after connection broken by "
                   "'NewConnectionError': /simple/emet-sdk/\n"
                   "ERROR: Could not find a version that satisfies the requirement emet-sdk>=0.5.6 (from versions: none)\n"
                   "ERROR: No matching distribution found for emet-sdk>=0.5.6\n")
    assert ci.index_verdict(1, unreachable, "", alone=True) == (
        False, "pip could not reach the index, so nothing was decided")
    other = "ERROR: No matching distribution found for jsonschema>=99\n"
    assert ci.index_verdict(1, other, "", alone=True) == (False, "pip failed: " + other.strip())
    assert ci.index_verdict(0, "", "", alone=True)[0] is False
    assert ci.index_verdict(0, "", '{"install": 3}', alone=True)[0] is False


def test_two_builds_are_compared_by_name_and_hash():
    first = {"emet_sdk-0.5.6-py3-none-any.whl": "a" * 64, "emet_hal-0.5.6-py3-none-any.whl": "b" * 64}
    assert ci.compare_builds(first, dict(first)) == []
    second = {**first, "emet_hal-0.5.6-py3-none-any.whl": "c" * 64}
    assert ci.compare_builds(first, second) == [
        "emet_hal-0.5.6-py3-none-any.whl: sha256 bbbbbbbbbbbb the first time, cccccccccccc the second"]
    one = {"emet_sdk-0.5.6-py3-none-any.whl": "a" * 64}
    assert ci.compare_builds(first, one) == ["emet_hal-0.5.6-py3-none-any.whl: built the first time and not the second"]
    assert ci.compare_builds(one, first) == ["emet_hal-0.5.6-py3-none-any.whl: built the second time and not the first"]
    assert ci.compare_builds({}, {}) == ["there is no wheel to compare"]


def test_the_sdk_wheel_must_carry_its_data_files():
    assert ci.missing_wheel_data(set(ci.WHEEL_DATA)) == []
    assert ci.missing_wheel_data(set(ci.WHEEL_DATA) - {"emet_sdk/chains/core.yaml"}) == ["emet_sdk/chains/core.yaml"]


def test_every_file_the_sdk_ships_beside_its_code_is_one_the_wheel_job_looks_for():
    """Review RB200: `py.typed` was in the package and not in the list, so a
    wheel without it passed. Each schema `force-include` moves inside the
    package and each file of the package that is not Python is named, and
    every name is a file of the source tree."""
    sdk = REPO / "emet-sdk"
    shipped = {f"emet_sdk/schemas/{path.name}" for path in (sdk / "schemas").glob("*.json")}
    shipped |= {path.relative_to(sdk).as_posix() for path in (sdk / "emet_sdk").rglob("*")
                if path.is_file() and path.suffix not in (".py", ".pyc") and "__pycache__" not in path.parts}
    assert "emet_sdk/py.typed" in shipped and shipped == set(ci.WHEEL_DATA)


def test_first_party_names_and_a_wheels_requirements(tmp_path):
    assert all(ci.first_party(name) for name in ("emet", "emet-sdk", "Emet_SDK", "emet.hal"))
    assert not any(ci.first_party(name) for name in ("emetic", "jsonschema", "my-emet"))
    wheel = tmp_path / "emet_hal-0.5.6-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("emet_hal-0.5.6.dist-info/METADATA",
                         "Name: emet-hal\nRequires-Dist: emet-sdk>=0.5.6\n"
                         "Requires-Dist: sounddevice>=0.4; extra == 'audio'\n\nRequires-Dist: not-a-header\n")
    assert ci.wheel_requirements(wheel) == ["emet-sdk>=0.5.6", "sounddevice>=0.4"]
    assert ci.first_party_requirements(ci.wheel_requirements(wheel)) == ["emet-sdk>=0.5.6"]


def builder(vary: bool = False, floor: str = "emet-sdk>=0.5.6", drop: str = "") -> Callable[[list[str]], None]:
    """Stands in for `python -m build`: a small wheel in the `--outdir`.
    `drop` leaves that data file out of the SDK's wheel, as a typo in its
    `force-include` would."""
    count = {"n": 0}

    def build(argv: list[str]) -> None:
        count["n"] += 1
        package, out = Path(argv[-1]).name, Path(argv[argv.index("--outdir") + 1])
        name = package.replace("-", "_")
        with zipfile.ZipFile(out / f"{name}-0.5.6-py3-none-any.whl", "w") as archive:
            requires = "" if package == "emet-sdk" else f"Requires-Dist: {floor}\n"
            stamp = f"Build: {count['n']}\n" if vary else ""
            meta = zipfile.ZipInfo(f"{name}-0.5.6.dist-info/METADATA", date_time=(2020, 2, 2, 0, 0, 0))
            archive.writestr(meta, f"Name: {package}\n{requires}{stamp}\n")
            data_files = {"emet-sdk": ci.WHEEL_DATA, "emet-engine": ci.ENGINE_WHEEL_DATA}.get(package, ())
            for data in data_files:
                if data != drop:
                    archive.writestr(zipfile.ZipInfo(data, date_time=(2020, 2, 2, 0, 0, 0)), "{}")

    return build


def pip_dry_run(old_floor: bool = False) -> Callable[[list[str]], Any]:
    """Stands in for pip's dry run against the index, as pip 24.3.1 answered
    on 2026-11-26: a dependent wheel alone finds no `emet-sdk` at the floor,
    or, with the old floor, takes the other project's 0.1.3."""

    def dry_run(argv: list[str]) -> Any:
        wheels = [Path(a).name for a in argv if a.endswith(".whl")]
        items = [(w.split("-")[0].replace("_", "-"), "0.5.6", LOCAL + w) for w in wheels]
        if len(wheels) == 1 and not wheels[0].startswith("emet_sdk"):
            if not old_floor:
                return ci.Done(1, NO_MATCH)
            items.append(("emet-sdk", "0.1.3", INDEX + "emet_sdk-0.1.3-py3-none-any.whl"))
        Path(argv[argv.index("--report") + 1]).write_text(report(*items), encoding="utf-8")
        return None

    return dry_run


def test_the_wheel_job_builds_twice_checks_the_index_and_installs_outside_the_tree(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder()).on("--report", do=pip_dry_run()).on("git log -1", out="1795684154\n")
    before = snapshot(root)
    ctx = context(root, fake, modules=("build",))
    outcome = ci.run_job(ctx, ci.BY_KEY["wheel"])
    assert outcome.status == ci.PASSED, ctx.out.text
    builds = [(line, env) for line, _, env in fake.calls if "-m build" in line]
    assert len(builds) == 8 and all(env["SOURCE_DATE_EPOCH"] == "1795684154" for _, env in builds)
    outdirs = {line.split("--outdir ")[1].split()[0] for line, _ in builds}
    assert len(outdirs) == 2 and all(out.startswith(TEMP) for out in outdirs)
    assert "both builds gave the same bytes" in ctx.out.text
    assert "ok  emet-sdk>=0.5.6 (asked by emet-hal, emet-providers, emet-engine): a wheel built here" in ctx.out.text
    assert ctx.out.text.count("the index holds nothing that satisfies emet-sdk") == 3
    assert "ok  the four together: resolves, 4 distributions, every Emet name from a local file" in ctx.out.text
    alone = [line for line in fake.ran("--report") if line.count(".whl") == 1]
    assert len(alone) == 4 and not any("--find-links" in line or "--no-index" in line for line in alone)
    install = next(line for line in fake.lines if "pip install" in line and "--dry-run" not in line)
    assert install.count(".whl") == 4 and install.startswith(TEMP)
    outside = [cwd for line, cwd, _ in fake.calls if "--version" in line or "--probe discovery" in line]
    assert len(outside) == 2 and all(root not in cwd.parents and cwd != root for cwd in outside)
    assert snapshot(root) == before


def test_the_wheel_job_fails_when_the_sdk_wheel_lacks_a_data_file(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder(drop="emet_sdk/chains/core.yaml")).on("--report", do=pip_dry_run())
    ctx = context(root, fake, modules=("build",))
    outcome = ci.run_job(ctx, ci.BY_KEY["wheel"])
    assert outcome.status == ci.FAILED and outcome.reason == "data: 1 data file(s) missing from the SDK's wheel"
    assert "  - emet_sdk/chains/core.yaml" in ctx.out.text and not fake.ran("-m venv")


def test_the_wheel_job_fails_when_the_engine_wheel_lacks_the_events_schema(tmp_path):
    """Finding A5-4 of 0.5.7: the data check read the SDK's wheel alone, so
    nothing held that the `--events` schema ships in the engine's."""
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder(drop="emet_engine/schemas/events-1.schema.json")) \
        .on("--report", do=pip_dry_run())
    ctx = context(root, fake, modules=("build",))
    outcome = ci.run_job(ctx, ci.BY_KEY["wheel"])
    assert outcome.status == ci.FAILED
    assert outcome.reason == ("data: emet_engine-0.5.6-py3-none-any.whl is missing "
                              "emet_engine/schemas/events-1.schema.json")
    assert not fake.ran("-m venv")


def test_the_engine_wheel_data_is_every_file_the_engine_ships_beside_its_code():
    engine = REPO / "emet-engine" / "emet_engine"
    shipped = {path.relative_to(engine.parent).as_posix() for path in engine.rglob("*")
               if path.is_file() and path.suffix not in (".py", ".pyc") and "__pycache__" not in path.parts}
    assert shipped == set(ci.ENGINE_WHEEL_DATA)


def test_the_wheel_job_fails_when_the_installed_cli_will_not_start_outside_the_tree(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder()).on("--report", do=pip_dry_run()).on("--version", code=1)
    outcome = ci.run_job(context(root, fake, modules=("build",)), ci.BY_KEY["wheel"])
    assert outcome.status == ci.FAILED and outcome.reason == "cli: emet --version exited 1"
    assert not fake.ran("--probe discovery")


def test_the_wheel_job_fails_when_the_second_build_differs(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder(vary=True)).on("--report", do=pip_dry_run())
    ctx = context(root, fake, modules=("build",))
    outcome = ci.run_job(ctx, ci.BY_KEY["wheel"])
    assert outcome.status == ci.FAILED and "rebuild: 4 wheel(s) differ" in outcome.reason
    assert not fake.ran("-m venv") and not fake.ran("pip install")


def test_the_wheel_job_fails_on_the_old_floor_with_no_sibling_offered(tmp_path):
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder(floor="emet-sdk>=0.1")).on("--report", do=pip_dry_run(old_floor=True))
    ctx = context(root, fake, modules=("build",))
    outcome = ci.run_job(ctx, ci.BY_KEY["wheel"])
    assert outcome.status == ci.FAILED and outcome.reason == "index: 3 resolution(s) wrong"
    assert ctx.out.text.count("an Emet name resolves from the index: emet-sdk 0.1.3 would come from") == 3
    assert not any("pip install" in line and "--dry-run" not in line for line in fake.lines)


def test_the_wheel_job_fails_when_no_wheel_built_here_meets_an_emet_requirement(tmp_path):
    """A floor above the release: the wheels built here cannot meet it with
    the index out of reach, and the job says which requirement asked."""
    root = make_tree(tmp_path)
    fake = Fake().on("-m build", do=builder(floor="emet-sdk>=0.5.7")) \
        .on("--no-index", code=1, out="ERROR: No matching distribution found for emet-sdk>=0.5.7\n") \
        .on("--report", do=pip_dry_run())
    ctx = context(root, fake, modules=("build",))
    outcome = ci.run_job(ctx, ci.BY_KEY["wheel"])
    assert outcome.status == ci.FAILED and outcome.reason == "index: 1 resolution(s) wrong"
    assert ("x   emet-sdk>=0.5.7 (asked by emet-hal, emet-providers, emet-engine): no wheel built here "
            "satisfies it") in ctx.out.text
    offline = fake.ran("--no-index")
    assert len(offline) == 1 and f"--no-deps --find-links {TEMP}" in offline[0]
    assert not any("pip install" in line and "--dry-run" not in line for line in fake.lines)


def test_a_build_that_writes_no_wheel_fails_and_no_build_tool_is_not_run(tmp_path):
    root = make_tree(tmp_path)
    empty = ci.run_job(context(root, Fake(), modules=("build",)), ci.BY_KEY["wheel"])
    assert empty.status == ci.FAILED and "the build wrote no wheel for emet-sdk" in empty.reason
    fake = Fake()
    none = ci.run_job(context(root, fake), ci.BY_KEY["wheel"])
    assert (none.status, none.reason) == (ci.NOT_RUN, "needs build") and fake.lines == []


def test_on_github_the_wheel_job_installs_the_hashed_tools_first(tmp_path):
    root = make_tree(tmp_path, ci.CI_TOOLS)
    fake = Fake().on("-m build", do=builder()).on("--report", do=pip_dry_run())
    ctx = context(root, fake, modules=("build",), install=True, os_name="linux")
    assert ci.run_job(ctx, ci.BY_KEY["wheel"]).status == ci.PASSED
    assert fake.lines[0].endswith("-m pip install --require-hashes -r tools/ci/requirements.txt")
    missing = context(make_tree(tmp_path / "other"), Fake(), install=True, env={"GITHUB_ACTIONS": "true"})
    refused = ci.run_job(missing, ci.BY_KEY["wheel"])
    assert refused.status == ci.FAILED and "tools/ci/requirements.txt is missing" in refused.reason


# ------------------------------------------------------- --all and the tree


def test_all_leaves_no_file_behind_and_ends_on_the_verdict(tmp_path):
    root = make_tree(tmp_path, "tools/check_health.py", "tools/check_licences.py", "tools/check_docs.py",
                     "tools/api_diff.py")
    fake = Fake().on("-m build", do=builder()).on("--report", do=pip_dry_run()).on("examples/invalid", code=1) \
        .on("coverage report", out="91\n").on("--collect-only", out="12 tests collected in 0.10s\n")
    before = snapshot(root)
    ctx = context(root, fake, modules=("build", "coverage", "httpx", "pocketsphinx"),
                  on_path=("gh", "zizmor", "flake8"))
    assert ci.run_jobs(ctx, list(ci.BY_KEY)) == 0
    assert snapshot(root) == before
    assert list((tmp_path / "temp").iterdir()) == []
    assert last_line(ctx) == ("ci: all green, 8 jobs, 9 not run here (test/extras-licences, test/numpy, "
                              "test/tools-licences, arm64, sound, mutmut, pip-audit, bank, bank-full)")
    # Contract §5's lines for the scripts other streams deliver.
    assert fake.ran("api_diff.py") == [f"{PY} tools/api_diff.py --base origin/master"]
    assert fake.ran("check_health.py") == [f"{PY} tools/check_health.py ."]
    assert fake.ran("check_docs.py") == [f"{PY} tools/check_docs.py ."]
    docs_env = next(env for line, _, env in fake.calls if "check_docs.py" in line)
    assert docs_env["PATH"].split(os.pathsep)[0] == str(tmp_path / "py")      # the documents name `emet` bare
    written_to = [line.split("--outdir ")[1].split()[0] for line in fake.ran("--outdir")]
    assert len(written_to) == 8 and all(out.startswith(TEMP) and TREE not in out for out in written_to)
    # Stream B1's F4: the two things `local_ci_055.sh` did that `--all` left out.
    assert ("tests collected: emet-sdk 12, emet-hal 12, emet-providers 12, emet-engine 12, tools/tests 12; "
            "48 in the four packages") in ctx.out.text
    tools_runs = [env.get("PYTHONPATH", "") for line, _, env in fake.calls
                  if line.endswith(" tools/tests") and "--collect-only" not in line]
    assert len(tools_runs) == 2 and tools_runs[0] == "" and tools_runs[1].startswith(str(tmp_path / "temp"))


def test_log_keeps_everything_printed_in_a_folder_it_makes(tmp_path, capsys):
    """A new release's `releases/vX.Y.Z/ci/` does not exist until the first run."""
    log = tmp_path / "releases" / "v9.9.9" / "ci" / "ci.log"
    out = ci.Output(log)
    ctx = context(make_tree(tmp_path), Fake().on("check_layering.py", out="layering: ok\n"),
                  on_path=("zizmor", "flake8"))
    ctx.out = out
    try:
        ci.run_jobs(ctx, ["layering"])
    finally:
        out.close()
    printed, logged = capsys.readouterr().out, log.read_text(encoding="utf-8")
    assert logged == printed and "layering: ok" in logged
    assert logged.rstrip().splitlines()[-1].startswith("ci: all green, 1 job, 1 not run here (layering/health)")


def test_run_command_returns_the_exit_code_and_both_streams():
    heard: list[str] = []
    done = ci.run_command([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
                          echo=heard.append)
    assert done.code == 3 and "out" in done.out and "err" in done.out and "".join(heard) == done.out
    assert ci.run_command(["no-such-command-emet-ci"]).code == 127


def test_main_hands_install_to_the_jobs(monkeypatch, capsys):
    seen: dict[str, Any] = {}

    def run_jobs(ctx, keys):
        seen.update(install=ctx.install, keys=keys)
        return 0

    monkeypatch.setattr(ci, "run_jobs", run_jobs)
    assert ci.main(["layering", "--install"]) == 0 and seen == {"install": True, "keys": ["layering"]}
    assert ci.main(["layering"]) == 0 and seen["install"] is False


def test_main_leaves_the_variables_that_change_a_test_run_out_of_every_job(monkeypatch, capsys):
    """Review RB275. Exported on the laptop, EMET_UPDATE_SNAPSHOTS would
    rewrite the snapshots a run should compare, a seed variable would run
    other seeds, and EMET_LIVE_TESTS would call a vendor."""
    seen: dict[str, str] = {}

    def run_jobs(ctx, keys):
        seen.clear()
        seen.update(ctx.env)
        return 0

    monkeypatch.setattr(ci, "run_jobs", run_jobs)
    assert set(ci.RUN_CHANGING) == {"EMET_UPDATE_SNAPSHOTS", "EMET_PROPERTY_SEED", "EMET_PROPERTY_SEEDS",
                                    "EMET_LIVE_TESTS", "EMET_LIVE_RECORD"}
    for name in ci.RUN_CHANGING:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("EMET_VIRTUAL_AUDIO", "1")
    assert ci.main(["layering"]) == 0
    assert not set(ci.RUN_CHANGING) & set(seen) and seen["EMET_VIRTUAL_AUDIO"] == "1"
    printed = capsys.readouterr().out.splitlines()
    assert printed[1] == ("ci: left out of every job, since each changes what a test run means: "
                          + ", ".join(ci.RUN_CHANGING))
    for name in ci.RUN_CHANGING:
        monkeypatch.delenv(name)
    assert ci.main(["layering"]) == 0 and "left out" not in capsys.readouterr().out
    # Each name is the one a suite reads, so a rename there cannot leave it reaching the jobs.
    readers = "".join(path.read_text(encoding="utf-8") for path in sorted(REPO.glob("emet-*/tests/*.py")))
    assert all(f'"{name}"' in readers for name in ci.RUN_CHANGING)


# --------------------------------------------------------------- time limits


def test_a_command_past_its_time_limit_is_ended_with_every_process_it_started():
    """A child that starts a grandchild, both asleep for a minute. The
    grandchild holds the pipe too, so the call returns only once both are
    ended (on Windows a venv's python.exe is a launcher, so even the child
    is two processes)."""
    script = ("import subprocess, sys, time\n"
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "print('started', flush=True)\n"
              "time.sleep(60)\n")
    began = time.monotonic()
    done = ci.run_command([sys.executable, "-c", script], timeout=1)
    took = time.monotonic() - began
    assert done.timed_out and "started" in done.out and "ci: ended after 1 s, its time limit" in done.out
    assert took < 20, took
    quick = ci.run_command([sys.executable, "-c", "print('done')"], timeout=30)
    assert (quick.code, quick.timed_out, quick.out) == (0, False, "done\n")


def workflow_minutes() -> dict[str, int]:
    """Each `tools/ci.py` key a workflow job runs, and that job's `timeout-minutes`."""
    yaml = pytest.importorskip("yaml")
    minutes: dict[str, int] = {}
    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        for job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).values():
            for step in job.get("steps", []):
                for match in re.finditer(r"python[0-9.]*\s+tools/ci\.py((?:\s+[^\s#|&;]+)*)", str(step.get("run", ""))):
                    for key in (word for word in match.group(1).split() if not word.startswith("-")):
                        minutes[key] = job.get("timeout-minutes")
    return minutes


def test_each_job_with_an_advisory_part_carries_its_workflows_minutes(tmp_path):
    """A job GitHub ends at `timeout-minutes` does not pass, and the audit
    holds a merge on it. So every job with an advisory part stops that part
    inside its workflow's limit, and carries the workflow's own number."""
    minutes = workflow_minutes()
    if not minutes:
        pytest.skip("no workflow calls tools/ci.py yet")
    ctx = context(make_tree(tmp_path), Fake(), env={"PR": "1"})
    for job in ci.JOBS:
        if job.key not in minutes:
            continue
        advisory_part = not job.blocking or any(step.advisory for step in job.steps(ctx))
        assert (job.minutes is not None) is advisory_part, job.key
        if advisory_part:
            assert job.minutes == minutes[job.key], (job.key, job.minutes, minutes[job.key])
            assert 0 < job.limit <= job.minutes * 60 - ci.WORKFLOW_MARGIN


def mutmut_tree(tmp_path: Path) -> tuple[Path, Fake]:
    root = make_tree(tmp_path, "tools/ci/mutmut-targets.txt", "emet-sdk/emet_sdk/resolve.py")
    (root / "tools/ci/mutmut-targets.txt").write_text("emet-sdk/emet_sdk/resolve.py::floor\n", encoding="utf-8")
    (root / "emet-sdk/emet_sdk/resolve.py").write_text(SOURCE, encoding="utf-8")
    fake = Fake().on("git merge-base", out="8efab3a\n").on("git diff -U0", out=DIFF) \
        .on("git ls-files", out="emet-sdk/emet_sdk/resolve.py\nemet-sdk/pyproject.toml\n")
    return root, fake


def test_on_github_mutmut_ends_inside_its_limit_warns_and_names_what_it_did_not_mutate(tmp_path):
    root, fake = mutmut_tree(tmp_path)
    fake.on("mutmut run", do=lambda argv: ci.Done(-9, "ci: ended after 1620 s, its time limit\n", timed_out=True))
    ctx = context(root, fake, os_name="linux", fork=True, modules=("mutmut",),
                  env={"GITHUB_ACTIONS": "true", "BASE_SHA": "base0"})
    assert ci.run_jobs(ctx, ["mutmut"]) == 0
    limit = ci.BY_KEY["mutmut"].limit
    assert limit == 30 * 60 - ci.WORKFLOW_MARGIN
    assert all(timeout is not None and 0 < timeout <= limit for timeout in fake.timeouts)
    assert fake.ran("mutmut run") and not fake.ran("mutmut results")
    warning = next(line for line in ctx.out.text.splitlines() if line.startswith("::warning title=mutmut (advisory)"))
    assert "out of time" in warning
    assert "1 target function(s) were not mutated: emet-sdk/emet_sdk/resolve.py::floor" in warning
    # Off GitHub nothing is timed.
    root, laptop = mutmut_tree(tmp_path / "laptop")
    ci.run_job(context(root, laptop.on("mutmut results", out=""), os_name="linux", fork=True, modules=("mutmut",)),
               ci.BY_KEY["mutmut"])
    assert laptop.ran("mutmut run") and set(laptop.timeouts) == {None}


def test_after_its_limit_no_command_of_an_advisory_job_starts(tmp_path):
    now = [0.0]

    def slow(argv: list[str]) -> None:
        now[0] += 10_000.0                       # this suite outlasts the job's limit

    fake = Fake().on("-m pytest", do=slow)
    ctx = context(make_tree(tmp_path, "tools/check_licences.py"), fake, os_name="linux", machine="aarch64",
                  env={"GITHUB_ACTIONS": "true"}, clock=lambda: now[0])
    assert ci.run_jobs(ctx, ["arm64"]) == 0
    assert len(fake.ran("-m pytest")) == 1
    assert ctx.out.text.count("ci: not started, the time limit has passed") == 3
    warning = next(line for line in ctx.out.text.splitlines() if line.startswith("::warning title=arm64 (advisory)"))
    assert all(f"{package}: out of time" in warning for package in ci.PACKAGES[1:])


def test_mutmut_results_that_fail_are_no_survivors_read(tmp_path):
    root, fake = mutmut_tree(tmp_path)
    fake.on("mutmut results", code=1, out="Traceback (most recent call last):\nKeyError: 'x'\n")
    outcome = ci.run_job(context(root, fake, os_name="linux", fork=True, modules=("mutmut",)), ci.BY_KEY["mutmut"])
    assert outcome.status == ci.FAILED
    assert outcome.reason == "mutmut: mutmut results exited 1 in emet-sdk, so no survivor was read: KeyError: 'x'"


# ------------------------------------------------------------------ pip-audit


def test_pip_audit_asks_the_index_for_every_extra_and_no_emet_name():
    text = PYPROJECT.format(name="emet-hal", deps='"emet-sdk>=0.5.6", "pyyaml>=6.0.4"')
    assert ci.pip_audit_requirements(text) == ["pyyaml>=6.0.4", "pytest>=9.2.0", "httpx>=0.27",
                                               "certifi>=2026.10.18"]


AUDIT_REPORT = json.dumps({"dependencies": [
    {"name": "certifi", "version": "2026.7.22",
     "vulns": [{"id": "PYSEC-2026-1", "fix_versions": ["2026.10.18"], "aliases": []}]},
    {"name": "pyyaml", "version": "6.0.4", "vulns": []},
    {"name": "odd", "version": "1", "vulns": [{"id": "GHSA-x", "fix_versions": []}]}], "fixes": []})


def test_pip_audit_findings_carry_their_fix():
    assert ci.pip_audit_findings(AUDIT_REPORT) == [
        {"name": "certifi", "version": "2026.7.22", "id": "PYSEC-2026-1", "fix": "2026.10.18"},
        {"name": "odd", "version": "1", "id": "GHSA-x", "fix": "none published"}]


def test_pip_audit_findings_are_an_advisory_failure_and_its_files_stay_outside(tmp_path):
    root = make_tree(tmp_path)

    def audit(argv: list[str]) -> ci.Done:
        Path(argv[argv.index("-o") + 1]).write_text(AUDIT_REPORT, encoding="utf-8")
        return ci.Done(1, "")

    fake, before = Fake().on("pip-audit", do=audit), snapshot(root)
    ctx = context(root, fake, on_path=("pip-audit",))
    assert ci.run_jobs(ctx, ["pip-audit"]) == 0
    assert len(fake.lines) == 4 and all(f"-r {TEMP}" in line and f"-o {TEMP}" in line for line in fake.lines)
    assert "pip-audit: emet-sdk: certifi 2026.7.22 PYSEC-2026-1, fix: 2026.10.18" in ctx.out.text
    assert last_line(ctx) == "ci: all green, 0 jobs, 0 not run here, 1 failed advisory (pip-audit)"
    assert snapshot(root) == before
    silent = context(root, Fake().on("pip-audit", code=1, out="network down\n"), on_path=("pip-audit",))
    assert ci.run_job(silent, ci.BY_KEY["pip-audit"]).status == ci.FAILED


# --------------------------------------------------------------------- mutmut

SOURCE = '''import functools


def untouched():
    return 1


@functools.cache
def floor(level):
    if level > 1:
        return None
    return level


class Gate:
    def refuses(self, row):
        return row.open
'''

DIFF = """diff --git a/emet-sdk/emet_sdk/resolve.py b/emet-sdk/emet_sdk/resolve.py
--- a/emet-sdk/emet_sdk/resolve.py
+++ b/emet-sdk/emet_sdk/resolve.py
@@ -10 +10,2 @@ def floor(level):
-    if level > 2:
+    if level > 1:
+        return None
@@ -30,0 +31 @@ def later():
+    pass
"""


def test_the_targets_file_and_a_diffs_changed_lines():
    text = "# the resolver\nemet-sdk/emet_sdk/resolve.py::floor\n\ntools\\plan_file.py::Gate.refuses  # a method\n"
    assert ci.read_targets(text) == [("emet-sdk/emet_sdk/resolve.py", "floor"), ("tools/plan_file.py", "Gate.refuses")]
    assert ci.changed_lines(DIFF) == {"emet-sdk/emet_sdk/resolve.py": [(10, 11), (31, 31)]}


def test_a_target_is_chosen_when_a_changed_line_falls_inside_its_function():
    assert ci.function_span(SOURCE, "floor") == (8, 12)          # its decorator counts
    assert ci.function_span(SOURCE, "Gate.refuses") == (16, 17)
    assert ci.function_span(SOURCE, "gone") is None
    targets = [("a.py", "untouched"), ("a.py", "floor"), ("a.py", "Gate.refuses"), ("b.py", "floor")]
    chosen = ci.changed_targets(targets, {"a.py": [(10, 11)]}, lambda path: SOURCE)
    assert chosen == [("a.py", "floor")]
    assert ci.changed_targets(targets, {"a.py": [(1, 2)]}, lambda path: SOURCE) == []


def test_mutmuts_names_and_its_results():
    assert ci.mutant_pattern("emet_sdk/resolve.py", "floor") == "emet_sdk.resolve.x_floor__mutmut_*"
    assert ci.mutant_pattern("plan_file.py", "Gate.refuses") == "plan_file.xǁGateǁrefuses__mutmut_*"
    # `mutmut results` as 3.8.1 prints it: every mutant of the mutated files
    # that was not killed, those the run was not asked for as "not checked".
    results = ("    emet_sdk.resolve.x_floor__mutmut_3: survived\n"
               "    emet_sdk.resolve.x_floor__mutmut_4: killed\n"
               "    emet_sdk.resolve.x_floor__mutmut_5: skipped\n"
               "    emet_sdk.resolve.x_floor__mutmut_6: not checked\n"
               "    emet_sdk.resolve.x_floor__mutmut_7: no tests\n"
               "    emet_sdk.resolve.x_floored__mutmut_1: survived\n"
               "    emet_sdk.resolve.x_untouched__mutmut_1: not checked\n"
               "    emet_sdk.chains.xǁGateǁrefuses__mutmut_2: not checked\nsome other line\n")
    chosen = [ci.mutant_pattern("emet_sdk/resolve.py", "floor")]
    assert ci.survivors(results, chosen) == ["emet_sdk.resolve.x_floor__mutmut_3: survived",
                                             "emet_sdk.resolve.x_floor__mutmut_6: not checked",
                                             "emet_sdk.resolve.x_floor__mutmut_7: no tests"]
    assert ci.survivors(results, []) == []


def test_mutmut_mutates_a_changed_target_in_a_copy_and_names_its_survivors(tmp_path):
    root = make_tree(tmp_path, "tools/ci/mutmut-targets.txt", "emet-sdk/emet_sdk/resolve.py",
                     "emet-sdk/tests/test_resolve.py")
    (root / "tools/ci/mutmut-targets.txt").write_text(
        "emet-sdk/emet_sdk/resolve.py::floor\nemet-sdk/emet_sdk/resolve.py::untouched\n", encoding="utf-8")
    (root / "emet-sdk/emet_sdk/resolve.py").write_text(SOURCE, encoding="utf-8")
    listed = "emet-sdk/emet_sdk/resolve.py\nemet-sdk/tests/test_resolve.py\nemet-sdk/pyproject.toml\n"
    results = ("    emet_sdk.resolve.x_floor__mutmut_1: survived\n"
               "    emet_sdk.resolve.x_floor__mutmut_2: killed\n"
               "    emet_sdk.resolve.x_untouched__mutmut_1: not checked\n")
    fake = Fake().on("git merge-base", out="8efab3a\n").on("git diff -U0", out=DIFF) \
        .on("git ls-files", out=listed).on("mutmut results", out=results)
    before = snapshot(root)
    ctx = context(root, fake, os_name="linux", fork=True, modules=("mutmut",))
    outcome = ci.run_job(ctx, ci.BY_KEY["mutmut"])
    run = next((line, cwd) for line, cwd, _ in fake.calls if "mutmut run" in line)
    assert run[0].endswith("-m mutmut run emet_sdk.resolve.x_floor__mutmut_*")
    assert run[1].name == "emet-sdk" and run[1].parent.name == "mutmut-emet-sdk" and root not in run[1].parents
    config = (run[1] / "setup.cfg").read_text(encoding="utf-8")
    assert "source_paths=\n    emet_sdk/\n" in config and "pytest_add_cli_args_test_selection=\n    tests/\n" in config
    assert (run[1] / "emet_sdk" / "resolve.py").read_text(encoding="utf-8") == SOURCE
    assert outcome.status == ci.FAILED and outcome.reason == "mutmut: 1 mutant(s) survived the tests"
    assert outcome.notes == ["mutmut: survived: emet_sdk.resolve.x_floor__mutmut_1: survived"]
    assert snapshot(root) == before
    ctx.close()


TOOLS_DIFF = DIFF.replace("emet-sdk/emet_sdk/resolve.py", "tools/plan_file.py")


def test_a_tools_target_mutates_from_the_root_of_its_own_copy_with_the_tools_beside_the_packages(tmp_path):
    """Stream B5's F4. mutmut runs a project's tests from `mutants/tests/`
    inside the folder it starts in, and a tools test takes the folder above
    its own to be the repository. Started in `tools/`, that folder was
    `tools/`, and a test looked for `tools/tools/bank/canary.py`."""
    root = make_tree(tmp_path, "tools/ci/mutmut-targets.txt", "tools/plan_file.py", "tools/tests/test_plan_file.py",
                     "tools/bank/canary.py", "tools/pytest.ini", "emet-sdk/emet_sdk/resolve.py")
    (root / "tools/ci/mutmut-targets.txt").write_text(
        "tools/plan_file.py::floor\nemet-sdk/emet_sdk/resolve.py::floor\n", encoding="utf-8")
    for source in ("tools/plan_file.py", "emet-sdk/emet_sdk/resolve.py"):
        (root / source).write_text(SOURCE, encoding="utf-8")
    listed = "\n".join(p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file()) + "\n"
    results = "    plan_file.x_floor__mutmut_1: killed\n    emet_sdk.resolve.x_floor__mutmut_1: killed\n"
    fake = Fake().on("git merge-base", out="8efab3a\n").on("git diff -U0", out=DIFF + TOOLS_DIFF) \
        .on("git ls-files", out=listed).on("mutmut results", out=results)
    before = snapshot(root)
    ctx = context(root, fake, os_name="linux", fork=True, modules=("mutmut",))
    outcome = ci.run_job(ctx, ci.BY_KEY["mutmut"])
    assert outcome.status == ci.PASSED, ctx.out.text
    runs = {line.rsplit(" ", 1)[-1]: cwd for line, cwd, _ in fake.calls if "mutmut run" in line}
    tools, sdk = runs["plan_file.x_floor__mutmut_*"], runs["emet_sdk.resolve.x_floor__mutmut_*"]

    # The tools' run starts at the root of a copy of its own: the module is
    # `plan_file`, as the tests import it, and the folder above
    # `mutants/tests/` holds the packages and `tools/` as the repository does.
    assert tools.name == "mutmut-tools" and root not in tools.parents
    assert (tools / "plan_file.py").read_text(encoding="utf-8") == SOURCE
    assert all((tools / name).exists() for name in ("tests", "bank", "pytest.ini", "emet-sdk", "tools"))
    test_file = tools / "mutants" / "tests" / "test_plan_file.py"
    assert (test_file.parents[2] / "tools" / "bank" / "canary.py").is_file()
    assert (test_file.parents[2] / "emet-sdk" / "pyproject.toml").is_file()
    config = (tools / "setup.cfg").read_text(encoding="utf-8")
    assert "source_paths=\n    plan_file.py\n" in config
    copied = config.split("also_copy=\n")[1].split()
    assert copied == sorted(p.name for p in (root / "tools").iterdir())
    assert "emet-sdk" not in copied and "tools" not in copied

    # The SDK's run keeps its package's folder, in a copy the tools' run never touched.
    assert sdk.name == "emet-sdk" and sdk.parent.name == "mutmut-emet-sdk"
    assert not (sdk.parent / "plan_file.py").exists() and not (sdk.parent / "setup.cfg").exists()
    assert snapshot(root) == before
    ctx.close()


def test_the_tools_are_never_copied_up_over_an_entry_the_root_already_has(tmp_path):
    tree = tmp_path / "copy"
    (tree / "tools" / "tests").mkdir(parents=True)
    (tree / "tools" / "README.md").write_text("tools\n", encoding="utf-8")
    (tree / "README.md").write_text("the repository\n", encoding="utf-8")
    with pytest.raises(ValueError, match="already holds README.md"):
        ci.mutmut_folder(tree, "tools")
    assert (tree / "README.md").read_text(encoding="utf-8") == "the repository\n"
    assert not (tree / "tests").exists()
    (tree / "emet-sdk").mkdir()
    (tree / "tools" / "hardware_guard.py").write_text("", encoding="utf-8")
    assert ci.mutmut_folder(tree, "emet-sdk") == (tree / "emet-sdk", [])


def test_a_mutmut_copy_without_the_hardware_guard_is_refused_naming_it(tmp_path):
    """C13: every suite's conftest walks up from mutants/tests/ to the guard
    and fails the collection without it, so the copy must hold it."""
    tree = tmp_path / "copy"
    (tree / "emet-sdk" / "tests").mkdir(parents=True)
    with pytest.raises(ValueError, match=r"the mutmut copy of emet-sdk has no tools/hardware_guard\.py, which every "
                                         r"suite's conftest\.py loads"):
        ci.mutmut_folder(tree, "emet-sdk")
    with pytest.raises(ValueError, match="has no tools/hardware_guard.py"):
        ci.mutmut_folder(tree, "emet-sdk", tmp_path / "nowhere.py")
    real = tmp_path / "hardware_guard.py"
    real.write_text("# the guard\n", encoding="utf-8")
    assert ci.mutmut_folder(tree, "emet-sdk", real) == (tree / "emet-sdk", ["tests"])
    assert (tree / "tools" / "hardware_guard.py").read_text(encoding="utf-8") == "# the guard\n"
    conftest = tree / "emet-sdk" / "mutants" / "tests" / "conftest.py"
    assert tree / "tools" / "hardware_guard.py" in [p / "tools" / "hardware_guard.py" for p in conftest.parents]


def test_the_mutmut_job_hands_each_copy_the_trees_guard(tmp_path):
    root = make_tree(tmp_path, "tools/ci/mutmut-targets.txt", "emet-sdk/emet_sdk/resolve.py")
    (root / "tools/hardware_guard.py").write_text("# the guard\n", encoding="utf-8")
    (root / "tools/ci/mutmut-targets.txt").write_text("emet-sdk/emet_sdk/resolve.py::floor\n", encoding="utf-8")
    (root / "emet-sdk/emet_sdk/resolve.py").write_text(SOURCE, encoding="utf-8")
    listed = "\n".join(p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))
                       if p.is_file() and p.name != "hardware_guard.py") + "\n"
    fake = Fake().on("git merge-base", out="8efab3a\n").on("git diff -U0", out=DIFF) \
        .on("git ls-files", out=listed).on("mutmut results", out="    emet_sdk.resolve.x_floor__mutmut_1: killed\n")
    ctx = context(root, fake, os_name="linux", fork=True, modules=("mutmut",))
    assert ci.run_job(ctx, ci.BY_KEY["mutmut"]).status == ci.PASSED, ctx.out.text
    (cwd,) = [cwd for line, cwd, _ in fake.calls if "mutmut run" in line]
    assert (cwd.parent / "tools" / "hardware_guard.py").read_text(encoding="utf-8") == "# the guard\n"
    ctx.close()


def test_mutmut_with_nothing_changed_passes_and_without_its_parts_is_not_run(tmp_path):
    root = make_tree(tmp_path, "tools/ci/mutmut-targets.txt")
    (root / "tools/ci/mutmut-targets.txt").write_text("tools/plan_file.py::read_plan\n", encoding="utf-8")
    fake = Fake().on("git merge-base", out="8efab3a\n")
    ctx = context(root, fake, os_name="linux", fork=True, modules=("mutmut",), env={"BASE_SHA": "base0"})
    assert ci.run_job(ctx, ci.BY_KEY["mutmut"]).status == ci.PASSED
    assert fake.lines == ["git merge-base base0 HEAD", "git diff -U0 8efab3a -- tools/plan_file.py"]
    assert "nothing to mutate" in ctx.out.text
    no_tool = ci.run_job(context(root, Fake(), os_name="linux", fork=True), ci.BY_KEY["mutmut"])
    assert (no_tool.status, no_tool.reason) == (ci.NOT_RUN, "needs mutmut in the interpreter that runs the suites")
    no_list = ci.run_job(context(make_tree(tmp_path / "bare"), Fake(), fork=True), ci.BY_KEY["mutmut"])
    assert no_list.reason == "tools/ci/mutmut-targets.txt is missing"
