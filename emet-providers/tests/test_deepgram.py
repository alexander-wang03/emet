"""The Deepgram transcriber, against a stand-in for Deepgram.

No network and no key. `FakeServer` answers `connect()` the way the library
would, records every frame it is sent, and replies with scripted messages in
the shapes Deepgram's documentation gives (verified 2026-09-12). What these
tests prove is the plugin's half of the protocol: the URL and header it
sends, the order of frames, how it assembles interims and finals into one
utterance, and what it says when a key is missing, rejected, or the network
is not there. The live test beside this file proves the other half, when a
key is present.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from emet_sdk.plugin import PluginError, TranscriberPlugin
from emet_sdk.types import AudioFormat, Transcript

from emet_providers.deepgram import (
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_URL,
    KEYTERM_TOKENS,
    DeepgramTranscriber,
    keyterm_count,
)

FRAME = bytes(2560)
KEY = "dg_test_key"


def run(coro):
    return asyncio.run(coro)


async def settle() -> None:
    """Let the pump, the sender and the receiver each take their turns.

    The fake answers a frame the instant it is sent, so without this a
    test's next `feed()` could observe two frames' worth of results at once
    and the sequence of partials would depend on scheduling order.
    """
    for _ in range(8):
        await asyncio.sleep(0)


# -------------------------------------------------------------- the fake


class _Closed(Exception):
    """Shaped like websockets' ConnectionClosed, it carries rcvd and sent."""

    def __init__(self, code: int = 1000) -> None:
        super().__init__(f"closed {code}")
        self.rcvd = types.SimpleNamespace(code=code, reason="")
        self.sent = types.SimpleNamespace(code=code, reason="")


class _Rejected(Exception):
    """Shaped like websockets' InvalidStatus, it carries response.status_code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        self.response = types.SimpleNamespace(status_code=status_code)


def results(transcript: str, *, final: bool, confidence: float = 0.9) -> dict:
    """One Results message, in the documented shape."""
    return {
        "type": "Results",
        "channel_index": [0, 1],
        "duration": 1.0,
        "start": 0.0,
        "is_final": final,
        "speech_final": False,
        "channel": {
            "alternatives": [
                {"transcript": transcript, "confidence": confidence, "words": []}
            ]
        },
    }


METADATA = {"type": "Metadata", "request_id": "r", "duration": 1.0, "channels": 1}


class FakeConnection:
    def __init__(self, script: dict, *, close_after_metadata: bool = True) -> None:
        #: `script` maps the count of binary frames received so far to the
        #: messages the server sends at that moment, and "close" to what it
        #: sends after CloseStream. Metadata and the close follow unless told
        #: otherwise.
        self.script = script
        self.close_after_metadata = close_after_metadata
        self.sent: list[Any] = []
        self.frames = 0
        self.closed = False
        self._inbox: asyncio.Queue = asyncio.Queue()

    def _say(self, message: Any) -> None:
        self._inbox.put_nowait(message if isinstance(message, str) else json.dumps(message))

    async def send(self, data: Any) -> None:
        self.sent.append(data)
        if isinstance(data, bytes):
            self.frames += 1
            for message in self.script.get(self.frames, []):
                self._say(message)
            return
        if json.loads(data).get("type") == "CloseStream":
            for message in self.script.get("close", []):
                self._say(message)
            if self.close_after_metadata:
                self._say(METADATA)
                self._inbox.put_nowait(_Closed())

    async def recv(self) -> str:
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class FakeServer:
    def __init__(self, script: dict | None = None, *, reject: BaseException | None = None, **conn_kw) -> None:
        self.script = script or {}
        self.reject = reject
        self.conn_kw = conn_kw
        self.urls: list[str] = []
        self.kwargs: list[dict] = []
        self.connections: list[FakeConnection] = []

    async def connect(self, url: str, **kwargs) -> FakeConnection:
        self.urls.append(url)
        self.kwargs.append(kwargs)
        if self.reject is not None:
            raise self.reject
        conn = FakeConnection(self.script, **self.conn_kw)
        self.connections.append(conn)
        return conn


def make(server: FakeServer, *, config: dict | None = None, **params) -> DeepgramTranscriber:
    cfg = {"provider": "deepgram", "params": params}
    cfg.update(config or {})
    return DeepgramTranscriber(cfg, AudioFormat(), connect=server.connect)


def started(server: FakeServer, monkeypatch, **kw) -> DeepgramTranscriber:
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    stt = make(server, **kw)
    run(stt.start())
    assert stt.describe().healthy, stt.health().detail
    return stt


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# --------------------------------------------------------------- request


def test_it_is_a_transcriber_plugin_registered_as_deepgram():
    stt = make(FakeServer())
    assert isinstance(stt, TranscriberPlugin)
    assert stt.provider == "deepgram"


def test_the_request_says_linear16_at_the_formats_rate_with_interims_on():
    stt = make(FakeServer())
    url = stt.request_url()
    assert url.startswith(DEFAULT_URL + "?")
    q = query_of(url)
    assert q["encoding"] == "linear16"
    assert q["sample_rate"] == "16000"
    assert q["channels"] == "1"
    assert q["interim_results"] == "true"
    assert q["punctuate"] == "true"
    assert q["model"] == DEFAULT_MODEL


def test_the_souls_model_is_sent_and_reported():
    stt = make(FakeServer(), config={"model": "nova-2"})
    assert query_of(stt.request_url())["model"] == "nova-2"
    assert stt.describe().model == "nova-2"


def test_body_query_params_ride_along_but_cannot_change_the_format():
    stt = make(
        FakeServer(),
        query={"language": "en-GB", "redact_usage": True, "sample_rate": 8000, "encoding": "mulaw", "channels": 2,
               "vad_events": True},
    )
    q = query_of(stt.request_url())
    assert q["language"] == "en-GB"
    assert q["redact_usage"] == "true"
    assert q["vad_events"] == "true"
    assert q["sample_rate"] == "16000"
    assert q["encoding"] == "linear16"
    assert q["channels"] == "1"


def test_the_request_carries_smart_format_false():
    """With `smart_format` on, Deepgram may hold a final for up to 3 s of
    silence to format an entity that looks unfinished, and the engine closes
    the stream about a second after the last word: on the 3 of 28 replayed
    turns where "what's two plus two" read "What's 22", the only final was
    the flush after the close. Off, the one of the three replayed on the
    same bytes got Deepgram's own final before the close (laptop replays,
    2026-10-01 to 03). Sent as "false", so a change of Deepgram's default
    cannot bring the hold back."""
    q = query_of(make(FakeServer()).request_url())
    assert q["smart_format"] == "false"


def test_a_body_can_set_a_key_the_plugin_sends():
    """`params.query` is laid over the plugin's own `interim_results`,
    `punctuate` and `smart_format`, so a body that wants numbers formatted,
    and accepts the held finals, says so in `models.stt.params.query`. It
    cannot set the format keys, which the audio format fixes, or `model`,
    which comes from `models.stt.model`."""
    stt = make(FakeServer(), query={"smart_format": True, "model": "nova-2"})
    q = query_of(stt.request_url())
    assert q["smart_format"] == "true"
    assert q["model"] == DEFAULT_MODEL


def test_the_url_can_point_at_a_self_hosted_deployment():
    stt = make(FakeServer(), url="wss://deepgram.internal/v1/listen")
    assert stt.request_url().startswith("wss://deepgram.internal/v1/listen?")


def test_the_key_travels_as_a_token_header(monkeypatch):
    server = FakeServer()
    started(server, monkeypatch)
    assert server.kwargs[0]["additional_headers"] == {"Authorization": f"Token {KEY}"}
    assert server.kwargs[0]["open_timeout"] == 10.0


# ------------------------------------------------- the opt-out and keyterms


def test_every_connection_carries_mip_opt_out_the_preflight_included(monkeypatch):
    """The maintainer, 2026-11-24: mip_opt_out=true with every Deepgram
    request. The preflight's connection and each utterance's."""
    server = FakeServer({"close": [results("one", final=True)]})
    stt = started(server, monkeypatch)

    async def scenario():
        await stt.feed(FRAME)
        await stt.finish()

    run(scenario())
    assert len(server.urls) == 2, "the preflight and one utterance"
    for url in server.urls:
        assert parse_qs(urlparse(url).query)["mip_opt_out"] == ["true"]
        assert url.count("mip_opt_out=") == 1


@pytest.mark.parametrize(
    "params",
    [
        {"query": {"mip_opt_out": True}},
        {"query": {"mip_opt_out": "TRUE"}},
        {"mip_opt_out": True},
        {"mip_opt_out": "true", "query": {"mip_opt_out": "true"}},
    ],
)
def test_a_body_may_say_the_opt_out_is_true_which_changes_nothing(params):
    stt = make(FakeServer(), **params)
    assert stt.refusal is None
    assert stt.request_url() == make(FakeServer()).request_url()


@pytest.mark.parametrize(
    "params, where",
    [
        ({"query": {"mip_opt_out": False}}, "params.query.mip_opt_out is False"),
        ({"query": {"mip_opt_out": "false"}}, "params.query.mip_opt_out is 'false'"),
        ({"mip_opt_out": False}, "params.mip_opt_out is False"),
        ({"mip_opt_out": 0}, "params.mip_opt_out is 0"),
        ({"mip_opt_out": None}, "params.mip_opt_out is None"),
    ],
)
def test_a_body_cannot_turn_the_opt_out_off(params, where):
    stt = make(FakeServer(), **params)
    assert stt.refusal is not None and stt.refusal.startswith(where)
    assert "a body cannot turn it off" in stt.refusal
    assert "keeps it out of Deepgram's Model Improvement Program" in stt.refusal, "it says what the opt-out does"
    assert "maintainer" not in stt.refusal, "an owner is told what the line does, not who decided it"
    with pytest.raises(PluginError, match="cannot turn it off"):
        stt.request_url()


@pytest.mark.parametrize(
    "url",
    [
        "wss://api.deepgram.com/v1/listen?mip_opt_out=false&",
        "wss://api.deepgram.com/v1/listen?keyterm=Emet,Emmett&",
        "wss://api.deepgram.com/v1/listen?",
        "wss://deepgram.internal/v1/listen#x",
    ],
)
def test_a_url_holding_a_query_is_refused_so_no_parameter_passes_the_rules(monkeypatch, url):
    """A query in `params.url` would go out ahead of the plugin's own, so a
    body could send mip_opt_out=false first, or one comma-joined keyterm,
    past every rule above. Refused at construction, naming the URL, and
    nothing reads the key or connects for it."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    server = FakeServer()
    stt = make(server, url=url)
    assert stt.refusal is not None and stt.refusal.startswith(f"params.url is {url!r}, which holds a query")
    assert "params.query or params.keyterms" in stt.refusal
    with pytest.raises(PluginError, match="holds a query"):
        stt.request_url()
    run(stt.start())
    assert not stt.describe().healthy and server.urls == [] and stt._key is None


def test_a_refused_body_reads_no_key_connects_nowhere_and_says_why(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    server = FakeServer()
    stt = make(server, mip_opt_out=False, keyterms=["Emet"])
    run(stt.start())
    assert not stt.describe().healthy
    health = stt.health()
    assert health.detail == stt.refusal and health.faults == ("start_failed",)
    assert server.urls == [], "no connection is attempted for a request that cannot be sent"
    assert stt._key is None, "the key is not read for it either"


def test_keyterms_are_sent_one_parameter_an_entry_in_the_bodys_order(monkeypatch):
    server = FakeServer()
    stt = started(server, monkeypatch, keyterms=["Emet", "Hey Emet", "Afflatus"])
    assert stt.keyterms == ("Emet", "Hey Emet", "Afflatus")
    for url in (stt.request_url(), server.urls[0]):
        assert parse_qs(urlparse(url).query)["keyterm"] == ["Emet", "Hey Emet", "Afflatus"]
        assert url.endswith("&keyterm=Emet&keyterm=Hey+Emet&keyterm=Afflatus"), "a space inside a term goes out as +"
    assert stt.query()["keyterm"] == ["Emet", "Hey Emet", "Afflatus"]


def test_the_query_string_for_a_body_with_two_keyterms():
    """The whole URL, so the report can quote it: no key is in it, since the
    key travels in a header."""
    stt = make(FakeServer(), keyterms=["Emet", "Hey Emet"])
    assert stt.request_url() == (
        "wss://api.deepgram.com/v1/listen?encoding=linear16&channels=1&interim_results=true"
        "&punctuate=true&smart_format=false&mip_opt_out=true&sample_rate=16000&model=nova-3"
        "&keyterm=Emet&keyterm=Hey+Emet"
    )


@pytest.mark.parametrize("params", [{}, {"keyterms": []}, {"keyterms": None}])
def test_a_body_with_no_keyterms_sends_none(params):
    stt = make(FakeServer(), **params)
    assert stt.refusal is None and stt.keyterms == ()
    assert "keyterm" not in stt.query() and "keyterm" not in stt.request_url()


def test_a_nova_3_variant_takes_keyterms():
    stt = make(FakeServer(), config={"model": "nova-3-medical"}, keyterms=["Emet"])
    assert stt.refusal is None
    assert query_of(stt.request_url())["keyterm"] == "Emet"


@pytest.mark.parametrize(
    "params, config, words",
    [
        ({"keyterms": "Emet"}, {}, ["params.keyterms is a list of terms", "str 'Emet'"]),
        ({"keyterms": {"Emet": 1}}, {}, ["params.keyterms is a list of terms", "dict"]),
        ({"keyterms": ["Emet", 42]}, {}, ["params.keyterms entry 2, 42, is int", "a keyterm is text"]),
        ({"keyterms": ["Emet", ""]}, {}, ["params.keyterms entry 2, '', is empty"]),
        ({"keyterms": ["   "]}, {}, ["params.keyterms entry 1, '   ', is empty"]),
        ({"keyterms": ["Emet "]}, {}, ["params.keyterms entry 1, 'Emet ', begins or ends with a space"]),
        (
            {"keyterms": ["Emet", "Emmett,Emet"]},
            {},
            ["params.keyterms entry 2, 'Emmett,Emet', holds a comma", "silently boosts nothing instead of failing",
             "developers.deepgram.com/docs/keyterm", "Give each term an entry of its own"],
        ),
        ({"keyterms": ["Emet;Emmett"]}, {}, ["entry 1, 'Emet;Emmett', holds a semicolon", "Do not separate keyterms"]),
        ({"keyterms": ["Emet\nEmmett"]}, {}, ["entry 1", "holds a line break or another control character"]),
        ({"keyterms": ["Emet\tEmmett"]}, {}, ["entry 1", "holds a line break or another control character"]),
        ({"keyterms": ["Emet\u0085Emmett"]}, {}, ["entry 1", "holds a line break or another control character"]),
        ({"keyterms": ["Emet\u2028Emmett"]}, {}, ["entry 1", "holds a line break or another control character"]),
        ({"keyterms": ["Emet\u2029Emmett"]}, {}, ["entry 1", "holds a line break or another control character"]),
        ({"keyterms": ["Emet\u009fEmmett"]}, {}, ["entry 1", "holds a line break or another control character"]),
        ({"keyterms": ["Emet:0.15"]}, {}, ["entry 1, 'Emet:0.15', ends in a weight", "boosts nothing",
                                           "Write the term alone, with no colon or number after it."]),
        ({"keyterms": ["Emet:2"]}, {}, ["entry 1, 'Emet:2', ends in a weight"]),
        ({"keyterms": ["Emet :2"]}, {}, ["entry 1, 'Emet :2', ends in a weight"]),
        ({"keyterms": ["Emet: 2"]}, {}, ["entry 1, 'Emet: 2', ends in a weight"]),
        ({"keyterms": ["Emet:-2"]}, {}, ["entry 1, 'Emet:-2', ends in a weight"]),
        ({"keyterms": ["Emet:.5"]}, {}, ["entry 1, 'Emet:.5', ends in a weight"]),
        ({"keyterms": ["Emet"] * 101}, {}, ["params.keyterms comes to 505 by Emet's count",
                                            "past Deepgram's limit of 500 tokens a request"]),
        ({"keyterms": ["Emet"]}, {"model": "nova-2"}, ["params.keyterms is for a Nova-3 model", "'nova-2'", "Keywords",
                                                       "Set models.stt.model to nova-3, or take params.keyterms out"]),
        ({"keyterm": "Emet"}, {}, ["params.keyterm is not read", "a list under params.keyterms"]),
        ({"query": {"keyterm": "Emet"}}, {}, ["params.query.keyterm is not read", "params.keyterms", "silently boosts nothing"]),
        ({"query": {"keyterm": "Emet,Emmett"}}, {}, ["params.query.keyterm is not read"]),
        ({"query": {"keyterms": ["Emet"]}}, {}, ["params.query.keyterms is not read"]),
        ({"query": ["language=en"]}, {}, ["params.query is a mapping of query parameters, not list"]),
    ],
)
def test_a_keyterm_list_that_cannot_be_sent_is_refused_naming_the_entry(params, config, words):
    stt = make(FakeServer(), config=config, **params)
    assert stt.refusal is not None
    for word in words:
        assert word in stt.refusal, (word, stt.refusal)
    assert stt.keyterms == ()
    with pytest.raises(PluginError):
        stt.query()


def test_the_model_refusal_names_no_model_this_plugin_cannot_reach():
    """Flux takes keyterms on Deepgram's page and speaks another protocol, so
    the refusal does not offer it to an owner."""
    for model in ("nova-2", "flux-general-en"):
        refusal = make(FakeServer(), config={"model": model}, keyterms=["Emet"]).refusal or ""
        assert refusal.startswith("params.keyterms is for a Nova-3 model") and "Flux" not in refusal


@pytest.mark.parametrize("term", ["Room 4:30", "Psalm 23:1", "Gate 4: 30", "v1.2:3", "Room 4 :30"])
def test_a_colon_and_digits_after_a_digit_are_part_of_the_term(term):
    """A time or a reference is no weight: Deepgram takes the term as
    written, and so it is sent as written."""
    stt = make(FakeServer(), keyterms=[term])
    assert stt.refusal is None, stt.refusal
    assert parse_qs(urlparse(stt.request_url()).query)["keyterm"] == [term]


def test_the_count_stops_at_deepgrams_500():
    """100 four-letter terms come to 500 by Emet's count and are sent; one
    more is refused. The count is UTF-8 bytes plus one a term, so a term
    outside ASCII counts each of its bytes."""
    assert make(FakeServer(), keyterms=["Emet"] * 100).refusal is None
    assert keyterm_count(["Émet"]) == 6
    assert keyterm_count(["Emet"] * 100) == KEYTERM_TOKENS


def test_the_bodys_order_is_kept_and_so_are_repeats():
    """Deepgram's page says nothing of order or of a term given twice; the
    plugin sends what the body wrote, and the cassette key keeps it."""
    stt = make(FakeServer(), keyterms=["Emmett", "Emet", "Emmett"])
    assert parse_qs(urlparse(stt.request_url()).query)["keyterm"] == ["Emmett", "Emet", "Emmett"]


def test_the_module_quotes_deepgrams_words_with_the_date():
    from emet_providers import deepgram

    doc = deepgram.__doc__ or ""
    assert "read 2026-11-26" in doc and "silently boosts nothing" in doc
    assert "mip_opt_out=true" in doc and "500 tokens" in doc


# ---------------------------------------------------------------- start


def test_preflight_opens_one_connection_sends_close_stream_and_closes(monkeypatch):
    server = FakeServer()
    started(server, monkeypatch)
    (conn,) = server.connections
    assert conn.sent == [json.dumps({"type": "CloseStream"})]
    assert conn.closed


def test_preflight_can_be_turned_off(monkeypatch):
    server = FakeServer()
    started(server, monkeypatch, preflight=False)
    assert server.connections == []


def test_no_key_is_unhealthy_and_names_the_variable(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    server = FakeServer()
    stt = make(server)
    run(stt.start())
    assert not stt.describe().healthy
    assert DEFAULT_KEY_ENV in (stt.health().detail or "")
    assert "no_key" in stt.health().faults
    assert server.connections == [], "no connection may be attempted without a key"


def test_the_souls_key_env_is_honoured(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    monkeypatch.setenv("MY_DG", KEY)
    server = FakeServer()
    stt = make(server, config={"key_env": "MY_DG"})
    run(stt.start())
    assert stt.describe().healthy
    assert server.kwargs[0]["additional_headers"]["Authorization"] == f"Token {KEY}"


def test_a_rejected_key_is_unhealthy_with_the_status(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, "wrong")
    stt = make(FakeServer(reject=_Rejected(401)))
    run(stt.start())
    detail = stt.health().detail or ""
    assert not stt.describe().healthy
    assert "401" in detail and DEFAULT_KEY_ENV in detail and "rejected" in detail


def test_an_unreachable_network_is_unhealthy_and_says_so(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    stt = make(FakeServer(reject=OSError("Name or service not known")))
    run(stt.start())
    detail = stt.health().detail or ""
    assert not stt.describe().healthy
    assert "could not reach" in detail and "network" in detail


def test_without_the_websockets_library_the_hint_names_the_extra(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    monkeypatch.setitem(sys.modules, "websockets", None)
    monkeypatch.setitem(sys.modules, "websockets.asyncio", None)
    monkeypatch.setitem(sys.modules, "websockets.asyncio.client", None)
    stt = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat())
    run(stt.start())
    assert not stt.describe().healthy
    assert "emet-providers[deepgram]" in (stt.health().detail or "")


# ------------------------------------------------------------- streaming


def test_frames_stream_in_order_and_close_stream_is_a_text_frame(monkeypatch):
    server = FakeServer()
    stt = started(server, monkeypatch)

    async def scenario():
        for i in range(3):
            await stt.feed(bytes([i]) * 2560)
        return await stt.finish()

    final = run(scenario())
    conn = server.connections[-1]
    assert conn.sent[:3] == [bytes([0]) * 2560, bytes([1]) * 2560, bytes([2]) * 2560]
    assert conn.sent[3] == json.dumps({"type": "CloseStream"})
    assert isinstance(conn.sent[3], str)
    assert conn.closed
    assert final == Transcript(text="", final=True, confidence=1.0)


def test_interims_become_partials_and_finals_join_into_the_utterance(monkeypatch):
    server = FakeServer(
        {
            1: [results("what", final=False, confidence=0.4)],
            2: [results("what time", final=False, confidence=0.6)],
            3: [results("What time", final=True, confidence=0.95)],
            4: [results("is it", final=False, confidence=0.5)],
            "close": [results("is it in Tokyo?", final=True, confidence=0.97)],
        }
    )
    stt = started(server, monkeypatch)

    async def scenario():
        partials = []
        # Six feeds for four scripted messages: each feed observes what the
        # previous frame's message changed, and the last two observe nothing.
        for _ in range(6):
            if (t := await stt.feed(FRAME)) is not None:
                partials.append(t)
            await settle()
        return partials, await stt.finish()

    partials, final = run(scenario())
    texts = [p.text for p in partials]
    assert texts == ["what", "what time", "What time", "What time is it"]
    assert all(not p.final for p in partials)
    assert partials[0].confidence == pytest.approx(0.4)
    assert final.final
    assert final.text == "What time is it in Tokyo?"
    assert final.confidence == pytest.approx((0.95 + 0.97) / 2)


def test_empty_interims_for_silence_add_nothing(monkeypatch):
    server = FakeServer({1: [results("", final=False)], 2: [results("", final=True)]})
    stt = started(server, monkeypatch)

    async def scenario():
        out = []
        for _ in range(3):
            out.append(await stt.feed(FRAME))
            await asyncio.sleep(0)
        return out, await stt.finish()

    partials, final = run(scenario())
    assert partials == [None, None, None]
    assert final.text == ""


def test_finish_with_nothing_fed_opens_nothing(monkeypatch):
    server = FakeServer()
    stt = started(server, monkeypatch)
    final = run(stt.finish())
    assert final == Transcript(text="", final=True, confidence=1.0)
    assert len(server.connections) == 1, "only the preflight connection"


def test_each_utterance_gets_its_own_connection(monkeypatch):
    server = FakeServer({"close": [results("one", final=True)]})
    stt = started(server, monkeypatch)

    async def scenario():
        await stt.feed(FRAME)
        first = await stt.finish()
        await stt.feed(FRAME)
        second = await stt.finish()
        return first.text, second.text

    assert run(scenario()) == ("one", "one")
    assert len(server.connections) == 3, "preflight plus one per utterance"


def test_a_server_that_never_answers_gives_back_what_was_heard(monkeypatch):
    server = FakeServer({1: [results("half a", final=False)]}, close_after_metadata=False)
    stt = started(server, monkeypatch, timeout_s=0.05)

    async def scenario():
        await stt.feed(FRAME)
        await asyncio.sleep(0)
        return await stt.finish()

    final = run(scenario())
    assert final.final and final.text == "half a"
    assert stt.last_error is not None and "CloseStream" in stt.last_error


def test_a_connection_that_drops_mid_utterance_is_not_a_broken_plugin(monkeypatch):
    class Dropping(FakeConnection):
        async def send(self, data):
            if isinstance(data, bytes) and self.frames >= 1:
                raise OSError("connection reset")
            await super().send(data)

    class DroppingServer(FakeServer):
        async def connect(self, url, **kwargs):
            self.urls.append(url)
            conn = Dropping(self.script)
            self.connections.append(conn)
            return conn

    server = DroppingServer({1: [results("hello", final=True)]})
    stt = started(server, monkeypatch)

    async def scenario():
        await stt.feed(FRAME)
        await settle()
        await stt.feed(FRAME)
        await settle()
        return await stt.finish()

    final = run(scenario())
    assert final.text == "hello"
    assert stt.last_error is not None
    assert stt.describe().healthy, "a transient failure is logged, not a fault"


def test_shutdown_mid_utterance_cancels_and_forgets(monkeypatch):
    server = FakeServer(close_after_metadata=False)
    stt = started(server, monkeypatch)

    async def scenario():
        await stt.feed(FRAME)
        await asyncio.sleep(0)
        await stt.shutdown()
        return stt.describe().healthy, await stt.finish()

    healthy, final = run(scenario())
    assert not healthy
    assert final.text == ""


def test_unknown_message_types_and_non_json_frames_are_ignored(monkeypatch):
    server = FakeServer(
        {
            1: [
                {"type": "SpeechStarted", "timestamp": 0.1},
                "this is not json",
                {"type": "UtteranceEnd", "last_word_end": 1.2},
                results("fine", final=True),
            ]
        }
    )
    stt = started(server, monkeypatch)

    async def scenario():
        await stt.feed(FRAME)
        await asyncio.sleep(0)
        return await stt.finish()

    assert run(scenario()).text == "fine"


def test_describe_before_start_is_unhealthy_and_after_is_streaming(monkeypatch):
    server = FakeServer()
    stt = make(server)
    assert not stt.describe().healthy
    started(server, monkeypatch)
    d = make(server).describe()
    assert d.streaming and d.sample_rate == 16000 and d.provider == "deepgram"


def test_a_connection_that_never_opens_reports_why_on_the_final(monkeypatch):
    """The reference body with its hotspot switched off between boot and the
    first question: the socket cannot resolve the host, no words arrive, and
    the final says so rather than looking like a cough. Without this the
    robot stands there in silence and the person cannot tell why."""

    class UnreachableAfterBoot(FakeServer):
        async def connect(self, url, **kwargs):
            if not self.connections:
                return await super().connect(url, **kwargs)  # the preflight, at boot
            raise OSError("[Errno -3] Temporary failure in name resolution")

    stt = started(UnreachableAfterBoot(), monkeypatch)

    async def scenario():
        await stt.feed(FRAME)
        await settle()
        return await stt.finish()

    final = run(scenario())
    assert final.final and final.text == ""
    assert final.error is not None and "name resolution" in final.error
    assert stt.describe().healthy, "a dead network is not a broken plugin"


def test_a_turn_that_went_fine_carries_no_error_after_one_that_did_not(monkeypatch):
    """`last_error` outlives an utterance; the transcript must not. A turn
    blamed for the previous turn's failure would have the robot apologising
    for words it heard perfectly well."""
    server = FakeServer({1: [results("hello", final=True)]})
    stt = started(server, monkeypatch)
    stt.last_error = "something from an earlier turn"

    async def scenario():
        await stt.feed(FRAME)
        await settle()
        return await stt.finish()

    final = run(scenario())
    assert final.text == "hello"
    assert final.error is None


def test_finish_does_not_wait_for_a_socket_that_will_not_unwind(monkeypatch):
    """`wait_for` cancels the task and then waits for that cancellation to
    finish. With mobile data off on the reference body it added ten seconds
    to the five, and the person stood in silence for fifteen before the
    robot said its failure line."""
    stt = started(FakeServer(), monkeypatch, timeout_s=0.05)

    async def will_not_unwind():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(30)  # a dead socket, taking its time

    async def scenario():
        await stt.feed(FRAME)
        await settle()
        stuck = asyncio.create_task(will_not_unwind())
        stt._utterance.task.cancel()
        stt._utterance.task = stuck
        begun = time.perf_counter()
        final = await stt.finish()
        waited = time.perf_counter() - begun
        stuck.cancel()
        return final, waited

    final, waited = run(scenario())
    assert waited < 1.0, f"the person waited {waited:.1f} s"
    assert final.final and final.error is not None
    assert "no final within" in final.error


# ------------------------------------- 0.5.7: where the audio goes, and refusals of a known kind


import logging  # noqa: E402

from emet_sdk.types import FAULT_KINDS  # noqa: E402

from emet_providers.deepgram import FAULT_CODES  # noqa: E402

#: Deepgram's documented bodies (developers.deepgram.com/docs/errors and
#: /reference/errors, read 2026-11-27), with a request id beside them.
NO_CREDIT = {
    "err_code": "ASR_PAYMENT_REQUIRED",
    "err_msg": "Project does not have enough credits for an ASR request and does not have an overage agreement.",
    "request_id": "r-402",
}
NO_ACCESS = {
    "err_code": "INSUFFICIENT_PERMISSIONS",
    "err_msg": "Project does not have access to the requested model.",
    "request_id": "r-403",
}
NO_SUCH_MODEL = {"err_code": "Bad Request", "err_msg": "Bad Request: No such model/language/tier combination found."}

OUT_OF_CREDIT = (
    "Deepgram says the account is out of credit (HTTP 402, ASR_PAYMENT_REQUIRED). "
    "Add credit to the project in Deepgram's console. Retrying will not help."
)


class _RefusedWith(Exception):
    """Shaped like websockets' InvalidStatus with its response: a status, the
    headers and the body, as `websockets.http11.Response` carries them
    (websockets 17.1, read 2026-11-27)."""

    def __init__(self, status_code: int, body: dict | bytes | None = None) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else b"")
        self.response = types.SimpleNamespace(status_code=status_code, headers={}, body=raw)


class Scripted(FakeServer):
    """Each connect takes the next of `answers`: None opens a connection, an
    exception is raised. The preflight is the first."""

    def __init__(self, answers: list, script: dict | None = None) -> None:
        super().__init__(script)
        self.answers = list(answers)

    async def connect(self, url, **kwargs):
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            self.urls.append(url)
            raise answer
        return await super().connect(url, **kwargs)


def refused_at_boot(monkeypatch, exc: BaseException, **kw) -> DeepgramTranscriber:
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    stt = make(FakeServer(reject=exc), **kw)
    run(stt.start())
    assert not stt.describe().healthy
    return stt


def one_turn(stt: DeepgramTranscriber) -> Transcript:
    async def scenario():
        await stt.feed(FRAME)
        await settle()
        return await stt.finish()

    return run(scenario())


def test_the_descriptor_names_where_the_audio_goes_and_the_key_that_pays():
    """SF2: the boot line prints `, at api.deepgram.com, key EMET_DEEPGRAM_KEY`
    and `--stats` counts the audio sent to that host, from these two fields.
    The endpoint is the URL as configured, with no query."""
    d = make(FakeServer()).describe()
    assert d.endpoint == DEFAULT_URL and d.key_env == DEFAULT_KEY_ENV
    assert d.host == "api.deepgram.com"
    assert make(FakeServer(), config={"key_env": "MY_DG"}).describe().key_env == "MY_DG"


def test_a_password_in_the_url_never_reaches_the_descriptor():
    d = make(FakeServer(), url="wss://owner:hunter2@example.com:8443/v1/listen").describe()
    assert d.endpoint == "wss://example.com:8443/v1/listen"
    assert d.host == "example.com:8443"
    assert "hunter2" not in repr(d)


def test_every_kind_in_the_table_is_one_the_sdk_takes():
    assert set(FAULT_CODES.values()) <= FAULT_KINDS
    assert all(code is None or isinstance(code, str) for _, code in FAULT_CODES)


def test_out_of_credit_at_the_handshake_refuses_the_boot_with_its_kind(monkeypatch):
    """OC3: Deepgram's 402 is named as an unpaid account, which retrying
    cannot fix, and not as a voice name or a network to check."""
    stt = refused_at_boot(monkeypatch, _RefusedWith(402, NO_CREDIT))
    health = stt.health()
    assert health.faults == ("out_of_credit",)
    assert health.detail == OUT_OF_CREDIT


def test_a_402_with_no_body_is_out_of_credit_by_its_status(monkeypatch):
    stt = refused_at_boot(monkeypatch, _RefusedWith(402))
    assert stt.health().faults == ("out_of_credit",)
    assert stt.health().detail.startswith("Deepgram says the account is out of credit (HTTP 402). Add credit")


def test_a_model_the_project_cannot_use_is_model_unavailable_at_boot(monkeypatch):
    """MC1 stage 3 at the handshake: 403 with INSUFFICIENT_PERMISSIONS is the
    page's "Project does not have access to the requested model"."""
    stt = refused_at_boot(monkeypatch, _RefusedWith(403, NO_ACCESS), config={"model": "nova-3-medical"})
    assert stt.health().faults == ("model_unavailable",)
    assert stt.health().detail == (
        "Deepgram does not serve the model 'nova-3-medical' (HTTP 403). Choose another under models.stt.model."
    )


def test_the_same_code_under_401_is_a_rejected_key_not_a_model(monkeypatch):
    """Deepgram's page gives INSUFFICIENT_PERMISSIONS under 401 for a user's
    permissions, so the table keys the code with its status."""
    stt = refused_at_boot(monkeypatch, _RefusedWith(401, {"err_code": "INSUFFICIENT_PERMISSIONS"}))
    assert stt.health().faults == ("start_failed",)
    assert "rejected the key" in stt.health().detail


@pytest.mark.parametrize("body", [{"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials."}, None])
def test_a_403_of_another_code_is_still_a_rejected_key(monkeypatch, body):
    stt = refused_at_boot(monkeypatch, _RefusedWith(403, body))
    assert stt.health().faults == ("start_failed",)
    assert "rejected the key in EMET_DEEPGRAM_KEY (HTTP 403)" in stt.health().detail


def test_the_kind_comes_from_the_code_never_from_the_message(monkeypatch):
    """A message that says model or credits names no kind: only the status
    and `err_code` do (contract 0.5.7 section 3.3)."""
    words = {"err_code": "Bad Request", "err_msg": "Project does not have access to the requested model. Out of credits."}
    stt = refused_at_boot(monkeypatch, _RefusedWith(400, words))
    assert stt.health().faults == ("start_failed",)


def test_an_unknown_model_is_refused_in_deepgrams_own_words(monkeypatch):
    """Deepgram documents no code that names an unknown model; its message
    does, so the boot's refusal quotes it in place of the library's
    "server rejected WebSocket connection"."""
    stt = refused_at_boot(monkeypatch, _RefusedWith(400, NO_SUCH_MODEL), config={"model": "nova-9"})
    assert stt.health().faults == ("start_failed",)
    assert stt.health().detail == (
        "Deepgram refused the connection: HTTP 400 Bad Request: Bad Request: No such model/language/tier combination found."
    )


def test_a_refusal_with_no_body_keeps_the_status_line(monkeypatch):
    stt = refused_at_boot(monkeypatch, _Rejected(400))
    assert stt.health().detail == (
        "Deepgram refused the connection with HTTP 400: server rejected WebSocket connection: HTTP 400"
    )


def test_at_another_url_the_server_is_named_by_its_host_and_deepgrams_console_is_not(monkeypatch):
    stt = refused_at_boot(monkeypatch, _RefusedWith(402, NO_CREDIT), url="wss://dg.internal:8443/v1/listen")
    assert stt.health().detail == (
        "the server at dg.internal:8443 says the account is out of credit (HTTP 402, ASR_PAYMENT_REQUIRED). "
        "Retrying will not help."
    )


def test_deepgrams_own_url_with_a_trailing_slash_is_still_deepgram(monkeypatch):
    stt = refused_at_boot(monkeypatch, _RefusedWith(402, NO_CREDIT), url=DEFAULT_URL + "/")
    assert stt.health().detail == OUT_OF_CREDIT


def test_out_of_credit_mid_run_is_the_finals_error_until_a_connection_opens(monkeypatch):
    """The stt-out-of-credit row: the final carries the sentence, `health()`
    names the kind for the engine to read after the error stop, and a turn
    whose connection opens clears it."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    server = Scripted(
        [None, _RefusedWith(402, NO_CREDIT), OSError("down"), _RefusedWith(402, NO_CREDIT), None],
        {1: [results("hello", final=True)]},
    )
    stt = make(server)
    run(stt.start())

    final = one_turn(stt)
    assert final.text == "" and final.error == OUT_OF_CREDIT
    assert stt.health().faults == ("out_of_credit",) and stt.health().detail == OUT_OF_CREDIT
    assert stt.describe().healthy, "the boot check is not run again"

    # Review finding providers/code#1 (contract 0.5.7 section 9, C15): a
    # connection that fails for a reason naming no kind ends the kind, so the
    # turn takes the words-lost row and its offline signal.
    assert one_turn(stt).error == "OSError: down"
    assert stt.health().ok and stt.health().faults == ()

    assert one_turn(stt).error == OUT_OF_CREDIT
    assert stt.health().faults == ("out_of_credit",)

    assert one_turn(stt).text == "hello"
    assert stt.health().ok and stt.health().faults == ()


def test_a_model_withdrawn_mid_run_is_model_unavailable_for_the_rest_of_the_run(monkeypatch):
    """The stt-model-rejected row, possible only with `preflight: false` or a
    model withdrawn after boot, since the preflight puts the model in the
    handshake."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    server = Scripted([None, _RefusedWith(403, NO_ACCESS), None], {1: [results("hello", final=True)]})
    stt = make(server)
    run(stt.start())

    final = one_turn(stt)
    rejected = "Deepgram does not serve the model 'nova-3' (HTTP 403). Choose another under models.stt.model."
    assert final.error == rejected and stt.last_error == rejected
    assert stt.health().faults == ("model_unavailable",)
    assert one_turn(stt).text == "hello"
    assert stt.health().faults == ("model_unavailable",), "a model that went away is not trusted again this run"


@pytest.mark.parametrize(
    ("url", "named"),
    [(None, "Deepgram"), ("wss://dg.example/v1/listen", "The server at dg.example")],
    ids=["deepgram", "another-server"],
)
def test_a_refused_handshake_mid_run_of_no_known_kind_quotes_deepgram(monkeypatch, url, named):
    """At another URL the sentence names that server (C7, review finding providers/code#5)."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    stt = make(Scripted([None, _RefusedWith(400, NO_SUCH_MODEL)]), **({"url": url} if url else {}))
    run(stt.start())
    final = one_turn(stt)
    assert final.error == (
        f"{named} refused the connection: HTTP 400 Bad Request: Bad Request: No such model/language/tier combination found."
    )
    assert stt.health().ok, "a fault of no kind costs one turn and leaves the plugin healthy"


def test_a_refusal_at_another_url_with_no_body_names_that_server(monkeypatch):
    stt = refused_at_boot(monkeypatch, _Rejected(400), url="wss://dg.example/v1/listen")
    assert stt.health().detail == (
        "The server at dg.example refused the connection with HTTP 400: server rejected WebSocket connection: HTTP 400"
    )


@pytest.mark.parametrize(
    ("exc", "said"),
    [
        (OSError("connection refused"), "could not reach wss://example.com/v1/listen: OSError: connection refused."),
        (asyncio.TimeoutError(), "could not reach wss://example.com/v1/listen within 10 s."),
    ],
    ids=["refused", "timeout"],
)
def test_a_password_in_the_url_reaches_no_boot_refusal(monkeypatch, exc, said):
    """Review finding providers/claims-scope#4: the sentence names the
    endpoint, which has no credentials, as the chat plugins' do."""
    stt = refused_at_boot(monkeypatch, exc, url="wss://owner:hunter2@example.com/v1/listen")
    assert (stt.health().detail or "").startswith(said)
    assert "hunter2" not in (stt.health().detail or "")


def test_websockets_own_refusal_is_read_for_its_kind(monkeypatch):
    """Review finding providers/tests#10: the library's InvalidStatus and
    Response, which the stand-ins above copy, give the same kind."""
    pytest.importorskip("websockets")
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    refused = InvalidStatus(Response(402, "Payment Required", Headers(), json.dumps(NO_CREDIT).encode()))
    stt = refused_at_boot(monkeypatch, refused)
    assert stt.health().faults == ("out_of_credit",) and stt.health().detail == OUT_OF_CREDIT


def test_each_start_refusal_names_its_kind_where_it_is_raised(monkeypatch):
    """The `"no key" in self._fault` copy is gone: the kind is set beside
    the reason, so rewording a reason cannot change it."""
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    no_key = make(FakeServer())
    run(no_key.start())
    assert no_key.health().faults == ("no_key",)

    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    for name in ("websockets", "websockets.asyncio", "websockets.asyncio.client"):
        monkeypatch.setitem(sys.modules, name, None)
    no_library = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat())
    run(no_library.start())
    assert no_library.health().faults == ("no_library",)

    unreachable = refused_at_boot(monkeypatch, OSError("no route"))
    assert unreachable.health().faults == ("start_failed",)


def test_a_start_refusal_and_a_lost_turn_are_logged_at_info(monkeypatch, caplog):
    """L16: the engine prints both, the refusal through `health().detail`
    and the turn through `Transcript.error`, and logging's last resort
    prints WARNING and above, so either at WARNING would print twice."""
    with caplog.at_level(logging.DEBUG, logger="emet_providers.deepgram"):
        refused_at_boot(monkeypatch, _RefusedWith(402, NO_CREDIT))
        stt = make(Scripted([None, OSError("[Errno -3] Temporary failure in name resolution")]))
        run(stt.start())
        assert one_turn(stt).error is not None
    said = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (logging.INFO, f"deepgram: {OUT_OF_CREDIT}") in said
    assert any(level == logging.INFO and "returning what was heard" in text for level, text in said)
    assert all(level < logging.WARNING for level, _ in said), said


def test_a_turn_with_no_final_in_time_is_logged_at_info(monkeypatch, caplog):
    """L16 on the other lost-turn branch, review finding providers/tests#6:
    the engine prints `Transcript.error`, so a WARNING would print it twice."""
    stt = started(FakeServer(), monkeypatch, timeout_s=0.05)

    async def scenario():
        await stt.feed(FRAME)
        await settle()
        stuck = asyncio.create_task(asyncio.sleep(30))
        stt._utterance.task.cancel()
        stt._utterance.task = stuck
        final = await stt.finish()
        stuck.cancel()
        return final

    with caplog.at_level(logging.DEBUG, logger="emet_providers.deepgram"):
        final = run(scenario())
    assert "no final within" in (final.error or "")
    said = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert any(level == logging.INFO and "no final within" in text for level, text in said), said
    assert all(level < logging.WARNING for level, _ in said), said
