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
import sys
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
        "chains": None,
        "state": None,
        "explain": False,
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


def test_the_sentence_count_leaves_out_a_filler_for_a_tag(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "f.wav", spoken(b"hello", b"there"))
    manifest = body(wav)
    manifest["models"] = {"chat": {"provider": "mock", "params": {"reply": "[express.curiosity] Well. That is odd."}}}
    stub_loaders(monkeypatch, manifest, talking_soul())

    asyncio.run(cli._run(args(speak=True)))

    out = capsys.readouterr().out
    assert "    reply: Well. That is odd." in out
    assert "    spoke 2 sentence(s)" in out


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


def test_on_a_terminal_the_caption_is_redrawn_in_place(capsys):
    """Whole-text partials were for this: each one replaces the line."""
    import io

    screen = io.StringIO()
    caption = cli.Caption(screen, tty=True)
    for text in ("what", "what time", "what time is it"):
        caption.show(Transcript(text=text))
    caption.close()
    drawn = screen.getvalue()
    assert drawn.count("\r") == 3 and drawn.count("\n") == 1, "one line, drawn three times"
    assert drawn.split("\r")[-1] == "    hearing 'what time is it'\n"


def test_a_shorter_partial_blanks_what_the_longer_one_left_behind():
    import io

    screen = io.StringIO()
    caption = cli.Caption(screen, tty=True)
    caption.show(Transcript(text="what time is it"))
    caption.show(Transcript(text="what"))
    last = screen.getvalue().split("\r")[-1]
    assert last.rstrip() == "    hearing 'what'" and len(last) == len("    hearing 'what time is it'")


def test_written_to_a_file_each_partial_keeps_its_own_line():
    import io

    log = io.StringIO()
    caption = cli.Caption(log, tty=False)
    caption.show(Transcript(text="what"))
    caption.show(Transcript(text="what time"))
    caption.close()
    assert log.getvalue() == "    hearing 'what'\n    hearing 'what time'\n"


def test_the_header_names_the_body_and_the_state_file(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "h.wav", saying(1))
    manifest = body(wav)
    manifest["body"] = {"id": "cli_body", "name": "a laptop pretending"}
    stub_loaders(monkeypatch, manifest, soul())

    asyncio.run(cli._run(args()))

    out = capsys.readouterr().out
    assert "  body     a laptop pretending (cli_body)" in out
    assert "  chains   31 resolved at boot" in out
    assert "cli_body.json" in out
    assert "  self " not in out, "without a language model nobody reads the self-model"


def test_explain_without_a_language_model_prints_the_self_model(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "x.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())

    asyncio.run(cli._run(args(explain=True)))

    out = capsys.readouterr().out
    assert "SELF-MODEL" in out and "cannot come to you" in out


def test_a_run_that_learned_something_keeps_it_and_says_where(tmp_path, monkeypatch, capsys):
    """A live run keeps what the wake engine learned. The file here stands in
    for a microphone, so the session is told to carry as a live one would."""
    import functools

    wav = write_wav(tmp_path / "k.wav", saying(1))
    manifest = body(wav)
    manifest["body"] = {"id": "keeps"}
    manifest["audio"]["wake"]["params"] = {"carry_over": {"cmninit": "1,2,3"}}
    stub_loaders(monkeypatch, manifest, soul())
    monkeypatch.setattr(cli, "ListenSession", functools.partial(ListenSession, carry=True))
    path = tmp_path / "keeps.json"

    asyncio.run(cli._run(args(state=str(path))))

    out = capsys.readouterr().out
    assert f"state: kept wake.mock (cmninit) for the next boot on this body, in {path}" in out
    assert "put it under audio.wake.params.cmninit" not in out.lower()


def test_a_replay_carries_nothing_in_or_out(tmp_path, monkeypatch, capsys):
    """A replay reproduces a run exactly. The mean a live run left is not
    handed to the detector, and the mean the recording taught it is not
    kept as this body's: the recording is somebody's room, maybe this one's
    on another day, and replaying it twice has to give the same answer."""
    import json

    wav = write_wav(tmp_path / "r.wav", saying(1))
    manifest = body(wav)
    manifest["body"] = {"id": "replayed"}
    manifest["audio"]["wake"]["params"] = {"cmninit": "manual", "carry_over": {"cmninit": "from-the-recording"}}
    stub_loaders(monkeypatch, manifest, soul())
    path = tmp_path / "replayed.json"
    path.write_text(json.dumps({"state_version": "0.1", "body_id": "replayed", "carry": {"wake.mock": {"cmninit": "live"}}}), encoding="utf-8")
    seen: list[dict] = []
    real_start = ListenSession.start

    async def start(self):
        await real_start(self)
        seen.append(dict(self._wake.params))

    monkeypatch.setattr(ListenSession, "start", start)

    asyncio.run(cli._run(args(state=str(path), replay=wav)))

    out = capsys.readouterr().out
    assert seen[0]["cmninit"] == "manual", "the live mean was not handed to a replay"
    assert json.loads(path.read_text(encoding="utf-8"))["carry"] == {"wake.mock": {"cmninit": "live"}}
    assert "nothing carried in or out" in out and "state: kept" not in out


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


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows' event loop takes no signal handlers")
@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_sigterm_and_sighup_end_the_run_the_way_ctrl_c_does(name):
    """An SSH session that drops with the hotspot sends SIGHUP; `systemctl
    stop` sends SIGTERM. Either one cancels the run, so its footer is printed,
    what it learned is kept, and its parts are put to rest."""
    import os
    import signal

    async def run_until_signalled():
        cli.install_stop_signals()
        os.kill(os.getpid(), getattr(signal, name))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return "cancelled"
        return "slept"

    assert asyncio.run(run_until_signalled()) == "cancelled"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows has no SIGHUP")
def test_a_run_started_under_nohup_outlives_the_hangup():
    """`nohup emet-talk ... &` asked for a run that survives the SSH session
    dropping with the hotspot. A hangup that was ignored stays ignored."""
    import os
    import signal

    async def run_through_a_hangup():
        cli.install_stop_signals()
        os.kill(os.getpid(), signal.SIGHUP)
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return "cancelled"
        return "slept"

    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        assert asyncio.run(run_through_a_hangup()) == "slept"
    finally:
        signal.signal(signal.SIGHUP, before)


def test_a_cancel_during_boot_ends_the_run_the_way_ctrl_c_does(tmp_path, monkeypatch, capsys):
    """SIGTERM and SIGHUP cancel the task. One that lands while the body boots
    (a voice's preflight over a slow hotspot) used to escape `main()` as a
    traceback, where Ctrl-C at the same moment printed "stopped."."""
    wav = write_wav(tmp_path / "boot.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())
    stopped: list[bool] = []
    real_start, real_stop = ListenSession.start, ListenSession.stop

    async def start(self):
        await real_start(self)
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    async def stop(self):
        await real_stop(self)
        stopped.append(True)

    monkeypatch.setattr(ListenSession, "start", start)
    monkeypatch.setattr(ListenSession, "stop", stop)

    assert cli.main(["body.yaml", "soul.yaml"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("stopped.")
    assert stopped == [True], "the parts were put to rest"


def test_a_replay_offers_no_mean_to_paste():
    """The header of a replay says nothing is carried in or out, and the
    footer used to go on to offer the recording's mean for the manifest."""
    replayed = type("Session", (), {
        "source_name": "wav", "dropped": 0, "underflows": 0, "starved": 0, "carry": False,
        "warm_start_hint": lambda self: "warm start: put it under audio.wake.params.cmninit",
    })()
    live = type(replayed)()
    live.carry = True
    import contextlib
    import io

    printed = []
    for session in (replayed, live):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_run_footer(session, stats=False, busy=False)
        printed.append(out.getvalue())
    assert "warm start" not in printed[0]
    assert "warm start" in printed[1], "a live run with nowhere to keep it is still asked"


def test_installing_the_stop_signals_is_harmless_where_there_are_none():
    async def install():
        cli.install_stop_signals()
        return "ok"

    assert asyncio.run(install()) == "ok"


def test_the_header_prints_the_speakers_own_latency_when_it_has_one(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "l.wav", saying(1))
    stub_loaders(monkeypatch, body(wav), soul())
    real_start = ListenSession.start

    async def start(self):
        await real_start(self)
        self._sink.stream_latency_s = 0.085

    monkeypatch.setattr(ListenSession, "start", start)
    asyncio.run(cli._run(args()))
    assert "  output   85 ms of stream latency, as the speaker reports it" in capsys.readouterr().out


def test_a_warning_from_the_state_file_is_printed_even_when_something_was_kept(tmp_path, monkeypatch, capsys):
    """One plugin carried something JSON cannot hold, another carried a plain
    value. The good one is kept and said to be kept; the bad one is not
    claimed, and the reason is printed."""
    import functools

    from emet_hal import mock

    wav = write_wav(tmp_path / "w.wav", saying(1))
    manifest = body(wav)
    manifest["body"] = {"id": "warns"}
    manifest["audio"]["wake"]["params"] = {"carry_over": {"cmninit": "1,2,3"}}
    manifest["capabilities"] = [{"id": "eyes", "type": "display", "role": "eyes", "driver": {"plugin": "emet_hal.mock"}}]
    stub_loaders(monkeypatch, manifest, soul())
    monkeypatch.setattr(mock.MockActuator, "carry_over", lambda self: {"blob": b"bytes"})
    monkeypatch.setattr(cli, "ListenSession", functools.partial(ListenSession, carry=True))

    asyncio.run(cli._run(args(state=str(tmp_path / "warns.json"))))

    out = capsys.readouterr().out
    assert "state: kept wake.mock (cmninit) for the next boot" in out
    assert "capability.eyes" not in out.split("state: kept", 1)[1].split("\n", 1)[0]
    assert "warning: capability.eyes gave a value that cannot be written down" in out
