"""`emet-talk`, the 0.4 gate as one command, driven offline.

The mocked example body and the mocked stages: a wav for a microphone, the
mock detector, the mock transcriber reading words out of the bytes, the mock
language model repeating them, the mock voice spelling them into the audio,
and a wav sink that can be read back. Nothing here needs a key, a network,
or a sound card, and the whole loop runs through the same entry points the
robot uses.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from emet_sdk.types import Transcript

from emet_engine import talk
from emet_engine.session import ListenSession

PHRASE = "hey emet"
FRAME = 1280


def write_wav(path: Path, pcm: bytes) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return str(path)


def frame(payload: bytes = b"") -> bytes:
    return payload + bytes(2 * FRAME - len(payload))


def spoken(*words: bytes) -> bytes:
    return frame() + frame(PHRASE.encode()) + b"".join(frame(w) for w in words) + frame() * 20


def false_wake() -> bytes:
    return frame() + frame(PHRASE.encode()) + frame() * 50


def body(path: str, out: str | None = None) -> dict:
    output = {"sink": "null", "device": "none"}
    if out:
        output = {"sink": "wav", "device": "none", "params": {"path": out}}
    return {
        "audio": {
            "input": {"source": "wav", "device": "file", "params": {"path": path}},
            "output": output,
            "wake": {"engine": "mock", "params": {}},
        },
        "models": {"stt": {"provider": "mock"}, "chat": {"provider": "mock"}, "tts": {"provider": "mock"}},
    }


def soul(**extra) -> dict:
    doc = {"identity": {"name": "Emet", "wake_word": PHRASE}}
    doc.update(extra)
    return doc


def stub_loaders(monkeypatch, manifest: dict, soul_doc: dict) -> None:
    monkeypatch.setattr(
        talk, "_load", lambda path, kind, registry: manifest if kind == "manifest" else soul_doc
    )


def test_it_talks(tmp_path, monkeypatch, capsys):
    """The gate. Wake, words, answer, voice, speaker, from the documents and
    nothing else. The wav the sink wrote reads back as the reply."""
    out = str(tmp_path / "said.wav")
    wav = write_wav(tmp_path / "talk.wav", spoken(b"what", b"time", b"is it"))
    stub_loaders(monkeypatch, body(wav, out), soul())

    rc = talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert rc == 0
    assert "Emet is listening for 'hey emet'" in text
    assert "  words    mock" in text and "  answers  mock" in text and "  voice    mock" in text
    assert "  you:  what time is it" in text
    assert "  emet: You said: what time is it" in text
    assert "source ended. 1 exchange(s)." in text
    from emet_providers.mock import read_back

    with wave.open(out, "rb") as w:
        assert read_back(w.readframes(w.getnframes())) == "You said: what time is it"


def test_two_exchanges_in_one_run_and_the_stats_at_the_end(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "two.wav", spoken(b"first", b"one") + spoken(b"second", b"two"))
    stub_loaders(monkeypatch, body(wav), soul())

    rc = talk.main(["body.yaml", "soul.yaml", "--stats"])

    text = capsys.readouterr().out
    assert rc == 0
    assert "  you:  first one" in text and "  you:  second two" in text
    assert "2 exchange(s)" in text
    assert "voice first" in text and "verdict" in text


def test_a_false_wake_is_reported_and_not_answered(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "false.wav", false_wake())
    stub_loaders(monkeypatch, body(wav), soul())

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "(then nothing)" in text
    assert "you:" not in text and "emet:" not in text


def test_the_souls_line_for_a_false_wake_is_spoken_when_it_has_one(tmp_path, monkeypatch, capsys):
    out = str(tmp_path / "hm.wav")
    wav = write_wav(tmp_path / "false2.wav", false_wake())
    stub_loaders(monkeypatch, body(wav, out), soul(persona={"lines": {"nothing_heard": "Yes?"}}))

    talk.main(["body.yaml", "soul.yaml"])

    assert "  emet: Yes?" in capsys.readouterr().out
    from emet_providers.mock import read_back

    with wave.open(out, "rb") as w:
        assert read_back(w.readframes(w.getnframes())) == "Yes?"


def test_a_failed_reply_gets_the_souls_words_for_failure(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "fail.wav", spoken(b"hello", b"there"))
    manifest = body(wav)
    manifest["models"]["chat"] = {"provider": "mock"}
    stub_loaders(monkeypatch, manifest, soul(persona={"lines": {"failed": "Lost it. Again?"}}))

    real_start = ListenSession.start

    async def start_then_break_the_model(self):
        await real_start(self)
        self._llm._started = False  # the vendor went away after boot

    monkeypatch.setattr(ListenSession, "start", start_then_break_the_model)

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "(the language model failed:" in text
    assert "  emet: Lost it. Again?" in text


def test_a_missing_stage_names_the_field(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "n.wav", spoken(b"hi"))
    manifest = body(wav)
    del manifest["models"]["tts"]
    stub_loaders(monkeypatch, manifest, soul())

    rc = talk.main(["body.yaml", "soul.yaml"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "models.tts.provider" in err


def test_a_reference_voice_with_nothing_installed_says_what_to_do(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "piper", None)
    wav = write_wav(tmp_path / "p.wav", spoken(b"hi"))
    manifest = body(wav)
    manifest["models"]["tts"] = {"provider": "piper"}
    stub_loaders(monkeypatch, manifest, soul())

    rc = talk.main(["body.yaml", "soul.yaml"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "emet-providers[piper]" in err and "will not run" in err


def test_the_trailing_clause_is_announced(tmp_path, monkeypatch, capsys):
    """A transcript that ends on a word no sentence ends on gets one more
    window, and the console says so."""
    wav = write_wav(tmp_path / "t.wav", spoken(b"tell me", b"about the") + frame() * 20)
    stub_loaders(monkeypatch, body(wav), soul(interaction={"extend_on_incomplete": True}))

    rc = talk.main(["body.yaml", "soul.yaml", "--stats"])

    text = capsys.readouterr().out
    assert rc == 0
    assert "extends once on a trailing clause" in text
    assert "(that sounded unfinished; waiting a little longer)" in text
    assert "extended 1" in text


def test_intents_the_model_tagged_are_listed_and_acted_on_and_never_read_aloud(tmp_path, monkeypatch, capsys):
    """The tag's name is never said; what the body does with it is. On a
    bodiless body curiosity is "hm?" in the robot's own voice."""
    out = str(tmp_path / "tagged.wav")
    wav = write_wav(tmp_path / "i.wav", spoken(b"hello"))
    manifest = body(wav, out)
    manifest["models"]["chat"] = {"provider": "mock", "params": {"reply": "[express.curiosity] Hello! [attend.speaker] Tell me more."}}
    stub_loaders(monkeypatch, manifest, soul())

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "  emet: Hello! Tell me more." in text
    assert "(intents: express.curiosity, attend.speaker)" in text
    from emet_providers.mock import read_back

    with wave.open(out, "rb") as w:
        assert read_back(w.readframes(w.getnframes())) == "hm? Hello! Tell me more."


def test_an_interrupted_run_still_reports(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "long.wav", spoken(b"one") + spoken(b"two"))
    stub_loaders(monkeypatch, body(wav), soul())
    real_talk = ListenSession.talk

    async def talk_then_ctrl_c(self, **kw):
        async for exchange in real_talk(self, **kw):
            yield exchange
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    monkeypatch.setattr(ListenSession, "talk", talk_then_ctrl_c)

    rc = talk.main(["body.yaml", "soul.yaml", "--stats"])

    text = capsys.readouterr().out
    assert rc == 0
    assert "stopped. 1 exchange(s)." in text and "verdict" in text


def test_a_cancel_during_boot_ends_the_run_the_way_ctrl_c_does(tmp_path, monkeypatch, capsys):
    """SIGTERM or SIGHUP while the voice runs its preflight: "stopped.", a
    clean exit, and every part put to rest, as Ctrl-C at that moment."""
    wav = write_wav(tmp_path / "boot.wav", spoken(b"hello"))
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

    assert talk.main(["body.yaml", "soul.yaml"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("stopped.")
    assert stopped == [True]


def test_help_names_no_stage_flags():
    with pytest.raises(SystemExit) as exc:
        talk.main(["--help"])
    assert exc.value.code == 0


def test_the_header_names_the_body_and_prints_the_binding_table(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "hdr.wav", false_wake())
    manifest = body(wav)
    manifest["body"] = {"id": "talk_body", "name": "a test rig", "scale": "desk", "power": "plugged_in"}
    stub_loaders(monkeypatch, manifest, soul())

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "  body     a test rig (talk_body)" in text
    assert "  chains   31 resolved at boot against 0 part(s): 0 to hardware, 31 to voice" in text
    assert "voice  utter" in text and "speak" in text
    assert "  state    " in text and "talk_body.json" in text
    assert "  self     " in text


def test_explain_prints_the_system_prompt_with_the_body_in_it(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "exp.wav", false_wake())
    stub_loaders(monkeypatch, body(wav), soul())

    talk.main(["body.yaml", "soul.yaml", "--explain"])

    text = capsys.readouterr().out
    assert "SYSTEM PROMPT" in text and "YOUR BODY" in text
    assert "cannot come to you" in text
    assert "[express.curiosity]" in text


def test_a_transcriber_that_lost_the_network_gets_the_souls_words(tmp_path, monkeypatch, capsys):
    """The Pi with mobile data off: the wake fired, the words never came, and
    0.4 answered with silence because an empty final looked like a cough.
    Somebody spoke, so the robot says its failure line out loud."""
    out = str(tmp_path / "lost.wav")
    wav = write_wav(tmp_path / "lost-in.wav", spoken(b"hello", b"there"))
    stub_loaders(monkeypatch, body(wav, out), soul(persona={"lines": {"failed": "I lost the thread there."}}))

    real_start = ListenSession.start

    async def start_then_cut_the_network(self):
        await real_start(self)

        async def finish():
            return Transcript(
                text="",
                final=True,
                error="OSError: [Errno -3] Temporary failure in name resolution",
            )

        self._stt.finish = finish

    monkeypatch.setattr(ListenSession, "start", start_then_cut_the_network)

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "(the words never arrived: OSError" in text
    assert "  emet: I lost the thread there." in text
    from emet_providers.mock import read_back

    with wave.open(out, "rb") as w:
        assert read_back(w.readframes(w.getnframes())) == "I lost the thread there."


def test_a_false_wake_does_not_get_the_failure_line(tmp_path, monkeypatch, capsys):
    """The same branch, the other way round. Break the distinction and this
    fails: a soul with words for a failure would use them on every cough."""
    wav = write_wav(tmp_path / "quiet.wav", false_wake())
    stub_loaders(monkeypatch, body(wav), soul(persona={"lines": {"failed": "I lost the thread there."}}))

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "(then nothing)" in text
    assert "I lost the thread there." not in text


def test_words_that_arrived_cut_short_are_answered_with_a_caveat(tmp_path, monkeypatch, capsys):
    """Half a question is still a question. The robot answers it and says the
    words may be short rather than throwing them away."""
    wav = write_wav(tmp_path / "cut.wav", spoken(b"hello", b"there"))
    stub_loaders(monkeypatch, body(wav), soul())

    real_start = ListenSession.start

    async def start_then_cut_the_final(self):
        await real_start(self)

        async def finish():
            return Transcript(text="what time is", final=True, error="no final within 5 s")

        self._stt.finish = finish

    monkeypatch.setattr(ListenSession, "start", start_then_cut_the_final)

    talk.main(["body.yaml", "soul.yaml"])

    text = capsys.readouterr().out
    assert "  you:  what time is" in text
    assert "  emet: You said: what time is" in text
    assert "(those words may be cut short: no final within 5 s)" in text
