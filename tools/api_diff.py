#!/usr/bin/env python3
"""Print what a builder or a plugin author would see change.

    sdk          every public name of `emet_sdk`'s contract modules: a
                 function's signature, a class's bases, fields and methods, a
                 constant's value, each as written
    entry-point  every entry of every entry-point group
    script       the console scripts
    package      the declared version, and each dependency with its floor
    schema       every schema property, definition and subschema by JSON
                 pointer: its type, whether it is required, its `P0`, `RSV`
                 or `V1` tag, and every keyword that narrows it
    flag         every flag of `emet`, `emet-listen` and `emet-talk`

**Why this exists.** The pull request template has a section, "If this
touches the contract layer", and whether a change does has been answered from
memory. A listing can be compared, so this builds one listing of the working
tree and one of the base, and prints what was added, removed and changed.
Stage 1 freezes a release's contract against it and Stage 6 reads it beside
the contract checklist. It is advisory: it prints, and a person decides what
a line means.

**How it reads.** Statically, with `ast`, `json` and `tomllib`, so nothing is
imported and nothing has to be installed. The base side comes out of git one
file at a time (`git show <base>:<path>`), so the base needs no checkout.
`emet-sdk/tests/test_public_api.py` holds the same surface a second way, from
the imported package, and a test in `tools/tests/test_api_diff.py` holds the
two readers to the same names.

**What a static reader cannot see,** and a reader that resolves names can
(griffe is the one the survey names; that it reads these is from its
documentation, unverified here, 2026-11-26): members a class inherits from a
base in another module (each class lists its own members and its bases as
written, so a change to a base shows once, under the base); the `__init__` a
dataclass writes for itself (the class line gives its fields in order
instead); the names a star import binds, and a computed `__all__` (each has
a line that says so: a star import names the module it reads, and a name
`__all__` lists that only a star import could bind says it is bound there);
and a signature changed by a decorator. Of the four, `emet_sdk` has the first
two today.

The contract modules are every module of `emet_sdk` and of the packages in
it, except `cli.py` and the `testing/` folder, the same rule the line budget
uses (decision E8): the command's surface is its flags, and a conformance
kit is counted apart.

A schema node's tag is the first word of its description when that word is
`P0`, `RSV` or `V1`, which is how the schemas carry it. A description that
tags its values at more than one level ("P0: none, encoders. RSV: slam.")
prints as `mixed`, with the sentences that hold the tags, so a value that
moves from one tag to another is a changed line. A node with no tag of its
own says which tagged property it sits under, when one does. Every property,
every definition under `$defs`, and every subschema (`items`, a map's
`additionalProperties`, `propertyNames`, each branch of `allOf` or of
`if`/`then`/`else`) has a line of its own.

A flag's help text is left out: the words are the console sheet's, and a
reworded sentence is no change to what a script may pass.

Standard library only, like `check_layering.py`.

Usage:  python tools/api_diff.py [--base REF] [--list] [repo_root]
Exit:   0 always when it could run, since it only prints. 2 when it could
        not: no Emet tree there, a tree that is no git checkout, or a base
        ref git does not know.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

#: Where the SDK's modules sit, and the name they import as.
SDK_DIR = "emet-sdk/emet_sdk"
SDK_NAME = "emet_sdk"

#: What directly under `emet_sdk/` is outside the contract (decision E8): the
#: command line, and the folder of the conformance kit 1.0 adds, with
#: everything in it. The same set `check_health.py` leaves out of the
#: 4,000-line budget.
OUTSIDE_THE_CONTRACT = frozenset({"cli.py", "testing"})

#: How long one git question may take before the tool gives up on it.
GIT_TIMEOUT_S = 120

SCHEMA_DIR = "emet-sdk/schemas"

#: The stability tags a schema property's description may open with
#: (`DESIGN.md` section 0).
TAG = re.compile(r"\s*(P0|RSV|V1)\b")

#: A stability tag anywhere in a description.
TAG_WORD = re.compile(r"\b(P0|RSV|V1)\b")

#: Keywords of `add_argument` that say nothing about what a caller may pass.
UNLISTED_KEYWORDS = frozenset({"help", "version"})

#: Schema keywords holding a map of schemas, one schema, or a list of them.
SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
SCHEMA_ONES = ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems",
               "contains", "propertyNames", "not", "if", "then", "else")
SCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")

#: Schema keywords a node's line already says, that have lines of their own,
#: or that are prose. `required` is said on each property's line; a required
#: name the node's own `properties` do not hold is printed with its limits.
SCHEMA_SAID = frozenset({"description", "title", "type", "enum", "const", "$ref", "required",
                         "$comment", "examples", "$schema", "$id",
                         *SCHEMA_MAPS, *SCHEMA_ONES, *SCHEMA_LISTS})


# --------------------------------------------------------------------------
# The two sides
# --------------------------------------------------------------------------


class Tree:
    """One side of the comparison: a way to read a file and to list a folder.

    Paths are relative to the repository's root, with forward slashes.
    """

    def read(self, path: str) -> str | None:
        raise NotImplementedError

    def names(self, folder: str) -> list[str]:
        """The files directly in a folder."""
        raise NotImplementedError

    def folders(self, folder: str) -> list[str]:
        """The folders directly in a folder."""
        raise NotImplementedError


class DiskTree(Tree):
    """The working tree, as it is on disk, committed or not."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, path: str) -> str | None:
        full = self.root / path
        if not full.is_file():
            return None
        return full.read_text(encoding="utf-8")

    def names(self, folder: str) -> list[str]:
        full = self.root / folder
        if not full.is_dir():
            return []
        return sorted(p.name for p in full.iterdir() if p.is_file())

    def folders(self, folder: str) -> list[str]:
        full = self.root / folder
        if not full.is_dir():
            return []
        return sorted(p.name for p in full.iterdir() if p.is_dir())


def git_output(root: Path, *args: str) -> str | None:
    """The one place git is asked anything. None when git refuses, is not
    installed, or takes longer than `GIT_TIMEOUT_S`.

    Read-only by construction: the callers below pass `show`, `ls-tree` and
    `rev-parse` and nothing else.
    """
    try:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False,
                              timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace")


class GitTree(Tree):
    """A commit, read out of git one file at a time. Nothing is checked out."""

    def __init__(self, root: Path, ref: str) -> None:
        self.root = root
        self.ref = ref

    def read(self, path: str) -> str | None:
        return git_output(self.root, "show", f"{self.ref}:{path}")

    def _entries(self, folder: str, kind: str) -> list[str]:
        out = git_output(self.root, "ls-tree", f"{self.ref}:{folder}")
        if out is None:
            return []
        found = []
        for line in out.splitlines():
            # "<mode> <type> <sha>\t<name>"; a folder is a tree, a file a blob.
            head, _, name = line.partition("\t")
            if name and head.split()[1:2] == [kind]:
                found.append(name)
        return sorted(found)

    def names(self, folder: str) -> list[str]:
        return self._entries(folder, "blob")

    def folders(self, folder: str) -> list[str]:
        return self._entries(folder, "tree")


# --------------------------------------------------------------------------
# The SDK's public names
# --------------------------------------------------------------------------


def _public_member(name: str) -> bool:
    """A class member a plugin author may rely on: a plain name, or one of
    Python's own double-ended names such as `__init__`."""
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def _top_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement run at import: the module's own, and those inside a
    top-level `if`, `try`, `with` or loop."""
    for stmt in body:
        yield stmt
        for part in ("body", "orelse", "finalbody"):
            inner = getattr(stmt, part, None)
            if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)) and inner:
                yield from _top_level(inner)
        if isinstance(stmt, ast.Try):
            for handler in stmt.handlers:
                yield from _top_level(handler.body)


def _targets(stmt: ast.stmt) -> list[str]:
    """The plain names an assignment binds."""
    if isinstance(stmt, ast.AnnAssign):
        return [stmt.target.id] if isinstance(stmt.target, ast.Name) else []
    names: list[str] = []
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    names.append(node.id)
    return names


def _bindings(tree: ast.Module, module: str, is_package: bool) -> dict[str, list[Any]]:
    """Every top-level name and what binds it, in file order.

    A value is an `ast` node (a function, a class, an assignment) or a string,
    the dotted name an import brings in.
    """
    bound: dict[str, list[Any]] = {}
    for stmt in _top_level(tree.body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.setdefault(stmt.name, []).append(stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            for name in _targets(stmt):
                bound.setdefault(name, []).append(stmt)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                name = alias.asname or alias.name.split(".")[0]
                bound.setdefault(name, []).append(alias.name if alias.asname else name)
        elif isinstance(stmt, ast.ImportFrom):
            source = _import_source(stmt, module, is_package)
            for alias in stmt.names:
                bound.setdefault(alias.asname or alias.name, []).append(f"{source}.{alias.name}")
    return bound


def _import_source(stmt: ast.ImportFrom, module: str, is_package: bool) -> str:
    """The absolute dotted module a `from ... import` names."""
    if not stmt.level:
        return stmt.module or ""
    parts = module.split(".")
    keep = len(parts) - stmt.level + (1 if is_package else 0)
    base = parts[: max(keep, 0)]
    return ".".join(base + ([stmt.module] if stmt.module else []))


def _literal_names(node: ast.expr | None) -> list[str] | None:
    """The strings of a literal list or tuple, or None when it is anything else."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names = []
    for element in node.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        names.append(element.value)
    return names


def _exported(tree: ast.Module, bound: dict[str, list[Any]]) -> tuple[list[str], bool]:
    """The module's public names, and whether `__all__` could be read.

    `__all__` when the module has one written as a literal, with any literal
    `__all__ += [...]` after it. Otherwise every top-level name the module
    binds itself that does not start with `_`.
    """
    names: list[str] | None = None
    readable = True
    for stmt in _top_level(tree.body):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and "__all__" in _targets(stmt):
            names = _literal_names(stmt.value)
            readable = names is not None
        elif (isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name)
              and stmt.target.id == "__all__"):
            more = _literal_names(stmt.value)
            if names is None or more is None:
                readable = False
            else:
                names = names + more
    if names is not None and readable:
        return sorted(set(names)), True
    own = [name for name, how in bound.items()
           if not name.startswith("_") and not isinstance(how[-1], str)]
    return sorted(own), readable


def _marks(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return "".join(f"@{ast.unparse(d)} " for d in node.decorator_list)


def _function_text(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """A signature as written: decorators, defaults and annotations."""
    word = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{_marks(node)}{word} ({ast.unparse(node.args)}){returns}"


def _value_text(stmt: ast.Assign | ast.AnnAssign) -> str:
    if isinstance(stmt, ast.AnnAssign):
        value = f" = {ast.unparse(stmt.value)}" if stmt.value is not None else ""
        return f"attribute: {ast.unparse(stmt.annotation)}{value}"
    return f"attribute = {ast.unparse(stmt.value)}"


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if ast.unparse(target).split(".")[-1] == "dataclass":
            return True
    return False


def _class_lines(key: str, node: ast.ClassDef) -> dict[str, str]:
    """A class: its bases, and each member it defines itself."""
    out: dict[str, str] = {}
    fields: list[str] = []
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _public_member(stmt.name):
                out[f"{key}.{stmt.name}"] = _function_text(stmt)
        elif isinstance(stmt, ast.ClassDef):
            if _public_member(stmt.name):
                out.update(_class_lines(f"{key}.{stmt.name}", stmt))
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            for name in _targets(stmt):
                # Every annotated name is a constructor argument of a
                # dataclass, whatever it is called.
                if isinstance(stmt, ast.AnnAssign) and "ClassVar" not in ast.unparse(stmt.annotation):
                    fields.append(name)
                if _public_member(name):
                    out[f"{key}.{name}"] = _value_text(stmt)
    bases = [ast.unparse(b) for b in node.bases]
    bases += [f"{k.arg}={ast.unparse(k.value)}" for k in node.keywords]
    head = f"{_marks(node)}class" + (f"({', '.join(bases)})" if bases else "")
    if _is_dataclass(node) and fields:
        # The order is the constructor's, so a reordering is a change.
        head += f", fields in order: {', '.join(fields)}"
    out[key] = head
    return out


def module_lines(module: str, source: str, is_package: bool = False) -> dict[str, str]:
    """One module's public names, read from its source."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {f"sdk {module}": f"could not be parsed: {exc.msg}, line {exc.lineno}"}
    bound = _bindings(tree, module, is_package)
    names, readable = _exported(tree, bound)
    out: dict[str, str] = {}
    if not readable:
        out[f"sdk {module}.__all__"] = ("computed, so it cannot be read without importing: "
                                        "the module's own public names are listed instead")
    # `from x import *` binds the key "*" to "x.*": the names it brings in
    # are x's, which only an import can tell.
    stars = " and ".join(source.removesuffix(".*") for source in bound.get("*", []))
    if stars:
        out[f"sdk {module}.*"] = (f"star import from {stars}, so the names it binds cannot be "
                                  f"read without importing")
    for name in names:
        key = f"sdk {module}.{name}"
        how = bound.get(name)
        if not how:
            out[key] = (f"named in __all__ and bound only by the star import from {stars}" if stars
                        else "named in __all__ and bound nowhere at the top level")
            continue
        last = how[-1]
        if isinstance(last, str):
            out[key] = f"re-export of {last}"
        elif isinstance(last, ast.ClassDef):
            out.update(_class_lines(key, last))
        elif isinstance(last, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[key] = _function_text(last)
        else:
            # A name bound more than once (a value, and its fallback under
            # `except`) shows every value it can take.
            texts: list[str] = []
            for stmt in how:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    text = _value_text(stmt)
                    if text not in texts:
                        texts.append(text)
            later = [t.removeprefix("attribute = ") for t in texts[1:]]
            out[key] = ", or ".join(texts[:1] + later)
    return out


def _package_lines(tree: Tree, folder: str, dotted: str, top: bool, out: dict[str, str]) -> None:
    """One package's modules, then each package inside it: a folder with an
    `__init__.py`, as Python imports it. Directly under `emet_sdk/`,
    `OUTSIDE_THE_CONTRACT` is left out."""
    for name in tree.names(folder):
        if not name.endswith(".py") or (top and name in OUTSIDE_THE_CONTRACT):
            continue
        source = tree.read(f"{folder}/{name}")
        if source is None:
            continue
        is_package = name == "__init__.py"
        module = dotted if is_package else f"{dotted}.{name[:-3]}"
        out.update(module_lines(module, source, is_package))
    for sub in tree.folders(folder):
        if sub.startswith(("_", ".")) or (top and sub in OUTSIDE_THE_CONTRACT):
            continue
        if "__init__.py" in tree.names(f"{folder}/{sub}"):
            _package_lines(tree, f"{folder}/{sub}", f"{dotted}.{sub}", False, out)


def sdk_lines(tree: Tree) -> dict[str, str]:
    out: dict[str, str] = {}
    _package_lines(tree, SDK_DIR, SDK_NAME, True, out)
    if tree.read(f"{SDK_DIR}/py.typed") is not None:
        out[f"sdk {SDK_NAME} py.typed"] = "present, so a type checker reads the SDK's annotations (PEP 561)"
    return out


# --------------------------------------------------------------------------
# Versions, floors, entry points and console scripts
# --------------------------------------------------------------------------


_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$", re.DOTALL)


def _requirement(text: str) -> tuple[str, str]:
    """A requirement's name, and the rest of it (extras, the floor, markers)."""
    match = _REQUIREMENT.match(text)
    if match is None:
        return text.strip(), "unreadable"
    return match.group(1), " ".join(match.group(2).split()) or "any version"


def _project(tree: Tree, package: str) -> dict[str, Any] | str | None:
    """A package's `[project]` table, None when it has no `pyproject.toml`,
    or a sentence when the file cannot be parsed."""
    text = tree.read(f"{package}/pyproject.toml")
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return f"pyproject.toml could not be parsed: {exc}"
    project = data.get("project")
    return project if isinstance(project, dict) else {}


def _put(out: dict[str, str], key: str, value: str) -> None:
    """Keep both when two packages claim one name, which is itself a finding."""
    out[key] = f"{out[key]}; also {value}" if key in out else value


def package_lines(tree: Tree) -> dict[str, str]:
    out: dict[str, str] = {}
    for package in PACKAGES:
        project = _project(tree, package)
        if project is None:
            continue
        if isinstance(project, str):
            out[f"package {package}"] = project
            continue
        out[f"package {package} version"] = str(project.get("version", "none declared"))
        if "requires-python" in project:
            out[f"package {package} requires-python"] = str(project["requires-python"])
        for requirement in project.get("dependencies") or []:
            name, rest = _requirement(str(requirement))
            _put(out, f"package {package} requires {name}", rest)
        for extra, requirements in sorted((project.get("optional-dependencies") or {}).items()):
            for requirement in requirements or []:
                name, rest = _requirement(str(requirement))
                _put(out, f"package {package} extra {extra} requires {name}", rest)
        for script, target in sorted((project.get("scripts") or {}).items()):
            _put(out, f"script {script}", f"{target}, from {package}")
        for group, entries in sorted((project.get("entry-points") or {}).items()):
            for name, target in sorted((entries or {}).items()):
                _put(out, f"entry-point {group} {name}", f"{target}, from {package}")
    return out


def console_scripts(tree: Tree) -> dict[str, str]:
    """Each console script and the `module:function` it runs."""
    scripts: dict[str, str] = {}
    for package in PACKAGES:
        project = _project(tree, package)
        if isinstance(project, dict):
            for script, target in (project.get("scripts") or {}).items():
                scripts.setdefault(str(script), str(target))
    return scripts


# --------------------------------------------------------------------------
# The schemas
# --------------------------------------------------------------------------


def _escape(token: str) -> str:
    """One reference token of a JSON pointer (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _tag_clauses(text: str) -> list[tuple[str, str]]:
    """Each tag a description gives a part of its values, with the sentence
    that holds it. A tag counts where a clause starts with it (the start of
    the text, or after `.`, `,` or `;`) or where a colon follows it, as in
    "P0: none, encoders. RSV: slam." or "micro is RSV: accepted". A tag
    inside a phrase, as in "ignored by the P0 engine", is prose."""
    found: list[tuple[str, str]] = []
    for match in TAG_WORD.finditer(text):
        before = text[: match.start()].rstrip()
        if before and before[-1] not in ".,;" and not text[match.end():].startswith(":"):
            continue
        start = text.rfind(". ", 0, match.start())
        start = 0 if start < 0 else start + 2
        end = text.find(". ", match.end())
        end = len(text) if end < 0 else end + 1
        found.append((match.group(1), text[start:end].strip()))
    return found


def _tag_of(node: Any) -> tuple[str, str]:
    """A node's tag in one word (`P0`, `RSV`, `V1`, `mixed`, or none), and
    as its line prints it: the word, or for `mixed` the sentences that hold
    the tags, so a value that moves from one tag to another changes it."""
    if not (isinstance(node, dict) and isinstance(node.get("description"), str)):
        return "", ""
    text = node["description"]
    clauses = _tag_clauses(text)
    if len({word for word, _ in clauses}) > 1:
        sentences = " ".join(dict.fromkeys(sentence for _, sentence in clauses))
        return "mixed", f"mixed ({sentences})"
    match = TAG.match(text)
    return (match.group(1), match.group(1)) if match else ("", "")


def _tag(node: Any) -> str:
    """A node's tag in one word, `mixed` when its values carry several."""
    return _tag_of(node)[0]


def _plain(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _shape(node: Any) -> str:
    """A property's type in a few words: what a document may put there."""
    if node is True:
        return "anything"
    if node is False:
        return "nothing"
    if not isinstance(node, dict):
        return "malformed"
    parts: list[str] = []
    if "$ref" in node:
        parts.append(f"$ref {node['$ref']}")
    kind = node.get("type")
    if isinstance(kind, list):
        parts.append("|".join(str(k) for k in kind))
    elif kind == "object":
        parts.append("closed object" if node.get("additionalProperties") is False else "object")
    elif kind == "array" and isinstance(node.get("items"), (dict, bool)):
        parts.append(f"array of ({_shape(node['items'])})")
    elif isinstance(kind, str):
        parts.append(kind)
    if "enum" in node and isinstance(node["enum"], list):
        parts.append("enum(" + "|".join(_plain(v) for v in node["enum"]) + ")")
    if "const" in node:
        parts.append(f"const({json.dumps(node['const'], sort_keys=True)})")
    if not parts:
        for word in SCHEMA_LISTS:
            if isinstance(node.get(word), list):
                parts.append(f"{word} of {len(node[word])}")
    return " ".join(parts) or "untyped"


def _limits(node: Any) -> str:
    """Every other keyword that narrows a node: a range, a pattern, a default,
    and a required name its own `properties` do not hold (an `if` that asks
    for one, say), which no property line can say."""
    if not isinstance(node, dict):
        return ""
    shown = {k: node[k] for k in node if k not in SCHEMA_SAID}
    required = node.get("required")
    own = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    if isinstance(required, list):
        loose = [name for name in required if name not in own]
        if loose:
            shown["required"] = loose
    elif required is not None:
        shown["required"] = required
    return ", ".join(f"{k}={json.dumps(shown[k], sort_keys=True)}" for k in sorted(shown))


def _node_text(node: Any, role: str, under: str) -> str:
    """One node's line: its type, its role (required, optional, matched by
    pattern, a definition or a subschema), its tag, and its limits."""
    tag = _tag_of(node)[1] or (f"untagged, under {under}" if under else "untagged")
    limits = _limits(node)
    return f"{_shape(node)}; {role}; {tag}" + (f"; {limits}" if limits else "")


def _said_by_shape(node: dict[str, Any], word: str) -> bool:
    """A subschema the parent's own line already says in full: the `false`
    that makes an object "closed object"."""
    return word == "additionalProperties" and node.get(word) is False and node.get("type") == "object"


def _walk_schema(node: Any, pointer: str, prefix: str, out: dict[str, str], under: str) -> None:
    """A line for every property, definition and subschema under `node`."""
    if not isinstance(node, dict):
        return
    under = _tag(node) or under
    required = node.get("required")
    required = set(required) if isinstance(required, list) else set()
    for word in SCHEMA_MAPS:
        entries = node.get(word)
        if not isinstance(entries, dict):
            continue
        for name, child in entries.items():
            at = f"{pointer}/{_escape(word)}/{_escape(str(name))}"
            if word == "properties":
                role, inherited = ("required" if name in required else "optional"), under
            elif word == "patternProperties":
                role, inherited = "matched by pattern", under
            elif word == "dependentSchemas":
                role, inherited = "subschema", under
            else:
                # A definition has no parent property, so it starts with no tag.
                role, inherited = "definition", ""
            out[prefix + at] = _node_text(child, role, inherited)
            _walk_schema(child, at, prefix, out, inherited)
    for word in SCHEMA_ONES:
        child = node.get(word)
        if isinstance(child, (dict, bool)) and not _said_by_shape(node, word):
            out[prefix + f"{pointer}/{word}"] = _node_text(child, "subschema", under)
        _walk_schema(child, f"{pointer}/{word}", prefix, out, under)
    for word in SCHEMA_LISTS:
        entries = node.get(word)
        if isinstance(entries, list):
            for index, child in enumerate(entries):
                at = f"{pointer}/{word}/{index}"
                if isinstance(child, (dict, bool)):
                    out[prefix + at] = _node_text(child, "subschema", under)
                _walk_schema(child, at, prefix, out, under)


def schema_lines(tree: Tree) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in tree.names(SCHEMA_DIR):
        if not name.endswith(".json"):
            continue
        text = tree.read(f"{SCHEMA_DIR}/{name}")
        if text is None:
            continue
        stem = name.removesuffix(".json").removesuffix(".schema")
        try:
            document = json.loads(text)
        except ValueError as exc:
            out[f"schema {stem}"] = f"could not be parsed: {exc}"
            continue
        if not isinstance(document, dict):
            out[f"schema {stem}"] = "is no JSON object"
            continue
        if "$id" in document:
            out[f"schema {stem} $id"] = str(document["$id"])
        limits = _limits(document)
        out[f"schema {stem} (root)"] = _shape(document) + (f"; {limits}" if limits else "")
        _walk_schema(document, "", f"schema {stem} ", out, "")
    return out


# --------------------------------------------------------------------------
# The flags of the three commands
# --------------------------------------------------------------------------


@dataclass
class _Parser:
    """A name bound to a parser, or to a group of one."""

    command: str            # "emet", "emet validate"
    group: str = ""         # the mutually exclusive group it adds to, if any
    holder: bool = False    # what `add_subparsers()` returned


@dataclass
class _Flag:
    command: str
    name: str
    text: str
    group: str


def _module_file(tree: Tree, dotted: str) -> tuple[str, str] | None:
    """The path and source of a first-party module, whichever package holds it."""
    relative = dotted.replace(".", "/")
    for package in PACKAGES:
        for path in (f"{package}/{relative}.py", f"{package}/{relative}/__init__.py"):
            text = tree.read(path)
            if text is not None:
                return path, text
    return None


def _scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Every node of one function (or of the module's own statements), without
    the bodies of the functions defined inside it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _scopes(module: ast.Module) -> list[ast.AST]:
    functions = [n for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return [module, *functions]


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _receiver(call: ast.Call) -> str:
    """The plain name a method is called on: `parser` in `parser.add_argument()`."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return ""


def _constant(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _assigned_calls(scope: ast.AST) -> Iterator[tuple[str, ast.Call]]:
    """Each `name = call(...)` of a scope."""
    for node in _scope_nodes(scope):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)):
            yield node.targets[0].id, node.value


def _births(scope: ast.AST, script: str) -> dict[str, _Parser]:
    """The names a scope binds to a new `ArgumentParser` for this script.

    A parser whose `prog` names another command is somebody else's.
    """
    born: dict[str, _Parser] = {}
    for name, call in _assigned_calls(scope):
        if _call_name(call) != "ArgumentParser":
            continue
        prog = _constant(_keyword(call, "prog"))
        if prog is None or prog == script:
            born[name] = _Parser(script)
    return born


def _function_named(tree: Tree, module: ast.Module, dotted: str, is_package: bool,
                    name: str) -> tuple[ast.Module, str, bool, ast.FunctionDef] | None:
    """A function a module can call by a plain name: its own, or one it
    imports from another first-party module."""
    for stmt in module.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == name:
            return module, dotted, is_package, stmt
    for stmt in ast.walk(module):
        if not isinstance(stmt, ast.ImportFrom):
            continue
        for alias in stmt.names:
            if (alias.asname or alias.name) != name:
                continue
            source = _import_source(stmt, dotted, is_package)
            found = _module_file(tree, source)
            if found is None:
                return None
            try:
                other = ast.parse(found[1])
            except SyntaxError:
                return None
            for inner in other.body:
                if isinstance(inner, ast.FunctionDef) and inner.name == alias.name:
                    return other, source, found[0].endswith("__init__.py"), inner
    return None


def _flag_of(call: ast.Call, parser: _Parser) -> _Flag | None:
    """One `add_argument` call as a line: its name, and every keyword but the help."""
    strings = [_constant(a) for a in call.args]
    names = [s for s in strings if s]
    if not names:
        return None
    options = [n for n in names if n.startswith("-")]
    if options:
        long_names = [n for n in options if n.startswith("--")]
        name = (long_names or options)[0]
        parts = ["option"] + [f"also {n}" for n in options if n != name]
    else:
        name = names[0]
        parts = ["positional"]
    for keyword in sorted(call.keywords, key=lambda k: k.arg or ""):
        if keyword.arg and keyword.arg not in UNLISTED_KEYWORDS:
            parts.append(f"{keyword.arg}={ast.unparse(keyword.value)}")
    return _Flag(parser.command, name, ", ".join(parts), parser.group)


def _made_parsers(tree: Tree, module: ast.Module, dotted: str, is_package: bool,
                  scope: ast.AST, script: str) -> dict[str, _Parser]:
    """The names a scope binds to this script's parser: one it makes, or one
    a helper made and returned (`parser = build_parser()`)."""
    made = _births(scope, script)
    for name, call in _assigned_calls(scope):
        if name in made or not isinstance(call.func, ast.Name):
            continue
        helper = _function_named(tree, module, dotted, is_package, call.func.id)
        if helper is None:
            continue
        born = _births(helper[3], script)
        returned = {r.value.id for r in _scope_nodes(helper[3])
                    if isinstance(r, ast.Return) and isinstance(r.value, ast.Name)}
        if any(n in returned for n in born):
            made[name] = _Parser(script)
    return made


def _read_parsers(tree: Tree, module: ast.Module, dotted: str, is_package: bool, scope: ast.AST,
                  bound: dict[str, _Parser], found: list[_Flag], depth: int = 0) -> None:
    """Follow the parsers bound in one scope: subcommands, groups, flags, and
    any helper a parser is handed to."""
    changed = True
    while changed:  # a group may be bound before the parser it belongs to is seen
        changed = False
        for name, call in _assigned_calls(scope):
            if name in bound:
                continue
            owner = bound.get(_receiver(call))
            if owner is None:
                continue
            method = _call_name(call)
            new: _Parser | None = None
            if method == "add_subparsers":
                new = _Parser(owner.command, holder=True)
            elif owner.holder and method == "add_parser":
                sub = _constant(call.args[0]) if call.args else None
                if sub:
                    new = _Parser(f"{owner.command} {sub}")
            elif method == "add_mutually_exclusive_group":
                new = _Parser(owner.command, group=f"{dotted}:{call.lineno}")
            elif method == "add_argument_group":
                new = _Parser(owner.command, group=owner.group)
            if new is not None:
                bound[name] = new
                changed = True
    for node in _scope_nodes(scope):
        if not isinstance(node, ast.Call):
            continue
        owner = bound.get(_receiver(node))
        if owner is not None and _call_name(node) == "add_argument":
            flag = _flag_of(node, owner)
            if flag is not None:
                found.append(flag)
        elif owner is not None and owner.holder and _call_name(node) == "add_parser":
            sub = _constant(node.args[0]) if node.args else None
            if sub:
                found.append(_Flag(owner.command, sub, "subcommand", ""))
        elif isinstance(node.func, ast.Name) and depth < 4:
            handed = {i: bound[a.id] for i, a in enumerate(node.args)
                      if isinstance(a, ast.Name) and a.id in bound}
            named = {k.arg: bound[k.value.id] for k in node.keywords
                     if k.arg and isinstance(k.value, ast.Name) and k.value.id in bound}
            if not handed and not named:
                continue
            helper = _function_named(tree, module, dotted, is_package, node.func.id)
            if helper is None:
                continue
            other, other_dotted, other_is_package, function = helper
            parameters = [a.arg for a in function.args.posonlyargs + function.args.args]
            inner = {parameters[i]: p for i, p in handed.items() if i < len(parameters)}
            inner.update(named)
            _read_parsers(tree, other, other_dotted, other_is_package, function, inner, found, depth + 1)


def flags_of(tree: Tree, script: str, target: str) -> dict[str, str]:
    """Every flag of one console script, read from its `add_argument` calls."""
    dotted = target.split(":")[0]
    located = _module_file(tree, dotted)
    if located is None:
        return {f"flag {script}": f"its module {dotted} is not in the tree"}
    path, source = located
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        return {f"flag {script}": f"{path} could not be parsed: {exc.msg}, line {exc.lineno}"}
    is_package = path.endswith("__init__.py")
    found: list[_Flag] = []
    for scope in _scopes(module):
        made = _made_parsers(tree, module, dotted, is_package, scope, script)
        if made:
            _read_parsers(tree, module, dotted, is_package, scope, made, found)
    if not found:
        return {f"flag {script}": f"no ArgumentParser for it could be read in {path}"}
    groups: dict[str, list[str]] = {}
    for flag in found:
        if flag.group:
            groups.setdefault(flag.group, []).append(flag.name)
    out: dict[str, str] = {}
    for flag in found:
        others = sorted(n for n in groups.get(flag.group, []) if n != flag.name)
        text = flag.text + (f", excludes {' and '.join(others)}" if others else "")
        _put(out, f"flag {flag.command} {flag.name}", text)
    return out


def flag_lines(tree: Tree) -> dict[str, str]:
    out: dict[str, str] = {}
    for script, target in sorted(console_scripts(tree).items()):
        out.update(flags_of(tree, script, target))
    return out


# --------------------------------------------------------------------------
# The listing and the diff
# --------------------------------------------------------------------------


def listing(tree: Tree) -> dict[str, str]:
    """Everything one side declares, as `what it is: what it says`."""
    out: dict[str, str] = {}
    for part in (sdk_lines, package_lines, schema_lines, flag_lines):
        out.update(part(tree))
    return out


def listing_text(lines: dict[str, str]) -> list[str]:
    """The listing as text: one line a name, sorted, so two runs agree."""
    return [f"{key}: {value}" for key, value in sorted(lines.items())]


def diff(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """The keys added, removed and changed between two listings."""
    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    changed = sorted(k for k in new if k in old and new[k] != old[k])
    return added, removed, changed


def report(old: dict[str, str], new: dict[str, str], base: str) -> list[str]:
    """What the tool prints: the one line, or the three lists."""
    added, removed, changed = diff(old, new)
    if not (added or removed or changed):
        return [f"api: no change against {base}"]
    lines = [f"api: {len(added)} added, {len(removed)} removed, {len(changed)} changed against {base}"]
    if added:
        lines.append("added:")
        lines += [f"  + {key}: {new[key]}" for key in added]
    if removed:
        lines.append("removed:")
        lines += [f"  - {key}: {old[key]}" for key in removed]
    if changed:
        lines.append("changed:")
        for key in changed:
            lines += [f"  ~ {key}", f"      was: {old[key]}", f"      now: {new[key]}"]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="api_diff.py",
        description="Print what changed in the public API, the schemas, the entry points, "
                    "the flags, the version and the floors, against a base ref.",
    )
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]),
                        help="the repository's root (default: the one this file sits in)")
    parser.add_argument("--base", default="origin/master", metavar="REF",
                        help="the ref to compare the working tree with (default: origin/master)")
    parser.add_argument("--list", action="store_true",
                        help="print the working tree's listing alone, and read no base")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    root = Path(args.root).resolve()
    if not (root / "emet-sdk").is_dir():
        print(f"api: {root} does not look like the Emet repository", file=sys.stderr)
        return 2
    if args.list:
        print("\n".join(listing_text(listing(DiskTree(root)))))
        return 0
    if git_output(root, "rev-parse", "--git-dir") is None:
        # A source archive, or the body runner's unpacked tree: fetching
        # cannot help there.
        print(f"api: {root} is no git checkout, or git is not installed, so there is no base to "
              f"read. Run it in a clone of the repository, or use --list.", file=sys.stderr)
        return 2
    if git_output(root, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}") is None:
        print(f"api: git knows no commit named {args.base} here, so there is nothing to "
              f"compare with. Fetch it, or name another with --base.", file=sys.stderr)
        return 2
    new = listing(DiskTree(root))
    old = listing(GitTree(root, args.base))
    print("\n".join(report(old, new, args.base)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
