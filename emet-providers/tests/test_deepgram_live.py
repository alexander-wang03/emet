"""One real round trip to Deepgram, when a key is present and asked for.

Skipped in CI and on any machine without both `EMET_DEEPGRAM_KEY` and
`EMET_LIVE_TESTS=1` in the environment. It sends two seconds of silence, so
the transcript is expected to be empty; what it proves is the half the fake
cannot: that the handshake, the header, the query string and the CloseStream
sequence are what the live service accepts today. Run it by hand before a
release, and whenever Deepgram announces a change.

With `EMET_LIVE_RECORD=<dir>` as well, every message Deepgram sent is kept
as it came, with the model version it reported (`conftest.py`). The test
below the live one checks that mode against a stand-in, and always runs.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from emet_sdk.types import AudioFormat

from emet_providers.deepgram import DEFAULT_KEY_ENV, DeepgramTranscriber

LIVE = bool(os.environ.get(DEFAULT_KEY_ENV)) and os.environ.get("EMET_LIVE_TESTS") == "1"


@pytest.mark.skipif(not LIVE, reason=f"set {DEFAULT_KEY_ENV} and EMET_LIVE_TESTS=1 to call Deepgram for real")
def test_a_live_round_trip_returns_a_final(live_record):
    pytest.importorskip("websockets", reason="emet-providers[deepgram] is not installed")
    connect = None
    if live_record is not None:
        from websockets.asyncio.client import connect as network

        connect = live_record.connect(network)
        live_record.vendor = "deepgram"
    stt = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat(), connect=connect)
    if live_record is not None:
        live_record.model_asked = stt.model_name

    async def scenario():
        await stt.start()
        assert stt.describe().healthy, stt.health().detail
        for _ in range(25):  # two seconds of silence at 80 ms frames
            await stt.feed(bytes(2560))
            await asyncio.sleep(0.08)
        final = await stt.finish()
        await stt.shutdown()
        return final

    final = asyncio.run(scenario())
    assert final.final
    assert isinstance(final.text, str)
    assert stt.last_error is None, stt.last_error


# ------------------------------------------------------- the recording mode


class _Closed(Exception):
    """Shaped like websockets' ConnectionClosed."""

    def __init__(self) -> None:
        super().__init__("closed")
        self.rcvd = self.sent = object()


class _StandIn:
    """Answers a connection the way Deepgram does after CloseStream: one
    final, the Metadata with the model, then the close."""

    def __init__(self) -> None:
        self.headers: list[dict] = []

    async def connect(self, url, **kwargs):
        self.headers.append(kwargs.get("additional_headers") or {})
        return _Socket()


class _Socket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, data):
        if isinstance(data, str) and json.loads(data).get("type") == "CloseStream":
            for message in (RESULTS, METADATA):
                self.inbox.put_nowait(message)
            self.inbox.put_nowait(_Closed())

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        pass


RESULTS = json.dumps(
    {
        "type": "Results",
        "is_final": True,
        "channel": {"alternatives": [{"transcript": "", "confidence": 0.0}]},
        "metadata": {"model_info": {"name": "general-nova-3", "version": "2025-04-17.21547", "arch": "nova-3"}},
    }
)
METADATA = json.dumps(
    {
        "type": "Metadata",
        "model_info": {"40bd": {"name": "general-nova-3", "version": "2025-04-17.21547", "arch": "nova-3"}},
    }
)

FAKE_KEY = "dg-recording-test-key-0123456789"


def test_the_recording_mode_keeps_what_deepgram_sent_and_never_the_key(monkeypatch, tmp_path, live_recorder):
    monkeypatch.setenv(DEFAULT_KEY_ENV, FAKE_KEY)
    record = live_recorder(tmp_path)
    record.vendor = "deepgram"
    server = _StandIn()
    stt = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat(), connect=record.connect(server.connect))

    async def scenario():
        await stt.start()
        await stt.feed(bytes(2560))
        final = await stt.finish()
        await stt.shutdown()
        return final

    assert asyncio.run(scenario()).final
    assert server.headers[0] == {"Authorization": f"Token {FAKE_KEY}"}, "the key still reached the server"
    path = record.save()
    text = path.read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    data = json.loads(text)
    assert data["format"] == "emet-live-record/1" and data["vendor"] == "deepgram"
    assert data["model"]["version"] == "2025-04-17.21547"
    preflight, utterance = data["connections"]
    assert utterance["audio_bytes_sent"] == 2560
    assert [m.get("text") for m in utterance["received"][:2]] == [RESULTS, METADATA], "as it came"
    assert data["time"].startswith(data["recorded"]) and len(data["recorded"]) == 10


@pytest.fixture
def one_record_at_teardown(tmp_path, monkeypatch):
    """`EMET_LIVE_RECORD` names a directory. Set up before `live_record` and
    so torn down after it, this checks what that fixture left behind."""
    directory = tmp_path / "live"
    monkeypatch.setenv("EMET_LIVE_RECORD", str(directory))
    yield directory
    files = sorted(directory.glob("*.json"))
    assert len(files) == 1, f"the live_record fixture left {len(files)} records"
    text = files[0].read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    data = json.loads(text)
    assert data["vendor"] == "deepgram" and len(data["connections"]) == 2


def test_the_live_record_fixture_saves_one_record_at_teardown(one_record_at_teardown, request, monkeypatch):
    """The path the canary relies on: the fixture itself, driven by the
    variable, and the file written after the test, pass or fail."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, FAKE_KEY)
    record = request.getfixturevalue("live_record")
    assert record is not None, "EMET_LIVE_RECORD names a directory, so the fixture records"
    record.vendor = "deepgram"
    stt = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat(), connect=record.connect(_StandIn().connect))

    async def scenario():
        await stt.start()
        await stt.feed(bytes(2560))
        await stt.finish()
        await stt.shutdown()

    asyncio.run(scenario())
    assert not list(one_record_at_teardown.glob("*.json")), "written at teardown, not before"


def test_the_live_record_fixture_is_none_without_the_variable(request, monkeypatch):
    monkeypatch.delenv("EMET_LIVE_RECORD", raising=False)
    assert request.getfixturevalue("live_record") is None


def test_a_record_holding_a_key_is_refused_and_nothing_is_written(monkeypatch, tmp_path, live_recorder):
    monkeypatch.setenv("EMET_SOMETHING_KEY", FAKE_KEY)
    record = live_recorder(tmp_path / "out")
    record.connections.append({"url": "wss://example.test", "received": [{"t": 0.0, "text": f"echo {FAKE_KEY}"}]})
    with pytest.raises(RuntimeError, match="EMET_SOMETHING_KEY") as caught:
        record.save()
    assert FAKE_KEY not in str(caught.value)
    assert not (tmp_path / "out").exists()
