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
import types
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from emet_sdk.plugin import TranscriberPlugin
from emet_sdk.types import AudioFormat, Transcript

from emet_providers.deepgram import DEFAULT_KEY_ENV, DEFAULT_MODEL, DEFAULT_URL, DeepgramTranscriber

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
    """Shaped like websockets' ConnectionClosed: it carries rcvd and sent."""

    def __init__(self, code: int = 1000) -> None:
        super().__init__(f"closed {code}")
        self.rcvd = types.SimpleNamespace(code=code, reason="")
        self.sent = types.SimpleNamespace(code=code, reason="")


class _Rejected(Exception):
    """Shaped like websockets' InvalidStatus: it carries response.status_code."""

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
    assert q["punctuate"] == "true" and q["smart_format"] == "true"
    assert q["model"] == DEFAULT_MODEL


def test_the_souls_model_is_sent_and_reported():
    stt = make(FakeServer(), config={"model": "nova-2"})
    assert query_of(stt.request_url())["model"] == "nova-2"
    assert stt.describe().model == "nova-2"


def test_body_query_params_ride_along_but_cannot_change_the_format():
    stt = make(
        FakeServer(),
        query={"language": "en-GB", "keyterm": "Emet", "sample_rate": 8000, "encoding": "mulaw", "vad_events": True},
    )
    q = query_of(stt.request_url())
    assert q["language"] == "en-GB"
    assert q["keyterm"] == "Emet"
    assert q["vad_events"] == "true"
    assert q["sample_rate"] == "16000"
    assert q["encoding"] == "linear16"


def test_the_url_can_point_at_a_self_hosted_deployment():
    stt = make(FakeServer(), url="wss://deepgram.internal/v1/listen")
    assert stt.request_url().startswith("wss://deepgram.internal/v1/listen?")


def test_the_key_travels_as_a_token_header(monkeypatch):
    server = FakeServer()
    started(server, monkeypatch)
    assert server.kwargs[0]["additional_headers"] == {"Authorization": f"Token {KEY}"}
    assert server.kwargs[0]["open_timeout"] == 10.0


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
