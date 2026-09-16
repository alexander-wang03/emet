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


def soul(chat: dict | None = None, **stt) -> dict:
    doc = {"identity": {"name": "Emet", "wake_word": PHRASE}}
    models: dict = {}
    if stt:
        models["stt"] = stt
    if chat is not None:
        models["chat"] = chat
    if models:
        doc["models"] = models
    return doc


def stub_loaders(monkeypatch, manifest: dict, soul_doc: dict) -> None:
    monkeypatch.setattr(
        cli, "_load", lambda path, kind, registry: manifest if kind == "manifest" else soul_doc
    )


def args(**overrides) -> argparse.Namespace:
    base = {
        "manifest": "body.yaml",
        "soul": "soul.yaml",
        "replay": None,
        "echo": False,
        "stats": True,
        "transcribe": False,
        "reply": False,
    }
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


def test_frames_dropped_during_echo_are_explained_not_blamed(tmp_path, monkeypatch, capsys):
    """The loop does not read the microphone while it plays audio back, so an
    echo run always drops frames during playback. Calling that "not keeping
    up" would be wrong; the reference body's first echo run said so."""
    wav = write_wav(tmp_path / "e.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())
    monkeypatch.setattr(ListenSession, "dropped", property(lambda self: 32))

    rc = asyncio.run(cli._run(args(echo=True)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "while the robot was speaking" in out
    assert "not keeping up" not in out


def test_frames_dropped_while_listening_are_a_warning(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "w.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())
    monkeypatch.setattr(ListenSession, "dropped", property(lambda self: 32))

    asyncio.run(cli._run(args()))
    assert "not keeping up" in capsys.readouterr().out


# ------------------------------------------------------------- --transcribe


def spoken(*words: bytes) -> bytes:
    """A turn: the phrase, then frames that spell words, then silence.

    The mock transcriber reads a frame's leading text; the energy detector
    hears those same bytes as loud, so the endpointer captures them as speech
    and closes the turn on the silence after.
    """
    def frame(payload: bytes = b"") -> bytes:
        return payload + bytes(2 * FRAME - len(payload))

    return frame() + frame(PHRASE.encode()) + b"".join(frame(w) for w in words) + frame() * 20


def test_transcribe_prints_partials_as_they_arrive_and_then_what_was_said(
    tmp_path, monkeypatch, capsys
):
    wav = write_wav(tmp_path / "said.wav", spoken(b"what", b"time", b"is it"))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock"))

    rc = asyncio.run(cli._run(args(transcribe=True)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "  stt      mock (streaming)" in out
    assert "    hearing 'what time'" in out
    assert "    said 'what time is it'" in out
    assert "stt final" in out, "the time to the final transcript is part of --stats"
    # The wake prints when it is heard, before the words that follow it.
    assert out.index("heard 'hey emet'") < out.index("hearing 'what'")


def test_without_the_flag_nothing_is_transcribed(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "quiet.wav", spoken(b"what", b"time"))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock"))

    asyncio.run(cli._run(args()))

    out = capsys.readouterr().out
    assert "said" not in out and "hearing" not in out and "stt" not in out


def test_transcribe_with_no_provider_configured_says_which_field_to_set(
    tmp_path, monkeypatch, capsys
):
    wav = write_wav(tmp_path / "none.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())

    rc = cli.main(["body.yaml", "soul.yaml", "--transcribe"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "models.stt.provider" in err
    assert "audio.stt.provider" in err


def test_transcribe_with_an_uninstalled_provider_is_a_missing_plugin(
    tmp_path, monkeypatch, capsys
):
    """A provider nobody has packaged has to be the plain missing-plugin
    message, not a stack trace."""
    wav = write_wav(tmp_path / "dg.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="whisper", key_env="EMET_OPENAI_KEY"))

    rc = cli.main(["body.yaml", "soul.yaml", "--transcribe"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "whisper" in err and "missing_plugin" in err


def test_transcribe_with_the_reference_provider_and_no_key_names_the_variable(
    tmp_path, monkeypatch, capsys
):
    """The reference soul names deepgram. Without the key, the run must stop
    before any network is touched and say which variable to export."""
    monkeypatch.delenv("EMET_DEEPGRAM_KEY", raising=False)
    wav = write_wav(tmp_path / "nokey.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="deepgram", key_env="EMET_DEEPGRAM_KEY"))

    rc = cli.main(["body.yaml", "soul.yaml", "--transcribe"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "EMET_DEEPGRAM_KEY" in err
    assert "will not run" in err


def test_a_false_wake_under_transcribe_reports_an_empty_final(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "false.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock"))

    rc = asyncio.run(cli._run(args(transcribe=True)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "probably a false wake" in out
    assert "said nothing the provider could make out" in out


# ------------------------------------------------------------------ --reply


def test_reply_prints_the_answer_as_it_streams_and_implies_transcribe(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "r.wav", spoken(b"what", b"time", b"is it"))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock", chat={"provider": "mock"}))

    rc = asyncio.run(cli._run(args(reply=True)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "  stt      mock (streaming)" in out, "--reply implies --transcribe"
    assert "  llm      mock" in out
    assert "    said 'what time is it'" in out
    assert "    reply: You said: what time is it" in out
    assert "llm first" in out and "llm done" in out


def test_reply_with_no_language_model_configured_names_the_field(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "n.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock"))

    rc = cli.main(["body.yaml", "soul.yaml", "--reply"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "models.chat.provider" in err


def test_reply_with_the_reference_provider_and_no_key_names_the_variable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EMET_OPENAI_KEY", raising=False)
    wav = write_wav(tmp_path / "k.wav", saying(1))
    stub_loaders(
        monkeypatch,
        body(wav),
        soul(provider="mock", chat={"provider": "openai", "key_env": "EMET_OPENAI_KEY"}),
    )

    rc = cli.main(["body.yaml", "soul.yaml", "--reply"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "EMET_OPENAI_KEY" in err and "will not run" in err


def test_a_false_wake_gets_no_reply(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "f.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock", chat={"provider": "mock"}))

    asyncio.run(cli._run(args(reply=True)))

    out = capsys.readouterr().out
    assert "said nothing the provider could make out" in out
    assert "reply:" not in out
