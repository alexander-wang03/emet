#!/usr/bin/env python3
"""The monthly canary: do the cassettes still say what the vendor says?

A cassette is what Deepgram's model said on the day it was recorded. Once a
month this runs the four live-vendor tests with their recording mode on
(`emet-providers/tests/conftest.py`, `EMET_LIVE_RECORD`), then replays one
cassette turn's exact bytes live through the real Deepgram plugin, and sets
the model version Deepgram reports and the final it gives beside the
cassette's. Either differing prints `stale`: the turns recorded on the old
model go on the next batch's paid list (`verification-design.md` 5.4, P15).

**It spends money only with `--spend`.** Without it, it runs only the
checks that need no network, says what it would spend (the cassette and the
turn, the audio found by its sha256, the request the plugin would make, the
live tests, and the ledger's count for the month), and spends nothing.

**Refused before anything is spent:** a cassette that does not load; a turn
that is not Deepgram's; audio whose sha256 or length is not the turn's; a
turn whose request the plugin would not make today; an output directory
that already holds something; a missing Deepgram key (other vendors' keys
only make their tests skip, which the result reports); a ledger that is
missing, malformed, or holds an entry whose month cannot be read; and a
month whose canary is spent.

**The ledger** is `{"spent": [...]}` with the entries of
`releases/v0.5.3/harness/deepgram-ledger.json`. The allowance is a month's:
20 replay entries and one canary (decided 2026-10-27, D6), unless the
ledger's own `"monthly": {"replays": N, "canaries": N}` says otherwise. An
entry's month is the first seven characters of its `time`; an entry whose
`kind` is `canary` is a canary, and every other entry a replay. The 0.5.3
lifetime cap `allowed` is not read. The canary's entry is written and read
back before any test runs or any connection opens, and it is spent whatever
happens after it.

**The replay** feeds the turn's bytes in 80 ms frames, one a frame's length
apart, as a microphone delivers them; the engine fed the first of them in a
burst, so a partial may come at another moment, and only the final and the
model are compared. Deepgram's Metadata message carries the sha256 of the
audio it received (seen in `dg09`, 2026-10-05), and a replay whose sha256
is not the turn's is a failure, never a verdict.

Usage (Git Bash, from emet/):

    PY=emet-sdk/.venv/Scripts/python.exe
    BANK="../voicebank"
    "$PY" tools/bank/canary.py --ledger "$BANK/ledger.json" --cassette "$BANK/cassettes" \\
        --turn 1 --audio "$BANK/turns" --keys ~/.config/emet/keys.env --out "$BANK/canary/2026-11"
    # then the same line with --spend

Writes `<out>/canary.json` (the verdict, the live turn's messages, each live
test's outcome), `<out>/live/` (the live tests' records) and
`<out>/live-tests.log` and `.xml`.

Exit: 0 fresh with every live test passed; 1 stale, failed, or a live test
that did not pass; 2 refused.
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
import wave
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from emet_sdk.types import AudioFormat

from emet_providers import cassette
from emet_providers.deepgram import DEFAULT_KEY_ENV, FORMAT_KEYS, DeepgramTranscriber

REPO = Path(__file__).resolve().parents[2]
TOP = REPO.parent

#: The allowance a month, decided 2026-10-27 (D6).
REPLAYS_A_MONTH = 20
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
    for n, entry in enumerate(data["spent"], 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("time"), str) or not _MONTH.match(entry["time"]):
            raise Refusal(f"{path.name}: entry {n} has no time its month can be read from")
    allowance(data)
    return data


def allowance(data: dict[str, Any]) -> tuple[int, int]:
    """(replays, canaries) a month."""
    monthly = data.get("monthly")
    if monthly is None:
        return REPLAYS_A_MONTH, CANARIES_A_MONTH
    if not isinstance(monthly, dict):
        raise Refusal('the ledger\'s "monthly" is {"replays": N, "canaries": N}')
    out = []
    for name, default in (("replays", REPLAYS_A_MONTH), ("canaries", CANARIES_A_MONTH)):
        value = monthly.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Refusal(f'the ledger\'s "monthly.{name}" is a whole number, not {value!r}')
        out.append(value)
    return out[0], out[1]


def month_counts(data: dict[str, Any], month: str) -> tuple[int, int]:
    """(replays, canaries) spent in `month`, YYYY-MM.

    Only entries from the ledger's `standing.since` on count, the same rule
    `paid_replay.allowance` keeps: the ten replays of September and October
    2026 were spent under the old ledger's ten, before the monthly allowance
    began on 2026-10-27, and count against nothing now.
    """
    since = str((data.get("standing") or {}).get("since") or "")[:10]
    replays = canaries = 0
    for entry in data["spent"]:
        if _MONTH.match(entry["time"]).group(1) != month or entry["time"][:10] < since:
            continue
        if entry.get("kind") == "canary":
            canaries += 1
        else:
            replays += 1
    return replays, canaries


def canary_allowed(data: dict[str, Any], month: str) -> str:
    """One line on the month's spending, or a `Refusal` when the canary's is spent."""
    replays_allowed, canaries_allowed = allowance(data)
    replays, canaries = month_counts(data, month)
    line = f"{month}: {replays} of {replays_allowed} replays and {canaries} of {canaries_allowed} canaries spent"
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
    """The ledger's time: `2026-10-27 23:40:00 PDT`."""
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


def plugin_for(request: dict[str, Any], connect: Any) -> DeepgramTranscriber:
    """The real Deepgram plugin, set up to make the turn's request, or a
    `Refusal` when it would make another one today."""
    if request["provider"] != "deepgram":
        raise Refusal(f"the turn was recorded from {request['provider']!r}; the canary replays Deepgram's")
    query = {k: v for k, v in request["query"].items() if k not in FORMAT_KEYS}
    stt = DeepgramTranscriber(
        {"provider": "deepgram", "model": request["model"], "params": {"query": query}},
        AudioFormat(sample_rate=request["sample_rate"]),
        connect=connect,
    )
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
    stt = plugin_for(turn["request"], cassette.recording_connect(connect, connections))
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


def compare(turn: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """`fresh`, `stale` (the model version or the final differs), or
    `failed` (the replay cannot be compared)."""

    def failed(reason: str) -> dict[str, Any]:
        return {"verdict": "failed", "reasons": [reason]}

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
    reasons = []
    then, now = turn["model_version"], live["model"]["version"]
    if now != then:
        reasons.append(f"model version: {then} in the cassette, {now} now")
    if final.get("text") != turn["final"]["text"]:
        reasons.append(f"final: {turn['final']['text']!r} in the cassette, {final.get('text')!r} now")
    return {"verdict": "stale" if reasons else "fresh", "reasons": reasons}


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
    """The four live tests in their own process, with recording on."""
    xml, log = out / "live-tests.xml", out / "live-tests.log"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", f"--junitxml={xml}", *LIVE_TESTS]
    with open(log, "w", encoding="utf-8") as handle:
        try:
            subprocess.run(command, cwd=REPO / "emet-providers", env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=900)
        except subprocess.TimeoutExpired:
            handle.write("\ncanary: the live tests took longer than 900 s and were stopped\n")
    return read_junit(xml)


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

    try:
        try:
            turns = cassette.load(args.cassette)
        except cassette.CassetteError as exc:
            raise Refusal(f"--cassette: {exc}") from None
        number, turn = pick_turn(turns, args.turn)
        audio_path, audio = find_audio(args.audio, turn)
        plugin_for(turn["request"], None)
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
            # The loader's message quotes the line, and a line that does not
            # parse may be a bare key. Its place only.
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
    record = {
        "format": "emet-canary/1",
        "time": local_time(started),
        "ledger_entry": number_spent,
        "cassette": entry["cassette"],
        "turn": turn,
        "live_tests": outcomes,
        "live": live,
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
    return 0 if result["verdict"] == "fresh" and passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
