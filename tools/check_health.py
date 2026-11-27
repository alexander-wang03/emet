#!/usr/bin/env python3
"""Hold the SDK to its line budget, and keep the code's health from slipping.

    sdk lines     the SDK's contract modules stay at or under 4,000 lines.
                  Required: over it, this exits 1
    the budget    per package, numbers that may fall and may not rise: its
                  lines, its functions over a complexity of 10, its highest
                  complexity, its longest function, its unused names.
                  Advisory: a rise is a warning, and exit 1 with --strict
    the report    the ten worst functions by complexity and by length, with
                  file and line, and vulture's list when it ran

**Why this exists.** The maintainer asked for "refactoring, regularly,
cleaning up regularly", and the code is written by many agents at once, each
adding a branch to a function that was already long, and nothing kept count.
A number in a file can be compared, so `tools/health_budget.json` holds
today's numbers, each with the date it was measured and the commit the
measured tree sat on (marked `-dirty` when the four packages held changes
not yet committed, as they do when a pull request lowers a number in the
same diff as the code), and a pull request that makes one worse hears about
it. One that makes a number better lowers the budget in the same diff, so
the gain is kept. He et al., MSR 2026, found that projects adopting Cursor's
agent saw a persistent rise in static-analysis warnings and code complexity
(`CITATIONS.md`); that finding is why these numbers are held. The script's
shape is `check_layering.py`'s.

**The SDK's line count** is `wc -l`'s: the newline characters in every
`.py` under `emet-sdk/emet_sdk/`, packages inside it included, except
`cli.py` and the `testing/` folder a conformance kit would be.
`DESIGN.md` section 3 calls the SDK "types and contracts and almost no
logic", so the cap guards the contract and the command line and the kit sit
outside it (decision E8, 2026-11-25).

**Complexity, exactly.** One for the function, plus one for each of these
inside it: `if` and `elif`; a conditional expression (`a if b else c`); `for`
and `async for`; `while`; each `except` clause; each `case` of a `match`;
each `for` of a comprehension and each `if` filtering one; and each `and` or
`or` beyond the first operand of its expression. Nothing for `else`,
`finally`, `with`, `try` itself or `assert`. A function defined inside
another is counted as its own function, and its branches are left out of
the outer one's; a lambda's belong to the function it is written in. This
approximates McCabe's count and matches no one tool's; it is written out so
the number can be checked by hand.

**A function's length** is from its `def` line to its last line, both
counted, with its docstring, comments and blank lines; decorators are not.

**Unused names** come from vulture, when it is importable, on `PATH` or in
`tools/.venv`. It is run once over the four packages, from 60% confidence.
vulture 2.16 gives 100% to an unused function argument (which it words as
"unused variable") and to unreachable code, 90% to an unused import, and
60% to everything else, a variable or an attribute included (its `core.py`,
verified 2026-11-26). The budget counts what it reports at 80% or more:
unused imports, unused function arguments and unreachable code
(`VULTURE_COUNTED`). The report also prints what it finds from 60%: the
variables, attributes, functions, methods, properties and classes nothing
in the four packages uses (`VULTURE_LISTED`), many of them public names a
plugin or a test calls. Those are candidates to read, so they are listed and
left out of the budget. Without vulture that part says "not run" and its
budget is left alone: an absent tool measured nothing, which is different
from measuring zero. A budget counted by another vulture version is said,
and `--update` lowers none of its numbers.

**The budget only falls.** `--update` writes the budget file when there is
none, lowers each number that fell, and adds a number measured for the first
time. It never raises one, and it writes nothing over a budget measured at
other thresholds. A number rises only by hand, in the file, with its date
and commit, where the pull request's diff shows it; deleting the file and
measuring afresh is the same decision made for every number at once.

Dependency-free and short enough to read, like `check_layering.py`. vulture
is run as a program and never imported.

Usage:  python tools/check_health.py [repo_root] [--strict] [--update] [--budget FILE]
Exit:   0 clean, or warnings only. 1 when the SDK's contract modules pass
        4,000 lines; with --strict also on any warning: a budgeted number
        that rose, a budget file that is missing, unreadable, or measured at
        other thresholds or by another vulture, or a source file that does
        not parse. 2 when it could not run.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The four packages and where each one's source sits.
PACKAGES: dict[str, str] = {
    "emet_sdk": "emet-sdk/emet_sdk",
    "emet_hal": "emet-hal/emet_hal",
    "emet_providers": "emet-providers/emet_providers",
    "emet_engine": "emet-engine/emet_engine",
}

#: The cap on the SDK's contract modules (`planning/ROADMAP.md`, 0.6; E8).
SDK_LINE_CAP = 4000

#: What directly under `emet_sdk/` is outside the contract, so outside the
#: cap (decision E8): the command line, and the folder of the conformance kit
#: 1.0 adds, with everything in it. The same set `api_diff.py` leaves out of
#: the public names.
OUTSIDE_THE_CONTRACT = frozenset({"cli.py", "testing"})

#: A function is counted as complex when its complexity passes this.
COMPLEXITY_OVER = 10

#: vulture's confidence, in percent: what the budget counts, what is listed.
VULTURE_BUDGET_CONFIDENCE = 80
VULTURE_LIST_CONFIDENCE = 60

#: What vulture 2.16 reports at each of the two, as the report says it
#: (vulture's `core.py`: 100 for an argument and for unreachable code, 90
#: for an import, 60 for the rest; verified 2026-11-26).
VULTURE_COUNTED = 'unused imports, unused function arguments (vulture words them "unused variable") and unreachable code'
VULTURE_LISTED = "unused variables, attributes, functions, methods, properties and classes"

#: How many of the worst functions the report names.
WORST = 10

BUDGET_NAME = "health_budget.json"
BUDGET_FORMAT = "emet-health-budget/1"

#: The budgeted numbers, in the order they print, with their column heads.
MEASURES: dict[str, str] = {
    "lines": "lines",
    "over_complexity": f"over {COMPLEXITY_OVER}",
    "highest_complexity": "highest",
    "longest_function": "longest",
    "unused_names": "unused",
}

_VULTURE_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<what>.+) \((?P<pct>\d+)% confidence[^)]*\)\s*$")


# --------------------------------------------------------------------------
# Measuring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Function:
    package: str
    path: str        # relative to the root, forward slashes
    line: int
    name: str        # dotted, with the classes and functions it sits in
    complexity: int
    length: int


@dataclass
class Unused:
    """What vulture said, or why it said nothing."""

    ran: bool
    reason: str = ""                                  # why not, when it did not run
    version: str = ""
    findings: list[tuple[str, int, str, int]] = field(default_factory=list)  # path, line, what, percent

    def count(self, package: str) -> int:
        prefix = PACKAGES[package] + "/"
        return sum(1 for path, _, _, pct in self.findings
                   if path.startswith(prefix) and pct >= VULTURE_BUDGET_CONFIDENCE)


@dataclass
class Measurement:
    sdk_contract_lines: int
    sdk_outside_lines: int
    numbers: dict[str, dict[str, int | None]]         # package -> measure -> value
    functions: list[Function]
    unused: Unused
    unparsed: list[str] = field(default_factory=list)


def line_count(path: Path) -> int:
    """`wc -l`'s count: the newline characters in the file."""
    return path.read_bytes().count(b"\n")


def sdk_lines(root: Path) -> tuple[int, int]:
    """The SDK's lines inside the contract, and those outside it: every
    `.py` at any depth, a package inside the SDK included, with what
    `OUTSIDE_THE_CONTRACT` names directly under `emet_sdk/` outside."""
    inside = outside = 0
    sdk = root / PACKAGES["emet_sdk"]
    for path in sorted(sdk.rglob("*.py")):
        relative = path.relative_to(sdk).parts
        if "__pycache__" in relative:
            continue
        if relative[0] in OUTSIDE_THE_CONTRACT:
            outside += line_count(path)
        else:
            inside += line_count(path)
    return inside, outside


def outside_text() -> str:
    """`OUTSIDE_THE_CONTRACT` as the report names it: `cli.py and testing/`."""
    return " and ".join(sorted(n if n.endswith(".py") else f"{n}/" for n in OUTSIDE_THE_CONTRACT))


def _own_nodes(function: ast.AST):
    """Every node of a function, without the functions defined inside it."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def complexity(function: ast.AST) -> int:
    """The count the module's docstring spells out."""
    total = 1
    for node in _own_nodes(function):
        if isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While,
                             ast.ExceptHandler, ast.match_case)):
            total += 1
        elif isinstance(node, ast.comprehension):
            total += 1 + len(node.ifs)
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
    return total


def functions_in(source: str, package: str, path: str) -> list[Function]:
    """Every function and method of one file, nested ones included."""
    found: list[Function] = []

    def visit(node: ast.AST, outer: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = (*outer, child.name)
                last = child.end_lineno or child.lineno
                found.append(Function(package, path, child.lineno, ".".join(name),
                                      complexity(child), last - child.lineno + 1))
                visit(child, name)
            elif isinstance(child, ast.ClassDef):
                visit(child, (*outer, child.name))
            else:
                visit(child, outer)

    visit(ast.parse(source), ())
    return found


def find_vulture(root: Path) -> list[str] | None:
    """The command that runs vulture here, or None when there is none."""
    if importlib.util.find_spec("vulture") is not None:
        return [sys.executable, "-m", "vulture"]
    on_path = shutil.which("vulture")
    if on_path:
        return [on_path]
    for candidate in (root / "tools" / ".venv" / "Scripts" / "vulture.exe",
                      root / "tools" / ".venv" / "bin" / "vulture"):
        if candidate.is_file():
            return [str(candidate)]
    return None


def _run(command: list[str], root: Path) -> tuple[int, str]:
    try:
        done = subprocess.run(command, cwd=root, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return done.returncode, done.stdout.decode("utf-8", errors="replace")


def run_vulture(root: Path, command: list[str] | None) -> Unused:
    if command is None:
        return Unused(False, "vulture is not importable, on PATH or in tools/.venv")
    code, out = _run([*command, "--version"], root)
    if code != 0:
        return Unused(False, f"`{command[-1]} --version` failed")
    version = out.strip().split()[-1] if out.strip() else "unknown"
    folders = [where for where in PACKAGES.values() if (root / where).is_dir()]
    code, out = _run([*command, *folders, "--min-confidence", str(VULTURE_LIST_CONFIDENCE)], root)
    # vulture exits 0 with nothing to report and 3 with dead code found; 1 and
    # 2 are a file it could not read and a bad command line (vulture 2.16's
    # `utils.ExitCode`, verified 2026-11-26).
    if code not in (0, 3):
        return Unused(False, f"vulture {version} exited {code}")
    findings = []
    for line in out.splitlines():
        match = _VULTURE_LINE.match(line)
        if match:
            findings.append((match["path"].replace("\\", "/"), int(match["line"]),
                             match["what"], int(match["pct"])))
    return Unused(True, version=version, findings=sorted(findings))


def measure(root: Path, vulture: list[str] | None) -> Measurement:
    inside, outside = sdk_lines(root)
    numbers: dict[str, dict[str, int | None]] = {}
    functions: list[Function] = []
    unparsed: list[str] = []
    unused = run_vulture(root, vulture)
    for package, where in PACKAGES.items():
        folder = root / where
        if not folder.is_dir():
            continue
        lines = 0
        own: list[Function] = []
        for path in sorted(folder.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            lines += line_count(path)
            try:
                own += functions_in(path.read_text(encoding="utf-8"), package, rel)
            except SyntaxError as exc:
                unparsed.append(f"{rel}: could not be parsed: {exc.msg}, line {exc.lineno}")
        functions += own
        numbers[package] = {
            "lines": lines,
            "over_complexity": sum(1 for f in own if f.complexity > COMPLEXITY_OVER),
            "highest_complexity": max((f.complexity for f in own), default=0),
            "longest_function": max((f.length for f in own), default=0),
            "unused_names": unused.count(package) if unused.ran else None,
        }
    return Measurement(inside, outside, numbers, functions, unused, unparsed)


# --------------------------------------------------------------------------
# The budget file
# --------------------------------------------------------------------------


class BudgetError(Exception):
    """The budget file is there and cannot be used."""


def read_budget(path: Path) -> dict[str, Any] | None:
    """The budget file's contents, or None when there is no file yet."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BudgetError(f"{path.name} is no JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("budgets"), dict):
        raise BudgetError(f'{path.name} has no "budgets" table')
    for package, entries in data["budgets"].items():
        if not isinstance(entries, dict):
            raise BudgetError(f"{path.name}: {package} is no table")
        for measure_name, entry in entries.items():
            value = entry.get("budget") if isinstance(entry, dict) else None
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise BudgetError(f"{path.name}: {package}.{measure_name} has no whole number under \"budget\"")
    return data


def budget_of(data: dict[str, Any] | None, package: str, measure_name: str) -> int | None:
    if data is None:
        return None
    entry = data["budgets"].get(package, {}).get(measure_name)
    return entry["budget"] if entry else None


def _entry(value: int, date: str, commit: str, tool: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {"budget": value, "date": date, "commit": commit}
    if tool:
        entry["tool"] = tool
    return entry


def _tool(measured: Measurement, measure_name: str) -> str:
    """What counted a number, when it was a tool outside this file."""
    return f"vulture {measured.unused.version}" if measure_name == "unused_names" else ""


def new_budget(measured: Measurement, date: str, commit: str) -> dict[str, Any]:
    """A budget file's contents from one measurement. Used when there is none."""
    data: dict[str, Any] = {
        "format": BUDGET_FORMAT,
        "about": ("The code-health numbers tools/check_health.py holds, each with the date it was "
                  "measured and the commit the measured tree sat on, marked -dirty when the four "
                  "packages held changes not yet committed. A number may fall and may not rise: "
                  "`python tools/check_health.py . --update` lowers what fell and never raises one."),
        "complexity_over": COMPLEXITY_OVER,
        "vulture_min_confidence": VULTURE_BUDGET_CONFIDENCE,
        "budgets": {},
    }
    lower_budget(data, measured, date, commit)
    return data


def lower_budget(data: dict[str, Any], measured: Measurement, date: str, commit: str) -> list[str]:
    """Lower each number that fell and add each measured for the first time.

    Returns one sentence a change. A number that rose is left as it is: this
    function has no line that writes a higher budget over a lower one.
    """
    changes: list[str] = []
    for package, numbers in measured.numbers.items():
        entries = data["budgets"].setdefault(package, {})
        for measure_name in MEASURES:
            value = numbers.get(measure_name)
            if value is None:
                continue  # not measured here: the budget stays
            old = entries.get(measure_name, {}).get("budget")
            if old is not None and entries[measure_name].get("tool", "") != _tool(measured, measure_name):
                continue  # counted by another tool, so this number is no fall from it
            if old is None:
                entries[measure_name] = _entry(value, date, commit, _tool(measured, measure_name))
                changes.append(f"{package} {measure_name}: {value}, measured for the first time")
            elif value < old:
                entries[measure_name] = _entry(value, date, commit, _tool(measured, measure_name))
                changes.append(f"{package} {measure_name}: lowered from {old} to {value}")
    return changes


def budget_text(data: dict[str, Any]) -> str:
    """The budget file's text: JSON, with each number on a line of its own,
    so a lowered budget is one changed line in a pull request."""
    lines = ["{"]
    for key, value in data.items():
        if key != "budgets":
            lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    lines.append('  "budgets": {')
    packages = list(data["budgets"].items())
    for p, (package, entries) in enumerate(packages):
        lines.append(f"    {json.dumps(package)}: {{")
        rows = list(entries.items())
        for r, (name, entry) in enumerate(rows):
            comma = "," if r + 1 < len(rows) else ""
            lines.append(f"      {json.dumps(name)}: {json.dumps(entry)}{comma}")
        lines.append("    }" + ("," if p + 1 < len(packages) else ""))
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def write_budget(path: Path, data: dict[str, Any]) -> None:
    path.write_text(budget_text(data), encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# Comparing and printing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    package: str
    measure: str
    value: int | None      # None: not measured here
    budget: int | None     # None: no budget yet

    @property
    def rose(self) -> bool:
        return self.value is not None and self.budget is not None and self.value > self.budget

    @property
    def fell(self) -> bool:
        return self.value is not None and self.budget is not None and self.value < self.budget


def compare(measured: Measurement, data: dict[str, Any] | None) -> list[Row]:
    return [Row(package, name, numbers.get(name), budget_of(data, package, name))
            for package, numbers in measured.numbers.items() for name in MEASURES]


def _cell(value: int | None) -> str:
    return "not run" if value is None else str(value)


def table(measured: Measurement, data: dict[str, Any] | None) -> list[str]:
    """Each package's numbers, with the budget beside any that differs."""
    head = f"  {'package':<16}" + "".join(f"{title:>16}" for title in MEASURES.values())
    lines = [head]
    for package, numbers in measured.numbers.items():
        cells = []
        for name in MEASURES:
            value, budget = numbers.get(name), budget_of(data, package, name)
            text = _cell(value)
            if value is not None and budget is not None and value != budget:
                text += f" ({budget})"
            cells.append(f"{text:>16}")
        lines.append(f"  {package:<16}" + "".join(cells))
    lines.append("  A number in brackets is the budget, shown where the measurement differs from it.")
    return lines


def worst(functions: list[Function]) -> list[str]:
    lines = [f"the {WORST} worst functions by complexity (over {COMPLEXITY_OVER} is counted):"]
    for f in sorted(functions, key=lambda f: (-f.complexity, f.path, f.line))[:WORST]:
        lines.append(f"  {f.complexity:>4}  {f.path}:{f.line}  {f.name}")
    lines.append(f"the {WORST} longest functions, in lines:")
    for f in sorted(functions, key=lambda f: (-f.length, f.path, f.line))[:WORST]:
        lines.append(f"  {f.length:>4}  {f.path}:{f.line}  {f.name}")
    return lines


def unused_lines(unused: Unused) -> list[str]:
    if not unused.ran:
        return [f"unused names: not run ({unused.reason}). Nothing was counted, and the budget stays."]
    counted = [f for f in unused.findings if f[3] >= VULTURE_BUDGET_CONFIDENCE]
    others = [f for f in unused.findings if f[3] < VULTURE_BUDGET_CONFIDENCE]
    lines = [f"unused names (vulture {unused.version}): {len(counted)} at {VULTURE_BUDGET_CONFIDENCE}% "
             f"confidence or more, which the budget counts: {VULTURE_COUNTED}"]
    lines += [f"  {path}:{line}: {what} ({pct}%)" for path, line, what, pct in counted]
    lines.append(f"and {len(others)} from {VULTURE_LIST_CONFIDENCE}%, to read and outside the budget: "
                 f"{VULTURE_LISTED}. A public name a plugin or a test calls shows here too:")
    lines += [f"  {path}:{line}: {what} ({pct}%)" for path, line, what, pct in others]
    return lines


def commit_now(root: Path) -> str:
    """The commit the tree sits on, marked when the measured folders differ from it."""
    code, out = _run(["git", "rev-parse", "--short", "HEAD"], root)
    if code != 0 or not out.strip():
        return "unknown"
    code, changed = _run(["git", "status", "--porcelain", "--", *PACKAGES.values()], root)
    return out.strip() + ("-dirty" if code == 0 and changed.strip() else "")


def check(root: Path, budget_path: Path, *, strict: bool = False, update: bool = False,
          vulture: list[str] | None = None, date: str = "", commit: str = "") -> int:
    """Measure, compare, print, and with `update` rewrite the budget. The exit code."""
    measured = measure(root, vulture)
    date = date or dt.date.today().isoformat()
    commit = commit or commit_now(root)
    shown = budget_path.name
    advisory: list[str] = list(measured.unparsed)

    over = measured.sdk_contract_lines > SDK_LINE_CAP
    left = SDK_LINE_CAP - measured.sdk_contract_lines
    print(f"health: the SDK's contract modules are {measured.sdk_contract_lines} lines of "
          f"{SDK_LINE_CAP} ({outside_text()} outside, {measured.sdk_outside_lines} lines; "
          f"{measured.sdk_contract_lines + measured.sdk_outside_lines} with them): "
          + (f"OVER by {-left}" if over else f"ok, {left} left"))

    data: dict[str, Any] | None = None
    readable = True
    try:
        data = read_budget(budget_path)
    except BudgetError as exc:
        readable = False
        advisory.append(f"{exc}. Mend it by hand, or delete it and run with --update to measure afresh.")

    # Read before anything is written: numbers measured at this tool's
    # thresholds never go into a file that names others.
    elsewhere = False
    if data is not None:
        for key, now in (("complexity_over", COMPLEXITY_OVER),
                         ("vulture_min_confidence", VULTURE_BUDGET_CONFIDENCE)):
            if data.get(key, now) != now:
                elsewhere = True
                advisory.append(f"{shown} was measured with {key} at {data[key]} and this tool counts "
                                f"at {now}, so its numbers mean something else. Delete it and run with "
                                f"--update to measure afresh.")
        if measured.unused.ran:
            now_tool = _tool(measured, "unused_names")
            tools = sorted({str(entries["unused_names"].get("tool", "no named tool"))
                            for entries in data["budgets"].values() if "unused_names" in entries}
                           - {now_tool})
            if tools:
                advisory.append(f"{shown} counted unused names with {' and '.join(tools)} and this run used "
                                f"{now_tool}, which counts by its own rules, so --update lowers none of "
                                f"them. Run the version tools/ci/requirements.txt pins, or delete the file "
                                f"and run with --update to measure afresh.")

    if update and readable:
        if data is None:
            data = new_budget(measured, date, commit)
            write_budget(budget_path, data)
            print(f"health: wrote {shown} from this measurement ({date}, {commit})")
        elif elsewhere:
            print(f"health: {shown} left as it is, since it was measured at other thresholds")
        else:
            changes = lower_budget(data, measured, date, commit)
            if changes:
                write_budget(budget_path, data)
                print(f"health: {shown} rewritten ({date}, {commit}):")
                for change in changes:
                    print(f"  {change}")
            else:
                print(f"health: {shown} left as it is, nothing fell")
    elif data is None and readable:
        advisory.append(f"there is no {shown}, so nothing is held. Write it with --update.")

    print()
    print("\n".join(table(measured, data)))

    rows = compare(measured, data)
    for row in rows:
        what = f"{row.package} {row.measure}"
        if row.rose:
            line = f"{what} rose: {row.value} against a budget of {row.budget}"
            if update:
                line += ". --update never raises a number, so the budget stays"
            line += (f". Bring it back down, or raise it by hand in {shown} with today's date and "
                     f"commit, where the pull request's diff shows it")
            advisory.append(line)
    fallen = [row for row in rows if row.fell]
    if fallen and not update:
        print()
        print(f"below budget: lower {shown} in the same pull request, by `python tools/check_health.py . --update` "
              f"or with these lines:")
        for row in fallen:
            entry = json.dumps(_entry(row.value or 0, date, commit, _tool(measured, row.measure)))
            print(f'  {row.package}: "{row.measure}": {entry}   (was {row.budget})')

    print()
    print("\n".join(worst(measured.functions)))
    print()
    print("\n".join(unused_lines(measured.unused)))
    print()

    if advisory:
        mark = "x" if strict else "!"
        for line in advisory:
            print(f"  {mark} {line}")
        print()
    if over:
        print(f"health: FAILED. The SDK's contract modules are {measured.sdk_contract_lines} lines, "
              f"{-left} over the {SDK_LINE_CAP} the contract is held to. Move logic out of the SDK, "
              f"or take the cap to the maintainer.")
        return 1
    if advisory:
        if strict:
            print(f"health: FAILED under --strict, {len(advisory)} problem(s).")
            return 1
        print(f"health: ok, with {len(advisory)} warning(s). The budget is advisory: "
              f"--strict makes each warning a failure.")
        return 0
    print("health: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_health.py",
        description="Hold the SDK to its line budget and the code-health numbers to theirs.",
    )
    parser.add_argument("root", nargs="?", default=".", help="the repository's root (default: here)")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 on any warning, as well as for the SDK's lines: a budgeted number "
                             "that rose, a budget file missing, unreadable, or measured at other "
                             "thresholds or by another vulture, a source file that does not parse")
    parser.add_argument("--update", action="store_true",
                        help="write the budget file when there is none, lower each number that fell, "
                             "and never raise one")
    parser.add_argument("--budget", metavar="FILE",
                        help=f"the budget file (default: tools/{BUDGET_NAME} under the root)")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    root = Path(args.root).resolve()
    if not (root / PACKAGES["emet_sdk"]).is_dir():
        print(f"health: {root} does not look like the Emet repository", file=sys.stderr)
        return 2
    budget_path = Path(args.budget).resolve() if args.budget else root / "tools" / BUDGET_NAME
    return check(root, budget_path, strict=args.strict, update=args.update, vulture=find_vulture(root))


if __name__ == "__main__":
    raise SystemExit(main())
