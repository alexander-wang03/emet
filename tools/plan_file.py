#!/usr/bin/env python3
"""Read a release's plan file. Every tool that reads one reads it here.

A plan is `releases/vX.Y.Z/plan.md`, a copy of `planning/templates/plan.md`
filled in as the release moves through its ten stages
(`planning/RELEASE_PROCESS.md` §1). Both live beside the repository, in
`Project Emet/`. `read_plan` returns what a script gates on:

    line 1          `# X.Y.Z: <title>`, the version and the title
    the kind        the first run of letters after `- **Kind:**`, above the
                    first `## ` heading: patch, minor, major or hotfix
    the stages      the first table under `## 2. Stages`, five columns
    the ledger      the first table under `## 4. Ledger`, seven columns
    the flag gaps   each table under `## 3. Budgets and measurements` headed
                    `| Flag not run | Reason | Answer | Date |`; a plan
                    without one has none

**The grammar,** frozen with the template's columns on 2026-11-26
(`releases/v0.5.6/contract.md` §2):

- A table belongs to the first `## ` heading above it, and a heading is
  named by how its text starts: `2. Stages`. The stages, the budgets and
  the ledger each have one heading. A later heading whose text starts the
  same is a problem, since nothing under it is read.
- A row is a line whose first character, after any indent, is `|`. Cells
  are split on each `|` that no backslash precedes, and stripped; a
  backslash and a `|` in a cell are read as `|`. A table is a run of rows.
  Its first row is the header, and a `|---|` row second is skipped. A blank
  line or a line of prose ends it.
- An HTML comment is skipped wherever it is. A line that held only a
  comment neither ends a table nor makes a row.
- A stage row's first cell is `N. Name`, N from 0 to 9, and the table has
  one row for each. Entered is empty or a date. Exited is empty, a date or
  `skipped: <reason>`.
- A ledger ID is `L` and digits, used once. A severity is S1 to S4. The
  state is the State cell's first word, lower case: open, fixed, carried or
  closed. The template's placeholder, `L1` with every other cell empty, is
  skipped. The ledger is the only table under its heading and a blank line
  ends it, so a blank line inside it, a last row written without its first
  `|`, or a second ledger heading is a problem: no row written under a
  ledger heading goes unread.
- A flag-gap row has four cells and names a flag. Its date is empty until
  the maintainer answers, then a date.
- A date is `YYYY-MM-DD` and a real day.
- The parser knows no code fence: inside one, `## ` still starts a section
  and `|` still starts a row. A fenced copy of the template's `## 4. Ledger`
  is therefore a ledger heading, and the real one below it a second.

**Problems are returned, never raised.** `Plan.problems` holds one sentence
for each malformed thing, in file order, each naming its line, but for a
thing the file lacks, which has none. A reader that gates on the plan (the
audit, `check_plan.py`) refuses a plan with any problem. `PlanError` is for
a plan that cannot be read at all: no file, a file that cannot be read, or
no table under `## 2. Stages`.

**What a wrong row leaves in the result.** A row with the wrong number of
cells, and a stage row whose first cell gives no number, are left out. Every
other row is kept as written, with a problem for each cell that breaks its
rule, so a reader that prints the plan still shows it.

**Who calls it.** `tools/release_audit.py` imports it for the merge-day
evidence check. `planning/check_plan.py`, `blocks.py`, `stage0.py` and
`digest.py`, beside the repository, import it by putting `emet/tools` on
`sys.path`. `tables_under` reads every other table of a plan (the scope,
the batch plan, the decisions, the touchpoints) by the same rules, so no
other code parses a plan's tables. `table_under`, the body rows of the
first such table, is kept because contract §2 names it; no reader calls it
today.

Standard library only, and no command line: `planning/check_plan.py` is the
command that checks a plan.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

#: The kinds of release a plan may declare (RELEASE_PROCESS.md §1).
KINDS = ("patch", "minor", "major", "hotfix")

#: A finding's severity (RELEASE_PROCESS.md §1.7).
SEVERITIES = ("S1", "S2", "S3", "S4")

#: A finding's state: the first word of the ledger's State cell.
STATES = ("open", "fixed", "carried", "closed")

#: The states that leave a finding unresolved. A carried finding moves to the
#: next plan still open.
UNRESOLVED = ("open", "carried")

#: How the text of each heading starts, after `## `.
STAGES_HEADING = "2. Stages"
BUDGETS_HEADING = "3. Budgets and measurements"
LEDGER_HEADING = "4. Ledger"

#: How each header cell starts, in the template's order. The evidence
#: column's header goes on to name the release's folder, and two of the
#: ledger's go on to say what the cell holds.
STAGE_COLUMNS = ("Stage", "Entered", "Exited", "Exit criterion", "Evidence")
LEDGER_COLUMNS = ("ID", "Found", "Severity", "Reproduced", "Regression test", "State", "Fixed in")

#: The flag-gap table's header, whole. The table is found by it.
FLAG_GAP_COLUMNS = ("Flag not run", "Reason", "Answer", "Date")

#: How the Exited cell of a stage the kind skips starts.
SKIPPED = "skipped:"

#: The stage numbers a plan's table holds.
STAGE_NUMBERS = tuple(range(10))

#: The headings a gating reader reads a table under, each written once.
GATED_HEADINGS = (STAGES_HEADING, BUDGETS_HEADING, LEDGER_HEADING)

_KIND = "- **Kind:**"
_LETTERS = re.compile(r"[A-Za-z]+")
_TITLE = re.compile(r"# ([0-9]+\.[0-9]+\.[0-9]+): \S.*")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_STAGE = re.compile(r"([0-9])\.\s+(\S.*)")
_ID = re.compile(r"L[0-9]+")
_PIPE = re.compile(r"(?<!\\)\|")
_RULE = re.compile(r":?-+:?")
_NEWLINE = re.compile(r"\r\n|\r|\n")

_ONE_OF_KINDS = "a kind is patch, minor, major or hotfix"
_A_DATE = "YYYY-MM-DD and a real day"
_ESCAPE = "a | inside a cell takes a backslash before it"
_TOO_FEW = "a cell is missing, or a backslash before a | joined two cells"


class PlanError(Exception):
    """The plan cannot be read at all: the file is missing or unreadable, or
    it has no table under `## 2. Stages`. Everything else that is wrong with
    a plan is a sentence in `Plan.problems`."""


@dataclass(frozen=True)
class StageRow:
    number: int          # 0 to 9
    name: str            # "Scope and research"
    entered: str         # "" or "YYYY-MM-DD"
    exited: str          # "", "YYYY-MM-DD" or "skipped: <reason>"
    criterion: str
    evidence: str

    @property
    def done(self) -> bool:
        """Whether the stage is behind the release: exited on a real day, or
        skipped."""
        return is_date(self.exited) or self.exited.startswith(SKIPPED)


@dataclass(frozen=True)
class LedgerRow:
    id: str              # "L1"
    found: str
    severity: str        # "S1" to "S4"
    reproduced: str
    test: str
    state: str           # the cell's first word, lower case: open, fixed, carried, closed
    fixed_in: str

    @property
    def unresolved(self) -> bool:
        """Whether the finding still stands: open, or carried to the next
        plan."""
        return self.state in UNRESOLVED


@dataclass(frozen=True)
class FlagGap:
    flag: str            # as written, backticks removed; the audit holds it to FLAG_ROWS
    reason: str
    answer: str          # the maintainer's words; "" until there are any
    date: str            # as written: a real date, or "" until the answer, unless problems names the row


@dataclass(frozen=True)
class Plan:
    path: Path
    version: str         # from line 1, "# X.Y.Z: <title>"; "" when line 1 names none
    title: str           # "X.Y.Z: <title>", line 1 without "# "
    kind: str            # the first run of letters after "- **Kind:**", lower case; "" when the
                         # line is missing, holds no letter or is the template's; any word but the
                         # four is a problem
    stages: tuple[StageRow, ...]
    ledger: tuple[LedgerRow, ...]
    flag_gaps: tuple[FlagGap, ...]
    problems: tuple[str, ...]   # every malformed row or field, in file order; never raised


@dataclass(frozen=True)
class Table:
    line: int                                     # the header row's line number
    header: tuple[str, ...]                       # the header row's cells
    rows: tuple[tuple[int, tuple[str, ...]], ...]  # line number and cells of each body row


def is_date(text: str) -> bool:
    """Whether `text` is `YYYY-MM-DD` and a real day. `2026-02-30` has the
    shape and is no day."""
    m = _DATE.fullmatch(text)
    if not m:
        return False
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return True


# ------------------------------------------------------------------ tables


def _shown(text: str) -> tuple[list[str | None], int | None]:
    """The text's lines with every HTML comment taken out, and the line an
    unclosed comment opens on (None when each one closes).

    A line that held only a comment, or lay wholly inside one, comes back as
    None: it is skipped, where an empty line would end a table. An unclosed
    comment runs to the end of the file.
    """
    shown: list[str | None] = []
    inside = False
    opened = 0
    for n, line in enumerate(_NEWLINE.split(text), 1):
        kept: list[str] = []
        commented = inside
        at = 0
        while True:
            if inside:
                end = line.find("-->", at)
                if end < 0:
                    break
                inside = False
                at = end + 3
            else:
                start = line.find("<!--", at)
                if start < 0:
                    kept.append(line[at:])
                    break
                kept.append(line[at:start])
                inside = commented = True
                opened = n
                at = start + 4
        rest = "".join(kept)
        shown.append(None if commented and not rest.strip() else rest)
    return shown, (opened if inside else None)


def _cells(line: str) -> tuple[str, ...]:
    """A row's cells: split on each `|` that no backslash precedes, stripped,
    with an escaped `|` read as the character it stands for."""
    body = line.strip()[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return tuple(cell.strip().replace("\\|", "|") for cell in _PIPE.split(body))


def _table(run: list[tuple[int, tuple[str, ...]]]) -> Table:
    (line, header), body = run[0], run[1:]
    if body and all(_RULE.fullmatch(cell) for cell in body[0][1]):
        body = body[1:]
    return Table(line, header, tuple(body))


def _heading_lines(shown: list[str | None], heading: str) -> list[int]:
    """The line of each `## ` heading whose text starts with `heading`, in
    file order."""
    return [n for n, line in enumerate(shown, 1)
            if line is not None and line.startswith("## ") and line[3:].strip().startswith(heading)]


def _heading_line(shown: list[str | None], heading: str) -> int | None:
    """The line of the first `## ` heading whose text starts with `heading`."""
    lines = _heading_lines(shown, heading)
    return lines[0] if lines else None


def _line_run_on(shown: list[str | None], table: Table) -> int | None:
    """The line right under a table, with no blank line between, that is
    neither a row nor a heading. The table ends above it, so a row written
    without its first `|` would be lost there with no word."""
    for n in range(table.line + 1, len(shown) + 1):
        line = shown[n - 1]
        if line is None or line.lstrip().startswith("|"):
            continue
        return n if line.strip() and not line.startswith("#") else None
    return None


def tables_under(text: str, heading: str) -> list[Table]:
    """Every table under the first `## ` heading whose text starts with
    `heading` (`"0. Scope"`), in file order. The section ends at the next
    `## ` heading. No such heading, or no table under it, is an empty list.
    A later heading of the same name is not read; `read_plan` names one as
    a problem for each section it gates on (`GATED_HEADINGS`).

    A caller finds the table it wants by its header: the batch plan under
    `0. Scope` is the one whose header starts `Batch`.
    """
    wanted = heading.lstrip("#").strip()
    shown, _ = _shown(text)
    tables: list[Table] = []
    run: list[tuple[int, tuple[str, ...]]] = []
    inside = False
    for n, line in enumerate(shown, 1):
        if line is None:
            continue
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().startswith(wanted)
            continue
        if not inside:
            continue
        if line.lstrip().startswith("|"):
            run.append((n, _cells(line)))
        elif run:
            tables.append(_table(run))
            run = []
    if run:
        tables.append(_table(run))
    return tables


def table_under(text: str, heading: str) -> list[tuple[int, list[str]]]:
    """The body rows of the first table under the first `## ` heading whose
    text starts with `heading`, each as its line number and its cells. The
    header row, the `|---|` row and HTML comments are skipped. No such
    heading, or no table under it, is an empty list."""
    tables = tables_under(text, heading)
    return [(n, list(cells)) for n, cells in tables[0].rows] if tables else []


# ---------------------------------------------------------------- the plan


class _Problems:
    """What is wrong with a plan, each sentence kept with the line it is
    sorted by."""

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []

    def at(self, line: int, sentence: str) -> None:
        self.found.append((line, f"line {line}: {sentence}"))

    def missing(self, where: int, sentence: str) -> None:
        """Something the file lacks, which has no line of its own; `where`
        places it among the others."""
        self.found.append((where, sentence))

    def in_file_order(self) -> tuple[str, ...]:
        return tuple(sentence for _, sentence in sorted(self.found, key=lambda found: found[0]))


def _first_word(text: str) -> str:
    words = text.split()
    return words[0].lower().rstrip(",.;:") if words else ""


def _miscounted(count: int, columns: int) -> str:
    """How a problem of a row with the wrong number of cells ends. A bare
    `|` splits a cell in two, so only a row with too many cells points to
    one."""
    return f", and {_ESCAPE}" if count > columns else f", so {_TOO_FEW}"


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _cell_count(count: int) -> str:
    return f"{count} cell" + ("" if count == 1 else "s")


def _first_line(shown: list[str | None], problems: _Problems) -> tuple[str, str]:
    """The version and the title, from line 1."""
    first = (shown[0] or "").rstrip() if shown else ""
    m = _TITLE.fullmatch(first)
    if m:
        return m.group(1), first[2:]
    problems.at(1, 'the first line names no version; line 1 reads "# X.Y.Z: <title>" with the release\'s own numbers')
    return "", first[2:].strip() if first.startswith("# ") else ""


def _kind(shown: list[str | None], problems: _Problems) -> str:
    """The kind, from the `- **Kind:**` line above the first section: its
    first run of letters, so a kind written as code or in bold reads as the
    word it is (contract §2)."""
    for n, line in enumerate(shown, 1):
        if line is None:
            continue
        if line.startswith("## "):
            break
        if not line.strip().startswith(_KIND):
            continue
        rest = line.strip()[len(_KIND):].strip()
        if "|" in rest:
            problems.at(n, f"the kind is still the template's; {_ONE_OF_KINDS}")
            return ""
        m = _LETTERS.search(rest)
        word = m.group(0).lower() if m else ""
        if not rest:
            problems.at(n, f"the kind is empty; {_ONE_OF_KINDS}")
        elif word not in KINDS:
            problems.at(n, f"the kind is {word or rest!r}; {_ONE_OF_KINDS}")
        return word
    problems.missing(1, f'the plan has no "{_KIND}" line above its first section; {_ONE_OF_KINDS}')
    return ""


def _header(table: Table, columns: tuple[str, ...], whose: str, problems: _Problems) -> None:
    """Hold a table's header to its columns. The header is the only place a
    plan shows their order, and a row read in another order reads wrong
    with nothing to show it: Entered and Exited are both dates."""
    order = f"the template's columns are {', '.join(columns)}, in that order"
    if len(table.header) != len(columns):
        problems.at(table.line, f"{whose} header has {_cell_count(len(table.header))}; {order}")
        return
    for position, (cell, column) in enumerate(zip(table.header, columns), 1):
        if not cell.casefold().startswith(column.casefold()):
            problems.at(table.line, f"{whose} column {position} is headed {cell!r}; {order}")
            return


def _stages(table: Table, problems: _Problems) -> tuple[StageRow, ...]:
    _header(table, STAGE_COLUMNS, "the stage table's", problems)
    rows: list[StageRow] = []
    seen: dict[int, int] = {}
    for n, cells in table.rows:
        if len(cells) != len(STAGE_COLUMNS):
            problems.at(n, f"a stage row has {_cell_count(len(cells))}; the stage table has "
                           f"{len(STAGE_COLUMNS)} columns{_miscounted(len(cells), len(STAGE_COLUMNS))}")
            continue
        m = _STAGE.fullmatch(cells[0])
        if not m:
            problems.at(n, f'a stage row starts {cells[0]!r}; its first cell is "N. Name", N from 0 to 9')
            continue
        number, entered, exited = int(m.group(1)), cells[1], cells[2]
        if number in seen:
            problems.at(n, f"stage {number} already has a row, on line {seen[number]}")
        else:
            seen[number] = n
        if entered and not is_date(entered):
            problems.at(n, f"stage {number}'s Entered is {entered!r}; an Entered cell is empty or a date, {_A_DATE}")
        if exited.startswith(SKIPPED):
            if not exited[len(SKIPPED):].strip():
                problems.at(n, f'stage {number} is skipped with no reason; Exited reads "skipped: <reason>"')
        elif exited and not is_date(exited):
            problems.at(n, f"stage {number}'s Exited is {exited!r}; an Exited cell is empty, a date ({_A_DATE}) "
                           f'or "skipped: <reason>"')
        rows.append(StageRow(number, m.group(2), entered, exited, cells[3], cells[4]))
    absent = [str(number) for number in STAGE_NUMBERS if number not in seen]
    if absent:
        problems.at(table.line, f"the stage table has no row for stage {_listed(absent)}")
    return tuple(rows)


def _ledger(text: str, shown: list[str | None], problems: _Problems) -> tuple[LedgerRow, ...]:
    tables = tables_under(text, LEDGER_HEADING)
    if not tables:
        at = _heading_line(shown, LEDGER_HEADING)
        if at is None:
            problems.missing(len(shown) + 1, f'the plan has no "## {LEDGER_HEADING}" heading, so no ledger')
        else:
            problems.at(at, f'no table under "## {LEDGER_HEADING}"')
        return ()
    table = tables[0]
    _header(table, LEDGER_COLUMNS, "the ledger's", problems)
    rows: list[LedgerRow] = []
    seen: dict[str, int] = {}
    for n, cells in table.rows:
        if cells[0] == "L1" and not any(cells[1:]):
            continue
        if len(cells) != len(LEDGER_COLUMNS):
            problems.at(n, f"a ledger row has {_cell_count(len(cells))}; the ledger has "
                           f"{len(LEDGER_COLUMNS)} columns{_miscounted(len(cells), len(LEDGER_COLUMNS))}")
            continue
        found_id, found, severity, reproduced, test, state_cell, fixed_in = cells
        whose = "this row's"
        if not _ID.fullmatch(found_id):
            problems.at(n, f"the ledger ID is {found_id!r}; an ID is L and digits, as in L1")
        else:
            whose = f"{found_id}'s"
            if found_id in seen:
                problems.at(n, f"the ledger ID {found_id} is already used, on line {seen[found_id]}")
            else:
                seen[found_id] = n
        if severity not in SEVERITIES:
            problems.at(n, f"{whose} severity is {severity!r}; a severity is S1, S2, S3 or S4")
        state = _first_word(state_cell)
        if state not in STATES:
            problems.at(n, f"{whose} state is {state_cell!r}; the State cell starts with open, fixed, "
                           f"carried or closed")
        rows.append(LedgerRow(found_id, found, severity, reproduced, test, state, fixed_in))
    run_on = _line_run_on(shown, table)
    if run_on is not None:
        problems.at(run_on, "the line right under the ledger starts without a |; a row starts with |, "
                            "and anything else takes a blank line first")
    for extra in tables[1:]:
        problems.at(extra.line, f'a second table under "## {LEDGER_HEADING}"; the ledger is one table, '
                                f"and a blank line inside it ends it")
    return tuple(rows)


def _flag_gaps(text: str, problems: _Problems) -> tuple[FlagGap, ...]:
    wanted = tuple(column.casefold() for column in FLAG_GAP_COLUMNS)
    gaps: list[FlagGap] = []
    for table in tables_under(text, BUDGETS_HEADING):
        if tuple(cell.casefold() for cell in table.header) != wanted:
            continue
        for n, cells in table.rows:
            if len(cells) != len(FLAG_GAP_COLUMNS):
                problems.at(n, f"a flag-gap row has {_cell_count(len(cells))}; the table has "
                               f"{len(FLAG_GAP_COLUMNS)} columns{_miscounted(len(cells), len(FLAG_GAP_COLUMNS))}")
                continue
            # A flag is written as code on the page; the runner's label has
            # no backtick in it.
            flag = cells[0].replace("`", "").strip()
            reason, answer, date = cells[1:]
            if not flag:
                problems.at(n, "a flag-gap row names no flag")
            if date and not is_date(date):
                problems.at(n, f"the flag gap for {flag or 'this row'} is dated {date!r}; a date is {_A_DATE}")
            gaps.append(FlagGap(flag, reason, answer, date))
    return tuple(gaps)


def _second_headings(shown: list[str | None], problems: _Problems) -> None:
    """A gated section written twice. Every reader takes the first heading
    of a name, so the tables under a second one are never read: an open S1
    in a second ledger, or a fenced copy of the template's ledger above the
    real one, would otherwise merge with no word."""
    for heading in GATED_HEADINGS:
        lines = _heading_lines(shown, heading)
        for n in lines[1:]:
            problems.at(n, f'a second "## {heading}" heading, after the one on line {lines[0]}; '
                           f"only the first is read, so nothing under this one is")


def read_plan(path: Path) -> Plan:
    """Read the plan at `path`.

    Raises `PlanError` when there is no file there, when it cannot be read,
    or when it has no table under `## 2. Stages`. Anything else that is
    wrong is a sentence in the result's `problems`, and the rest of the plan
    is still read.
    """
    path = Path(path)
    if not path.is_file():
        raise PlanError(f"{path}: no plan file there")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise PlanError(f"{path}: the plan cannot be read ({error})") from error

    shown, unclosed = _shown(text)
    stage_tables = tables_under(text, STAGES_HEADING)
    if not stage_tables:
        later = _heading_lines(shown, STAGES_HEADING)[1:]
        if unclosed:
            why = f" (an HTML comment opens on line {unclosed} and never closes)"
        elif later:
            why = f" (the first has none, and the one on line {later[0]} is not read)"
        else:
            why = ""
        raise PlanError(f'{path}: no table under a "## {STAGES_HEADING}" heading{why}')

    problems = _Problems()
    version, title = _first_line(shown, problems)
    kind = _kind(shown, problems)
    _second_headings(shown, problems)
    stages = _stages(stage_tables[0], problems)
    flag_gaps = _flag_gaps(text, problems)
    ledger = _ledger(text, shown, problems)
    if unclosed:
        problems.at(unclosed, "an HTML comment opens here and never closes, so nothing after it is read")
    return Plan(path, version, title, kind, stages, ledger, flag_gaps, problems.in_file_order())


def stage(plan: Plan, number: int) -> StageRow | None:
    """The row of stage `number`, or None when the plan's table has none."""
    return next((row for row in plan.stages if row.number == number), None)
