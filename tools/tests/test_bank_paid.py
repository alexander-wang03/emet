"""tools/bank/paid_replay.py and tools/bank/seed_cassettes.py: the ledger's
arithmetic and refusals, the offline request, the guard's decisions, a
paid replay against a fake Deepgram, and paid runs turned into cassettes.
Nothing here reaches a network or spends anything."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import paid_replay as paid
import seed_cassettes as seed
from conftest import BANK_TOOLS

pytest.importorskip("emet_providers")

OCT28 = datetime.date(2026, 10, 28)


def old_ledger(tmp_path: Path) -> Path:
    """The shape of releases/v0.5.3/harness/deepgram-ledger.json: ten spent."""
    times = ["2026-09-29 22:24:12 PDT"] + [f"2026-10-0{d} 23:20:27 PDT" for d in (1, 1, 2, 2, 2, 2, 3, 5, 5)]
    path = tmp_path / "deepgram-ledger.json"
    path.write_text(json.dumps({"allowed": 10, "spent": [{"time": t, "recording": "r"} for t in times]}), encoding="utf-8")
    return path


def a_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.json"
    paid.seed_ledger(old_ledger(tmp_path), path)
    return path


def entry(time: str, kind: str = "replay") -> dict:
    return {"time": time, "kind": kind}


# ------------------------------------------------------------- the ledger


def test_the_seeded_ledger_keeps_the_ten_and_spends_none_of_the_standing_allowance(tmp_path):
    path = a_ledger(tmp_path)
    data = paid.ledger_read(path)
    assert len(data["spent"]) == 10 and all(e["kind"] == "replay" for e in data["spent"])
    assert data["spent"][0]["recording"] == "r" and data["before"]["allowed"] == 10
    assert data["monthly"] == {"replays": 20, "canaries": 1} and data["standing"]["since"] == "2026-10-27"
    assert paid.allowance(data, OCT28) == {"month": "2026-10", "allowed": 20, "spent": 0, "left": 20}


def test_seeding_refuses_an_existing_ledger_and_a_ledger_not_spent_out(tmp_path):
    path = a_ledger(tmp_path)
    with pytest.raises(paid.Refusal, match="seeded once"):
        paid.seed_ledger(old_ledger(tmp_path), path)
    half = tmp_path / "half.json"
    half.write_text(json.dumps({"allowed": 10, "spent": [{"time": "2026-10-01"}]}), encoding="utf-8")
    with pytest.raises(paid.Refusal, match="ten entries spent"):
        paid.seed_ledger(half, tmp_path / "other.json")


def test_the_allowance_counts_replays_in_the_month_from_the_day_it_began(tmp_path):
    data = paid.ledger_read(a_ledger(tmp_path))
    data["spent"] += [entry("2026-10-27 09:00:00 PDT"), entry("2026-10-28 09:00:00 PDT"),
                      entry("2026-10-28 10:00:00 PDT", "canary"), entry("2026-11-01 08:00:00 PST")]
    assert paid.allowance(data, OCT28)["spent"] == 2
    assert paid.allowance(data, datetime.date(2026, 11, 2)) == {"month": "2026-11", "allowed": 20, "spent": 1, "left": 19}
    assert paid.allowance(data, datetime.date(2026, 10, 26))["allowed"] == 0


def test_a_spent_month_is_refused_and_the_next_month_is_not(tmp_path):
    data = paid.ledger_read(a_ledger(tmp_path))
    data["spent"] += [entry(f"2026-10-28 10:{m:02d}:00 PDT") for m in range(19)]
    assert paid.check_spend(data, OCT28, None)["left"] == 1
    data["spent"].append(entry("2026-10-28 11:00:00 PDT"))
    with pytest.raises(paid.Refusal, match="20 of 20 replay entries spent; anything more asks the maintainer"):
        paid.check_spend(data, OCT28, None)
    assert paid.check_spend(data, datetime.date(2026, 11, 1), None)["left"] == 20


def test_expect_spent_must_be_what_the_ledger_holds(tmp_path):
    data = paid.ledger_read(a_ledger(tmp_path))
    with pytest.raises(paid.Refusal, match="--expect-spent 9, and the ledger holds 10"):
        paid.check_spend(data, OCT28, 9)
    assert paid.check_spend(data, OCT28, 10)["left"] == 20


def test_an_entry_is_written_and_read_back(tmp_path):
    path = a_ledger(tmp_path)
    assert paid.ledger_spend(path, entry("2026-10-28 12:00:00 PDT"), OCT28, 10) == 11
    assert paid.allowance(paid.ledger_read(path), OCT28)["spent"] == 1


@pytest.mark.parametrize("text, expect", [
    (None, "no ledger"),
    ("{", "not JSON"),
    (json.dumps({"spent": []}), "not an emet-ledger/1 file"),
    (json.dumps({"format": "emet-ledger/1", "spent": [], "monthly": {"replays": "20", "canaries": 1},
                 "standing": {"since": "2026-10-27"}}), '"monthly"'),
    (json.dumps({"format": "emet-ledger/1", "spent": [{"time": "2026-10-28"}], "monthly": {"replays": 20, "canaries": 1},
                 "standing": {"since": "2026-10-27"}}), "no time or no kind"),
])
def test_a_ledger_that_cannot_be_trusted_is_refused(tmp_path, text, expect):
    path = tmp_path / "ledger.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    with pytest.raises(paid.Refusal, match=expect):
        paid.ledger_read(path)


# ------------------------------------------------------- the miss list


def a_miss_list(tmp_path: Path, seconds: float = 1.0) -> Path:
    raw = bytes(range(256)) * int(seconds * 32000 / 256)
    raw = raw[: len(raw) // 2560 * 2560]
    (tmp_path / "m").mkdir(exist_ok=True)
    (tmp_path / "m" / "t-turn-0001.raw").write_bytes(raw)
    doc = {"format": "emet-misses/1", "turns": [{
        "recording": "t-0.1", "recording_sha256": "0" * 64, "turn": 1, "take": "t-0.1/01",
        "fed_sha256": hashlib.sha256(raw).hexdigest(), "fed_bytes": len(raw), "feed_sizes": [2560] * (len(raw) // 2560),
        "handover_feeds": 2, "raw": "t-turn-0001.raw"}]}
    path = tmp_path / "m" / "misses.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_miss_list_whose_bytes_changed_is_refused(tmp_path):
    path = a_miss_list(tmp_path)
    assert paid.read_misses(path)[0]["fed_bytes"] == 30720
    raw = path.parent / "t-turn-0001.raw"
    raw.write_bytes(raw.read_bytes()[:-1] + b"\x00")
    with pytest.raises(paid.Refusal, match="not its sha256"):
        paid.read_misses(path)


@pytest.mark.parametrize("pair, expect", [
    ("model=nova-2", "the plugin fixes it"),
    ("callback=https://example.com", "sends the transcript elsewhere"),
    ("api_key=x", "named like a credential"),
    ("smart_format", "not KEY=VALUE"),
])
def test_a_query_that_could_leak_or_change_the_format_is_refused(pair, expect):
    with pytest.raises(paid.Refusal, match=expect):
        paid.parse_query([pair])


def test_the_request_is_built_and_checked_offline():
    good = paid.offline_request(paid.selection({"smart_format": "false"}, "nova-3"), 16000)
    assert good["problems"] == []
    assert good["url"].startswith("wss://api.deepgram.com/v1/listen?") and "smart_format=false" in good["url"]
    elsewhere = paid.selection({}, "nova-3")
    elsewhere["params"]["url"] = "wss://listen.example.com/v1/listen"
    found = paid.offline_request(elsewhere, 16000)["problems"]
    assert any("not wss://api.deepgram.com/v1/listen" in p for p in found)
    assert any("not api.deepgram.com" in p for p in found)


def plan_args(tmp_path: Path, misses: Path, **over) -> list[str]:
    args = [str(misses), "--ledger", str(a_ledger(tmp_path)), "--cassette", str(tmp_path / "out.json")]
    for k, v in over.items():
        args += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
    return args


def no_way_out(monkeypatch) -> None:
    """Every door a run could connect through fails the test: Python's
    sockets, a name lookup, the event loop's connect, the WebSocket client,
    and the paid replay itself."""
    import asyncio.base_events

    def fail(*a, **k):
        pytest.fail("a dry run tried to connect")

    async def afail(*a, **k):
        pytest.fail("a dry run tried to connect")

    for owner, name in ((paid.socket.socket, "connect"), (paid.socket.socket, "connect_ex"), (paid.socket, "getaddrinfo"),
                        (paid.socket, "create_connection")):
        monkeypatch.setattr(owner, name, fail)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", afail)
    monkeypatch.setattr(paid, "replay_turns", afail)
    try:
        import websockets.asyncio.client as client

        monkeypatch.setattr(client, "connect", fail)
    except ImportError:
        pass


def test_a_dry_run_spends_nothing_writes_nothing_and_connects_nowhere(tmp_path, capsys, monkeypatch):
    misses = a_miss_list(tmp_path)
    args = plan_args(tmp_path, misses, dry_run=True)
    before = (tmp_path / "ledger.json").read_bytes()
    no_way_out(monkeypatch)
    assert paid.main(args, today=OCT28) == 0
    out = capsys.readouterr().out
    assert "dry run: nothing spent, written or connected" in out and '"kind": "replay"' in out
    assert (tmp_path / "ledger.json").read_bytes() == before and not (tmp_path / "out.json").exists()


def test_too_much_audio_for_one_entry_is_refused_before_the_ledger(tmp_path, capsys):
    misses = a_miss_list(tmp_path, seconds=131.0)
    assert paid.main(plan_args(tmp_path, misses, dry_run=True), today=OCT28) == 2
    assert "past one entry's 130 s" in capsys.readouterr().err


def test_a_spent_month_refuses_the_dry_run_too(tmp_path, capsys):
    misses = a_miss_list(tmp_path)
    args = plan_args(tmp_path, misses, dry_run=True)
    data = paid.ledger_read(tmp_path / "ledger.json")
    data["spent"] += [entry("2026-10-28 10:00:00 PDT") for _ in range(20)]
    (tmp_path / "ledger.json").write_text(json.dumps(data), encoding="utf-8")
    assert paid.main(args, today=OCT28) == 2
    assert "20 of 20 replay entries spent" in capsys.readouterr().err
    assert paid.main(args, today=datetime.date(2026, 11, 1)) == 0


@pytest.mark.parametrize("given", [{}, {"keys": "keys.env"}, {"expect_spent": 10}])
def test_spending_needs_both_the_keys_and_the_count(tmp_path, given):
    misses = a_miss_list(tmp_path)
    with pytest.raises(SystemExit) as exc:
        paid.main(plan_args(tmp_path, misses, **given), today=OCT28)
    assert exc.value.code == 2


def test_no_deepgram_key_is_refused_before_the_ledger_and_the_guard(tmp_path, capsys, monkeypatch):
    """The key is checked before the entry is spent: a reviewer's run once
    spent entry 11 and then found no key. Nothing connects, and the guard
    is never installed in this process."""
    misses = a_miss_list(tmp_path)
    keys = tmp_path / "keys.env"
    keys.write_text("# no key here\n", encoding="utf-8")
    args = plan_args(tmp_path, misses, keys=keys, expect_spent=10)
    before = (tmp_path / "ledger.json").read_bytes()
    monkeypatch.setitem(sys.modules, "sounddevice", sys.modules.get("sounddevice"))
    monkeypatch.setattr(paid.os, "environ", dict(os.environ))
    connect = paid.socket.socket.connect
    monkeypatch.setattr(paid.Guard, "install", lambda self: pytest.fail("the guard was installed before the key"))
    assert paid.main(args, today=OCT28) == 2
    assert "holds no EMET_DEEPGRAM_KEY" in capsys.readouterr().err
    assert (tmp_path / "ledger.json").read_bytes() == before and paid.socket.socket.connect is connect


def test_git_is_asked_without_taking_the_index_lock(monkeypatch):
    seen: list = []

    def run(cmd, **kwargs):
        seen.append(kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    paid.engine_tree()
    assert seen and all(env.get("GIT_OPTIONAL_LOCKS") == "0" and env.get("LC_ALL") == "C" for env in seen)


def test_an_entry_that_does_not_read_back_is_refused(tmp_path, monkeypatch):
    path = a_ledger(tmp_path)
    monkeypatch.setattr(paid, "_write_ledger", lambda p, data: None)
    with pytest.raises(paid.Refusal, match="did not read back"):
        paid.ledger_spend(path, entry("2026-10-28 12:00:00 PDT"), OCT28, 10)


# ------------------------------------------------------------ the guard


def test_the_guard_reaches_nobody_unarmed_and_only_deepgram_armed():
    guard = paid.Guard()
    assert not guard.name_ok("api.deepgram.com") and not guard.addr_ok("203.0.113.5", 443)
    guard.armed = True
    guard.addresses.add("203.0.113.5")
    assert guard.name_ok("API.Deepgram.com.") and not guard.name_ok("example.com")
    assert guard.addr_ok("203.0.113.5", 443) and not guard.addr_ok("203.0.113.5", 80)
    assert not guard.addr_ok("192.0.2.1", 443) and not guard.addr_ok("127.0.0.1", 443)
    assert guard.addr_ok("::ffff:203.0.113.5", 443)


GUARD_PROBE = """
import socket, sys
sys.path.insert(0, sys.argv[1])
import paid_replay
guard = paid_replay.Guard()
guard.install()
def attempt(what, fn):
    try:
        fn()
        print(what, "allowed")
    except OSError as exc:
        print(what, "refused", type(exc).__name__)
attempt("lookup unarmed", lambda: socket.getaddrinfo("api.deepgram.com", 443))
guard.armed = True
guard.addresses.add("203.0.113.5")  # as if api.deepgram.com had resolved to it
attempt("lookup other", lambda: socket.getaddrinfo("example.com", 443))
attempt("connect other", lambda: socket.create_connection(("192.0.2.1", 443), timeout=1))
def direct(address):
    s = socket.socket()
    s.settimeout(1)
    s.connect(address)
attempt("socket other", lambda: direct(("192.0.2.1", 443)))
attempt("socket allowed address, other port", lambda: direct(("203.0.113.5", 80)))
a, b = socket.socketpair()
a.sendall(b"x")
print("loopback", b.recv(1))
print("refused", len(guard.refused))
"""


def test_the_installed_guard_refuses_and_counts_and_keeps_the_self_pipe(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(GUARD_PROBE, encoding="utf-8")
    out = subprocess.run([sys.executable, str(probe), str(BANK_TOOLS)], capture_output=True, text=True, timeout=60).stdout
    assert "lookup unarmed refused PermissionError" in out
    assert "lookup other refused PermissionError" in out and "connect other refused PermissionError" in out
    assert "socket other refused PermissionError" in out
    assert "socket allowed address, other port refused PermissionError" in out
    assert "loopback b'x'" in out and "refused 5" in out


# ------------------------------------------- a paid replay, Deepgram faked


class FakeDeepgram:
    """A connection that answers like Deepgram: an interim after the third
    frame, a final at the close, then Metadata with the model's version."""

    def __init__(self, urls: list[str], headers: list | None = None) -> None:
        self.urls = urls
        self.headers = [] if headers is None else headers

    async def __call__(self, url, **kwargs):
        self.urls.append(url)
        self.headers.append(kwargs.get("additional_headers"))
        return FakeSocket()


class Closed(Exception):
    """What websockets raises when the server closes: it has rcvd and sent."""

    rcvd = sent = None


class FakeSocket:
    def __init__(self) -> None:
        self.frames = 0
        self.out: asyncio.Queue = asyncio.Queue()

    def results(self, text: str, final: bool) -> str:
        return json.dumps({"type": "Results", "is_final": final, "channel": {"alternatives": [
            {"transcript": text, "confidence": 0.98}]}, "metadata": {"model_info": {"name": "general-nova-3",
                                                                                     "version": "2025-04-17.21547", "arch": "nova-3"}}})

    async def send(self, data):
        if isinstance(data, (bytes, bytearray)):
            self.frames += 1
            if self.frames == 3:
                await self.out.put(self.results("Where are", False))
        elif "CloseStream" in str(data):
            await self.out.put(self.results("Where are my keys?", True))
            await self.out.put(json.dumps({"type": "Metadata", "model_info": {}}))
            await self.out.put(None)

    async def recv(self):
        message = await self.out.get()
        if message is None:
            raise Closed()
        return message

    async def close(self):
        pass


def test_a_paid_replay_writes_cassette_turns_from_what_deepgram_said(tmp_path, monkeypatch):
    monkeypatch.setenv("EMET_DEEPGRAM_KEY", "a-fake-key-for-a-fake-server")
    turns = paid.read_misses(a_miss_list(tmp_path))
    chosen = paid.selection({}, "nova-3")
    offline = paid.offline_request(chosen, 16000)
    urls: list[str] = []
    headers: list = []

    async def go():
        # Paced as a microphone would deliver it, so the fake's interim
        # arrives between feeds: 12 frames, about 0.8 s.
        return await paid.replay_turns(turns, chosen, 16000, connect=FakeDeepgram(urls, headers),
                                       offline_url=offline["url"], entry=11, recorded="2026-10-28",
                                       source="misses.json", pace=True)

    out, problems, connections = asyncio.run(go())
    assert problems == []
    assert len(urls) == 2 and set(urls) == {offline["url"]}  # the preflight and one turn
    turn = out[0]
    assert turn["fed_sha256"] == turns[0]["fed_sha256"] and turn["fed_bytes"] == turns[0]["fed_bytes"]
    assert turn["final"]["text"] == "Where are my keys?" and turn["ledger_entry"] == 11
    assert turn["model_version"] == "2025-04-17.21547" and turn["request"]["query"]["smart_format"] == "false"
    raw = turns[0]["bytes"]
    assert [p["text"] for p in turn["partials"]][:1] == ["Where are"]
    for p in turn["partials"]:
        assert p["prefix_sha256"] == hashlib.sha256(raw[:p["after_bytes"]]).hexdigest()
    # The key went to the server in a header, and into nothing kept.
    assert all("a-fake-key" in json.dumps(h) for h in headers) and len(headers) == 2
    assert len(connections) == 2 and connections[1]["audio_bytes_sent"] == len(raw)
    assert "a-fake-key" not in json.dumps(out) and "a-fake-key" not in json.dumps(connections)


def test_a_connection_to_another_url_is_a_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("EMET_DEEPGRAM_KEY", "a-fake-key-for-a-fake-server")
    turns = paid.read_misses(a_miss_list(tmp_path))
    chosen = paid.selection({}, "nova-3")
    expected = paid.offline_request(paid.selection({"diarize": "true"}, "nova-3"), 16000)["url"]
    out, problems, _ = asyncio.run(paid.replay_turns(
        turns, chosen, 16000, connect=FakeDeepgram([]), offline_url=expected, entry=11,
        recorded="2026-10-28", source="misses.json", pace=False))
    assert any(p.startswith("a connection used") and "not the offline URL" in p for p in problems), problems


# ------------------------------------------------- paid runs as cassettes


def a_paid_run(tmp_path: Path, raw: bytes) -> Path:
    """The shape of a dg run's deepgram.json and record.json, one turn."""
    run = tmp_path / "dg99_t"
    run.mkdir()
    frames = len(raw) // 2560
    url = "wss://api.deepgram.com/v1/listen?encoding=linear16&channels=1&interim_results=true&punctuate=true&smart_format=false&sample_rate=16000&model=nova-3"
    received = [{"t": 1.0, "data": {"type": "Results", "metadata": {"model_info": {"name": "n", "version": "2025-04-17.21547", "arch": "a"}}}}]
    (run / "deepgram.json").write_text(json.dumps({
        "mean": "carried: cmninit 37.7991,...", "recording_sha256": "0" * 64,
        "ledger": {"entry_number": 9, "entry": {"time": "2026-10-05 22:00:01 PDT"}},
        "turns": [{"wake": 1, "frames_fed": frames, "bytes_fed": len(raw),
                   "partials": [{"t": 1.0, "after_frames": 3, "text": "Where"}],
                   "final": {"text": "Where are my keys?", "confidence": 0.999, "error": None}}],
        "connections": [{"kind": "preflight", "wake": None, "url": url, "received": []},
                        {"kind": "utterance", "wake": 1, "url": url, "received": received}],
    }), encoding="utf-8")
    (run / "record.json").write_text(json.dumps({"wakes": [{
        "turn_index": 0, "wake_end_sample": 179200, "turn_fed_sha256": hashlib.sha256(raw).hexdigest(),
        "fed_handover_sha256": hashlib.sha256(raw[:7680]).hexdigest(), "handover_samples": 3840}]}), encoding="utf-8")
    return run


def test_a_paid_turn_becomes_a_cassette_turn_with_its_prefixes(tmp_path):
    raw = bytes(range(256)) * 100
    run = a_paid_run(tmp_path, raw)
    p = seed.paid_turns(run)[0]
    assert p["partials"] == [{"text": "Where", "after_bytes": 7680}] and p["ledger_entry"] == 9
    turn = seed.cassette_turn(p, raw)
    assert turn["partials"][0]["prefix_sha256"] == hashlib.sha256(raw[:7680]).hexdigest()
    assert turn["model_version"] == "2025-04-17.21547" and turn["recorded"] == "2026-10-05"
    assert turn["request"] == {"provider": "deepgram", "model": "nova-3", "sample_rate": 16000, "query": {
        "channels": "1", "encoding": "linear16", "interim_results": "true", "punctuate": "true", "smart_format": "false"}}


def test_a_turn_is_kept_only_when_every_byte_is_the_paid_runs(tmp_path):
    raw = bytes(range(256)) * 100
    p = seed.paid_turns(a_paid_run(tmp_path, raw))[0]
    assert seed.judge(p, {179200: raw}) == (raw, "kept")
    assert seed.judge(p, {179200: raw + bytes(2560)})[1] == "the turn ran 10 frames in the paid run and 11 here"
    moved = bytes(2) + raw[2:]
    assert seed.judge(p, {179200: moved})[1].startswith("the handover differs")
    late = raw[:9000] + bytes(1) + raw[9001:]
    assert seed.judge(p, {179200: late})[1] == "the same length and handover, other bytes after it"
    assert seed.judge(p, {181760: raw})[1] == "no wake at sample 179200 in the regeneration"


def test_the_banks_seeded_cassettes_are_the_paid_runs_answers(real_bank):
    """Every cassette turn the bank holds has the sha256, length and final
    its paid run recorded (releases/v0.5.3/harness/runs/, laptop only)."""
    from emet_providers import cassette

    runs = real_bank.parent / "releases" / "v0.5.3" / "harness" / "runs"
    files = sorted((real_bank / "cassettes").glob("dg*.json"))
    if not files or not runs.is_dir():
        pytest.skip("no seeded cassettes or no paid runs on this machine")
    for f in files:
        run = next(runs.glob(f"{f.stem}_*"))
        dg = json.loads((run / "deepgram.json").read_text(encoding="utf-8"))
        record = json.loads((run / "record.json").read_text(encoding="utf-8"))
        turns = cassette.load_file(f)["turns"]
        assert [t["fed_sha256"] for t in turns] == [w["turn_fed_sha256"] for w in record["wakes"]][:len(turns)]
        assert [t["final"]["text"] for t in turns] == [t["final"]["text"] for t in dg["turns"]][:len(turns)]
