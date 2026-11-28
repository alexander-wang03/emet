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
import subprocess
import wave
from pathlib import Path

import pytest

from emet_sdk.types import Transcript

from emet_engine import board, cli, talk
from emet_engine.session import ListenSession

PHRASE = "hey emet"
FRAME = 1280


@pytest.fixture(autouse=True)
def no_real_board_or_git(monkeypatch):
    """No test reads this machine's board or asks its git: a tree that
    matches its commit, and a board with neither reader, unless the test
    says otherwise."""
    status = "# branch.oid eb55748541483e9769610526bbaa21bfbe439f0b\n# branch.head master\n"
    monkeypatch.setattr(
        cli, "_run_git", lambda args, directory: subprocess.CompletedProcess(["git", *args], 0, status, "")
    )
    monkeypatch.setattr(board, "find_vcgencmd", lambda: None)
    monkeypatch.setattr(board, "load_average", lambda: None)


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


def test_stats_puts_each_wakes_cut_under_it(tmp_path, monkeypatch, capsys):
    """With `--stats`, emet-talk says what went to the transcriber with the
    name, in its own aside style; without it, nothing."""
    pcm = frame() + frame(PHRASE.encode()) + b"what".ljust(2 * FRAME, b" ") + frame(b"time") + frame() * 20
    manifest = body(write_wav(tmp_path / "s.wav", pcm))
    manifest["audio"]["wake"]["params"] = {"lag_frames": 1}
    stub_loaders(monkeypatch, manifest, soul())

    assert talk.main(["body.yaml", "soul.yaml", "--stats"]) == 0
    text = capsys.readouterr().out
    assert "  (heard 'hey emet')\n  (lag 80 ms, name cut 0 ms, level stop alone 0 ms)\n" in text

    assert talk.main(["body.yaml", "soul.yaml"]) == 0
    assert "(lag " not in capsys.readouterr().out


def test_stats_says_when_the_tail_was_handed_over_as_heard(tmp_path, monkeypatch, capsys):
    """A 720 ms lag, past `MAX_WAKE_LAG_MS`, hands over the last 640 ms as
    heard, and emet-talk's aside says so, as emet-listen's line does."""
    manifest = body(write_wav(tmp_path / "late.wav", spoken(*[b"what"] * 9)))
    manifest["audio"]["wake"]["params"] = {"lag_frames": 9}
    stub_loaders(monkeypatch, manifest, soul())

    assert talk.main(["body.yaml", "soul.yaml", "--stats"]) == 0
    aside = "(lag 720 ms, name cut 0 ms, level stop alone 0 ms; the last 640 ms handed over as heard)"
    assert f"  (heard 'hey emet')\n  {aside}\n" in capsys.readouterr().out


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
    assert "  chains   32 resolved at boot against 0 part(s): 0 to hardware, 32 to voice" in text
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


def test_the_first_line_names_the_tree_that_ran(tmp_path, monkeypatch, capsys):
    """emet-talk is what runs on the body, so its log starts with which tree
    ran, in the same shape as emet-listen's."""
    wav = write_wav(tmp_path / "id.wav", false_wake())
    stub_loaders(monkeypatch, body(wav), soul())
    monkeypatch.setattr(cli, "_version", lambda name: "0.5.5")

    talk.main(["body.yaml", "soul.yaml"])

    assert capsys.readouterr().out.splitlines()[0] == (
        "emet-talk: emet-sdk 0.5.5, emet-hal 0.5.5, emet-providers 0.5.5, emet-engine 0.5.5; "
        "revision eb5574854148, working tree matches"
    )


def test_stats_prints_the_board_at_the_start_and_the_end(tmp_path, monkeypatch, capsys):
    """Read once at boot, before the session starts, and once at the
    footer: the readers give two readings and fail on a third, and each
    reading records the phase of the run it was taken in."""
    wav = write_wav(tmp_path / "b.wav", spoken(b"what", b"time"))
    stub_loaders(monkeypatch, body(wav), soul())
    printed = iter(["temp=49.9'C\n", "throttled=0x0\n", "temp=50.4'C\n", "throttled=0x0\n"])
    loads = iter([(0.07, 0.06, 0.05), (0.2, 0.08, 0.06)])
    phase, phases = {"now": "boot"}, []
    real_start, real_footer = ListenSession.start, talk.print_run_footer

    async def start(self):
        phase["now"] = "running"
        await real_start(self)

    def footer(*a, **kw):
        phase["now"] = "footer"
        return real_footer(*a, **kw)

    def load_average():
        phases.append(phase["now"])
        return next(loads)

    monkeypatch.setattr(ListenSession, "start", start)
    monkeypatch.setattr(talk, "print_run_footer", footer)
    monkeypatch.setattr(board, "find_vcgencmd", lambda: lambda command: next(printed))
    monkeypatch.setattr(board, "load_average", load_average)

    assert talk.main(["body.yaml", "soul.yaml", "--stats"]) == 0

    rows = capsys.readouterr().out.splitlines()
    verdict = next(i for i, row in enumerate(rows) if row.startswith("  verdict "))
    assert rows[verdict - 3:verdict] == [
        "  board         at start: temp 49.9'C, throttled 0x0, load 0.07 0.06 0.05",
        "  board         at end:   temp 50.4'C, throttled 0x0, load 0.20 0.08 0.06",
        "  board         during the run: no throttle flag set",
    ]
    assert phases == ["boot", "footer"]


def test_the_identity_line_is_printed_before_discovery(monkeypatch, capsys):
    class NothingInstalled:
        @staticmethod
        def discover():
            print("discovering")
            return {}

    monkeypatch.setattr(talk, "PluginRegistry", NothingInstalled)

    assert talk.main(["body.yaml", "soul.yaml"]) == 2
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("emet-talk: emet-sdk ") and lines[1] == "discovering"


def test_without_stats_the_board_is_not_read(tmp_path, monkeypatch, capsys):
    wav = write_wav(tmp_path / "n.wav", false_wake())
    stub_loaders(monkeypatch, body(wav), soul())
    read: list[bool] = []
    monkeypatch.setattr(board, "load_average", lambda: read.append(True))

    talk.main(["body.yaml", "soul.yaml"])

    assert read == [] and "  board " not in capsys.readouterr().out


# ------------------------------------------------------------- 0.5.7's lines


def test_each_exchange_ends_with_its_turn_line_under_stats(tmp_path, monkeypatch, capsys):
    """TT8 and OC1: `  (turn gap ...)` closes each exchange, and `--events`
    writes the same turn beside it, with no word said."""
    import json

    wav = write_wav(tmp_path / "two.wav", spoken(b"first", b"one") + spoken(b"second", b"two"))
    stub_loaders(monkeypatch, body(wav), soul())
    path = tmp_path / "talk.jsonl"

    rc = talk.main(["body.yaml", "soul.yaml", "--stats", "--events", str(path)])

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    turns = [i for i, line in enumerate(out) if line.startswith("  (turn gap ")]
    assert len(turns) == 2
    assert all(out[i - 1].startswith("  emet: ") for i in turns)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # A5's end-to-end check of 0.5.7: every record emet-talk writes is one
    # the shipped schema holds.
    from jsonschema import Draft202012Validator

    from emet_engine.events import SCHEMA

    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    for record in records:
        validator.validate(record)
    assert records[0]["program"] == "emet-talk"
    assert [r.get("kind") for r in records[1:]].count("turn") == 2
    assert records[-1]["kind"] == "footer"
    assert "first one" not in path.read_text(encoding="utf-8")


def test_events_without_stats_is_refused_with_exit_2(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert talk.main(["body.yaml", "soul.yaml", "--events", "run.jsonl"]) == 2
    assert capsys.readouterr().err == "emet-talk: --events writes what --stats prints; add --stats\n"
    assert list(tmp_path.iterdir()) == [], "refused before the file is opened"


def test_a_declined_reply_prints_no_empty_speaker_tag(tmp_path, monkeypatch, capsys):
    """L16: the speaker's tag is printed with the reply's first word, so a
    reply with none leaves no `  emet: ` line standing empty."""
    from emet_providers.mock import MockLanguageModel
    from emet_sdk.types import ReplyDone

    async def reply(self, prompt):
        yield ReplyDone(text="", stop_reason="refusal", model="mock")

    monkeypatch.setattr(MockLanguageModel, "reply", reply)
    wav = write_wav(tmp_path / "d.wav", spoken(b"hello"))
    stub_loaders(monkeypatch, body(wav), soul(persona={"lines": {"declined": "Not that one."}}))

    talk.main(["body.yaml", "soul.yaml"])

    out = capsys.readouterr().out.splitlines()
    assert "  emet: " not in out
    said = out.index("  you:  hello")
    assert out[said + 1 : said + 3] == ["  (the provider declined)", "  emet: Not that one."]


def test_the_speaker_tag_waits_for_the_first_word():
    import io
    from contextlib import redirect_stdout

    screen = io.StringIO()
    with redirect_stdout(screen):
        speaker = talk.Speaker("  emet: ")
        speaker.said("hello")
        speaker.delta("")
        speaker.close()
        speaker.delta("Hi")
        speaker.delta(" there.")
        speaker.close()
        speaker.close()
    assert screen.getvalue() == "  you:  hello\n  emet: Hi there.\n"


class _Terminal:
    """A stream that says it is a terminal, writing into a journal it
    shares with its partner, each write with the stream's name."""

    def __init__(self, name: str, journal: list[tuple[str, str]]) -> None:
        self.name = name
        self.journal = journal

    def write(self, text: str) -> int:
        self.journal.append((self.name, text))
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def test_stats_at_a_terminal_draws_the_live_line_and_guards_stderr(tmp_path, monkeypatch):
    """BVB3 stage 1 gives the live line to emet-talk as well: under --stats
    at a terminal it is drawn, a write to stderr while it is drawn clears it
    first, and both streams are the program's own again at exit."""
    wav = write_wav(tmp_path / "l.wav", spoken(b"hello"))
    stub_loaders(monkeypatch, body(wav), soul())
    journal: list[tuple[str, str]] = []
    screen, errors = _Terminal("out", journal), _Terminal("err", journal)
    monkeypatch.setattr("sys.stdout", screen)
    monkeypatch.setattr("sys.stderr", errors)
    real_start = ListenSession.start
    warned: list[bool] = []

    async def start(self):
        drawn = self.on_frame

        def frame(level, speech, quiet):
            drawn(level, speech, quiet)
            if not warned:
                warned.append(True)
                import sys

                print("  ! a warning in the middle of the run", file=sys.stderr)

        self.on_frame = frame
        await real_start(self)

    monkeypatch.setattr(ListenSession, "start", start)

    assert talk.main(["body.yaml", "soul.yaml", "--stats"]) == 0

    import sys

    assert sys.stdout is screen and sys.stderr is errors
    out = [text for name, text in journal if name == "out"]
    assert any(text.startswith("\r    ears ") for text in out), "the line was never drawn"
    at = journal.index(("err", "  ! a warning in the middle of the run"))
    before = [text for name, text in journal[:at] if name == "out"]
    assert before[-1] == "\r\x1b[K", "the warning landed on the drawn line"


def test_a_replay_file_that_will_not_open_is_refused_before_the_boot_tone(tmp_path, monkeypatch, capsys):
    """The refusal comes before the robot makes a sound: the file is opened
    before the rising pair plays, so the sink is handed nothing."""
    out = tmp_path / "said.wav"
    stub_loaders(monkeypatch, body(str(tmp_path / "nothere.wav"), str(out)), soul())

    assert talk.main(["body.yaml", "soul.yaml"]) == 1
    err = capsys.readouterr().err
    assert "the audio source 'wav' cannot start, so the robot would listen to nothing. It will not run." in err
    if out.exists():
        with wave.open(str(out), "rb") as w:
            assert w.getnframes() == 0, "the boot tone reached the sink before the refusal"


UNAVAILABLE = {"provider": "mock", "params": {
    "fail_on_start": True, "fault": "model_unavailable", "fault_detail": "The mock was told its model is retired."}}


def test_a_false_wake_is_silent_while_the_model_cannot_answer(tmp_path, monkeypatch, capsys):
    """Review finding engine-console/code#1, settled by the rule that a false
    wake is silent unless `persona.lines.nothing_heard` says otherwise: with
    the chat model unavailable, a wake with nobody speaking says nothing,
    where it said the unavailable line aloud (contract 0.5.7 section 9)."""
    out = str(tmp_path / "said.wav")
    manifest = body(write_wav(tmp_path / "f.wav", false_wake()), out)
    manifest["models"]["chat"] = UNAVAILABLE
    stub_loaders(monkeypatch, manifest, soul())

    assert talk.main(["body.yaml", "soul.yaml"]) == 0
    text = capsys.readouterr().out
    assert "cannot answer; nothing was sent" not in text
    assert "The part of me that thinks" not in text
    from emet_providers.mock import read_back

    with wave.open(out, "rb") as w:
        assert "The part of me that thinks" not in read_back(w.readframes(w.getnframes()))


def test_a_question_while_the_model_cannot_answer_still_gets_the_line(tmp_path, monkeypatch, capsys):
    """The other half of engine-console/code#1: a wake with speech still gets
    the unavailable line at the end of its turn (C5 (a))."""
    manifest = body(write_wav(tmp_path / "q.wav", spoken(b"what", b"time", b"is it")))
    manifest["models"]["chat"] = UNAVAILABLE
    stub_loaders(monkeypatch, manifest, soul())

    assert talk.main(["body.yaml", "soul.yaml"]) == 0
    text = capsys.readouterr().out
    assert "  (the language model cannot answer; nothing was sent to the transcriber or the model)" in text
    assert "  emet: The part of me that thinks has been switched off. My owner needs to choose another." in text
