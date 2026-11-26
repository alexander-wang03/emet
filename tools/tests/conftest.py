"""Shared setup for the tests of tools/.

The bank tools are scripts, run as `python tools/bank/<tool>.py`, so they
import each other by module name from their own folder. These tests put
that folder on the path the same way.

**The real bank.** It lives off GitHub (tools/bank/FORMAT.md), so a test
that needs its audio skips without it, as the live-vendor tests skip
without keys. `EMET_BANK` names the bank's root; without it the laptop's
place is tried, `Project Emet/voicebank/` beside the repository.
`EMET_BANK_SEARCH` lists more directories to look in for a recording not
at its manifest's path, separated as PATH is (`os.pathsep`); on the
reference body the audio sits in `~/voicebank/audio/`.

**Hardware.** Nothing here opens a microphone or a speaker: the replay
tests use the wav source and the null sink, and `sounddevice` is made
unimportable in every process the replay runner starts.
"""

from __future__ import annotations

import math
import os
import struct
import sys
import wave
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
BANK_TOOLS = TOOLS / "bank"
if str(BANK_TOOLS) not in sys.path:
    sys.path.insert(0, str(BANK_TOOLS))


def real_bank_root() -> Path | None:
    """The real bank's root, when there is one on this machine."""
    named = os.environ.get("EMET_BANK")
    root = Path(named) if named else REPO.parent / "voicebank"
    return root if (root / "bank.json").is_file() else None


def real_bank_search() -> list[Path]:
    root = real_bank_root()
    dirs = [Path(d) for d in os.environ.get("EMET_BANK_SEARCH", "").split(os.pathsep) if d]
    if root is not None and (root / "audio").is_dir():
        dirs.append(root / "audio")
    return dirs


@pytest.fixture
def real_bank() -> Path:
    root = real_bank_root()
    if root is None:
        pytest.skip("no voice bank on this machine: set EMET_BANK to its root")
    return root


def write_wav(path: Path, samples: list[int] | bytes, rate: int = 16000) -> Path:
    """A 16-bit mono wav holding these samples (or these raw bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = samples if isinstance(samples, (bytes, bytearray)) else struct.pack(f"<{len(samples)}h", *samples)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(data))
    return path


def tone(seconds: float, rate: int = 16000, hz: float = 440.0, level: float = 0.1) -> list[int]:
    """A sine, so every frame of a test recording differs from the next."""
    n = int(round(seconds * rate))
    return [int(32767 * level * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)]


@pytest.fixture
def make_wav():
    return write_wav


@pytest.fixture
def make_tone():
    return tone
