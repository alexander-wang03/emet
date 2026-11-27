#!/usr/bin/env python3
"""Check the things a release gets wrong that no single package can notice.

Every package has its own tests, and they pass while the *repository* is
inconsistent, because a package cannot see its siblings. `emet-sdk` could sit
at 0.3.0 beside an `emet-hal` still at 0.2.0 and every suite would be green.
This looks across the whole tree instead.

It is deliberately narrow. It does not re-run the test suites, because CI does
that and duplicating it here would produce a slow script people stop running.
It checks the *cross-cutting* invariants:

    versions      all four packages agree
    floors        every requirement on an Emet package is `name>=X.Y.Z`, the release's version
    plugins       every discovery group has a shipped implementation
    docs          nothing advertises a version the code no longer is
    readmes       every shipped entry point is named in its package's README
    changelog     the declared version has its entry, dated, with a summary
    security      each line of a `### Security` section names its advisory id
    markers       no TODO or FIXME left in shipped source

**Why this exists.** 0.3's scope was "audio in/out, wake word, VAD,
endpointing". Audio *out* was not wired, and it went unnoticed for days because
the work was checked against a memory of the scope rather than the written
scope. A script cannot read a roadmap, so it cannot catch that one (see
`RELEASING.md` for the human half), but everything it *can* mechanise, it
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

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

#: Docs that state a version and will lie if they are not updated. A stale
#: README is the first thing a newcomer reads and the last thing anybody edits.
VERSIONED_DOCS = (
    "README.md",
    "emet-sdk/README.md",
    "emet-hal/README.md",
    "emet-providers/README.md",
    "emet-engine/README.md",
)

#: Groups the SDK knows how to discover. Each should have at least one shipped
#: implementation, or the group is a promise nothing keeps.
EXPECTED_GROUPS = (
    "emet.actuators",
    "emet.sensors",
    "emet.locomotion",
    "emet.wake",
    "emet.audio",
    "emet.audio_out",
    "emet.stt",
    "emet.llm",
    "emet.tts",
)

#: A requirement string (PEP 508): the distribution's name, its extras in
#: brackets, and the rest, which is the specifier and any marker.
REQUIREMENT = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?(.*)", re.DOTALL)

#: An advisory id, as each database writes its own. GitHub: `GHSA-` and
#: three groups of four from a fixed set of lower-case letters and digits
#: (GitHub Docs, "About the GitHub Advisory database", verified 2026-11-26).
#: CVE: the year and four to nineteen digits (the `cveId` pattern of the CVE
#: record format, github.com/CVEProject/cve-schema, verified 2026-11-26).
#: PyPA's database: `PYSEC-`, the year and a number (the OSV schema's table
#: of prefixes lists `PYSEC`, and its Python example's id is
#: `PYSEC-2021-XXXX`, docs/schema.md, verified 2026-11-26; no document
#: found states the count of digits, so any count passes).
ADVISORY_ID = re.compile(
    r"(?<![A-Za-z0-9-])(?:"
    r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}"
    r"|CVE-[0-9]{4}-[0-9]{4,19}"
    r"|PYSEC-[0-9]{4}-[0-9]+"
    r")(?![A-Za-z0-9-])"
)

#: The form GitHub's own page gives, which its alphabet happens to allow: `x`
#: is one of the twenty characters. Copied into an entry it names nothing.
ADVISORY_PLACEHOLDER = "GHSA-xxxx-xxxx-xxxx"

ENTRY_HEADING = re.compile(r"^## \[([^\]]+)\]")

#: A heading, as Markdown renders one: up to three spaces, one to six `#`,
#: then a space, a tab or the line's end (CommonMark 0.31.2, section 4.2,
#: spec.commonmark.org, CC BY-SA 4.0, verified 2026-11-26).
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]|$)")
SECURITY_HEADING = re.compile(r"^ {0,3}###\s*security\b", re.IGNORECASE)

#: A list item's first line, at any indent, so a nested item is an item
#: too: its indent, then a bullet (`-`, `*` or `+`) or an ordered mark (one
#: to nine digits and `.` or `)`, section 5.2 of the same spec), then the
#: spaces before its text.
LIST_ITEM = re.compile(r"^(\s*)(?:[-*+]|(\d{1,9})([.)]))(\s+)\S")

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
    """One repository, one version. Four packages released together that
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


def canonical(name: str) -> str:
    """A distribution's name as an installer compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def toml_code(line: str) -> str:
    """One TOML line without its comment. A `#` inside a string stays."""
    quote = ""
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "#":
            return line[:i]
        i += 1
    return line


def line_of(lines: list[str], needles: tuple[str, ...], taken: set[int]) -> int | None:
    """The first line holding one of these outside a comment, a line no
    earlier requirement took when there is one. None when no line does."""
    found = [n for n, line in enumerate(lines, 1) if any(needle in toml_code(line) for needle in needles)]
    if not found:
        return None
    fresh = [n for n in found if n not in taken]
    n = (fresh or found)[0]
    taken.add(n)
    return n


def check_floors(root: Path, version: str | None) -> None:
    """Every requirement on an Emet package is `name>=X.Y.Z`, the release.

    An unrelated project, FUSE, took the name `emet-sdk` on PyPI on
    2026-10-30, and its 0.1.3 met the floor `emet-providers` declared then,
    `emet-sdk>=0.1`: installing the providers alone could fetch that package
    in place of Emet's SDK (PyPI, verified 2026-11-26; RELEASE_PROCESS.md
    §10, the 0.5.5 row). So the three packages that import `emet_sdk` name
    it in their base dependencies, and every first-party requirement, base
    or extra, is exactly the name, `>=` and the version the four declare.
    A lower floor, another operator, a marker, a second clause and a bare
    name are all refused, each with the line to write. An extra on another
    Emet package stays in that line; the SDK is named with no extra.

    This holds the floor. The wheel job's dry run checks that no Emet name
    resolves from the index at all; it needs a network, so it runs in CI.
    """
    if not version:
        return
    count = 0
    for pkg in PACKAGES:
        path = root / pkg / "pyproject.toml"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        project = tomllib.loads(text).get("project", {})
        rel = f"{pkg}/pyproject.toml"

        listed = [("its dependencies", r) for r in project.get("dependencies") or []]
        for extra, entries in (project.get("optional-dependencies") or {}).items():
            listed.extend((f"the `{extra}` extra", r) for r in entries or [])

        taken: set[int] = set()
        base: set[str] = set()
        for where, requirement in listed:
            m = REQUIREMENT.match(requirement) if isinstance(requirement, str) else None
            if not m or canonical(m.group(1)) not in PACKAGES:
                continue
            name = canonical(m.group(1))
            extras = "".join((m.group(2) or "").split())
            if where == "its dependencies":
                base.add(name)
            want = f"{name}{'' if name == 'emet-sdk' else extras}>={version}"
            # The string as written first, then any string that starts with
            # the name: a requirement written with TOML escapes has no line
            # that holds it letter for letter. Line 1 when neither is found,
            # so a problem always names a line.
            exact = (f'"{requirement}"', f"'{requirement}'")
            loose = (f'"{m.group(1)}', f"'{m.group(1)}")
            n = line_of(lines, exact, taken) or line_of(lines, loose, taken) or 1
            if f"{m.group(1)}{extras}{''.join(m.group(3).split())}" == want:
                count += 1
                continue
            if name == "emet-sdk" and extras:
                reason = "the SDK is required with no extra; its one extra, dev, holds its own test tools"
            else:
                reason = (
                    "every first-party requirement is the name, \">=\" and the release's own "
                    "version, since an unrelated project's emet-sdk 0.1.3 on PyPI met \"emet-sdk>=0.1\""
                )
            problem(f'{rel}:{n} requires "{requirement}" in {where}. Write "{want}": {reason}.')

        if pkg != "emet-sdk" and "emet-sdk" not in base:
            if "dependencies" not in project:
                n = line_of(lines, ("[project]",), set()) or 1
                problem(
                    f'{rel}:{n} has no dependencies. Write dependencies = ["emet-sdk>={version}"] '
                    f"under [project]: the package imports emet_sdk, and its floor is the release's own version."
                )
                continue
            key = re.compile(r"\s*dependencies\s*=")
            n = next((i for i, line in enumerate(lines, 1) if key.match(toml_code(line))), None)
            n = n or line_of(lines, ("[project]",), set()) or 1
            problem(
                f'{rel}:{n} names no emet-sdk in its dependencies. Write "emet-sdk>={version}" '
                f"there: the package imports emet_sdk, and its floor is the release's own version."
            )
    if count:
        notes.append(f"{count} first-party floor(s), each >={version}")


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


def check_plugin_readmes(root: Path) -> None:
    """Every entry point a package registers is named in that package's README.

    0.3 shipped seven new plugins and emet-hal's "what ships" table kept
    listing the four from 0.2, through several rounds of "what is left".
    A newcomer reads that table before anything else. emet-providers has the
    same table and the same exposure, so the rule covers every package.
    """
    for pkg in PACKAGES:
        pyproject = root / pkg / "pyproject.toml"
        readme = root / pkg / "README.md"
        if not pyproject.exists() or not readme.exists():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        text = readme.read_text(encoding="utf-8")
        for group, entries in (data.get("project", {}).get("entry-points") or {}).items():
            for name in entries:
                if f"`{name}`" not in text:
                    problem(
                        f"{pkg}/README.md does not name the {group} entry point `{name}`. "
                        f"The 'what ships' table is the first thing a contributor reads."
                    )


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


def check_changelog(root: Path, version: str | None) -> None:
    """The declared version has a changelog entry with a date and a summary.

    Every merge to master is a version and every version is an entry, so a
    pull request that bumps the four pyproject files without writing the
    entry fails here, on the pull request, rather than at the tag, where the
    person is holding a merge commit that cannot be amended without another
    pull request. The summary line under the heading is the tag's first line
    and the commit title on master, so it has to be there too.
    """
    if not version:
        return
    path = root / "CHANGELOG.md"
    if not path.exists():
        problem("CHANGELOG.md is missing")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    heading = re.compile(r"^## \[" + re.escape(version) + r"\] - \d{4}-\d{2}-\d{2}\s*$")
    for i, line in enumerate(lines):
        if not heading.match(line):
            continue
        after = [entry.strip() for entry in lines[i + 1 : i + 4] if entry.strip()]
        if not after or after[0].startswith(("#", "-", "[")):
            problem(
                f"CHANGELOG.md: the {version} entry needs a one-line summary under its "
                f"heading. It is the tag's first line and the commit title on master."
            )
        else:
            notes.append(f"changelog {version}: {after[0]}")
        return
    problem(
        f"CHANGELOG.md has no entry `## [{version}] - YYYY-MM-DD`. Every merge to "
        f"master is a version, and every version is an entry."
    )


def check_security_sections(root: Path, version: str | None) -> None:
    """Each line of a `### Security` section names its advisory id.

    An entry may hold a Security section beside Added, Changed, Fixed and
    Deprecated (Keep a Changelog 1.1.0, MIT: Security, "in case of
    vulnerabilities"). SECURITY.md promises that the entry which ships a fix
    names the advisory, so a person who reads "fixed a vulnerability" can
    find out which one and whether their robot had it. A line with no id
    breaks that promise quietly, so it is refused here, and so is a
    section with no line at all. Every entry is read, since a tag carries
    its entry for good.

    A line is a list item with the lines that continue it, read as
    Markdown renders it (CommonMark 0.31.2, sections 4.2, 5.2 and 5.3). A
    bullet or a numbered item at any indent is its own line, a nested one
    included. A numbered line straight after another line starts an item
    only when Markdown would start one there: at 1, or as the next item of
    a numbered list. A later paragraph indented under an item is part of
    it. Any other text in the section is refused, save one paragraph after
    the list when the entry's next heading follows it: the entry's closing
    "Verified on" line, which needs no id.
    """
    path = root / "CHANGELOG.md"
    if not path.exists():
        return                                         # check_changelog says so
    lines = path.read_text(encoding="utf-8").splitlines()
    entry: str | None = None
    i = 0
    while i < len(lines):
        heading = ENTRY_HEADING.match(lines[i])
        if heading:
            entry = heading.group(1)
        if not SECURITY_HEADING.match(lines[i]):
            i += 1
            continue
        where = f"the {entry} entry" if entry else "the changelog"
        at = i + 1
        items: list[list] = []                         # each item's first line, and its text
        paragraphs: list[int] = []                     # the first line of each paragraph outside the list
        stack: list[tuple[int, int, str, int]] = []    # open items: mark indent, text indent, delimiter, index
        open_text: tuple[str, int] | None = None       # the paragraph a next line would continue
        i += 1
        while i < len(lines) and not HEADING.match(lines[i]):
            line = lines[i]
            i += 1
            if not line.strip():
                open_text = None
                continue
            indent = len(line) - len(line.lstrip())
            mark = LIST_ITEM.match(line)
            if mark and mark.group(2) and open_text is not None and int(mark.group(2)) != 1:
                # A numbered line inside a paragraph starts an item only as
                # the next of a numbered list with the same delimiter.
                sibling = [s for s in stack if s[0] <= indent < s[1] and s[2] == mark.group(3)]
                mark = mark if sibling else None
            if mark:
                spaces = len(mark.group(4))
                width = len(mark.group(2) or "") + 1
                while stack and stack[-1][1] > indent:
                    stack.pop()
                items.append([i, line])
                text_indent = indent + width + (spaces if spaces <= 4 else 1)
                stack.append((indent, text_indent, mark.group(3) or "", len(items) - 1))
                open_text = ("item", len(items) - 1)
                continue
            if open_text is None:
                while stack and stack[-1][1] > indent:
                    stack.pop()
                if stack:
                    open_text = ("item", stack[-1][3])
                else:
                    paragraphs.append(i)
                    open_text = ("outside", len(paragraphs) - 1)
                    continue
            if open_text[0] == "item":
                items[open_text[1]][1] += f" {line.strip()}"
        ends_entry = i >= len(lines) or len(HEADING.match(lines[i]).group(1)) <= 2
        if not items:
            problem(
                f'CHANGELOG.md:{at} is a "### Security" heading with no list item under it, in '
                f'{where}. Write each fix as a "- " item with its advisory id, or take the heading out.'
            )
            continue
        closing = paragraphs[-1] if paragraphs and ends_entry and paragraphs[-1] > items[-1][0] else None
        for n in paragraphs:
            if n != closing:
                problem(
                    f"CHANGELOG.md:{n} is text in the Security section of {where} outside its list. "
                    f'Write each fix as a "- " item with its advisory id. Only the entry\'s closing '
                    f"paragraph may follow the list."
                )
        named: list[str] = []
        for n, text in items:
            ids = [m for m in ADVISORY_ID.findall(text) if m != ADVISORY_PLACEHOLDER]
            if not ids:
                problem(
                    f"CHANGELOG.md:{n} is a Security line of {where} that names no advisory id. "
                    f"Write the id the advisory was given in the line: GHSA- and three groups of "
                    f"four characters, CVE-YYYY-NNNN, or PYSEC-YYYY-NNNN."
                )
            named.extend(ids)
        if entry == version and named:
            notes.append(f"changelog {version}: {len(items)} security line(s), naming {', '.join(named)}")


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
    check_floors(root, version)
    check_single_declaration(root)
    check_groups(root)
    check_docs(root, version)
    check_plugin_readmes(root)
    check_changelog(root, version)
    check_security_sections(root, version)
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
