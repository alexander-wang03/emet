"""Every warning is an error in this suite, and the line that makes it so is held."""

import warnings

import pytest


def test_the_suite_runs_with_warnings_as_errors(request):
    """`filterwarnings` in `pyproject.toml`: `error` first, and after it
    only an `ignore` that names the start of a message, a category and a
    module. A blanket `ignore::DeprecationWarning`, a deleted line or a
    `-W` that loosens it fails here (review RB143, RB158)."""
    lines = request.config.getini("filterwarnings")
    assert lines[:1] == ["error"]
    for line in lines[1:]:
        action, message, category, module = (line.split(":") + [""] * 4)[:4]
        assert action == "ignore" and message and category and module, f"too broad: {line!r}"
    with pytest.raises(DeprecationWarning, match="canary"):
        warnings.warn("canary", DeprecationWarning)
