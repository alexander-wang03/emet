#!/usr/bin/env python3
"""Replay the turns a cassette missed through the real Deepgram plugin, on the ledger.

    python tools/bank/paid_replay.py MISSES --ledger BANK/ledger.json --cassette OUT.json
                                     --keys FILE --expect-spent N [--query KEY=VALUE ...]
    python tools/bank/paid_replay.py MISSES --ledger BANK/ledger.json --cassette OUT.json --dry-run
    python tools/bank/paid_replay.py --seed-ledger OLD.json --ledger BANK/ledger.json

MISSES is what `replay.py --misses` wrote: each turn the cassettes missed,
with the bytes the mock was fed beside it. Each turn's bytes are sent to
Deepgram as the engine fed them, the frames of the handover in one burst
and the rest one frame's length apart, through `DeepgramTranscriber` itself,
and what came back is written as cassette turns (FORMAT.md, "Cassettes"),
so the next replay of those bytes gets the answer for nothing. This is the
0.5.3 harness's ledger (releases/v0.5.3/harness/cut_deepgram.py) moved into
the bank's tools; it spends the maintainer's voice and credits, so most of
it is refusal.

**Refused before anything is spent:** a miss list that is not one, or a
turn whose bytes are not its sha256 or its length; a query key the plugin
fixes (`encoding`, `sample_rate`, `channels`, `model`), one that sends the
transcript elsewhere (`callback`, `callback_method`), or one named like a
credential; a request URL, built offline by a plugin that is never
started, that is not `wss://api.deepgram.com/v1/listen` with exactly the
query expected; more than `MAX_ENTRY_S` of audio, since an entry is one
replay of at most that much; a ledger that is missing or malformed;
`--expect-spent` that is not what the ledger holds; a month whose
replays are spent; and a keys file with no Deepgram key, or no
`websockets`, either of which would stop the plugin after the entry was
spent.

**The allowance** (D6, decided 2026-10-27): 20 replay entries a month, a
monthly canary, and up to 20 real-vendor turns a release, each logged in
the ledger; anything more asks the maintainer first. Only entries made
under it count, from its first day, `standing.since`: the ten spent in
2026-09 and 2026-10 were the 0.5.3 ledger's own allowance of ten. The
canary is `canary.py`'s, and the turns a release spends outside a replay
are the release's own; this tool spends replay entries only.

**The network.** Nothing at all until the entry is written and read back.
Then Deepgram's host by name, and on its port the addresses that name
resolved to; everything else is refused and counted, in Python's socket
calls, the Windows proactor's connect, and an audit hook under both.
Loopback only inside `socket.socketpair`, the event loop's self-pipe. Every
connection opened must use the offline URL, or the run says so.

**Keys** come from the file `--keys` names, read by the engine's own
loader, which prints names and never values; this script never opens it.

`--dry-run` spends nothing and connects nowhere: it runs every check above,
prints the URL and the entry a real run would write, and writes nothing.

The standard library and the four packages only.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import manifest as bank_files  # noqa: E402  the standard library only

#: The most audio one ledger entry sends: the longest of the ten replays the
#: 0.5.3 ledger spent, the 130 s one-breath recording. D6 reckons 20 entries
#: at about 40 minutes, so an entry is about two minutes.
MAX_ENTRY_S = 130.0

#: D6, decided 2026-10-27.
STANDING = {
    "since": "2026-10-27",
    "vendor_turns_a_release": 20,
    "decided": "D6, research/references-2026-10-26/decisions.md, 2026-10-27",
    "beyond": "anything more asks the maintainer first",
}
MONTHLY = {"replays": 20, "canaries": 1}

#: Query keys a run may not send: the plugin fixes the first four, and the
#: callbacks would have Deepgram send the transcript to another host.
REFUSED_QUERY = frozenset({"encoding", "sample_rate", "channels", "model", "callback", "callback_method"})
_CREDENTIAL = re.compile(r"key|token|auth|secret|pass", re.I)
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


class Refusal(RuntimeError):
    """Nothing is spent. The message says why."""


# ================================================================== the ledger


def seed_ledger(old_path: Path, new_path: Path) -> dict[str, Any]:
    """The bank's ledger, started from the 0.5.3 ledger's spent entries,
    each kept as it was with its kind added, under the standing allowance.
    The old file is read, never written."""
    if new_path.exists():
        raise Refusal(f"{new_path} exists; a ledger is seeded once")
    old = json.loads(Path(old_path).read_text(encoding="utf-8"))
    if old.get("allowed") != len(old.get("spent") or []) or old.get("allowed") != 10:
        raise Refusal(f"{old_path} is not the 0.5.3 ledger with its ten entries spent")
    data = {
        "format": "emet-ledger/1",
        "monthly": dict(MONTHLY),
        "standing": dict(STANDING),
        "before": {"allowed": old["allowed"], "ledger": "releases/v0.5.3/harness/deepgram-ledger.json",
                   "sha256": bank_files.sha256_file(old_path)},
        "spent": [{**entry, "kind": "replay"} for entry in old["spent"]],
    }
    _write_ledger(new_path, data)
    return data


def ledger_read(path: Path) -> dict[str, Any]:
    """The ledger, or a Refusal saying why it cannot be trusted."""
    if not Path(path).is_file():
        raise Refusal(f"no ledger at {path}: the budget cannot be checked")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Refusal(f"{Path(path).name} is not JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != "emet-ledger/1" or not isinstance(data.get("spent"), list):
        raise Refusal(f'{Path(path).name} is not an emet-ledger/1 file with a "spent" list')
    monthly, standing = data.get("monthly"), data.get("standing")
    if (not isinstance(monthly, dict) or not all(isinstance(monthly.get(k), int) and not isinstance(monthly.get(k), bool)
                                                 and monthly.get(k) >= 0 for k in ("replays", "canaries"))):
        raise Refusal('the ledger\'s "monthly" is {"replays": N, "canaries": N}')
    if not isinstance(standing, dict) or not _DATE.match(str(standing.get("since") or "")):
        raise Refusal('the ledger\'s "standing.since" is the date the allowance began')
    for n, entry in enumerate(data["spent"], 1):
        if not isinstance(entry, dict) or not _DATE.match(str(entry.get("time") or "")) or not entry.get("kind"):
            raise Refusal(f"{Path(path).name}: entry {n} has no time or no kind")
    return data


def allowance(data: dict[str, Any], today: datetime.date) -> dict[str, Any]:
    """This month's replay entries: allowed, spent under the standing
    allowance, and left. An entry counts in the month of its time, from
    `standing.since` on; a canary is not a replay."""
    month = today.strftime("%Y-%m")
    since = data["standing"]["since"][:10]
    spent = sum(1 for e in data["spent"]
                if e["kind"] == "replay" and e["time"][:7] == month and e["time"][:10] >= since)
    allowed = data["monthly"]["replays"] if today.isoformat() >= since else 0
    return {"month": month, "allowed": allowed, "spent": spent, "left": max(0, allowed - spent)}


def check_spend(data: dict[str, Any], today: datetime.date, expect_spent: int | None) -> dict[str, Any]:
    """Refuse unless one more replay entry fits this month."""
    left = allowance(data, today)
    if expect_spent is not None and len(data["spent"]) != expect_spent:
        raise Refusal(f"--expect-spent {expect_spent}, and the ledger holds {len(data['spent'])} entries")
    if left["left"] < 1:
        raise Refusal(f"{left['month']}: {left['spent']} of {left['allowed']} replay entries spent; "
                      f"{STANDING['beyond']}")
    return left


def _write_ledger(path: Path, data: dict[str, Any]) -> None:
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def ledger_spend(path: Path, entry: dict[str, Any], today: datetime.date, expect_spent: int | None) -> int:
    """Append one entry and read it back, or refuse. Returns its number."""
    data = ledger_read(path)
    check_spend(data, today, expect_spent)
    data["spent"].append(entry)
    _write_ledger(path, data)
    back = ledger_read(path)
    if len(back["spent"]) != len(data["spent"]) or back["spent"][-1] != entry:
        raise Refusal("the new entry did not read back")
    return len(back["spent"])


# ============================================================== the miss list


def read_misses(path: Path) -> list[dict[str, Any]]:
    """The miss list replay.py wrote, each turn's bytes read and checked."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"{path}: not a readable miss list: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != "emet-misses/1" or not isinstance(data.get("turns"), list):
        raise Refusal(f"{path}: not an emet-misses/1 file")
    turns = []
    for n, turn in enumerate(data["turns"], 1):
        raw = (Path(path).parent / str(turn.get("raw", ""))).read_bytes() if turn.get("raw") else b""
        if (hashlib.sha256(raw).hexdigest() != turn.get("fed_sha256") or len(raw) != turn.get("fed_bytes")
                or sum(turn.get("feed_sizes") or []) != len(raw)):
            raise Refusal(f"{path}: turn {n}'s bytes are not its sha256, its length or its feeds")
        turns.append({**turn, "bytes": raw})
    if not turns:
        raise Refusal(f"{path}: no turn missed, so nothing needs paying for")
    return turns


def audio_seconds(turns: list[dict[str, Any]], rate: int) -> float:
    return sum(t["fed_bytes"] for t in turns) / 2 / rate


# =========================================================== the request offline


def parse_query(pairs: list[str]) -> dict[str, str]:
    """--query KEY=VALUE pairs, each key checked before anything else."""
    query: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise Refusal(f"--query {pair!r} is not KEY=VALUE")
        if key in REFUSED_QUERY:
            raise Refusal(f"--query {key}: the plugin fixes it, or it sends the transcript elsewhere")
        if _CREDENTIAL.search(key):
            raise Refusal(f"--query {key}: named like a credential, which a ledger or cassette never keeps")
        query[key] = value
    return query


def selection(query: dict[str, str], model: str) -> dict[str, Any]:
    """What a body taking the stage over would give the plugin."""
    from emet_providers.deepgram import DEFAULT_KEY_ENV

    return {"provider": "deepgram", "model": model, "key_env": DEFAULT_KEY_ENV, "params": {"query": dict(query)}}


def offline_request(chosen: dict[str, Any], rate: int) -> dict[str, Any]:
    """The URL the plugin builds, asked of a plugin constructed and never
    started, so nothing connects; and the problems with it."""
    from emet_providers.deepgram import DEFAULT_URL, FIXED_QUERY, DeepgramTranscriber
    from emet_sdk.types import AudioFormat

    plugin = DeepgramTranscriber(chosen, AudioFormat(sample_rate=rate))
    url = plugin.request_url()
    parts = urlparse(url)
    got = dict(parse_qsl(parts.query))
    want = {**FIXED_QUERY, **chosen["params"]["query"], "sample_rate": str(rate), "model": chosen["model"]}
    problems = []
    if f"{parts.scheme}://{parts.netloc}{parts.path}" != DEFAULT_URL:
        problems.append(f"the plugin would request {parts.scheme}://{parts.netloc}{parts.path}, not {DEFAULT_URL}")
    if (parts.hostname or "").lower() != "api.deepgram.com":
        problems.append(f"the host is {parts.hostname!r}, not api.deepgram.com")
    if got != want:
        problems.append(f"the plugin's query is {got}, not {want}")
    if any(_CREDENTIAL.search(k) for k in got):
        problems.append("the URL has a parameter named like a credential")
    return {"url": url, "query": plugin.query(), "problems": problems}


# ================================================================ the network

LOOPBACK = {"localhost"}


def _norm(host: Any) -> str:
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    h = str(host).strip().lower().rstrip(".").strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return h
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _loopback(h: str) -> bool:
    if h in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class Guard:
    """Who may be reached: nobody until `arm()`, then one host by name and,
    on its port, the addresses that name resolved to. Loopback only inside
    `socket.socketpair`."""

    def __init__(self, host: str = "api.deepgram.com", port: int = 443) -> None:
        self.host, self.port = host, port
        self.armed = False
        self.addresses: set[str] = set()
        self.refused: list[str] = []
        self.allowed: list[str] = []
        self._local = threading.local()
        self._lock = threading.RLock()

    @property
    def in_socketpair(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def name_ok(self, host: Any) -> bool:
        if host is None:
            return False
        h = _norm(host)
        if self.in_socketpair and _loopback(h):
            return True
        with self._lock:
            return self.armed and (h == self.host or h in self.addresses)

    def addr_ok(self, host: Any, port: Any) -> bool:
        h = _norm(host)
        if self.in_socketpair and _loopback(h):
            return True
        with self._lock:
            return self.armed and h in self.addresses and port == self.port

    def refuse(self, what: str, host: Any, port: Any = None) -> PermissionError:
        with self._lock:
            self.refused.append(f"{what} {host}:{port}")
        return PermissionError(f"paid_replay: {what} to {host}:{port} refused")

    def install(self) -> None:
        """Python's socket calls, the Windows proactor, and an audit hook."""
        guard = self
        getaddrinfo, connect, connect_ex, socketpair = (
            socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, socket.socketpair)

        def split(address: Any) -> tuple[str, Any]:
            if isinstance(address, tuple) and address:
                return _norm(address[0]), address[1] if len(address) > 1 else None
            return _norm(address), None

        def g_getaddrinfo(host, port, *args, **kwargs):
            if not guard.name_ok(host):
                raise guard.refuse("getaddrinfo", host, port)
            result = getaddrinfo(host, port, *args, **kwargs)
            if _norm(host) == guard.host:
                with guard._lock:
                    guard.addresses.update(_norm(info[4][0]) for info in result)
            guard.allowed.append(f"getaddrinfo {host}:{port}")
            return result

        def g_connect(self, address):
            host, port = split(address)
            if not guard.addr_ok(host, port):
                raise guard.refuse("connect", host, port)
            return connect(self, address)

        def g_connect_ex(self, address):
            host, port = split(address)
            if not guard.addr_ok(host, port):
                raise guard.refuse("connect_ex", host, port)
            return connect_ex(self, address)

        def g_socketpair(*args, **kwargs):
            guard._local.depth = getattr(guard._local, "depth", 0) + 1
            try:
                return socketpair(*args, **kwargs)
            finally:
                guard._local.depth -= 1

        socket.getaddrinfo = g_getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = g_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = g_connect_ex  # type: ignore[method-assign]
        socket.socketpair = g_socketpair  # type: ignore[assignment]
        if sys.platform == "win32":
            # The proactor's ConnectEx is seen by no socket audit event
            # (cut_deepgram.py, checked on the laptop, 2026-10-01).
            import asyncio.windows_events as windows_events

            iocp_connect = windows_events.IocpProactor.connect

            def g_iocp_connect(self, conn, address):
                host, port = split(address)
                if not guard.addr_ok(host, port):
                    raise guard.refuse("ConnectEx", host, port)
                return iocp_connect(self, conn, address)

            windows_events.IocpProactor.connect = g_iocp_connect  # type: ignore[method-assign]

        def audit(event: str, args: tuple) -> None:
            if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
                address = args[1] if len(args) > 1 else None
                if address is not None:
                    host, port = split(address)
                    if not guard.addr_ok(host, port):
                        raise guard.refuse(f"audit {event}", host, port)
            elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
                if not guard.name_ok(args[0] if args else None):
                    raise guard.refuse(f"audit {event}", args[0] if args else None)

        sys.addaudithook(audit)


# ============================================================== the replay


async def replay_turns(turns: list[dict[str, Any]], chosen: dict[str, Any], rate: int, *, connect: Callable,
                       offline_url: str, entry: int | None, recorded: str, source: str,
                       pace: bool = True, sleep: Callable = asyncio.sleep,
                       ) -> tuple[list[dict], list[str], list[dict]]:
    """Each turn's bytes through the real plugin, as cassette turns; any
    problem with what was sent or connected to; and each connection's
    record (`cassette.recording_connect`: its URL without a credential, and
    what it sent and received, never its headers)."""
    from emet_providers import cassette
    from emet_providers.deepgram import DeepgramTranscriber
    from emet_sdk.types import AudioFormat

    connections: list[dict[str, Any]] = []
    plugin = DeepgramTranscriber(chosen, AudioFormat(sample_rate=rate),
                                 connect=cassette.recording_connect(connect, connections))
    await plugin.start()
    if not plugin.describe().healthy:
        return [], [f"the plugin did not start: {plugin.health().detail}"], connections
    request = cassette.request_for("deepgram", plugin.query())
    out, problems = [], []
    try:
        for t in turns:
            recorder = cassette.TurnRecorder(request)
            frame_s = t["feed_sizes"][-1] / 2 / rate if t["feed_sizes"] else 0.0
            burst = int(t.get("handover_feeds") or 0)
            start, pos = time.perf_counter(), 0
            for i, size in enumerate(t["feed_sizes"]):
                if pace and i >= burst:
                    due = start + (i - burst + 1) * frame_s
                    while (now := time.perf_counter()) < due:
                        await sleep(due - now)
                frame = t["bytes"][pos:pos + size]
                pos += size
                recorder.feed(frame)
                partial = await plugin.feed(frame)
                if partial is not None:
                    recorder.partial(partial.text)
            final = await plugin.finish()
            version = cassette.model_version(connections[-1]["received"]) if connections else None
            out.append(recorder.turn(final.text, confidence=final.confidence, error=final.error,
                                     model_version=(version or {}).get("version"), ledger_entry=entry,
                                     recorded=recorded, source=f"{source} turn {t['recording']}/{t['turn']}"))
            if out[-1]["fed_sha256"] != t["fed_sha256"]:
                problems.append(f"{t['recording']} turn {t['turn']}: sent bytes that are not the miss list's")
    finally:
        await plugin.shutdown()
    used = {c["url"] for c in connections}
    if used != {cassette.safe_url(offline_url)}:
        problems.append(f"a connection used {sorted(used)}, not the offline URL")
    return out, problems, connections


# ================================================================== the run


def local_time(now: datetime.datetime) -> str:
    name = now.tzname() or ""
    if " " in name:  # Windows says "Pacific Daylight Time"
        name = "".join(word[0] for word in name.split())
    return now.strftime("%Y-%m-%d %H:%M:%S ") + name


def engine_tree() -> dict[str, Any]:
    """The repo that ran: HEAD and whether the tree was clean, read-only git.

    `GIT_OPTIONAL_LOCKS=0` keeps `git status` from writing the index, so it
    never holds the lock while the maintainer stages a change, and
    `LC_ALL=C` keeps git's messages in English, as `cli.revision` runs it."""
    import subprocess

    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}

    def git(*a: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL, env=env)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    status = git("status", "--porcelain")
    return {"repo_head": git("rev-parse", "HEAD"), "tree_clean": None if status is None else status == ""}


def plan(args: argparse.Namespace, today: datetime.date) -> dict[str, Any]:
    """Every check that needs no network and spends nothing."""
    query = parse_query(args.query)
    turns = read_misses(args.misses)
    rate = int(args.rate)
    seconds = audio_seconds(turns, rate)
    if seconds > MAX_ENTRY_S:
        raise Refusal(f"{seconds:.1f} s of audio, past one entry's {MAX_ENTRY_S:g} s; split the miss list")
    chosen = selection(query, args.model)
    offline = offline_request(chosen, rate)
    if offline["problems"]:
        raise Refusal("; ".join(offline["problems"]))
    data = ledger_read(args.ledger)
    left = check_spend(data, today, None if args.dry_run else args.expect_spent)
    if Path(args.cassette).exists():
        raise Refusal(f"{args.cassette} exists; a paid replay writes a cassette of its own")
    entry = {
        "time": None, "kind": "replay", "tool": "tools/bank/paid_replay.py",
        "misses": str(Path(args.misses).resolve()), "misses_sha256": bank_files.sha256_file(args.misses),
        "turns": [f"{t['recording']}/{t['turn']}" for t in turns], "audio_s": round(seconds, 3),
        "request_url": offline["url"], "stt_query": query, "engine": engine_tree(),
        "cassette": str(Path(args.cassette).resolve()), "paced": not args.unpaced,
    }
    return {"turns": turns, "rate": rate, "chosen": chosen, "offline": offline, "left": left, "entry": entry}


def main(argv: list[str] | None = None, *, today: datetime.date | None = None) -> int:
    """The command. `today` is the date the allowance is reckoned on; a
    test names it, a run takes the machine's."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("misses", type=Path, nargs="?", help="the miss list replay.py --misses wrote")
    ap.add_argument("--ledger", type=Path, required=True, help="the bank's ledger.json")
    ap.add_argument("--cassette", type=Path, help="the cassette file to write")
    ap.add_argument("--keys", type=Path, help="the keys file, read by the engine's loader; values never printed")
    ap.add_argument("--expect-spent", type=int, metavar="N", help="to spend: the entries the ledger holds now")
    ap.add_argument("--query", action="append", default=[], metavar="KEY=VALUE", help="a Deepgram query parameter")
    ap.add_argument("--model", default="nova-3")
    ap.add_argument("--rate", default="16000")
    ap.add_argument("--unpaced", action="store_true", help="send as fast as the plugin takes it")
    ap.add_argument("--dry-run", action="store_true", help="every check, and nothing spent, written or connected")
    ap.add_argument("--seed-ledger", type=Path, metavar="OLD", help="start the bank's ledger from the 0.5.3 one")
    args = ap.parse_args(argv)
    today = today or datetime.date.today()
    try:
        if args.seed_ledger:
            data = seed_ledger(args.seed_ledger, args.ledger)
            print(f"paid_replay: {args.ledger} seeded with {len(data['spent'])} spent entries; "
                  f"{allowance(data, today)}")
            return 0
        if args.misses is None or args.cassette is None:
            ap.error("a replay needs MISSES and --cassette")
        if not args.dry_run and (args.keys is None or args.expect_spent is None):
            ap.error("spending needs --keys and --expect-spent N, the entries the ledger holds now")
        p = plan(args, today)
    except Refusal as exc:
        print(f"paid_replay: refused, nothing spent: {exc}", file=sys.stderr)
        return 2
    left = p["left"]
    print(f"paid_replay: {len(p['turns'])} turn(s), {p['entry']['audio_s']:g} s of audio; "
          f"{left['month']}: {left['spent']} of {left['allowed']} replay entries spent")
    print(f"  request {p['offline']['url']}")
    if args.dry_run:
        print("  dry run: nothing spent, written or connected. A real run writes this entry:")
        print("  " + json.dumps({**p["entry"], "time": "(when it runs)"}, ensure_ascii=False))
        return 0

    sys.modules["sounddevice"] = None  # type: ignore[assignment]  # no microphone, no speaker
    from emet_engine.keys import load_keys
    from emet_providers.deepgram import DEFAULT_KEY_ENV

    for name in [n for n in os.environ if n.startswith("EMET_")]:
        os.environ.pop(name)
    try:
        loaded_keys = load_keys(str(args.keys))
    except FileNotFoundError:
        print(f"paid_replay: refused, nothing spent: no keys file at {args.keys}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # The loader's message quotes the line, and the line may hold a key.
        line = re.match(r"^.*:(\d+): ", str(exc))
        print(f"paid_replay: refused, nothing spent: line {line.group(1) if line else '?'} of the keys "
              f"file does not parse", file=sys.stderr)
        return 2
    for loaded in loaded_keys:
        print(f"  keys {', '.join(loaded.names) or 'nothing'} from {loaded.path}")
    # Everything the plugin's start() would refuse for, refused before the
    # entry is spent: an entry spent on a run that cannot connect buys nothing.
    if not os.environ.get(DEFAULT_KEY_ENV):
        print(f"paid_replay: refused, nothing spent: {args.keys} holds no {DEFAULT_KEY_ENV}", file=sys.stderr)
        return 2
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        print("paid_replay: refused, nothing spent: the websockets library is not installed "
              "(emet-providers[deepgram])", file=sys.stderr)
        return 2
    guard = Guard()
    guard.install()
    entry = {**p["entry"], "time": local_time(datetime.datetime.now().astimezone())}
    try:
        number = ledger_spend(args.ledger, entry, today, args.expect_spent)
    except Refusal as exc:
        print(f"paid_replay: refused, nothing spent: {exc}", file=sys.stderr)
        return 2
    print(f"  ledger entry {number} written and read back; it is spent whatever happens next")
    guard.armed = True

    turns, problems, _connections = asyncio.run(replay_turns(
        p["turns"], p["chosen"], p["rate"], connect=connect, offline_url=p["offline"]["url"], entry=number,
        recorded=today.isoformat(), source=Path(args.misses).name, pace=not args.unpaced))
    guard.armed = False
    from emet_providers import cassette

    if turns:
        cassette.save(args.cassette, turns)
    print(f"  {len(turns)} cassette turn(s) in {args.cassette}; network refused {len(guard.refused)}, "
          f"allowed {len(guard.allowed)}")
    for problem in problems + guard.refused:
        print(f"  PROBLEM {problem}")
    return 1 if problems or guard.refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
