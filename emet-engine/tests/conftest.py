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


# --------------------------------------------------------- the hardware guard


def _hardware_guard():
    """Load `tools/hardware_guard.py` once by path, the first one found
    walking up from this folder, and put its wrappers in place (0.5.7-D10
    (a)). A tree without one fails the collection, naming the file."""
    import importlib.util
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for folder in (here, *here.parents):
        path = folder / "tools" / "hardware_guard.py"
        if path.is_file():
            module = sys.modules.get("emet_hardware_guard")
            if module is None:
                spec = importlib.util.spec_from_file_location("emet_hardware_guard", path)
                module = importlib.util.module_from_spec(spec)
                sys.modules["emet_hardware_guard"] = module
                spec.loader.exec_module(module)
            module.install()
            return module
    raise RuntimeError(f"no tools/hardware_guard.py in {here} or any folder above it: every suite loads the "
                       f"hardware guard (0.5.7-D10 (a)), and these tests do not run without it")


#: Autouse: no test opens a sound device stream or connects off this machine.
hardware_guard = _hardware_guard().hardware_guard
