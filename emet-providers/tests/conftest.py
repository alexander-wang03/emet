"""What every provider test may assume about its surroundings.

**No real voices.** The Piper plugin looks for a voice in `voices_dir`, then
in `$XDG_DATA_HOME/emet/voices` or `~/.local/share/emet/voices`, then in
`/etc/emet/voices`. On the reference body the reference voice sits in the
second of those, and a test asserting "no model, so unhealthy" found it,
loaded it through the stand-in library and failed there alone, since no CI
machine has a voice downloaded. Both default places are pointed at the
test's own temporary directory, so a test sees only the voices it made.
"""

from __future__ import annotations

import pytest

from emet_providers import piper


@pytest.fixture(autouse=True)
def no_developer_voices(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setattr(piper, "SYSTEM_VOICES", tmp_path / "machine-voices")
