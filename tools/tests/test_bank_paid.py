"""The ledger's arithmetic and refusals of tools/bank/paid_replay.py and
tools/bank/seed_cassettes.py, with the offline request, the guard's
decisions, a paid replay against a fake Deepgram, and paid runs turned into
cassettes. Nothing here reaches a network or spends anything."""

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
    """A ledger shaped like releases/v0.5.3/harness/deepgram-ledger.json, with ten spent."""
    times = ["2026-09-29 22:24:12 PDT"] + [f"2026-10-0{d} 23:20:27 PDT" for d in (1, 1, 2, 2, 2, 2, 3, 5, 5)]
    path = tmp_path / "deepgram-ledger.json"
    path.write_text(json.dumps({"allowed": 10, "spent": [{"time": t, "recording": "r"} for t in times]}), encoding="utf-8")
    return path


def a_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.json"
    paid.seed_ledger(old_ledger(tmp_path), path)
    return path


def entry(time: str, kind: str = "replay", audio_s: float = 60.0) -> dict:
    """A ledger entry; a replay counts by its seconds of audio."""
    return {"time": time, "kind": kind, **({"audio_s": audio_s} if kind == "replay" else {})}


# ------------------------------------------------------------- the ledger


def test_the_seeded_ledger_keeps_the_ten_and_spends_none_of_the_standing_allowance(tmp_path):
    path = a_ledger(tmp_path)
    data = paid.ledger_read(path)
    assert len(data["spent"]) == 10 and all(e["kind"] == "replay" for e in data["spent"])
    assert data["spent"][0]["recording"] == "r" and data["before"]["allowed"] == 10
    assert data["monthly"] == {"replay_minutes": 1000, "canaries": 1} and data["standing"]["since"] == "2026-10-27"
    assert data["standing"]["vendor_turns_a_release"] == 200 and "the maintainer, 2026-11-24" in data["standing"]["decided"]
    assert paid.allowance(data, OCT28) == {"month": "2026-10", "allowed_min": 1000.0, "spent_min": 0.0, "left_min": 1000.0}


def test_seeding_refuses_an_existing_ledger_and_a_ledger_not_spent_out(tmp_path):
    path = a_ledger(tmp_path)
    with pytest.raises(paid.Refusal, match="seeded once"):
        paid.seed_ledger(old_ledger(tmp_path), path)
    half = tmp_path / "half.json"
    half.write_text(json.dumps({"allowed": 10, "spent": [{"time": "2026-10-01"}]}), encoding="utf-8")
    with pytest.raises(paid.Refusal, match="ten entries spent"):
        paid.seed_ledger(half, tmp_path / "other.json")


def test_the_allowance_counts_replay_minutes_in_the_month_from_the_day_it_began(tmp_path):
    """Seconds of audio, summed and given in minutes; a canary is no
    replay, and the number of entries does not count."""
    data = paid.ledger_read(a_ledger(tmp_path))
    data["spent"] += [entry("2026-10-27 09:00:00 PDT", audio_s=90.0), entry("2026-10-28 09:00:00 PDT", audio_s=30.0),
                      entry("2026-10-28 10:00:00 PDT", "canary"), entry("2026-11-01 08:00:00 PST", audio_s=600.0)]
    assert paid.allowance(data, OCT28)["spent_min"] == 2.0
    assert paid.allowance(data, datetime.date(2026, 11, 2)) == {"month": "2026-11", "allowed_min": 1000.0,
                                                                 "spent_min": 10.0, "left_min": 990.0}
    assert paid.allowance(data, datetime.date(2026, 10, 26))["allowed_min"] == 0.0


def test_a_months_minutes_spent_refuse_and_say_how_many_are_left(tmp_path):
    data = paid.ledger_read(a_ledger(tmp_path))
    data["spent"] += [entry(f"2026-10-28 10:{m:02d}:00 PDT", audio_s=6000.0) for m in range(9)]
    assert paid.check_spend(data, OCT28, None, 6000.0)["left_min"] == 100.0
    with pytest.raises(paid.Refusal, match=r"900\.0 of 1000 replay minutes spent, 100\.0 left, and this replay is "
                                           r"100\.1; anything more asks the maintainer first"):
        paid.check_spend(data, OCT28, None, 6006.0)
    assert paid.check_spend(data, datetime.date(2026, 11, 1), None, 6006.0)["left_min"] == 1000.0


def test_many_entries_and_one_longer_than_130_s_are_accepted(tmp_path):
    """No cap on the count, and an entry as long as its recording: the soak
    recording's ten minutes in one entry, after 50 short ones."""
    data = paid.ledger_read(a_ledger(tmp_path))
    data["spent"] += [entry(f"2026-10-28 0{m // 10}:{m % 10:02d}:00 PDT", audio_s=2.0) for m in range(50)]
    assert paid.check_spend(data, OCT28, None, 600.0)["left_min"] > 900


def test_the_ledgers_own_monthly_minutes_win(tmp_path):
    data = paid.ledger_read(a_ledger(tmp_path))
    data["monthly"] = {"replay_minutes": 5, "canaries": 1}
    data["spent"].append(entry("2026-10-28 10:00:00 PDT", audio_s=240.0))
    assert paid.allowance(data, OCT28)["left_min"] == 1.0
    with pytest.raises(paid.Refusal, match=r"4\.0 of 5 replay minutes spent, 1\.0 left"):
        paid.check_spend(data, OCT28, None, 61.0)


def test_expect_spent_must_be_what_the_ledger_holds(tmp_path):
    data = paid.ledger_read(a_ledger(tmp_path))
    with pytest.raises(paid.Refusal, match="--expect-spent 9, and the ledger holds 10"):
        paid.check_spend(data, OCT28, 9, 1.0)
    assert paid.check_spend(data, OCT28, 10, 1.0)["left_min"] == 1000.0


def test_an_entry_is_written_and_read_back(tmp_path):
    path = a_ledger(tmp_path)
    assert paid.ledger_spend(path, entry("2026-10-28 12:00:00 PDT", audio_s=150.0), OCT28, 10) == 11
    assert paid.allowance(paid.ledger_read(path), OCT28)["spent_min"] == 2.5


def ledger_text(monthly: dict, spent: list | None = None) -> str:
    return json.dumps({"format": "emet-ledger/1", "spent": spent or [], "monthly": monthly,
                       "standing": {"since": "2026-10-27"}})


@pytest.mark.parametrize("text, expect", [
    (None, "no ledger"),
    ("{", "not JSON"),
    (json.dumps({"spent": []}), "not an emet-ledger/1 file"),
    (ledger_text({"replay_minutes": "1000", "canaries": 1}), '"monthly"'),
    (ledger_text({"replays": 20, "canaries": 1}), "replays was an entry count"),
    (ledger_text({"replay_minutes": 1000, "canaries": 1}, [{"time": "2026-10-28"}]), "no time or no kind"),
    (ledger_text({"replay_minutes": 1000, "canaries": 1}, [{"time": "2026-10-28", "kind": "replay"}]),
     "has no audio_s to count"),
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


def test_the_offline_request_carries_the_opt_out_and_each_keyterm_in_order():
    plain = paid.offline_request(paid.selection({}, "nova-3"), 16000)
    assert plain["problems"] == [] and "&mip_opt_out=true&" in plain["url"] and "keyterm" not in plain["url"]
    terms = paid.offline_request(paid.selection({}, "nova-3", ["Emet", "Hey Emet"]), 16000)
    assert terms["problems"] == []
    assert terms["url"].endswith("&keyterm=Emet&keyterm=Hey+Emet") and "&mip_opt_out=true&" in terms["url"]
    assert terms["query"]["keyterm"] == ["Emet", "Hey Emet"]


def test_a_plugin_that_dropped_the_opt_out_or_moved_a_keyterm_is_a_problem(monkeypatch):
    """The offline check reads the URL the plugin built, so a plugin that
    stopped sending mip_opt_out, or sent the terms in another order, is
    refused before anything is spent."""
    from emet_providers.deepgram import DeepgramTranscriber

    real = DeepgramTranscriber.query

    def without_opt_out(self):
        return {k: v for k, v in real(self).items() if k != "mip_opt_out"}

    monkeypatch.setattr(DeepgramTranscriber, "query", without_opt_out)
    found = paid.offline_request(paid.selection({}, "nova-3"), 16000)["problems"]
    assert any(p.startswith("the URL does not carry mip_opt_out=true") for p in found), found

    def reversed_terms(self):
        out = real(self)
        out["keyterm"] = out["keyterm"][::-1]
        return out

    monkeypatch.setattr(DeepgramTranscriber, "query", reversed_terms)
    found = paid.offline_request(paid.selection({}, "nova-3", ["Emet", "Afflatus"]), 16000)["problems"]
    assert found == ["the plugin's keyterms are ['Afflatus', 'Emet'], not ['Emet', 'Afflatus'] in that order"]


@pytest.mark.parametrize("first", ["mip_opt_out=false", "smart_format=true"])
def test_a_name_sent_twice_is_a_problem_even_when_its_last_value_is_the_expected_one(monkeypatch, first):
    """A plugin that sent mip_opt_out=false and then the real query, with
    mip_opt_out=true in it, would pass every other check, since the last
    value of a name is the one compared. Only the rule against a name sent
    twice refuses it, before anything is spent."""
    from emet_providers.deepgram import DeepgramTranscriber

    real = DeepgramTranscriber.request_url
    monkeypatch.setattr(DeepgramTranscriber, "request_url", lambda self: real(self).replace("?", f"?{first}&", 1))
    found = paid.offline_request(paid.selection({}, "nova-3"), 16000)["problems"]
    assert found == ["the URL names a parameter other than keyterm twice"]


@pytest.mark.parametrize("pair, expect", [
    ("mip_opt_out=false", "sends mip_opt_out=true with every request"),
    ("mip_opt_out=true", "a run does not change it"),
    ("keyterm=Emet", "a --keyterm of its own"),
    ("keyterms=Emet", "a --keyterm of its own"),
])
def test_the_opt_out_and_keyterms_are_never_query_pairs(pair, expect):
    with pytest.raises(paid.Refusal, match=expect):
        paid.parse_query([pair])


def fake_keys(tmp_path: Path) -> Path:
    """A keys file holding a fake Deepgram key, for a run that must refuse
    before it would use one."""
    keys = tmp_path / "keys.env"
    keys.write_text("EMET_DEEPGRAM_KEY=a-fake-key-for-no-server-0123456789\n", encoding="utf-8")
    return keys


def a_real_run_reads_keys_here(monkeypatch) -> None:
    """A run that is not a dry run pops the EMET_ variables and loads the
    keys file into the environment, and sets sounddevice to None: all three
    put back after the test."""
    monkeypatch.setitem(sys.modules, "sounddevice", sys.modules.get("sounddevice"))
    monkeypatch.setattr(paid.os, "environ", dict(os.environ))


@pytest.mark.parametrize("dry_run", [True, False])
def test_keyterms_the_plugin_refuses_refuse_the_run_before_the_ledger(tmp_path, capsys, monkeypatch, dry_run):
    """A real run as well as a dry one: a refusal that held only for a dry
    run would let a real run spend its entry and then find the plugin will
    not start."""
    misses = a_miss_list(tmp_path)
    over = {"dry_run": True} if dry_run else {"keys": fake_keys(tmp_path), "expect_spent": 10}
    args = plan_args(tmp_path, misses, **over) + ["--keyterm", "Emet,Emmett"]
    a_real_run_reads_keys_here(monkeypatch)
    monkeypatch.setattr(paid.Guard, "install", lambda self: pytest.fail("the guard was installed for a refused run"))
    before = (tmp_path / "ledger.json").read_bytes()
    assert paid.main(args, today=OCT28) == 2
    err = capsys.readouterr().err
    assert "refused, nothing spent: the plugin refuses the request: params.keyterms entry 1, 'Emet,Emmett', holds a comma" in err
    assert (tmp_path / "ledger.json").read_bytes() == before
    found = paid.offline_request(paid.selection({}, "nova-2", ["Emet"]), 16000)["problems"]
    assert found[0].startswith("the plugin refuses the request: params.keyterms is for a Nova-3 model")


def two_recordings(tmp_path: Path) -> Path:
    """A miss list of three turns from two recordings."""
    (tmp_path / "m").mkdir(exist_ok=True)
    rows = []
    for n, (recording, frames) in enumerate((("one-breath-t", 12), ("how-far-t", 25), ("how-far-t", 50)), 1):
        raw = bytes((n + i) % 251 for i in range(frames * 2560))
        name = f"{recording}-turn-{n:04d}.raw"
        (tmp_path / "m" / name).write_bytes(raw)
        rows.append({"recording": recording, "recording_sha256": "0" * 64, "turn": n, "take": f"{recording}/01",
                     "fed_sha256": hashlib.sha256(raw).hexdigest(), "fed_bytes": len(raw),
                     "feed_sizes": [2560] * frames, "handover_feeds": 2, "raw": name})
    path = tmp_path / "m" / "misses.json"
    path.write_text(json.dumps({"format": "emet-misses/1", "turns": rows}), encoding="utf-8")
    return path


def test_the_dry_run_prints_each_recordings_minutes_the_total_and_the_estimate(tmp_path, capsys, monkeypatch):
    """Spending nothing: no connection is attempted, and the stand-in that
    would answer one counts zero. The run is given everything a real run
    needs (a keys file with a key, the entry count, websockets on the
    stand-in, a guard that installs nothing, the real replay), so a dry run
    that went on would reach the stand-in, and the count could fail."""
    client = pytest.importorskip("websockets.asyncio.client")
    misses = two_recordings(tmp_path)
    server = FakeDeepgram([])
    monkeypatch.setattr(client, "connect", server)
    a_real_run_reads_keys_here(monkeypatch)
    monkeypatch.setattr(paid.Guard, "install", lambda self: None)
    environ = dict(paid.os.environ)
    args = plan_args(tmp_path, misses, dry_run=True, keys=fake_keys(tmp_path), expect_spent=10, unpaced=True)
    before = (tmp_path / "ledger.json").read_bytes()
    code = paid.main(args + ["--keyterm", "Emet"], today=OCT28)
    assert server.urls == [], "the stand-in server counts zero connections"
    assert code == 0
    assert paid.os.environ == environ, "the dry run read the keys into a mapping of its own"
    out = capsys.readouterr().out
    assert "3 turn(s), 6.96 s of audio; 2026-10: 0.0 of 1000 replay minutes spent, 1000.0 left" in out
    assert "  one-breath-t: 1 turn(s), 0.96 s, 0.02 min\n  how-far-t: 2 turn(s), 6.00 s, 0.10 min\n" in out
    assert "  total 3 turn(s), 0.12 min against 1000.0 left in 2026-10; 999.9 left after" in out
    assert ("  estimate about $0.0007 at $0.0048 a minute for nova-3 and $0.0013 a minute for keyterms "
            "(deepgram.com/pricing, pay as you go, read 2026-11-26); the bill is Deepgram's") in out
    assert "  keys EMET_DEEPGRAM_KEY from " in out and "a-fake-key" not in out
    assert "not checked" not in out, "with --keys and --expect-spent every check ran"
    assert "&mip_opt_out=true&" in out and "&keyterm=Emet" in out
    entry = json.loads(out.rstrip().splitlines()[-1])
    assert entry["kind"] == "replay" and entry["audio_s"] == 6.96 and entry["keyterms"] == ["Emet"]
    assert entry["turns"] == ["one-breath-t/1", "how-far-t/2", "how-far-t/3"]
    assert "mip_opt_out=true" in entry["request_url"]
    assert (tmp_path / "ledger.json").read_bytes() == before and not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("over, words", [
    ({"expect_spent": 99}, "--expect-spent 99, and the ledger holds 10 entries"),
    ({"keys": "absent.env"}, "no keys file at"),
    ({"keys": "keys.env"}, "holds no EMET_DEEPGRAM_KEY"),
])
def test_a_dry_run_given_the_keys_or_the_count_refuses_what_a_real_run_would(tmp_path, capsys, monkeypatch, over, words):
    """The dry run is the step before the paid one, so what it is given it
    checks as the real run does."""
    misses = a_miss_list(tmp_path)
    (tmp_path / "keys.env").write_text("# no key here\n", encoding="utf-8")
    over = {k: (tmp_path / v if k == "keys" else v) for k, v in over.items()}
    a_real_run_reads_keys_here(monkeypatch)
    no_way_out(monkeypatch)
    args = plan_args(tmp_path, misses, dry_run=True, **over)
    before = (tmp_path / "ledger.json").read_bytes()
    assert paid.main(args, today=OCT28) == 2
    assert words in capsys.readouterr().err
    assert (tmp_path / "ledger.json").read_bytes() == before and not (tmp_path / "out.json").exists()


def test_a_dry_run_names_what_it_left_unchecked(tmp_path, capsys, monkeypatch):
    no_way_out(monkeypatch)
    assert paid.main(plan_args(tmp_path, a_miss_list(tmp_path), dry_run=True), today=OCT28) == 0
    assert ("  not checked, which a real run checks before it spends: the keys file and websockets (give --keys); "
            "the ledger's entry count (give --expect-spent N)\n") in capsys.readouterr().out


def test_the_estimate_names_a_model_with_no_price_read():
    assert paid.estimate(60.0, "nova-3", False).startswith("about $0.0048 at $0.0048 a minute for nova-3")
    assert paid.estimate(60.0, "nova-2", False).startswith("no price read for nova-2")


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


def test_a_miss_list_longer_than_130_s_plans_as_one_entry(tmp_path, capsys):
    """The 130 s cap of D6's reading is gone (the maintainer, 2026-11-24)."""
    misses = a_miss_list(tmp_path, seconds=131.0)
    assert paid.main(plan_args(tmp_path, misses, dry_run=True), today=OCT28) == 0
    out = capsys.readouterr().out
    assert "130.96 s of audio; 2026-10: 0.0 of 1000 replay minutes spent, 1000.0 left" in out
    assert '"audio_s": 130.96' in out


def test_a_spent_month_refuses_the_dry_run_too(tmp_path, capsys):
    misses = a_miss_list(tmp_path)
    args = plan_args(tmp_path, misses, dry_run=True)
    data = paid.ledger_read(tmp_path / "ledger.json")
    data["spent"] += [entry("2026-10-28 10:00:00 PDT", audio_s=60000.0)]
    (tmp_path / "ledger.json").write_text(json.dumps(data), encoding="utf-8")
    assert paid.main(args, today=OCT28) == 2
    assert "1000.0 of 1000 replay minutes spent, 0.0 left" in capsys.readouterr().err
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
    a_real_run_reads_keys_here(monkeypatch)
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
    """What websockets raises when the server closes, which has rcvd and sent."""

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
    # The opt-out on the wire, the preflight included, and in the key.
    assert all("&mip_opt_out=true&" in url for url in urls)
    assert turn["request"]["query"]["mip_opt_out"] == "true" and "keyterm" not in turn["request"]["query"]


def test_a_paid_replay_with_keyterms_sends_them_and_keys_the_turn_by_them(tmp_path, monkeypatch):
    from emet_providers import cassette

    monkeypatch.setenv("EMET_DEEPGRAM_KEY", "a-fake-key-for-a-fake-server")
    turns = paid.read_misses(a_miss_list(tmp_path))
    chosen = paid.selection({}, "nova-3", ["Emet", "Hey Emet"])
    offline = paid.offline_request(chosen, 16000)
    urls: list[str] = []
    out, problems, connections = asyncio.run(paid.replay_turns(
        turns, chosen, 16000, connect=FakeDeepgram(urls), offline_url=offline["url"], entry=11,
        recorded="2026-11-26", source="misses.json", pace=False))
    assert problems == [] and len(urls) == 2
    assert all(url.endswith("&keyterm=Emet&keyterm=Hey+Emet") for url in urls)
    assert all(c["url"].endswith("&keyterm=Emet&keyterm=Hey+Emet") for c in connections), "kept, not dropped as a credential"
    assert out[0]["request"]["query"]["keyterm"] == ["Emet", "Hey Emet"]
    player = cassette.Player(out, request=cassette.request_for("deepgram", paid.offline_request(
        paid.selection({}, "nova-3", ["Emmett"]), 16000)["query"]))
    player.feed(turns[0]["bytes"])
    assert not player.finish().hit, "an answer recorded with one list never stands for another"


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


def test_a_paid_run_of_0_5_3_is_keyed_by_its_old_request_and_says_it_predates_the_opt_out(tmp_path):
    from emet_providers import cassette

    raw = bytes(range(256)) * 100
    turn = seed.cassette_turn(seed.paid_turns(a_paid_run(tmp_path, raw))[0], raw)
    assert cassette.predates_opt_out(turn["request"])
    lines = seed.key_lines([turn], 16000)
    assert lines[0].startswith('  keyed by {"model": "nova-3", "provider": "deepgram"')
    assert lines[1] == ("  1 of 1 turn(s) predate the opt-out key: recorded without mip_opt_out, they answer no "
                        "request the plugin makes from 0.5.6 on. A reseed through tools/bank/paid_replay.py would "
                        "send 0.80 s, 0.01 min, of their audio")


def test_a_paid_runs_url_with_the_opt_out_and_keyterms_keys_its_turn_by_both(tmp_path):
    """The new key read back from a URL: every keyterm in order. Such a
    turn is no longer stale, so no reseed line is printed for it."""
    raw = bytes(range(256)) * 100
    run = a_paid_run(tmp_path, raw)
    dg = json.loads((run / "deepgram.json").read_text(encoding="utf-8"))
    for conn in dg["connections"]:
        conn["url"] += "&mip_opt_out=true&keyterm=Emet&keyterm=Hey+Emet"
    (run / "deepgram.json").write_text(json.dumps(dg), encoding="utf-8")
    turn = seed.cassette_turn(seed.paid_turns(run)[0], raw)
    assert turn["request"]["query"]["keyterm"] == ["Emet", "Hey Emet"]
    assert turn["request"]["query"]["mip_opt_out"] == "true"
    assert len(seed.key_lines([turn], 16000)) == 1


def a_seed_bank(tmp_path: Path, raw: bytes) -> tuple[Path, Path, Path]:
    """A bank of one recording, a paid run of it, and a regeneration made
    elsewhere holding the paid turn's bytes: seed's whole path, no decoder."""
    from conftest import write_wav

    import manifest as bank_files

    wav = write_wav(tmp_path / "rec" / "t-0.1.wav", bytes(4 * 32000))
    doc = {
        "format": "emet-bank/1", "id": "t-0.1", "path": "../rec/t-0.1.wav", "sha256": bank_files.sha256_file(wav),
        "sample_rate": 16000, "channels": 1, "bits": 16, "frames": 64000, "duration_s": 4.0,
        "recorded": "2026-10-27", "start_time": None, "device": None, "capture_level": None,
        "room": "unrecorded", "position": None, "distance": "near", "distance_m": None, "facing": None,
        "speaker": "s01", "noise": None, "kind": "takes", "lead_s": 0.0, "prompt_every_s": 4.0, "made_with": "a test",
        "takes": [{"id": "t-0.1/01", "kind": "one_breath", "text": "hey emet, stop", "question": "stop",
                   "first_sound": "s", "slot": {"start": 0, "end": 64000}, "expect_wake": True,
                   "retake_of": None, "exclude": None}],
    }
    root = tmp_path / "bank"
    bank_files.write(root / "manifests" / "t-0.1.json", doc, "manifest")
    bank_files.write(root / "bank.json", {
        "format": "emet-bank/1", "version": "2026-10-27", "files": ["manifests/t-0.1.json"],
        "rooms": {"unrecorded": {"description": "no record names the room"}},
        "speakers": {"s01": {"consent": "consent/s01.txt"}}}, "bank")
    run = a_paid_run(tmp_path, raw)
    dg = json.loads((run / "deepgram.json").read_text(encoding="utf-8"))
    dg["recording_sha256"] = doc["sha256"]
    (run / "deepgram.json").write_text(json.dumps(dg), encoding="utf-8")
    regen = tmp_path / "regen"
    regen.mkdir()
    (regen / "turn-0001.raw").write_bytes(raw)
    (regen / "turns.jsonl").write_text(json.dumps({"turn": 1, "bytes": len(raw), "feed_sizes": [2560] * 10,
                                                   "fed_sha256": hashlib.sha256(raw).hexdigest()}) + "\n",
                                       encoding="utf-8")
    (regen / "wakes.json").write_text(json.dumps({"wake_end_samples": [179200]}), encoding="utf-8")
    return root, run, regen


def test_seeds_dry_run_judges_every_turn_and_writes_nothing(tmp_path, capsys):
    from emet_providers import cassette

    raw = bytes(range(256)) * 100
    root, run, regen = a_seed_bank(tmp_path, raw)
    out = tmp_path / "dg99.json"
    args = [str(root), str(run), "--out", str(out), "--regenerated", str(regen)]
    assert seed.main(args + ["--dry-run"]) == 0
    said = capsys.readouterr().out
    assert "seed: dg99_t: 1 of 1 turn(s) kept\n" in said and "turn 1 (wake at 11.20 s): kept" in said
    assert "1 of 1 turn(s) predate the opt-out key" in said and "would send 0.80 s, 0.01 min" in said
    assert said.rstrip().endswith("dry run: no cassette written to --out. This tool sends nothing and writes no ledger line.")
    assert not out.exists()
    assert seed.main(args + ["--provisional", "--dry-run"]) == 0
    assert "a provisional cassette of 1 turn(s)\n" in capsys.readouterr().out and not out.exists()
    assert seed.main(args) == 0
    assert f"1 of 1 turn(s) kept, in {out}" in capsys.readouterr().out
    assert [t["fed_sha256"] for t in cassette.load(out)] == [hashlib.sha256(raw).hexdigest()]


def test_seeds_dry_run_on_this_tree_writes_only_into_the_work_folder(tmp_path, capsys, monkeypatch):
    """Without --regenerated the second pass replays with the provisional
    cassette, so a dry run writes that one into the work folder, and says
    where when --work keeps it; nothing goes to --out. The replays are
    stood in for: each pass gives the paid turn's bytes."""
    raw = bytes(range(256)) * 100
    root, run, _ = a_seed_bank(tmp_path, raw)
    passes: list = []

    def regenerate(doc, wav, work, mean, cassette_path, request):
        passes.append((work.name, cassette_path, request))
        return {179200: raw}

    monkeypatch.setattr(seed, "regenerate_here", regenerate)
    out, work = tmp_path / "dg99.json", tmp_path / "work"
    work.mkdir()
    assert seed.main([str(root), str(run), "--out", str(out), "--work", str(work), "--dry-run"]) == 0
    said = capsys.readouterr().out
    assert "seed: dg99_t: 1 of 1 turn(s) kept\n" in said and not out.exists()
    assert [(name, request) for name, _, request in passes] == [("pass1", None), ("pass2", None)], \
        "the second pass names no request"
    assert passes[1][1].resolve() == (work / "provisional.json").resolve() and (work / "provisional.json").is_file()
    assert said.rstrip().endswith(f"The second pass's provisional cassette stays in {work}, as --work asked.")


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
