#!/usr/bin/env python3
"""Make the release tag the way the project makes release tags.

Signed, annotated, with its message taken from CHANGELOG.md, on a clean
master that matches origin, at the version the four packages declare. Every
check here is a mistake that has been made or nearly made:

    unsigned tag     v0.3 first went up with `git tag -a`, no `-s`, and showed
                     Unverified on GitHub
    wrong branch     a tag on a feature branch points at a commit master
                     never had
    dirty tree       a tag with uncommitted work nearby is a tag of something
                     nobody can rebuild
    behind origin    a tag on a local master that has not pulled the squash
                     commit points at the wrong parent
    version drift    the tag says 0.3, a pyproject still says 0.2
    no entry         a version with nothing in the changelog is a tag with
                     nothing to say

Every merge to master is one version, so this runs after every merge. The
tag message is the version's section of CHANGELOG.md: the summary line
first, as `X.Y.Z: Summary`, then the entries.

Nothing here bypasses the person: it runs `git tag` and, only with `--push`,
`git push`. It never commits and never touches the index.

Usage:
    python tools/tag_release.py 0.4.1
    python tools/tag_release.py 0.4.1 --push
    python tools/tag_release.py 0.4.1 --dry-run
Exit:   0 tagged (or dry run printed), 1 a check failed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

#: A version this project tags: three numbers, nothing else.
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def fail(msg: str) -> int:
    print(f"tag-release: {msg}", file=sys.stderr)
    return 1


def changelog_section(changelog: Path, version: str) -> tuple[str, str] | None:
    """The summary line and the body of one version's entry, or None.

    An entry is `## [X.Y.Z] - YYYY-MM-DD`, then one line saying what the
    version does, then the Added, Changed and Fixed lists, up to the next
    `## [` heading. The summary becomes the tag's first line and the commit
    title on master, so the three always say the same thing.
    """
    lines = changelog.read_text(encoding="utf-8").splitlines()
    heading = re.compile(r"^## \[" + re.escape(version) + r"\] - \d{4}-\d{2}-\d{2}\s*$")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")),
        len(lines),
    )
    body = [line for line in lines[start + 1 : end]]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return None
    summary = body[0].strip().rstrip(".")
    rest = "\n".join(body[1:]).strip("\n")
    return summary, rest


def tag_message(version: str, summary: str, rest: str) -> str:
    return f"{version}: {summary}\n\n{rest}\n" if rest else f"{version}: {summary}\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tag_release.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="the version being tagged, as X.Y.Z")
    parser.add_argument("--push", action="store_true", help="push the tag to origin after making it")
    parser.add_argument("--dry-run", action="store_true", help="run the checks and print the commands only")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv[1:])

    root = Path(args.root).resolve()
    if not (root / "emet-sdk").exists():
        return fail(f"{root} does not look like the Emet repository")

    version = args.version
    if not VERSION.match(version):
        return fail(f"{version!r} is not X.Y.Z. Every tag carries all three numbers")
    tag = f"v{version}"

    # The changelog entry, before anything else: it is the tag message.
    changelog = root / "CHANGELOG.md"
    if not changelog.exists():
        return fail("CHANGELOG.md is missing")
    section = changelog_section(changelog, version)
    if section is None:
        return fail(
            f"CHANGELOG.md has no entry for {version}. It needs a heading "
            f"`## [{version}] - YYYY-MM-DD` with a one-line summary under it"
        )
    summary, rest = section
    message = tag_message(version, summary, rest)

    # The four packages agree with the tag.
    for pkg in PACKAGES:
        data = tomllib.loads((root / pkg / "pyproject.toml").read_text(encoding="utf-8"))
        declared = data["project"]["version"]
        if declared != version:
            return fail(f"{pkg}/pyproject.toml declares {declared}, the tag says {version}")

    # Master, clean, and level with origin.
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "master":
        return fail(f"on branch {branch!r}; release tags are made on master")
    if git(root, "status", "--porcelain"):
        return fail("the working tree is not clean; commit or stash first")
    git(root, "fetch", "--quiet", "origin", "master", "--tags")
    if git(root, "rev-parse", "HEAD") != git(root, "rev-parse", "origin/master"):
        return fail("local master and origin/master differ; pull (or push) first")

    # The commit being tagged says what the changelog says.
    title = git(root, "log", "-1", "--format=%s")
    expected = f"{version}: {summary}"
    if title != expected:
        return fail(
            f"the commit on master is titled {title!r}; the changelog summary says "
            f"{expected!r}. The squash subject and the changelog entry are the same "
            f"sentence, so fix one of them before tagging"
        )

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
        ["git", "tag", "-s", tag, "-m", message],
        ["git", "tag", "-v", tag],
    ]
    if args.push:
        commands.append(["git", "push", "origin", tag])

    print(f"tag-release: {tag} at {git(root, 'rev-parse', '--short', 'HEAD')} ({expected})")
    for cmd in commands:
        shown = " ".join(cmd[:4]) + (" -m <changelog entry>" if cmd[1] == "tag" and "-m" in cmd else "")
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
    minor_or_major = version.endswith(".0")
    if minor_or_major:
        print(
            f"tag-release: a minor or major, so it gets a GitHub release too: "
            f"gh release create {tag} --verify-tag --prerelease --notes-from-tag"
        )
    else:
        print("tag-release: a patch. The tag is the release; no GitHub release page for it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
