"""The `--events` file, held to its schema and to its contract.

Every record a run writes is checked against the JSON Schema the package
ships, its keys are held to the contract's order, and its numbers to their
form. A run driven through the mocks writes a file that holds none of the
words said in it, which is decision N138.
"""

from __future__ import annotations

import asyncio
import json
import math
import platform
import sys
import wave
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

from emet_sdk.discovery import PluginRegistry

from emet_engine import cli, events
from emet_engine.board import BoardReading
from emet_engine.events import FORMAT, PACKAGES, SCHEMA, VERSION, EventsFileError, EventWriter
from emet_engine.metrics import OUTCOMES, Handover, SessionStats, TurnRecord
from emet_engine.session import EngineError, ListenSession

PHRASE = "hey emet"
FRAME = 1280

WAKE_KEYS = ["kind", "wake", "at_s", "confidence", "lag_ms", "heard_ms", "name_ms", "level_ms", "cut_short"]
TURN_KEYS = [
    "kind", "turn", "at_s", "outcome", "gap_ms",
    "tail_ms", "final_ms", "reply_first_ms", "voice_first_ms", "input_tokens", "output_tokens",
    "stt_audio_s", "stt_host", "llm_chars", "llm_host", "voice_chars", "voice_host", "sentences", "cleaned",
]
FOOTER_KEYS = [
    "kind", "at_s", "frames", "audio_s", "wakes", "turns", "extended",
    "frame_mean_ms", "frame_p50_ms", "frame_p95_ms", "frame_max_ms", "budget_ms", "over_budget",
    "realtime", "dropped", "dropped_busy", "overflows",
    "stt_final", "llm_first", "llm_done", "voice_first", "voice_done",
    "name_cut", "turn_gap", "tokens", "sent", "cleaned", "clock", "board", "kept_up",
]


@pytest.fixture(scope="module")
def schema() -> Draft202012Validator:
    doc = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(doc)
    return Draft202012Validator(doc)


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def a_turn(**changes) -> TurnRecord:
    figures = dict(
        turn=1, outcome="answered", tail_ms=960.4, final_ms=410.2, reply_first_ms=620.5, voice_first_ms=890.6,
        input_tokens=967, output_tokens=18, stt_audio_s=3.31234, stt_host="api.deepgram.com",
        llm_chars=2071, llm_host="api.openai.com", voice_chars=78, voice_host=None, sentences=2, cleaned=1,
    )
    return TurnRecord(**{**figures, **changes})


def a_full_run() -> SessionStats:
    """Stats with every footer line filled, from three turns, two of them spoken."""
    s = SessionStats()
    for ms in (2.5, 3.25, 9.125):
        s.record_frame(ms)
    s.wakes, s.turns, s.extended, s.dropped, s.dropped_busy, s.overflows = 3, 3, 1, 5, 4, 2
    s.record_final(410.2)
    s.record_handover(Handover(190.0, 190.0, 130.0, 130.0))
    s.record_handover(Handover(80.0, 80.0, 0.0, 0.0))
    s.record_reply(620.5, 1500.4)
    s.record_speech(890.6, 2400.2, spoken=2, lost=1)
    s.record_turn(a_turn())
    s.record_turn(a_turn(turn=2, outcome="failed", input_tokens=None, output_tokens=None, sentences=0, cleaned=0))
    s.record_turn(TurnRecord(turn=3, outcome="no_speech", tail_ms=900.0, stt_audio_s=1.2, stt_host="api.deepgram.com"))
    return s


def write_a_full_run(path: Path, *, live: bool, board: bool) -> list[dict]:
    clock = Clock()
    start = BoardReading(vcgencmd=True, temp_c=47.7, throttled=0, load=(0.12109375, 0.09, 0.03))
    end = BoardReading(vcgencmd=True, temp_c=46.6, throttled=0x50000, load=(0.06, 0.09, 0.03))
    stats = a_full_run()
    with EventWriter(path, "emet-talk", clock=clock) as writer:
        clock.now += 1.23456
        writer.wake(Handover(190.4, 190.4, 130.6, 130.6), 1.0, number=1)
        writer.wake(Handover(720.0, 640.0, 0.0, 0.0, cut_short=True), 0.876)
        for record in stats.turn_records:
            clock.now += 2.0
            writer.turn(record)
        writer.footer(stats, live=live, board_start=start if board else None, board_end=end if board else None)
    return lines(path)


# ------------------------------------------------------------------ the header


def test_the_file_starts_with_a_header_naming_what_wrote_it(tmp_path):
    path = tmp_path / "e.jsonl"
    EventWriter(path, "emet-listen").close()
    (head,) = lines(path)
    assert list(head) == ["format", "version", "program", "packages", "platform", "python"]
    assert head["format"] == FORMAT == "emet-events" and head["version"] == VERSION == "1.0"
    assert head["program"] == "emet-listen"
    assert list(head["packages"]) == list(PACKAGES)
    assert head["packages"]["emet-engine"] == cli._version("emet-engine")
    assert head["platform"] == f"{sys.platform}-{platform.machine().lower()}"


def test_the_header_names_the_packages_the_identity_line_names():
    assert PACKAGES == cli.DISTRIBUTIONS


# ------------------------------------------------------------------ the records


def test_each_kind_keeps_the_contracts_keys_in_order(tmp_path):
    head, *records = write_a_full_run(tmp_path / "e.jsonl", live=True, board=True)
    kinds = [r["kind"] for r in records]
    assert kinds == ["wake", "wake", "turn", "turn", "turn", "footer"]
    for record in records:
        assert list(record) == {"wake": WAKE_KEYS, "turn": TURN_KEYS, "footer": FOOTER_KEYS}[record["kind"]]


def test_the_turn_record_carries_every_field_of_the_record_from_tail_on():
    """A field added to `TurnRecord` arrives in the events file by itself, so
    this fails until somebody has read it against N138."""
    from dataclasses import fields

    assert TURN_KEYS[5:] == [f.name for f in fields(TurnRecord)][2:]


def test_every_record_a_run_writes_is_one_the_schema_holds(tmp_path, schema):
    for live in (False, True):
        for board in (False, True):
            for record in write_a_full_run(tmp_path / f"e-{live}-{board}.jsonl", live=live, board=board):
                schema.validate(record)


def test_an_empty_run_writes_a_footer_the_schema_holds(tmp_path, schema):
    path = tmp_path / "e.jsonl"
    with EventWriter(path, "emet-listen") as writer:
        writer.footer(SessionStats(), live=True, board_start=None, board_end=None)
    head, footer = lines(path)
    schema.validate(head)
    schema.validate(footer)
    assert footer["sent"] == [] and footer["turn_gap"] is None and footer["name_cut"] is None


def walk(record: dict):
    for key, value in record.items():
        if isinstance(value, dict):
            yield from walk(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from walk(item)
        else:
            yield key, value


def test_milliseconds_are_whole_and_seconds_have_three_decimals(tmp_path):
    for record in write_a_full_run(tmp_path / "e.jsonl", live=True, board=True):
        for key, value in walk(record):
            if key.endswith("_ms") and not key.startswith("frame_"):
                assert value is None or (type(value) is int), (record.get("kind"), key, value)
            elif key.endswith("_s"):
                assert value is None or round(value, 3) == value, (record.get("kind"), key, value)


def test_the_numbers_are_the_ones_the_console_prints(tmp_path):
    _, wake, short, turn, failed, quiet, footer = write_a_full_run(tmp_path / "e.jsonl", live=True, board=True)
    assert wake == {
        "kind": "wake", "wake": 1, "at_s": 1.235, "confidence": 1.0,
        "lag_ms": 190, "heard_ms": 190, "name_ms": 131, "level_ms": 131, "cut_short": False,
    }
    assert short["wake"] == 2 and short["confidence"] == 0.88 and short["cut_short"] is True
    assert short["heard_ms"] == 640, "the wake records are counted when no number is given"
    record = a_turn()
    assert turn["gap_ms"] == round(record.gap_ms) == int(record.line().split()[2])
    assert turn["tail_ms"] == 960 and turn["stt_audio_s"] == 3.312 and turn["voice_host"] is None
    assert failed["outcome"] == "failed" and failed["input_tokens"] is None
    assert quiet["gap_ms"] is None and quiet["voice_first_ms"] is None
    assert footer["frame_max_ms"] == float(f"{9.125:.2f}") == 9.12, "rounded as the footer's :.2f rounds it"
    assert footer["frame_mean_ms"] == 4.96
    assert footer["budget_ms"] == 80 and footer["overflows"] == 2 and footer["dropped_busy"] == 4
    assert footer["voice_done"] == {"mean_ms": 2400, "max_ms": 2400, "n": 1, "spoken": 2, "lost": 1}
    assert footer["name_cut"] == {"moved": 1, "of": 2, "mean_ms": 130, "max_ms": 130}
    assert footer["turn_gap"] == {"median_ms": 2261, "mean_ms": 2261, "max_ms": 2261, "n": 2}
    assert footer["tokens"] == {"in": 967, "out": 18, "n": 1}
    assert footer["sent"] == [
        {"stage": "stt", "host": "api.deepgram.com", "audio_s": 7.825},
        {"stage": "llm", "host": "api.openai.com", "chars": 4142},
    ]
    assert footer["cleaned"] == {"changed": 1, "of": 2}
    assert footer["board"] == {
        "start": {"temp_c": 47.7, "throttled": 0, "load": [0.12, 0.09, 0.03]},
        "end": {"temp_c": 46.6, "throttled": 0x50000, "load": [0.06, 0.09, 0.03]},
    }
    assert footer["kept_up"] is False, "a frame dropped while listening is no pass"
    assert set(footer["clock"]) == {"wall_s", "audio_s", "skew_s", "skew_pct"}


def test_each_footer_figure_is_the_one_the_console_prints_beside_its_label(tmp_path):
    """Review finding engine-core/tests#6 of 0.5.7: two values for each
    latency and the name cut, and three turns with three different gaps, so
    a mean, a largest and a median cannot stand in for one another. Each is
    read back from `SessionStats.report()`, the console's footer."""
    import re

    s = SessionStats()
    s.record_frame(2.0)
    s.record_final(400.0)
    s.record_final(600.0)
    s.record_reply(100.0, 1000.0)
    s.record_reply(300.0, 2000.0)
    s.record_speech(200.0, 1500.0, spoken=1, lost=0)
    s.record_speech(500.0, 3500.0, spoken=2, lost=1)
    for cut in (100.0, 300.0, 0.0):
        s.record_handover(Handover(190.0, 190.0, cut, cut))
    for turn, voice in enumerate((100.0, 400.0, 1600.0), start=1):
        s.record_turn(TurnRecord(turn=turn, tail_ms=900.0, voice_first_ms=voice))
    path = tmp_path / "e.jsonl"
    with EventWriter(path, "emet-talk") as writer:
        writer.footer(s, live=False, board_start=None, board_end=None)
    footer = lines(path)[-1]
    console = {line[2:16].strip(): line[16:] for line in s.report(live=False).splitlines()}
    for key in ("stt_final", "llm_first", "llm_done", "voice_first", "voice_done", "name_cut"):
        mean, largest = re.search(r"mean (\d+) ms   max (\d+)", console[key.replace("_", " ")]).groups()
        assert (footer[key]["mean_ms"], footer[key]["max_ms"]) == (int(mean), int(largest)), key
        assert footer[key]["mean_ms"] != footer[key]["max_ms"], key
    median, mean, largest = re.search(r"median (\d+) ms   mean (\d+)   max (\d+)", console["turn gap"]).groups()
    assert footer["turn_gap"] == {"median_ms": int(median), "mean_ms": int(mean), "max_ms": int(largest), "n": 3}
    assert (int(median), int(mean), int(largest)) == (1300, 1600, 2500)


def test_a_file_has_no_card_so_no_overflows_and_no_clock(tmp_path):
    footer = write_a_full_run(tmp_path / "e.jsonl", live=False, board=False)[-1]
    assert footer["overflows"] is None and footer["clock"] is None and footer["board"] is None


def test_a_board_with_nothing_to_read_is_null_as_the_footer_prints_no_line(tmp_path):
    path = tmp_path / "e.jsonl"
    nothing = BoardReading(vcgencmd=False)
    with EventWriter(path, "emet-talk") as writer:
        writer.footer(SessionStats(), live=False, board_start=nothing, board_end=nothing)
    assert lines(path)[-1]["board"] is None


def test_at_s_counts_from_the_opening_of_the_file(tmp_path):
    clock = Clock()
    path = tmp_path / "e.jsonl"
    with EventWriter(path, "emet-talk", clock=clock) as writer:
        clock.now += 0.0004
        writer.turn(TurnRecord(turn=1))
        clock.now += 61.5
        writer.footer(SessionStats(), live=False, board_start=None, board_end=None)
    assert [r.get("at_s") for r in lines(path)] == [None, 0.0, 61.5]


def test_each_line_is_flushed_as_it_is_written(tmp_path):
    """A run stopped hard keeps every record it finished."""
    path = tmp_path / "e.jsonl"
    writer = EventWriter(path, "emet-listen")
    assert len(lines(path)) == 1
    writer.turn(TurnRecord(turn=1))
    assert [r.get("kind") for r in lines(path)] == [None, "turn"]
    writer.close()
    writer.close()
    with pytest.raises(ValueError, match="was closed, and 'turn' came after"):
        writer.turn(TurnRecord(turn=2))


def test_the_file_is_ascii_whatever_a_host_is_called(tmp_path):
    path = tmp_path / "e.jsonl"
    with EventWriter(path, "emet-talk") as writer:
        writer.turn(TurnRecord(turn=1, llm_chars=5, llm_host="bücher.example"))
    raw = path.read_bytes()
    assert raw.isascii() and b"\\u00fc" in raw
    assert lines(path)[-1]["llm_host"] == "bücher.example"


def test_an_existing_file_is_truncated(tmp_path):
    path = tmp_path / "e.jsonl"
    path.write_text("left over from another run\n" * 3, encoding="utf-8")
    EventWriter(path, "emet-talk").close()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_file_that_cannot_be_opened_is_refused_with_the_programs_name(tmp_path):
    missing = tmp_path / "no-such-folder" / "e.jsonl"
    with pytest.raises(EventsFileError) as caught:
        EventWriter(missing, "emet-listen")
    message = str(caught.value)
    assert message.startswith(f"emet-listen: cannot write the events file '{missing}': ")
    assert "No such file or directory" in message
    assert isinstance(caught.value, EngineError), "main() prints it as it prints any refusal"
    assert isinstance(caught.value.__cause__, OSError)


# ------------------------------------------------------------------ the schema


def test_the_schema_is_shipped_beside_the_module():
    assert SCHEMA.parent == Path(events.__file__).resolve().parent / "schemas"
    assert SCHEMA.name == "events-1.schema.json" and VERSION.split(".")[0] == "1"


def test_the_schema_requires_every_key_the_writer_writes(schema):
    defs = schema.schema["$defs"]
    assert defs["wake"]["required"] == WAKE_KEYS
    assert defs["turn"]["required"] == TURN_KEYS
    assert defs["footer"]["required"] == FOOTER_KEYS
    assert defs["header"]["required"] == ["format", "version", "program", "packages", "platform", "python"]
    assert defs["header"]["properties"]["packages"]["required"] == list(PACKAGES)


def test_the_schemas_outcomes_are_the_records(schema):
    assert schema.schema["$defs"]["turn"]["properties"]["outcome"]["enum"] == list(OUTCOMES)


def test_the_schema_refuses_what_the_contract_rules_out(tmp_path, schema):
    _, wake, *_, footer = write_a_full_run(tmp_path / "e.jsonl", live=True, board=True)
    head = events.header("emet-talk")
    turn = events.turn_record(a_turn(), at_s=1.0)
    bad = [
        {**head, "format": "emet-log"},
        {**head, "version": "2.0"},
        {k: v for k, v in turn.items() if k != "cleaned"},
        {**turn, "outcome": "answred"},
        {**turn, "gap_ms": 2261.4},
        {**wake, "kind": "heard"},
        {**footer, "sent": [{"stage": "stt", "host": "api.deepgram.com", "chars": 3}]},
        {**footer, "kept_up": "kept up"},
    ]
    for record in bad:
        with pytest.raises(SchemaError):
            schema.validate(record)


def test_the_schema_allows_a_key_a_minor_version_adds(schema):
    schema.validate({**events.turn_record(a_turn(), at_s=1.0), "cached_input_tokens": 512})
    schema.validate({**events.header("emet-listen"), "version": "1.3"})


# ------------------------------------------------------------------ a real run


def write_wav(path: Path, pcm: bytes) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return str(path)


def frame(payload: bytes = b"") -> bytes:
    return payload + bytes(2 * FRAME - len(payload))


def mock_run(tmp_path: Path) -> tuple[ListenSession, Path]:
    """Two turns through the mocks, a wav for a microphone and the null sink,
    each wake's record written from the session's own hooks as `--stats`
    prints its line."""
    # The wake fires a frame late, on the silent frame, which is handed over.
    said = frame() + frame(PHRASE.encode()) + frame() + frame(b"kettle") + frame(b"boiled") + frame() * 20
    manifest = {
        "audio": {
            "input": {"source": "wav", "device": "file", "params": {"path": write_wav(tmp_path / "in.wav", said * 2)}},
            "output": {"sink": "null", "device": "none"},
            "wake": {"engine": "mock", "params": {"lag_frames": 1}},
        },
        "models": {"stt": {"provider": "mock"}, "chat": {"provider": "mock"}, "tts": {"provider": "mock"}},
    }
    soul = {"identity": {"name": "Emet", "wake_word": PHRASE}}
    path = tmp_path / "run.events.jsonl"
    writer = EventWriter(path, "emet-talk")
    heard: dict[str, float] = {}

    async def run() -> ListenSession:
        session = ListenSession(
            manifest,
            soul,
            registry=PluginRegistry.discover(),
            transcribe=True,
            reply=True,
            speak=True,
            on_wake=lambda event: heard.update(confidence=event.confidence),
            on_handover=lambda handover: writer.wake(handover, heard["confidence"], number=session.stats.wakes),
        )
        async with session:
            async for _ in session.talk():
                pass
        return session

    with writer:
        session = asyncio.run(run())
        for record in session.stats.turn_records:
            writer.turn(record)
        writer.footer(session.stats, live=False, board_start=None, board_end=None)
    return session, path


def test_a_real_run_writes_records_the_schema_holds_and_no_word_said_in_it(tmp_path, schema):
    session, path = mock_run(tmp_path)
    records = lines(path)
    for record in records:
        schema.validate(record)
    assert [r["wake"] for r in records if r.get("kind") == "wake"] == [1, 2]
    footer = records[-1]
    assert footer["kind"] == "footer" and footer["wakes"] == 2 and footer["turns"] == 2
    assert footer["name_cut"]["of"] == 2, "a handover for each wake"
    said = " ".join(m.content for m in session.conversation.messages).lower()
    assert "kettle boiled" in said, "the words were heard and answered, so they could have leaked"
    text = path.read_text(encoding="utf-8").lower()
    for word in ("kettle", "boiled", "you said", PHRASE):
        assert word not in text, f"{word!r} reached the events file (N138)"
    assert math.isclose(footer["audio_s"], session.stats.audio_ms / 1000, abs_tol=0.0005)


def test_a_real_run_has_one_turn_record_per_turn_each_with_its_gap(tmp_path):
    """The records are the session's to fill (0.5.7 contract 2.1); this holds
    that what it fills reaches the file and prints a line."""
    session, path = mock_run(tmp_path)
    turns = [r for r in lines(path) if r.get("kind") == "turn"]
    assert [r["turn"] for r in turns] == [1, 2]
    assert all(r["outcome"] == "answered" and r["gap_ms"] is not None for r in turns)
    assert all(line is not None for line in (record.line() for record in session.stats.turn_records))
