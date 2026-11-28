#!/usr/bin/env python3
"""Enforce the house style that a review keeps missing.

    em-dashes      none, anywhere in the tree: a comma, a colon, or two
                   sentences instead
    banned words   delve, leverage, robust, seamless, comprehensive,
                   underscore, in prose
    addresses      no email address outside four allowed forms: the
                   project's own, GitHub's noreply domain, and placeholders
                   at example.com and example.invalid

and four prose rules from 0.5.7-D10 (c), which are problems like the rest
while `PROSE_RULES_BLOCK` is True, as it is since 0.5.7's sweep rewrote what
they found (set False, they print `warning:` lines and leave the exit code
alone):

    docstrings     no one-line docstring shaped "X: Y", a `: ` outside
                   backticks; one plain sentence instead
    litotes        a closed list ("not without", "no small", ...); say the
                   positive thing
    not just       "not just (only, merely, simply) ... but" within 80
                   characters of one line; say what it is
    inflections    the banned words' other forms (delved, leveraging,
                   robustly, seamlessly, comprehensively, underscores, ...)

`--file PATH`, repeatable, reads a file outside the tree with every rule,
such as a release's `../releases/vX.Y.Z/pr-body.md`.

**Why this exists.** 0.3 removed 178 em-dashes by hand from code, comments,
schemas, examples and docs after they had crept in one at a time over three
releases. Each one had passed review, because a reviewer reading for meaning
does not see punctuation. A script does. The banned words are the ones that
read as press release rather than as a person; CONTRIBUTING.md has the rule.

The addresses are here for the same reason. The project has one public
address, and a line pushed to a public repository stays public: it is cloned
and cached, and taking it out later does not take it back. A personal
address reaches a file the way an em-dash does: pasted into a fixture, a
sign-off example or an attribution line, one at a time, past a reviewer who
is reading for meaning. The idea of scanning the tree for what must never be
pushed is taken from LeRobot's pre-commit, which runs gitleaks (LeRobot's
.pre-commit-config.yaml runs `id: gitleaks`; LeRobot Apache-2.0 and gitleaks
MIT, each from its repository's licence file through the GitHub API;
verified 2026-11-26); no code or rule of either is used. See CITATIONS.md.

The check is deliberately blunt. The style rules have no allowlist, and no
rule has a way to mark a line as exempt, because every exemption is the
first of many. The address rule allows four forms and nothing else. If a
legitimate case appears, change the rule here, in one place, with a reason.

An address problem names the file, the line and the column and never the
address: this output lands in a public CI log, and printing a personal
address there would publish it a second time. A path whose folder or file
name holds an address outside the four is refused too, and every problem
prints that path with the address cut out.

Dependency-free and short enough to read, like `check_layering.py`.

Usage:  python tools/check_style.py [repo_root] [--file PATH ...]
Exit:   0 clean (warnings allowed), 1 problems found.
"""

from __future__ import annotations

import argparse
import ast
import codecs
import re
import subprocess
import sys
from pathlib import Path

EM_DASH = "—"

#: Prose that sounds like marketing. Word-bounded, case-insensitive.
BANNED = ("delve", "leverage", "robust", "seamless", "comprehensive", "underscore")

#: What counts as text worth checking for style.
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".sh", ".ps1"}

#: Never descend into these.
SKIP_DIRS = {".git", ".venv", "__pycache__", "dist", "build", ".pytest_cache", "node_modules"}

#: Third-party text the project did not write and should not rewrite, plus
#: this file, which has to name what it bans. The address rule reads these
#: too: a code of conduct is where a contact address gets pasted.
SKIP_FILES = {"CODE_OF_CONDUCT.md", "LICENSE", "check_style.py"}

BANNED_RE = re.compile(r"\b(" + "|".join(BANNED) + r")\b", re.IGNORECASE)

#: 0.5.7-D10 (c). True since A12's sweep rewrote what the four prose rules
#: found, so they block like the rest; False makes them warnings again.
PROSE_RULES_BLOCK = True

#: The banned words' other forms. The bare words are BANNED's.
INFLECTED_RE = re.compile(
    r"\b(delv(?:es|ed|ing)|leverag(?:es|ed|ing)|robust(?:ly|ness)|seamlessly|comprehensive(?:ly|ness)"
    r"|underscor(?:es|ed|ing))\b",
    re.IGNORECASE,
)

#: Litotes, a closed list: CLAUDE.md rule 3 and the corrections behind
#: 0.5.7-D10 (c). Any run of spaces between the words counts as one.
LITOTES = ("not without", "no small", "not a coincidence", "not unlike", "not uncommon", "not unusual",
           "not insignificant", "not unimportant", "not unreasonable", "no mean feat")
LITOTES_RE = re.compile(r"\b(" + "|".join(r"\s+".join(map(re.escape, l.split())) for l in LITOTES) + r")\b",
                        re.IGNORECASE)

#: Negative parallelism on one line: "not just X but Y", with at most 80
#: characters between the two.
NOT_JUST_RE = re.compile(r"\bnot\s+(?:just|only|merely|simply)\b.{0,80}?\bbut\b", re.IGNORECASE)

#: A span in backticks, which a docstring's `: ` may sit inside.
BACKTICKS = re.compile(r"`[^`]*`")

#: An address-shaped string: a local part, `@`, and a dotted domain whose
#: last label is letters or a punycode `xn--` label, so
#: `actions/checkout@v6.0.1` and `pkg@1.0.dev1` are none.
#:
#: The local part is letters, digits and `._%+-`, and it starts at the first
#: of them, so the quote and the `=` of `email='...'` stay outside it. It may
#: end in one more character RFC 5322 allows in an address (atext, section
#: 3.2.3, verified 2026-11-26), as `someone!@` does, except `'` and `/`,
#: which code and links put before an `@`. A quoted local part,
#: `"someone"@`, is an address too. `[bot]` is how GitHub writes an app's
#: login, as in Dependabot's address.
#:
#: The domain is taken whole. An address at `example.com.elsewhere.net` or
#: `example.com.xn--p1ai` is judged as that and never as its first labels,
#: and one whose domain goes on past its last letters label, as
#: `example.com.co2`, is no address at all. A hyphen after the last label
#: ends the address, since `--` is how a dash gets typed where an em-dash
#: is banned: an address followed by `--or` is judged without the `--or`.
ADDRESS_RE = re.compile(
    r"(?:\"[^\"\s@]+\""
    r"|(?<![A-Za-z0-9._%+-])(?:[A-Za-z0-9._%+-]+(?:\[bot\])?|[A-Za-z0-9._%+-]*[!#$&*=?^`{|}~]))"
    r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    r"\.(?:xn--[A-Za-z0-9-]*[A-Za-z0-9]|[A-Za-z]{2,})"
    r"(?![A-Za-z0-9]|\.[A-Za-z0-9])"
)

#: The four forms an address in the tree may take. Each pattern is anchored
#: at both ends and the whole address is held to it: without its `$`, the
#: third would pass any address whose domain only starts with example.com.
#:
#:   1. The project's address, the one contact the documents give.
#:   2. GitHub's noreply domain: a contributor's `ID+login` or `login` form,
#:      and Dependabot's and the Actions bot's `ID+name[bot]`. These are
#:      what a sign-off example or a workflow's `git config` line holds.
#:   3. A placeholder at example.com, a name RFC 2606 reserves for use as
#:      an example (section 3, verified 2026-11-26).
#:   4. A placeholder at example.invalid, under the top-level name the same
#:      RFC reserves for names "sure to be invalid" (section 2), for a test
#:      whose address must reach nobody.
#:
#: Refused, since they are outside the four: `example.org` and
#: `example.net` (two spellings of a placeholder are enough), GitHub's
#: web-flow committer address and Dependabot's sign-off address at
#: github.com (the history holds them, and this check reads the tree), and
#: every other vendor's noreply address, so an attribution line that
#: carries one is caught. A line with no address, such as a "Generated
#: with" line, is outside this rule.
ALLOWED_ADDRESSES = (
    re.compile(r"^wake\.up\.emet@gmail\.com$", re.IGNORECASE),
    re.compile(r"^[^@]+@users\.noreply\.github\.com$", re.IGNORECASE),
    re.compile(r"^[^@]+@example\.com$", re.IGNORECASE),
    re.compile(r"^[^@]+@example\.invalid$", re.IGNORECASE),
)


#: What a problem prints in place of an address in a path.
CUT = "(an address)"


def skipped(root: Path, path: Path) -> bool:
    """True for a path under a folder the walks skip. The path is judged
    below the root, so a checkout that itself sits in a folder named
    `build` is still read."""
    parts = path.relative_to(root).parts
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parts)


def text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if skipped(root, path):
            continue
        if not path.is_file() or path.name in SKIP_FILES:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in ("NOTICE",):
            yield path


def tracked_files(root: Path) -> list[Path] | None:
    """Every file git tracks or would track, or None when git cannot say.

    Asked only of a checkout's own top folder. A tree unpacked with no
    `.git`, as the body runner's is, gets the walk below, and so does any
    folder that merely sits inside some other repository.
    """
    if not (root / ".git").exists():
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    names = done.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return sorted(root / name for name in names if name)


def address_files(root: Path) -> list[Path]:
    """Every file whose addresses could reach GitHub: git's own list of
    what it tracks or would track, whatever the suffix, and without git
    every file outside the skipped directories."""
    files = tracked_files(root)
    if files is None:
        files = [path for path in sorted(root.rglob("*")) if not skipped(root, path)]
    return [path for path in files if path.is_file()]


def as_text(data: bytes) -> str | None:
    """A file's text, or None for a binary file, which is not read.

    Binary is git's own test: a NUL in the first 8000 bytes. A file with a
    UTF-16 mark is text that Windows wrote and is read as that. Bytes that
    are no UTF-8 are replaced and the file is still read: an address is
    ASCII, and one in a Latin-1 file is as public as any other.
    """
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def allowed(address: str) -> bool:
    return any(pattern.search(address) for pattern in ALLOWED_ADDRESSES)


def shown(rel: str) -> str:
    """A path as a problem prints it. An address outside the four in a
    folder's or a file's name is cut out, since the log would publish it
    as surely as a line would."""
    return ADDRESS_RE.sub(lambda m: m.group(0) if allowed(m.group(0)) else CUT, rel)


def check_addresses(root: Path) -> list[str]:
    problems: list[str] = []
    for path in address_files(root):
        name = path.relative_to(root).as_posix()
        rel = shown(name)
        if rel != name:
            problems.append(f"{rel}: an address outside the allowlist in the path. Rename it.")
        text = as_text(path.read_bytes())
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in ADDRESS_RE.finditer(line):
                if not allowed(m.group(0)):
                    problems.append(
                        f"{rel}:{n}: an address outside the allowlist, at column {m.start() + 1}. "
                        f"Use the project's address, or a placeholder at example.com."
                    )
    return problems


def one_line_docstrings(text: str) -> list[int]:
    """The line of each one-line docstring shaped "X: Y" in a module's text:
    a `: ` outside backticks. A module that does not parse has none here."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                continue
            value = first.value.value.strip()
            if "\n" not in value and ": " in BACKTICKS.sub("", value):
                found.append(first.value.lineno)
    return sorted(found)


def prose_findings(rel: str, lines: list[str], text: str, python: bool) -> list[str]:
    """What the four rules of 0.5.7-D10 (c) find in one file."""
    found: list[str] = []
    if python:
        found += [f'{rel}:{n}: a one-line docstring shaped "X: Y"; write one plain sentence'
                  for n in one_line_docstrings(text)]
    for n, line in enumerate(lines, 1):
        for m in LITOTES_RE.finditer(line):
            found.append(f'{rel}:{n}:{m.start() + 1}: litotes "{" ".join(m.group(0).split())}"; say the positive thing')
        for m in NOT_JUST_RE.finditer(line):
            found.append(f'{rel}:{n}:{m.start() + 1}: "not just ... but"; say what it is')
        for m in INFLECTED_RE.finditer(line):
            found.append(f"{rel}:{n}: {m.group(0)!r}. Say the plain thing.")
    return found


def style_findings(rel: str, text: str, python: bool) -> tuple[list[str], list[str]]:
    """One file's problems and prose findings, by the rules for text."""
    problems: list[str] = []
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        if EM_DASH in line:
            problems.append(f"{rel}:{n}: em-dash. Use a comma, a colon, or two sentences.")
        for m in BANNED_RE.finditer(line):
            problems.append(f"{rel}:{n}: {m.group(0)!r}. Say the plain thing.")
    return problems, prose_findings(rel, lines, text, python)


def check_prose(root: Path, extra: list[Path] | None = None) -> tuple[list[str], list[str]]:
    """Every rule but the addresses over the tree, then every rule over each
    `--file`: the problems, and the prose rules' findings."""
    problems: list[str] = []
    prose: list[str] = []
    for path in text_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found, more = style_findings(shown(path.relative_to(root).as_posix()), text, path.suffix == ".py")
        problems += found
        prose += more
    problems.extend(check_addresses(root))
    for path in extra or []:
        rel = shown(path.as_posix())
        if not path.is_file():
            problems.append(f"{rel}: no such file to read (--file)")
            continue
        text = as_text(path.read_bytes())
        if text is None:
            problems.append(f"{rel}: a binary file, which --file does not read")
            continue
        found, more = style_findings(rel, text, path.suffix == ".py")
        problems += found
        prose += more
        for n, line in enumerate(text.splitlines(), 1):
            for m in ADDRESS_RE.finditer(line):
                if not allowed(m.group(0)):
                    problems.append(f"{rel}:{n}: an address outside the allowlist, at column {m.start() + 1}. "
                                    f"Use the project's address, or a placeholder at example.com.")
    return problems, prose


def check(root: Path, extra: list[Path] | None = None) -> list[str]:
    """Every problem, the prose rules' findings among them once they block."""
    problems, prose = check_prose(root, extra)
    return problems + prose if PROSE_RULES_BLOCK else problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="check_style.py", description="Enforce the house style.")
    parser.add_argument("root", nargs="?", default=".", help="the repository's root")
    parser.add_argument("--file", action="append", default=[], type=Path,
                        help="a file outside the tree to read with every rule, such as a pull request's body")
    args = parser.parse_args(argv[1:])
    root = Path(args.root).resolve()
    if not (root / "emet-sdk").exists():
        print(f"style-check: {root} does not look like the Emet repository", file=sys.stderr)
        return 1
    problems, prose = check_prose(root, args.file)
    if PROSE_RULES_BLOCK:
        problems, prose = problems + prose, []
    for line in prose:
        print(f"  warning: {line}")
    if prose:
        print(f"\n{len(prose)} warning(s) from the prose rules of 0.5.7-D10 (c), which block once "
              f"PROSE_RULES_BLOCK is True.")
    if not problems:
        print("style-check: ok")
        return 0
    for p in problems:
        print(f"  x {p}")
    print(f"\n{len(problems)} problem(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
