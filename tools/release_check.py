#!/usr/bin/env python3
"""Check the things a release gets wrong that no single package can notice.

Every package has its own tests, and they pass while the *repository* is
inconsistent — because a package cannot see its siblings. `emet-sdk` could sit
at 0.3.0 beside an `emet-hal` still at 0.2.0 and every suite would be green.
This looks across the whole tree instead.

It is deliberately narrow. It does not re-run the test suites, because CI does
that and duplicating it here would produce a slow script people stop running.
It checks the *cross-cutting* invariants:

    versions      all three packages agree
    plugins       every discovery group has a shipped implementation
    docs          nothing advertises a version the code no longer is
    markers       no TODO or FIXME left in shipped source

**Why this exists.** 0.3's scope was "audio in/out, wake word, VAD,
endpointing". Audio *out* was not wired, and it went unnoticed for days because
the work was checked against a memory of the scope rather than the written
scope. A script cannot read a roadmap, so it cannot catch that one — see
`RELEASING.md` for the human half — but everything it *can* mechanise, it
should, because the checks people skip are the ones that need remembering.

Dependency-free and short enough to read, like `check_layering.py`.

Usage:  python tools/release_check.py [repo_root]
Exit:   0 clean, 1 problems found.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

PACKAGES = ("emet-sdk", "emet-hal", "emet-engine")

#: Docs that state a version and will lie if they are not updated. A stale
#: README is the first thing a newcomer reads and the last thing anybody edits.
VERSIONED_DOCS = ("README.md", "emet-sdk/README.md", "emet-hal/README.md", "emet-engine/README.md")

#: Groups the SDK knows how to discover. Each should have at least one shipped
#: implementation, or the group is a promise nothing keeps.
EXPECTED_GROUPS = (
    "emet.actuators",
    "emet.sensors",
    "emet.locomotion",
    "emet.wake",
    "emet.audio",
    "emet.audio_out",
)

problems: list[str] = []
notes: list[str] = []


def problem(msg: str) -> None:
    problems.append(msg)


def declared_versions(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for pkg in PACKAGES:
        path = root / pkg / "pyproject.toml"
        if not path.exists():
            problem(f"{pkg}: no pyproject.toml")
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        version = data.get("project", {}).get("version")
        if not version:
            problem(f"{pkg}: pyproject.toml declares no version")
            continue
        out[pkg] = version
    return out


def check_versions_agree(versions: dict[str, str]) -> str | None:
    """One repository, one version. Three packages released together that
    disagree about which release they are is the kind of thing nobody notices
    until a bug report quotes two of them."""
    distinct = set(versions.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(versions.items()))
        problem(f"packages disagree about the version: {detail}")
        return None
    version = distinct.pop() if distinct else None
    if version:
        notes.append(f"version {version} across {len(versions)} packages")
    return version


def check_single_declaration(root: Path) -> None:
    """The version belongs in pyproject.toml and nowhere else.

    A hardcoded `__version__` is how the source and the installed metadata
    drifted before: both were written by hand, and nothing compared them.
    """
    for pkg in PACKAGES:
        module = pkg.replace("-", "_")
        init = root / pkg / module / "__init__.py"
        if not init.exists():
            continue
        text = init.read_text(encoding="utf-8")
        if re.search(r'^__version__\s*=\s*["\']', text, re.M):
            problem(
                f"{pkg}/{module}/__init__.py hardcodes __version__. Read it from "
                f"the installed distribution instead, so pyproject.toml stays the "
                f"only declaration."
            )


def check_groups(root: Path) -> None:
    """Every discoverable group should have something shipped behind it."""
    provided: dict[str, list[str]] = {}
    for pkg in PACKAGES:
        path = root / pkg / "pyproject.toml"
        if not path.exists():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for group, entries in (data.get("project", {}).get("entry-points") or {}).items():
            provided.setdefault(group, []).extend(entries)

    for group in EXPECTED_GROUPS:
        if not provided.get(group):
            problem(
                f"entry-point group {group!r} has no shipped implementation. Either "
                f"something is missing or the group should not exist yet."
            )
    if provided:
        total = sum(len(v) for v in provided.values())
        notes.append(f"{total} entry points across {len(provided)} groups")


def check_docs(root: Path, version: str | None) -> None:
    """Nothing should advertise a version the code no longer is."""
    if not version:
        return
    series = ".".join(version.split(".")[:2])          # 0.3.0 -> 0.3
    stale = re.compile(r"\b0\.\d+\b")
    for rel in VERSIONED_DOCS:
        path = root / rel
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Only lines that are *claiming* a version, not every mention of a
            # number: "what is in 0.2", "status: 0.2", "shipped in 0.2".
            if not re.search(r"(?i)\b(status|what is in|shipped in|version)\b", line):
                continue
            found = [m for m in stale.findall(line) if m != series]
            if found:
                problem(f"{rel}:{n} still says {found[0]}, but this is {series}: {line.strip()}")


def check_markers(root: Path) -> None:
    """No TODO or FIXME in shipped source. Notes to self are fine in a branch
    and are not fine in a tag."""
    hits: list[str] = []
    for pkg in PACKAGES:
        module = pkg.replace("-", "_")
        for py in (root / pkg / module).rglob("*.py"):
            for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line):
                    hits.append(f"{py.relative_to(root)}:{n}")
    for hit in hits:
        problem(f"marker left in shipped source: {hit}")


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not (root / "emet-sdk").exists():
        print(f"release-check: {root} does not look like the Emet repository", file=sys.stderr)
        return 1

    versions = declared_versions(root)
    version = check_versions_agree(versions)
    check_single_declaration(root)
    check_groups(root)
    check_docs(root, version)
    check_markers(root)

    for note in notes:
        print(f"release-check: {note}")
    if not problems:
        print("release-check: ok")
        print(
            "release-check: mechanical checks only. The scope and acceptance "
            "criteria in RELEASING.md still need a person."
        )
        return 0
    print()
    for p in problems:
        print(f"  x {p}")
    print(f"\n{len(problems)} problem(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
