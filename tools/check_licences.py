#!/usr/bin/env python3
"""Hold every installed dependency's licence to the allowed list (BVP1).

    python tools/check_licences.py
        every distribution installed for the interpreter that runs this
    python tools/check_licences.py --table
        the same, with the whole table printed in Markdown
    python tools/check_licences.py --requirements FILE --python EXE [--table]
        a tools venv's pinned list, read from the venv EXE belongs to

**What is read.** Each distribution's own metadata: `License-Expression`
first, an SPDX expression; then the free-text `License` field, when it
names a licence this script knows; then the `License ::` classifiers.
PEP 639 (https://peps.python.org/pep-0639/, Final, verified 2026-11-25 by
the survey) puts `License-Expression` first and makes it override
`License`, and deprecates the classifiers because they cannot say which
variant or version is meant. Reading the `License` field before the
classifiers is this script's choice. Several classifiers are read as all
of them, joined by AND; the bare `OSI Approved` beside another names no
licence and is dropped. A family with no variant or version (the `BSD
License` or `Apache Software License` classifier, a `License` field of
`BSD`) is read as that family and marked so, with where it was read. A
distribution that declares nothing readable is undeclared.

**What passes.** `tools/licences.json` holds the allowed list as the
maintainer decided it under E55 on 2026-11-26: Apache-2.0, MIT,
BSD-2-Clause, BSD-3-Clause, ISC, PSF-2.0 and MIT-0. An expression passes
when every licence an AND joins is allowed, and when one side of an OR is.
A family names no variant, and an undeclared distribution names nothing,
so either passes only through the file's `read` list: the licence a person
read in the distribution's own licence file, and where. A `read` entry
holds while the metadata says what it said when the file was read, and the
licence it gives is then judged like a declared one. Anything outside the
allowed list needs a named exception in the same file, giving the package,
the licence, the paths that bring it (the base install, or an extra of a
first-party package) and the reason. An exception holds only while the
distribution declares exactly that licence, its ANDs and ORs in any order,
and only for the paths it names: a GPL package an exception allows through
an extra is refused when the base install brings it, and when nothing
first-party brings it. An exception or a `read` entry whose distribution
is not installed is printed as a note. The four first-party packages are
skipped by name.

**A tools venv.** `--requirements` reads a pinned list such as
`tools/bank/requirements.txt` by D9's rule, the same list with MPL-2.0
allowed (E55 (c)), with the file's `tools_exceptions` and its `read` list.
The licences come from the venv that has the list installed, named with
`--python`, since a pin's licence is only known once pip has resolved it.
Every distribution in that venv is judged, a pin or not. With no
`--python` the script prints the table it would check and exits 2, "not
run".

**Where the idea comes from.** OpenVoiceOS's reusable `license-check.yml`
workflow (https://github.com/OpenVoiceOS/gh-automations, `dev` branch,
Apache-2.0, verified 2026-11-26) fails a run whose installed dependencies
carry a strong, network or weak copyleft licence, or one it cannot read;
allows MPL-2.0 by default; and takes named package exceptions. The idea is
taken and no code is. OpenVoiceOS reads the licences with `pip-licenses`;
this script reads the same metadata with the standard library. See
CITATIONS.md.

Standard library only, like `tools/check_layering.py`.

Exit: 0 every licence allowed; 1 a licence outside the list with no
exception, a family or an undeclared licence with no `read` entry, or an
exception or a `read` entry that no longer matches; 2 not run, or a usage
or file error.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

TOOLS = Path(__file__).resolve().parent
DEFAULT_POLICY = TOOLS / "licences.json"

# ------------------------------------------------------------------ SPDX

#: SPDX identifiers this script prints in their canonical spelling, keyed
#: in lower case. An identifier outside the table keeps the spelling it was
#: given, and no allowed list names one, so it is refused. The deprecated
#: bare GPL forms map to their `-only` names, and a trailing `+` to
#: `-or-later`, as the SPDX list itself does.
CANONICAL = {
    name.lower(): name
    for name in (
        "0BSD", "AFL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "Apache-1.1", "Apache-2.0",
        "Artistic-2.0", "BSD-1-Clause", "BSD-2-Clause", "BSD-3-Clause", "BSD-3-Clause-Clear",
        "BSD-4-Clause", "BSL-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "CDDL-1.0", "EPL-1.0",
        "EPL-2.0", "EUPL-1.2", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
        "HPND", "ISC", "LGPL-2.0-only", "LGPL-2.0-or-later", "LGPL-2.1-only", "LGPL-2.1-or-later",
        "LGPL-3.0-only", "LGPL-3.0-or-later", "MIT", "MIT-0", "MIT-CMU", "MPL-1.1", "MPL-2.0",
        "MPL-2.0-no-copyleft-exception", "NCSA", "OFL-1.1", "PSF-2.0", "Python-2.0", "Unicode-3.0",
        "Unicode-DFS-2016", "Unlicense", "UPL-1.0", "WTFPL", "X11", "Zlib", "ZPL-2.1",
    )
}
for _old in ("GPL-2.0", "GPL-3.0", "LGPL-2.0", "LGPL-2.1", "LGPL-3.0", "AGPL-3.0"):
    CANONICAL[_old.lower()] = f"{_old}-only"

#: Free text in a `License` field, folded to lower case with runs of space
#: and punctuation made one space, and the SPDX reading of each. Only text
#: that names exactly one licence is here; a field holding a licence's whole
#: text is read through the classifiers instead.
LICENSE_TEXT = {
    "mit": "MIT", "mit license": "MIT", "the mit license": "MIT", "mit licence": "MIT",
    "expat": "MIT", "mit no attribution": "MIT-0",
    "apache 2 0": "Apache-2.0", "apache 2": "Apache-2.0", "apache2": "Apache-2.0",
    "apache license 2 0": "Apache-2.0", "apache license version 2 0": "Apache-2.0",
    "apache software license 2 0": "Apache-2.0", "apache license v2 0": "Apache-2.0",
    "apache software license": "Apache", "apache license": "Apache", "apache": "Apache",
    "bsd": "BSD", "bsd license": "BSD", "bsd licence": "BSD",
    "3 clause bsd license": "BSD-3-Clause", "3 clause bsd": "BSD-3-Clause", "bsd 3 clause": "BSD-3-Clause",
    "bsd 3 clause license": "BSD-3-Clause", "new bsd": "BSD-3-Clause", "new bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause", "bsd 3": "BSD-3-Clause",
    "2 clause bsd license": "BSD-2-Clause", "2 clause bsd": "BSD-2-Clause", "bsd 2 clause": "BSD-2-Clause",
    "bsd 2 clause license": "BSD-2-Clause", "simplified bsd": "BSD-2-Clause", "freebsd": "BSD-2-Clause",
    "isc": "ISC", "isc license": "ISC", "iscl": "ISC",
    "psf": "PSF-2.0", "psfl": "PSF-2.0", "psf license": "PSF-2.0", "psf 2 0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl 2 0": "MPL-2.0", "mozilla public license 2 0": "MPL-2.0", "mozilla public license 2 0 mpl 2 0": "MPL-2.0",
    "gpl": "GPL", "gplv2": "GPL-2.0-only", "gplv3": "GPL-3.0-only", "gplv3+": "GPL-3.0-or-later",
    "lgpl": "LGPL", "agpl": "AGPL", "public domain": "Public-Domain",
}

#: The `License ::` classifiers, after that prefix, and the reading of each.
#: `BSD`, `Apache`, `GPL`, `LGPL` and `OSI Approved` are families with no
#: variant or version: the classifier cannot say more.
CLASSIFIERS = {
    "OSI Approved": "OSI-Approved",
    "OSI Approved :: MIT License": "MIT",
    "OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "OSI Approved :: Apache Software License": "Apache",
    "OSI Approved :: BSD License": "BSD",
    "OSI Approved :: ISC License (ISCL)": "ISC",
    "OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "OSI Approved :: Zero-Clause BSD (0BSD)": "0BSD",
    "OSI Approved :: zlib/libpng License": "Zlib",
    "OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "OSI Approved :: Mozilla Public License 1.1 (MPL 1.1)": "MPL-1.1",
    "OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "OSI Approved :: Eclipse Public License 2.0 (EPL-2.0)": "EPL-2.0",
    "OSI Approved :: GNU General Public License (GPL)": "GPL",
    "OSI Approved :: GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "OSI Approved :: GNU General Public License v2 or later (GPLv2+)": "GPL-2.0-or-later",
    "OSI Approved :: GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "OSI Approved :: GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "OSI Approved :: GNU Library or Lesser General Public License (LGPL)": "LGPL",
    "OSI Approved :: GNU Lesser General Public License v2 (LGPLv2)": "LGPL-2.0-only",
    "OSI Approved :: GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    "OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0-or-later",
    "OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0-only",
    "OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)": "AGPL-3.0-or-later",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
    "Public Domain": "Public-Domain",
    "Other/Proprietary License": "Proprietary",
    "Freeware": "Freeware",
}

#: Families a classifier or a `License` field can name, each with its name
#: as the table prints it and what it leaves unsaid. No allowed list names
#: one: a family passes only through a `read` entry.
FAMILY_NAMES = {
    "BSD": ("BSD", "no variant"),
    "Apache": ("Apache", "no version"),
    "GPL": ("GPL", "no version"),
    "LGPL": ("LGPL", "no version"),
    "AGPL": ("AGPL", "no version"),
    "OSI-Approved": ("OSI Approved", "no licence named"),
}

#: What a source is called beside a family read from it.
FAMILY_SOURCES = {"classifiers": "classifier", "License": "License field"}

UNDECLARED = "undeclared"
#: The installers a Python interpreter brings with it. GitHub's Python 3.11
#: carries setuptools 79.0.1, whose metadata declares no licence, though no
#: Emet package asks for it. One of these is the machine's and is skipped,
#: unless a first-party package brings it, when it is judged like any other.
INSTALLERS = frozenset({"pip", "setuptools", "wheel"})
#: A `read` entry's `declares` for a distribution whose metadata names nothing.
NOTHING = "nothing"


def family_text(name: str, source: str) -> str:
    shown, missing = FAMILY_NAMES[name]
    where = FAMILY_SOURCES.get(source)
    return f"{shown} ({where}, {missing})" if where else f"{shown} ({missing})"


class ExpressionError(ValueError):
    """An SPDX expression this script cannot parse."""


@dataclass(frozen=True)
class Node:
    """One node of a parsed licence expression: `op` is "id", "and" or "or"."""

    op: str
    name: str = ""
    parts: tuple["Node", ...] = ()

    def ids(self) -> list[str]:
        if self.op == "id":
            return [self.name]
        return [i for part in self.parts for i in part.ids()]

    def text(self, top: bool = True) -> str:
        if self.op == "id":
            return self.name
        inner = f" {self.op.upper()} ".join(part.text(top=False) for part in self.parts)
        return inner if top else f"({inner})"

    def key(self) -> str:
        """The expression in one canonical order, to compare two by: the
        parts of an AND or an OR sorted, and an AND inside an AND (an OR
        inside an OR) flattened, so `MPL-2.0 AND MIT` is `MIT AND MPL-2.0`."""
        if self.op == "id":
            return self.name
        return f"{self.op}(" + ",".join(sorted(part.key() for part in self.flat())) + ")"

    def flat(self) -> Iterable["Node"]:
        """The parts, with a part of the same operator opened into its own."""
        for part in self.parts:
            if part.op == self.op:
                yield from part.flat()
            else:
                yield part


def canonical_id(token: str) -> str:
    """One identifier in its SPDX spelling. `WITH` exceptions are judged by
    the licence they modify, so the caller has already dropped them."""
    plus = token.endswith("+")
    base = token[:-1] if plus else token
    name = CANONICAL.get(base.lower(), base)
    if plus and name.endswith("-only"):
        name = name[: -len("-only")] + "-or-later"
    elif plus and not name.endswith("-or-later"):
        name = name + "-or-later"
    return name


def parse_expression(text: str) -> Node:
    """An SPDX licence expression: identifiers joined by AND and OR, with
    parentheses, AND binding tighter, and `WITH <exception>` kept to its
    licence. Raises ExpressionError on anything else."""
    tokens = re.findall(r"\(|\)|[^\s()]+", text)
    if not tokens:
        raise ExpressionError("empty")
    pos = 0

    def peek() -> str:
        return tokens[pos] if pos < len(tokens) else ""

    def take() -> str:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def atom() -> Node:
        token = take() if pos < len(tokens) else ""
        if token == "(":
            node = either()
            closing = take() if pos < len(tokens) else ""
            if closing != ")":
                raise ExpressionError(f"unbalanced parenthesis in {text!r}")
            return node
        if not token or token.upper() in ("AND", "OR", "WITH") or token == ")":
            raise ExpressionError(f"a licence was expected in {text!r}")
        if not re.fullmatch(r"[A-Za-z0-9.+-]+(:[A-Za-z0-9.+-]+)?", token):
            raise ExpressionError(f"{token!r} is no SPDX identifier")
        if peek().upper() == "WITH":
            take()
            if not peek() or peek() in "()":
                raise ExpressionError(f"WITH needs an exception in {text!r}")
            take()
        return Node("id", canonical_id(token))

    def both() -> Node:
        parts = [atom()]
        while peek().upper() == "AND":
            take()
            parts.append(atom())
        return parts[0] if len(parts) == 1 else Node("and", parts=tuple(parts))

    def either() -> Node:
        parts = [both()]
        while peek().upper() == "OR":
            take()
            parts.append(both())
        return parts[0] if len(parts) == 1 else Node("or", parts=tuple(parts))

    node = either()
    if pos != len(tokens):
        raise ExpressionError(f"unexpected {tokens[pos]!r} in {text!r}")
    return node


def passes(node: Node, allowed: set[str]) -> bool:
    if node.op == "id":
        return node.name in allowed
    if node.op == "and":
        return all(passes(part, allowed) for part in node.parts)
    return any(passes(part, allowed) for part in node.parts)


def outside(node: Node, allowed: set[str]) -> list[str]:
    """The identifiers that keep `node` from passing: every refused one an
    AND needs, and for an OR, those of its least refused side."""
    if node.op == "id":
        return [] if node.name in allowed else [node.name]
    if node.op == "and":
        return [i for part in node.parts for i in outside(part, allowed)]
    sides = [outside(part, allowed) for part in node.parts]
    return min(sides, key=len)


# --------------------------------------------------------------- reading


@dataclass(frozen=True)
class Reading:
    """A distribution's licence as read from its metadata."""

    source: str              # "License-Expression", "License", "classifiers", "none", "not read" or "read"
    node: Node | None        # None when nothing readable was declared
    raw: str = ""            # what the metadata held; for "read", what it reads as

    @property
    def text(self) -> str:
        if self.source == "not read":
            return "not read"
        if self.node is None:
            return UNDECLARED if not self.raw else f"unreadable: {shorten(self.raw)}"
        if self.source == "read":
            return f"{self.node.text()} (read by hand; its metadata: {self.raw})"
        if self.node.op == "id" and self.node.name in FAMILY_NAMES:
            return family_text(self.node.name, self.source)
        text = self.node.text()
        return f"{text} (classifiers)" if self.source == "classifiers" and self.node.op != "id" else text

    @property
    def family(self) -> str:
        """The family this reading is, or ''."""
        node = self.node
        return node.name if node is not None and node.op == "id" and node.name in FAMILY_NAMES else ""

    def declares(self, what: str) -> bool:
        """Whether the metadata says what a `read` entry's `declares` holds:
        a family's name, `nothing`, or the metadata's own text."""
        if what in FAMILY_NAMES:
            return self.family == what
        if self.node is not None:
            return False
        if what == NOTHING:
            return not self.raw
        return " ".join(self.raw.split()) == " ".join(what.split())


def shorten(text: str, limit: int = 60) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 3] + "..."


def fold(text: str) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", text.lower()).strip()


def read_licence(expression: str | None, licence: str | None, classifiers: Iterable[str]) -> Reading:
    """The expression first, which PEP 639 makes override the field; then
    the field, then the classifiers, which is this script's order."""
    if expression and expression.strip():
        try:
            return Reading("License-Expression", parse_expression(expression.strip()), expression)
        except ExpressionError:
            return Reading("License-Expression", None, expression)
    field_text = (licence or "").strip()
    if field_text and field_text.upper() not in ("UNKNOWN", "NONE"):
        known = LICENSE_TEXT.get(fold(field_text))
        if known:
            return Reading("License", Node("id", known), field_text)
        if len(field_text) <= 80 and "\n" not in field_text:
            try:
                node = parse_expression(field_text)
            except ExpressionError:
                node = None
            if node is not None and all(i in CANONICAL.values() for i in node.ids()):
                return Reading("License", node, field_text)
    names = []
    for classifier in classifiers:
        if not classifier.startswith("License ::"):
            continue
        rest = classifier[len("License ::"):].strip()
        names.append(CLASSIFIERS.get(rest, rest))
    names = sorted(set(names))
    if len(names) > 1 and "OSI-Approved" in names:
        # The bare parent classifier names no licence; beside a child it
        # adds nothing to read.
        names.remove("OSI-Approved")
    if names:
        # Several classifiers are read as all of them: PEP 639 says they are
        # ambiguous, and AND is the reading that can only refuse more.
        parts = tuple(Node("id", n) for n in names)
        return Reading("classifiers", parts[0] if len(parts) == 1 else Node("and", parts=parts),
                       "; ".join(names))
    return Reading("none", None, field_text if field_text.upper() not in ("UNKNOWN", "NONE") else "")


# ---------------------------------------------------------- distributions


def normalise(name: str) -> str:
    """PEP 503's form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Record:
    """One installed distribution, as this script needs it."""

    name: str
    version: str
    reading: Reading
    requires: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return normalise(self.name)


def record_from(dist: Any) -> Record | None:
    """A Record from an importlib Distribution, or any object with its
    `metadata` (`get`, `get_all`) and `version`."""
    meta = dist.metadata
    name = meta.get("Name")
    if not name:
        return None
    reading = read_licence(meta.get("License-Expression"), meta.get("License"), meta.get_all("Classifier") or [])
    return Record(
        name=name,
        version=str(dist.version),
        reading=reading,
        requires=list(meta.get_all("Requires-Dist") or []),
        extras=list(meta.get_all("Provides-Extra") or []),
    )


def installed(dists: Iterable[Any] | None = None) -> dict[str, Record]:
    """Every distribution once, by normalised name. importlib yields them in
    `sys.path` order, so the first of two copies is the one that imports."""
    out: dict[str, Record] = {}
    for dist in importlib.metadata.distributions() if dists is None else dists:
        rec = record_from(dist)
        if rec is not None and rec.key not in out:
            out[rec.key] = rec
    return out


# ---------------------------------------------------------------- markers


def marker_environment() -> dict[str, str]:
    """PEP 508's marker variables for the interpreter that runs this."""
    impl = sys.implementation
    version = impl.version
    impl_version = f"{version.major}.{version.minor}.{version.micro}"
    if version.releaselevel != "final":
        impl_version += version.releaselevel[0] + str(version.serial)
    return {
        "implementation_name": impl.name,
        "implementation_version": impl_version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    }


_MARKER_TOKEN = re.compile(r"\s*(\(|\)|'[^']*'|\"[^\"]*\"|===|==|!=|<=|>=|~=|<|>|not\s+in\b|in\b|and\b|or\b|[A-Za-z_][A-Za-z0-9_.]*)")
_VERSION_VARS = {"python_version", "python_full_version", "implementation_version"}


#: PEP 440's prefix match: a release segment, then `.*`.
_WILDCARD = re.compile(r"(\d+(?:\.\d+)*)\.\*")


def _version_tuple(text: str) -> tuple[int, ...] | None:
    m = re.match(r"(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _prefix_match(candidate: str, wildcard: str) -> bool:
    """PEP 440's `== X.Y.*` (https://peps.python.org/pep-0440/#version-matching,
    read 2026-11-26), which `!=` inverts: the candidate's release segment,
    padded with zeros to the prefix's length and cut to it, equals the
    prefix. What follows the release segment is ignored, so 3.13.0rc1 is in
    3.13.* and 3.130.1 is not in 3.13.*. Raises ValueError, which leaves
    the marker unread, for a prefix with more than a release segment or a
    candidate that is no version."""
    m = _WILDCARD.fullmatch(wildcard.strip())
    have = _version_tuple(candidate)
    if m is None or have is None:
        raise ValueError(wildcard)
    want = tuple(int(p) for p in m.group(1).split("."))
    have = have + (0,) * (len(want) - len(have))
    return have[: len(want)] == want


def _compare(left: str, op: str, right: str, versions: bool) -> bool:
    if op in ("in", "not in"):
        found = left in right
        return found if op == "in" else not found
    if versions:
        if op in ("==", "!=") and right.strip().endswith(".*"):
            return _prefix_match(left, right) == (op == "==")
        # PEP 440 allows a wildcard with == and != only. Anywhere else it is
        # no version, so the strings are compared below.
        a = None if left.strip().endswith(".*") else _version_tuple(left)
        b = None if right.strip().endswith(".*") else _version_tuple(right)
        if a is not None and b is not None:
            # PEP 440's compatible release: `~= 3.13` is `>= 3.13, == 3.*`,
            # so the prefix is the right side as written, less its last part.
            n = len(b)
            width = max(len(a), len(b))
            a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
            if op == "~=":
                if n < 2:
                    raise ValueError(right)  # PEP 440 needs two parts; the marker is left unread
                return a >= b and a[: n - 1] == b[: n - 1]
            return {"==": a == b, "===": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    if op in ("==", "===", "~="):
        return left == right
    if op == "!=":
        return left != right
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]


def evaluate_marker(marker: str, env: dict[str, str]) -> bool | None:
    """PEP 508's environment markers, enough of them for Requires-Dist:
    variables, quoted strings, the comparison operators, `in`, `not in`,
    `and`, `or` and parentheses. A version compared with `==` or `!=` to
    one ending `.*` is PEP 440's prefix match. None when the marker cannot
    be read."""
    tokens: list[str] = []
    pos = 0
    text = marker.strip()
    while pos < len(text):
        m = _MARKER_TOKEN.match(text, pos)
        if not m:
            return None
        tokens.append(re.sub(r"\s+", " ", m.group(1)))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    i = 0

    def value() -> tuple[str, str]:
        nonlocal i
        token = tokens[i]
        i += 1
        if token[:1] in ("'", '"'):
            return token[1:-1], ""
        if token not in env and token != "extra":
            raise ValueError(token)
        return env.get(token, ""), token

    def comparison() -> bool:
        nonlocal i
        if tokens[i] == "(":
            i += 1
            result = disjunction()
            if tokens[i] != ")":
                raise ValueError(")")
            i += 1
            return result
        left, left_var = value()
        op = tokens[i]
        i += 1
        right, right_var = value()
        if "extra" in (left_var, right_var):
            return _compare(normalise(left), op, normalise(right), False)
        return _compare(left, op, right, bool(_VERSION_VARS & {left_var, right_var}))

    def conjunction() -> bool:
        nonlocal i
        result = comparison()
        while i < len(tokens) and tokens[i] == "and":
            i += 1
            result = comparison() and result
        return result

    def disjunction() -> bool:
        nonlocal i
        result = conjunction()
        while i < len(tokens) and tokens[i] == "or":
            i += 1
            result = conjunction() or result
        return result

    try:
        result = disjunction()
    except (IndexError, KeyError, ValueError):
        return None
    return result if i == len(tokens) else None


_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;]*?)\s*(?:;\s*(.*))?$")


def parse_requirement(text: str) -> tuple[str, list[str], str, str] | None:
    """Requires-Dist's name, extras, version and marker."""
    m = _REQUIREMENT.match(text)
    if not m:
        return None
    extras = [normalise(e) for e in (m.group(2) or "").strip("[]").split(",") if e.strip()]
    return normalise(m.group(1)), extras, m.group(3).strip(), (m.group(4) or "").strip()


# ------------------------------------------------------------------ walk


def edges(records: dict[str, Record], env: dict[str, str]) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """Which installed distribution requires which, as nodes of a name and
    an extra ("" for the base install). A requirement whose marker is true
    for an extra and false without it belongs to that extra. A marker that
    cannot be read counts as true: the walk only says who brings what."""
    out: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for rec in records.values():
        extras = {normalise(e) for e in rec.extras}
        parsed = [p for p in (parse_requirement(r) for r in rec.requires) if p]
        for _name, _x, _v, marker in parsed:
            for m in re.finditer(r"extra\s*==\s*['\"]([^'\"]+)['\"]", marker):
                extras.add(normalise(m.group(1)))
        for extra in ["", *sorted(extras)]:
            for name, wanted, _version, marker in parsed:
                if name not in records:
                    continue
                here = evaluate_marker(marker, {**env, "extra": extra}) if marker else True
                if here is False:
                    continue
                if extra:
                    base = evaluate_marker(marker, {**env, "extra": ""}) if marker else True
                    if base is not False:
                        continue
                targets = out.setdefault((rec.key, extra), set())
                targets.add((name, ""))
                targets.update((name, x) for x in wanted)
    return out


def live(graph: dict[tuple[str, str], set[tuple[str, str]]], records: dict[str, Record],
         extras_of: Iterable[str] = ()) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """The edges that can bring anything: from each installed distribution's
    base install, from every extra of the packages in `extras_of` (the
    first-party ones, whose extras the install line names), and from an
    extra something live asks for. An extra nobody asked for brings
    nothing, so `pytest[dev]` does not make pytest's test tools installed."""
    seeds = {(key, "") for key in records}
    for key in extras_of:
        seeds.update(src for src in graph if src[0] == key)
    reached = set(seeds)
    queue = deque(sorted(seeds))
    while queue:
        node = queue.popleft()
        for dst in sorted(graph.get(node, ())):
            if dst not in reached:
                reached.add(dst)
                queue.append(dst)
    return {src: dsts for src, dsts in graph.items() if src in reached}


def node_label(records: dict[str, Record], node: tuple[str, str]) -> str:
    name = records[node[0]].name if node[0] in records else node[0]
    return f"{name}[{node[1]}]" if node[1] else name


def brought_by(records: dict[str, Record], graph: dict[tuple[str, str], set[tuple[str, str]]],
               target: str, roots: set[str]) -> tuple[list[str], list[str]]:
    """The root nodes that bring `target`, each as `name[extra]` or a bare
    name for a base install, and one shortest chain from each, `a > b > c`.
    `roots` are the normalised names a walk stops at. It stops at a root's
    base install; at a root's extra it records the extra and goes on, since
    whatever asks for that extra brings the target too."""
    reverse: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for src, dsts in graph.items():
        for dst in dsts:
            reverse.setdefault(dst, set()).add(src)
    start = (target, "")
    previous: dict[tuple[str, str], tuple[str, str] | None] = {start: None}
    queue = deque([start])
    found: list[tuple[str, str]] = []
    while queue:
        node = queue.popleft()
        if node != start and node[0] in roots:
            found.append(node)
            if not node[1]:
                continue
        for src in sorted(reverse.get(node, ())):
            if src not in previous:
                previous[src] = node
                queue.append(src)
    labels, chains = [], []
    for node in sorted(found, key=lambda n: node_label(records, n).lower()):
        names: list[str] = []
        step: tuple[str, str] | None = node
        while step is not None:
            first_party_extra = step[0] in roots and step[1]
            label = node_label(records, step) if step == node or first_party_extra else records[step[0]].name
            if not names or names[-1] != label:
                names.append(label)
            step = previous[step]
        labels.append(node_label(records, node))
        chains.append(" > ".join(names))
    return labels, chains


def direct_dependents(records: dict[str, Record], graph: dict[tuple[str, str], set[tuple[str, str]]],
                      target: str) -> list[str]:
    return sorted({records[src[0]].name for src, dsts in graph.items() if (target, "") in dsts and src[0] != target},
                  key=str.lower)


# ---------------------------------------------------------------- policy


@dataclass(frozen=True)
class Exception_:
    package: str
    licence: str             # as written, made canonical
    via: tuple[str, ...]
    reason: str
    recorded: str
    key: str = ""            # the licence's Node.key(), to compare a reading by


@dataclass(frozen=True)
class ReadEntry:
    """The licence a person read for a distribution whose metadata says
    only a family, or nothing."""

    package: str
    declares: str            # what the metadata said when the file was read
    licence: str
    node: Node
    where: str               # the file read, the version and the date


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]
    tools_allowed: frozenset[str]
    first_party: frozenset[str]
    exceptions: dict[str, Exception_]
    tools_exceptions: dict[str, Exception_]
    read: dict[str, ReadEntry] = field(default_factory=dict)


class PolicyError(ValueError):
    """`tools/licences.json` is missing a field or holds the wrong shape."""


def _items(raw: Any, where: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise PolicyError(f"{where} must be a list")
    seen: set[str] = set()
    for n, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PolicyError(f"{where}[{n}] must be an object")
        missing = [k for k in fields if not item.get(k)]
        if missing:
            raise PolicyError(f"{where}[{n}] has no {', '.join(missing)}")
        key = normalise(str(item["package"]))
        if key in seen:
            raise PolicyError(f"{where} names {item['package']} twice")
        seen.add(key)
    return raw


def _licence(item: dict[str, Any], where: str) -> Node:
    try:
        return parse_expression(item["licence"])
    except ExpressionError as exc:
        raise PolicyError(f"{where}.licence: {exc}") from None


def _exceptions(raw: Any, where: str, allowed: frozenset[str]) -> dict[str, Exception_]:
    out: dict[str, Exception_] = {}
    for n, item in enumerate(_items(raw, where, ("package", "licence", "via", "reason", "recorded"))):
        if not isinstance(item["via"], list) or not all(isinstance(v, str) for v in item["via"]):
            raise PolicyError(f"{where}[{n}].via must be a list of `package[extra]` or `package` strings")
        node = _licence(item, f"{where}[{n}]")
        if passes(node, set(allowed)):
            raise PolicyError(f"{where}[{n}]: {node.text()} passes the allowed list on its own, so "
                              f"{item['package']} needs no exception: take it out")
        out[normalise(item["package"])] = Exception_(
            item["package"], node.text(), tuple(normalise_via(v) for v in item["via"]), item["reason"],
            item["recorded"], node.key())
    return out


def _read(raw: Any) -> dict[str, ReadEntry]:
    out: dict[str, ReadEntry] = {}
    for n, item in enumerate(_items(raw, "read", ("package", "declares", "licence", "where"))):
        node = _licence(item, f"read[{n}]")
        if any(i in FAMILY_NAMES for i in node.ids()):
            raise PolicyError(f"read[{n}].licence names a family; give the variant the file holds")
        out[normalise(item["package"])] = ReadEntry(item["package"], str(item["declares"]), node.text(), node,
                                                    item["where"])
    return out


_VIA = re.compile(r"\s*([A-Za-z0-9._-]+)\s*(?:\[\s*([A-Za-z0-9._-]+)\s*\])?\s*")


def normalise_via(text: str) -> str:
    m = _VIA.fullmatch(text)
    if not m:
        raise PolicyError(f"via {text!r} is not `package` or `package[extra]`")
    return normalise(m.group(1)) + (f"[{normalise(m.group(2))}]" if m.group(2) else "")


def via_key(text: str) -> str:
    """A path as an exception's `via` would hold it, or the text itself."""
    m = _VIA.fullmatch(text)
    return normalise_via(text) if m else text.strip().lower()


def load_policy(path: Path = DEFAULT_POLICY) -> Policy:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"{path} does not exist") from None
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path} is no JSON: {exc}") from None
    if not isinstance(data, dict):
        raise PolicyError(f"{path} must hold one object")
    for key in ("allowed", "tools_allowed", "first_party", "read", "exceptions", "tools_exceptions"):
        if key not in data:
            raise PolicyError(f"{path} has no {key!r}")
    if "families" in data:
        raise PolicyError(f"{path} names families, which no longer pass: a family passes through a `read` "
                          f"entry that gives the licence a person read")
    lists = []
    for key in ("allowed", "tools_allowed"):
        names = data[key]
        if not isinstance(names, list) or not all(isinstance(a, str) for a in names):
            raise PolicyError(f"{key} must be a list of SPDX identifiers")
        for name in names:
            if canonical_id(name) != name or name not in CANONICAL.values():
                raise PolicyError(f"{name!r} in {key} is not an SPDX identifier in its canonical spelling")
        lists.append(frozenset(names))
    allowed, tools_allowed = lists
    return Policy(
        allowed=allowed,
        tools_allowed=tools_allowed,
        first_party=frozenset(normalise(n) for n in data["first_party"]),
        exceptions=_exceptions(data["exceptions"], "exceptions", allowed),
        tools_exceptions=_exceptions(data["tools_exceptions"], "tools_exceptions", allowed | tools_allowed),
        read=_read(data["read"]),
    )


# --------------------------------------------------------------- verdicts


@dataclass
class Verdict:
    record: Record
    allowed: bool
    why: str                         # "allowed", the exception's reason, or the refusal
    brought: list[str]
    chains: list[str]
    exception: Exception_ | None = None
    fix: str = ""                    # for a refusal, what a person does next
    entry: ReadEntry | None = None   # the `read` entry the licence came from

    @property
    def brought_text(self) -> str:
        return ", ".join(self.brought) if self.brought else ""


#: What a refusal tells a person to do next.
FIX_OUTSIDE = ("drop the dependency, or ask the maintainer for an exception in tools/licences.json, with its "
               "CITATIONS.md entry")
FIX_READ = ("read the distribution's own licence file and give it an entry in the `read` list of "
            "tools/licences.json, naming the file, the version and the date")
FIX_STALE = "read the licence again and correct tools/licences.json"
FIX_PATH = "keep the package off that path, or ask the maintainer to name the path in the exception's via"


def and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def declared_as(declares: str) -> str:
    """A `read` entry's `declares`, as a refusal names it."""
    if declares in FAMILY_NAMES:
        return family_text(declares, "")
    return NOTHING if declares == NOTHING else f"unreadable text ({shorten(declares)})"


def declared_now(reading: Reading) -> str:
    """What a reading declares, as a refusal names it."""
    if reading.node is None:
        return NOTHING if not reading.raw else f"unreadable text ({shorten(reading.raw)})"
    return reading.text


def judge(rec: Record, allowed: set[str], exceptions: dict[str, Exception_], read: dict[str, ReadEntry],
          brought: list[str], chains: list[str], keys: list[str] | None = None) -> Verdict:
    """One distribution's verdict. `keys` are the via keys of `brought` when
    a label is not one, as a tools list's own pin is labelled "the list"."""
    entry = read.get(rec.key)
    if entry is not None:
        if not rec.reading.declares(entry.declares):
            return Verdict(rec, False, f"its `read` entry in tools/licences.json was written when its metadata "
                                       f"said {declared_as(entry.declares)}, and this version declares "
                                       f"{declared_now(rec.reading)}. The entry is stale", brought, chains,
                           fix=FIX_STALE, entry=entry)
        rec = replace(rec, reading=Reading("read", entry.node, rec.reading.text))
    node = rec.reading.node
    as_read = f"; {entry.licence} as read in {entry.where}" if entry is not None else ""
    exc = exceptions.get(rec.key)
    if exc is not None:
        if node is None:
            return Verdict(rec, False, f"the exception was written for {exc.licence} as declared, and this "
                                       f"version declares {declared_now(rec.reading)}: no licence this script "
                                       f"can read. The exception is stale", brought, chains, exc, fix=FIX_STALE)
        if node.key() != exc.key:
            return Verdict(rec, False, f"the exception was written for {exc.licence}, and this version declares "
                                       f"{node.text()}. The exception is stale", brought, chains, exc,
                           fix=FIX_STALE, entry=entry)
        if not brought:
            return Verdict(rec, False, f"nothing first-party brings it, so no path the exception names applies "
                                       f"(it names {', '.join(exc.via)})", brought, chains, exc, fix=FIX_PATH,
                           entry=entry)
        via = keys if keys is not None else [via_key(b) for b in brought]
        uncovered = [b for b, k in zip(brought, via) if k not in exc.via]
        if uncovered:
            return Verdict(rec, False, f"brought by {', '.join(uncovered)}, which the exception does not name "
                                       f"(it names {', '.join(exc.via)})", brought, chains, exc, fix=FIX_PATH,
                           entry=entry)
        return Verdict(rec, True, f"exception: {exc.reason}{as_read}", brought, chains, exc, entry=entry)
    if node is None:
        return Verdict(rec, False, "no licence can be read from its metadata, and no `read` entry in "
                                   "tools/licences.json gives the licence a person read", brought, chains,
                       fix=FIX_READ)
    if passes(node, allowed):
        return Verdict(rec, True, f"allowed{as_read.replace('; ', ': ', 1)}", brought, chains, entry=entry)
    refused = sorted(set(outside(node, allowed)), key=str.lower)
    if all(r in FAMILY_NAMES for r in refused):
        names = "it" if node.op == "id" else and_list([family_text(r, rec.reading.source) for r in refused])
        says = "says" if names == "it" or len(refused) == 1 else "say"
        return Verdict(rec, False, f"{names} {says} no more than a family, and no `read` entry in "
                                   f"tools/licences.json gives the licence a person read", brought, chains,
                       fix=FIX_READ)
    shown = [family_text(r, rec.reading.source) if r in FAMILY_NAMES else r for r in refused]
    what = "it is" if node.op == "id" else f"{and_list(shown)} {'is' if len(shown) == 1 else 'are'}"
    return Verdict(rec, False, f"{what} outside the allowed list, and no exception in tools/licences.json "
                               f"names {rec.name}", brought, chains, fix=FIX_OUTSIDE, entry=entry)


def judge_installed(records: dict[str, Record], policy: Policy, env: dict[str, str]) -> list[Verdict]:
    graph = live(edges(records, env), records, policy.first_party)
    verdicts = []
    for key in sorted(records, key=str.lower):
        rec = records[key]
        if key in policy.first_party:
            continue
        labels, chains = brought_by(records, graph, key, set(policy.first_party))
        if not labels and key in INSTALLERS:
            continue
        if not labels:
            users = direct_dependents(records, graph, key)
            chains = [f"required by {', '.join(users)}, which no first-party package brings"] if users else \
                ["nothing installed requires it"]
        verdicts.append(judge(rec, set(policy.allowed), policy.exceptions, policy.read, labels, chains))
    return verdicts


def unused(exceptions: dict[str, Exception_], read: dict[str, ReadEntry], installed_keys: set[str]) -> list[str]:
    """A note for each exception and `read` entry whose distribution is not installed."""
    notes = [f"the exception for {e.package} is not used here: {e.package} is not installed"
             for key, e in sorted(exceptions.items()) if key not in installed_keys]
    notes += [f"the `read` entry for {e.package} is not used here: {e.package} is not installed"
              for key, e in sorted(read.items()) if key not in installed_keys]
    return notes


# ---------------------------------------------------------- requirements


@dataclass(frozen=True)
class Pin:
    name: str
    spec: str
    marker: str
    line: int
    via: tuple[str, ...]

    @property
    def key(self) -> str:
        return normalise(self.name)

    @property
    def version(self) -> str:
        return self.spec[2:] if self.spec.startswith("==") and not self.spec.startswith("===") else ""


def read_requirements(path: Path) -> list[Pin]:
    """A pip requirements file: one requirement a logical line, `\\` joining
    lines, comments from `#`, option lines skipped, `--hash` and other
    options after a requirement dropped. A `# via` comment under a pin, as
    uv and pip-tools write it, is kept as what brings the pin."""
    pins: list[Pin] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        start = i
        text = lines[i]
        while text.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            text = text.rstrip()[:-1] + " " + lines[i]
        i += 1
        body = re.sub(r"(^|\s)#.*$", "", text).strip()
        if not body or body.startswith("-"):
            continue
        body = re.split(r"\s+--?[A-Za-z]", body, maxsplit=1)[0].strip()
        parsed = parse_requirement(body)
        if parsed is None:
            continue
        name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", body).group(1)
        via: list[str] = []
        # The indented comment lines under a pin say what brings it.
        while i < len(lines) and re.match(r"\s+#", lines[i]):
            words = re.sub(r"^\s+#\s*", "", lines[i])
            words = re.sub(r"^via\b", "", words).strip()
            via.extend(v.strip() for v in words.split(",") if v.strip())
            i += 1
        pins.append(Pin(name, parsed[2].replace(" ", ""), parsed[3], start + 1, tuple(via)))
    return pins


def metadata_dump() -> dict[str, Any]:
    """What `--python` reads from the other interpreter."""
    raw = []
    seen = set()
    for dist in importlib.metadata.distributions():
        meta = dist.metadata
        name = meta.get("Name")
        if not name or normalise(name) in seen:
            continue
        seen.add(normalise(name))
        raw.append({
            "name": name,
            "version": str(dist.version),
            "License-Expression": meta.get("License-Expression"),
            "License": meta.get("License"),
            "Classifier": [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")],
            "Requires-Dist": list(meta.get_all("Requires-Dist") or []),
            "Provides-Extra": list(meta.get_all("Provides-Extra") or []),
        })
    return {"format": "emet-licence-metadata/1", "python": sys.executable,
            "environment": marker_environment(), "distributions": raw}


def records_from_dump(dump: dict[str, Any]) -> dict[str, Record]:
    out: dict[str, Record] = {}
    for d in dump.get("distributions", []):
        rec = Record(d["name"], d["version"], read_licence(d.get("License-Expression"), d.get("License"),
                                                           d.get("Classifier") or []),
                     list(d.get("Requires-Dist") or []), list(d.get("Provides-Extra") or []))
        out.setdefault(rec.key, rec)
    return out


def read_other(python: str) -> dict[str, Any]:
    done = subprocess.run([python, "-I", str(Path(__file__).resolve()), "--metadata-json"],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError(f"{python} could not read its metadata: {(done.stderr or done.stdout).strip()[-400:]}")
    return json.loads(done.stdout)


def judge_requirements(pins: list[Pin], dump: dict[str, Any], policy: Policy) -> tuple[list[Verdict], list[str]]:
    """Each pin against the venv's own copy of it, then every other
    distribution in that venv by the same rules: D9 holds for what the venv
    holds, a pin or not. A pin whose marker is false for that venv's
    interpreter is listed and not judged."""
    records = records_from_dump(dump)
    env = dump.get("environment") or marker_environment()
    graph = live(edges(records, env), records)
    listed = {p.key for p in pins}
    allowed = set(policy.allowed) | set(policy.tools_allowed)
    verdicts: list[Verdict] = []
    notes: list[str] = []
    roots = {k for k in listed if not any((k, "") in dsts for src, dsts in graph.items()
                                          if src[0] in listed and src[0] != k)}
    for pin in pins:
        if pin.marker and evaluate_marker(pin.marker, {**env, "extra": ""}) is False:
            notes.append(f"{pin.name}: not for this interpreter ({pin.marker}), so not judged here")
            continue
        rec = records.get(pin.key)
        named = list(pin.via) or ["the list"]
        if rec is None:
            verdicts.append(Verdict(Record(pin.name, pin.version or pin.spec or "?", Reading("not read", None)),
                                    False, f"pinned at line {pin.line} and not installed in that venv", named, [],
                                    fix="install the list into that venv first"))
            continue
        if pin.version and rec.version != pin.version:
            verdicts.append(Verdict(rec, False, f"pinned at {pin.version} on line {pin.line}, and that venv holds "
                                                f"{rec.version}", named, [],
                                    fix="install the list into that venv again"))
            continue
        labels, chains = brought_by(records, graph, pin.key, roots - {pin.key})
        keys = None
        if not labels:
            if pin.key in roots:
                # A pin of the list's own: a tools exception names it by its name.
                labels, keys = ["the list"], [pin.key]
            else:
                labels = list(pin.via)
        verdicts.append(judge(rec, allowed, policy.tools_exceptions, policy.read, labels, chains, keys))
    unpinned = [key for key in sorted(records, key=str.lower) if key not in listed and key not in policy.first_party]
    for key in unpinned:
        labels, chains = brought_by(records, graph, key, listed)
        if not labels:
            chains = ["not in the list, and no pin requires it"]
        verdicts.append(judge(records[key], allowed, policy.tools_exceptions, policy.read, labels, chains))
    extra = [records[k].name for k in unpinned if k not in ("pip", "setuptools")]
    if extra:
        notes.append("installed in that venv and not in the list, judged all the same: " + ", ".join(extra))
    return verdicts, notes


# ----------------------------------------------------------------- output


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def brought_cell(v: Verdict) -> str:
    """The table's "Brought by": the roots that share one path through the
    tree together, each path's middle named once."""
    if not v.brought:
        return v.chains[0] if v.chains else ""
    if len(v.chains) != len(v.brought):
        return v.brought_text
    paths: dict[str, list[str]] = {}
    for label, chain in zip(v.brought, v.chains):
        middle = ", ".join(chain.split(" > ")[1:-1])
        paths.setdefault(middle, []).append(label)
    return "; ".join(", ".join(labels) + (f" (through {middle})" if middle else "")
                     for middle, labels in paths.items())


def table(verdicts: list[Verdict]) -> list[str]:
    rows = ["| Package | Version | Licence | Brought by | Verdict |", "|---|---|---|---|---|"]
    for v in verdicts:
        brought = brought_cell(v)
        verdict = v.why if v.allowed else "REFUSED: " + v.why
        rows.append(f"| {cell(v.record.name)} | {cell(v.record.version)} | {cell(v.record.reading.text)} | "
                    f"{cell(brought)} | {cell(verdict)} |")
    return rows


def report(verdicts: list[Verdict], *, show_table: bool, out=print) -> int:
    refused = [v for v in verdicts if not v.allowed]
    if show_table:
        for line in table(verdicts):
            out(line)
        out("")
    for v in verdicts:
        if v.allowed and v.exception is not None:
            out(f"  exception: {v.record.name} {v.record.version}, {v.record.reading.text}, "
                f"brought by {v.brought_text or 'nothing first-party'}")
        elif v.allowed and v.entry is not None:
            out(f"  read by hand: {v.record.name} {v.record.version}, {v.entry.licence}, as read in {v.entry.where}")
    fixes: list[str] = []
    for v in refused:
        what = "; ".join(v.chains) if v.chains else (v.brought_text or "nothing first-party")
        out(f"  x {v.record.name} {v.record.version}, {v.record.reading.text}: {v.why}. Brought by: {what}")
        if v.fix and v.fix not in fixes:
            fixes.append(v.fix)
    for line in fixes:
        out(f"  fix: {line}")
    if refused:
        out(f"licences: {len(refused)} refused of {len(verdicts)}")
        return 1
    out(f"licences: ok, {len(verdicts)} distribution(s), {sum(v.exception is not None for v in verdicts)} by "
        f"exception, {sum(v.entry is not None for v in verdicts)} read by hand")
    return 0


def not_run_table(pins: list[Pin]) -> list[str]:
    rows = ["| Package | Version | Licence | Brought by | Verdict |", "|---|---|---|---|---|"]
    for pin in pins:
        via = ", ".join(pin.via) if pin.via else ""
        marker = f" ({pin.marker})" if pin.marker else ""
        rows.append(f"| {cell(pin.name)} | {cell(pin.version or pin.spec or '?')} | not read | {cell(via)} | "
                    f"not run{cell(marker)} |")
    return rows


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold every installed dependency's licence to the allowed list.")
    parser.add_argument("--table", action="store_true", help="print the whole table in Markdown")
    parser.add_argument("--requirements", metavar="FILE", help="a tools venv's pinned list")
    parser.add_argument("--python", metavar="EXE", help="the interpreter of the venv that has --requirements installed")
    parser.add_argument("--policy", metavar="FILE", default=str(DEFAULT_POLICY), help=argparse.SUPPRESS)
    parser.add_argument("--metadata-json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.metadata_json:
        print(json.dumps(metadata_dump()))
        return 0
    if args.python and not args.requirements:
        parser.error("--python names the venv of a --requirements list")
    try:
        policy = load_policy(Path(args.policy))
    except PolicyError as exc:
        print(f"licences: {exc}", file=sys.stderr)
        return 2

    if not args.requirements:
        records = installed()
        verdicts = judge_installed(records, policy, marker_environment())
        skipped = sorted(records[k].name for k in records if k in policy.first_party)
        judged = {v.record.key for v in verdicts}
        machine = sorted(records[k].name for k in records if k in INSTALLERS and k not in judged)
        print(f"licences: {len(verdicts)} distribution(s) installed for {sys.executable}; "
              f"first party, skipped: {', '.join(skipped) or 'none installed'}")
        if machine:
            print(f"  note: the interpreter's own installers, which no first-party package brings, skipped: "
                  f"{', '.join(machine)}")
        for note in unused(policy.exceptions, policy.read, set(records)):
            print(f"  note: {note}")
        return report(verdicts, show_table=args.table)

    path = Path(args.requirements)
    try:
        pins = read_requirements(path)
    except OSError as exc:
        print(f"licences: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    if not args.python:
        for line in not_run_table(pins):
            print(line)
        print(f"licences: not run: {len(pins)} pin(s) in {path}, and no venv to read their licences from. "
              f"Install the list into a venv and name its interpreter with --python.")
        return 2
    try:
        dump = read_other(args.python)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"licences: {exc}", file=sys.stderr)
        return 2
    verdicts, notes = judge_requirements(pins, dump, policy)
    print(f"licences: {len(pins)} pin(s) in {path}, read from {dump.get('python', args.python)}, "
          f"by D9's rule with {', '.join(sorted(policy.tools_allowed))} allowed")
    for note in notes + unused(policy.tools_exceptions, policy.read, set(records_from_dump(dump))):
        print(f"  note: {note}")
    return report(verdicts, show_table=args.table)


if __name__ == "__main__":
    raise SystemExit(main())
