"""`emet-listen`, driven the way a person drives it.

Offline, like the rest of the suite: a wav file for a microphone, the mock
detector for pocketsphinx, and a sink that discards. The manifest and soul
loaders are stubbed so the loop can be exercised without a full document,
which the session tests already cover from the other side.

The case that matters is the interrupted one. A microphone never ends, so
Ctrl-C is how every live run finishes, and `asyncio.run` delivers Ctrl-C as a
cancellation of the running task. A run that dropped its numbers at that point
would make the ten-minute live soak in the 0.3 criteria unreportable.
"""

from __future__ import annotations

import argparse
import asyncio
import wave
from pathlib import Path

from emet_engine import cli
from emet_engine.session import ListenSession

PHRASE = "hey emet"
FRAME = 1280  # samples per frame at 16 kHz, 80 ms


def write_wav(path: Path, pcm: bytes) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return str(path)


def silence(frames: int) -> bytes:
    return bytes(2 * frames)


def saying(times: int) -> bytes:
    """Frames whose bytes spell the phrase, which is what the mock hears.

    Four seconds of silence after each phrase: the endpointer waits a 2.5 s
    lead-in for speech before it gives up on a wake, and the next phrase must
    arrive after that, or it is swallowed by the turn still in progress.
    """
    return (silence(FRAME) + PHRASE.encode() + silence(FRAME * 50)) * times


def body(path: str) -> dict:
    return {
        "audio": {
            "input": {"source": "wav", "device": "file", "params": {"path": path}},
            "output": {"sink": "null", "device": "none"},
            "wake": {"engine": "mock", "params": {}},
        }
    }


def soul() -> dict:
    return {"identity": {"name": "Emet", "wake_word": PHRASE}}


def stub_loaders(monkeypatch, manifest: dict, soul_doc: dict) -> None:
    monkeypatch.setattr(
        cli, "_load", lambda path, kind, registry: manifest if kind == "manifest" else soul_doc
    )


def args(**overrides) -> argparse.Namespace:
    base = {"manifest": "body.yaml", "soul": "soul.yaml", "replay": None, "echo": False, "stats": True}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_a_finished_replay_reports(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "three.wav", saying(3))
    stub_loaders(monkeypatch, body(wav), soul())

    rc = asyncio.run(cli._run(args()))

    out = capsys.readouterr().out
    assert rc == 0
    assert "source ended. heard it 3 time(s)." in out
    assert "realtime" in out and "verdict" in out


def test_an_interrupted_live_run_still_reports(tmp_path, monkeypatch, capsys):
    """Cancel the task after the first turn, as `asyncio.run` does on SIGINT.

    A real microphone blocks between frames, so the cancellation lands at an
    await inside the loop. A file never blocks, so the stand-in yields to the
    event loop once after cancelling, which is where the cancellation is
    delivered. The run must still print what it measured, and exit cleanly.
    """
    wav = write_wav(tmp_path / "long.wav", saying(3))
    stub_loaders(monkeypatch, body(wav), soul())

    real_turns = ListenSession.turns

    async def turns_then_ctrl_c(self):
        async for turn in real_turns(self):
            yield turn
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    monkeypatch.setattr(ListenSession, "turns", turns_then_ctrl_c)

    rc = asyncio.run(cli._run(args()))

    out = capsys.readouterr().out
    assert rc == 0
    assert "stopped. heard it 1 time(s)." in out
    assert "source ended" not in out
    assert "realtime" in out and "verdict" in out
