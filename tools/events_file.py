#!/usr/bin/env python3
"""Read the events file that `--events PATH` writes beside `--stats`.

    python tools/events_file.py FILE

`emet-listen --stats --events PATH` and `emet-talk --stats --events PATH`
write every `--stats` line a second time as JSON, one object a line, so a
tool can read a run's numbers without parsing its console. The first line
is the header, `{"format": "emet-events", "version": "1.0", ...}`; each
line after it is a record with a `kind`: `wake` with each per-wake line,
`turn` with each per-turn line, `footer` with the footer. The records hold
numbers and verdicts only, never a word anybody said. The contract is
`releases/v0.5.7/contract.md` section 2.2 and the schema
`emet-engine/emet_engine/schemas/events-1.schema.json`.

**What this reader holds to.** A header whose `format` is not
`emet-events`, or whose `version` is not a string of major version 1, is
refused with `EventsError`, as is a line that is not a JSON object. A minor
version may add keys and kinds, so a key this reader does not know is
left alone and a record of a kind it does not know is skipped. Blank lines
are skipped. A file that cannot be opened raises the `OSError` itself, so
a caller can tell a run that wrote no events file from one whose file is
wrong.

From the command line it prints each turn, then the turn gap's median,
mean and max, the tokens, and what was sent off the body, summed from the
turn records, so a run stopped before its footer still reads. Exit status
0, or 1 when the file is refused or cannot be read.

The body runner, `tools/body/run.py`, imports it. The standard library
only, so it runs on a bare Python as the runner does.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = ["FORMAT", "MAJOR", "KINDS", "EventsError", "EventsFile", "read", "gaps", "tokens",
           "on_this_machine", "sent", "summary", "summary_lines", "main"]

#: The header's `format`.
FORMAT = "emet-events"
#: The major version this reader knows. A file of version "1.x" is read.
MAJOR = 1
#: The record kinds this reader keeps, in the order a run writes them.
KINDS = ("wake", "turn", "footer")


class EventsError(ValueError):
    """An events file this reader will not read. The message names the file."""


@dataclass(frozen=True)
class EventsFile:
    """One run's events, the header and its records of known kinds in file order."""

    header: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def wakes(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("kind") == "wake"]

    @property
    def turns(self) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("kind") == "turn"]

    @property
    def footer(self) -> dict[str, Any] | None:
        """The last footer record, or None for a run stopped before its footer."""
        found = [r for r in self.records if r.get("kind") == "footer"]
        return found[-1] if found else None


def _major(version: Any) -> int | None:
    """The major number of a version string such as "1.0", or None."""
    if not isinstance(version, str):
        return None
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def read(path: str | Path) -> EventsFile:
    """Read and check one events file.

    Raises `EventsError` for a header of another format or major version and
    for a line that is not a JSON object, and the `OSError` itself for a file
    that cannot be opened."""
    shown = str(path)
    with open(path, "rb") as handle:
        raw_lines = handle.read().split(b"\n")
    header: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    for n, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            obj = None
        if not isinstance(obj, dict):
            raise EventsError(f"{shown}:{n}: not a JSON object")
        if header is None:
            header = obj
            if obj.get("format") != FORMAT or _major(obj.get("version")) != MAJOR:
                raise EventsError(f"{shown} is format {obj.get('format')!r} version {obj.get('version')!r}; "
                                  f"this reader knows {FORMAT} {MAJOR}.x")
            continue
        if obj.get("kind") in KINDS:
            records.append(obj)
    if header is None:
        raise EventsError(f"{shown} is format None version None; this reader knows {FORMAT} {MAJOR}.x")
    return EventsFile(header=header, records=records)


# ------------------------------------------------------------------ the sums


def _numbers(turns: Iterable[dict[str, Any]], key: str) -> list[float]:
    return [float(t[key]) for t in turns
            if isinstance(t.get(key), (int, float)) and not isinstance(t.get(key), bool)]


def gaps(turns: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The turn gap over the turns that have one, as the footer's `turn_gap`
    record holds it, or None when no turn has a gap."""
    values = _numbers(turns, "gap_ms")
    if not values:
        return None
    return {"median_ms": round(statistics.median(values)), "mean_ms": round(statistics.fmean(values)),
            "max_ms": round(max(values)), "n": len(values)}


def _side(turns: list[dict[str, Any]], key: str) -> int | None:
    """One side's tokens summed, or None when no turn counted that side, as
    the engine's `SessionStats.token_totals()` sums them, so a vendor that
    reports only what it wrote reads as None in and its count out."""
    values = _numbers(turns, key)
    return int(sum(values)) if values else None


def tokens(turns: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Tokens in and out summed over the turns that counted any, as the
    footer's `tokens` record holds them, or None when no turn counted. A
    side no turn counted is None."""
    counted = [t for t in turns if _numbers([t], "input_tokens") or _numbers([t], "output_tokens")]
    if not counted:
        return None
    return {"in": _side(counted, "input_tokens"), "out": _side(counted, "output_tokens"), "n": len(counted)}


#: Each stage's host key, the key of its amount, and the amount's name in
#: the `sent` records.
_STAGES = (("stt", "stt_host", "stt_audio_s", "audio_s"),
           ("llm", "llm_host", "llm_chars", "chars"),
           ("voice", "voice_host", "voice_chars", "chars"))


def on_this_machine(host: str) -> bool:
    """Whether a stage's host is the machine the run was on, by the engine's
    rule (`emet_engine.metrics`): `localhost`, a loopback address
    (127.0.0.0/8, `::1`) or the unspecified address (`0.0.0.0`, `::`), the
    port optional. Any other host is off the body. A copy, since this reader
    runs on a bare Python with no engine to import."""
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def sent(turns: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """What left the body, summed per stage and host, in the footer's `sent`
    form. A turn whose host is None (a provider on the body) or this machine
    (a model server at 127.0.0.1), or whose amount is 0, adds nothing."""
    totals: dict[tuple[str, str], float] = {}
    for turn in turns:
        for stage, host_key, amount_key, _name in _STAGES:
            host, amount = turn.get(host_key), _numbers([turn], amount_key)
            if isinstance(host, str) and not on_this_machine(host) and amount and amount[0] > 0:
                totals[(stage, host)] = totals.get((stage, host), 0.0) + amount[0]
    out = []
    for stage, _host_key, _amount_key, name in _STAGES:
        for (s, host), amount in totals.items():
            if s == stage:
                out.append({"stage": stage, "host": host,
                            name: round(amount, 3) if name == "audio_s" else int(amount)})
    return out


def summary(events: EventsFile) -> dict[str, Any]:
    """The run's turns, gap, tokens and what was sent, summed from its turn records."""
    turns = events.turns
    return {"turns": len(turns), "gap": gaps(turns), "tokens": tokens(turns), "sent": sent(turns)}


# ------------------------------------------------------------------ printing


def _sent_text(entries: list[dict[str, Any]]) -> str:
    parts = []
    for e in entries:
        if "audio_s" in e:
            parts.append(f"{e['audio_s']:.1f} s of audio to {e['host']}")
        else:
            parts.append(f"{e['chars']} characters to {e['host']}")
    return ", ".join(parts) if parts else "nothing left the body"


def _tokens_text(tin: int | None, tout: int | None, word: str = "") -> str | None:
    """`967 in, 18 out`, or the one side counted, or None for neither, as
    the engine's per-turn line and footer have it. `word` goes after the
    first number."""
    if tin is not None and tout is not None:
        return f"{tin}{word} in, {tout} out"
    if tin is not None:
        return f"{tin}{word} in"
    if tout is not None:
        return f"{tout}{word} out"
    return None


def _turn_line(turn: dict[str, Any]) -> str:
    gap = turn.get("gap_ms")
    bits = [f"gap {gap:.0f} ms" if isinstance(gap, (int, float)) else "gap - (nothing was spoken)"]
    # As the per-turn line has it: both counts, or the one there is.
    counted = _tokens_text(_side([turn], "input_tokens"), _side([turn], "output_tokens"), " tokens")
    if counted is not None:
        bits.append(counted)
    bits.append(_sent_text(sent([turn])))
    return f"  turn {turn.get('turn')}  {turn.get('outcome')}: " + "; ".join(bits)


def summary_lines(events: EventsFile) -> list[str]:
    """What `python tools/events_file.py FILE` prints."""
    h = events.header
    engine = (h.get("packages") or {}).get("emet-engine") if isinstance(h.get("packages"), dict) else None
    lines = [f"{h.get('program')}, {FORMAT} {h.get('version')}, {h.get('platform')}, "
             f"Python {h.get('python')}, emet-engine {engine}"]
    turns = events.turns
    lines += [_turn_line(t) for t in turns]
    s = summary(events)
    lines.append(f"  turns     {s['turns']}")
    gap = s["gap"]
    lines.append("  turn gap  " + (f"median {gap['median_ms']} ms   mean {gap['mean_ms']}   max {gap['max_ms']}   "
                                   f"({gap['n']} turn(s) with a gap)" if gap else "none: nothing was spoken"))
    tok = s["tokens"]
    lines.append("  tokens    " + (f"{_tokens_text(tok['in'], tok['out'])}   ({tok['n']} turn(s) that counted them)"
                                   if tok else "none counted"))
    lines.append(f"  sent      {_sent_text(s['sent'])}")
    if events.footer is None:
        lines.append("  (no footer: the run stopped before it printed one)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="events_file.py",
        description="Print a run's turns, turn gap, tokens and what was sent, from its --events file.")
    parser.add_argument("file", help="the events file a run wrote with --stats --events FILE")
    args = parser.parse_args(argv)
    try:
        events = read(args.file)
    except EventsError as exc:
        print(f"events_file: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"events_file: cannot read {args.file!r}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print("\n".join(summary_lines(events)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
