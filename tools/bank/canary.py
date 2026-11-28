#!/usr/bin/env python3
"""Check once a month that the cassettes still say what the vendor says.

A cassette is what Deepgram's model said on the day it was recorded. Once a
month this runs the four live-vendor tests with their recording mode on
(`emet-providers/tests/conftest.py`, `EMET_LIVE_RECORD`), then replays one
cassette turn's exact bytes live through the real Deepgram plugin, and sets
the model version Deepgram reports and the final it gives beside the
cassette's. Either differing prints `stale`: the turns recorded on the old
model go on the next batch's paid list (`verification-design.md` 5.4, P15).
A final differs when its words do. One that differs in case or punctuation
alone stays `fresh` with a note, since Deepgram has given the same bytes
back without their question mark at the same model version (ledger L45).

**It spends money only with `--spend`.** Without it, it runs only the
checks that need no network, says what it would spend (the cassette and the
turn, the audio found by its sha256, the request the plugin would make, the
live tests, and the ledger's count for the month), and spends nothing.

**The request** is the plugin's own, so every Deepgram connection the
canary and its live tests open carries `mip_opt_out=true` (the maintainer,
2026-11-24), and a turn recorded with keyterms is replayed with the same
list in the same order. The language-model tests call Anthropic and
OpenAI, which take no such parameter.

**Refused before anything is spent:** a cassette that does not load; a turn
that is not Deepgram's; audio whose sha256 or length is not the turn's; a
turn whose request the plugin would not make today, a turn recorded before
0.5.6 without `mip_opt_out` among them, named with its date and with the
first turn of the cassette that carries it; a turn whose keyterms the
plugin refuses; an output directory
that already holds something; a missing Deepgram key (other vendors' keys
only make their tests skip, which the result reports); a ledger that is
missing, malformed, or holds an entry whose month cannot be read; and a
month whose canary is spent.

**The ledger** is `{"spent": [...]}` with the entries of
`releases/v0.5.3/harness/deepgram-ledger.json`. The allowance is a month's:
1,000 minutes of replay audio (the maintainer, 2026-11-24, replacing D6's
20 replay entries) and one canary (D6, 2026-10-27), unless the ledger's own
`"monthly": {"replay_minutes": N, "canaries": N}` says otherwise. An entry's
month is the first seven characters of its `time`; an entry whose `kind` is
`canary` is a canary, and one whose `kind` is `replay` a replay counted by
its `audio_s`, as `paid_replay.py` counts them. The 0.5.3 lifetime cap
`allowed` is not read. The canary's entry is written and read
back before any test runs or any connection opens, and it is spent whatever
happens after it.

**The replay** feeds the turn's bytes in 80 ms frames, one a frame's length
apart, as a microphone delivers them; the engine fed the first of them in a
burst, so a partial may come at another moment, and only the final and the
model are compared. Deepgram's Metadata message carries the sha256 of the
audio it received (seen in `dg09`, 2026-10-05), and a replay whose sha256
is not the turn's is a failure, never a verdict.

Usage (Git Bash, from emet/), once 0.5.6's reseed has run. `--cassette`
names the reseed's cassette, and `--audio` the folder of the miss list it
was recorded from, whose raw files are the bytes each turn was fed. Bank
v0's nine cassettes, `dg02.json` to `dg10.json`, predate the opt-out key
and are refused. A directory numbers its turns across every file in name
order, so `--turn 1` of `$BANK/cassettes` is a turn of `dg02.json`; the
refusal names the first turn there that carries the opt-out.

    PY=emet-sdk/.venv/Scripts/python.exe
    BANK="../voicebank"
    "$PY" tools/bank/canary.py --ledger "$BANK/ledger.json" --cassette "$BANK/cassettes/paid-2026-11-26.json" \\
        --turn 1 --audio "$BANK/misses/2026-11-26" --keys ~/.config/emet/keys.env --out "$BANK/canary/2026-11"
    # then the same line with --spend

`--out` is made absolute before anything runs, since the live tests run
from `emet-providers/`: a relative `--out` once put their XML and records
inside the repository's tree and left `canary.json` reading no XML (ledger
L43, 2026-11-27).

**The lifecycle step** comes after the live turn (contract §5.3, MC3): one
free `GET` a row of `emet_providers/lifecycle.py` at the vendor's models
endpoint, which spends no token, with the key the provider reads. It prints
one line a row, `lifecycle  MODEL  VENDOR  WHAT THE VENDOR SAYS (table: DATE
or none)`, and fails a row the vendor does not know (a 404, or a Deepgram
model it does not list) or whose retirement date the table lacks, naming the
row to update. A vendor whose key is absent is `not run: no key`.

Writes `<out>/canary.json` (the verdict, the live turn's messages, each live
test's outcome, each lifecycle row), `<out>/live/` (the live tests' records)
and `<out>/live-tests.log` and `.xml`.

Exit: 0 fresh with every live test passed and every lifecycle row read and
current; 1 stale, failed, a live test that did not pass, or a lifecycle row
that failed or was not run; 2 refused.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from emet_sdk.types import AudioFormat

from emet_providers import cassette
from emet_providers.deepgram import DEFAULT_KEY_ENV, FORMAT_KEYS, DeepgramTranscriber

REPO = Path(__file__).resolve().parents[2]
TOP = REPO.parent

#: The allowance a month: one canary since D6 (2026-10-27), and replay
#: minutes since the maintainer's 2026-11-24 decision, which replaced D6's
#: 20 replay entries of at most 130 s (`paid_replay.py`, STANDING). The
#: ledger's own `monthly` wins over both.
REPLAY_MINUTES_A_MONTH = 1000
CANARIES_A_MONTH = 1

#: The four live-vendor tests, from `emet-providers/`.
LIVE_TESTS = (
    "tests/test_deepgram_live.py::test_a_live_round_trip_returns_a_final",
    "tests/test_llm_live.py::test_anthropic_answers",
    "tests/test_llm_live.py::test_openai_answers",
    "tests/test_tts_live.py::test_deepgram_speaks",
)

#: The key each live test needs. Deepgram's also pays for the replay.
LIVE_KEYS = {
    LIVE_TESTS[0]: DEFAULT_KEY_ENV,
    LIVE_TESTS[1]: "EMET_ANTHROPIC_KEY",
    LIVE_TESTS[2]: "EMET_OPENAI_KEY",
    LIVE_TESTS[3]: DEFAULT_KEY_ENV,
}

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

_MONTH = re.compile(r"^(\d{4}-\d{2})-\d{2}\b")


class Refusal(RuntimeError):
    """Nothing is spent. The message says why."""


def held_keys(text: str) -> list[str]:
    """The `*_KEY` variables whose values appear in `text`, by name. A value
    shorter than 8 characters is too short to search for."""
    return sorted(name for name, value in os.environ.items() if name.upper().endswith("_KEY") and len(value) >= 8 and value in text)


def shown(path: Path) -> str:
    """A path relative to `Project Emet/` when it is inside it, as the
    0.5.3 ledger writes them."""
    path = Path(path).resolve()
    try:
        return path.relative_to(TOP).as_posix()
    except ValueError:
        return path.as_posix()


# ------------------------------------------------------------------- ledger


def ledger_read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Refusal(f"no ledger at {path}: the budget cannot be checked")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Refusal(f"{path.name} is not JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("spent"), list):
        raise Refusal(f'{path.name} does not hold {{"spent": [...]}}')
    since = _since(data)
    for n, entry in enumerate(data["spent"], 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("time"), str) or not _MONTH.match(entry["time"]):
            raise Refusal(f"{path.name}: entry {n} has no time its month can be read from")
        seconds = entry.get("audio_s")
        if (entry.get("kind") == "replay" and entry["time"][:10] >= since
                and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0)):
            raise Refusal(f"{path.name}: entry {n}, a replay under the allowance, has no audio_s to count")
    allowance(data)
    return data


def _since(data: dict[str, Any]) -> str:
    return str((data.get("standing") or {}).get("since") or "")[:10]


def allowance(data: dict[str, Any]) -> tuple[float, int]:
    """(replay minutes, canaries) a month: the ledger's own `monthly`, or
    the standing allowance's when it has none."""
    monthly = data.get("monthly")
    if monthly is None:
        return float(REPLAY_MINUTES_A_MONTH), CANARIES_A_MONTH
    if not isinstance(monthly, dict):
        raise Refusal('the ledger\'s "monthly" is {"replay_minutes": N, "canaries": N}')
    if "replays" in monthly:
        raise Refusal('the ledger\'s "monthly.replays" was an entry count; the allowance has been '
                      '"monthly.replay_minutes" since 2026-11-24')
    minutes = monthly.get("replay_minutes", REPLAY_MINUTES_A_MONTH)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
        raise Refusal(f'the ledger\'s "monthly.replay_minutes" is a number of minutes, not {minutes!r}')
    canaries = monthly.get("canaries", CANARIES_A_MONTH)
    if isinstance(canaries, bool) or not isinstance(canaries, int) or canaries < 0:
        raise Refusal(f'the ledger\'s "monthly.canaries" is a whole number, not {canaries!r}')
    return float(minutes), canaries


def month_counts(data: dict[str, Any], month: str) -> tuple[float, int]:
    """(replay minutes, canaries) spent in `month`, YYYY-MM.

    The rule `paid_replay.allowance` keeps: a replay is an entry whose
    `kind` is `replay`, counted by its `audio_s`, and a canary one whose
    `kind` is `canary`. Only entries from the ledger's `standing.since` on
    count: the ten replays of September and October 2026 were spent under
    the old ledger's ten, before the monthly allowance began on 2026-10-27,
    and count against nothing now.
    """
    since = _since(data)
    seconds, canaries = 0.0, 0
    for entry in data["spent"]:
        if _MONTH.match(entry["time"]).group(1) != month or entry["time"][:10] < since:
            continue
        if entry.get("kind") == "canary":
            canaries += 1
        elif entry.get("kind") == "replay":
            seconds += float(entry["audio_s"])
    return seconds / 60.0, canaries


def canary_allowed(data: dict[str, Any], month: str) -> str:
    """One line on the month's spending, or a `Refusal` when the canary's is spent."""
    minutes_allowed, canaries_allowed = allowance(data)
    minutes, canaries = month_counts(data, month)
    line = (f"{month}: {minutes:.1f} of {minutes_allowed:g} replay minutes and "
            f"{canaries} of {canaries_allowed} canaries spent")
    if canaries >= canaries_allowed:
        raise Refusal(f"the ledger's canary for {month} is spent ({line})")
    return line


def ledger_spend(path: Path, entry: dict[str, Any], month: str) -> int:
    """Append the entry and read it back, or refuse. Returns its number."""
    data = ledger_read(path)
    canary_allowed(data, month)
    data["spent"].append(entry)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    back = ledger_read(path)
    if back["spent"][-1] != entry or len(back["spent"]) != len(data["spent"]):
        raise Refusal("the new entry did not read back")
    return len(back["spent"])


def local_time(now: datetime.datetime) -> str:
    """The ledger's time, as `2026-10-27 23:40:00 PDT`."""
    name = now.tzname() or ""
    if " " in name:  # Windows says "Pacific Daylight Time"
        name = "".join(word[0] for word in name.split())
    return now.strftime("%Y-%m-%d %H:%M:%S ") + name


#: What `git` runs with: `GIT_OPTIONAL_LOCKS=0` keeps `git status` from
#: writing the index, so the canary never holds the lock while the
#: maintainer stages a change in the same tree, and `LC_ALL=C` keeps its
#: messages in English. The environment `emet_engine.cli` gives git too.
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def engine_info() -> dict[str, Any]:
    """The tree that ran: HEAD, whether it was clean, the packages' versions.
    Read-only git."""

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(REPO), *args],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
                env={**os.environ, **GIT_ENV},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = git("status", "--porcelain")
    versions: dict[str, str | None] = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"repo_head": git("rev-parse", "HEAD"), "tree_clean": None if status is None else status == "", "package_versions": versions}


# -------------------------------------------------------------------- turn


def pick_turn(turns: list[dict[str, Any]], selector: str) -> tuple[int, dict[str, Any]]:
    """`selector` is a 1-based number, or at least 8 hex digits of a turn's
    `fed_sha256`, which stays right when files are added to a directory."""
    if selector.isdigit() and len(selector) < 8:
        n = int(selector)
        if not 1 <= n <= len(turns):
            raise Refusal(f"--turn {n}: the cassette holds {len(turns)} turns")
        return n, turns[n - 1]
    if not re.fullmatch(r"[0-9a-f]{8,64}", selector):
        raise Refusal(f"--turn {selector}: a turn's number, or 8 to 64 hex digits of its fed_sha256")
    found = [(n, t) for n, t in enumerate(turns, 1) if t["fed_sha256"].startswith(selector)]
    if len(found) != 1:
        raise Refusal(f"--turn {selector}: {len(found)} turns' fed_sha256 start so; name one")
    return found[0]


def with_opt_out(turns: list[dict[str, Any]]) -> str:
    """Which of the cassette's turns carry the opt-out, for the refusal of
    one that does not: how many, and the first by its `--turn` number."""
    numbers = [n for n, t in enumerate(turns, 1) if not cassette.predates_opt_out(t["request"])]
    if not numbers:
        return f"None of the {len(turns)} turn(s) here carries mip_opt_out"
    verb = "carries" if len(numbers) == 1 else "carry"
    return f"Of the {len(turns)} turn(s) here, {len(numbers)} {verb} mip_opt_out, the first --turn {numbers[0]}"


def read_audio(path: Path, rate: int) -> bytes | None:
    """A file's samples: a .wav's frames, mono 16-bit at `rate`, or a raw
    file's bytes. None for a .wav of another shape."""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != rate:
                return None
            return w.readframes(w.getnframes())
    return path.read_bytes()


def find_audio(path: Path, turn: dict[str, Any]) -> tuple[Path, bytes]:
    """The turn's bytes: `path` itself, or the first `.raw` or `.wav` in the
    directory whose samples hash to the turn's `fed_sha256`."""
    rate = turn["request"]["sample_rate"]
    want, size = turn["fed_sha256"], turn["fed_bytes"]
    if path.is_dir():
        candidates = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in (".raw", ".wav"))
    elif path.is_file():
        candidates = [path]
    else:
        raise Refusal(f"--audio {path}: no such file or directory")
    for candidate in candidates:
        data = read_audio(candidate, rate)
        if data is not None and len(data) == size and hashlib.sha256(data).hexdigest() == want:
            return candidate, data
    if path.is_file():
        data = read_audio(path, rate)
        got = "not mono 16-bit at the turn's rate" if data is None else f"{len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()[:12]}"
        raise Refusal(f"--audio {path}: {got}; the turn is {size} bytes, sha256 {want[:12]}")
    raise Refusal(f"--audio {path}: no .raw or .wav there holds the turn's {size} bytes, sha256 {want[:12]}")


def plugin_for(request: dict[str, Any], connect: Any, *, recorded: str | None = None) -> DeepgramTranscriber:
    """The real Deepgram plugin, set up to make the turn's request, or a
    `Refusal` when it would make another one today.

    The turn's `keyterm` list becomes the plugin's `params.keyterms`, and
    `mip_opt_out` is the plugin's own, sent with every request: a turn
    recorded without it predates the opt-out key, and replaying it live
    would ask Deepgram another question, so it is refused with the day it
    was recorded."""
    if request["provider"] != "deepgram":
        raise Refusal(f"the turn was recorded from {request['provider']!r}; the canary replays Deepgram's")
    if cassette.predates_opt_out(request):
        raise Refusal(
            f"the turn was recorded on {recorded or 'a day before 0.5.6'} for a request without mip_opt_out, and the "
            f"plugin sends mip_opt_out=true with every request from 0.5.6 on, so a live replay would ask Deepgram "
            f"another question. Pick a turn recorded from 0.5.6 on with --turn; tools/bank/paid_replay.py writes "
            f"them")
    skip = FORMAT_KEYS | {cassette.KEYTERM, cassette.OPT_OUT}
    params: dict[str, Any] = {"query": {k: v for k, v in request["query"].items() if k not in skip}}
    if cassette.KEYTERM in request["query"]:
        params["keyterms"] = list(request["query"][cassette.KEYTERM])
    stt = DeepgramTranscriber(
        {"provider": "deepgram", "model": request["model"], "params": params},
        AudioFormat(sample_rate=request["sample_rate"]),
        connect=connect,
    )
    if stt.refusal:
        raise Refusal(f"the plugin refuses the turn's request: {stt.refusal}")
    asked = cassette.request_for("deepgram", stt.query())
    if asked != request:
        raise Refusal(f"the plugin would ask for {asked}, and the turn was recorded for {request}")
    return stt


# ------------------------------------------------------------------ replay


def _metadata_sha256(received: list[dict[str, Any]]) -> str | None:
    for item in reversed(received):
        try:
            data = json.loads(item.get("text") or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "Metadata" and isinstance(data.get("sha256"), str):
            return data["sha256"]
    return None


async def replay(
    turn: dict[str, Any],
    audio: bytes,
    connect: Any,
    sleep: Callable[[float], Any],
) -> dict[str, Any]:
    """Feed the turn's bytes through the real plugin. Never raises for a
    vendor failure: the reason is in `error`."""
    connections: list[dict[str, Any]] = []
    stt = plugin_for(turn["request"], cassette.recording_connect(connect, connections), recorded=turn.get("recorded"))
    fmt = AudioFormat(sample_rate=turn["request"]["sample_rate"])
    out: dict[str, Any] = {"error": None, "final": None, "partials": [], "model": None,
                           "sent_sha256": None, "deepgram_sha256": None, "connections": connections}
    await stt.start()
    if not stt.describe().healthy:
        out["error"] = stt.health().detail
        return out
    try:
        for start in range(0, len(audio), fmt.frame_bytes):
            partial = await stt.feed(audio[start:start + fmt.frame_bytes])
            if partial is not None:
                out["partials"].append({"after_bytes": min(start + fmt.frame_bytes, len(audio)), "text": partial.text})
            await sleep(fmt.frame_ms / 1000.0)
        final = await stt.finish()
    finally:
        await stt.shutdown()
    out["final"] = {"text": final.text, "confidence": final.confidence, "error": final.error}
    utterance = [c for c in connections if c["audio_bytes_sent"]]
    received = [m for c in utterance for m in c["received"]]
    out["model"] = cassette.model_version(received)
    out["sent_sha256"] = utterance[-1]["audio_sha256"] if utterance else None
    out["deepgram_sha256"] = _metadata_sha256(received)
    return out


#: A word of a final as two finals are compared: a run of letters and digits.
WORD = re.compile(r"[^\W_]+")


def final_words(text: str | None) -> list[str]:
    """A final's words, case folded, with its punctuation and the apostrophe
    inside a word dropped. Deepgram gave 2 of dg08 and dg09's 17 finals
    again without their question mark, at the same model version and from
    the same bytes (ledger L45, 2026-11-27), so the punctuation alone says
    nothing about the model."""
    return WORD.findall((text or "").casefold().replace("'", "").replace("’", ""))


def compare(turn: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """`fresh`, `stale` (the model version or the final's words differ), or
    `failed` (the replay cannot be compared). A final that differs in its
    case or punctuation alone stays fresh, with a note that says so."""

    def failed(reason: str) -> dict[str, Any]:
        return {"verdict": "failed", "reasons": [reason], "notes": []}

    if live.get("error"):
        return failed(f"the replay did not run: {live['error']}")
    final = live.get("final") or {}
    if final.get("error"):
        return failed(f"the live final carries an error: {final['error']}")
    if live.get("sent_sha256") != turn["fed_sha256"]:
        return failed("the bytes sent are not the turn's")
    if live.get("deepgram_sha256") not in (None, turn["fed_sha256"]):
        return failed("Deepgram's Metadata names other bytes than the turn's")
    if not live.get("model") or not live["model"].get("version"):
        return failed("Deepgram reported no model version")
    reasons, notes = [], []
    then, now = turn["model_version"], live["model"]["version"]
    if now != then:
        reasons.append(f"model version: {then} in the cassette, {now} now")
    shown = f"{turn['final']['text']!r} in the cassette, {final.get('text')!r} now"
    if final_words(final.get("text")) != final_words(turn["final"]["text"]):
        reasons.append(f"final: {shown}")
    elif final.get("text") != turn["final"]["text"]:
        notes.append(f"final: {shown}, the same words")
    return {"verdict": "stale" if reasons else "fresh", "reasons": reasons, "notes": notes}


# -------------------------------------------------------------- live tests


def read_junit(path: Path) -> dict[str, str]:
    """Each live test's outcome from pytest's JUnit XML: passed, failed,
    skipped, or not run."""
    outcomes = {test: "not run" for test in LIVE_TESTS}
    if not path.is_file():
        return outcomes
    by_name = {test.split("::")[-1]: test for test in LIVE_TESTS}
    for case in ET.parse(path).getroot().iter("testcase"):
        test = by_name.get(case.get("name", ""))
        if test is None:
            continue
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[test] = "failed"
        elif case.find("skipped") is not None:
            outcomes[test] = "skipped"
        else:
            outcomes[test] = "passed"
    return outcomes


def run_live_tests(env: dict[str, str], out: Path) -> dict[str, str]:
    """The four live tests in their own process, with recording on. The
    process runs from `emet-providers/`, so the XML's path is made absolute
    first and the XML read back is the one it wrote (ledger L43)."""
    out = Path(out).resolve()
    xml, log = out / "live-tests.xml", out / "live-tests.log"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", f"--junitxml={xml}", *LIVE_TESTS]
    with open(log, "w", encoding="utf-8") as handle:
        try:
            subprocess.run(command, cwd=REPO / "emet-providers", env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=900)
        except subprocess.TimeoutExpired:
            handle.write("\ncanary: the live tests took longer than 900 s and were stopped\n")
    return read_junit(xml)


# --------------------------------------------------------------- lifecycle

#: The lifecycle step's limit for each GET, in seconds (contract §5.3).
LIFECYCLE_TIMEOUT_S = 20

#: Each vendor's models endpoint, read at its API reference on 2026-11-27.
#: OpenAI's "Retrieve model", `GET /v1/models/{model}` with a Bearer key,
#: answers `shutdown_date`, a date or null. Anthropic's "Get a Model",
#: `GET /v1/models/{model_id}` with `x-api-key` and `anthropic-version`,
#: answers `lifecycle` (`active`, `deprecated` or `retired`) and
#: `retires_at`, an RFC 3339 time or null. Deepgram's "List Models",
#: `GET /v1/models` with `Authorization: Token`, lists `stt` and `tts`
#: models by `canonical_name` and states no retirement date; its "Get a
#: Model" takes a model's UUID and never its name, so the list is read.
#: Each is free and spends no token.
LIFECYCLE_URLS = {
    "openai": "https://api.openai.com/v1/models/{model}",
    "anthropic": "https://api.anthropic.com/v1/models/{model}",
    "deepgram": "https://api.deepgram.com/v1/models",
}


def lifecycle_defaults() -> tuple[Any, ...]:
    """`emet_providers.lifecycle.DEFAULTS`, the table release_check.py holds
    the defaults to."""
    from emet_providers import lifecycle

    return tuple(lifecycle.DEFAULTS)


def lifecycle_keys() -> dict[str, str]:
    """The variable each vendor's key is read from, the provider's own."""
    from emet_providers import anthropic, deepgram, openai

    return {"openai": openai.DEFAULT_KEY_ENV, "anthropic": anthropic.DEFAULT_KEY_ENV,
            "deepgram": deepgram.DEFAULT_KEY_ENV}


def lifecycle_headers(vendor: str, key: str) -> dict[str, str]:
    if vendor == "anthropic":
        from emet_providers import anthropic

        return {"x-api-key": key, "anthropic-version": anthropic.API_VERSION}
    if vendor == "deepgram":
        return {"Authorization": f"Token {key}"}
    return {"Authorization": f"Bearer {key}"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect. urllib's own handler copies every header but
    Content-Length and Content-Type into the request it sends to the new
    place, the key's header among them, and takes any http, https or ftp
    address, plain http after https included (CPython 3.13's
    `urllib/request.py`, `redirect_request` and `http_error_302`, read
    2026-11-27). The providers send the same keys through httpx, which
    follows no redirect unless asked. So a 3xx here is the vendor's answer
    and a failed row, and the key goes to no other place."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_url(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """One GET, its status and its body. An HTTP error is a status and never
    an exception, a redirect among them, since none is followed; a network
    failure raises `OSError`."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


def vendor_says(vendor: str, model: str, body: Any) -> tuple[str, str | None, bool]:
    """What the vendor's answer says of one model. It returns the words for
    the line, the retirement date given (YYYY-MM-DD) or None, and whether
    the vendor knows the model and still serves it."""
    if not isinstance(body, dict):
        return "an answer that is not a JSON object", None, False
    if vendor == "openai":
        date = body.get("shutdown_date")
        date = str(date)[:10] if date else None
        return (f"shuts down on {date}" if date else "known, no shutdown date announced"), date, True
    if vendor == "anthropic":
        stage = str(body.get("lifecycle") or "active")
        date = body.get("retires_at")
        date = str(date)[:10] if date else None
        words = f"{stage}, retires on {date}" if date else f"{stage}, no retirement scheduled"
        return words, date, stage != "retired"
    listed = [m for kind in ("stt", "tts") for m in body.get(kind) or [] if isinstance(m, dict)]
    if any(m.get("canonical_name") == model for m in listed):
        return "listed, and Deepgram states no retirement date", None, True
    return f"not listed among Deepgram's {len(listed)} models", None, False


def lifecycle_check(rows: tuple[Any, ...], keys: dict[str, str], env: dict[str, str],
                    fetch: Callable[[str, dict[str, str], float], tuple[int, bytes]] = fetch_url
                    ) -> list[dict[str, Any]]:
    """One GET a row, and the row's verdict. A row is `ok`; `failed`, when
    the vendor does not know the model, retires it on a date the table
    lacks, or could not be read; or `not run`. The key is sent and never
    kept or printed. Deepgram's list is fetched once for all its rows."""
    results = []
    lists: dict[str, tuple[int | None, Any, str | None]] = {}
    for row in rows:
        vendor, model = str(row.vendor).casefold(), str(row.model)
        table = row.retires or "none"
        result: dict[str, Any] = {"model": model, "vendor": vendor, "table": row.retires, "says": None,
                                  "vendor_retires": None, "verdict": "ok", "reason": None}
        results.append(result)
        key_env = keys.get(vendor)
        if vendor not in LIFECYCLE_URLS or key_env is None:
            result.update(verdict="not run", says="not run: no models endpoint is known for this vendor")
            continue
        if not env.get(key_env):
            result.update(verdict="not run", says="not run: no key")
            continue
        url = LIFECYCLE_URLS[vendor].format(model=urllib.parse.quote(model, safe=""))
        if url not in lists:
            try:
                status, raw = fetch(url, lifecycle_headers(vendor, env[key_env]), LIFECYCLE_TIMEOUT_S)
            except OSError as exc:
                lists[url] = (None, None, f"{type(exc).__name__}: {exc}")
            else:
                try:
                    body = json.loads(raw.decode("utf-8") or "null")
                except ValueError:
                    body = None
                lists[url] = (status, body, None)
        status, body, error = lists[url]
        if error is not None:
            result.update(verdict="failed", says=f"could not be read ({error})",
                          reason="the vendor could not be read, so the row is unchecked")
        elif status == 404:
            result.update(verdict="failed", says="HTTP 404: the vendor does not know the model",
                          reason="the vendor does not know the model; choose another default")
        elif status != 200:
            result.update(verdict="failed", says=f"HTTP {status}, and no answer about the model",
                          reason="the vendor could not be read, so the row is unchecked")
        else:
            words, date, known = vendor_says(vendor, model, body)
            result.update(says=words, vendor_retires=date)
            if not known:
                result.update(verdict="failed", reason="the vendor no longer serves the model; choose another default")
            elif date and date != row.retires:
                result.update(verdict="failed",
                              reason=f"the vendor retires it on {date}, and the table says {table}. Record "
                                     f"{date} with its source and the day it was read")
    return results


def lifecycle_lines(results: list[dict[str, Any]]) -> list[str]:
    """The step's lines, one a row, with the row to update under a failed one."""
    lines = []
    for r in results:
        lines.append(f"lifecycle  {r['model']}  {r['vendor']}  {r['says']} (table: {r['table'] or 'none'})")
        if r["verdict"] == "failed":
            lines.append(f"  update the row for {r['model']!r} in emet-providers/emet_providers/lifecycle.py: "
                         f"{r['reason']}")
    return lines


# -------------------------------------------------------------------- main


def _network_connect() -> Any:
    from websockets.asyncio.client import connect

    return connect


def main(
    argv: list[str] | None = None,
    *,
    run_tests: Callable[[dict[str, str], Path], dict[str, str]] = run_live_tests,
    connect: Any = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now().astimezone(),
    engine: Callable[[], dict[str, Any]] = engine_info,
    rows: Callable[[], tuple[Any, ...]] = lifecycle_defaults,
    fetch: Callable[[str, dict[str, str], float], tuple[int, bytes]] = fetch_url,
) -> int:
    parser = argparse.ArgumentParser(description="The monthly canary. Spends money only with --spend.")
    parser.add_argument("--ledger", required=True, type=Path, help="the Deepgram ledger")
    parser.add_argument("--cassette", required=True, type=Path, help="a cassette file, or a directory of them")
    parser.add_argument("--turn", required=True, help="the turn's number, or hex digits of its fed_sha256")
    parser.add_argument("--audio", required=True, type=Path, help="the turn's bytes: a .raw or .wav, or a directory holding one")
    parser.add_argument("--out", required=True, type=Path, help="a new directory for this canary's records")
    parser.add_argument("--keys", type=Path, help="the keys file; without it, the default places")
    parser.add_argument("--spend", action="store_true", help="run the live tests and the live turn, and write the ledger")
    args = parser.parse_args(argv)
    # The live tests run from emet-providers/, so a relative --out would be
    # read there by them and here by this process (ledger L43).
    args.out = args.out.resolve()

    try:
        try:
            turns = cassette.load(args.cassette)
        except cassette.CassetteError as exc:
            raise Refusal(f"--cassette: {exc}") from None
        number, turn = pick_turn(turns, args.turn)
        try:
            plugin_for(turn["request"], None, recorded=turn["recorded"])
        except Refusal as exc:
            if not cassette.predates_opt_out(turn["request"]):
                raise
            raise Refusal(f"{exc}. {with_opt_out(turns)}") from None
        audio_path, audio = find_audio(args.audio, turn)
        if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
            raise Refusal(f"--out {args.out}: already holds something; give each canary a directory of its own")
        started = now()
        month = started.strftime("%Y-%m")
        spending = canary_allowed(ledger_read(args.ledger), month)

        print(f"canary: turn {number} of {shown(args.cassette)}, fed_sha256 {turn['fed_sha256'][:12]}, "
              f"{turn['fed_bytes']} bytes, model {turn['model_version']}, recorded {turn['recorded']}")
        print(f"canary: from {turn['source']}")
        print(f"canary: audio {shown(audio_path)}, sha256 matches")
        print(f"canary: the request {json.dumps(turn['request'], sort_keys=True)}")
        print(f"canary: ledger {shown(args.ledger)}, {spending}")
        print(f"canary: would run {len(LIVE_TESTS)} live tests and stream {turn['fed_bytes'] / 2 / turn['request']['sample_rate']:.1f} s to Deepgram")
        if not args.spend:
            raise Refusal("the canary spends money. Run it again with --spend")

        try:
            from emet_engine.keys import load_keys

            loaded = load_keys(args.keys)
        except FileNotFoundError as exc:
            raise Refusal(f"--keys: {exc}") from None
        except ValueError as exc:
            # The loader names the line by its number and never quotes it;
            # its place is enough here.
            where = re.match(r"^(.*:\d+):", str(exc))
            raise Refusal(f"--keys: {where.group(1) if where else 'the keys file'}: a line that does not parse") from None
        for keys in loaded:
            print(f"canary: keys from {keys.path}: {', '.join(keys.names) or 'none new'}")
        if not os.environ.get(DEFAULT_KEY_ENV):
            raise Refusal(f"no {DEFAULT_KEY_ENV}: the live turn needs it")
        missing = sorted({name for name in LIVE_KEYS.values() if not os.environ.get(name)})
        if missing:
            print(f"canary: no {', '.join(missing)}: those live tests will skip, and the canary will not pass")
        if connect is None:
            try:
                connect = _network_connect()
            except ImportError:
                raise Refusal("the websockets library is not installed: install emet-providers[deepgram]") from None

        entry = {
            "time": local_time(started),
            "kind": "canary",
            "recording": shown(audio_path),
            "sha256": turn["fed_sha256"],
            "engine": engine(),
            "stt_query": turn["request"]["query"],
            "cassette": {"path": shown(args.cassette), "turn": number, "source": turn["source"],
                         "model_version": turn["model_version"], "recorded": turn["recorded"]},
            "live_tests": list(LIVE_TESTS),
            "paced": True,
            "result_file": shown(args.out / "canary.json"),
            "note": "The monthly canary: the four live tests with recording on, and one cassette turn replayed live through the Deepgram plugin.",
        }
        number_spent = ledger_spend(args.ledger, entry, month)
    except Refusal as exc:
        print(f"refused: {exc}")
        return 2
    print(f"canary: ledger entry {number_spent} written and read back")

    args.out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "EMET_LIVE_TESTS": "1", "EMET_LIVE_RECORD": str(args.out / "live")}
    outcomes = run_tests(env, args.out)
    passed = sum(1 for o in outcomes.values() if o == "passed")
    print(f"canary: live tests {passed} of {len(outcomes)} passed")
    for test, outcome in outcomes.items():
        if outcome != "passed":
            print(f"  {outcome}: {test}")

    live = asyncio.run(replay(turn, audio, connect, sleep))
    result = compare(turn, live)
    try:
        table: tuple[Any, ...] | None = rows()
    except (ImportError, AttributeError) as exc:
        table = None
        print(f"lifecycle  not run: the defaults table could not be read ({type(exc).__name__}: {exc})")
    checked = [] if table is None else lifecycle_check(table, lifecycle_keys(), dict(os.environ), fetch)
    for line in lifecycle_lines(checked):
        print(line)
    current = table is not None and all(r["verdict"] == "ok" for r in checked)
    record = {
        "format": "emet-canary/1",
        "time": local_time(started),
        "ledger_entry": number_spent,
        "cassette": entry["cassette"],
        "turn": turn,
        "live_tests": outcomes,
        "live": live,
        "lifecycle": checked,
        **result,
    }
    text = json.dumps(record, indent=1, ensure_ascii=False) + "\n"
    holding = held_keys(text)
    if holding:
        # The final and the reasons quote what came back, so they are
        # withheld from the screen too. Names only.
        print(f"canary: the result holds the value of {', '.join(holding)}; canary.json was not written")
        print(result["verdict"])
        return 1
    (args.out / "canary.json").write_text(text, encoding="utf-8")
    final = (live.get("final") or {}).get("text")
    version = (live.get("model") or {}).get("version")
    print(f"canary: live turn: model {version} (cassette {turn['model_version']}), final {final!r} (cassette {turn['final']['text']!r})")
    print(result["verdict"])
    for reason in result["reasons"]:
        print(f"  {reason}")
    for note in result["notes"]:
        print(f"  note: {note}")
    return 0 if result["verdict"] == "fresh" and passed == len(outcomes) and current else 1


if __name__ == "__main__":
    sys.exit(main())
