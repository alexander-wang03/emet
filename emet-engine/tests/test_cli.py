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

import pytest

from emet_sdk.types import Transcript

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
        "speak": False,
        "keys": None,
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
    assert "in the body" in err


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


# ------------------------------------------------------------------ --keys


def test_keys_are_read_from_a_file_before_the_providers_start(tmp_path, monkeypatch, capsys):
    """The BYOK noun: a key file, loaded once, names printed and values not."""
    monkeypatch.delenv("EMET_TEST_KEY", raising=False)
    keys = tmp_path / "keys.env"
    keys.write_text("EMET_TEST_KEY=hunter2\n", encoding="utf-8")
    wav = write_wav(tmp_path / "k.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())

    rc = cli.main(["body.yaml", "soul.yaml", "--keys", str(keys)])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"  keys     EMET_TEST_KEY from {keys}" in out
    assert "hunter2" not in out
    import os

    assert os.environ["EMET_TEST_KEY"] == "hunter2"


def test_the_suite_never_sees_a_developers_keys_file():
    """The guard in conftest.py. Without it, a laptop holding real keys turns
    every 'no key' test into a live call to a vendor."""
    from emet_engine import keys

    assert keys.default_paths() == []


def test_a_missing_keys_file_stops_the_run_with_its_path(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "m.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())

    rc = cli.main(["body.yaml", "soul.yaml", "--keys", str(tmp_path / "absent.env")])

    err = capsys.readouterr().err
    assert rc == 1
    assert "absent.env" in err and "no keys file" in err


def test_without_keys_and_without_default_files_the_header_says_nothing_about_keys(
    tmp_path, monkeypatch, capsys
):
    from emet_engine import keys as keys_module

    monkeypatch.setattr(keys_module, "default_paths", lambda: [tmp_path / "none.env"])
    monkeypatch.delenv("EMET_KEYS", raising=False)
    wav = write_wav(tmp_path / "q.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())

    asyncio.run(cli._run(args(keys=None)))

    assert "  keys " not in capsys.readouterr().out


# ------------------------------------------------------------------ --speak


def talking_soul() -> dict:
    return soul(provider="mock", chat={"provider": "mock"}) | {"models": {"stt": {"provider": "mock"}, "chat": {"provider": "mock"}, "tts": {"provider": "mock"}}}


def test_speak_says_the_reply_and_implies_reply_and_transcribe(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "s.wav", spoken(b"what", b"time", b"is it"))
    stub_loaders(monkeypatch, body(wav), talking_soul())

    rc = asyncio.run(cli._run(args(speak=True)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "  stt      mock (streaming)" in out
    assert "  llm      mock" in out
    assert "  voice    mock (16000 Hz)" in out
    assert "    reply: You said: what time is it" in out
    assert "    spoke 1 sentence(s)" in out
    assert "voice first" in out and "voice done" in out


def test_speak_writes_what_was_said_through_the_wav_sink(tmp_path, monkeypatch, capsys):
    """The whole 0.4 path on a laptop: wake, words, reply, voice, sink."""
    wav = write_wav(tmp_path / "w.wav", spoken(b"hello", b"there"))
    manifest = body(wav)
    out = str(tmp_path / "said.wav")
    manifest["audio"]["output"] = {"sink": "wav", "device": "none", "params": {"path": out}}
    stub_loaders(monkeypatch, manifest, talking_soul())

    rc = asyncio.run(cli._run(args(speak=True)))
    assert rc == 0
    from emet_providers.mock import read_back

    with wave.open(out, "rb") as w:
        assert w.getframerate() == 16000
        assert read_back(w.readframes(w.getnframes())) == "You said: hello there"


def test_a_lost_sentence_is_printed_with_its_reason(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "l.wav", spoken(b"hello", b"there"))
    manifest = body(wav)
    manifest["models"] = {
        "chat": {"provider": "mock", "params": {"reply": "Fine. The POISON word. Fine again."}},
        "tts": {"provider": "mock", "params": {"fail_on": "POISON"}},
    }
    stub_loaders(monkeypatch, manifest, talking_soul())

    asyncio.run(cli._run(args(speak=True)))

    out = capsys.readouterr().out
    assert "    spoke 2 sentence(s)" in out
    assert "the voice could not say 'The POISON word.'" in out


def test_speak_with_no_voice_configured_names_the_field(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "n.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock", chat={"provider": "mock"}))

    rc = cli.main(["body.yaml", "soul.yaml", "--speak"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "models.tts.provider" in err


def test_speak_with_the_reference_voice_and_nothing_installed_says_what_to_do(tmp_path, monkeypatch, capsys):
    """The reference soul names piper. Without the extra, or without the
    voice model, the run must stop before the microphone opens and say
    which command fixes it."""
    import sys

    monkeypatch.setitem(sys.modules, "piper", None)
    wav = write_wav(tmp_path / "p.wav", saying(1))
    doc = talking_soul()
    doc["models"]["tts"] = {"provider": "piper", "model": "en_US-ljspeech-medium"}
    stub_loaders(monkeypatch, body(wav), doc)

    rc = cli.main(["body.yaml", "soul.yaml", "--speak"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "will not run" in err and "emet-providers[piper]" in err


def test_speak_with_the_cloud_voice_and_no_key_names_the_variable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EMET_DEEPGRAM_KEY", raising=False)
    wav = write_wav(tmp_path / "k.wav", saying(1))
    doc = talking_soul()
    doc["models"]["tts"] = {"provider": "deepgram", "key_env": "EMET_DEEPGRAM_KEY"}
    stub_loaders(monkeypatch, body(wav), doc)

    rc = cli.main(["body.yaml", "soul.yaml", "--speak"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "EMET_DEEPGRAM_KEY" in err and "will not run" in err


def test_echo_and_speak_exclude_each_other(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["body.yaml", "soul.yaml", "--echo", "--speak"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_a_false_wake_is_not_spoken_to(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "f.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), talking_soul())

    asyncio.run(cli._run(args(speak=True)))

    out = capsys.readouterr().out
    assert "spoke" not in out and "reply:" not in out


def footer_session(**overrides):
    fields = {"source_name": "wav", "dropped": 0, "underflows": 0, "starved": 0}
    fields.update(overrides)
    return type("Session", (), {**fields, "warm_start_hint": lambda self: None})()


def test_the_footer_reports_late_callbacks_and_a_voice_that_fell_behind(capsys):
    """Two silences with different causes, and the footer used to blame one
    counter for both. PortAudio reports a late callback when this process
    missed the card's deadline. It reports nothing at all when the queue ran
    dry, because it was served on time with silence, so the engine counts
    that itself."""
    cli.print_run_footer(footer_session(underflows=3, starved=7), stats=False, busy=True)

    out = capsys.readouterr().out
    assert "fell behind real time 7 time(s)" in out and "inside a word" in out
    assert "3 late callback(s)" in out


def test_a_clean_run_says_nothing_about_either(capsys):
    cli.print_run_footer(footer_session(), stats=False, busy=True)

    out = capsys.readouterr().out
    assert "late callback" not in out and "fell behind" not in out


def test_transcribe_says_when_the_words_never_arrived(tmp_path, monkeypatch, capsys):
    """`emet-listen --transcribe` used to report a dead speech service as
    "said nothing the provider could make out", which is a sentence about
    the person when the fault is the network's."""
    wav = write_wav(tmp_path / "gone.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul(provider="mock"))

    real_start = ListenSession.start

    async def start_then_cut_the_network(self):
        await real_start(self)

        async def finish():
            return Transcript(text="", final=True, error="OSError: name resolution failed")

        self._stt.finish = finish

    monkeypatch.setattr(ListenSession, "start", start_then_cut_the_network)

    rc = asyncio.run(cli._run(args(transcribe=True)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "the words never arrived: OSError" in out
    assert "said nothing the provider could make out" not in out
