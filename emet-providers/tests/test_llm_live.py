"""One real reply from each vendor, when a key is present and asked for.

Skipped in CI and on any machine without `EMET_LIVE_TESTS=1` and the vendor's
key in the environment. Each asks for a single word and spends a few tokens.
What it proves is the half the stand-ins cannot: that the headers, the body
and the stream are what the live service accepts today. Run by hand before a
release, and whenever a vendor announces a change.

With `EMET_LIVE_RECORD=<dir>` as well, each keeps the stream it got, byte for
byte, with the model the vendor reported (`conftest.py`), so the stand-ins'
parsing can be tested against real bytes too. The tests below the live ones
check that mode against a stand-in, and run wherever httpx is installed.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from emet_sdk.types import Message, Prompt, ReplyDone, TextDelta

LIVE = os.environ.get("EMET_LIVE_TESTS") == "1"


def _round_trip(llm, record=None):
    prompt = Prompt(
        system="You are a test. Reply with the single word: ready.",
        messages=(Message(role="user", content="Are you there?"),),
        max_tokens=64,
    )
    if record is not None:
        record.model_asked = llm.model_name

    async def scenario():
        await llm.start()
        assert llm.describe().healthy, llm.health().detail
        events = [e async for e in llm.reply(prompt)]
        await llm.shutdown()
        return events

    events = asyncio.run(scenario())
    done = events[-1]
    if record is not None and isinstance(done, ReplyDone):
        record.model = done.model
    assert isinstance(done, ReplyDone)
    assert done.stop_reason == "end", done
    assert done.text.strip(), done
    assert any(isinstance(e, TextDelta) for e in events)
    assert done.output_tokens is None or done.output_tokens > 0
    assert llm.last_error is None, llm.last_error
    return done


def _transport(record):
    if record is None:
        return None
    return record.transport()


@pytest.mark.skipif(
    not (LIVE and os.environ.get("EMET_ANTHROPIC_KEY")),
    reason="set EMET_ANTHROPIC_KEY and EMET_LIVE_TESTS=1 to call Anthropic for real",
)
def test_anthropic_answers(live_record):
    pytest.importorskip("httpx", reason="emet-providers[anthropic] is not installed")
    from emet_providers.anthropic import AnthropicLanguageModel

    if live_record is not None:
        live_record.vendor = "anthropic"
    done = _round_trip(AnthropicLanguageModel({"provider": "anthropic"}, transport=_transport(live_record)), live_record)
    assert done.model


@pytest.mark.skipif(
    not (LIVE and os.environ.get("EMET_OPENAI_KEY")),
    reason="set EMET_OPENAI_KEY and EMET_LIVE_TESTS=1 to call OpenAI for real",
)
def test_openai_answers(live_record):
    pytest.importorskip("httpx", reason="emet-providers[openai] is not installed")
    from emet_providers.openai import OpenAILanguageModel

    if live_record is not None:
        live_record.vendor = "openai"
    done = _round_trip(OpenAILanguageModel({"provider": "openai"}, transport=_transport(live_record)), live_record)
    assert done.model


# ------------------------------------------------------- the recording mode


FAKE_KEY = "sk-recording-test-key-0123456789"

#: Anthropic's documented event sequence, cut to what one word needs.
STREAM = (
    'event: message_start\ndata: {"type": "message_start", "message": {"id": "m", "model": "claude-test-1",'
    ' "content": [], "usage": {"input_tokens": 3, "output_tokens": 1}}}\n\n'
    'event: content_block_start\ndata: {"type": "content_block_start", "index": 0,'
    ' "content_block": {"type": "text", "text": ""}}\n\n'
    'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0,'
    ' "delta": {"type": "text_delta", "text": "ready"}}\n\n'
    'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n'
    'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"},'
    ' "usage": {"output_tokens": 1}}\n\n'
    'event: message_stop\ndata: {"type": "message_stop"}\n\n'
)


def test_the_recording_mode_keeps_the_stream_byte_for_byte_and_never_the_key(monkeypatch, tmp_path, live_recorder):
    httpx = pytest.importorskip("httpx", reason="emet-providers[anthropic] is not installed")
    from emet_providers.anthropic import AnthropicLanguageModel

    monkeypatch.setenv("EMET_ANTHROPIC_KEY", FAKE_KEY)
    body = STREAM.encode()
    pieces = [body[:50], body[50:51], body[51:]]
    seen: list = []

    class Pieces(httpx.AsyncByteStream):
        async def __aiter__(self):
            for piece in pieces:
                yield piece

    def server(request):
        seen.append(request.headers.get("x-api-key"))
        if request.method == "GET":
            return httpx.Response(200, json={"data": []}, headers={"set-cookie": f"session={FAKE_KEY}"})
        return httpx.Response(
            200,
            stream=Pieces(),
            headers={"content-type": "text/event-stream", "request-id": "req_1", "x-echo": FAKE_KEY},
        )

    record = live_recorder(tmp_path)
    record.vendor = "anthropic"
    llm = AnthropicLanguageModel({"provider": "anthropic"}, transport=record.transport(httpx.MockTransport(server)))
    done = _round_trip(llm, record)
    assert done.text == "ready" and done.model == "claude-test-1"
    assert seen == [FAKE_KEY, FAKE_KEY], "the key still reached the server"

    text = record.save().read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    data = json.loads(text)
    assert data["model"] == "claude-test-1" and data["model_asked"] == llm.model_name
    models, messages = data["exchanges"]
    assert models["method"] == "GET" and "set-cookie" not in models["headers"]
    assert messages["status"] == 200 and messages["headers"] == {"content-type": "text/event-stream", "request-id": "req_1"}
    assert [c["text"] for c in messages["chunks"]] == [p.decode() for p in pieces], "chunk by chunk, as they came"
    assert messages["body"]["model"] == llm.model_name and "Are you there?" in json.dumps(messages["body"])
