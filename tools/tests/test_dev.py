"""The contributor's script, `tools/dev.py`, against fake installs.

`check` is given a tree of four `pyproject.toml` written here and fake
distributions with the fields it reads (`version`, `entry_points`,
`read_text`), so no test reads the venv it runs in. `setup` and `update`
run with a fake runner, or with `--dry-run` and `subprocess.run` replaced
by one that fails the test, against a venv path under `tmp_path`: no test
touches the shared venv. `sounddevice` is never imported: a fake loader
stands in for it.
"""

from __future__ import annotations

import json
import sys
import textwrap
from importlib.metadata import EntryPoint, PackageNotFoundError
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import dev  # noqa: E402

PYPROJECTS = {
    "emet-sdk": """\
        [project]
        name = "emet-sdk"
        version = "0.5.6"
        [project.scripts]
        emet = "emet_sdk.cli:main"
        """,
    "emet-hal": """\
        [project]
        name = "emet-hal"
        version = "0.5.6"
        [project.entry-points."emet.wake"]
        "mock" = "emet_hal.mock:MockWake"
        """,
    "emet-providers": """\
        [project]
        name = "emet-providers"
        version = "0.5.6"
        [project.entry-points."emet.stt"]
        "mock" = "emet_providers.mock:MockTranscriber"
        """,
    "emet-engine": """\
        [project]
        name = "emet-engine"
        version = "0.5.6"
        [project.scripts]
        emet-listen = "emet_engine.cli:main"
        """,
}


def write_tree(root: Path, changes: dict[str, str] | None = None) -> Path:
    for package, text in {**PYPROJECTS, **(changes or {})}.items():
        (root / package).mkdir(parents=True, exist_ok=True)
        (root / package / "pyproject.toml").write_text(textwrap.dedent(text), encoding="utf-8")
    (root / "tools").mkdir(exist_ok=True)
    return root


class FakeDist:
    def __init__(self, root: Path, package: str, version: str = "0.5.6", points=None, editable=True, where=None):
        self.version = version
        self.entry_points = [EntryPoint(name, value, group) for group, name, value in points]
        self._direct = json.dumps({"url": (where or root / package).as_uri(), "dir_info": {"editable": editable}})

    def read_text(self, name: str):
        return self._direct if name == "direct_url.json" else None


def installed(root: Path, **override) -> dict[str, FakeDist]:
    """Fake distributions that match the tree, each changed by `override`."""
    decl = dev.declared(root)
    out = {}
    for package in dev.PACKAGES:
        kwargs = {"points": sorted(decl[package].entry_points), **override.get(package.replace("-", "_"), {})}
        out[package] = FakeDist(root, package, **kwargs)
    return out


def lookup_from(dists: dict[str, FakeDist]):
    def lookup(name: str):
        if name not in dists:
            raise PackageNotFoundError(name)
        return dists[name]
    return lookup


def loads(name: str):
    return object()


def run_check(root: Path, dists, *, load=loads, version=(3, 13, 2)) -> dev.Report:
    return dev.check(root, lookup=lookup_from(dists), load=load, version=version, python="venv-python")


# ------------------------------------------------------------------ check


def test_a_clean_install_passes(tmp_path):
    root = write_tree(tmp_path)
    rep = run_check(root, installed(root))
    assert rep.problems == [] and rep.fixes == []
    lines: list[str] = []
    assert dev.print_report(rep, "venv-python", root, out=lines.append) == 0
    assert lines[-1] == "dev: ok"


def test_an_entry_point_declared_since_the_install_is_stale(tmp_path):
    """The roadmap's test: plant an entry point in a pyproject.toml after the install."""
    root = write_tree(tmp_path)
    dists = installed(root)
    write_tree(root, {"emet-hal": PYPROJECTS["emet-hal"] + '"planted" = "emet_hal.planted:Planted"\n'})
    rep = run_check(root, dists)
    assert rep.problems == ["emet-hal: emet.wake planted = emet_hal.planted:Planted is declared in "
                            "emet-hal/pyproject.toml and not installed, so discovery does not see it: a stale "
                            "install"]
    assert len(rep.fixes) == 1 and rep.fixes[0].startswith(f"reinstall, from {root}: venv-python -m pip install -e")


def test_an_entry_point_removed_since_the_install_is_stale(tmp_path):
    root = write_tree(tmp_path)
    old = sorted(dev.declared(root)["emet-providers"].entry_points) + [("emet.stt", "gone", "emet_providers.gone:Gone")]
    rep = run_check(root, installed(root, emet_providers={"points": old}))
    assert rep.problems == ["emet-providers: emet.stt gone = emet_providers.gone:Gone is installed and no longer "
                            "declared in emet-providers/pyproject.toml: a stale install"]


def test_a_console_script_renamed_since_the_install_shows_both_ways(tmp_path):
    root = write_tree(tmp_path)
    dists = installed(root)
    write_tree(root, {"emet-engine": PYPROJECTS["emet-engine"].replace("emet-listen =", "emet-hear =")})
    rep = run_check(root, dists)
    assert [p.split(" is ")[0] for p in rep.problems] == [
        "emet-engine: console_scripts emet-hear = emet_engine.cli:main",
        "emet-engine: console_scripts emet-listen = emet_engine.cli:main",
    ]


def test_a_version_that_moved_and_versions_that_disagree(tmp_path):
    root = write_tree(tmp_path)
    rep = run_check(root, installed(root, emet_sdk={"version": "0.5.5"}))
    assert rep.problems == [
        "emet-sdk: installed as 0.5.5, and emet-sdk/pyproject.toml declares 0.5.6: a stale install",
        "the installed versions disagree: emet-sdk 0.5.5, emet-hal 0.5.6, emet-providers 0.5.6, emet-engine 0.5.6",
    ]


def test_a_copy_or_another_checkout_is_named(tmp_path):
    root = write_tree(tmp_path / "here")
    other = tmp_path / "elsewhere" / "emet-hal"
    rep = run_check(root, installed(root, emet_sdk={"editable": False}, emet_hal={"where": other}))
    assert rep.problems == [
        "emet-sdk is installed as a copy, so a change in this tree does not reach it",
        f"emet-hal is installed editable from {other}, another checkout",
    ]


LINE = "-m pip install -e emet-sdk[dev] -e emet-hal[dev,audio,wake] -e emet-providers[dev] -e emet-engine[dev]"


def test_a_package_not_installed(tmp_path):
    root = write_tree(tmp_path)
    dists = installed(root)
    del dists["emet-engine"]
    rep = run_check(root, dists)
    assert rep.problems == ["emet-engine is not installed for venv-python"]
    assert rep.fixes == [f"run `python tools/dev.py setup`, or from {root}: venv-python {LINE}"]


def test_another_python_than_the_venvs_is_named_with_the_line_that_checks_the_venv(tmp_path):
    """CONTRIBUTING's `python tools/dev.py check` with no venv active: setup
    again would change nothing for this interpreter."""
    root = write_tree(tmp_path)
    venv_py = dev.venv_python(root / "emet-sdk" / ".venv")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    rep = run_check(root, {})
    check_venv = dev.shown([str(venv_py), "tools/dev.py", "check"])
    assert rep.notes[0] == (f"this checks venv-python, which is not emet-sdk/.venv's interpreter; from {root}, "
                            f"`{check_venv}` checks the venv setup made")
    assert rep.fixes == [f"check the venv setup made, from {root}: {check_venv}; or install into this "
                         f"interpreter: venv-python {LINE}"]
    rep = dev.check(root, lookup=lookup_from(installed(root)), load=loads, version=(3, 13, 2), python=str(venv_py))
    assert rep.notes == [] and rep.problems == []


def test_sounddevice_without_portaudio_raises_oserror_and_names_the_package(tmp_path):
    root = write_tree(tmp_path)

    def no_portaudio(name: str):
        raise OSError("PortAudio library not found")

    rep = run_check(root, installed(root), load=no_portaudio)
    assert rep.problems == ["sounddevice is installed and cannot load PortAudio: PortAudio library not found"]
    assert rep.fixes == [dev.PORTAUDIO_FIX] and "libportaudio2" in dev.PORTAUDIO_FIX


def test_sounddevice_not_installed_is_a_note(tmp_path):
    root = write_tree(tmp_path)

    def absent(name: str):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)   # as the import system raises it

    rep = run_check(root, installed(root), load=absent)
    assert rep.problems == [] and rep.notes[0].startswith("sounddevice is not installed")


@pytest.mark.parametrize("error, problem", [
    (ImportError("DLL load failed while importing _cffi_backend"),
     "sounddevice is installed and fails to import: ImportError: DLL load failed while importing _cffi_backend"),
    (ModuleNotFoundError("No module named '_cffi_backend'", name="_cffi_backend"),
     "sounddevice is installed and fails to import, for want of _cffi_backend: No module named '_cffi_backend'"),
    (RuntimeError("a broken build"), "sounddevice is installed and fails to import: RuntimeError: a broken build"),
], ids=["dll", "missing-dependency", "other"])
def test_sounddevice_installed_and_failing_to_import_is_a_problem(tmp_path, error, problem):
    root = write_tree(tmp_path)

    def broken(name: str):
        raise error

    rep = run_check(root, installed(root), load=broken)
    assert rep.problems == [problem] and rep.notes == []
    assert rep.fixes == ["install sounddevice again with this interpreter: "
                         "venv-python -m pip install --force-reinstall sounddevice"]
    assert dev.print_report(rep, "venv-python", root, out=lambda s: None) == 1


@pytest.mark.parametrize("version", [(3, 10, 14), (3, 14, 0)])
def test_a_python_outside_the_ci_range(tmp_path, version):
    root = write_tree(tmp_path)
    rep = run_check(root, installed(root), version=version)
    named = ".".join(map(str, version))
    assert rep.problems == [f"Python {named} is outside 3.11 to 3.13, the versions CI's test jobs run"]


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 12, 5), (3, 13, 9)])
def test_each_python_ci_runs_is_inside(tmp_path, version):
    root = write_tree(tmp_path)
    rep = run_check(root, installed(root), version=version)
    assert rep.problems == [] and rep.ok[0] == f"Python {'.'.join(map(str, version))}, inside 3.11 to 3.13"


def test_on_312_the_install_line_leaves_the_wake_extra_out(tmp_path):
    """pocketsphinx has cp311 and cp313 wheels and no cp312 one."""
    assert "emet-hal[dev,audio]" in dev.install_args(False, (3, 12))
    assert "emet-hal[dev,audio,wake]" in dev.install_args(True, (3, 11))
    assert "emet-hal[dev,audio,wake]" in dev.install_args(False, (3, 13, 2))
    root = write_tree(tmp_path)
    dists = installed(root)
    del dists["emet-sdk"]
    rep = run_check(root, dists, version=(3, 12, 3))
    assert rep.notes == [dev.no_wake_note((3, 12))]
    assert "-e emet-hal[dev,audio] -e" in rep.fixes[0]


def test_the_four_pyproject_toml_declaring_different_versions_is_named(tmp_path):
    root = write_tree(tmp_path)
    dists = installed(root, emet_hal={"version": "0.5.7"})
    write_tree(root, {"emet-hal": PYPROJECTS["emet-hal"].replace('"0.5.6"', '"0.5.7"')})
    rep = run_check(root, dists)
    assert rep.problems[0] == ("the four pyproject.toml declare different versions: emet-sdk 0.5.6, emet-hal 0.5.7, "
                               "emet-providers 0.5.6, emet-engine 0.5.6")
    assert rep.fixes[0] == "give the four packages one version, as `python tools/release_check.py .` asks"


def test_a_report_with_a_problem_exits_1(tmp_path):
    rep = dev.Report(problems=["one"], fixes=["do this"])
    lines: list[str] = []
    assert dev.print_report(rep, "venv-python", tmp_path, out=lines.append) == 1
    assert lines[-3:] == ["  x one", "  fix: do this", "dev: 1 problem(s)"]


def test_the_real_tree_declares_the_nine_groups_and_three_scripts():
    tree = dev.declared(REPO)
    groups = {g for d in tree.values() for g, _n, _v in d.entry_points}
    assert groups == set(dev.GROUPS) | {dev.SCRIPTS}
    scripts = {n for d in tree.values() for g, n, _v in d.entry_points if g == dev.SCRIPTS}
    assert scripts == {"emet", "emet-listen", "emet-talk"}


# ---------------------------------------------------------- setup, update


def test_dry_run_runs_nothing(tmp_path, monkeypatch, capsys):
    root = write_tree(tmp_path / "repo")

    def refuse(*args, **kwargs):
        raise AssertionError(f"a dry run ran {args!r}")

    monkeypatch.setattr(dev.subprocess, "run", refuse)
    venv = tmp_path / "venv"
    assert dev.main(["setup", "--dry-run", "--root", str(root), "--venv", str(venv)]) == 0
    assert dev.main(["update", "--dry-run", "--root", str(root), "--venv", str(venv)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("$ ") and "-m venv" in out[0]
    assert out[1].startswith("$ ") and "-m pip install -e emet-sdk[dev]" in out[1].replace("'", "")
    assert out[2].startswith("$ ") and "dev.py check --root" in out[2].replace('"', "").replace("'", "")
    assert out[3] == "dev: a dry run, so nothing ran"
    assert out[4] == f"dev: no venv at {venv.resolve()}: run `python tools/dev.py setup` first"
    assert not venv.exists()


def test_setup_with_no_venv_named_makes_emet_sdks(tmp_path, monkeypatch, capsys):
    """CONTRIBUTING: one command makes emet-sdk/.venv, the shared venv."""
    root = write_tree(tmp_path / "repo")
    monkeypatch.setattr(dev.subprocess, "run", lambda *a, **k: pytest.fail("a dry run ran something"))
    assert dev.main(["setup", "--dry-run", "--root", str(root)]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == "$ " + dev.shown([sys.executable, "-m", "venv", str(root.resolve() / "emet-sdk" / ".venv")])


def test_setup_makes_the_venv_installs_and_checks_in_that_order(tmp_path):
    root = write_tree(tmp_path / "repo")
    venv = tmp_path / "venv"
    events: list[tuple[str, object]] = []
    assert dev.install(root, venv, make=True, extras=True, dry_run=False, out=lambda s: events.append(("out", s)),
                       run=lambda argv, cwd: events.append(("run", (argv, cwd))) or 0, version=(3, 13)) == 0
    ran = [argv for kind, (argv, _cwd) in ((k, e) for k, e in events if k == "run")]
    assert ran[0] == [sys.executable, "-m", "venv", str(venv)]
    assert ran[1] == [str(dev.venv_python(venv)), *dev.install_args(True)]
    assert "emet-providers[dev,deepgram,openai,anthropic]" in ran[1]
    assert not any("piper" in arg for arg in ran[1])
    assert ran[2][1:] == [str(root / "tools" / "dev.py"), "check", "--root", str(root)]
    # Every command runs from the root, where the install line's paths are
    # written from, and is printed just before it runs.
    assert all(cwd == root for kind, (argv, cwd) in ((k, e) for k, e in events if k == "run"))
    for i, (kind, event) in enumerate(events):
        if kind == "run":
            assert events[i - 1] == ("out", "$ " + dev.shown(event[0]))


def test_setup_on_312_leaves_the_wake_extra_out_and_says_so(tmp_path):
    root = write_tree(tmp_path / "repo")
    lines: list[str] = []
    assert dev.install(root, tmp_path / "venv", make=True, extras=False, dry_run=True, out=lines.append,
                       version=(3, 12)) == 0
    assert f"dev: {dev.no_wake_note((3, 12))}" in lines
    assert "emet-hal[dev,audio]" in lines[2] and "wake" not in lines[2]


def test_setup_refuses_to_make_a_venv_on_a_python_outside_the_range(tmp_path):
    root = write_tree(tmp_path / "repo")
    lines: list[str] = []
    assert dev.install(root, tmp_path / "venv", make=True, extras=False, dry_run=False, out=lines.append,
                       version=(3, 10), run=lambda argv, cwd: pytest.fail(f"ran {argv}")) == 1
    assert lines == ["dev: this is Python 3.10, outside 3.11 to 3.13; run setup with one of those"]


def test_update_reinstalls_without_making_a_venv_and_stops_at_a_failure(tmp_path):
    root = write_tree(tmp_path / "repo")
    venv = tmp_path / "venv"
    python = dev.venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    ran: list[list[str]] = []
    assert dev.install(root, venv, make=False, extras=False, dry_run=False, out=lambda s: None,
                       run=lambda argv, cwd: ran.append(argv) or 1) == 1
    assert ran == [[str(python), *dev.install_args(False)]]


@pytest.mark.parametrize("cfg, wake", [
    ("home = /usr/bin\nversion = 3.12.3\n", False),
    ("home = /usr/bin\nversion_info = 3.12.3.final.0\n", False),
    ("home = /usr/bin\nversion = 3.13.5\n", True),
    ("home = /usr/bin\n", True),
], ids=["venv-312", "virtualenv-312", "venv-313", "unknown"])
def test_update_reads_the_venvs_python_from_its_pyvenv_cfg(tmp_path, cfg, wake):
    root = write_tree(tmp_path / "repo")
    venv = tmp_path / "venv"
    python = dev.venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    ran: list[list[str]] = []
    assert dev.install(root, venv, make=False, extras=False, dry_run=False, out=lambda s: None,
                       run=lambda argv, cwd: ran.append(argv) or 0) == 0
    assert ("emet-hal[dev,audio,wake]" in ran[0]) is wake


def test_the_install_line_is_the_one_the_docstring_gives():
    """The line is a copy of the release process's (planning/RELEASE_PROCESS.md
    §3, outside the repository); the module's docstring states it, and this
    holds the code to the docstring."""
    stated = next(line.strip() for line in dev.__doc__.splitlines() if line.strip().startswith("pip install -e"))
    assert dev.install_args(False) == ["-m", *stated.replace("'", "").split()]
    assert dev.install_args(False) == ["-m", "pip", "install", "-e", "emet-sdk[dev]", "-e", "emet-hal[dev,audio,wake]",
                                       "-e", "emet-providers[dev]", "-e", "emet-engine[dev]"]
