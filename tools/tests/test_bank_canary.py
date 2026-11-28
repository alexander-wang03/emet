"""The monthly canary against fakes, with no network, no key and no vendor.

The live tests are a function that says how they went, Deepgram is a
stand-in that answers CloseStream the way the 2026-10-05 replays recorded
it, and the keys are fake values in the environment beside an empty keys
file. What is tested is what protects money and keys: the refusals, the
ledger entry written before anything runs, the compare, and the exit codes.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import collections
import http.server
import importlib.util
import json
import threading
import types
from pathlib import Path

import pytest

from emet_sdk.types import AudioFormat

from emet_providers import cassette
from emet_providers.deepgram import DeepgramTranscriber

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("bank_canary", REPO / "tools" / "bank" / "canary.py")
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)

VERSION = "2025-04-17.21547"
FINAL = "Where are my keys?"
NOW = datetime.datetime(2026, 10, 27, 23, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-7), "PDT"))
AUDIO = bytes((i * 31) % 251 for i in range(25 * 2560))
FAKE_KEYS = {
    "EMET_DEEPGRAM_KEY": "dg-canary-test-key-0123456789",
    "EMET_OPENAI_KEY": "sk-canary-test-key-0123456789",
    "EMET_ANTHROPIC_KEY": "sk-ant-canary-test-key-0123456789",
}


def todays_request(**query) -> dict:
    stt = DeepgramTranscriber({"provider": "deepgram", "params": {"query": query}}, AudioFormat())
    return cassette.request_for("deepgram", stt.query())


def old_entry(time: str, **extra) -> dict:
    return {"time": time, "recording": "recordings/one-breath-0.5.2.wav", "sha256": "0" * 64, **extra}


# ----------------------------------------------------------------- fakes


class _Closed(Exception):
    def __init__(self) -> None:
        super().__init__("closed")
        self.rcvd = self.sent = types.SimpleNamespace(code=1000, reason="")


class Deepgram:
    """Answers each connection: on CloseStream, one final, the Metadata with
    the model and the sha256 of the audio received, then the close."""

    def __init__(self, ledger: Path | None = None, *, final: str = FINAL, version: str = VERSION, sha256: str | None = None):
        self.ledger = ledger
        self.final = final
        self.version = version
        self.sha256 = sha256
        self.connections = 0
        self.urls: list[str] = []

    async def connect(self, url, **kwargs):
        if self.ledger is not None:
            spent = json.loads(self.ledger.read_text(encoding="utf-8"))["spent"]
            assert spent[-1]["kind"] == "canary", "a connection opened before the ledger entry"
        self.connections += 1
        self.urls.append(url)
        return _Socket(self)


class _Socket:
    def __init__(self, server: Deepgram) -> None:
        self.server = server
        self.audio = hashlib.sha256()
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, data):
        if isinstance(data, bytes):
            self.audio.update(data)
            return
        if json.loads(data).get("type") == "CloseStream":
            info = {"name": "general-nova-3", "version": self.server.version, "arch": "nova-3"}
            self.inbox.put_nowait(json.dumps({
                "type": "Results", "is_final": True, "metadata": {"model_info": info},
                "channel": {"alternatives": [{"transcript": self.server.final, "confidence": 0.99}]},
            }))
            self.inbox.put_nowait(json.dumps({
                "type": "Metadata", "sha256": self.server.sha256 or self.audio.hexdigest(),
                "model_info": {"40bd": info},
            }))
            self.inbox.put_nowait(_Closed())

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        pass


class LiveTests:
    def __init__(self, ledger: Path, outcomes: dict | None = None):
        self.ledger = ledger
        self.outcomes = outcomes
        self.envs: list[dict] = []

    def __call__(self, env, out):
        spent = json.loads(self.ledger.read_text(encoding="utf-8"))["spent"]
        assert spent[-1]["kind"] == "canary", "the live tests ran before the ledger entry"
        self.envs.append(env)
        return dict(self.outcomes or {t: "passed" for t in canary.LIVE_TESTS})


async def no_wait(_seconds):
    return None


#: Rows shaped as `emet_providers.lifecycle.DefaultModel`'s, the fields the
#: canary reads: one a vendor, as the table's four defaults are.
Row = collections.namedtuple("Row", "vendor model retires")
ROWS = (
    Row("openai", "gpt-5.6-terra", None),
    Row("anthropic", "claude-opus-5", None),
    Row("deepgram", "nova-3", None),
    Row("deepgram", "aura-2-thalia-en", None),
)


class Vendors:
    """The three models endpoints, answering as each vendor's reference
    says (read 2026-11-27), with no network. `answers` overrides a URL."""

    def __init__(self, answers: dict | None = None):
        self.answers = answers or {}
        self.asked: list[tuple[str, dict, float]] = []

    def __call__(self, url, headers, timeout):
        self.asked.append((url, headers, timeout))
        if url in self.answers:
            answer = self.answers[url]
            if isinstance(answer, BaseException):
                raise answer
            return answer[0], json.dumps(answer[1]).encode("utf-8")
        if url.startswith("https://api.openai.com/v1/models/"):
            return 200, json.dumps({"id": url.rsplit("/", 1)[1], "object": "model", "created": 1,
                                    "owned_by": "openai", "shutdown_date": None}).encode("utf-8")
        if url.startswith("https://api.anthropic.com/v1/models/"):
            return 200, json.dumps({"type": "model", "id": url.rsplit("/", 1)[1], "lifecycle": "active",
                                    "retires_at": None}).encode("utf-8")
        if url == "https://api.deepgram.com/v1/models":
            return 200, json.dumps({"stt": [{"canonical_name": "nova-3"}],
                                    "tts": [{"canonical_name": "aura-2-thalia-en"}]}).encode("utf-8")
        return 404, b"{}"


@pytest.fixture
def bank(tmp_path, monkeypatch):
    for name, value in FAKE_KEYS.items():
        monkeypatch.setenv(name, value)
    rec = cassette.TurnRecorder(todays_request())
    for start in range(0, len(AUDIO), 2560):
        rec.feed(AUDIO[start:start + 2560])
    turn = rec.turn(FINAL, confidence=0.999, model_version=VERSION, recorded="2026-10-05", ledger_entry=9, source="dg09 wakes[0]")
    cassette.save(tmp_path / "cassette.json", [turn])
    (tmp_path / "turns").mkdir()
    (tmp_path / "turns" / "turn-0001.raw").write_bytes(AUDIO)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"allowed": 10, "spent": [old_entry("2026-10-05 22:00:01 PDT"), old_entry("2026-10-05 22:03:04 PDT")]}), encoding="utf-8")
    (tmp_path / "keys.env").write_text("", encoding="utf-8")
    return types.SimpleNamespace(root=tmp_path, ledger=ledger, turn=turn)


def argv(bank, *extra, out="out", audio=None) -> list[str]:
    return [
        "--ledger", str(bank.ledger), "--cassette", str(bank.root / "cassette.json"), "--turn", "1",
        "--audio", str(audio or bank.root / "turns"), "--out", str(bank.root / out),
        "--keys", str(bank.root / "keys.env"), *extra,
    ]


def run(bank, *extra, server=None, tests=None, rows=ROWS, vendors=None, **kw):
    server = server or Deepgram(bank.ledger)
    tests = tests or LiveTests(bank.ledger)
    code = canary.main(argv(bank, *extra, **kw), run_tests=tests, connect=server.connect, sleep=no_wait,
                       now=lambda: NOW, engine=lambda: {"repo_head": "test", "tree_clean": True, "package_versions": {}},
                       rows=lambda: rows, fetch=vendors or Vendors())
    return code, server, tests


# -------------------------------------------------------------- refusals


def test_without_spend_it_refuses_and_spends_nothing(bank, capsys):
    before = bank.ledger.read_bytes()
    code, server, tests = run(bank)
    out = capsys.readouterr().out
    assert code == 2
    assert "refused: the canary spends money. Run it again with --spend" in out
    assert "sha256 matches" in out and "0 of 1 canaries" in out, "the offline checks ran first"
    assert bank.ledger.read_bytes() == before
    assert server.connections == 0 and tests.envs == []
    assert not (bank.root / "out").exists()


def test_a_months_canary_spent_is_refused(bank, capsys):
    data = json.loads(bank.ledger.read_text(encoding="utf-8"))
    data["spent"].append(old_entry("2026-10-01 09:00:00 PDT", kind="canary"))
    bank.ledger.write_text(json.dumps(data), encoding="utf-8")
    before = bank.ledger.read_bytes()
    code, server, tests = run(bank, "--spend")
    assert code == 2
    assert "canary for 2026-10 is spent" in capsys.readouterr().out
    assert bank.ledger.read_bytes() == before and server.connections == 0 and tests.envs == []


def test_last_months_canary_does_not_count(bank):
    data = json.loads(bank.ledger.read_text(encoding="utf-8"))
    data["spent"].append(old_entry("2026-09-30 23:59:59 PDT", kind="canary"))
    bank.ledger.write_text(json.dumps(data), encoding="utf-8")
    assert run(bank, "--spend")[0] == 0


def test_entries_before_the_standing_allowance_do_not_count(bank):
    """`paid_replay.allowance` counts from `standing.since`; the canary
    counts the same way, so the two tools never disagree about a month."""
    data = json.loads(bank.ledger.read_text(encoding="utf-8"))
    data["standing"] = {"since": "2026-10-27"}
    data["spent"].append(old_entry("2026-10-05 22:00:01 PDT", kind="canary"))
    bank.ledger.write_text(json.dumps(data), encoding="utf-8")
    assert canary.month_counts(data, "2026-10") == (0, 0)
    assert run(bank, "--spend")[0] == 0
    data["spent"].append(old_entry("2026-10-27 09:00:00 PDT", kind="canary"))
    assert canary.month_counts(data, "2026-10") == (0, 1)


def test_the_ledgers_own_monthly_allowance_wins(bank):
    data = json.loads(bank.ledger.read_text(encoding="utf-8"))
    data["spent"].append(old_entry("2026-10-01 09:00:00 PDT", kind="canary"))
    data["monthly"] = {"replay_minutes": 1000, "canaries": 2}
    bank.ledger.write_text(json.dumps(data), encoding="utf-8")
    assert run(bank, "--spend")[0] == 0
    assert canary.allowance({"monthly": {"replay_minutes": 5, "canaries": 3}}) == (5.0, 3)
    assert canary.allowance({}) == (1000.0, 1)


def minutes_ledger() -> dict:
    """The bank's ledger shape, with kinds, `standing.since` and replays by `audio_s`."""
    return {"format": "emet-ledger/1", "monthly": {"replay_minutes": 1000, "canaries": 1},
            "standing": {"since": "2026-10-27"}, "spent": [
                old_entry("2026-10-05 22:00:01 PDT", kind="replay"),
                old_entry("2026-11-02 10:00:00 PST", kind="replay", audio_s=600.0),
                old_entry("2026-11-03 10:00:00 PST", kind="replay", audio_s=150.0),
                old_entry("2026-11-04 10:00:00 PST", kind="canary"),
                old_entry("2026-12-01 10:00:00 PST", kind="replay", audio_s=30.0)]}


def test_the_canary_counts_replay_minutes_as_the_paid_replay_does():
    import paid_replay  # tools/bank is on the path (conftest.py)

    data = minutes_ledger()
    assert canary.month_counts(data, "2026-11") == (12.5, 1)
    assert canary.month_counts(data, "2026-10") == (0.0, 0)
    for month, day in (("2026-11", datetime.date(2026, 11, 24)), ("2026-12", datetime.date(2026, 12, 2))):
        assert canary.month_counts(data, month)[0] == paid_replay.allowance(data, day)["spent_min"]
    assert canary.canary_allowed({**data, "spent": data["spent"][:3]}, "2026-11") == (
        "2026-11: 12.5 of 1000 replay minutes and 0 of 1 canaries spent")


def test_spent_replay_minutes_leave_the_canary_its_own(bank):
    """The canary is one a month whatever the replays spent."""
    data = json.loads(bank.ledger.read_text(encoding="utf-8"))
    data.update(monthly={"replay_minutes": 1, "canaries": 1}, standing={"since": "2026-10-27"})
    data["spent"].append(old_entry("2026-10-28 09:00:00 PDT", kind="replay", audio_s=600.0))
    bank.ledger.write_text(json.dumps(data), encoding="utf-8")
    assert run(bank, "--spend")[0] == 0


@pytest.mark.parametrize("monthly, words", [
    ({"replays": 20, "canaries": 1}, "was an entry count"),
    ({"replay_minutes": "1000", "canaries": 1}, "monthly.replay_minutes"),
    ({"replay_minutes": -1, "canaries": 1}, "monthly.replay_minutes"),
])
def test_a_monthly_that_is_not_minutes_is_refused(monthly, words):
    with pytest.raises(canary.Refusal, match=words):
        canary.allowance({"monthly": monthly})


def test_a_replay_with_no_seconds_under_the_allowance_is_refused(tmp_path):
    data = minutes_ledger()
    data["spent"].append(old_entry("2026-11-05 10:00:00 PST", kind="replay"))
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(canary.Refusal, match="entry 6, a replay under the allowance, has no audio_s"):
        canary.ledger_read(path)


@pytest.mark.parametrize(
    "content, words",
    [
        (None, "no ledger"),
        ("{oops", "not JSON"),
        (json.dumps({"allowed": 10}), '"spent"'),
        (json.dumps({"spent": [{"recording": "x"}]}), "entry 1 has no time"),
        (json.dumps({"spent": [], "monthly": {"canaries": "1"}}), "monthly.canaries"),
    ],
)
def test_a_ledger_that_cannot_be_read_is_refused(bank, capsys, content, words):
    if content is None:
        bank.ledger.unlink()
    else:
        bank.ledger.write_text(content, encoding="utf-8")
    code, server, tests = run(bank, "--spend")
    assert code == 2 and words in capsys.readouterr().out
    assert server.connections == 0 and tests.envs == []


def test_audio_that_is_not_the_turns_is_refused_before_the_ledger(bank, capsys):
    changed = bytearray(AUDIO)
    changed[1000] ^= 1
    (bank.root / "other.raw").write_bytes(bytes(changed))
    before = bank.ledger.read_bytes()
    code, server, _ = run(bank, "--spend", audio=bank.root / "other.raw")
    assert code == 2 and "sha256" in capsys.readouterr().out
    assert bank.ledger.read_bytes() == before and server.connections == 0


def test_a_directory_is_searched_by_hash(bank, capsys):
    (bank.root / "turns" / "turn-0000.raw").write_bytes(bytes(100))
    run(bank)
    assert "turn-0001.raw, sha256 matches" in capsys.readouterr().out


def test_a_turn_the_plugin_would_not_ask_for_today_is_refused(bank, capsys):
    """FORMAT.md's example request has no interim_results, which the plugin
    always sends: replaying it live would ask Deepgram something else."""
    request = todays_request()
    request["query"].pop("interim_results")
    turn = dict(bank.turn, request=request)
    cassette.save(bank.root / "cassette.json", [turn])
    code, server, _ = run(bank, "--spend")
    assert code == 2 and "the plugin would ask for" in capsys.readouterr().out
    assert server.connections == 0


def test_a_turn_from_before_the_opt_out_key_is_refused_with_its_date(bank, capsys):
    """Bank v0's cassettes were recorded without mip_opt_out. The plugin
    sends it with every request from 0.5.6, so a live replay of such a turn
    would ask Deepgram another question: refused, named with its day, and
    nothing spent."""
    request = todays_request()
    request["query"].pop("mip_opt_out")
    cassette.save(bank.root / "cassette.json", [dict(bank.turn, request=request)])
    before = bank.ledger.read_bytes()
    code, server, _ = run(bank, "--spend")
    out = capsys.readouterr().out
    assert code == 2 and "recorded on 2026-10-05 for a request without mip_opt_out" in out
    assert "Pick a turn recorded from 0.5.6 on with --turn" in out
    assert "None of the 1 turn(s) here carries mip_opt_out" in out
    assert server.connections == 0 and bank.ledger.read_bytes() == before


def test_a_directory_of_old_and_new_cassettes_names_the_first_turn_with_the_opt_out(bank, capsys):
    """The bank's directory holds bank v0's files, which sort ahead of the
    reseed's, so turn 1 is an old one. Its refusal names the turn to pick,
    and that turn passes every check that needs no money."""
    request = todays_request()
    request["query"].pop("mip_opt_out")
    old = dict(bank.turn, request=request, recorded="2026-10-01", source="dg02 wakes[0]")
    folder = bank.root / "cassettes"
    folder.mkdir()
    cassette.save(folder / "dg02.json", [old, old])
    cassette.save(folder / "paid-2026-11-26.json", [bank.turn])
    args = argv(bank)
    args[args.index("--cassette") + 1] = str(folder)
    before = bank.ledger.read_bytes()
    assert canary.main(args, run_tests=LiveTests(bank.ledger), now=lambda: NOW) == 2
    out = capsys.readouterr().out
    assert "refused: the turn was recorded on 2026-10-01 for a request without mip_opt_out" in out
    assert "Of the 3 turn(s) here, 1 carries mip_opt_out, the first --turn 3" in out
    args[args.index("--turn") + 1] = "3"
    assert canary.main(args, run_tests=LiveTests(bank.ledger), now=lambda: NOW) == 2
    out = capsys.readouterr().out
    assert "canary: turn 3 of" in out and "sha256 matches" in out
    assert "refused: the canary spends money. Run it again with --spend" in out
    assert bank.ledger.read_bytes() == before


def test_a_turn_whose_keyterms_the_plugin_refuses_is_refused_cleanly(bank, capsys):
    """A cassette takes any non-empty text as a keyterm, so a turn holding a
    comma-joined one loads. The plugin refuses it at construction, and the
    canary says so and exits 2, before the ledger, the keys or a
    connection."""
    request = todays_request()
    request["query"]["keyterm"] = ["Emet,Emmett"]
    cassette.save(bank.root / "cassette.json", [dict(bank.turn, request=request)])
    before = bank.ledger.read_bytes()
    code, server, tests = run(bank, "--spend")
    out = capsys.readouterr().out
    assert code == 2
    assert "refused: the plugin refuses the turn's request: params.keyterms entry 1, 'Emet,Emmett', holds a comma" in out
    assert server.connections == 0 and tests.envs == [] and bank.ledger.read_bytes() == before


def test_every_connection_carries_the_opt_out_and_the_turns_keyterms_in_order(bank, capsys):
    request = cassette.request_for("deepgram", DeepgramTranscriber(
        {"provider": "deepgram", "params": {"keyterms": ["Emet", "Hey Emet"]}}, AudioFormat()).query())
    cassette.save(bank.root / "cassette.json", [dict(bank.turn, request=request)])
    code, server, _ = run(bank, "--spend")
    assert code == 0 and capsys.readouterr().out.rstrip().splitlines()[-1] == "fresh"
    assert len(server.urls) == 2, "the preflight and the turn"
    for url in server.urls:
        assert "&mip_opt_out=true&" in url and url.endswith("&keyterm=Emet&keyterm=Hey+Emet")


def test_no_deepgram_key_is_refused_before_the_ledger(bank, capsys, monkeypatch):
    monkeypatch.delenv("EMET_DEEPGRAM_KEY")
    before = bank.ledger.read_bytes()
    assert run(bank, "--spend")[0] == 2
    assert "no EMET_DEEPGRAM_KEY" in capsys.readouterr().out
    assert bank.ledger.read_bytes() == before


def test_a_bad_keys_line_is_reported_by_its_place_and_never_quoted(bank, capsys, monkeypatch):
    monkeypatch.delenv("EMET_DEEPGRAM_KEY")
    (bank.root / "keys.env").write_text("dg-a-bare-key-pasted-alone-0123456789\n", encoding="utf-8")
    assert run(bank, "--spend")[0] == 2
    out = capsys.readouterr().out
    assert "keys.env:1" in out and "dg-a-bare-key" not in out


def test_an_output_directory_with_something_in_it_is_refused(bank, capsys):
    (bank.root / "out").mkdir()
    (bank.root / "out" / "canary.json").write_text("{}", encoding="utf-8")
    assert run(bank, "--spend")[0] == 2
    assert "a directory of its own" in capsys.readouterr().out


@pytest.mark.parametrize("selector, words", [("2", "holds 1 turns"), ("abcdef01", "0 turns"), ("xyz", "hex digits")])
def test_a_turn_that_is_not_there_is_refused(bank, capsys, selector, words):
    args = argv(bank)
    args[args.index("--turn") + 1] = selector
    assert canary.main(args, run_tests=LiveTests(bank.ledger), now=lambda: NOW) == 2
    assert words in capsys.readouterr().out


def test_a_turn_is_found_by_its_hash(bank, capsys):
    args = argv(bank)
    args[args.index("--turn") + 1] = bank.turn["fed_sha256"][:10]
    assert canary.main(args, run_tests=LiveTests(bank.ledger), now=lambda: NOW) == 2
    assert "turn 1 of" in capsys.readouterr().out


# ------------------------------------------------------------- spending


def test_a_fresh_canary_spends_one_entry_first_and_says_fresh(bank, capsys):
    code, server, tests = run(bank, "--spend")
    out = capsys.readouterr().out
    assert code == 0
    assert out.rstrip().splitlines()[-1] == "fresh"
    assert "ledger entry 3 written and read back" in out
    entry = json.loads(bank.ledger.read_text(encoding="utf-8"))["spent"][-1]
    assert entry["time"] == "2026-10-27 23:00:00 PDT" and entry["kind"] == "canary"
    assert entry["sha256"] == bank.turn["fed_sha256"]
    assert entry["stt_query"] == bank.turn["request"]["query"]
    assert entry["cassette"]["turn"] == 1 and entry["cassette"]["model_version"] == VERSION
    assert entry["live_tests"] == list(canary.LIVE_TESTS) and entry["result_file"].endswith("out/canary.json")
    assert set(entry) >= {"time", "recording", "sha256", "engine", "paced", "result_file", "note"}, "the 0.5.3 ledger's fields"
    (env,) = tests.envs
    assert env["EMET_LIVE_TESTS"] == "1" and env["EMET_LIVE_RECORD"] == str(bank.root / "out" / "live")
    assert server.connections == 2, "the preflight and the turn"
    result = json.loads((bank.root / "out" / "canary.json").read_text(encoding="utf-8"))
    assert result["verdict"] == "fresh" and result["ledger_entry"] == 3
    assert result["live"]["sent_sha256"] == bank.turn["fed_sha256"]
    text = (bank.root / "out" / "canary.json").read_text(encoding="utf-8")
    assert not any(value in text for value in FAKE_KEYS.values())


def test_a_result_holding_a_key_is_never_written_or_shown(bank, capsys):
    """A final that echoes a key's value: the canary still says its verdict,
    and neither canary.json nor the screen carries the value."""
    leaked = FAKE_KEYS["EMET_OPENAI_KEY"]
    code, _, _ = run(bank, "--spend", server=Deepgram(bank.ledger, final=f"my key is {leaked}"))
    out = capsys.readouterr().out
    assert code == 1
    assert "holds the value of EMET_OPENAI_KEY; canary.json was not written" in out
    assert leaked not in out
    assert out.rstrip().splitlines()[-1] == "stale"
    assert not (bank.root / "out" / "canary.json").exists()


def test_a_new_model_version_is_stale(bank, capsys):
    code, _, _ = run(bank, "--spend", server=Deepgram(bank.ledger, version="2026-11-01.1"))
    out = capsys.readouterr().out
    assert code == 1 and "\nstale\n" in out
    assert f"model version: {VERSION} in the cassette, 2026-11-01.1 now" in out


def test_a_final_with_other_words_is_stale(bank, capsys):
    code, _, _ = run(bank, "--spend", server=Deepgram(bank.ledger, final="Where are my car keys?"))
    out = capsys.readouterr().out
    assert code == 1 and "\nstale\n" in out
    assert "  final: 'Where are my keys?' in the cassette, 'Where are my car keys?' now" in out


@pytest.mark.parametrize("then, now", [
    ("Where are my keys?", "Where are my keys"),       # dg08 and dg09's two (ledger L45): the question mark gone
    ("Where are my keys?", "where are my keys."),
    ("What's two plus two?", "Whats two plus two"),
])
def test_a_final_that_differs_in_case_or_punctuation_alone_is_fresh_with_a_note(bank, capsys, then, now):
    """Deepgram gave 2 of dg08 and dg09's 17 finals again without their
    question mark, from the same bytes at the same model version, so the
    canary that called them stale would send the bank to a paid reseed for
    nothing (ledger L45, 2026-11-27)."""
    turn = {**bank.turn, "final": {**bank.turn["final"], "text": then}}
    live = {"error": None, "final": {"text": now, "error": None}, "sent_sha256": turn["fed_sha256"],
            "deepgram_sha256": None, "model": {"version": VERSION}}
    assert canary.compare(turn, live) == {
        "verdict": "fresh", "reasons": [], "notes": [f"final: {then!r} in the cassette, {now!r} now, the same words"]}
    code, _, _ = run(bank, "--spend", server=Deepgram(bank.ledger, final="where are my keys"))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "\nfresh\n  note: final: 'Where are my keys?' in the cassette, 'where are my keys' now, the same words\n" in out


def test_a_replay_that_cannot_reach_deepgram_failed_and_is_still_spent(bank, capsys):
    class Down:
        connections = 0

        async def connect(self, url, **kwargs):
            raise OSError("no route to host")

    code, _, _ = run(bank, "--spend", server=Down())
    out = capsys.readouterr().out
    assert code == 1 and "\nfailed\n" in out and "stale" not in out
    assert json.loads(bank.ledger.read_text(encoding="utf-8"))["spent"][-1]["kind"] == "canary"


def test_deepgram_naming_other_bytes_is_a_failure(bank, capsys):
    code, _, _ = run(bank, "--spend", server=Deepgram(bank.ledger, sha256="f" * 64))
    assert code == 1 and "Deepgram's Metadata names other bytes" in capsys.readouterr().out


def test_a_live_test_that_did_not_pass_fails_the_canary_and_is_named(bank, capsys):
    outcomes = {t: "passed" for t in canary.LIVE_TESTS}
    outcomes[canary.LIVE_TESTS[2]] = "skipped"
    code, _, _ = run(bank, "--spend", tests=LiveTests(bank.ledger, outcomes))
    out = capsys.readouterr().out
    assert code == 1 and "live tests 3 of 4 passed" in out
    assert f"skipped: {canary.LIVE_TESTS[2]}" in out and out.rstrip().endswith("fresh")


# ---------------------------------------------------------------- parts


def test_compare_needs_a_model_version_and_the_turns_bytes():
    turn = {"fed_sha256": "a" * 64, "model_version": VERSION, "final": {"text": FINAL}}
    live = {"error": None, "final": {"text": FINAL, "error": None}, "sent_sha256": "a" * 64, "deepgram_sha256": None, "model": {"version": VERSION}}
    assert canary.compare(turn, live) == {"verdict": "fresh", "reasons": [], "notes": []}
    assert canary.compare(turn, {**live, "model": None})["verdict"] == "failed"
    assert canary.compare(turn, {**live, "sent_sha256": "b" * 64})["verdict"] == "failed"
    assert canary.compare(turn, {**live, "final": {"text": "", "error": "no final within 5 s"}})["verdict"] == "failed"
    both = canary.compare(turn, {**live, "model": {"version": "x"}, "final": {"text": "y", "error": None}})
    assert both["verdict"] == "stale" and len(both["reasons"]) == 2


def test_read_junit_maps_each_live_test_to_its_outcome(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_deepgram_live" name="test_a_live_round_trip_returns_a_final"/>'
        '<testcase classname="tests.test_llm_live" name="test_anthropic_answers"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_llm_live" name="test_openai_answers"><skipped message="no key"/></testcase>'
        '</testsuite></testsuites>',
        encoding="utf-8",
    )
    assert list(canary.read_junit(xml).values()) == ["passed", "failed", "skipped", "not run"]
    assert set(canary.read_junit(tmp_path / "absent.xml").values()) == {"not run"}


def test_git_runs_without_taking_the_index_lock(monkeypatch):
    """`git status` may refresh the index; GIT_OPTIONAL_LOCKS=0 stops it, so
    the canary never holds the lock on the maintainer's tree."""
    seen: list = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="0123abcd\n")

    monkeypatch.setattr(canary.subprocess, "run", fake_run)
    info = canary.engine_info()
    assert [command[3] for command, _ in seen] == ["status", "rev-parse"]
    assert all(command[:3] == ["git", "-C", str(canary.REPO)] for command, _ in seen)
    assert all(kw["env"]["GIT_OPTIONAL_LOCKS"] == "0" and kw["env"]["LC_ALL"] == "C" for _, kw in seen)
    assert info["repo_head"] == "0123abcd" and info["tree_clean"] is False


# ------------------------------------------------- the out folder (L43)


def test_a_relative_out_is_made_absolute_before_the_live_tests_run(bank, monkeypatch, capsys):
    """Ledger L43: from emet/, `--out ../voicebank/canary/2026-11` reached
    the live tests' process, which runs from emet-providers/, as a relative
    path. The XML and the recordings landed inside the repository's tree and
    canary.json read no XML. Here the real `run_live_tests` runs with pytest
    replaced by a stand-in that writes its XML where pytest would, relative
    to its own working folder, as pytest resolves `--junitxml`."""
    monkeypatch.chdir(bank.root)
    seen: list = []

    def fake_pytest(command, *, cwd, env, **kwargs):
        seen.append((command, cwd, env))
        (xml,) = [arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")]
        target = Path(cwd) / xml
        # Under the bug the XML would land in the repository: refuse to
        # write it there, so the test fails without leaving it behind.
        assert not target.resolve().is_relative_to(canary.REPO), f"pytest would write {target} inside the repository"
        target.parent.mkdir(parents=True, exist_ok=True)
        cases = "".join(f'<testcase name="{t.split("::")[-1]}"/>' for t in canary.LIVE_TESTS)
        target.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(canary.subprocess, "run", fake_pytest)
    args = argv(bank, "--spend")
    args[args.index("--out") + 1] = "relative/out"
    server = Deepgram(bank.ledger)
    code = canary.main(args, connect=server.connect, sleep=no_wait, now=lambda: NOW,
                       engine=lambda: {"repo_head": "test", "tree_clean": True, "package_versions": {}},
                       rows=lambda: ROWS, fetch=Vendors())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "live tests 4 of 4 passed" in out
    (command, cwd, env), = seen
    assert Path(cwd) == canary.REPO / "emet-providers"
    absolute = (bank.root / "relative" / "out").resolve()
    assert f"--junitxml={absolute / 'live-tests.xml'}" in command
    assert env["EMET_LIVE_RECORD"] == str(absolute / "live")
    assert (absolute / "live-tests.xml").is_file() and (absolute / "canary.json").is_file()
    result = json.loads((absolute / "canary.json").read_text(encoding="utf-8"))
    assert set(result["live_tests"].values()) == {"passed"}
    assert not (canary.REPO / "emet-providers" / "relative").exists(), "nothing is written inside the repository"


# ----------------------------------------------------- the lifecycle step


def test_the_lifecycle_step_prints_one_line_a_row_and_passes_when_all_are_current(bank, capsys):
    vendors = Vendors()
    code, _, _ = run(bank, "--spend", vendors=vendors)
    out = capsys.readouterr().out
    assert code == 0, out
    lines = [line for line in out.splitlines() if line.startswith("lifecycle  ")]
    assert lines == [
        "lifecycle  gpt-5.6-terra  openai  known, no shutdown date announced (table: none)",
        "lifecycle  claude-opus-5  anthropic  active, no retirement scheduled (table: none)",
        "lifecycle  nova-3  deepgram  listed, and Deepgram states no retirement date (table: none)",
        "lifecycle  aura-2-thalia-en  deepgram  listed, and Deepgram states no retirement date (table: none)",
    ]
    urls = [url for url, _, _ in vendors.asked]
    assert urls == ["https://api.openai.com/v1/models/gpt-5.6-terra",
                    "https://api.anthropic.com/v1/models/claude-opus-5",
                    "https://api.deepgram.com/v1/models"], "Deepgram's list is read once for both rows"
    assert all(timeout == canary.LIFECYCLE_TIMEOUT_S == 20 for _, _, timeout in vendors.asked)
    headers = {url: h for url, h, _ in vendors.asked}
    assert headers[urls[0]] == {"Authorization": f"Bearer {FAKE_KEYS['EMET_OPENAI_KEY']}"}
    assert headers[urls[1]]["x-api-key"] == FAKE_KEYS["EMET_ANTHROPIC_KEY"]
    assert headers[urls[1]]["anthropic-version"]
    assert headers[urls[2]] == {"Authorization": f"Token {FAKE_KEYS['EMET_DEEPGRAM_KEY']}"}
    result = json.loads((bank.root / "out" / "canary.json").read_text(encoding="utf-8"))
    assert [r["verdict"] for r in result["lifecycle"]] == ["ok"] * 4
    text = (bank.root / "out" / "canary.json").read_text(encoding="utf-8")
    assert not any(value in text for value in FAKE_KEYS.values()), "a key is sent and never kept"


def test_a_model_the_vendor_does_not_know_fails_the_step_naming_the_row(bank, capsys):
    vendors = Vendors({"https://api.openai.com/v1/models/gpt-5.6-terra": (404, {"error": {"code": "model_not_found"}})})
    code, _, _ = run(bank, "--spend", vendors=vendors)
    out = capsys.readouterr().out
    assert code == 1
    assert "lifecycle  gpt-5.6-terra  openai  HTTP 404: the vendor does not know the model (table: none)" in out
    assert ("  update the row for 'gpt-5.6-terra' in emet-providers/emet_providers/lifecycle.py: the vendor does "
            "not know the model; choose another default") in out
    assert out.rstrip().splitlines()[-1] == "fresh", "the transcription's verdict is its own"


def test_a_retirement_date_the_table_lacks_fails_the_step(bank, capsys):
    vendors = Vendors({"https://api.anthropic.com/v1/models/claude-opus-5": (
        200, {"type": "model", "id": "claude-opus-5", "lifecycle": "deprecated", "retires_at": "2027-07-24T00:00:00Z"})})
    code, _, _ = run(bank, "--spend", vendors=vendors)
    out = capsys.readouterr().out
    assert code == 1
    assert "lifecycle  claude-opus-5  anthropic  deprecated, retires on 2027-07-24 (table: none)" in out
    assert "the vendor retires it on 2027-07-24, and the table says none. Record 2027-07-24" in out


def test_a_retirement_date_the_table_holds_passes(bank, capsys):
    rows = (Row("anthropic", "claude-opus-5", "2027-07-24"),)
    vendors = Vendors({"https://api.anthropic.com/v1/models/claude-opus-5": (
        200, {"type": "model", "id": "claude-opus-5", "lifecycle": "deprecated", "retires_at": "2027-07-24T00:00:00Z"})})
    code, _, _ = run(bank, "--spend", rows=rows, vendors=vendors)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "lifecycle  claude-opus-5  anthropic  deprecated, retires on 2027-07-24 (table: 2027-07-24)" in out


@pytest.mark.parametrize("vendor, url, body", [
    ("openai", "https://api.openai.com/v1/models/gpt-5.6-terra",
     {"id": "gpt-5.6-terra", "object": "model", "shutdown_date": "2027-01-15"}),
    ("anthropic", "https://api.anthropic.com/v1/models/claude-opus-5",
     {"type": "model", "id": "claude-opus-5", "lifecycle": "retired", "retires_at": None}),
    ("deepgram", "https://api.deepgram.com/v1/models", {"stt": [{"canonical_name": "nova-2"}], "tts": []}),
])
def test_each_vendors_word_for_a_model_going_away_fails_its_row(bank, capsys, vendor, url, body):
    code, _, _ = run(bank, "--spend", vendors=Vendors({url: (200, body)}))
    out = capsys.readouterr().out
    assert code == 1
    failed = [line for line in out.splitlines() if line.startswith("  update the row for ")]
    assert failed and all("lifecycle.py" in line for line in failed)


def test_a_vendor_whose_key_is_absent_is_not_run_and_the_canary_does_not_pass(bank, capsys, monkeypatch):
    monkeypatch.delenv("EMET_OPENAI_KEY")
    vendors = Vendors()
    code, _, _ = run(bank, "--spend", vendors=vendors)
    out = capsys.readouterr().out
    assert code == 1
    assert "lifecycle  gpt-5.6-terra  openai  not run: no key (table: none)" in out
    assert not any("openai" in url for url, _, _ in vendors.asked)


def test_a_vendor_that_cannot_be_reached_fails_its_row(bank, capsys):
    vendors = Vendors({"https://api.deepgram.com/v1/models": OSError("timed out")})
    code, _, _ = run(bank, "--spend", vendors=vendors)
    out = capsys.readouterr().out
    assert code == 1
    assert "lifecycle  nova-3  deepgram  could not be read (OSError: timed out) (table: none)" in out


def test_a_missing_defaults_table_is_not_run_and_fails(bank, capsys):
    def no_table():
        raise ImportError("No module named 'emet_providers.lifecycle'")

    server, tests = Deepgram(bank.ledger), LiveTests(bank.ledger)
    code = canary.main(argv(bank, "--spend"), run_tests=tests, connect=server.connect, sleep=no_wait, now=lambda: NOW,
                       engine=lambda: {"repo_head": "test", "tree_clean": True, "package_versions": {}},
                       rows=no_table, fetch=Vendors())
    out = capsys.readouterr().out
    assert code == 1
    assert "lifecycle  not run: the defaults table could not be read (ImportError: " in out


class _Recorded(http.server.BaseHTTPRequestHandler):
    """Answers each GET as its server's `answer` says and keeps the headers
    it was sent. Loopback only."""

    def do_GET(self):
        self.server.asked.append({k.lower(): v for k, v in self.headers.items()})
        status, headers, body = self.server.answer
        self.send_response(status)
        for name, value in {**headers, "Content-Length": str(len(body))}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _server(answer):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorded)
    server.asked, server.answer = [], answer
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_redirect_is_not_followed_so_the_key_goes_to_no_other_place(monkeypatch):
    """urllib's default opener follows a 3xx and copies the key's header to
    the place it names, plain http included. The canary's GET follows none:
    the 3xx is its answer, the row fails, and the other place is never
    asked (review finding checks-ci/code#1, 2026-11-27)."""
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    elsewhere = _server((200, {"Content-Type": "application/json"}, b'{"shutdown_date": null}'))
    target = f"http://127.0.0.1:{elsewhere.server_address[1]}/somewhere-else"
    vendor = _server((302, {"Location": target}, b""))
    url = f"http://127.0.0.1:{vendor.server_address[1]}/v1/models/gpt-5.6-terra"
    key = "sk-canary-test-key-0123456789"
    try:
        for name in ("openai", "anthropic", "deepgram"):
            status, _ = canary.fetch_url(url, canary.lifecycle_headers(name, key), 5)
            assert status == 302, name
        results = canary.lifecycle_check((ROWS[0],), {"openai": "EMET_OPENAI_KEY"}, {"EMET_OPENAI_KEY": key},
                                         lambda _url, headers, timeout: canary.fetch_url(url, headers, timeout))
    finally:
        for server in (vendor, elsewhere):
            server.shutdown()
            server.server_close()
    assert len(vendor.asked) == 4 and vendor.asked[0]["authorization"] == f"Bearer {key}"
    assert elsewhere.asked == [], "the redirect's target was asked"
    assert results[0]["verdict"] == "failed" and results[0]["says"] == "HTTP 302, and no answer about the model"


def test_the_live_tests_it_names_exist():
    for test in canary.LIVE_TESTS:
        path, name = test.split("::")
        assert f"def {name}(" in (REPO / "emet-providers" / path).read_text(encoding="utf-8")
