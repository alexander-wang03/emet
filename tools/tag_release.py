#!/usr/bin/env python3
"""Make the release tag the way the project makes release tags.

Signed, annotated, from the release-notes file, on a clean master that
matches origin, at the version the three packages declare. Every check here
is a mistake that has been made or nearly made:

    unsigned tag     v0.3 first went up with `git tag -a`, no `-s`, and showed
                     Unverified on GitHub
    wrong branch     a tag on a feature branch points at a commit master
                     never had
    dirty tree       a tag with uncommitted work nearby is a tag of something
                     nobody can rebuild
    behind origin    a tag on a local master that has not pulled the squash
                     commit points at the wrong parent
    version drift    the tag says 0.3, a pyproject still says 0.2

Nothing here bypasses the person: it runs `git tag` and, only with `--push`,
`git push`. It never commits and never touches the index.

Usage:
    python tools/tag_release.py 0.3 --notes ../release-notes/v0.3.txt
    python tools/tag_release.py 0.3 --notes ../release-notes/v0.3.txt --push
    python tools/tag_release.py 0.3 --notes ../release-notes/v0.3.txt --dry-run
Exit:   0 tagged (or dry run printed), 1 a check failed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

PACKAGES = ("emet-sdk", "emet-hal", "emet-engine")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def fail(msg: str) -> int:
    print(f"tag-release: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tag_release.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="the release, as 0.N or 0.N.M")
    parser.add_argument("--notes", required=True, help="release-notes file; becomes the tag message")
    parser.add_argument("--push", action="store_true", help="push the tag to origin after making it")
    parser.add_argument("--dry-run", action="store_true", help="run the checks and print the commands only")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv[1:])

    root = Path(args.root).resolve()
    if not (root / "emet-sdk").exists():
        return fail(f"{root} does not look like the Emet repository")

    series = args.version
    full = series if series.count(".") == 2 else f"{series}.0"
    tag = f"v{series}"
    notes = Path(args.notes).resolve()

    # The notes file, before anything else: it is the tag message.
    if not notes.exists():
        return fail(f"notes file not found: {notes}")
    first = notes.read_text(encoding="utf-8").splitlines()[0] if notes.stat().st_size else ""
    if not first.startswith(f"{series}:"):
        return fail(f"first line of {notes.name} is {first!r}; expected it to start with {series + ':'!r}")

    # The three packages agree with the tag.
    for pkg in PACKAGES:
        data = tomllib.loads((root / pkg / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data["project"]["version"]
        if declared != full:
            return fail(f"{pkg}/pyproject.toml declares {declared}, the tag says {full}")

    # Master, clean, and level with origin.
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "master":
        return fail(f"on branch {branch!r}; release tags are made on master")
    if git(root, "status", "--porcelain"):
        return fail("the working tree is not clean; commit or stash first")
    git(root, "fetch", "--quiet", "origin", "master", "--tags")
    if git(root, "rev-parse", "HEAD") != git(root, "rev-parse", "origin/master"):
        return fail("local master and origin/master differ; pull (or push) first")

    # Not twice.
    if git(root, "tag", "--list", tag):
        return fail(f"{tag} already exists locally. To replace it: git tag -d {tag}, then run again")
    if git(root, "ls-remote", "--tags", "origin", tag):
        return fail(f"{tag} already exists on origin. Replacing a pushed tag is a deliberate act; see RELEASE_PROCESS.md")

    # Signing is configured, so `-s` will succeed rather than prompt.
    try:
        key = git(root, "config", "--get", "user.signingkey")
    except subprocess.CalledProcessError:
        key = ""
    if not key:
        return fail("git has no user.signingkey; the tag would be unsigned. Set gpg.format and user.signingkey first")

    # The mechanical release checks, because a tag is a promise they held.
    py = sys.executable
    for script in ("tools/check_layering.py", "tools/release_check.py", "tools/check_style.py"):
        result = subprocess.run([py, str(root / script), str(root)], cwd=root, capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout, end="")
            return fail(f"{script} failed; fix that before tagging")

    commands = [
        ["git", "tag", "-s", tag, "-F", str(notes)],
        ["git", "tag", "-v", tag],
    ]
    if args.push:
        commands.append(["git", "push", "origin", tag])

    print(f"tag-release: {tag} at {git(root, 'rev-parse', '--short', 'HEAD')} ({first})")
    for cmd in commands:
        shown = " ".join(cmd)
        if args.dry_run:
            print(f"  would run: {shown}")
            continue
        print(f"  {shown}")
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8")
        if result.stdout.strip():
            print("    " + result.stdout.strip().replace("\n", "\n    "))
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return fail(f"{shown} failed")
        if result.stderr.strip() and cmd[1] == "tag" and "-v" in cmd:
            print("    " + result.stderr.strip().replace("\n", "\n    "))

    if not args.push and not args.dry_run:
        print(f"tag-release: made and verified. Push it with: git push origin {tag}")
    print(
        f"tag-release: then the GitHub release: gh release create {tag} --verify-tag "
        f"--prerelease --title \"{first}\" --notes-file \"{notes}\""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
