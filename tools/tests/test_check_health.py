"""The health check, `tools/check_health.py`, against small trees written here.

Each test builds four tiny packages in a temporary folder, measures them, and
changes one thing. vulture is never installed for these: a test that needs
it writes a stand-in program that prints what vulture would and records what
it was run with, and a test that needs it absent passes no command at all.

The last tests read the real tree: the SDK is under its cap, and the budget
file holds every number with its date and its commit.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import api_diff  # noqa: E402  a tool, imported by its bare name from tools/
import check_health  # noqa: E402  a tool, imported by its bare name from tools/
from check_health import COMPLEXITY_OVER, MEASURES, PACKAGES, SDK_LINE_CAP  # noqa: E402  a tool, imported by its bare name from tools/

DAY_ONE = ("2026-01-01", "aaaaaaa")
DAY_TWO = ("2026-02-02", "bbbbbbb")

def branchy(name: str, branches: int) -> str:
    """A function of `branches` plain `if` statements in a row, so a
    complexity of `branches + 1`."""
    body = "".join(f"    if x == {n}:\n        x += 1\n" for n in range(branches))
    return f"def {name}(x):\n{body}    return x\n"


def write(root: Path, path: str, text: str) -> Path:
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text, encoding="utf-8", newline="\n")
    return full


def lines_of(count: int) -> str:
    """A module of exactly `count` lines, by `wc -l`."""
    return "".join(f"x{n} = {n}\n" for n in range(count))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Four small packages, and a `tools/` folder for the budget file."""
    root = tmp_path / "repo"
    write(root, "emet-sdk/emet_sdk/__init__.py", '"""The contract."""\n')
    write(root, "emet-sdk/emet_sdk/types.py", lines_of(20))
    write(root, "emet-sdk/emet_sdk/cli.py", lines_of(7))
    write(root, "emet-hal/emet_hal/__init__.py", "")
    write(root, "emet-hal/emet_hal/drive.py", branchy("turn", 3))
    write(root, "emet-providers/emet_providers/__init__.py", "")
    write(root, "emet-providers/emet_providers/voice.py", branchy("speak", 12))
    write(root, "emet-engine/emet_engine/__init__.py", "")
    write(root, "emet-engine/emet_engine/loop.py", branchy("run", 2) + "\n\n" + branchy("stop", 1))
    (root / "tools").mkdir()
    return root


def run(root: Path, capsys, *, when=DAY_ONE, vulture=None, **flags) -> tuple[int, str]:
    code = check_health.check(root, root / "tools" / "health_budget.json", vulture=vulture,
                              date=when[0], commit=when[1], **flags)
    return code, capsys.readouterr().out


def budgets(root: Path) -> dict:
    return json.loads((root / "tools" / "health_budget.json").read_text(encoding="utf-8"))["budgets"]


def fake_vulture(tmp_path: Path, lines: list[str], *, exit_code: int = 3) -> list[str]:
    """A program that answers as vulture does: a version, then its findings
    from `--min-confidence` up, or exit 2 when it is given no path. It writes
    the arguments it was run with to `fake_vulture.args`, for `vulture_args`."""
    script = tmp_path / "fake_vulture.py"
    script.write_text(textwrap.dedent(f"""\
        import json, re, sys
        if "--version" in sys.argv:
            print("vulture 9.9")
            raise SystemExit(0)
        args = sys.argv[1:]
        with open({str(tmp_path / "fake_vulture.args")!r}, "w", encoding="utf-8") as handle:
            json.dump(args, handle)
        floor = 0
        if "--min-confidence" in args:
            at = args.index("--min-confidence")
            floor = int(args[at + 1])
            del args[at:at + 2]
        if not args:
            print("vulture: error: no path given", file=sys.stderr)
            raise SystemExit(2)
        for line in {lines!r}:
            if int(re.search(r"[(](\\d+)% confidence", line).group(1)) >= floor:
                print(line)
        raise SystemExit({exit_code})
        """), encoding="utf-8")
    return [sys.executable, str(script)]


def vulture_args(tmp_path: Path) -> list[str]:
    """What `fake_vulture` was last run with, `--version` aside."""
    return json.loads((tmp_path / "fake_vulture.args").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The SDK's line cap, which is the required part
# --------------------------------------------------------------------------


def test_a_tree_over_the_line_budget_fails(tree, capsys):
    write(tree, "emet-sdk/emet_sdk/big.py", lines_of(SDK_LINE_CAP - 21))   # 1 + 20 + 3979 = 4000
    code, out = run(tree, capsys, update=True)
    assert code == 0, out
    assert f"{SDK_LINE_CAP} lines of {SDK_LINE_CAP}" in out and "ok, 0 left" in out

    write(tree, "emet-sdk/emet_sdk/big.py", lines_of(SDK_LINE_CAP - 20))   # one line more
    code, out = run(tree, capsys)
    assert code == 1
    assert "OVER by 1" in out
    assert out.rstrip().splitlines()[-1].startswith("health: FAILED. The SDK's contract modules are 4001 lines")


def test_the_cap_fails_without_strict_and_whatever_the_budget_says(tree, capsys):
    """The required part does not wait on the advisory one: no budget file,
    no --strict, and the exit is still 1."""
    write(tree, "emet-sdk/emet_sdk/big.py", lines_of(SDK_LINE_CAP))
    code, out = run(tree, capsys)
    assert code == 1
    assert "OVER by 21" in out


def test_cli_py_is_not_counted(tree, capsys):
    write(tree, "emet-sdk/emet_sdk/big.py", lines_of(SDK_LINE_CAP - 21))
    write(tree, "emet-sdk/emet_sdk/cli.py", lines_of(500))
    assert check_health.sdk_lines(tree) == (SDK_LINE_CAP, 500)
    code, out = run(tree, capsys, update=True)
    assert code == 0, out
    assert "cli.py and testing/ outside, 500 lines; 4500 with them" in out

    # The same 500 lines in a contract module are 500 over.
    (tree / "emet-sdk/emet_sdk/cli.py").rename(tree / "emet-sdk/emet_sdk/commands.py")
    assert check_health.sdk_lines(tree) == (SDK_LINE_CAP + 500, 0)
    code, out = run(tree, capsys)
    assert code == 1 and "OVER by 500" in out


def test_the_count_is_wc_l(tmp_path):
    """Newline characters, so a last line with no newline is not a line, as
    `wc -l` has it."""
    assert check_health.line_count(write(tmp_path, "a.py", "one\ntwo\n")) == 2
    assert check_health.line_count(write(tmp_path, "b.py", "one\ntwo")) == 1
    assert check_health.line_count(write(tmp_path, "c.py", "")) == 0


def test_a_package_inside_the_sdk_is_under_the_cap_and_the_kit_is_not(tree, capsys):
    """Decision E8 leaves two things outside the 4,000: `cli.py`, and the
    conformance kit in `emet_sdk.testing`. A contract module in any other
    folder of the SDK, memory rows in `emet_sdk/memory/` say, is counted."""
    write(tree, "emet-sdk/emet_sdk/testing/kit.py", lines_of(900))
    write(tree, "emet-sdk/emet_sdk/memory/__init__.py", lines_of(1))
    write(tree, "emet-sdk/emet_sdk/memory/rows.py", lines_of(50))
    write(tree, "emet-sdk/emet_sdk/memory/cli.py", lines_of(4))      # only the top-level cli.py is outside
    write(tree, "emet-sdk/emet_sdk/__pycache__/types.cpython-313.py", lines_of(99))
    inside, outside = check_health.sdk_lines(tree)
    assert (inside, outside) == (21 + 1 + 50 + 4, 7 + 900)
    # The package's own line budget counts all of it.
    assert check_health.measure(tree, None).numbers["emet_sdk"]["lines"] == 21 + 55 + 7 + 900

    # The same rows past the cap fail it, as they would at the top level.
    write(tree, "emet-sdk/emet_sdk/memory/rows.py", lines_of(SDK_LINE_CAP))
    code, out = run(tree, capsys)
    assert code == 1 and f"OVER by {21 + 1 + 4}" in out


# --------------------------------------------------------------------------
# The budget that only falls
# --------------------------------------------------------------------------


def test_update_writes_a_missing_budget_from_the_measurement(tree, capsys):
    code, out = run(tree, capsys, update=True)
    assert code == 0, out
    assert "wrote health_budget.json from this measurement (2026-01-01, aaaaaaa)" in out
    held = budgets(tree)
    assert list(held) == list(PACKAGES)
    assert held["emet_providers"]["highest_complexity"] == {"budget": 13, "date": "2026-01-01", "commit": "aaaaaaa"}
    assert held["emet_providers"]["over_complexity"]["budget"] == 1
    assert held["emet_hal"]["over_complexity"]["budget"] == 0
    assert held["emet_engine"]["longest_function"]["budget"] == 6
    assert held["emet_sdk"]["lines"]["budget"] == 28
    # vulture did not run, so there is no number to hold: absent, never 0.
    assert all("unused_names" not in entries for entries in held.values())
    code, out = run(tree, capsys)
    assert code == 0 and out.rstrip().endswith("health: ok")


def test_a_rise_is_a_warning_and_refused_under_strict(tree, capsys):
    run(tree, capsys, update=True)
    write(tree, "emet-hal/emet_hal/drive.py", branchy("turn", 3) + "\n\n" + branchy("spin", COMPLEXITY_OVER))

    code, out = run(tree, capsys)
    assert code == 0, "advisory: a rise is a warning without --strict"
    assert "  ! emet_hal over_complexity rose: 1 against a budget of 0" in out
    assert "  ! emet_hal highest_complexity rose: 11 against a budget of 4" in out
    assert "  ! emet_hal longest_function rose: 22 against a budget of 8" in out
    assert "  ! emet_hal lines rose: 32 against a budget of 8" in out
    assert ("rose: 32 against a budget of 8. Bring it back down, or raise it by hand in health_budget.json "
            "with today's date and commit, where the pull request's diff shows it") in out
    assert out.rstrip().splitlines()[-1] == (
        "health: ok, with 4 warning(s). The budget is advisory: --strict makes each warning a failure.")

    code, out = run(tree, capsys, strict=True)
    assert code == 1
    assert "  x emet_hal over_complexity rose: 1 against a budget of 0" in out
    assert out.rstrip().endswith("health: FAILED under --strict, 4 problem(s).")


def test_a_fall_is_offered_and_the_file_is_left_alone(tree, capsys):
    run(tree, capsys, update=True)
    before = (tree / "tools/health_budget.json").read_bytes()
    write(tree, "emet-providers/emet_providers/voice.py", branchy("speak", 4))

    code, out = run(tree, capsys, when=DAY_TWO)
    assert code == 0
    assert "below budget: lower health_budget.json in the same pull request" in out
    assert ('  emet_providers: "highest_complexity": {"budget": 5, "date": "2026-02-02", '
            '"commit": "bbbbbbb"}   (was 13)') in out
    assert '  emet_providers: "over_complexity": {"budget": 0,' in out
    assert "13 (" not in out and "5 (13)" in out, "the table shows the budget beside a number that moved"
    assert out.rstrip().endswith("health: ok"), "a fall is good news, so it is no warning"
    assert (tree / "tools/health_budget.json").read_bytes() == before


def test_update_lowers_what_fell_and_never_raises(tree, capsys):
    run(tree, capsys, update=True)
    before = budgets(tree)
    write(tree, "emet-providers/emet_providers/voice.py", branchy("speak", 4))            # falls
    write(tree, "emet-engine/emet_engine/loop.py", branchy("run", 15))                    # rises

    code, out = run(tree, capsys, when=DAY_TWO, update=True)
    assert code == 0
    after = budgets(tree)
    assert after["emet_providers"]["highest_complexity"] == {"budget": 5, "date": "2026-02-02", "commit": "bbbbbbb"}
    assert after["emet_providers"]["over_complexity"]["budget"] == 0
    assert "emet_providers highest_complexity: lowered from 13 to 5" in out
    # What rose keeps its old number, its old date and its old commit.
    assert after["emet_engine"] == before["emet_engine"]
    assert ("emet_engine highest_complexity rose: 16 against a budget of 3. "
            "--update never raises a number, so the budget stays") in out
    for package, entries in after.items():
        for name, entry in entries.items():
            assert entry["budget"] <= before[package][name]["budget"], f"{package} {name} was raised"

    # And under --strict the rise still fails, updated or not.
    code, out = run(tree, capsys, when=DAY_TWO, update=True, strict=True)
    assert code == 1


def test_update_with_nothing_fallen_leaves_the_file_as_it_is(tree, capsys):
    run(tree, capsys, update=True)
    before = (tree / "tools/health_budget.json").read_bytes()
    code, out = run(tree, capsys, when=DAY_TWO, update=True)
    assert code == 0 and "left as it is, nothing fell" in out
    assert (tree / "tools/health_budget.json").read_bytes() == before


def test_without_a_budget_file_nothing_is_held_and_it_says_so(tree, capsys):
    code, out = run(tree, capsys)
    assert code == 0
    assert "there is no health_budget.json, so nothing is held. Write it with --update." in out
    code, out = run(tree, capsys, strict=True)
    assert code == 1


def test_a_broken_budget_file_is_a_problem_and_is_never_written_over(tree, capsys):
    path = tree / "tools/health_budget.json"
    for broken in ("{ not json", '{"budgets": 7}', '{"budgets": {"emet_sdk": {"lines": {"budget": "many"}}}}',
                   '{"budgets": {"emet_sdk": {"lines": {"budget": -1}}}}',
                   '{"budgets": {"emet_sdk": {"lines": {"budget": true}}}}'):
        path.write_text(broken, encoding="utf-8")
        code, out = run(tree, capsys, update=True)
        assert code == 0 and "health_budget.json" in out and "delete it and run with --update" in out
        assert path.read_text(encoding="utf-8") == broken
        code, out = run(tree, capsys, strict=True)
        assert code == 1


@pytest.mark.parametrize("key, value, now", [("complexity_over", 15, COMPLEXITY_OVER),
                                             ("vulture_min_confidence", 60, check_health.VULTURE_BUDGET_CONFIDENCE)])
def test_a_budget_measured_at_another_threshold_is_said_and_never_written_over(tree, capsys, key, value, now):
    run(tree, capsys, update=True)
    path = tree / "tools/health_budget.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[key] = value
    path.write_text(check_health.budget_text(data), encoding="utf-8")
    code, out = run(tree, capsys)
    assert code == 0
    assert (f"was measured with {key} at {value} and this tool counts at {now}, so its numbers mean "
            f"something else. Delete it and run with --update to measure afresh.") in out

    # A number measured at this tool's threshold is not written under the
    # file's other one, even when it fell.
    before = path.read_bytes()
    write(tree, "emet-providers/emet_providers/voice.py", branchy("speak", 4))
    code, out = run(tree, capsys, when=DAY_TWO, update=True)
    assert code == 0
    assert "health: health_budget.json left as it is, since it was measured at other thresholds" in out
    assert path.read_bytes() == before
    code, out = run(tree, capsys, strict=True)
    assert code == 1


def test_a_budget_counted_by_another_vulture_is_said_and_not_lowered_from(tree, tmp_path, capsys):
    run(tree, capsys, update=True, vulture=fake_vulture(tmp_path, FINDINGS))
    path = tree / "tools/health_budget.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for entries in data["budgets"].values():
        entries["unused_names"]["tool"] = "vulture 2.16"
    path.write_text(check_health.budget_text(data), encoding="utf-8")
    before = path.read_bytes()

    # vulture 9.9 finds one fewer: no fall from 2.16's count.
    code, out = run(tree, capsys, when=DAY_TWO, update=True, vulture=fake_vulture(tmp_path, FINDINGS[1:]))
    assert code == 0
    assert ("health_budget.json counted unused names with vulture 2.16 and this run used vulture 9.9, "
            "which counts by its own rules, so --update lowers none of them") in out
    assert "unused_names: lowered" not in out
    assert path.read_bytes() == before
    code, out = run(tree, capsys, strict=True, vulture=fake_vulture(tmp_path, FINDINGS))
    assert code == 1


def test_the_budget_file_is_json_with_a_number_a_line(tree, capsys):
    run(tree, capsys, update=True)
    text = (tree / "tools/health_budget.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["format"] == check_health.BUDGET_FORMAT
    assert check_health.budget_text(data) == text, "what is written is what is read back"
    assert '      "lines": {"budget": 28, "date": "2026-01-01", "commit": "aaaaaaa"},\n' in text
    # Something the tool does not know is kept when the file is rewritten.
    data["note"] = "kept"
    data["budgets"]["emet_sdk"]["type_ignores"] = {"budget": 3, "date": "2026-01-01", "commit": "aaaaaaa"}
    (tree / "tools/health_budget.json").write_text(check_health.budget_text(data), encoding="utf-8")
    write(tree, "emet-sdk/emet_sdk/types.py", lines_of(10))
    run(tree, capsys, when=DAY_TWO, update=True)
    again = json.loads((tree / "tools/health_budget.json").read_text(encoding="utf-8"))
    assert again["note"] == "kept"
    assert again["budgets"]["emet_sdk"]["type_ignores"]["budget"] == 3
    assert again["budgets"]["emet_sdk"]["lines"] == {"budget": 18, "date": "2026-02-02", "commit": "bbbbbbb"}


# --------------------------------------------------------------------------
# Unused names: vulture when it is there, "not run" when it is not
# --------------------------------------------------------------------------

FINDINGS = [
    r"emet-hal\emet_hal\drive.py:1: unused import 'os' (90% confidence)",
    "emet-hal/emet_hal/drive.py:9: unreachable code after 'return' (100% confidence, 2 lines)",
    "emet-engine/emet_engine/loop.py:4: unused variable 'spare' (100% confidence)",
    "emet-engine/emet_engine/loop.py:2: unused function 'stop' (60% confidence)",
    "emet-sdk/emet_sdk/types.py:3: unused variable 'x2' (60% confidence)",
]


def test_vulture_absent_is_not_run_and_never_zero(tree, tmp_path, capsys):
    measured = check_health.measure(tree, None)
    assert not measured.unused.ran
    assert all(numbers["unused_names"] is None for numbers in measured.numbers.values())

    # Seeded with vulture there: two packages hold a number above zero.
    run(tree, capsys, update=True, vulture=fake_vulture(tmp_path, FINDINGS))
    held = budgets(tree)
    assert held["emet_hal"]["unused_names"]["budget"] == 2
    assert held["emet_engine"]["unused_names"]["budget"] == 1

    # Then run where vulture is not. Zero would read as a fall from 2 and 1.
    before = (tree / "tools/health_budget.json").read_bytes()
    code, out = run(tree, capsys, when=DAY_TWO, update=True, vulture=None)
    assert code == 0
    assert "unused names: not run (vulture is not importable, on PATH or in tools/.venv)" in out
    assert "Nothing was counted, and the budget stays." in out
    assert out.count("not run") >= 1 + len(PACKAGES), "each package's cell says so too"
    assert "unused_names" not in out, "no number of it rose, fell or was lowered"
    assert "below budget" not in out
    assert (tree / "tools/health_budget.json").read_bytes() == before
    assert out.rstrip().endswith("health: ok")


def test_vulture_is_counted_by_package_from_the_budgets_confidence(tree, tmp_path, capsys):
    command = fake_vulture(tmp_path, FINDINGS)
    measured = check_health.measure(tree, command)
    assert measured.unused.ran and measured.unused.version == "9.9"
    # Run once over the four packages, from the confidence the report lists.
    assert vulture_args(tmp_path) == [*PACKAGES.values(), "--min-confidence",
                                      str(check_health.VULTURE_LIST_CONFIDENCE)]
    counts = {package: numbers["unused_names"] for package, numbers in measured.numbers.items()}
    assert counts == {"emet_sdk": 0, "emet_hal": 2, "emet_providers": 0, "emet_engine": 1}

    code, out = run(tree, capsys, update=True, vulture=command)
    assert code == 0
    assert "unused names (vulture 9.9): 3 at 80% confidence or more, which the budget counts" in out
    assert "  emet-hal/emet_hal/drive.py:1: unused import 'os' (90%)" in out, "a Windows path reads as the others do"
    assert "and 2 from 60%, to read and outside the budget" in out
    assert "  emet-engine/emet_engine/loop.py:2: unused function 'stop' (60%)" in out
    assert budgets(tree)["emet_hal"]["unused_names"] == {
        "budget": 2, "date": "2026-01-01", "commit": "aaaaaaa", "tool": "vulture 9.9"}

    # One more at 100% is a rise; one fewer is a fall.
    code, out = run(tree, capsys, vulture=fake_vulture(
        tmp_path, FINDINGS + ["emet-sdk/emet_sdk/types.py:1: unused import 're' (90% confidence)"]))
    assert "  ! emet_sdk unused_names rose: 1 against a budget of 0" in out
    code, out = run(tree, capsys, when=DAY_TWO, update=True, vulture=fake_vulture(tmp_path, FINDINGS[1:]))
    assert "emet_hal unused_names: lowered from 2 to 1" in out


def test_what_vulture_reports_at_each_confidence_is_said(tree, tmp_path, capsys):
    """vulture 2.16's confidences: 100 for an unused function argument,
    which it words "unused variable", and for unreachable code; 90 for an
    import; 60 for the rest, a variable or an attribute included (its
    `core.py`, verified 2026-11-26). So an unused local never moves the
    budget, and the report says which kinds the budget counts."""
    realistic = [
        "emet-hal/emet_hal/drive.py:1: unused variable 'time_info' (100% confidence)",   # an argument
        "emet-hal/emet_hal/drive.py:2: unused variable 'last_saved' (60% confidence)",   # a local
        "emet-hal/emet_hal/drive.py:3: unused attribute 'spare' (60% confidence)",
    ]
    code, out = run(tree, capsys, update=True, vulture=fake_vulture(tmp_path, realistic))
    assert budgets(tree)["emet_hal"]["unused_names"]["budget"] == 1, "the argument alone"
    assert ("1 at 80% confidence or more, which the budget counts: unused imports, unused function "
            'arguments (vulture words them "unused variable") and unreachable code') in out
    assert ("and 2 from 60%, to read and outside the budget: unused variables, attributes, functions, "
            "methods, properties and classes.") in out


def test_a_vulture_that_fails_measured_nothing(tree, tmp_path, capsys):
    for code_from_vulture in (1, 2):
        measured = check_health.measure(tree, fake_vulture(tmp_path, FINDINGS, exit_code=code_from_vulture))
        assert not measured.unused.ran
        assert measured.unused.reason == f"vulture 9.9 exited {code_from_vulture}"
        assert measured.numbers["emet_hal"]["unused_names"] is None
    # Exit 0 is vulture finding nothing, which is a measurement of zero.
    clean = check_health.measure(tree, fake_vulture(tmp_path, [], exit_code=0))
    assert clean.unused.ran and clean.numbers["emet_hal"]["unused_names"] == 0
    missing = check_health.run_vulture(tree, [str(tmp_path / "no-such-program")])
    assert not missing.ran
    # Given no folder, vulture refuses its command line.
    empty = tmp_path / "no-packages"
    empty.mkdir()
    assert check_health.run_vulture(empty, fake_vulture(tmp_path, FINDINGS)).reason == "vulture 9.9 exited 2"


def test_vulture_is_looked_for_in_three_places(tree, monkeypatch):
    monkeypatch.setattr(check_health.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(check_health.shutil, "which", lambda name: None)
    assert check_health.find_vulture(tree) is None

    venv = write(tree, "tools/.venv/bin/vulture", "")
    assert check_health.find_vulture(tree) == [str(venv)]
    monkeypatch.setattr(check_health.shutil, "which", lambda name: "/usr/bin/vulture")
    assert check_health.find_vulture(tree) == ["/usr/bin/vulture"]
    monkeypatch.setattr(check_health.importlib.util, "find_spec", lambda name: object())
    assert check_health.find_vulture(tree) == [sys.executable, "-m", "vulture"]


# --------------------------------------------------------------------------
# What is counted, exactly
# --------------------------------------------------------------------------


def functions(source: str) -> dict[str, check_health.Function]:
    found = check_health.functions_in(textwrap.dedent(source), "emet_hal", "emet-hal/emet_hal/x.py")
    return {f.name: f for f in found}


def test_complexity_counts_what_the_docstring_says():
    found = functions("""\
        def plain():
            return 1

        def branches(a, b, items):
            if a:                                  # +1
                pass
            elif b:                                # +1
                pass
            else:                                  # nothing
                pass
            for item in items:                     # +1
                while item:                        # +1
                    item -= 1
            try:
                pass
            except ValueError:                     # +1
                pass
            except (KeyError, OSError):            # +1
                pass
            else:                                  # nothing
                pass
            finally:                               # nothing
                pass
            with open(a) as handle:                # nothing
                assert handle                      # nothing
            return a if b else None                # +1

        def booleans(a, b, c, d):
            return (a and b and c) or d            # +2 for the ands, +1 for the or

        def comprehensions(rows):
            return [cell for row in rows for cell in row if cell if cell > 1]   # +2 for, +2 if

        def matching(value):
            match value:
                case 1:                            # +1
                    return "one"
                case [x, y] if x:                  # +1 (the guard is the case's)
                    return "pair"
                case _:                            # +1
                    return "other"

        async def waits(stream):
            async for item in stream:              # +1
                await item
        """)
    assert found["plain"].complexity == 1
    assert found["branches"].complexity == 1 + 7
    assert found["booleans"].complexity == 1 + 3
    assert found["comprehensions"].complexity == 1 + 4
    assert found["matching"].complexity == 1 + 3
    assert found["waits"].complexity == 1 + 1


def test_a_nested_function_is_its_own_and_a_lambda_is_its_outers():
    found = functions("""\
        class Loop:
            def outer(self, items):
                def inner(x):
                    if x:
                        return 1
                    if not x:
                        return 2
                async def waits(x):
                    while x:
                        x -= 1
                key = lambda item: item if item else 0
                if items:
                    return sorted(items, key=key)
        """)
    assert set(found) == {"Loop.outer", "Loop.outer.inner", "Loop.outer.waits"}
    assert found["Loop.outer.inner"].complexity == 3
    assert found["Loop.outer.waits"].complexity == 2
    assert found["Loop.outer"].complexity == 1 + 1 + 1, "its own if, and the lambda's, and none of inner's or waits'"


def test_length_runs_from_the_def_line_to_the_last_line():
    found = functions("""\
        import functools

        @functools.cache
        @staticmethod
        def decorated(
            a,
            b,
        ):
            \"\"\"A docstring counts.\"\"\"

            # so does a comment, and the blank line above
            return a
        """)
    assert found["decorated"].line == 5
    assert found["decorated"].length == 8


def test_the_report_names_the_worst_with_file_and_line(tree, capsys):
    code, out = run(tree, capsys, update=True)
    lines = out.splitlines()
    start = lines.index(f"the {check_health.WORST} worst functions by complexity (over {COMPLEXITY_OVER} is counted):")
    assert lines[start + 1] == "    13  emet-providers/emet_providers/voice.py:1  speak"
    assert lines[start + 2] == "     4  emet-hal/emet_hal/drive.py:1  turn"
    longest = lines.index(f"the {check_health.WORST} longest functions, in lines:")
    assert lines[longest + 1] == "    26  emet-providers/emet_providers/voice.py:1  speak"
    assert "     4  emet-engine/emet_engine/loop.py:9  stop" in lines


def test_a_file_that_does_not_parse_is_said_and_the_rest_is_measured(tree, capsys):
    write(tree, "emet-hal/emet_hal/broken.py", "def (:\n")
    code, out = run(tree, capsys, update=True)
    assert code == 0
    assert "emet-hal/emet_hal/broken.py: could not be parsed" in out
    assert budgets(tree)["emet_hal"]["lines"]["budget"] == 9


# --------------------------------------------------------------------------
# The command, as `tools/ci.py` calls it
# --------------------------------------------------------------------------


def command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOLS / "check_health.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", check=False, timeout=300)


def test_the_commit_is_marked_when_the_measured_folders_differ_from_it(tree, monkeypatch):
    """What `--update` records when a pull request lowers a number in the
    same diff as the code: the commit the tree sits on, marked -dirty."""
    answers = {"rev-parse": (0, "2cc6acb\n"), "status": (0, " M emet-hal/emet_hal/drive.py\n")}
    asked: list[list[str]] = []

    def fake_run(command: list[str], root: Path) -> tuple[int, str]:
        asked.append(command)
        return answers[command[1]]

    monkeypatch.setattr(check_health, "_run", fake_run)
    assert check_health.commit_now(tree) == "2cc6acb-dirty"
    assert asked[1] == ["git", "status", "--porcelain", "--", *PACKAGES.values()]
    answers["status"] = (0, "")
    assert check_health.commit_now(tree) == "2cc6acb"
    answers["rev-parse"] = (128, "fatal: not a git repository")
    assert check_health.commit_now(tree) == "unknown"


def test_the_command_line_and_its_exit_codes(tree, tmp_path):
    done = command(str(tree), "--update")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tree / "tools/health_budget.json").is_file()
    assert command(str(tree)).returncode == 0

    write(tree, "emet-engine/emet_engine/loop.py", branchy("run", 15))
    assert command(str(tree)).returncode == 0
    assert command(str(tree), "--strict").returncode == 1

    elsewhere = tmp_path / "other.json"
    assert command(str(tree), "--budget", str(elsewhere), "--update").returncode == 0
    assert elsewhere.is_file()

    write(tree, "emet-sdk/emet_sdk/big.py", lines_of(SDK_LINE_CAP))
    done = command(str(tree))
    assert done.returncode == 1 and "health: FAILED. The SDK's contract modules" in done.stdout

    done = command(str(tmp_path / "nothing-here"))
    assert done.returncode == 2
    assert "does not look like the Emet repository" in done.stderr


# --------------------------------------------------------------------------
# The real tree
# --------------------------------------------------------------------------


def test_the_real_sdk_is_under_its_cap():
    inside, outside = check_health.sdk_lines(REPO)
    assert 0 < inside <= SDK_LINE_CAP, f"the SDK's contract modules are {inside} lines"
    assert outside == check_health.line_count(REPO / "emet-sdk/emet_sdk/cli.py")


def test_the_real_budget_holds_every_number_with_its_date_and_commit():
    path = TOOLS / check_health.BUDGET_NAME
    data = check_health.read_budget(path)
    assert data is not None, f"no {path.name}: write it with `python tools/check_health.py . --update`"
    assert data["format"] == check_health.BUDGET_FORMAT
    assert data["complexity_over"] == COMPLEXITY_OVER
    assert data["vulture_min_confidence"] == check_health.VULTURE_BUDGET_CONFIDENCE
    assert list(data["budgets"]) == list(PACKAGES)
    for package, entries in data["budgets"].items():
        assert list(entries) == list(MEASURES), package
        for name, entry in entries.items():
            assert isinstance(entry["budget"], int), (package, name)
            assert len(entry["date"]) == 10 and entry["date"][4] == "-", (package, name)
            assert entry["commit"] and entry["commit"] != "unknown", (package, name)
        assert entries["unused_names"]["tool"].startswith("vulture "), package
    assert check_health.budget_text(data) == path.read_text(encoding="utf-8")


def test_one_rule_says_what_is_outside_the_contract():
    """The line budget, the API listing and the API golden leave the same
    things out: `cli.py` and the conformance kit's `testing/` (E8)."""
    assert check_health.OUTSIDE_THE_CONTRACT == api_diff.OUTSIDE_THE_CONTRACT == frozenset({"cli.py", "testing"})
    spec = importlib.util.spec_from_file_location("emet_sdk_public_api_rule",
                                                  REPO / "emet-sdk/tests/test_public_api.py")
    golden = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(golden)
    assert {name.removesuffix(".py") for name in check_health.OUTSIDE_THE_CONTRACT} == golden.OUTSIDE_THE_CONTRACT
