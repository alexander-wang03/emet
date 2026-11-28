"""tools/events_file.py, the reader of the events file `--events PATH`
writes: what it refuses, what it skips, and what it sums and prints."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import events_file  # noqa: E402  a tool, imported by its bare name from tools/

HEADER = {"format": "emet-events", "version": "1.0", "program": "emet-talk",
          "packages": {"emet-sdk": "0.5.7", "emet-hal": "0.5.7", "emet-providers": "0.5.7",
                       "emet-engine": "0.5.7"},
          "platform": "linux-aarch64", "python": "3.13.5"}
WAKE = {"kind": "wake", "wake": 1, "at_s": 4.2, "confidence": 1.0, "lag_ms": 170, "heard_ms": 170,
        "name_ms": 0, "level_ms": 0, "cut_short": False}


def turn(n: int, gap: int | None, *, tin: int | None = None, tout: int | None = None,
         audio: float = 0.0, stt: str | None = None, chars: int = 0, llm: str | None = None,
         voice_chars: int = 0, voice: str | None = None, outcome: str = "answered") -> dict:
    """A turn record with the contract's keys in the contract's order."""
    return {"kind": "turn", "turn": n, "at_s": 10.0 * n, "outcome": outcome, "gap_ms": gap,
            "tail_ms": 960, "final_ms": 410, "reply_first_ms": 620, "voice_first_ms": 890,
            "input_tokens": tin, "output_tokens": tout, "stt_audio_s": audio, "stt_host": stt,
            "llm_chars": chars, "llm_host": llm, "voice_chars": voice_chars, "voice_host": voice,
            "sentences": 2, "cleaned": 0}


FOOTER = {"kind": "footer", "at_s": 40.0, "frames": 500, "audio_s": 40.0, "wakes": 3, "turns": 3}

RUN = [HEADER, WAKE,
       turn(1, 2260, tin=967, tout=18, audio=3.3, stt="api.deepgram.com", chars=2071, llm="api.openai.com",
            voice_chars=57, voice="api.deepgram.com"),
       turn(2, None, outcome="failed", audio=1.25, stt="api.deepgram.com", chars=2100, llm="api.openai.com"),
       turn(3, 1800, tin=1000, tout=None, audio=2.0, stt="api.deepgram.com", chars=0, llm="api.openai.com",
            voice_chars=40, voice=None),
       FOOTER]


def write(path: Path, objs: list, extra: str = "") -> Path:
    path.write_text("".join(json.dumps(o, ensure_ascii=True) + "\n" for o in objs) + extra, encoding="utf-8")
    return path


def test_a_run_as_the_contract_writes_it_reads_back_whole(tmp_path):
    events = events_file.read(write(tmp_path / "run.jsonl", RUN))
    assert events.header == HEADER
    assert [r["kind"] for r in events.records] == ["wake", "turn", "turn", "turn", "footer"]
    assert [t["turn"] for t in events.turns] == [1, 2, 3] and events.wakes == [WAKE]
    assert events.footer == FOOTER


def test_the_sums_are_the_footers_over_the_turn_records(tmp_path):
    s = events_file.summary(events_file.read(write(tmp_path / "run.jsonl", RUN)))
    assert s["turns"] == 3
    assert s["gap"] == {"median_ms": 2030, "mean_ms": 2030, "max_ms": 2260, "n": 2}
    assert s["tokens"] == {"in": 1967, "out": 18, "n": 2}
    # Turn 1's voice sent its characters off the body. Turn 3's voice was
    # on the body (host None) and turn 3 sent no characters to the model, so
    # neither adds an entry or an amount.
    assert s["sent"] == [{"stage": "stt", "host": "api.deepgram.com", "audio_s": 6.55},
                         {"stage": "llm", "host": "api.openai.com", "chars": 4171},
                         {"stage": "voice", "host": "api.deepgram.com", "chars": 57}]


def test_no_gap_no_count_and_nothing_sent_read_as_none(tmp_path):
    events = events_file.read(write(tmp_path / "run.jsonl", [HEADER, turn(1, None, outcome="no_speech")]))
    assert events_file.summary(events) == {"turns": 1, "gap": None, "tokens": None, "sent": []}
    lines = events_file.summary_lines(events)
    assert "  turn 1  no_speech: gap - (nothing was spoken); nothing left the body" in lines
    assert "  sent      nothing left the body" in lines
    assert lines[-1] == "  (no footer: the run stopped before it printed one)"


@pytest.mark.parametrize("tin, tout, want, says, said", [
    (None, 18, {"in": None, "out": 18, "n": 1}, "  tokens    18 out   (1 turn(s) that counted them)", "18 tokens out"),
    (967, None, {"in": 967, "out": None, "n": 1}, "  tokens    967 in   (1 turn(s) that counted them)", "967 tokens in"),
], ids=["output-only", "input-only"])
def test_a_side_no_turn_counted_reads_as_none_and_is_left_out_of_the_lines(tmp_path, tin, tout, want, says, said):
    """As the engine's footer sums them, a side no turn counted is None and
    the lines name only the side counted (0.5.7's review, tools-runner
    tests#1)."""
    events = events_file.read(write(tmp_path / "run.jsonl", [HEADER, turn(1, 1500, tin=tin, tout=tout)]))
    assert events_file.summary(events)["tokens"] == want
    lines = events_file.summary_lines(events)
    assert says in lines, lines
    assert f"  turn 1  answered: gap 1500 ms; {said}; nothing left the body" in lines, lines


@pytest.mark.parametrize("header, says", [
    ({**HEADER, "format": "emet-turns"}, "is format 'emet-turns' version '1.0'"),
    ({**HEADER, "version": "2.0"}, "is format 'emet-events' version '2.0'"),
    ({**HEADER, "version": 1.0}, "is format 'emet-events' version 1.0"),
    ({**HEADER, "version": "one"}, "is format 'emet-events' version 'one'"),
    ({k: v for k, v in HEADER.items() if k != "version"}, "is format 'emet-events' version None"),
    (WAKE, "is format None version None"),
])
def test_another_format_or_major_is_refused_naming_the_file(tmp_path, header, says):
    path = write(tmp_path / "run.jsonl", [header, WAKE])
    with pytest.raises(events_file.EventsError) as caught:
        events_file.read(path)
    assert str(caught.value) == f"{path} {says}; this reader knows emet-events 1.x"


def test_a_minor_version_reads_and_its_new_keys_and_kinds_pass(tmp_path):
    """A minor version may add keys and kinds: the keys are left alone and
    a record of an unknown kind is skipped."""
    newer = {**HEADER, "version": "1.4", "host_name": "emet-pi"}
    later = [newer, {**WAKE, "snr_db": 12.5}, {"kind": "board", "temp_c": 51.0}, {"no": "kind"}, FOOTER]
    events = events_file.read(write(tmp_path / "run.jsonl", later))
    assert events.header["host_name"] == "emet-pi"
    assert [r["kind"] for r in events.records] == ["wake", "footer"]
    assert events.wakes[0]["snr_db"] == 12.5


@pytest.mark.parametrize("bad", ["[1, 2]", "not json at all", '"a string"', '{"kind": "turn", "turn": 1'])
def test_a_line_that_is_no_json_object_is_refused_with_its_number(tmp_path, bad):
    path = write(tmp_path / "run.jsonl", [HEADER, WAKE], extra=bad + "\n")
    with pytest.raises(events_file.EventsError) as caught:
        events_file.read(path)
    assert str(caught.value) == f"{path}:3: not a JSON object"


def test_a_line_that_is_not_utf8_is_refused_and_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "run.jsonl"
    path.write_bytes(b"\n" + json.dumps(HEADER).encode() + b"\r\n\r\n" + json.dumps(WAKE).encode() + b"\n  \n")
    assert events_file.read(path).wakes == [WAKE]
    path.write_bytes(json.dumps(HEADER).encode() + b"\n\xff\xfe\n")
    with pytest.raises(events_file.EventsError, match=r":2: not a JSON object$"):
        events_file.read(path)


def test_an_empty_file_is_refused_and_a_missing_one_raises_its_own_error(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    with pytest.raises(events_file.EventsError, match="is format None version None"):
        events_file.read(empty)
    with pytest.raises(FileNotFoundError):
        events_file.read(tmp_path / "never-written.jsonl")


def test_the_command_prints_the_turns_the_gap_the_tokens_and_what_was_sent(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", RUN)
    assert events_file.main([str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "emet-talk, emet-events 1.0, linux-aarch64, Python 3.13.5, emet-engine 0.5.7",
        "  turn 1  answered: gap 2260 ms; 967 tokens in, 18 out; "
        "3.3 s of audio to api.deepgram.com, 2071 characters to api.openai.com, 57 characters to api.deepgram.com",
        "  turn 2  failed: gap - (nothing was spoken); 1.2 s of audio to api.deepgram.com, "
        "2100 characters to api.openai.com",
        "  turn 3  answered: gap 1800 ms; 1000 tokens in; 2.0 s of audio to api.deepgram.com",
        "  turns     3",
        "  turn gap  median 2030 ms   mean 2030   max 2260   (2 turn(s) with a gap)",
        "  tokens    1967 in, 18 out   (2 turn(s) that counted them)",
        "  sent      6.5 s of audio to api.deepgram.com, 4171 characters to api.openai.com, "
        "57 characters to api.deepgram.com",
    ]


def test_the_command_refuses_on_stderr_with_exit_1(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", [{**HEADER, "version": "2.0"}])
    assert events_file.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert err == f"events_file: {path} is format 'emet-events' version '2.0'; this reader knows emet-events 1.x\n"
    assert events_file.main([str(tmp_path / "nothere.jsonl")]) == 1
    assert capsys.readouterr().err.startswith(f"events_file: cannot read {str(tmp_path / 'nothere.jsonl')!r}: ")


def test_it_runs_on_a_bare_python(tmp_path):
    """The body runner starts on a bare Python and imports this reader, so
    `-S -I` (no site-packages, no script folder) must be enough."""
    path = write(tmp_path / "run.jsonl", RUN)
    done = subprocess.run([sys.executable, "-S", "-I", str(TOOLS / "events_file.py"), str(path)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "  turns     3" in done.stdout.splitlines()


def test_a_file_the_engine_writes_reads_back_to_the_footers_sums(tmp_path):
    """Asked by A14 of 0.5.7: the reader's sums of a file `EventWriter`
    wrote equal the footer record's `turn_gap`, `tokens` and `sent`. The
    reader sums gaps the file holds in whole milliseconds and the footer
    sums them unrounded, so a median or a mean can differ by 1 ms when two
    gaps sit just under a half (I1's finding F2 of 0.5.7); these do not."""
    deepgram, openai = "api.deepgram.com:443", "api.openai.com:443"
    # Each turn: tail, final, first sound, tokens in and out, and the
    # transcriber's, the model's and the voice's host. Turn 5 counted only
    # what the model wrote (tools-runner tests#1 of 0.5.7's review), and
    # turns 1 and 5 handed their characters to an Aura voice (tests#2).
    figures = [(960.0, 412.4, 251.3, 31, 12, deepgram, openai, deepgram),
               (960.0, 388.6, 250.9, 40, 9, deepgram, openai, None),
               (1920.0, 401.2, 262.7, None, None, deepgram, None, None),
               (960.0, None, 300.5, 18, 7, None, "127.0.0.1:9380", None),
               (960.0, 395.0, 270.0, None, 7, deepgram, openai, deepgram)]
    footer, got = engine_file(tmp_path / "engine.jsonl", figures)
    assert got["turns"] == len(figures)
    assert got["gap"] == footer["turn_gap"] and got["tokens"] == footer["tokens"]
    assert got["sent"] == footer["sent"]
    assert {"stage": "voice", "host": deepgram, "chars": 120} in got["sent"], got["sent"]


def engine_file(path: Path, figures: list[tuple]) -> tuple[dict, dict]:
    """The footer record of a file `EventWriter` wrote for turns of these
    figures, and the reader's summary of the same file. A turn whose voice
    is on the body hands it 30 characters, which are not sent."""
    from emet_engine.events import EventWriter
    from emet_engine.metrics import SessionStats, TurnRecord

    stats = SessionStats()
    with EventWriter(path, "emet-talk", clock=lambda: 1.0) as writer:
        for n, (tail, final, first, tin, tout, stt, llm, voice) in enumerate(figures, start=1):
            record = TurnRecord(turn=n, outcome="answered", tail_ms=tail, final_ms=final, voice_first_ms=first,
                                input_tokens=tin, output_tokens=tout, stt_audio_s=2.5 * n if stt else 0.0,
                                stt_host=stt, llm_chars=100 * n if llm else 0, llm_host=llm,
                                voice_chars=20 * n if voice else 30, voice_host=voice)
            stats.record_turn(record)
            writer.turn(record)
        writer.footer(stats, live=False, board_start=None, board_end=None)
    events = events_file.read(path)
    assert events.footer is not None
    return events.footer, events_file.summary(events)


@pytest.mark.parametrize("host", ["127.0.0.1:9380", "127.8.0.1", "localhost:11434", "LOCALHOST", "[::1]:8080",
                                  "0.0.0.0:80", "[::]:80", "api.openai.com:443", "api.deepgram.com",
                                  "192.168.1.20:8080", "[2001:db8::1]:443", "emet-pi.local:9380"])
def test_a_host_on_this_machine_is_the_one_the_engine_names(host):
    """The reader's copy of the engine's rule: a model server at 127.0.0.1
    sent nothing off the body, and any other host did."""
    from emet_engine.metrics import _on_this_machine

    assert events_file.on_this_machine(host) == _on_this_machine(host)


@pytest.mark.parametrize("tin, tout", [(None, 18), (967, None)], ids=["no-input-counted", "no-output-counted"])
def test_a_run_that_counted_one_side_reads_back_as_the_footer_holds_it(tmp_path, tin, tout):
    """The engine's footer holds None for a side no turn counted, and so
    does the reader, over two turns that counted the other side."""
    figures = [(960.0, 400.0, 250.0, tin, tout, "api.deepgram.com:443", "api.openai.com:443", None)] * 2
    footer, got = engine_file(tmp_path / "engine.jsonl", figures)
    assert footer["tokens"] == {"in": tin and 2 * tin, "out": tout and 2 * tout, "n": 2}
    assert got["tokens"] == footer["tokens"]
