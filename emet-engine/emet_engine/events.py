"""The `--events` file, every `--stats` line written again as a JSON record.

The runner, the budget table and the sitting's join read the console, with
a regular expression for each line they need, and a change to the wording of
one line breaks a reader in another folder. The events file gives them the
same numbers as data. A header names the format, its version, the four
packages and the platform; then one record follows for each line printed
under a wake, one for each per-turn line, and one for the footer. Each is one
JSON object on one line, written in ASCII and flushed as it is written, so a
run stopped hard keeps every record it had finished.

**Numbers and verdicts only** (decision N138). A record never holds a word
anybody said, a reply's text or any audio. The only free strings are the
hosts each stage was set up with and the header's versions and platform,
beside the vocabulary's own names. The file holds every figure of each
`--stats` line, and a few the console leaves out: each turn's outcome, its
sentences and how many cleaning changed, what a stage on this body was
handed, and a handover's heard audio when it was not cut short. Writing it
changes no console line.

**Two ideas are borrowed, and no code.** A file that describes itself in its
header, and a reader that refuses a version it does not know, are PX4's
ULog's: "The format is self-describing", and "If a parser finds any of these
bits set that isn't specified, it must refuse to parse the log"
(docs.px4.io/main/en/dev_log/ulog_file_format.html, both sentences verified
2026-11-27; the PX4-Autopilot repository is BSD-3-Clause by the GitHub API,
verified 2026-11-27, and the documentation's own licence is unread). A run's
record kept as a file a person can download, read and share is Home
Assistant's automation traces (home-assistant.io/docs/automation/testing/,
verified 2026-11-25 by the project's survey); its documentation is CC
BY-NC-SA 4.0 (the repository's LICENSE.md, read 2026-11-27), so the idea is
taken and none of its words. `CITATIONS.md` has the entry.

**Versions.** `VERSION` is major and minor. A minor version may add keys,
kinds and values; a reader ignores a key it does not know, skips a kind it
does not know, and refuses a format other than `FORMAT` or a major other
than its own. `SCHEMA` is the JSON Schema of this major, draft 2020-12, with
every key of the version required and others allowed, shipped inside the
package beside this module.

**The numbers' form.** Every `*_ms` is whole milliseconds, rounded as the
console's `:.0f` rounds them, except the four `frame_*_ms`, which keep the
footer's two decimals. Every `*_s` has at most three decimals. A missing
value is `null`. `at_s` is seconds since the file was opened.
"""

from __future__ import annotations

import json
import math
import platform
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import fields
from importlib import metadata
from pathlib import Path
from typing import IO, Any

from emet_engine.board import BoardReading
from emet_engine.metrics import Handover, Sent, SessionStats, TurnRecord
from emet_engine.session import EngineError

__all__ = [
    "FORMAT",
    "PACKAGES",
    "SCHEMA",
    "VERSION",
    "EventWriter",
    "EventsFileError",
    "footer_record",
    "header",
    "turn_record",
    "wake_record",
]

#: What the header's `format` says. A reader refuses any other.
FORMAT = "emet-events"

#: Major and minor. A reader refuses a major other than its own.
VERSION = "1.0"

#: The JSON Schema every record of this major is checked against.
SCHEMA = Path(__file__).resolve().parent / "schemas" / "events-1.schema.json"

#: The four distributions the header names, in the order the identity line
#: names them (`emet_engine.cli.DISTRIBUTIONS`, which a test holds this to).
PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")


class EventsFileError(EngineError):
    """The events file could not be opened for writing.

    Its message names the program, the path and the operating system's
    reason, and `main()` prints it as it prints any refusal, before a device
    opens.
    """


def _version(distribution: str) -> str:
    """A distribution's installed version, or `not-installed`, as the
    identity line writes it."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _ms(value: float | None) -> int | None:
    """Whole milliseconds, rounded as `:.0f` rounds them, or None."""
    if value is None or not math.isfinite(value):
        return None
    return round(value)


def _s(value: float | None) -> float | None:
    """Seconds to three decimals, or None."""
    if value is None or not math.isfinite(value):
        return None
    return round(value, 3)


def header(program: str) -> dict[str, Any]:
    """The file's first line, which names what wrote it."""
    return {
        "format": FORMAT,
        "version": VERSION,
        "program": program,
        "packages": {name: _version(name) for name in PACKAGES},
        "platform": f"{sys.platform}-{platform.machine().lower()}",
        "python": platform.python_version(),
    }


def wake_record(handover: Handover, confidence: float, *, wake: int, at_s: float) -> dict[str, Any]:
    """The record of the line `--stats` prints under a wake."""
    return {
        "kind": "wake",
        "wake": wake,
        "at_s": _s(at_s),
        "confidence": round(confidence, 2),
        "lag_ms": _ms(handover.lag_ms),
        "heard_ms": _ms(handover.heard_ms),
        "name_ms": _ms(handover.name_ms),
        "level_ms": _ms(handover.level_ms),
        "cut_short": handover.cut_short,
    }


def turn_record(record: TurnRecord, *, at_s: float) -> dict[str, Any]:
    """The record of the per-turn line.

    The gap first, then every field of the `TurnRecord` from `tail_ms` on,
    in the dataclass's order. A field added to the record arrives here by
    itself, and the test that holds the keys fails until it is read: a
    field holding words would break N138.
    """
    out: dict[str, Any] = {
        "kind": "turn",
        "turn": record.turn,
        "at_s": _s(at_s),
        "outcome": record.outcome,
        "gap_ms": _ms(record.gap_ms),
    }
    for item in fields(record):
        if item.name in ("turn", "outcome"):
            continue
        value = getattr(record, item.name)
        if item.name.endswith("_ms"):
            value = _ms(value)
        elif item.name.endswith("_s"):
            value = _s(value)
        out[item.name] = value
    return out


def _summary(values: Sequence[float]) -> dict[str, Any] | None:
    """`{"mean_ms", "max_ms", "n"}` of one footer latency, or None."""
    if not values:
        return None
    return {"mean_ms": _ms(sum(values) / len(values)), "max_ms": _ms(max(values)), "n": len(values)}


def _voice_done(stats: SessionStats) -> dict[str, Any] | None:
    done = _summary(stats.speech_done_ms)
    if done is not None:
        done.update(spoken=stats.sentences_spoken, lost=stats.sentences_lost)
    return done


def _name_cut(stats: SessionStats) -> dict[str, Any] | None:
    """The footer's `name cut` line, the handovers whose cut moved and all of them."""
    if not stats.name_cut_ms:
        return None
    moved = [ms for ms in stats.name_cut_ms if ms > 0]
    return {
        "moved": len(moved),
        "of": len(stats.name_cut_ms),
        "mean_ms": _ms(sum(moved) / len(moved)) if moved else None,
        "max_ms": _ms(max(moved)) if moved else None,
    }


def _turn_gap(stats: SessionStats) -> dict[str, Any] | None:
    spread = stats.gap_spread()
    if spread is None:
        return None
    return {
        "median_ms": _ms(spread.median_ms),
        "mean_ms": _ms(spread.mean_ms),
        "max_ms": _ms(spread.max_ms),
        "n": spread.n,
    }


def _tokens(stats: SessionStats) -> dict[str, Any] | None:
    tokens = stats.token_totals()
    if tokens is None:
        return None
    return {"in": tokens.input, "out": tokens.output, "n": tokens.n}


def _sent(entry: Sent) -> dict[str, Any]:
    if entry.stage == "stt":
        return {"stage": entry.stage, "host": entry.host, "audio_s": _s(entry.amount)}
    return {"stage": entry.stage, "host": entry.host, "chars": int(entry.amount)}


def _cleaned(stats: SessionStats) -> dict[str, Any] | None:
    cleaned = stats.cleaned_totals()
    if cleaned is None:
        return None
    return {"changed": cleaned[0], "of": cleaned[1]}


def _clock(stats: SessionStats) -> dict[str, Any]:
    """The footer's clock line, the card's clock against the system's."""
    wall, audio = stats.wall_ms, stats.audio_ms
    skew = wall - audio
    pct = (skew / audio * 100.0) if audio else 0.0
    return {
        "wall_s": _s(wall / 1000),
        "audio_s": _s(audio / 1000),
        "skew_s": _s(skew / 1000),
        "skew_pct": round(pct, 2),
    }


def _reading(reading: BoardReading) -> dict[str, Any]:
    """One board reading, to the precision the footer prints it."""
    return {
        "temp_c": round(reading.temp_c, 1) if reading.temp_c is not None else None,
        "throttled": reading.throttled,
        "load": [round(x, 2) for x in reading.load] if reading.load is not None else None,
    }


def _board(start: BoardReading | None, end: BoardReading | None) -> dict[str, Any] | None:
    """Both readings, or None where the footer prints no board line."""
    if start is None or end is None or not (start.parts() or end.parts()):
        return None
    return {"start": _reading(start), "end": _reading(end)}


def footer_record(
    stats: SessionStats,
    *,
    live: bool,
    board_start: BoardReading | None,
    board_end: BoardReading | None,
    at_s: float,
) -> dict[str, Any]:
    """The record of the footer, from `frames` to `verdict`.

    `live` and the two board readings are what `SessionStats.report` and
    `board.footer_lines` are given for the same footer. A key the footer
    leaves out for this run is null: `overflows` and `clock` for a file, a
    latency no turn measured.
    """
    return {
        "kind": "footer",
        "at_s": _s(at_s),
        "frames": stats.frames,
        "audio_s": _s(stats.audio_ms / 1000),
        "wakes": stats.wakes,
        "turns": stats.turns,
        "extended": stats.extended,
        "frame_mean_ms": round(stats.mean_ms, 2),
        "frame_p50_ms": round(stats.percentile(50), 2),
        "frame_p95_ms": round(stats.percentile(95), 2),
        "frame_max_ms": round(stats.process_ms_max, 2),
        "budget_ms": _ms(stats.frame_ms),
        "over_budget": stats.over_budget,
        "realtime": round(stats.realtime_factor, 4),
        "dropped": stats.dropped,
        "dropped_busy": stats.dropped_busy,
        "overflows": stats.overflows if live else None,
        "stt_final": _summary(stats.final_ms),
        "llm_first": _summary(stats.reply_first_ms),
        "llm_done": _summary(stats.reply_done_ms),
        "voice_first": _summary(stats.speech_first_ms),
        "voice_done": _voice_done(stats),
        "name_cut": _name_cut(stats),
        "turn_gap": _turn_gap(stats),
        "tokens": _tokens(stats),
        "sent": [_sent(entry) for entry in stats.sent_totals()],
        "cleaned": _cleaned(stats),
        "clock": _clock(stats) if live else None,
        "board": _board(board_start, board_end),
        "kept_up": stats.kept_up,
    }


class EventWriter:
    """One run's events file, opened (truncated) and headed when made.

    `emet-listen` and `emet-talk` make one after the identity line and
    before the boot lines, then call `wake` beside each line printed under a
    wake, `turn` beside each per-turn line, `footer` beside the footer, and
    `close` at exit. A path that cannot be opened raises `EventsFileError`
    with the program's name first: `emet-listen: cannot write the events
    file 'PATH': ...`. `clock` is the monotonic clock `at_s` is read from,
    given only by tests.
    """

    def __init__(self, path: str | Path, program: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self.program = program
        self._clock = clock
        self._wakes = 0
        try:
            self._file: IO[str] | None = open(self.path, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise EventsFileError(f"{program}: cannot write the events file '{path}': {exc}") from exc
        self._opened = clock()
        self._write(header(program))

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _at(self) -> float:
        return self._clock() - self._opened

    def _write(self, record: dict[str, Any]) -> None:
        if self._file is None:
            raise ValueError(f"the events file {self.path} was closed, and {record.get('kind')!r} came after")
        self._file.write(json.dumps(record, ensure_ascii=True) + "\n")
        self._file.flush()

    def wake(self, handover: Handover, confidence: float, *, number: int | None = None) -> None:
        """Write the record of the line under a wake. `number` is the run's
        count of wakes, `SessionStats.wakes`, when the caller has it; without
        it the wake records are counted."""
        self._wakes += 1
        self._write(wake_record(handover, confidence, wake=self._wakes if number is None else number, at_s=self._at()))

    def turn(self, record: TurnRecord) -> None:
        """Write the record of a turn's line, as the record stands now."""
        self._write(turn_record(record, at_s=self._at()))

    def footer(
        self,
        stats: SessionStats,
        *,
        live: bool,
        board_start: BoardReading | None,
        board_end: BoardReading | None,
    ) -> None:
        """Write the footer's record."""
        self._write(footer_record(stats, live=live, board_start=board_start, board_end=board_end, at_s=self._at()))

    def close(self) -> None:
        """Close the file. A second call does nothing."""
        if self._file is not None:
            self._file.close()
            self._file = None
