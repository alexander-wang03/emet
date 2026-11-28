#!/usr/bin/env python3
"""Check that every workflow in `.github/workflows/`, and the Dependabot
file beside them, keep the house rules.

    python tools/ci/check_workflows.py [ROOT]

zizmor reads the same files for what an attacker could reach, and three of
the rules below are zizmor audits too (an unpinned action, an expression in
`run:`, a checkout that keeps its token). This script holds every house
rule, standard library only, and runs where zizmor is not installed:

- every `uses:` names a full 40-hex commit SHA with its version in a comment
  beside it (`# v7.0.1`), so Dependabot can update both and a reader can see
  which release runs; a local action (`./path`) is exempt, and a
  `docker://` image needs its `@sha256:` digest;
- every job sets `timeout-minutes` (GitHub's default is 360 minutes), except
  a job that calls a reusable workflow, where GitHub allows no timeout;
- no `${{ }}` expression inside a `run:` line. A value an event gives
  reaches the shell through `env:` as a variable the script reads, so its
  text never becomes part of the script;
- the top level sets `permissions:` to `contents: read` and nothing else. A
  job may be given more: each entry other than `contents: read` or `none`
  carries a comment beside it that says why, and a job's `write-all` or
  `read-all`, which names no entry, is refused;
- every `actions/checkout` step sets `persist-credentials: false`;
- in a workflow that a schedule or `workflow_dispatch` starts, every
  concurrency group holds `github.event_name`, so the weekly run, a run by
  hand and a push to master never share a group;
- every check name `tools/ci.py --list` gives is the `name:` of exactly one
  job, and that job runs `tools/ci.py` with the job key the name belongs to.
  The master ruleset requires checks by name, so a renamed job leaves a
  required check waiting forever, and a second job with the name could
  answer for it. A `${{ matrix.X }}` in a name is expanded from the job's
  matrix;
- a step that calls `tools/ci.py` runs `python tools/ci.py <key> --install`
  and nothing else, and takes no `if:`, `shell:` or `continue-on-error:`;
  neither its job nor any other step of it sets `continue-on-error:`, and
  neither the job nor the workflow names a default shell. The job's result
  is then the script's exit code, which nothing after the call can turn
  green;
- every entry of `.github/dependabot.yml` waits seven days or more before
  it proposes a release (`cooldown:`, every `*-days` key in it), and none
  narrows the wait to some dependencies with `include:` or `exclude:`.

The files are read line by line, as this repository writes them: block
style, a list indented under its key. A spelling the reader does not
follow is itself a problem, so no rule passes because a line went unread:
a flow mapping, an anchor, an alias or a tag, a list at its key's
indentation, and a value that goes on past its own line. The standard
library has no YAML reader, and a script the `layering` job runs on a bare
Python can import nothing else.

Exit 0 when every rule holds, 1 with each problem as `file:line: what`.
"""

from __future__ import annotations

import importlib.util
import itertools
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
DIGEST = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")
VERSION_COMMENT = re.compile(r"^#\s*v\d")
BLOCK_SCALAR = re.compile(r":\s*[|>][-+0-9]*$")
MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
CI_CALL = re.compile(r"python[0-9.]*\s+tools/ci\.py((?:\s+[^\s#|&;]+)*)")
CI_LINE = re.compile(r"^python tools/ci\.py [a-z0-9][a-z0-9-]* --install$")
ITEM = re.compile(r"^-(?:\s+|$)")
KEY = re.compile(r"""^(["']?)([A-Za-z0-9_.-]+)\1\s*:(?:\s+(.*))?$""")
FLOW_MAPPING = re.compile(r"(?<![$\{])\{")     # a `{` that does not open `${{`
WAIT_DAYS = 7


@dataclass
class Line:
    number: int
    indent: int
    code: str            # the line without its indentation and its comment
    comment: str = ""
    scalar: bool = False  # a line inside a `|` or `>` block
    spelling: str = ""    # why the reader cannot follow this line, or ""


@dataclass
class Job:
    key: str
    number: int
    props: dict[str, Line] = field(default_factory=dict)
    lines: list[Line] = field(default_factory=list)


def strip_comment(text: str) -> str:
    """The text before a `#` that starts a comment: one at the start or
    after a space, outside quotes."""
    quote = ""
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def item_prefix(code: str) -> int:
    """The width of a list item's dash and the spaces after it; 0 when the
    line is no list item."""
    match = ITEM.match(code)
    return match.end() if match else 0


def key_of(code: str) -> tuple[str, str]:
    """`- key: value` or `key: value` as (key, value); ("", "") when the
    line is not a mapping entry."""
    match = KEY.match(code[item_prefix(code):])
    return (match.group(2), (match.group(3) or "").strip()) if match else ("", "")


def unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


def left_open(value: str) -> bool:
    """A value that opens a quote and does not close it on its line."""
    if not value or value[0] not in "'\"":
        return False
    quote, i = value[0], 1
    while i < len(value):
        if quote == '"' and value[i] == "\\":
            i += 2
            continue
        if value[i] == quote:
            if quote == "'" and value[i + 1:i + 2] == "'":
                i += 2
                continue
            return False
        i += 1
    return True


def spelling_of(line: Line, before: Line | None) -> str:
    """Why the reader cannot follow `line`, read after `before`, the last
    line outside a block scalar that was no continuation; "" when it can."""
    body = line.code[item_prefix(line.code):]
    key, value = key_of(line.code)
    if before is not None:
        column = before.indent + item_prefix(before.code)
        held_key, held_value = key_of(before.code)
        held = held_value if held_key else before.code[item_prefix(before.code):]
        if line.indent > column and (held or not (key or item_prefix(line.code))):
            return f"the value of line {before.number} goes on here; keep it on one line or write a | block"
        if (line.indent == before.indent and item_prefix(line.code) and held_key and not held_value
                and not item_prefix(before.code)):
            return "a list at its key's indentation; indent it under the key"
    scalar = value if key else body
    if body.startswith("{") or (scalar.startswith(("{", "[")) and scalar != "{}" and FLOW_MAPPING.search(scalar)):
        return "a flow mapping; write it in block style"
    if body.startswith("<<") or scalar.startswith(("&", "*", "!")):
        return "an anchor, an alias or a tag; write the value out in full"
    if left_open(scalar):
        return "a quoted value left open at the end of its line; keep it on one line or write a | block"
    return ""


def read_lines(text: str) -> list[Line]:
    """Every line that holds something, blank lines and comment lines left
    out, each inside a block scalar marked as such, and each the reader
    cannot follow given its reason."""
    lines: list[Line] = []
    opened_at: int | None = None        # a `|` block's lines are indented past this column
    before: Line | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if opened_at is not None:
            if not stripped or indent > opened_at:
                if stripped:
                    lines.append(Line(number, indent, stripped, scalar=True))
                continue
            opened_at = None
        if not stripped or stripped.startswith("#"):
            continue
        code = strip_comment(stripped)
        line = Line(number, indent, code, stripped[len(code):].strip())
        line.spelling = spelling_of(line, before)
        lines.append(line)
        if BLOCK_SCALAR.search(code):
            opened_at = indent + item_prefix(code)
        if not line.spelling.startswith("the value of line"):
            before = line
    return lines


def block(lines: list[Line], start: int) -> list[Line]:
    """The lines nested under `lines[start]`: those after it, up to the next
    line at its indentation or less. Block-scalar lines are always nested."""
    head = lines[start]
    out = []
    for line in lines[start + 1:]:
        if not line.scalar and line.indent <= head.indent:
            break
        out.append(line)
    return out


def items_of(lines: list[Line], start: int) -> list[list[Line]]:
    """Each item of the list nested under `lines[start]`, as its lines, the
    dash's line first."""
    body = block(lines, start)
    heads = [line for line in body if item_prefix(line.code) and not line.scalar]
    if not heads:
        return []
    indent = heads[0].indent
    items: list[list[Line]] = []
    for line in body:
        if item_prefix(line.code) and line.indent == indent and not line.scalar:
            items.append([line])
        elif items:
            items[-1].append(line)
    return items


def top_level(lines: list[Line]) -> dict[str, int]:
    return {key_of(line.code)[0]: i for i, line in enumerate(lines) if line.indent == 0 and not line.scalar}


def jobs_of(lines: list[Line]) -> list[Job]:
    at = top_level(lines).get("jobs")
    if at is None:
        return []
    body = block(lines, at)
    if not body:
        return []
    job_indent = body[0].indent
    jobs: list[Job] = []
    for line in body:
        if line.indent == job_indent and not line.scalar:
            jobs.append(Job(key_of(line.code)[0], line.number))
        elif jobs:
            jobs[-1].lines.append(line)
    for job in jobs:
        if job.lines:
            prop_indent = job.lines[0].indent
            for line in job.lines:
                if line.indent == prop_indent and not line.scalar and not item_prefix(line.code):
                    job.props[key_of(line.code)[0]] = line
    return jobs


def steps_of(job: Job) -> list[list[Line]]:
    """Each step of a job as its lines, the dash's line first."""
    if "steps" not in job.props:
        return []
    return items_of(job.lines, job.lines.index(job.props["steps"]))


def matrix_of(job: Job) -> dict[str, list[str]]:
    """The job's `strategy.matrix` lists written in flow style, as `X: [a, b]`."""
    found: dict[str, list[str]] = {}
    for i, line in enumerate(job.lines):
        if key_of(line.code)[0] == "matrix" and not line.scalar:
            for inner in block(job.lines, i):
                key, value = key_of(inner.code)
                if key and value.startswith("[") and value.endswith("]"):
                    found[key] = [unquote(v.strip()) for v in value[1:-1].split(",") if v.strip()]
    return found


def check_names(job: Job) -> list[str]:
    """The job's `name:` with its matrix expanded; the job key when unnamed."""
    name = unquote(key_of(job.props["name"].code)[1]) if "name" in job.props else job.key
    refs = list(dict.fromkeys(MATRIX_REF.findall(name)))
    if not refs:
        return [name]
    matrix = matrix_of(job)
    if any(ref not in matrix for ref in refs):
        return [name]
    names = []
    for values in itertools.product(*(matrix[ref] for ref in refs)):
        expanded = name
        for ref, value in zip(refs, values):
            expanded = re.sub(r"\$\{\{\s*matrix\." + re.escape(ref) + r"\s*\}\}", value, expanded)
        names.append(expanded)
    return names


def run_texts(lines: list[Line]) -> list[tuple[Line, str]]:
    """Every shell line a `run:` holds, with the line it is on."""
    texts: list[tuple[Line, str]] = []
    for i, line in enumerate(lines):
        if line.scalar or key_of(line.code)[0] != "run":
            continue
        value = key_of(line.code)[1]
        if BLOCK_SCALAR.search(line.code):
            texts += [(inner, inner.code) for inner in block(lines, i) if inner.scalar]
        else:
            texts.append((line, unquote(value)))
    return texts


def ci_keys(job: Job) -> list[str]:
    keys: list[str] = []
    for _, text in run_texts(job.lines):
        for match in CI_CALL.finditer(text):
            keys += [word for word in match.group(1).split() if not word.startswith("-")]
    return keys


def sets_shell(lines: list[Line], at: int) -> bool:
    """Whether the `defaults:` at `lines[at]` names a shell."""
    return any(key_of(line.code)[0] == "shell" for line in block(lines, at) if not line.scalar)


def triggers(lines: list[Line]) -> set[str]:
    """The events under the top-level `on:`, in any of its three forms."""
    at = top_level(lines).get("on")
    if at is None:
        return set()
    value = key_of(lines[at].code)[1]
    if value:
        return {unquote(v.strip()) for v in value.strip("[]").split(",") if v.strip()}
    entries = [line for line in block(lines, at) if not line.scalar]
    if not entries:
        return set()
    level = entries[0].indent
    return {key_of(line.code)[0] or unquote(line.code[item_prefix(line.code):])
            for line in entries if line.indent == level}


def concurrency_group(lines: list[Line], at: int) -> str:
    """The group a `concurrency:` names: its value in the short form, or its
    `group:`."""
    value = key_of(lines[at].code)[1]
    if value:
        return value
    return next((key_of(line.code)[1] for line in block(lines, at) if key_of(line.code)[0] == "group"), "")


def load_checks(root: Path) -> dict[str, tuple[str, ...]] | None:
    """Each `tools/ci.py` job key and its GitHub check names, from the script
    itself, so this file keeps no list of its own."""
    path = root / "tools" / "ci.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("emet_tools_ci_for_check_workflows", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    writes = sys.dont_write_bytecode
    sys.dont_write_bytecode = True        # leave no __pycache__ in the tree
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes
        sys.modules.pop(spec.name, None)
    return {job.key: tuple(job.checks) for job in module.JOBS}


def check_file(path: Path, shown: str) -> tuple[list[str], list[tuple[str, int, list[str], list[str]]]]:
    """The problems of one workflow, and each job's (key, line, check names,
    tools/ci.py keys) for the name rule."""
    lines = read_lines(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    def problem(number: int, text: str) -> None:
        problems.append(f"{shown}:{number}: {text}")

    for line in lines:
        if line.spelling:
            problem(line.number, line.spelling)

    for line in lines:
        if line.scalar or key_of(line.code)[0] != "uses":
            continue
        target = unquote(key_of(line.code)[1])
        if target.startswith("./"):
            continue
        if target.startswith("docker://"):
            if not DIGEST.match(target):
                problem(line.number, f"{target} is not pinned to an image digest (@sha256:...)")
        elif not SHA.match(target):
            problem(line.number, f"{target} is not pinned to a full commit SHA")
        elif not VERSION_COMMENT.match(line.comment):
            problem(line.number, f"{target} has no version comment beside it (# vX.Y.Z)")

    for line, text in run_texts(lines):
        if "${{" in text:
            problem(line.number, "an expression inside a run: line; pass the value through env:")

    head = top_level(lines)
    if "permissions" not in head:
        problem(1, "no top-level permissions: (contents: read)")
    else:
        at = head["permissions"]
        inline = key_of(lines[at].code)[1]
        entries = [key_of(line.code) for line in block(lines, at)]
        if inline or entries != [("contents", "read")]:
            problem(lines[at].number, "the top-level permissions: must be contents: read and nothing else")

    if triggers(lines) & {"schedule", "workflow_dispatch"}:
        places = [("the workflow", lines, head["concurrency"])] if "concurrency" in head else []
        places += [(f"job {job.key}", job.lines, job.lines.index(job.props["concurrency"]))
                   for job in jobs_of(lines) if "concurrency" in job.props]
        for who, where, at in places:
            if "github.event_name" not in concurrency_group(where, at):
                problem(where[at].number, f"{who}: a concurrency group without github.event_name, in a workflow "
                                          "a schedule or a run by hand starts; the weekly run and a push to "
                                          "master would share it")

    default_shell = "defaults" in head and sets_shell(lines, head["defaults"])
    names: list[tuple[str, int, list[str], list[str]]] = []
    for job in jobs_of(lines):
        if "uses" not in job.props and "timeout-minutes" not in job.props:
            problem(job.number, f"job {job.key} has no timeout-minutes")

        if "permissions" in job.props:
            at = job.lines.index(job.props["permissions"])
            inline = key_of(job.lines[at].code)[1]
            if inline and inline != "{}":
                problem(job.lines[at].number, f"job {job.key}: permissions: {inline} names no entry; "
                                              "list each with the reason beside it")
            for line in block(job.lines, at):
                key, value = key_of(line.code)
                if (key, value) == ("contents", "read") or value == "none" or line.scalar:
                    continue
                if not line.comment.lstrip("#").strip():
                    problem(line.number, f"job {job.key}: permissions {key}: {value} has no comment saying why")

        calls_ci = False
        for step in steps_of(job):
            uses = next((unquote(key_of(line.code)[1]) for line in step
                         if not line.scalar and key_of(line.code)[0] == "uses"), "")
            if uses.startswith("actions/checkout@") and not any(
                    key_of(line.code) == ("persist-credentials", "false") for line in step if not line.scalar):
                problem(step[0].number, f"job {job.key}: a checkout without persist-credentials: false")
            shell = run_texts(step)
            if not any("tools/ci.py" in text for _, text in shell):
                continue
            calls_ci = True
            if len(shell) != 1 or not CI_LINE.match(shell[0][1].strip()):
                problem(shell[0][0].number, f"job {job.key}: a step that calls tools/ci.py runs "
                                            "`python tools/ci.py <key> --install` and nothing else")
            for line in step:
                key = key_of(line.code)[0]
                if not line.scalar and key in ("if", "shell", "continue-on-error"):
                    problem(line.number, f"job {job.key}: the step that runs tools/ci.py takes no {key}:, "
                                         "so the job's result is the script's exit code")
        if calls_ci:
            if "continue-on-error" in job.props:
                problem(job.props["continue-on-error"].number,
                        f"job {job.key} runs tools/ci.py and takes no continue-on-error:")
            for step in steps_of(job):
                if any("tools/ci.py" in text for _, text in run_texts(step)):
                    continue
                for line in step:
                    if not line.scalar and key_of(line.code)[0] == "continue-on-error":
                        problem(line.number, f"job {job.key} runs tools/ci.py, so no step of it takes "
                                             "continue-on-error:")
            if default_shell:
                problem(lines[head["defaults"]].number, f"job {job.key} runs tools/ci.py under a default shell "
                                                        "the workflow names; leave GitHub's")
            if "defaults" in job.props and sets_shell(job.lines, job.lines.index(job.props["defaults"])):
                problem(job.props["defaults"].number, f"job {job.key} runs tools/ci.py under a default shell "
                                                      "it names; leave GitHub's")
        names.append((job.key, job.number, check_names(job), ci_keys(job)))
    return problems, names


def check_dependabot(root: Path) -> list[str]:
    """Every entry of the Dependabot file waits `WAIT_DAYS` or more."""
    path = next((root / ".github" / name for name in ("dependabot.yml", "dependabot.yaml")
                 if (root / ".github" / name).is_file()), None)
    if path is None:
        return [".github/dependabot.yml is missing, so no pin is kept current"]
    shown = path.relative_to(root).as_posix()
    lines = read_lines(path.read_text(encoding="utf-8"))
    problems = [f"{shown}:{line.number}: {line.spelling}" for line in lines if line.spelling]
    at = top_level(lines).get("updates")
    if at is None:
        return [*problems, f"{shown}:1: no updates:"]
    for item in items_of(lines, at):
        head = item[0]
        column = head.indent + item_prefix(head.code)
        props = {key_of(line.code)[0]: (i, line) for i, line in enumerate(item)
                 if i == 0 or (line.indent == column and not line.scalar)}
        ecosystem = key_of(props["package-ecosystem"][1].code)[1] if "package-ecosystem" in props else "?"
        folder = next((key_of(props[k][1].code)[1] for k in ("directory", "directories") if k in props), "?")
        entry = f"the {ecosystem} entry for {folder}"
        if "cooldown" not in props:
            problems.append(f"{shown}:{head.number}: {entry} has no cooldown:; every entry waits "
                            f"{WAIT_DAYS} days or more")
            continue
        at_cooldown, cooldown = props["cooldown"]
        inside = [line for line in block(item, at_cooldown) if not line.scalar]
        if not any(key_of(line.code)[0] == "default-days" for line in inside):
            problems.append(f"{shown}:{cooldown.number}: {entry} has no default-days: under cooldown:")
        for line in inside:
            key, value = key_of(line.code)
            value = unquote(value)
            if key.endswith("-days") and (not value.isdigit() or int(value) < WAIT_DAYS):
                problems.append(f"{shown}:{line.number}: {entry} waits {key}: {value}; every wait is "
                                f"{WAIT_DAYS} days or more")
            if key in ("include", "exclude"):
                problems.append(f"{shown}:{line.number}: {entry} narrows the wait with {key}:; "
                                "every dependency waits")
    return problems


def check(root: Path) -> list[str]:
    folder = root / ".github" / "workflows"
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    if not files:
        return [f"{folder}: no workflow files"]
    problems: list[str] = []
    named: list[tuple[str, str, list[str], list[str]]] = []      # where, job key, check names, ci.py keys
    for path in files:
        shown = path.relative_to(root).as_posix()
        found, jobs = check_file(path, shown)
        problems += found
        named += [(f"{shown}:{number}", key, names, keys) for key, number, names, keys in jobs]
    problems += check_dependabot(root)

    checks = load_checks(root)
    if checks is None:
        return [*problems, "tools/ci.py is missing, so the check names cannot be held to it"]
    owner = {name: key for key, names in checks.items() for name in names}
    seen: dict[str, str] = {}
    for where, key, names, keys in named:
        unknown = [k for k in keys if k not in checks]
        if unknown:
            problems.append(f"{where}: job {key} runs tools/ci.py {' '.join(unknown)}, which is no job of tools/ci.py")
        elif keys:
            wanted = sorted({name for k in keys for name in checks[k]})
            if sorted(set(names)) != wanted:
                problems.append(f"{where}: job {key} is named {', '.join(sorted(set(names)))}; "
                                f"tools/ci.py {' '.join(keys)} reports as {', '.join(wanted)}")
        else:
            for name in names:
                if name in owner:
                    problems.append(f"{where}: job {key} is named {name}, the check of tools/ci.py "
                                    f"{owner[name]}, and runs no tools/ci.py")
        for name in names:
            if name not in owner:
                continue
            if name in seen:
                problems.append(f"{where}: the check name {name} is also {seen[name]}'s")
            seen.setdefault(name, where)
    for key, names in checks.items():
        for name in names:
            if name not in seen:
                problems.append(f"tools/ci.py: the check {name} (job {key}) is no workflow job's name")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0] if args else ".").resolve()
    problems = check(root)
    for line in problems:
        print(f"  x {line}")
    files = len([*(root / ".github" / "workflows").glob("*.y*ml")])
    print(f"workflows: {len(problems)} problem(s)" if problems else f"workflows: ok, {files} files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
