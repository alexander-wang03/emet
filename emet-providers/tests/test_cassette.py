"""Cassettes: how they are written, read, checked and played back.

No audio anybody said: the bytes are counted patterns, and the answers are
made up. What is tested is the key (every byte and the request), the rule
for partials, the refusals, and that no credential is ever kept.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import types

import pytest

from emet_providers import cassette
from emet_providers.cassette import (
    FORMAT,
    CassetteError,
    Player,
    RunningHash,
    TurnRecorder,
    check_turn,
    load,
    model_version,
    normalise_request,
    recording_connect,
    safe_url,
    save,
)

REQUEST = {
    "provider": "deepgram",
    "model": "nova-3",
    "sample_rate": 16000,
    "query": {"punctuate": True, "smart_format": "false"},
}


def chunk(n: int, size: int = 2560) -> bytes:
    """Distinct, deterministic bytes: chunk(1) differs from chunk(2)."""
    return bytes((n + i) % 251 for i in range(size))


def recorded(frames, partials: dict[int, str], final: str, request=REQUEST, source="test", **kw) -> dict:
    """A turn as a writer makes one: `partials` maps the 1-based feed after
    which a partial was returned to its text."""
    rec = TurnRecorder(request)
    for n, frame in enumerate(frames, 1):
        rec.feed(frame)
        if n in partials:
            rec.partial(partials[n])
    return rec.turn(final, confidence=0.99, model_version="2025-04-17.21547", recorded="2026-10-27", source=source, **kw)


# ------------------------------------------------------------------ hashing


def test_the_running_hash_is_the_hash_of_every_byte_and_survives_a_digest():
    h = RunningHash()
    h.update(b"abc")
    assert h.hexdigest() == hashlib.sha256(b"abc").hexdigest()
    h.update(b"def")
    assert h.total == 6
    assert h.hexdigest() == hashlib.sha256(b"abcdef").hexdigest()


# ------------------------------------------------------------------ request


def test_a_request_is_compared_as_the_plugin_sends_it():
    a = normalise_request(REQUEST)
    b = normalise_request(
        {"query": {"smart_format": False, "punctuate": "true"}, "sample_rate": "16000", "model": "nova-3", "provider": "deepgram"}
    )
    assert a == b
    assert a["query"] == {"punctuate": "true", "smart_format": "false"}
    assert list(a["query"]) == sorted(a["query"])


@pytest.mark.parametrize("name", ["key", "api_key", "token", "access_token", "Authorization", "client_secret", "password"])
def test_a_query_parameter_named_like_a_credential_is_refused(name):
    with pytest.raises(CassetteError, match="credential"):
        normalise_request({**REQUEST, "query": {name: "x"}})


def test_keyterm_is_refused_too_by_the_rule_the_bank_checker_shares():
    """The substring rule of cut_deepgram.py and tools/bank/manifest.py: a
    body that takes `keyterm` up changes both rules together."""
    with pytest.raises(CassetteError, match="keyterm"):
        normalise_request({**REQUEST, "query": {"keyterm": "Emet"}})


@pytest.mark.parametrize(
    "request_, words",
    [
        ({k: v for k, v in REQUEST.items() if k != "model"}, "no 'model'"),
        ({**REQUEST, "key_env": "EMET_DEEPGRAM_KEY"}, "unknown field 'key_env'"),
        ({**REQUEST, "sample_rate": True}, "sample_rate"),
        ({**REQUEST, "provider": ""}, "provider"),
    ],
)
def test_a_request_has_exactly_its_four_fields(request_, words):
    with pytest.raises(CassetteError, match=words):
        normalise_request(request_)


def test_the_request_is_the_one_the_deepgram_plugin_would_make():
    """Built offline: the plugin is asked for its query and never started."""
    from emet_sdk.types import AudioFormat

    from emet_providers.deepgram import DeepgramTranscriber

    stt = DeepgramTranscriber({"provider": "deepgram", "params": {"query": {"language": "en-GB"}}}, AudioFormat())
    request = cassette.request_for("deepgram", stt.query())
    assert request["model"] == "nova-3" and request["sample_rate"] == 16000
    assert "model" not in request["query"] and "sample_rate" not in request["query"]
    assert request["query"]["smart_format"] == "false" and request["query"]["language"] == "en-GB"
    assert request["query"]["interim_results"] == "true"


def test_a_request_without_a_provider_is_refused():
    with pytest.raises(CassetteError, match="provider"):
        normalise_request({"model": "nova-3"})


# --------------------------------------------------------------------- turn


def test_a_recorded_turn_carries_each_partials_total_and_prefix_hash():
    frames = [chunk(1, 5440), chunk(2), chunk(3)]
    turn = recorded(frames, {2: "Where", 3: "Where are my keys?"}, "Where are my keys?")
    assert turn["fed_bytes"] == 5440 + 2560 + 2560
    assert turn["fed_sha256"] == hashlib.sha256(b"".join(frames)).hexdigest()
    first, second = turn["partials"]
    assert first["after_bytes"] == 8000
    assert first["prefix_sha256"] == hashlib.sha256(b"".join(frames[:2])).hexdigest()
    assert second["prefix_sha256"] == turn["fed_sha256"]
    assert turn["final"] == {"text": "Where are my keys?", "confidence": 0.99, "error": None}


def test_two_partials_after_one_feed_are_refused():
    rec = TurnRecorder(REQUEST)
    rec.feed(chunk(1))
    rec.partial("a")
    with pytest.raises(CassetteError, match="one a feed"):
        rec.partial("b")


def _bad(**change) -> dict:
    turn = recorded([chunk(1), chunk(2)], {1: "a"}, "a b")
    turn.update(change)
    return turn


@pytest.mark.parametrize(
    "change, words",
    [
        ({"fed_sha256": "ABC"}, "sha256"),
        ({"fed_bytes": -1}, "fed_bytes"),
        ({"final": {"confidence": 1.0}}, "final"),
        ({"partials": [{"text": "a", "after_bytes": 9000, "prefix_sha256": "0" * 64}]}, "past the turn"),
        (
            {
                "partials": [
                    {"text": "a", "after_bytes": 2560, "prefix_sha256": "0" * 64},
                    {"text": "b", "after_bytes": 2560, "prefix_sha256": "0" * 64},
                ]
            },
            "they rise",
        ),
        ({"partials": [{"text": "a", "after_bytes": 5120, "prefix_sha256": "0" * 64}]}, "covers every byte"),
        ({"ledger_entry": 0}, "ledger_entry"),
        ({"recorded": None}, "recorded is a date"),
        ({"recorded": "5 Oct"}, "recorded is a date"),
        ({"source": ""}, "source"),
        ({"request": {**REQUEST, "query": {"punctuate": True}}}, "text to text"),
    ],
)
def test_a_malformed_turn_is_refused_with_the_reason(change, words):
    with pytest.raises(CassetteError, match=words):
        check_turn(_bad(**change))


def test_an_unknown_field_anywhere_is_refused_never_ignored():
    """FORMAT.md's fields exactly, as tools/bank/manifest.py checks them."""
    with pytest.raises(CassetteError, match="unknown field 'messages'"):
        check_turn(_bad(messages=[]))
    turn = _bad()
    turn["partials"][0]["confidence"] = 0.9
    with pytest.raises(CassetteError, match="unknown field 'confidence'"):
        check_turn(turn)
    turn = _bad()
    turn["final"]["words"] = []
    with pytest.raises(CassetteError, match="unknown field 'words'"):
        check_turn(turn)


def test_a_file_with_a_field_beside_format_and_turns_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"format": FORMAT, "turns": [], "note": "x"}), encoding="utf-8")
    with pytest.raises(CassetteError, match="unknown field 'note'"):
        load(path)


# -------------------------------------------------------------------- files


def test_a_cassette_round_trips_through_its_file(tmp_path):
    turn = recorded([chunk(1), chunk(2)], {1: "Where"}, "Where are my keys?", source="test", ledger_entry=9)
    path = save(tmp_path / "c.json", [turn])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == ["format", "turns"] and data["format"] == FORMAT
    assert list(data["turns"][0]) == list(cassette.TURN_FIELDS), "FORMAT.md's fields, in its order"
    assert load(path) == [turn]
    assert not (tmp_path / "c.json.tmp").exists()


def test_save_refuses_a_file_that_does_not_read_back(tmp_path, monkeypatch):
    """The disk is asked, never trusted: a file whose turns come back other
    than written is refused."""
    turn = recorded([chunk(1)], {}, "x")
    monkeypatch.setattr(cassette, "load_file", lambda path: {"format": FORMAT, "turns": []})
    with pytest.raises(CassetteError, match="did not read back"):
        save(tmp_path / "c.json", [turn])


def test_save_refuses_a_credential_and_writes_nothing(tmp_path):
    turn = recorded([chunk(1)], {}, "x")
    turn["request"] = {**REQUEST, "query": {"token": "secret-value"}}
    with pytest.raises(CassetteError, match="credential"):
        save(tmp_path / "c.json", [turn])
    assert list(tmp_path.iterdir()) == []


def test_a_directory_is_read_in_file_name_order(tmp_path):
    a = recorded([chunk(1)], {}, "from a")
    b = recorded([chunk(2)], {}, "from b")
    save(tmp_path / "2-b.json", [b])
    save(tmp_path / "1-a.json", [a])
    (tmp_path / "notes.txt").write_text("not a cassette", encoding="utf-8")
    assert [t["final"]["text"] for t in load(tmp_path)] == ["from a", "from b"]


@pytest.mark.parametrize(
    "content, words",
    [
        ("{not json", "not JSON"),
        (json.dumps({"format": "emet-golden/1", "turns": []}), "format"),
        (json.dumps({"format": FORMAT, "turns": {}}), "turns is a list"),
        (json.dumps({"format": FORMAT, "turns": [{"request": REQUEST}]}), "turn 1"),
    ],
)
def test_a_file_that_is_not_a_cassette_is_refused_by_name(tmp_path, content, words):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CassetteError, match=words) as caught:
        load(path)
    assert "bad.json" in str(caught.value)


def test_a_missing_path_is_refused(tmp_path):
    with pytest.raises(CassetteError, match="no such file"):
        load(tmp_path / "absent.json")


# --------------------------------------------------------------------- play


def test_a_player_gives_each_partial_after_its_feed_and_the_final_on_a_hit():
    frames = [chunk(1, 5440), chunk(2), chunk(3)]
    player = Player([recorded(frames, {2: "Where", 3: "Where are my keys?"}, "Where are my keys?")])
    got = [player.feed(f) for f in frames]
    assert got[0] is None
    assert [p["text"] for p in got[1:]] == ["Where", "Where are my keys?"]
    played = player.finish()
    assert played.hit and played.turn["final"]["text"] == "Where are my keys?"
    assert played.fed_bytes == 10560


def test_the_same_bytes_in_other_feed_sizes_still_hit_but_give_only_the_partials_whose_totals_land():
    frames = [chunk(1), chunk(2), chunk(3)]
    player = Player([recorded(frames, {1: "Where", 3: "Where are my keys?"}, "Where are my keys?")])
    whole = b"".join(frames)
    assert player.feed(whole[:4000]) is None, "no feed ends at 2560 bytes, so the first partial is never due"
    assert player.feed(whole[4000:])["text"] == "Where are my keys?"
    assert player.finish().hit


def test_one_changed_byte_is_a_miss_and_its_partials_stop_where_the_bytes_part():
    frames = [chunk(1), chunk(2), chunk(3)]
    player = Player([recorded(frames, {1: "Where", 2: "Where are", 3: "Where are my keys?"}, "Where are my keys?")])
    changed = bytearray(frames[1])
    changed[100] ^= 1
    assert player.feed(frames[0])["text"] == "Where"
    assert player.feed(bytes(changed)) is None
    assert player.feed(frames[2]) is None
    played = player.finish()
    assert not played.hit and played.turn is None


def test_a_player_starts_each_utterance_afresh():
    frames = [chunk(1)]
    player = Player([recorded(frames, {1: "one"}, "one")])
    player.feed(frames[0])
    assert player.finish().hit
    assert player.feed(frames[0])["text"] == "one"
    assert player.finish().hit


def test_only_turns_recorded_for_the_request_are_candidates():
    frames = [chunk(1)]
    formatted = recorded(frames, {}, "What's 22", request={**REQUEST, "query": {"punctuate": "true", "smart_format": "true"}})
    words = recorded(frames, {}, "What's two plus two")
    player = Player([formatted, words], request=REQUEST)
    player.feed(frames[0])
    assert player.finish().turn["final"]["text"] == "What's two plus two"
    every = Player([formatted, words])
    every.feed(frames[0])
    assert every.finish().turn["final"]["text"] == "What's 22", "without a request the first turn wins"


def test_two_recordings_of_the_same_bytes_are_never_mixed():
    """Turn A has a partial late, turn B early, and they disagree. Once B's
    partial is given, B gives the rest, and B's final ends the utterance,
    though A comes first."""
    frames = [chunk(1), chunk(2), chunk(3)]
    a = recorded(frames, {3: "A late"}, "A final")
    b = recorded(frames, {1: "B early", 3: "B late"}, "B final")
    player = Player([a, b])
    assert player.feed(frames[0])["text"] == "B early"
    assert player.feed(frames[1]) is None
    assert player.feed(frames[2])["text"] == "B late"
    assert player.finish().turn["final"]["text"] == "B final"


def test_the_same_bytes_from_another_provider_or_at_another_rate_miss():
    """Every field of the request counts: the same bytes heard at 8 kHz, or
    answered by another provider, are another question."""
    frames = [chunk(1)]
    other_rate = recorded(frames, {}, "at 8 kHz", request={**REQUEST, "sample_rate": 8000})
    other_provider = recorded(frames, {}, "from the mock", request={**REQUEST, "provider": "mock"})
    player = Player([other_rate, other_provider], request=REQUEST)
    assert player.turns == []
    player.feed(frames[0])
    assert not player.finish().hit


def test_a_player_with_no_candidates_misses_everything():
    player = Player([recorded([chunk(1)], {}, "x")], request={**REQUEST, "model": "nova-2"})
    assert player.turns == []
    player.feed(chunk(1))
    assert not player.finish().hit


# -------------------------------------------------------------- live capture


class _Closed(Exception):
    def __init__(self) -> None:
        super().__init__("closed")
        self.rcvd = self.sent = types.SimpleNamespace(code=1000, reason="")


class _Socket:
    def __init__(self, inbox):
        self.inbox = list(inbox)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self.inbox:
            raise _Closed()
        return self.inbox.pop(0)

    async def close(self):
        pass


RESULTS = json.dumps(
    {
        "type": "Results",
        "is_final": True,
        "channel": {"alternatives": [{"transcript": "Hello.", "confidence": 0.9}]},
        "metadata": {"model_info": {"name": "general-nova-3", "version": "2025-04-17.21547", "arch": "nova-3"}},
    }
)
METADATA = json.dumps(
    {
        "type": "Metadata",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "model_info": {"40bd": {"name": "general-nova-3", "version": "2026-01-01.1", "arch": "nova-3"}},
    }
)


def test_a_recorded_connection_keeps_every_message_and_never_the_headers():
    connections: list[dict] = []
    seen: list[dict] = []

    async def connect(url, **kwargs):
        seen.append(kwargs)
        return _Socket([RESULTS, METADATA])

    wrapped = recording_connect(connect, connections)

    async def scenario():
        ws = await wrapped(
            "wss://example.test/v1/listen?model=nova-3&token=abc123",
            additional_headers={"Authorization": "Token dg_live_secret"},
            open_timeout=10,
        )
        await ws.send(chunk(1))
        await ws.send(json.dumps({"type": "CloseStream"}))
        got = [await ws.recv(), await ws.recv()]
        with pytest.raises(_Closed):
            await ws.recv()
        await ws.close()
        return got

    got = asyncio.run(scenario())
    assert got == [RESULTS, METADATA], "messages pass through unchanged"
    assert seen[0]["additional_headers"] == {"Authorization": "Token dg_live_secret"}
    (record,) = connections
    assert "token" not in record["url"] and "model=nova-3" in record["url"]
    assert record["audio_frames_sent"] == 1 and record["audio_bytes_sent"] == 2560
    assert record["audio_sha256"] == hashlib.sha256(chunk(1)).hexdigest()
    assert [m.get("text") for m in record["received"][:2]] == [RESULTS, METADATA]
    assert record["received"][2]["raised"] == "_Closed"
    assert record["text_sent"][0]["text"] == '{"type": "CloseStream"}'
    assert "dg_live_secret" not in json.dumps(connections) and "abc123" not in json.dumps(connections)


def test_a_refused_connection_is_recorded_by_type_and_status_only():
    connections: list[dict] = []

    class Rejected(Exception):
        def __init__(self):
            super().__init__("server rejected WebSocket connection: HTTP 401, header Token dg_live_secret")
            self.response = types.SimpleNamespace(status_code=401)

    async def connect(url, **kwargs):
        raise Rejected()

    async def scenario():
        with pytest.raises(Rejected):
            await recording_connect(connect, connections)("wss://example.test/v1/listen")

    asyncio.run(scenario())
    assert connections[0]["error"] == "Rejected HTTP 401"
    assert "secret" not in json.dumps(connections)


def test_the_model_version_is_read_from_results_and_metadata():
    assert model_version([{"text": RESULTS}]) == {"name": "general-nova-3", "version": "2025-04-17.21547", "arch": "nova-3"}
    assert model_version([{"text": RESULTS}, {"text": METADATA}])["version"] == "2026-01-01.1", "the last report wins"
    assert model_version([{"text": "not json"}, {"raised": "_Closed"}, {"bytes_b64": ""}]) is None


def test_safe_url_keeps_the_query_and_drops_credentials():
    assert safe_url("wss://h/p?a=1&api_key=s&model=nova-3") == "wss://h/p?a=1&model=nova-3"


def test_the_module_exports_what_a_writer_needs():
    for name in ("load", "save", "TurnRecorder", "RunningHash", "Player", "MISS", "recording_connect", "model_version"):
        assert hasattr(cassette, name)
    assert cassette.MISS == "cassette miss"
