"""The certifi floor in every extra that reaches a vendor over HTTPS.

Emet never imports certifi. httpx verifies each HTTPS call to a vendor
against certifi's bundle, and versions before 2026.10.18 carry a root with
compromised intermediates (PYSEC-2026-4320, alias GHSA-6q48-6c7w-9gw2, fixed
in 2026.10.18: osv.dev, verified 2026-11-26). A fresh resolve takes the
newest certifi with or without the floor, so this test is what sees one
dropped or lowered (review RB144, RB157).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
FLOOR = (2026, 10, 18)


def test_every_extra_that_brings_httpx_floors_certifi():
    extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    with_httpx = sorted(name for name, reqs in extras.items() if any(re.match(r"httpx\b", r) for r in reqs))
    assert {"anthropic", "deepgram", "openai"} <= set(with_httpx)
    for name in with_httpx:
        floors = [
            tuple(int(part) for part in m.groups())
            for req in extras[name]
            if (m := re.fullmatch(r"certifi>=(\d+)\.(\d+)\.(\d+)", req.replace(" ", "")))
        ]
        assert floors, f"the {name} extra has no certifi floor"
        assert all(floor >= FLOOR for floor in floors), f"the {name} extra floors certifi below 2026.10.18"
