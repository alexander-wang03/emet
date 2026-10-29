"""The monthly canary, against fakes: no network, no key, no vendor.

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
import importlib.util
import json
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

    async def connect(self, url, **kwargs):
        if self.ledger is not None:
            spent = json.loads(self.ledger.read_text(encoding="utf-8"))["spent"]
            assert spent[-1]["kind"] == "canary", "a connection opened before the ledger entry"
        self.connections += 1
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


def run(bank, *extra, server=None, tests=None, **kw):
    server = server or Deepgram(bank.ledger)
    tests = tests or LiveTests(bank.ledger)
    code = canary.main(argv(bank, *extra, **kw), run_tests=tests, connect=server.connect, sleep=no_wait,
                       now=lambda: NOW, engine=lambda: {"repo_head": "test", "tree_clean": True, "package_versions": {}})
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
    data["monthly"] = {"replays": 20, "canaries": 2}
    bank.ledger.write_text(json.dumps(data), encoding="utf-8")
    assert run(bank, "--spend")[0] == 0


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


def test_a_changed_final_is_stale(bank, capsys):
    code, _, _ = run(bank, "--spend", server=Deepgram(bank.ledger, final="Where are my keys"))
    out = capsys.readouterr().out
    assert code == 1 and "\nstale\n" in out and "final:" in out


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
    assert canary.compare(turn, live) == {"verdict": "fresh", "reasons": []}
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


def test_the_live_tests_it_names_exist():
    for test in canary.LIVE_TESTS:
        path, name = test.split("::")
        assert f"def {name}(" in (REPO / "emet-providers" / path).read_text(encoding="utf-8")
