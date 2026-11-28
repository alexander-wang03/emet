"""The defaults table, read the way the release check reads it.

`tools/release_check.py` reads `emet_providers/lifecycle.py` with `ast`,
without importing it, so these tests hold the table to that shape: one
`DefaultModel(...)` call per row, keyword arguments only, literal values
only. They also hold that every default a plugin ships has a row, so a new
default cannot reach a release without its vendor's dates beside it.
"""

from __future__ import annotations

import ast
import datetime
import inspect
import sys
from pathlib import Path

import pytest
import yaml

from emet_providers import anthropic, deepgram, deepgram_voice, lifecycle, openai
from emet_providers.lifecycle import DEFAULTS, DefaultModel

SOURCE = Path(inspect.getfile(lifecycle))
VENDORS = {"openai", "anthropic", "deepgram"}


def table_calls() -> list[ast.Call]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "DEFAULTS":
            assert isinstance(node.value, ast.Tuple), "DEFAULTS is one tuple literal"
            return list(node.value.elts)
    raise AssertionError("lifecycle.py has no DEFAULTS assignment")


def read_without_importing() -> list[dict]:
    rows = []
    for call in table_calls():
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "DefaultModel", ast.dump(call)
        assert not call.args, "a row is written with keyword arguments only"
        rows.append({kw.arg: ast.literal_eval(kw.value) for kw in call.keywords})
    return rows


def test_the_table_reads_with_ast_alone_and_matches_the_module():
    rows = read_without_importing()
    assert [DefaultModel(**row) for row in rows] == list(DEFAULTS)
    for row in rows:
        assert set(row) == set(DefaultModel._fields), row


def test_every_value_is_a_literal_of_its_fields_type():
    for row in DEFAULTS:
        assert row.vendor in VENDORS, row
        assert isinstance(row.model, str) and row.model, row
        assert isinstance(row.used_by, tuple) and row.used_by and all(isinstance(u, str) for u in row.used_by), row
        assert row.pinned in (True, False, None), row
        assert row.source.startswith("https://"), row


def test_every_date_is_a_day_and_no_row_was_read_in_the_future():
    today = datetime.date.today()
    for row in DEFAULTS:
        assert datetime.date.fromisoformat(row.read) <= today, row
        for day in (row.retires, row.floor):
            if day is not None:
                datetime.date.fromisoformat(day)
                assert len(day) == 10, row


def test_each_model_has_one_row():
    models = [row.model for row in DEFAULTS]
    assert len(models) == len(set(models))


@pytest.mark.parametrize(
    ("module", "vendor"),
    [(openai, "openai"), (anthropic, "anthropic"), (deepgram, "deepgram"), (deepgram_voice, "deepgram")],
)
def test_every_plugins_default_model_has_a_row_of_its_vendor(module, vendor):
    rows = {row.model: row for row in DEFAULTS}
    assert module.DEFAULT_MODEL in rows, f"{module.__name__}.DEFAULT_MODEL has no row in lifecycle.py"
    assert rows[module.DEFAULT_MODEL].vendor == vendor


def test_every_cloud_model_of_the_example_soul_has_a_row():
    soul = next((p / "emet-sdk" / "examples" / "emet-soul.yaml" for p in SOURCE.parents if (p / "emet-sdk" / "examples" / "emet-soul.yaml").is_file()), None)
    if soul is None:
        pytest.skip("the example soul is outside this installed tree; release_check.py reads it from the repository")
    stages = yaml.safe_load(soul.read_text(encoding="utf-8"))["models"]
    rows = {row.model for row in DEFAULTS}
    cloud = {s["model"] for s in stages.values() if isinstance(s, dict) and s.get("provider") in VENDORS and s.get("model")}
    assert cloud and cloud <= rows, cloud - rows


def test_the_rows_hold_what_was_read_on_2026_11_27():
    """The plan's Stage 0.10 and ledger L42, as the vendors' pages gave them."""
    rows = {row.model: row for row in DEFAULTS}
    opus = rows["claude-opus-5"]
    assert (opus.retires, opus.floor, opus.pinned) == (None, "2027-07-24", True)
    terra = rows["gpt-5.6-terra"]
    assert (terra.retires, terra.floor, terra.pinned) == (None, None, None), "L42: pinned is unverified"
    for model in ("nova-3", "aura-2-thalia-en"):
        assert (rows[model].retires, rows[model].floor) == (None, None)


def test_the_module_imports_only_the_standard_library():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names
