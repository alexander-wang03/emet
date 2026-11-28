"""The plan parser, `tools/plan_file.py`, against small plans written here.

Most tests take the good plan below, change one thing in it, write it to a
temporary folder and read it back. A problem is checked by the line it
names, found in the changed text, so no test counts lines by hand.

The last tests read the real 0.5.6 plan and the real template. Both live
beside the repository, in `Project Emet/`, and CI has neither, so they skip
there.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import plan_file  # noqa: E402  a tool, imported by its bare name from tools/
from plan_file import FlagGap, LedgerRow, PlanError, StageRow, Table  # noqa: E402  a tool, imported by its bare name from tools/

#: `Project Emet/`, the folder the repository sits in.
PROJECT = Path(__file__).resolve().parents[3]
REAL_PLAN = PROJECT / "releases" / "v0.5.6" / "plan.md"
REAL_TEMPLATE = PROJECT / "planning" / "templates" / "plan.md"

STAGE_HEADER = "| Stage | Entered | Exited | Exit criterion | Evidence (path under releases/v0.9.1/) |"
BUILD = "| 2. Build | 2026-11-03 | | Green on the laptop | |"
TUNE = "| 5. Tune | | skipped: no default changes | Only when a default changes | |"

LEDGER_HEADER = ("| ID | Found (stage, date, by) | Severity | Reproduced how "
                 "| Regression test, or the reason there is none | State | Fixed in |")
L1 = "| L1 | Stage 3, 2026-11-04, review | S2 | Ran it twice | `test_count.py::test_ten` | fixed | 0.9.1 |"
L2 = "| L2 | Stage 3, 2026-11-04, review | S1 | By hand | none: a wording fix | Open, waiting on a word | |"
L3 = "| L3 | Stage 4, 2026-11-05, the body | S3 | On the Pi | none yet | carried to 0.9.2 | |"
L4 = "| L4 | Stage 4, 2026-11-05, the body | S4 | Could not | none | closed: a duplicate of L3 | |"
LEDGER_ROWS = "\n".join((L1, L2, L3, L4)) + "\n"

GAP_HEADER = "| Flag not run | Reason | Answer | Date |"
GAP_ECHO = '| `--echo` | The speaker was away | "Fine, next release." | 2026-11-20 |'
GAP_TALK = "| emet-talk with no flags | Nobody home | | |"
GAP_TABLE = "\n".join((GAP_HEADER, "|---|---|---|---|", GAP_ECHO, GAP_TALK)) + "\n"

GOOD = f"""\
# 0.9.1: Teach it to count

- **Kind:** patch, one batch (E1).
- **Title:** `0.9.1: Teach it to count`
- **Sitting:** yes | no sitting: no behaviour change

## 0. Scope

| Task (roadmap wording, id) | Status | File | Test | Caller | Batch |
|---|---|---|---|---|---|
| Count to ten (T1) | keep | `count.py` | `test_count.py` | the engine | A |

**The batch plan.**

| Batch | Tasks (ids) | Estimate (h) | Actual (h) | Closed |
|---|---|---|---|---|
| A | T1 | 2 | | |

## 1. Decisions

None bear on this release.

## 2. Stages

<!-- A comment above the table,
     on two lines. -->

{STAGE_HEADER}
|---|---|---|---|---|
| 0. Scope and research | 2026-11-01 | 2026-11-02 | A line for every check | `brief.md` |
| 1. Contract and freeze | 2026-11-02 | 2026-11-03 | Frozen, with a date | `contract.md` |
{BUILD}
| 3. Review and fix | | | No open S1 | |
| 4. Body evidence | | | A passing run | |
{TUNE}
| 6. API and UX gate | | | The console sheet complete | |
| 7. Candidate and sitting | | | The evidence check | |
| 8. Ship | | | The audit passes | |
| 9. Close | | | The next brief exists | |

**Next: Stage 2.**

## 3. Budgets and measurements

| Measure | Last release | This release | Change | Bound | File |
|---|---|---|---|---|---|
| Boot to first frame | | | | | |

**Flags not run on the body in this release:**

{GAP_TABLE}
## 4. Ledger

{LEDGER_HEADER}
|---|---|---|---|---|---|---|
{LEDGER_ROWS}
## 5. Register rows touched

None yet.
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "plan.md"
    path.write_text(text, encoding="utf-8")
    return path


def read(tmp_path: Path, text: str) -> plan_file.Plan:
    return plan_file.read_plan(write(tmp_path, text))


def changed(old: str, new: str) -> str:
    """The good plan with one piece of it replaced, which must be there."""
    assert GOOD.count(old) == 1, f"{old!r} is in the good plan {GOOD.count(old)} times"
    return GOOD.replace(old, new)


def line_of(text: str, needle: str) -> int:
    """The number of the first line of `text` that holds `needle`."""
    for n, line in enumerate(text.split("\n"), 1):
        if needle in line:
            return n
    raise AssertionError(f"no line holds {needle!r}")


def lines_named(problems: tuple[str, ...]) -> list[int]:
    return [int(m.group(1)) for m in (re.match(r"line (\d+): ", p) for p in problems) if m]


def the_one(problems: tuple[str, ...], line: int, says: str) -> None:
    """Exactly one problem, naming this line and saying this."""
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"line {line}: "), problems[0]
    assert says in problems[0], problems[0]


# ------------------------------------------------------------- a good plan


def test_a_good_plan_reads_with_no_problem(tmp_path):
    path = write(tmp_path, GOOD)
    plan = plan_file.read_plan(path)
    assert plan.problems == ()
    assert plan.path == path
    assert plan.version == "0.9.1"
    assert plan.title == "0.9.1: Teach it to count"
    assert plan.kind == "patch"
    assert [row.number for row in plan.stages] == list(range(10))
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L3", "L4"]
    assert len(plan.flag_gaps) == 2


def test_a_path_given_as_a_string_is_read_too(tmp_path):
    plan = plan_file.read_plan(str(write(tmp_path, GOOD)))
    assert plan.path == tmp_path / "plan.md"
    assert plan.problems == ()


def test_a_stage_row_holds_its_five_cells(tmp_path):
    plan = read(tmp_path, GOOD)
    assert plan_file.stage(plan, 0) == StageRow(
        0, "Scope and research", "2026-11-01", "2026-11-02", "A line for every check", "`brief.md`")
    assert plan_file.stage(plan, 2) == StageRow(2, "Build", "2026-11-03", "", "Green on the laptop", "")


def test_a_stage_is_done_when_it_exited_on_a_day_or_was_skipped(tmp_path):
    plan = read(tmp_path, GOOD)
    assert plan_file.stage(plan, 0).done
    assert not plan_file.stage(plan, 2).done, "entered, and still open"
    assert not plan_file.stage(plan, 3).done
    assert [row.number for row in plan.stages if row.done] == [0, 1, 5]


def test_a_skipped_stage_keeps_its_reason_and_is_done(tmp_path):
    tune = plan_file.stage(read(tmp_path, GOOD), 5)
    assert tune.entered == ""
    assert tune.exited == "skipped: no default changes"
    assert tune.done


def test_stage_gives_none_for_a_number_the_table_lacks(tmp_path):
    plan = read(tmp_path, GOOD)
    assert plan_file.stage(plan, 10) is None
    assert plan_file.stage(plan, -1) is None


def test_a_ledger_row_holds_its_seven_cells(tmp_path):
    plan = read(tmp_path, GOOD)
    assert plan.ledger[0] == LedgerRow(
        "L1", "Stage 3, 2026-11-04, review", "S2", "Ran it twice", "`test_count.py::test_ten`", "fixed", "0.9.1")
    assert plan.ledger[2].fixed_in == ""


def test_the_state_is_the_cells_first_word_in_lower_case(tmp_path):
    plan = read(tmp_path, GOOD)
    # "fixed", "Open, waiting on a word", "carried to 0.9.2", "closed: a duplicate of L3"
    assert [row.state for row in plan.ledger] == ["fixed", "open", "carried", "closed"]


def test_an_open_or_a_carried_finding_is_unresolved(tmp_path):
    plan = read(tmp_path, GOOD)
    assert [row.id for row in plan.ledger if row.unresolved] == ["L2", "L3"]
    assert not plan.ledger[0].unresolved, "fixed"
    assert not plan.ledger[3].unresolved, "closed"


def test_the_rows_and_the_plan_are_frozen(tmp_path):
    plan = read(tmp_path, GOOD)
    for thing, field in ((plan, "kind"), (plan.stages[0], "exited"), (plan.ledger[0], "state"),
                         (plan.flag_gaps[0], "answer")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(thing, field, "changed")


def test_windows_line_endings_and_a_byte_order_mark_change_nothing(tmp_path):
    plain = read(tmp_path, GOOD)
    path = tmp_path / "crlf.md"
    path.write_bytes(b"\xef\xbb\xbf" + GOOD.replace("\n", "\r\n").encode("utf-8"))
    crlf = plan_file.read_plan(path)
    assert crlf.problems == ()
    assert (crlf.version, crlf.title, crlf.kind) == (plain.version, plain.title, plain.kind)
    assert (crlf.stages, crlf.ledger, crlf.flag_gaps) == (plain.stages, plain.ledger, plain.flag_gaps)


# ------------------------------------------------------- line 1 and the kind


@pytest.mark.parametrize("first", [
    "# X.Y.Z: <title>",          # the template's
    "# 0.9: Teach it to count",  # two numbers
    "# Teach it to count",
    "0.9.1: Teach it to count",  # no "# "
    "# 0.9.1:",
    "",
])
def test_a_first_line_with_no_version_is_a_problem(tmp_path, first):
    plan = read(tmp_path, changed("# 0.9.1: Teach it to count", first))
    the_one(plan.problems, 1, "names no version")
    assert plan.version == ""


def test_the_template_title_is_kept_when_it_names_no_version(tmp_path):
    plan = read(tmp_path, changed("# 0.9.1: Teach it to count", "# X.Y.Z: <title>"))
    assert plan.title == "X.Y.Z: <title>"


@pytest.mark.parametrize("line, kind", [
    ("- **Kind:** patch", "patch"),
    ("- **Kind:** patch, three batches (E1 (e)).", "patch"),
    ("- **Kind:** Minor", "minor"),
    ("- **Kind:** major.", "major"),
    ("- **Kind:**   hotfix: the wheel would not install", "hotfix"),
    # Contract §2: the first run of letters, so markup around the word is
    # no part of it.
    ("- **Kind:** `patch`", "patch"),
    ("- **Kind:** (patch)", "patch"),
    ("- **Kind:** **minor**", "minor"),
    ("- **Kind:** _major_", "major"),
    ("- **Kind:** patch/one batch", "patch"),
    ("- **Kind:** patch(E1)", "patch"),
])
def test_the_kind_is_the_first_run_of_letters_after_its_label(tmp_path, line, kind):
    plan = read(tmp_path, changed("- **Kind:** patch, one batch (E1).", line))
    assert plan.problems == ()
    assert plan.kind == kind


def test_the_templates_kind_line_is_a_problem(tmp_path):
    text = changed("- **Kind:** patch, one batch (E1).", "- **Kind:** patch | minor | major | hotfix")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "**Kind:**"), "the kind is still the template's")
    assert plan.kind == "", "its first word is patch, and nobody chose it"


@pytest.mark.parametrize("line, says, kind", [
    ("- **Kind:** feature", "the kind is 'feature'; a kind is patch, minor, major or hotfix", "feature"),
    ("- **Kind:** patchy", "the kind is 'patchy'", "patchy"),
    ("- **Kind:** `patchy`", "the kind is 'patchy'", "patchy"),
    ("- **Kind:** 1.0", "the kind is '1.0'", ""),
    ("- **Kind:**", "the kind is empty", ""),
])
def test_a_kind_outside_the_four_is_a_problem(tmp_path, line, says, kind):
    text = changed("- **Kind:** patch, one batch (E1).", line)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "**Kind:**"), says)
    assert plan.kind == kind


def test_a_plan_with_no_kind_line_says_so(tmp_path):
    plan = read(tmp_path, changed("- **Kind:** patch, one batch (E1).\n", ""))
    assert len(plan.problems) == 1
    assert 'no "- **Kind:**" line' in plan.problems[0]
    assert plan.kind == ""


def test_a_kind_line_below_the_first_section_is_not_the_kind(tmp_path):
    text = changed("- **Kind:** patch, one batch (E1).\n", "").replace(
        "None bear on this release.", "- **Kind:** major")
    plan = read(tmp_path, text)
    assert plan.kind == ""
    assert len(plan.problems) == 1


# ---------------------------------------------------------- the stage table


@pytest.mark.parametrize("row, says", [
    ("| 2. Build | 2026-11-03 | | Green on the laptop |", "a stage row has 4 cells"),
    ("| 2. Build | 2026-11-03 | | Green | on the laptop | |", "a stage row has 6 cells"),
    ("| 2. Build |", "a stage row has 1 cell;"),
    ("| Build | 2026-11-03 | | Green on the laptop | |", "a stage row starts 'Build'"),
    ("| 2 Build | 2026-11-03 | | Green on the laptop | |", "a stage row starts '2 Build'"),
    ("| 2.Build | 2026-11-03 | | Green on the laptop | |", "a stage row starts '2.Build'"),
    ("| 2. | 2026-11-03 | | Green on the laptop | |", "a stage row starts '2.'"),
    ("| 12. Build | 2026-11-03 | | Green on the laptop | |", "a stage row starts '12. Build'"),
    ("| | | | | |", "a stage row starts ''"),
])
def test_a_stage_row_of_another_shape_is_a_problem_and_is_left_out(tmp_path, row, says):
    text = changed(BUILD, row)
    plan = read(tmp_path, text)
    header, at = line_of(text, "| Stage |"), line_of(text, row)
    assert len(plan.problems) == 2, plan.problems
    assert plan.problems[0] == f"line {header}: the stage table has no row for stage 2"
    assert plan.problems[1].startswith(f"line {at}: {says}"), plan.problems[1]
    assert plan_file.stage(plan, 2) is None
    assert len(plan.stages) == 9


@pytest.mark.parametrize("row, says", [
    ("| 2. Build | soon | | Green on the laptop | |", "stage 2's Entered is 'soon'"),
    ("| 2. Build | 2026-02-30 | | Green on the laptop | |", "stage 2's Entered is '2026-02-30'"),
    ("| 2. Build | 26-11-03 | | Green on the laptop | |", "stage 2's Entered is '26-11-03'"),
    ("| 2. Build | 2026-11-03 | 2026-02-30 | Green on the laptop | |", "stage 2's Exited is '2026-02-30'"),
    ("| 2. Build | 2026-11-03 | 2026-11-4 | Green on the laptop | |", "stage 2's Exited is '2026-11-4'"),
    ("| 2. Build | 2026-11-03 | done | Green on the laptop | |", "stage 2's Exited is 'done'"),
    ("| 2. Build | 2026-11-03 | 2026-11-04, late | Green on the laptop | |", "stage 2's Exited is"),
    ("| 2. Build | 2026-11-03 | Skipped: no need | Green on the laptop | |", "stage 2's Exited is"),
    ("| 2. Build | 2026-11-03 | skipped | Green on the laptop | |", "stage 2's Exited is 'skipped'"),
])
def test_a_stage_cell_that_breaks_its_rule_is_a_problem_and_the_row_is_kept(tmp_path, row, says):
    text = changed(BUILD, row)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, row), says)
    build = plan_file.stage(plan, 2)
    assert build is not None and build.name == "Build"
    assert not build.done, "what is in Exited is neither a day nor a skip"


def test_an_impossible_day_is_no_date(tmp_path):
    text = changed("| 0. Scope and research | 2026-11-01 | 2026-11-02 |",
                   "| 0. Scope and research | 2026-11-01 | 2026-02-30 |")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "| 0. Scope and research"), "stage 0's Exited is '2026-02-30'")
    assert plan_file.stage(plan, 0).exited == "2026-02-30", "kept as written"
    assert not plan_file.stage(plan, 0).done


@pytest.mark.parametrize("text, real", [
    ("2026-11-26", True),
    ("2024-02-29", True),     # a leap year
    ("2026-02-29", False),
    ("2026-02-30", False),
    ("2026-04-31", False),
    ("2026-13-01", False),
    ("2026-00-10", False),
    ("0000-01-01", False),
    ("2026-11-26 ", False),
    ("2026-11-26\n", False),
    ("2026-1-26", False),
    ("20261126", False),
    ("26-11-2026", False),
    ("2026/11/26", False),
    ("", False),
])
def test_a_date_is_ten_characters_and_a_real_day(text, real):
    assert plan_file.is_date(text) is real


def test_a_skip_with_no_reason_is_a_problem_and_still_done(tmp_path):
    row = "| 5. Tune | | skipped: | Only when a default changes | |"
    text = changed(TUNE, row)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, row), "stage 5 is skipped with no reason")
    assert plan_file.stage(plan, 5).done


def test_a_stage_numbered_twice_is_a_problem_naming_both_lines(tmp_path):
    text = changed("| 3. Review and fix |", "| 2. Review and fix |")
    plan = read(tmp_path, text)
    header, first, second = line_of(text, "| Stage |"), line_of(text, BUILD), line_of(text, "| 2. Review and fix")
    assert plan.problems == (
        f"line {header}: the stage table has no row for stage 3",
        f"line {second}: stage 2 already has a row, on line {first}",
    )
    assert plan_file.stage(plan, 2).name == "Build", "the first row is the stage's"
    assert len(plan.stages) == 10, "both rows are kept as written"


def test_a_stage_table_missing_rows_names_them_in_one_sentence(tmp_path):
    text = changed("| 8. Ship | | | The audit passes | |\n| 9. Close | | | The next brief exists | |\n", "")
    text = text.replace("| 4. Body evidence | | | A passing run | |\n", "")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "| Stage |"), "the stage table has no row for stage 4, 8 and 9")
    assert plan_file.stage(plan, 8) is None


def test_a_stage_table_with_a_header_and_no_rows_is_a_problem_not_an_error(tmp_path):
    start, end = GOOD.index("| 0. Scope and research"), GOOD.index("**Next: Stage 2.**")
    text = GOOD[:start] + "\n" + GOOD[end:]
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "| Stage |"),
            "the stage table has no row for stage 0, 1, 2, 3, 4, 5, 6, 7, 8 and 9")
    assert plan.stages == ()
    assert len(plan.ledger) == 4, "the rest of the plan is still read"


def test_a_stage_header_in_another_order_is_a_problem(tmp_path):
    text = changed("| Stage | Entered | Exited |", "| Stage | Exited | Entered |")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "| Stage |"), "the stage table's column 2 is headed 'Exited'")
    assert "Stage, Entered, Exited, Exit criterion, Evidence, in that order" in plan.problems[0]


def test_a_stage_header_with_a_column_missing_is_a_problem(tmp_path):
    text = changed(STAGE_HEADER, "| Stage | Entered | Exited | Exit criterion |")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "| Stage |"), "the stage table's header has 4 cells")


# --------------------------------------------------------- an escaped pipe


def test_an_escaped_pipe_stays_inside_its_cell(tmp_path):
    row = "| 2. Build | 2026-11-03 | | Green on the laptop \\| the Pi | `a\\|b` |"
    plan = read(tmp_path, changed(BUILD, row))
    assert plan.problems == ()
    build = plan_file.stage(plan, 2)
    assert build.criterion == "Green on the laptop | the Pi"
    assert build.evidence == "`a|b`"


def test_a_bare_pipe_in_a_cell_splits_it_and_the_problem_says_how_to_write_one(tmp_path):
    row = "| 2. Build | 2026-11-03 | | Green on the laptop | the Pi | |"
    text = changed(BUILD, row)
    plan = read(tmp_path, text)
    assert plan.problems[1].startswith(f"line {line_of(text, row)}: a stage row has 6 cells")
    assert "a | inside a cell takes a backslash before it" in plan.problems[1]


@pytest.mark.parametrize("old, row, says", [
    (BUILD, "| 2. Build | 2026-11-03 | | Green on the laptop |",
     "a stage row has 4 cells; the stage table has 5 columns"),
    (L3, "| L3 | Stage 4, 2026-11-05, the body | S3 | On the Pi | none yet | carried to 0.9.2 |",
     "a ledger row has 6 cells; the ledger has 7 columns"),
    (GAP_ECHO, "| `--echo` | The speaker was away | 2026-11-20 |",
     "a flag-gap row has 3 cells; the table has 4 columns"),
])
def test_a_row_short_of_a_cell_is_told_a_cell_is_missing_and_not_to_escape_a_pipe(tmp_path, old, row, says):
    # A bare | only ever adds a cell; a short row lost one, or a backslash
    # before a | joined two.
    text = changed(old, row)
    told = [p for p in read(tmp_path, text).problems if p.startswith(f"line {line_of(text, row)}: ")]
    assert told == [f"line {line_of(text, row)}: {says}, so a cell is missing, or a backslash before a | "
                    "joined two cells"]


def test_an_escaped_pipe_at_the_end_of_a_row_is_its_last_cell():
    text = "## 0. Scope\n\n| a | b |\n|---|---|\n| one | two \\|\n"
    assert plan_file.table_under(text, "0. Scope") == [(5, ["one", "two |"])]


# ---------------------------------------------------------------- the ledger


def test_the_templates_placeholder_row_is_skipped(tmp_path):
    plan = read(tmp_path, changed(LEDGER_ROWS, "| L1 | | | | | | |\n"))
    assert plan.problems == ()
    assert plan.ledger == ()


def test_the_placeholder_is_skipped_beside_real_rows(tmp_path):
    plan = read(tmp_path, changed(LEDGER_ROWS, "| L1 | | | | | | |\n" + LEDGER_ROWS))
    assert plan.problems == ()
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L3", "L4"]


def test_an_l1_row_with_anything_written_is_a_finding(tmp_path):
    row = "| L1 | Stage 2, 2026-11-03, the build | | | | | |"
    text = changed(LEDGER_ROWS, row + "\n")
    plan = read(tmp_path, text)
    at = line_of(text, row)
    assert len(plan.problems) == 2, plan.problems
    assert plan.problems[0].startswith(f"line {at}: L1's severity is ''")
    assert plan.problems[1].startswith(f"line {at}: L1's state is ''")
    assert [row.id for row in plan.ledger] == ["L1"]


@pytest.mark.parametrize("written", range(6))
def test_an_l1_row_with_any_one_cell_written_is_kept(tmp_path, written):
    # Contract §2: the placeholder is L1 with every other cell empty, so
    # any one cell written, Found or not, makes the row a finding.
    cells = ["Stage 2, 2026-11-03, the build", "S1", "By hand", "none", "open", "0.9.1"]
    row = "| L1 | " + " | ".join(cell if i == written else "" for i, cell in enumerate(cells)) + " |"
    plan = read(tmp_path, changed(LEDGER_ROWS, row + "\n"))
    assert [found.id for found in plan.ledger] == ["L1"], plan.problems


def test_an_open_s1_with_an_empty_found_cell_stays_in_the_ledger(tmp_path):
    plan = read(tmp_path, changed(LEDGER_ROWS, "| L1 | | S1 | By hand | none | open | |\n"))
    assert plan.problems == ()
    assert [(row.id, row.severity, row.unresolved) for row in plan.ledger] == [("L1", "S1", True)]


def test_a_ledger_id_used_twice_is_a_problem_naming_both_lines(tmp_path):
    text = changed("| L3 | Stage 4", "| L2 | Stage 4")
    plan = read(tmp_path, text)
    first, second = line_of(text, "| L2 | Stage 3"), line_of(text, "| L2 | Stage 4")
    assert plan.problems == (f"line {second}: the ledger ID L2 is already used, on line {first}",)
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L2", "L4"], "both rows are kept as written"


@pytest.mark.parametrize("found_id", ["3", "L", "l3", "L3a", "L-3", "F3", ""])
def test_a_ledger_id_that_is_not_l_and_digits_is_a_problem(tmp_path, found_id):
    text = changed("| L3 | Stage 4", f"| {found_id} | Stage 4")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "On the Pi"), f"the ledger ID is {found_id!r}")
    assert plan.ledger[2].id == found_id
    assert plan.ledger[2].unresolved, "the row is kept, and it is still carried"


@pytest.mark.parametrize("severity", ["S5", "S0", "s3", "3", "high", "S3, was S2", ""])
def test_a_severity_outside_s1_to_s4_is_a_problem(tmp_path, severity):
    text = changed("the body | S3 | On the Pi", f"the body | {severity} | On the Pi")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "On the Pi"), f"L3's severity is {severity!r}")
    assert plan.ledger[2].severity == severity


@pytest.mark.parametrize("cell, state", [
    ("done", "done"),
    ("**open**", "**open**"),
    ("reopened", "reopened"),
    ("", ""),
])
def test_a_state_outside_the_four_is_a_problem_and_is_not_unresolved(tmp_path, cell, state):
    text = changed("| carried to 0.9.2 |", f"| {cell} |")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "On the Pi"), f"L3's state is {cell!r}")
    assert plan.ledger[2].state == state
    assert not plan.ledger[2].unresolved


@pytest.mark.parametrize("row, cells", [
    ("| L3 | Stage 4, 2026-11-05, the body | S3 | On the Pi | none yet | carried to 0.9.2 |", 6),
    ("| L3 | Stage 4, 2026-11-05, the body | S3 | On the Pi | a | b | carried to 0.9.2 | |", 8),
])
def test_a_ledger_row_with_another_count_of_cells_is_a_problem_and_is_left_out(tmp_path, row, cells):
    text = changed(L3, row)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, row), f"a ledger row has {cells} cells; the ledger has 7 columns")
    assert [found.id for found in plan.ledger] == ["L1", "L2", "L4"]


def test_a_ledger_header_in_another_order_is_a_problem(tmp_path):
    text = changed("| State | Fixed in |", "| Fixed in | State |")
    plan = read(tmp_path, text)
    assert plan.problems[0].startswith(
        f"line {line_of(text, '| ID |')}: the ledger's column 6 is headed 'Fixed in'")


def test_a_plan_with_no_ledger_heading_says_so_last(tmp_path):
    start, end = GOOD.index("## 4. Ledger"), GOOD.index("## 5. Register rows touched")
    text = (GOOD[:start] + GOOD[end:]).replace(BUILD, "| 2. Build | soon | | Green on the laptop | |")
    plan = read(tmp_path, text)
    assert len(plan.problems) == 2, plan.problems
    assert plan.problems[0].startswith(f"line {line_of(text, '| 2. Build')}: ")
    assert plan.problems[1] == 'the plan has no "## 4. Ledger" heading, so no ledger'
    assert plan.ledger == ()


def test_a_ledger_heading_with_no_table_is_a_problem(tmp_path):
    start, end = GOOD.index(LEDGER_HEADER), GOOD.index("## 5. Register rows touched")
    text = GOOD[:start] + "Nothing found.\n\n" + GOOD[end:]
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "## 4. Ledger"), 'no table under "## 4. Ledger"')
    assert plan.ledger == ()


def test_a_blank_line_inside_the_ledger_is_a_problem_since_it_hides_the_rows_below(tmp_path):
    text = changed(L2 + "\n" + L3, L2 + "\n\n" + L3)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "| L3 |"), 'a second table under "## 4. Ledger"')
    assert [row.id for row in plan.ledger] == ["L1", "L2"], "the first table is the ledger"


def test_a_last_ledger_row_without_its_first_pipe_is_a_problem_not_a_finding_lost(tmp_path):
    bare = L4[2:]
    text = changed(L4, bare)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, bare), "the line right under the ledger starts without a |")
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L3"]


def test_a_row_without_its_first_pipe_inside_the_ledger_is_two_problems(tmp_path):
    bare = L2[2:]
    text = changed(L2, bare)
    plan = read(tmp_path, text)
    assert lines_named(plan.problems) == [line_of(text, bare), line_of(text, "| L3 |")]
    assert "starts without a |" in plan.problems[0] and "a second table" in plan.problems[1]
    assert [row.id for row in plan.ledger] == ["L1"]


def test_a_heading_or_a_comment_right_under_the_ledger_is_no_problem(tmp_path):
    heading = read(tmp_path, changed(L4 + "\n\n## 5.", L4 + "\n## 5."))
    assert heading.problems == ()
    comment = read(tmp_path, changed(L4 + "\n", L4 + "\n<!-- the last row so far -->\n"))
    assert comment.problems == ()
    assert len(heading.ledger) == len(comment.ledger) == 4


# ------------------------------------------- a gated heading written twice

L9 = "| L9 | Stage 4, 2026-11-06, the body | S1 | On the Pi | none yet | open | |"
SECOND_LEDGER = f"## 4. Ledger, after the freeze\n\n{LEDGER_HEADER}\n|---|---|---|---|---|---|---|\n{L9}\n\n"


def second_heading(text: str, heading: str) -> str:
    """The problem a heading written twice gives, at the second."""
    lines = [line_of(text, f"## {heading}")]
    lines.append(next(n for n, line in enumerate(text.split("\n"), 1)
                      if n > lines[0] and line.startswith(f"## {heading}")))
    return (f'line {lines[1]}: a second "## {heading}" heading, after the one on line {lines[0]}; '
            "only the first is read, so nothing under this one is")


def test_a_second_ledger_heading_below_the_ledger_is_a_problem_since_its_rows_go_unread(tmp_path):
    text = changed("## 5. Register rows touched", SECOND_LEDGER + "## 5. Register rows touched")
    plan = read(tmp_path, text)
    assert plan.problems == (second_heading(text, "4. Ledger"),)
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L3", "L4"], "L9 is not read, and the plan is refused"


def test_a_fenced_copy_of_the_templates_ledger_above_the_real_one_is_a_problem(tmp_path):
    # The parser knows no code fence, so the quoted heading is the first,
    # its placeholder row is skipped, and the real ledger is the second.
    # The reviewer's shape: a blank line closes the quoted table before the
    # fence does, so nothing else in it reads as wrong.
    fenced = (f"```markdown\n## 4. Ledger\n\n{LEDGER_HEADER}\n|---|---|---|---|---|---|---|\n"
              "| L1 | | | | | | |\n\n```\n\n")
    text = changed("## 4. Ledger\n", fenced + "## 4. Ledger\n")
    plan = read(tmp_path, text)
    assert plan.problems == (second_heading(text, "4. Ledger"),)
    assert plan.ledger == ()


def test_a_draft_stage_table_above_the_real_one_is_a_problem(tmp_path):
    draft = "## 2. Stages (draft)\n\n" + STAGE_HEADER + "\n|---|---|---|---|---|\n" + "".join(
        f"| {n}. Stage | 2026-11-01 | 2026-11-02 | | |\n" for n in range(10)) + "\n"
    text = changed("## 2. Stages\n", draft + "## 2. Stages\n")
    plan = read(tmp_path, text)
    assert plan.problems == (second_heading(text, "2. Stages"),)


def test_a_second_budgets_heading_is_a_problem(tmp_path):
    text = changed("## 4. Ledger", "## 3. Budgets and measurements, continued\n\nNone.\n\n## 4. Ledger")
    plan = read(tmp_path, text)
    assert plan.problems == (second_heading(text, "3. Budgets and measurements"),)


def test_a_stages_heading_with_no_table_above_the_real_one_raises_and_names_the_second(tmp_path):
    text = changed("## 2. Stages\n", "## 2. Stages, read first\n\nNothing here.\n\n## 2. Stages\n")
    later = next(n for n, line in enumerate(text.split("\n"), 1) if line == "## 2. Stages")
    with pytest.raises(PlanError, match=re.escape(f"(the first has none, and the one on line {later} is not read)")):
        read(tmp_path, text)


# ------------------------------------------------------------ the flag gaps


def test_the_flag_gaps_are_read_with_backticks_removed(tmp_path):
    plan = read(tmp_path, GOOD)
    assert plan.flag_gaps == (
        FlagGap("--echo", "The speaker was away", '"Fine, next release."', "2026-11-20"),
        FlagGap("emet-talk with no flags", "Nobody home", "", ""),
    )


def test_a_gap_with_no_answer_yet_is_no_problem(tmp_path):
    plan = read(tmp_path, GOOD)
    assert plan.problems == ()
    assert (plan.flag_gaps[1].answer, plan.flag_gaps[1].date) == ("", "")


def test_a_plan_with_no_flag_gap_table_has_none(tmp_path):
    plan = read(tmp_path, changed(GAP_TABLE, ""))
    assert plan.problems == ()
    assert plan.flag_gaps == ()


def test_a_flag_gap_table_with_a_header_and_no_rows_has_none(tmp_path):
    plan = read(tmp_path, changed(GAP_TABLE, GAP_HEADER + "\n|---|---|---|---|\n"))
    assert plan.problems == ()
    assert plan.flag_gaps == ()


def test_only_the_table_with_the_flag_gap_header_is_read(tmp_path):
    # The budget table sits under the same heading, and its row is no gap.
    plan = read(tmp_path, GOOD)
    assert all(gap.flag != "Boot to first frame" for gap in plan.flag_gaps)
    renamed = read(tmp_path, changed(GAP_HEADER, "| Flag | Reason | Answer | Date |"))
    assert renamed.flag_gaps == ()
    assert renamed.problems == ()


@pytest.mark.parametrize("header", ["| Flag not run | Why | Answer | Date |", "| Flag not run | Reason | Answer |",
                                    "| Flag not run | Reason | Answer | Date | Who |"])
def test_the_flag_gap_header_is_held_whole(tmp_path, header):
    plan = read(tmp_path, changed(GAP_HEADER, header))
    assert plan.flag_gaps == ()


def test_every_table_with_the_flag_gap_header_is_read(tmp_path):
    later = GAP_HEADER + "\n|---|---|---|---|\n| --speak | The speaker was away | \"Fine.\" | 2026-11-21 |\n"
    plan = read(tmp_path, changed(GAP_TABLE, GAP_TABLE + "\nAsked again on the day:\n\n" + later))
    assert plan.problems == ()
    assert [gap.flag for gap in plan.flag_gaps] == ["--echo", "emet-talk with no flags", "--speak"]


def test_a_flag_gap_table_under_another_heading_is_not_read(tmp_path):
    text = changed(GAP_TABLE, "").replace("None yet.", GAP_TABLE)
    plan = read(tmp_path, text)
    assert plan.flag_gaps == ()
    assert plan.problems == ()


@pytest.mark.parametrize("row, says, kept", [
    ("| `--echo` | The speaker was away | 2026-11-20 |", "a flag-gap row has 3 cells; the table has 4 columns", 1),
    ("| `--echo` | The speaker | was away | yes | 2026-11-20 |", "a flag-gap row has 5 cells", 1),
    ("| `--echo` | The speaker was away | yes | 2026-02-30 |", "the flag gap for --echo is dated '2026-02-30'", 2),
    ("| `--echo` | The speaker was away | yes | last week |", "the flag gap for --echo is dated 'last week'", 2),
    ("| | The speaker was away | yes | 2026-11-20 |", "a flag-gap row names no flag", 2),
])
def test_a_malformed_flag_gap_row_is_a_problem(tmp_path, row, says, kept):
    text = changed(GAP_ECHO, row)
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, row), says)
    assert len(plan.flag_gaps) == kept


def test_an_empty_flag_gap_row_is_a_problem_too(tmp_path):
    text = changed(GAP_ECHO, "| | | | |")
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, GAP_TALK) - 1, "a flag-gap row names no flag")


# ------------------------------------------------------------ HTML comments


def test_a_commented_row_is_skipped_and_does_not_end_the_table(tmp_path):
    hidden = "<!-- | L9 | Stage 1, 2026-11-02, nobody | S1 | never | none | open | | -->"
    plan = read(tmp_path, changed(L1 + "\n", L1 + "\n" + hidden + "\n"))
    assert plan.problems == ()
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L3", "L4"]


def test_a_comment_over_several_lines_hides_the_rows_and_blank_lines_inside_it(tmp_path):
    hidden = "<!-- two rows, set aside:\n| L8 | a | S1 | b | c | open | |\n\n| L9 | a | S1 | b | c | open | |\n-->"
    plan = read(tmp_path, changed(L1 + "\n", L1 + "\n" + hidden + "\n"))
    assert plan.problems == ()
    assert [row.id for row in plan.ledger] == ["L1", "L2", "L3", "L4"]


def test_a_heading_inside_a_comment_is_no_heading(tmp_path):
    text = changed("<!-- A comment above the table,\n     on two lines. -->",
                   "<!-- Not a section:\n## 9. Elsewhere\n| not | a | row |\n-->")
    plan = read(tmp_path, text)
    assert plan.problems == ()
    assert len(plan.stages) == 10


def test_a_comment_inside_a_cell_is_taken_out_of_it(tmp_path):
    row = "| 2. Build | 2026-11-03 <!-- a day late --> | | Green on <!-- | --> the laptop | |"
    plan = read(tmp_path, changed(BUILD, row))
    assert plan.problems == ()
    assert plan_file.stage(plan, 2).entered == "2026-11-03"


def test_a_comment_that_never_closes_is_a_problem_at_the_line_it_opens(tmp_path):
    text = GOOD + "\n<!-- a note with no end\n\nmore of it\n"
    plan = read(tmp_path, text)
    the_one(plan.problems, line_of(text, "<!-- a note with no end"), "an HTML comment opens here and never closes")
    assert len(plan.ledger) == 4


def test_a_comment_that_never_closes_hides_the_ledger_below_it(tmp_path):
    text = changed("**Next: Stage 2.**", "<!-- **Next: Stage 2.**")
    plan = read(tmp_path, text)
    assert plan.problems == (
        f"line {line_of(text, '<!-- **Next')}: an HTML comment opens here and never closes, "
        "so nothing after it is read",
        'the plan has no "## 4. Ledger" heading, so no ledger',
    )
    assert len(plan.stages) == 10
    assert plan.flag_gaps == ()


# --------------------------------------------------- a plan that is no plan


def test_a_missing_file_raises(tmp_path):
    with pytest.raises(PlanError, match="no plan file there") as raised:
        plan_file.read_plan(tmp_path / "v0.9.1" / "plan.md")
    assert "plan.md" in str(raised.value)


def test_a_folder_raises(tmp_path):
    with pytest.raises(PlanError, match="no plan file there"):
        plan_file.read_plan(tmp_path)


def test_bytes_that_are_not_utf8_raise_the_plans_own_error(tmp_path):
    path = tmp_path / "plan.md"
    path.write_bytes(b"# 0.9.1: Teach it\n\n\xff\xfe\n")
    with pytest.raises(PlanError, match="cannot be read"):
        plan_file.read_plan(path)


def test_a_plan_with_no_stages_heading_raises(tmp_path):
    text = changed("## 2. Stages", "## 2. Steps")
    with pytest.raises(PlanError, match='no table under a "## 2. Stages" heading'):
        read(tmp_path, text)


def test_a_shipped_list_with_no_stage_table_raises(tmp_path):
    text = "# 0.5.1: Keep the words\n\n## Shipped list, moved from ROADMAP.md on 2026-11-25\n\n- one\n"
    with pytest.raises(PlanError):
        read(tmp_path, text)


def test_a_stages_heading_with_no_table_under_it_raises(tmp_path):
    start, end = GOOD.index(STAGE_HEADER), GOOD.index("**Next: Stage 2.**")
    with pytest.raises(PlanError, match="2. Stages"):
        read(tmp_path, GOOD[:start] + GOOD[end:])


def test_another_table_first_under_the_stages_heading_is_read_as_the_stage_table_and_fails_loudly(tmp_path):
    # The budget table now sits under "## 2. Stages", and the stage table under no heading read.
    text = changed("## 2. Stages", "## 2. Steps").replace("## 3. Budgets and measurements", "## 2. Stages")
    plan = read(tmp_path, text)
    header, row = line_of(text, "| Measure |"), line_of(text, "| Boot to first frame")
    assert plan.problems[:3] == (
        f"line {header}: the stage table's header has 6 cells; the template's columns are Stage, Entered, Exited, "
        "Exit criterion, Evidence, in that order",
        f"line {header}: the stage table has no row for stage 0, 1, 2, 3, 4, 5, 6, 7, 8 and 9",
        f"line {row}: a stage row has 6 cells; the stage table has 5 columns, and a | inside a cell takes "
        "a backslash before it",
    )
    assert plan.stages == ()


def test_a_stage_table_hidden_by_an_unclosed_comment_raises_and_says_why(tmp_path):
    text = changed("     on two lines. -->", "     on two lines.")
    opened = line_of(text, "<!-- A comment above the table")
    with pytest.raises(PlanError, match=f"an HTML comment opens on line {opened} and never closes"):
        read(tmp_path, text)


def test_a_comment_closes_at_the_next_closing_mark_however_far(tmp_path):
    # Opened in section 1, it runs to the end of the comment above the stage
    # table, and takes the "## 2. Stages" heading with it.
    text = changed("None bear on this release.", "<!-- None bear on this release.")
    with pytest.raises(PlanError, match='no table under a "## 2. Stages" heading$'):
        read(tmp_path, text)


# ------------------------------------------------------ problems, in order


def test_problems_come_in_file_order_one_sentence_each(tmp_path):
    text = GOOD
    for old, new in (
        ("# 0.9.1: Teach it to count", "# X.Y.Z: <title>"),
        ("- **Kind:** patch, one batch (E1).", "- **Kind:** patch | minor | major | hotfix"),
        (BUILD, "| 2. Build | 2026-02-30 | | Green on the laptop | |"),
        (GAP_ECHO, "| `--echo` | The speaker was away | yes | 2026-11-31 |"),
        ("| L3 | Stage 4", "| L2 | Stage 4"),
        ("| S4 | Could not", "| S9 | Could not"),
    ):
        assert old in text
        text = text.replace(old, new)
    plan = read(tmp_path, text)
    named = lines_named(plan.problems)
    assert len(plan.problems) == 6 and len(named) == 6, plan.problems
    assert named == sorted(named)
    assert named == [1, line_of(text, "**Kind:**"), line_of(text, "| 2. Build"), line_of(text, "`--echo`"),
                     line_of(text, "| L2 | Stage 4"), line_of(text, "| S9 |")]
    assert all(problem.count("\n") == 0 for problem in plan.problems)


# ------------------------------------------------- table_under, tables_under


def test_table_under_gives_the_first_tables_body_rows_with_their_lines():
    rows = plan_file.table_under(GOOD, "0. Scope")
    assert rows == [(line_of(GOOD, "Count to ten"),
                     ["Count to ten (T1)", "keep", "`count.py`", "`test_count.py`", "the engine", "A"])]


def test_table_under_stops_at_the_next_heading():
    assert plan_file.table_under(GOOD, "1. Decisions") == []
    assert len(plan_file.table_under(GOOD, "2. Stages")) == 10


def test_table_under_names_a_heading_by_how_its_text_starts():
    stages = plan_file.table_under(GOOD, "2. Stages")
    assert plan_file.table_under(GOOD, "2.") == stages
    assert plan_file.table_under(GOOD, "## 2. Stages") == stages
    assert plan_file.table_under(GOOD, "Stages") == []
    assert plan_file.table_under(GOOD, "8. Nowhere") == []


def test_table_under_skips_the_comment_above_the_table():
    rows = plan_file.table_under(GOOD, "2. Stages")
    assert rows[0] == (line_of(GOOD, "| 0. Scope and research"),
                       ["0. Scope and research", "2026-11-01", "2026-11-02", "A line for every check", "`brief.md`"])
    assert rows[2] == (line_of(GOOD, BUILD), ["2. Build", "2026-11-03", "", "Green on the laptop", ""])


def test_table_under_reads_the_first_heading_of_that_name():
    text = "## 0. Scope\n\n| a |\n|---|\n| first |\n\n## 0. Scope\n\n| a |\n|---|\n| second |\n"
    assert plan_file.table_under(text, "0. Scope") == [(5, ["first"])]


def test_a_row_may_be_indented_and_may_leave_off_its_last_pipe():
    text = "## 0. Scope\n\n  | Task | Status |\n  |---|---|\n  | one | keep\n| two | cut |\n"
    assert plan_file.table_under(text, "0. Scope") == [(5, ["one", "keep"]), (6, ["two", "cut"])]


def test_a_rule_row_with_alignment_colons_is_skipped_and_a_missing_one_costs_no_row():
    aligned = "## 0. Scope\n| Task | Hours |\n|:---|---:|\n| one | 2 |\n"
    assert plan_file.table_under(aligned, "0. Scope") == [(4, ["one", "2"])]
    bare = "## 0. Scope\n| Task | Hours |\n| one | 2 |\n"
    assert plan_file.table_under(bare, "0. Scope") == [(3, ["one", "2"])]


def test_a_dashed_row_lower_in_the_table_is_a_body_row():
    text = "## 0. Scope\n| Task | Hours |\n|---|---|\n| one | 2 |\n|---|---|\n"
    assert plan_file.table_under(text, "0. Scope") == [(4, ["one", "2"]), (5, ["---", "---"])]


def test_a_deeper_heading_does_not_end_the_section():
    text = "## 0. Scope\n\n### Batch A\n\n| Task |\n|---|\n| one |\n\n## 1. Decisions\n\n| Task |\n|---|\n| two |\n"
    assert plan_file.table_under(text, "0. Scope") == [(7, ["one"])]


def test_an_empty_row_comes_back_as_empty_cells():
    text = "## 0. Scope\n| Task | Status | File |\n|---|---|---|\n| | | |\n"
    assert plan_file.table_under(text, "0. Scope") == [(4, ["", "", ""])]


def test_tables_under_gives_every_table_under_the_heading_in_file_order():
    tables = plan_file.tables_under(GOOD, "0. Scope")
    assert len(tables) == 2
    scope, batches = tables
    assert scope.line == line_of(GOOD, "| Task (roadmap wording, id)")
    assert scope.header == ("Task (roadmap wording, id)", "Status", "File", "Test", "Caller", "Batch")
    assert scope.rows == ((line_of(GOOD, "Count to ten"),
                           ("Count to ten (T1)", "keep", "`count.py`", "`test_count.py`", "the engine", "A")),)
    assert batches == Table(
        line_of(GOOD, "| Batch | Tasks"),
        ("Batch", "Tasks (ids)", "Estimate (h)", "Actual (h)", "Closed"),
        ((line_of(GOOD, "| A | T1"), ("A", "T1", "2", "", "")),),
    )


def test_a_table_is_found_under_its_heading_by_its_header():
    tables = plan_file.tables_under(GOOD, "3. Budgets and measurements")
    assert [table.header[0] for table in tables] == ["Measure", "Flag not run"]
    gaps = next(table for table in tables if table.header == plan_file.FLAG_GAP_COLUMNS)
    assert [cells[0] for _, cells in gaps.rows] == ["`--echo`", "emet-talk with no flags"]


def test_tables_under_a_heading_with_no_table_is_an_empty_list():
    assert plan_file.tables_under(GOOD, "1. Decisions") == []
    assert plan_file.tables_under(GOOD, "5. Register rows touched") == []


def test_tables_under_a_heading_the_text_lacks_is_an_empty_list():
    assert plan_file.tables_under(GOOD, "8. Nowhere") == []
    assert plan_file.tables_under("", "0. Scope") == []


def test_a_table_with_a_header_and_no_rows_is_a_table_with_none():
    text = "## 3. Budgets and measurements\n\n| Flag not run | Reason | Answer | Date |\n|---|---|---|---|\n"
    assert plan_file.tables_under(text, "3. Budgets") == [Table(3, plan_file.FLAG_GAP_COLUMNS, ())]
    assert plan_file.table_under(text, "3. Budgets") == []


def test_a_table_is_frozen():
    table = plan_file.tables_under(GOOD, "0. Scope")[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        table.line = 1


def test_table_under_is_the_first_of_tables_under():
    for heading in ("0. Scope", "2. Stages", "3. Budgets and measurements", "4. Ledger"):
        first = plan_file.tables_under(GOOD, heading)[0]
        assert plan_file.table_under(GOOD, heading) == [(n, list(cells)) for n, cells in first.rows]


# ------------------------------------------------- the real plan and template


def test_the_real_0_5_6_plan_reads_with_no_problem():
    if not REAL_PLAN.is_file():
        pytest.skip("no releases folder beside the repository")
    plan = plan_file.read_plan(REAL_PLAN)
    assert plan.problems == ()
    assert plan.version == "0.5.6"
    assert plan.title.startswith("0.5.6: ")
    assert plan.kind == "patch"
    assert [row.number for row in plan.stages] == list(range(10))
    assert plan_file.stage(plan, 0).done
    ids = [row.id for row in plan.ledger]
    assert ids and len(set(ids)) == len(ids)
    assert all(row.severity in plan_file.SEVERITIES and row.state in plan_file.STATES for row in plan.ledger)
    # No bare pipe inside a cell of the tables check_plan.py and blocks.py read.
    scope = plan_file.tables_under(REAL_PLAN.read_text(encoding="utf-8"), "0. Scope")
    assert scope and scope[0].header[0].startswith("Task")
    assert all(len(cells) == len(table.header) for table in scope for _, cells in table.rows)


def test_the_real_template_reads_with_only_its_two_placeholders_as_problems():
    if not REAL_TEMPLATE.is_file():
        pytest.skip("no planning folder beside the repository")
    plan = plan_file.read_plan(REAL_TEMPLATE)
    assert len(plan.problems) == 2, plan.problems
    assert plan.problems[0].startswith("line 1: the first line names no version")
    assert "the kind is still the template's" in plan.problems[1]
    assert (plan.version, plan.kind) == ("", "")
    assert [row.number for row in plan.stages] == list(range(10))
    assert not any(row.done for row in plan.stages)
    assert plan.ledger == (), "the placeholder row is skipped"
    assert plan.flag_gaps == ()
    # An empty flag_gaps also comes from a template with no flag-gap table,
    # so the header itself is held to the parser's columns (ledger L19).
    budgets = plan_file.tables_under(REAL_TEMPLATE.read_text(encoding="utf-8"), plan_file.BUDGETS_HEADING)
    assert plan_file.FLAG_GAP_COLUMNS in [table.header for table in budgets], [t.header for t in budgets]
