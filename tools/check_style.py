#!/usr/bin/env python3
"""Enforce the house style that a review keeps missing.

    em-dashes      none, anywhere in the tree: a comma, a colon, or two
                   sentences instead
    banned words   delve, leverage, robust, seamless, comprehensive,
                   underscore, in prose

**Why this exists.** 0.3 removed 178 em-dashes by hand from code, comments,
schemas, examples and docs after they had crept in one at a time over three
releases. Each one had passed review, because a reviewer reading for meaning
does not see punctuation. A script does. The banned words are the ones that
read as press release rather than as a person; CONTRIBUTING.md has the rule.

The check is deliberately blunt. There is no allowlist and no way to mark a
line as exempt, because every exemption is the first of many. If a legitimate
case appears, change the rule here, in one place, with a reason.

Dependency-free and short enough to read, like `check_layering.py`.

Usage:  python tools/check_style.py [repo_root]
Exit:   0 clean, 1 problems found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

EM_DASH = "—"

#: Prose that sounds like marketing. Word-bounded, case-insensitive.
BANNED = ("delve", "leverage", "robust", "seamless", "comprehensive", "underscore")

#: What counts as text worth checking.
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".sh", ".ps1"}

#: Never descend into these.
SKIP_DIRS = {".git", ".venv", "__pycache__", "dist", "build", ".pytest_cache", "node_modules"}

#: Third-party text the project did not write and should not rewrite, plus
#: this file, which has to name what it bans.
SKIP_FILES = {"CODE_OF_CONDUCT.md", "LICENSE", "check_style.py"}

BANNED_RE = re.compile(r"\b(" + "|".join(BANNED) + r")\b", re.IGNORECASE)


def text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
            continue
        if not path.is_file() or path.name in SKIP_FILES:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in ("NOTICE",):
            yield path


def check(root: Path) -> list[str]:
    problems: list[str] = []
    for path in text_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root).as_posix()
        for n, line in enumerate(lines, 1):
            if EM_DASH in line:
                problems.append(f"{rel}:{n}: em-dash. Use a comma, a colon, or two sentences.")
            for m in BANNED_RE.finditer(line):
                problems.append(f"{rel}:{n}: {m.group(0)!r}. Say the plain thing.")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not (root / "emet-sdk").exists():
        print(f"style-check: {root} does not look like the Emet repository", file=sys.stderr)
        return 1
    problems = check(root)
    if not problems:
        print("style-check: ok")
        return 0
    for p in problems:
        print(f"  x {p}")
    print(f"\n{len(problems)} problem(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
