"""What every engine test may assume about its surroundings.

**No keys file.** `emet-listen` reads `~/.config/emet/keys.env` and
`/etc/emet/keys.env` before the providers start, which is right for a person
and wrong for a test: on the first laptop that held real keys, two tests
asserting "no key, so stop before the network" found the keys, started two
vendors' preflights and passed a network call off as a unit test. The
default places are emptied here for every test, and `EMET_KEYS` is unset, so
a test sees only the environment it built. A test that wants a keys file
names one with `--keys`.

**No real state place.** A body with a `body.id` keeps its state under
`/etc/emet/state/` or `~/.local/state/emet/`. A suite that wrote there would
leave files in a developer's home, and one that read a stale real file would
pass or fail by whatever the last run on that machine left behind. Both
places are pointed at the test's own temporary directory instead, so every
test starts with no state and leaves none.
"""

from __future__ import annotations

import pytest

from emet_engine import keys, state


@pytest.fixture(autouse=True)
def no_developer_keys(monkeypatch):
    monkeypatch.setattr(keys, "default_paths", lambda: [])
    monkeypatch.delenv("EMET_KEYS", raising=False)


@pytest.fixture(autouse=True)
def no_developer_state(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "default_dirs", lambda: [tmp_path / "machine-state", tmp_path / "user-state"])
