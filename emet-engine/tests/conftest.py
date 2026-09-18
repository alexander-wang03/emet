"""What every engine test may assume about its surroundings.

**No keys file.** `emet-listen` reads `~/.config/emet/keys.env` and
`/etc/emet/keys.env` before the providers start, which is right for a person
and wrong for a test: on the first laptop that held real keys, two tests
asserting "no key, so stop before the network" found the keys, started two
vendors' preflights and passed a network call off as a unit test. The
default places are emptied here for every test, and `EMET_KEYS` is unset, so
a test sees only the environment it built. A test that wants a keys file
names one with `--keys`.
"""

from __future__ import annotations

import pytest

from emet_engine import keys


@pytest.fixture(autouse=True)
def no_developer_keys(monkeypatch):
    monkeypatch.setattr(keys, "default_paths", lambda: [])
    monkeypatch.delenv("EMET_KEYS", raising=False)
