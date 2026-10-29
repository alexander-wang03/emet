"""A transcriber's recorded answers, so a replay can give them again for nothing.

A cassette is what a real transcriber said for exact bytes. The mock
transcriber plays one back (`params.cassette`), and the bank's tools write
them from paid runs. The file format is `tools/bank/FORMAT.md`, section
"Cassettes", and a file is held to it exactly: every field present and no
other, as `tools/bank/manifest.py` checks it, so the mock and the bank's
checker never disagree about a file.

**The key is the bytes and the request.** A turn is found by the sha256 and
the length of every byte fed in the utterance, among the turns whose
`request` (provider, model, sample rate, query) equals the one the run
stands for. A cut that moves by one sample changes the hash, so a replay
gets the recorded answer only for the audio that was really sent, and
anything else is a miss for the runner to put on the paid list.

**Partials are checked once.** A recorded partial carries the total bytes
fed at the end of the `feed()` call that returned it (`after_bytes`) and the
sha256 of those bytes. The running hash is compared only when the total
reaches one of those numbers, so most frames cost one `update()`: 1.90 us
median for an 80 ms frame of 2560 bytes, and 0.55 us for a digest of the
running state (Windows 11 laptop, Python 3.13.2, 2026-10-28); 1.83 us and
0.46 us on the reference body (Raspberry Pi 5, Python 3.13.5, load 0.06,
2026-10-28). The mock's whole `feed()` with a 100-turn cassette took a
median 2.60 us a frame on the laptop and 2.56 us on the body, against
0.60 us and 0.59 us without one. The script and its output are in
`releases/v0.5.5/timings/`, outside this repository.

**No key is ever kept.** A query parameter whose name contains `key`,
`token`, `auth`, `secret` or `pass` is refused on load and on save, the rule
`cut_deepgram.py` kept its URLs by (0.5.3) and the bank's checker applies.
It refuses Deepgram's `keyterm` too, so a body that takes `keyterm` up
changes this rule and the checker's together.

Cassettes hold what the maintainer said, so they live in the bank
(`<bank>/cassettes/`) and never in this repository.

The idea and the name, a recorded exchange replayed when the same request
comes again, are those of Ruby's VCR (Hippocratic License 2.1) and Python's
vcrpy (MIT), both checked 2026-10-27; no code is taken from either, and
neither is a dependency. See `CITATIONS.md`.

`recording_connect()` is for the writers: it wraps a WebSocket `connect` so
every message a live Deepgram connection received is kept as it came, with
its time, and the headers (where the key travels) are never seen.
`model_version()` reads the version Deepgram reported out of those messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "FORMAT",
    "MISS",
    "CassetteError",
    "RunningHash",
    "TurnRecorder",
    "Player",
    "Played",
    "sha256_hex",
    "normalise_request",
    "request_for",
    "check_turn",
    "load",
    "load_file",
    "save",
    "safe_url",
    "recording_connect",
    "model_version",
]

FORMAT = "emet-cassette/1"

#: The error a missed turn's final carries. The runner reads it to build the
#: paid list, so it is spelled once, here.
MISS = "cassette miss"

#: A name that could carry a credential, matched anywhere in it.
_CREDENTIAL = re.compile(r"key|token|auth|secret|pass", re.I)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: The fields of each part, exactly, in FORMAT.md's order.
REQUEST_FIELDS = ("provider", "model", "sample_rate", "query")
TURN_FIELDS = ("request", "fed_sha256", "fed_bytes", "partials", "final", "model_version", "recorded", "ledger_entry", "source")
PARTIAL_FIELDS = ("text", "after_bytes", "prefix_sha256")
FINAL_FIELDS = ("text", "confidence", "error")


class CassetteError(ValueError):
    """A cassette that cannot be read or written. The message says why."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RunningHash:
    """The sha256 of every byte fed so far, and how many there were.

    `hexdigest()` leaves the state as it was, so the next `update()` carries
    on the same stream (hashlib's own behaviour, checked 2026-10-27).
    """

    __slots__ = ("_hash", "total")

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self.total = 0

    def update(self, data: bytes) -> int:
        self._hash.update(data)
        self.total += len(data)
        return self.total

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


# ------------------------------------------------------------------ fields


def _exact(doc: Mapping[str, Any], fields: tuple[str, ...], where: str) -> None:
    """Every field present and no other: a misspelt field is refused, never
    quietly ignored."""
    for name in fields:
        if name not in doc:
            raise CassetteError(f"{where} has no {name!r}")
    for name in doc:
        if name not in fields:
            raise CassetteError(f"{where} has an unknown field {name!r}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any) -> str:
    """A query value the way the Deepgram plugin sends it: booleans in lower
    case, everything else as `str()` gives it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ------------------------------------------------------------------ request


def normalise_request(request: Any) -> dict[str, Any]:
    """A request in the one shape two requests are compared in.

    It has exactly `provider`, `model`, `sample_rate` and `query`. Query
    values become strings as the plugin sends them, so a body's YAML may
    write `smart_format: false`, and the query's keys are sorted;
    `sample_rate` may be written as a string. Raises `CassetteError` for a
    missing or unknown field, or a query parameter named like a credential.
    """
    if not isinstance(request, Mapping):
        raise CassetteError(f"a request is a mapping, not {type(request).__name__}")
    _exact(request, REQUEST_FIELDS, "the request")
    provider, model, rate, query = (request[name] for name in REQUEST_FIELDS)
    if not isinstance(provider, str) or not provider:
        raise CassetteError("the request's provider is text")
    if model is not None and not isinstance(model, str):
        raise CassetteError("the request's model is text or null")
    if isinstance(rate, str) and rate.isdigit():
        rate = int(rate)
    if not _is_int(rate) or rate <= 0:
        raise CassetteError(f"the request's sample_rate is a number, not {rate!r}")
    if not isinstance(query, Mapping):
        raise CassetteError("the request's query is a mapping")
    for key in query:
        if _CREDENTIAL.search(str(key)):
            raise CassetteError(f"the request's query names {str(key)!r}, which could carry a credential; a cassette never keeps one")
    return {
        "provider": provider,
        "model": model,
        "sample_rate": rate,
        "query": {str(k): _text(query[k]) for k in sorted(query, key=str)},
    }


def request_for(provider: str, query: Mapping[str, Any]) -> dict[str, Any]:
    """The request a plugin made, from the whole query it sent.

    `model` and `sample_rate` move out of the query into fields of their
    own; every other parameter the plugin sends stays, its fixed ones
    included, so two runs whose plugins sent different defaults never share
    a turn. For Deepgram, `query` is `DeepgramTranscriber.query()`.
    """
    rest = {str(k): v for k, v in query.items() if str(k) not in ("model", "sample_rate")}
    return normalise_request(
        {"provider": provider, "model": query.get("model"), "sample_rate": query.get("sample_rate"), "query": rest}
    )


# --------------------------------------------------------------------- turn


def _hex64(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise CassetteError(f"{what} is a sha256 in lower-case hex, not {value!r}")
    return value


def _count(value: Any, what: str, *, least: int = 0) -> int:
    if not _is_int(value) or value < least:
        raise CassetteError(f"{what} is a whole number of at least {least}, not {value!r}")
    return value


def check_turn(turn: Any) -> dict[str, Any]:
    """One turn, checked against FORMAT.md, with its request normalised.

    Stricter than the format in two ways a file never needs: partials rise
    strictly, since one comes from each `feed()` call, and a partial that
    covers every byte of the turn has the turn's own hash. Raises
    `CassetteError` naming the first thing wrong.
    """
    if not isinstance(turn, Mapping):
        raise CassetteError(f"a turn is a mapping, not {type(turn).__name__}")
    _exact(turn, TURN_FIELDS, "the turn")
    request = turn["request"]
    if isinstance(request, Mapping) and isinstance(request.get("query"), Mapping):
        if not all(isinstance(v, str) for v in request["query"].values()):
            raise CassetteError("the request's query maps text to text in a file")
    out: dict[str, Any] = {"request": normalise_request(request)}
    out["fed_sha256"] = _hex64(turn["fed_sha256"], "fed_sha256")
    out["fed_bytes"] = fed = _count(turn["fed_bytes"], "fed_bytes")

    partials = turn["partials"]
    if not isinstance(partials, list):
        raise CassetteError("partials is a list")
    checked: list[dict[str, Any]] = []
    last = 0
    for n, partial in enumerate(partials, 1):
        if not isinstance(partial, Mapping):
            raise CassetteError(f"partial {n} is a mapping")
        _exact(partial, PARTIAL_FIELDS, f"partial {n}")
        if not isinstance(partial["text"], str):
            raise CassetteError(f"partial {n}'s text is a string")
        after = _count(partial["after_bytes"], f"partial {n}'s after_bytes", least=1)
        if after <= last:
            raise CassetteError(f"partial {n} comes after {after} bytes, and partial {n - 1} after {last}: they rise")
        if after > fed:
            raise CassetteError(f"partial {n} comes after {after} bytes, past the turn's {fed}")
        prefix = _hex64(partial["prefix_sha256"], f"partial {n}'s prefix_sha256")
        if after == fed and prefix != out["fed_sha256"]:
            raise CassetteError(f"partial {n} covers every byte of the turn and its hash is not the turn's")
        checked.append({"text": partial["text"], "after_bytes": after, "prefix_sha256": prefix})
        last = after
    out["partials"] = checked

    final = turn["final"]
    if not isinstance(final, Mapping):
        raise CassetteError("final is a mapping")
    _exact(final, FINAL_FIELDS, "the final")
    confidence, error = final["confidence"], final["error"]
    if not isinstance(final["text"], str):
        raise CassetteError("the final's text is a string")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))):
        raise CassetteError("the final's confidence is a number or null")
    if error is not None and not isinstance(error, str):
        raise CassetteError("the final's error is a string or null")
    out["final"] = {"text": final["text"], "confidence": confidence, "error": error}

    if turn["model_version"] is not None and not isinstance(turn["model_version"], str):
        raise CassetteError("model_version is text or null")
    out["model_version"] = turn["model_version"]
    if not isinstance(turn["recorded"], str) or not _DATE.match(turn["recorded"]):
        raise CassetteError(f"recorded is a date, YYYY-MM-DD, not {turn['recorded']!r}")
    out["recorded"] = turn["recorded"]
    if turn["ledger_entry"] is not None:
        _count(turn["ledger_entry"], "ledger_entry", least=1)
    out["ledger_entry"] = turn["ledger_entry"]
    if not isinstance(turn["source"], str) or not turn["source"]:
        raise CassetteError("source says where the answer was recorded")
    out["source"] = turn["source"]
    return out


class TurnRecorder:
    """Builds one cassette turn while the bytes go to a real transcriber.

    Feed it every byte the plugin was fed, in the same calls, and note each
    partial the plugin returned straight after the call that returned it.
    `turn()` gives the finished, checked turn for `save()`.
    """

    def __init__(self, request: Mapping[str, Any]) -> None:
        self.request = normalise_request(request)
        self._hash = RunningHash()
        self.partials: list[dict[str, Any]] = []

    @property
    def total(self) -> int:
        return self._hash.total

    def feed(self, data: bytes) -> int:
        return self._hash.update(data)

    def partial(self, text: str) -> None:
        if self.partials and self.partials[-1]["after_bytes"] == self.total:
            raise CassetteError(f"a partial is already noted after {self.total} bytes; one a feed() call")
        if self.total == 0:
            raise CassetteError("a partial before any byte was fed")
        self.partials.append({"text": text, "after_bytes": self.total, "prefix_sha256": self._hash.hexdigest()})

    def turn(
        self,
        text: str,
        *,
        recorded: str,
        source: str,
        confidence: float | None = None,
        error: str | None = None,
        model_version: str | None = None,
        ledger_entry: int | None = None,
    ) -> dict[str, Any]:
        return check_turn(
            {
                "request": self.request,
                "fed_sha256": self._hash.hexdigest(),
                "fed_bytes": self.total,
                "partials": list(self.partials),
                "final": {"text": text, "confidence": confidence, "error": error},
                "model_version": model_version,
                "recorded": recorded,
                "ledger_entry": ledger_entry,
                "source": source,
            }
        )


# --------------------------------------------------------------------- files


def load_file(path: Path | str) -> dict[str, Any]:
    """One cassette file, checked: `{"format": ..., "turns": [...]}`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CassetteError(f"{path}: cannot be read: {exc.strerror or exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CassetteError(f"{path}: not JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        got = data.get("format") if isinstance(data, dict) else None
        raise CassetteError(f"{path}: its format is {got!r}, and this reads {FORMAT!r}")
    try:
        _exact(data, ("format", "turns"), "the cassette")
    except CassetteError as exc:
        raise CassetteError(f"{path}: {exc}") from None
    turns = data["turns"]
    if not isinstance(turns, list):
        raise CassetteError(f"{path}: turns is a list")
    checked = []
    for n, turn in enumerate(turns, 1):
        try:
            checked.append(check_turn(turn))
        except CassetteError as exc:
            raise CassetteError(f"{path}: turn {n}: {exc}") from None
    return {"format": FORMAT, "turns": checked}


def load(path: Path | str) -> list[dict[str, Any]]:
    """Every turn in a cassette file, or in every `*.json` directly inside a
    directory, in the order of the files' names and then of the turns.

    The order matters: when two turns match the same bytes, the first wins.
    """
    path = Path(path).expanduser()
    if path.is_dir():
        files = sorted(p for p in path.glob("*.json") if p.is_file())
    elif path.exists():
        files = [path]
    else:
        raise CassetteError(f"{path}: no such file or directory")
    turns: list[dict[str, Any]] = []
    for file in files:
        turns.extend(load_file(file)["turns"])
    return turns


def save(path: Path | str, turns: Iterable[Mapping[str, Any]]) -> Path:
    """Write a cassette file and read it back. Each turn is checked first, so
    a turn with a credential in its request is never written.

    The write goes to a temporary file that replaces the old one, so a
    reader never sees half a cassette.
    """
    path = Path(path).expanduser()
    checked = [check_turn(t) for t in turns]
    data = {"format": FORMAT, "turns": checked}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if load_file(path)["turns"] != checked:
        raise CassetteError(f"{path}: the turns written did not read back")
    return path


# --------------------------------------------------------------------- play


@dataclass(frozen=True)
class Played:
    """How one utterance ended: the turn it matched, or None for a miss."""

    turn: dict[str, Any] | None
    fed_sha256: str
    fed_bytes: int

    @property
    def hit(self) -> bool:
        return self.turn is not None


class Player:
    """Plays turns back against the bytes fed, one utterance at a time.

    The candidates are the turns whose request equals `request`, or every
    turn when it is None. A partial is given after the `feed()` that brings
    the total to its `after_bytes`, when the bytes so far hash to its
    `prefix_sha256`. A turn whose prefix fails that check is out for the rest
    of the utterance. Once a turn's partial is given, only that turn gives
    partials while it stays in, so two recordings of the same bytes are
    never mixed. `finish()` matches the whole utterance, and prefers the turn
    whose partials were given.
    """

    def __init__(self, turns: Iterable[Mapping[str, Any]], request: Mapping[str, Any] | None = None) -> None:
        wanted = normalise_request(request) if request is not None else None
        self.request = wanted
        self.turns: list[dict[str, Any]] = [
            t for t in (check_turn(t) for t in turns) if wanted is None or t["request"] == wanted
        ]
        self._partials_at: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        self._finals: dict[tuple[str, int], list[int]] = {}
        for i, turn in enumerate(self.turns):
            for partial in turn["partials"]:
                self._partials_at.setdefault(partial["after_bytes"], []).append((i, partial))
            self._finals.setdefault((turn["fed_sha256"], turn["fed_bytes"]), []).append(i)
        self.begin()

    def begin(self) -> None:
        """Forget the utterance in progress."""
        self._hash = RunningHash()
        self._out: set[int] = set()
        self._followed: int | None = None

    @property
    def total(self) -> int:
        return self._hash.total

    def feed(self, data: bytes) -> dict[str, Any] | None:
        """The partial due after these bytes, or None."""
        total = self._hash.update(data)
        here = self._partials_at.get(total)
        if not here:
            return None
        digest = self._hash.hexdigest()
        alive: list[tuple[int, dict[str, Any]]] = []
        for i, partial in here:
            if i in self._out:
                continue
            if partial["prefix_sha256"] != digest:
                self._out.add(i)
                continue
            alive.append((i, partial))
        if not alive:
            return None
        if self._followed is not None and self._followed not in self._out:
            chosen = next((pair for pair in alive if pair[0] == self._followed), None)
            if chosen is None:
                return None
        else:
            chosen = alive[0]
        self._followed = chosen[0]
        return chosen[1]

    def finish(self) -> Played:
        """The turn these bytes match, or a miss; then a fresh utterance."""
        digest, total = self._hash.hexdigest(), self._hash.total
        matches = self._finals.get((digest, total), [])
        if self._followed in matches:
            pick: int | None = self._followed
        else:
            pick = matches[0] if matches else None
        self.begin()
        return Played(self.turns[pick] if pick is not None else None, digest, total)


# ------------------------------------------------------------ live capture


def safe_url(url: str) -> str:
    """The URL without any query parameter named like a credential."""
    parts = urlsplit(str(url))
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _CREDENTIAL.search(k)]
    return urlunsplit(parts._replace(query=urlencode(query)))


class _RecordedSocket:
    """A connection that keeps every message received, as it came.

    What went out is counted and hashed; text frames sent (CloseStream) are
    kept. Headers never reach this object: they went to `connect()`.
    """

    def __init__(self, ws: Any, record: dict[str, Any], clock: Callable[[], float], t0: float) -> None:
        self._ws = ws
        self._record = record
        self._clock = clock
        self._t0 = t0
        self._sent = hashlib.sha256()

    def _t(self) -> float:
        return round(self._clock() - self._t0, 4)

    async def send(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray, memoryview)):
            chunk = bytes(data)
            self._sent.update(chunk)
            self._record["audio_frames_sent"] += 1
            self._record["audio_bytes_sent"] += len(chunk)
            self._record["audio_sha256"] = self._sent.hexdigest()
        else:
            self._record["text_sent"].append({"t": self._t(), "text": str(data)})
        await self._ws.send(data)

    async def recv(self) -> Any:
        try:
            message = await self._ws.recv()
        except Exception as exc:
            self._record["received"].append({"t": self._t(), "raised": type(exc).__name__})
            raise
        if isinstance(message, (bytes, bytearray)):
            self._record["received"].append({"t": self._t(), "bytes_b64": base64.b64encode(bytes(message)).decode("ascii")})
        else:
            self._record["received"].append({"t": self._t(), "text": message})
        return message

    async def close(self) -> None:
        self._record["closed_s"] = self._t()
        await self._ws.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)


def recording_connect(
    connect: Callable[..., Awaitable[Any]],
    connections: list[dict[str, Any]],
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a WebSocket `connect` so each connection's traffic is kept.

    Each connection appends one record to `connections`: its URL with any
    credential-named parameter removed, when it was asked for and opened,
    the audio sent (frames, bytes, sha256), the text frames sent, and every
    message received with its time in seconds from the wrapping. The keyword
    arguments, which carry the key in a header, are passed through and never
    stored.
    """
    t0 = clock()

    async def wrapped(url: str, **kwargs: Any) -> Any:
        record: dict[str, Any] = {
            "url": safe_url(url),
            "requested_s": round(clock() - t0, 4),
            "opened_s": None,
            "error": None,
            "audio_frames_sent": 0,
            "audio_bytes_sent": 0,
            "audio_sha256": None,
            "text_sent": [],
            "received": [],
            "closed_s": None,
        }
        connections.append(record)
        try:
            ws = await connect(url, **kwargs)
        except Exception as exc:
            # The type and the status only: a library's message could quote
            # what it was handed.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            record["error"] = type(exc).__name__ + (f" HTTP {status}" if isinstance(status, int) else "")
            raise
        record["opened_s"] = round(clock() - t0, 4)
        return _RecordedSocket(ws, record, clock, t0)

    return wrapped


def model_version(received: Iterable[Mapping[str, Any]]) -> dict[str, str] | None:
    """The model Deepgram reported in a connection's messages, or None.

    Results messages carry it under `metadata.model_info`, and the Metadata
    message after CloseStream under `model_info`, keyed by the model's uuid
    (both in the replay of 2026-10-05, `dg09`, on disk). The last one
    reported wins. Returns `{"name", "version", "arch"}` as Deepgram gave
    them.
    """
    found: dict[str, str] | None = None
    for item in received:
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        infos: list[Any] = []
        if data.get("type") == "Results":
            infos.append((data.get("metadata") or {}).get("model_info"))
        elif data.get("type") == "Metadata":
            infos.extend((data.get("model_info") or {}).values())
        for info in infos:
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                found = {k: str(info[k]) for k in ("name", "version", "arch") if k in info}
    return found
