#!/usr/bin/env python3
"""Set a contributor's tree up, and say when its install has gone stale (BVP5).

    python tools/dev.py setup [--extras] [--dry-run]    make emet-sdk/.venv, install, then check
    python tools/dev.py check                           this interpreter's install against the tree
    python tools/dev.py update [--extras] [--dry-run]   install again, then check

**setup** makes `emet-sdk/.venv` when it is absent and runs the install
line of the release process (`planning/RELEASE_PROCESS.md` §3) with the
venv's interpreter, from the repository's root:

    pip install -e 'emet-sdk[dev]' -e 'emet-hal[dev,audio,wake]' -e 'emet-providers[dev]' -e 'emet-engine[dev]'

On Python 3.12 the `wake` extra is left out, as CI's 3.12 job leaves it
out: pocketsphinx publishes cp311 and cp313 wheels and no cp312 one (PyPI,
5.1.1, verified 2026-11-26), so pip would build it from source with a C
compiler, and the wake tests skip without it. The venv's own Python
decides, read from its `pyvenv.cfg`. `--extras` adds the vendors' extras
to `emet-providers` (`deepgram`, `openai`, `anthropic`). The `piper` extra
stays out: Piper is GPL-3.0, and it goes in a scratch venv, never the
shared one. Then `setup` runs `check` with the venv's interpreter. Each
command is printed before it runs, and `--dry-run` prints them and runs
none. On Windows a printed command is written for cmd and Git Bash;
PowerShell needs `& ` before a quoted program. `--venv DIR` names another
venv.

**update** runs the install line again into the venv, then `check`: the
step after a pull that changed a `pyproject.toml`.

**check** holds the interpreter that runs it to the tree. Run with
another Python than `emet-sdk/.venv`'s, it says so and names the line
that checks the venv:

- each of the four packages is installed, editable, from this tree;
- its installed version is the one its `pyproject.toml` declares, and the
  four agree;
- its installed entry points in the nine `emet.*` groups, and its console
  scripts, are the ones its `pyproject.toml` declares. pip writes an
  editable install's entry points when it runs, so discovery misses one
  added since and still finds one removed since, until the install line
  runs again;
- `sounddevice` loads. Without the PortAudio library it raises `OSError` on
  import, not `ImportError`, and the fix is the system's package
  (`libportaudio2` on Debian, which the Pi's Raspberry Pi OS is built on).
  Loading it starts PortAudio, which lists the sound devices and opens no
  stream. A `sounddevice` that is not installed is a note: the tests that
  need it skip. One that is installed and fails to import is a problem;
- the Python is 3.11 to 3.13, the versions CI's test jobs run.

**Where the idea comes from.** GitHub's "Scripts to Rule Them All"
(https://github.com/github/scripts-to-rule-them-all, CC0-1.0, archived,
verified 2026-11-26) names a `setup` for a fresh tree and an `update` for
one after a pull; Home Assistant's `script/setup` (home-assistant/core,
Apache-2.0, verified 2026-11-26) makes `.venv` and installs into it. The
ideas are taken; no code is. See CITATIONS.md.

Standard library only.

Exit: 0 clean; 1 a problem found, or a command that failed; 2 a usage error.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older, which check refuses
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

#: The entry-point groups the engine discovers plugins through (DESIGN.md).
GROUPS = (
    "emet.actuators", "emet.sensors", "emet.locomotion", "emet.wake", "emet.audio",
    "emet.audio_out", "emet.stt", "emet.llm", "emet.tts",
)
SCRIPTS = "console_scripts"

#: The release process's install line, §3, one argument a package.
INSTALL = ("emet-sdk[dev]", "emet-hal[dev,audio,wake]", "emet-providers[dev]", "emet-engine[dev]")
#: `--extras`: the vendors' extras. Piper is GPL-3.0 and stays out of the shared venv.
WITH_EXTRAS = {"emet-providers[dev]": "emet-providers[dev,deepgram,openai,anthropic]"}
#: Where pocketsphinx, the wake extra, has no wheel, the line without it
#: (tools/ci.py leaves it out of the 3.12 job for the same reason).
NO_WAKE_WHEEL = {(3, 12)}
WITHOUT_WAKE = {"emet-hal[dev,audio,wake]": "emet-hal[dev,audio]"}

#: The Pythons CI's test jobs run: test (py3.11), (py3.12) and (py3.13).
SUPPORTED = ((3, 11), (3, 13))

PORTAUDIO_FIX = ("install the PortAudio library: `sudo apt install libportaudio2` on Debian, "
                 "which the Pi's Raspberry Pi OS is built on")


# ---------------------------------------------------------------- the tree


@dataclass(frozen=True)
class Declared:
    """What one package's `pyproject.toml` declares."""

    name: str
    version: str
    entry_points: frozenset[tuple[str, str, str]]   # (group, name, value)


def declared(root: Path) -> dict[str, Declared]:
    """The four packages' declarations: the version, the console scripts,
    and the entry points of the nine groups."""
    if tomllib is None:
        raise RuntimeError("tomllib needs Python 3.11 or later")
    out: dict[str, Declared] = {}
    for package in PACKAGES:
        data = tomllib.loads((root / package / "pyproject.toml").read_text(encoding="utf-8"))
        project = data.get("project", {})
        points = {(SCRIPTS, name, str(value).replace(" ", "")) for name, value in project.get("scripts", {}).items()}
        for group, items in project.get("entry-points", {}).items():
            if group in GROUPS:
                points.update((group, name, str(value).replace(" ", "")) for name, value in items.items())
        out[package] = Declared(str(project.get("name", package)), str(project.get("version", "")),
                                frozenset(points))
    return out


# ----------------------------------------------------------- the install


def installed_entry_points(dist: Any) -> frozenset[tuple[str, str, str]]:
    return frozenset((ep.group, ep.name, ep.value.replace(" ", "")) for ep in dist.entry_points
                     if ep.group in GROUPS or ep.group == SCRIPTS)


def install_location(dist: Any) -> tuple[bool, Path | None]:
    """Whether pip installed it editable, and the folder it came from, read
    from the `direct_url.json` pip writes (PEP 610)."""
    raw = dist.read_text("direct_url.json")
    if not raw:
        return False, None
    try:
        data = json.loads(raw)
    except ValueError:
        return False, None
    editable = bool((data.get("dir_info") or {}).get("editable"))
    url = urllib.parse.urlsplit(str(data.get("url", "")))
    if url.scheme != "file":
        return editable, None
    return editable, Path(urllib.request.url2pathname(url.path))


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def shown(argv: list[str]) -> str:
    """A command as cmd and Git Bash take it on Windows, and as a POSIX
    shell takes it elsewhere. PowerShell needs `& ` before a quoted program."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def install_args(extras: bool, version: tuple[int, ...] | None = None) -> list[str]:
    """The install line's arguments, for a venv whose Python is `version`
    when it is known: on 3.12 without the wake extra."""
    wheel = version is None or tuple(version[:2]) not in NO_WAKE_WHEEL
    args = ["-m", "pip", "install"]
    for spec in INSTALL:
        spec = WITH_EXTRAS.get(spec, spec) if extras else spec
        args += ["-e", spec if wheel else WITHOUT_WAKE.get(spec, spec)]
    return args


def no_wake_note(version: tuple[int, ...]) -> str:
    return (f"Python {'.'.join(map(str, version[:2]))}: pocketsphinx, the wake extra, publishes no wheel for it, "
            f"so the install line leaves the extra out and the wake tests skip")


def venv_version(venv: Path) -> tuple[int, ...] | None:
    """The Python a venv was made with, from the `pyvenv.cfg` that `venv`
    and virtualenv write (`version = 3.13.2`, or `version_info`)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key.strip().lower()] = value.strip()
    for key in ("version_info", "version"):
        parts = found.get(key, "").split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    return None


# ------------------------------------------------------------------- check


@dataclass
class Report:
    ok: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    def fix(self, line: str) -> None:
        if line not in self.fixes:
            self.fixes.append(line)


Lookup = Callable[[str], Any]
Load = Callable[[str], Any]


def check(root: Path = ROOT, *, lookup: Lookup = importlib.metadata.distribution,
          load: Load = importlib.import_module, version: tuple[int, ...] = tuple(sys.version_info[:3]),
          python: str = sys.executable) -> Report:
    """The install of the interpreter `python` against the tree at `root`.
    `lookup`, `load` and `version` are importlib's and this interpreter's,
    and a test passes its own."""
    rep = Report()
    line = shown([python, *install_args(False, version)])
    reinstall = f"reinstall, from {root}: {line} (`python tools/dev.py update` runs the same line into emet-sdk/.venv)"
    venv_py = venv_python(root / "emet-sdk" / ".venv")
    check_venv = shown([str(venv_py), "tools/dev.py", "check"])
    other = venv_py.exists() and not same_path(Path(python), venv_py)
    if other:
        rep.notes.append(f"this checks {python}, which is not emet-sdk/.venv's interpreter; from {root}, "
                         f"`{check_venv}` checks the venv setup made")

    low, high = SUPPORTED
    major_minor = tuple(version[:2])
    named = ".".join(str(v) for v in version)
    if major_minor in NO_WAKE_WHEEL:
        rep.notes.append(no_wake_note(version))
    if low <= major_minor <= high:
        rep.ok.append(f"Python {named}, inside 3.11 to 3.13")
    else:
        rep.problems.append(f"Python {named} is outside 3.11 to 3.13, the versions CI's test jobs run")
        rep.fix("make the venv with Python 3.11, 3.12 or 3.13: run `tools/dev.py setup` with that interpreter")
        if tomllib is None:
            return rep

    tree = declared(root)
    declared_versions = {p: d.version for p, d in tree.items()}
    if len(set(declared_versions.values())) > 1:
        rep.problems.append("the four pyproject.toml declare different versions: "
                            + ", ".join(f"{p} {v}" for p, v in declared_versions.items()))
        rep.fix("give the four packages one version, as `python tools/release_check.py .` asks")

    versions: dict[str, str] = {}
    for package, decl in tree.items():
        try:
            dist = lookup(package)
        except importlib.metadata.PackageNotFoundError:
            rep.problems.append(f"{package} is not installed for {python}")
            if other:
                rep.fix(f"check the venv setup made, from {root}: {check_venv}; or install into this "
                        f"interpreter: {line}")
            else:
                rep.fix(f"run `python tools/dev.py setup`, or from {root}: {line}")
            continue
        found = str(dist.version)
        versions[package] = found
        stale = False
        if found != decl.version:
            rep.problems.append(f"{package}: installed as {found}, and {package}/pyproject.toml declares "
                                f"{decl.version}: a stale install")
            stale = True
        editable, where = install_location(dist)
        if not editable:
            rep.problems.append(f"{package} is installed as a copy, so a change in this tree does not reach it")
            stale = True
        elif where is None or not same_path(where, root / package):
            rep.problems.append(f"{package} is installed editable from {where}, another checkout")
            stale = True
        have = installed_entry_points(dist)
        for group, name, value in sorted(decl.entry_points - have):
            rep.problems.append(f"{package}: {group} {name} = {value} is declared in {package}/pyproject.toml "
                                f"and not installed, so discovery does not see it: a stale install")
            stale = True
        for group, name, value in sorted(have - decl.entry_points):
            rep.problems.append(f"{package}: {group} {name} = {value} is installed and no longer declared "
                                f"in {package}/pyproject.toml: a stale install")
            stale = True
        if stale:
            rep.fix(reinstall)
        else:
            rep.ok.append(f"{package} {found}, editable from this tree, its {len(have)} entry point(s) "
                          f"as declared")
    if len(set(versions.values())) > 1:
        rep.problems.append("the installed versions disagree: "
                            + ", ".join(f"{p} {v}" for p, v in versions.items()))
        rep.fix(reinstall)

    again = shown([python, "-m", "pip", "install", "--force-reinstall", "sounddevice"])
    broken = f"install sounddevice again with this interpreter: {again}"
    try:
        load("sounddevice")
    except OSError as exc:
        rep.problems.append(f"sounddevice is installed and cannot load PortAudio: {exc}")
        rep.fix(PORTAUDIO_FIX)
    except ModuleNotFoundError as exc:
        if exc.name == "sounddevice":
            rep.notes.append("sounddevice is not installed: emet-hal's audio extra is absent, so no microphone "
                             "or speaker plugin loads, and the tests that need one skip")
        else:
            rep.problems.append(f"sounddevice is installed and fails to import, for want of {exc.name}: {exc}")
            rep.fix(broken)
    except Exception as exc:  # a library that fails to load, or anything else its import raises
        rep.problems.append(f"sounddevice is installed and fails to import: {type(exc).__name__}: {exc}")
        rep.fix(broken)
    else:
        rep.ok.append("sounddevice loads, with PortAudio")
    return rep


def print_report(rep: Report, python: str, root: Path, out: Callable[[str], None] = print) -> int:
    out(f"dev: {python} against {root}")
    for line in rep.ok:
        out(f"  ok {line}")
    for line in rep.notes:
        out(f"  - {line}")
    for line in rep.problems:
        out(f"  x {line}")
    for line in rep.fixes:
        out(f"  fix: {line}")
    if rep.problems:
        out(f"dev: {len(rep.problems)} problem(s)")
        return 1
    out("dev: ok")
    return 0


# ----------------------------------------------------------- setup, update


Run = Callable[[list[str], Path], int]


def real_run(argv: list[str], cwd: Path) -> int:
    return subprocess.run(argv, cwd=cwd).returncode


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install(root: Path, venv: Path, *, make: bool, extras: bool, dry_run: bool, run: Run = real_run,
            out: Callable[[str], None] = print, version: tuple[int, ...] = tuple(sys.version_info[:2])) -> int:
    """`setup` when `make`, else `update`: the venv when absent (setup
    only), the install line, then check, each printed before it runs."""
    python = venv_python(venv)
    steps: list[list[str]] = []
    target: tuple[int, ...] | None
    if python.exists():
        if make:
            out(f"dev: {venv} is there, so it is used as it is")
        target = venv_version(venv)
    elif make:
        low, high = SUPPORTED
        if not low <= tuple(version[:2]) <= high:
            out(f"dev: this is Python {'.'.join(map(str, version[:2]))}, outside 3.11 to 3.13; run setup "
                f"with one of those")
            return 1
        steps.append([sys.executable, "-m", "venv", str(venv)])
        target = tuple(version[:2])
    else:
        out(f"dev: no venv at {venv}: run `python tools/dev.py setup` first")
        return 1
    if target is not None and tuple(target[:2]) in NO_WAKE_WHEEL:
        out(f"dev: {no_wake_note(target)}")
    steps.append([str(python), *install_args(extras, target)])
    steps.append([str(python), str(root / "tools" / "dev.py"), "check", "--root", str(root)])
    for argv in steps:
        out("$ " + shown(argv))
        if dry_run:
            continue
        code = run(argv, root)
        if code != 0:
            out(f"dev: that command exited {code}")
            return 1
    if dry_run:
        out("dev: a dry run, so nothing ran")
    return 0


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a contributor's tree up, and say when its install is stale.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("setup", "make emet-sdk/.venv when absent, install the four packages, then check"),
                       ("check", "hold this interpreter's install to the tree"),
                       ("update", "install the four packages again, then check")):
        each = sub.add_parser(name, help=text, description=text)
        each.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
        if name != "check":
            each.add_argument("--extras", action="store_true",
                              help="add emet-providers' deepgram, openai and anthropic extras (never piper)")
            each.add_argument("--dry-run", action="store_true",
                              help="print each command and run none (on Windows, as cmd and Git Bash take it)")
            each.add_argument("--venv", metavar="DIR", help="the venv to use (default: emet-sdk/.venv)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    missing = [p for p in PACKAGES if not (root / p / "pyproject.toml").is_file()]
    if missing:
        print(f"dev: {root} is not the Emet repository: no pyproject.toml in {', '.join(missing)}", file=sys.stderr)
        return 2
    if args.command == "check":
        return print_report(check(root), sys.executable, root)
    venv = Path(args.venv).resolve() if args.venv else root / "emet-sdk" / ".venv"
    return install(root, venv, make=args.command == "setup", extras=args.extras, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
