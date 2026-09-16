"""The Anthropic language model, against a stand-in for the Messages API.

No network and no key. An `httpx.MockTransport` answers every request the
way the API would, in the shapes Anthropic's reference gives (verified
2026-09-15), and records what it was sent. What these tests prove is the
plugin's half of the protocol: headers, body, the mapping of the seam's
messages and tools into the vendor's, and how a streamed reply is assembled
back into text, tool calls and a stop reason. The live test beside this file
proves the other half, when a key is present.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

httpx = pytest.importorskip("httpx", reason="emet-providers[anthropic] is not installed")

from emet_sdk.plugin import LanguageModelPlugin  # noqa: E402
from emet_sdk.types import Message, Prompt, ReplyDone, TextDelta, ToolCall, ToolSpec  # noqa: E402

from emet_providers.anthropic import (  # noqa: E402
    API_VERSION,
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_URL,
    FALLBACK_BETA_ARRAY,
    FALLBACK_BETA_DEFAULT,
    AnthropicLanguageModel,
)

KEY = "sk-ant-test"


def run(coro):
    return asyncio.run(coro)


async def collect(events) -> list:
    return [e async for e in events]


# -------------------------------------------------------------- the fake


def sse(*events: tuple[str, dict]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def message_stream(
    *pieces: str,
    stop: str = "end_turn",
    tool: tuple[str, dict] | None = None,
    model: str = "claude-opus-5",
    close: bool = True,
) -> bytes:
    """A whole streamed message in the documented event sequence."""
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "usage": {"input_tokens": 12, "output_tokens": 1},
                },
            },
        ),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ]
    for piece in pieces:
        events.append(
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}})
        )
    events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    if tool is not None:
        name, arguments = tool
        encoded = json.dumps(arguments)
        events += [
            (
                "content_block_start",
                {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}},
            ),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": encoded[:6]}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": encoded[6:]}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ]
    if close:
        events += [
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 7}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    return sse(*events)


class FakeAnthropic:
    """Answers `GET /v1/models` and `POST /v1/messages`, recording each request."""

    def __init__(
        self,
        stream: bytes = b"",
        *,
        models_status: int = 200,
        reply_status: int = 200,
        reply_json: dict | None = None,
        raise_on_connect: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.models_status = models_status
        self.reply_status = reply_status
        self.reply_json = reply_json
        self.raise_on_connect = raise_on_connect
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if request.method == "GET" and request.url.path.endswith("/v1/models"):
            if self.models_status == 200:
                return httpx.Response(200, json={"data": [{"id": "claude-opus-5"}]})
            return httpx.Response(
                self.models_status,
                json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
            )
        if request.method == "POST" and request.url.path.endswith("/v1/messages"):
            if self.reply_status != 200:
                return httpx.Response(self.reply_status, json=self.reply_json or {"type": "error", "error": {"type": "api_error", "message": "boom"}})
            return httpx.Response(200, content=self.stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"error": {"type": "not_found_error", "message": request.url.path}})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def make(fake: FakeAnthropic, *, config: dict | None = None, **params: Any) -> AnthropicLanguageModel:
    cfg: dict[str, Any] = {"provider": "anthropic", "params": params}
    cfg.update(config or {})
    return AnthropicLanguageModel(cfg, transport=httpx.MockTransport(fake))


def started(fake: FakeAnthropic, monkeypatch, **kw) -> AnthropicLanguageModel:
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(fake, **kw)
    run(llm.start())
    assert llm.describe().healthy, llm.health().detail
    return llm


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def prompt(*texts: str, system: str = "Be brief.", tools: tuple = ()) -> Prompt:
    messages = tuple(
        Message(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts)
    )
    return Prompt(system=system, messages=messages, tools=tools, max_tokens=300)


# --------------------------------------------------------------- request


def test_it_is_a_language_model_plugin_registered_as_anthropic():
    llm = make(FakeAnthropic())
    assert isinstance(llm, LanguageModelPlugin)
    assert llm.provider == "anthropic"
    assert llm.url == DEFAULT_URL


def test_the_request_carries_the_documented_headers_and_no_sampling_knobs(monkeypatch):
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch)
    run(collect(llm.reply(prompt("hello"))))
    (post,) = fake.posts
    assert post.headers["x-api-key"] == KEY
    assert post.headers["anthropic-version"] == API_VERSION
    assert "anthropic-beta" not in post.headers
    body = body_of(post)
    assert body["model"] == DEFAULT_MODEL
    assert body["max_tokens"] == 300
    assert body["stream"] is True
    assert body["system"] == "Be brief."
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tools", "output_config", "fallbacks"):
        assert forbidden not in body, forbidden


def test_the_souls_model_and_the_bodys_effort_are_sent(monkeypatch):
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, config={"model": "claude-sonnet-5"}, effort="low")
    run(collect(llm.reply(prompt("hello"))))
    body = body_of(fake.posts[0])
    assert body["model"] == "claude-sonnet-5"
    assert body["output_config"] == {"effort": "low"}
    assert llm.describe().model == "claude-sonnet-5"


def test_fallbacks_are_off_unless_asked_and_carry_the_matching_beta_header(monkeypatch):
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, fallbacks="default")
    run(collect(llm.reply(prompt("hello"))))
    post = fake.posts[0]
    assert post.headers["anthropic-beta"] == FALLBACK_BETA_DEFAULT
    assert body_of(post)["fallbacks"] == "default"

    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, fallbacks=["claude-opus-4-8"])
    run(collect(llm.reply(prompt("hello"))))
    post = fake.posts[0]
    assert post.headers["anthropic-beta"] == FALLBACK_BETA_ARRAY
    assert body_of(post)["fallbacks"] == [{"model": "claude-opus-4-8"}]


def test_tools_and_tool_traffic_take_the_vendors_shape(monkeypatch):
    fake = FakeAnthropic(message_stream("done"))
    llm = started(fake, monkeypatch)
    remember = ToolSpec(name="remember", description="keep a fact", parameters={"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]})
    call = ToolCall(id="toolu_1", name="remember", arguments={"fact": "likes tea"})
    messages = (
        Message(role="user", content="remember I like tea"),
        Message(role="assistant", content="Noted.", tool_calls=(call,)),
        Message(role="tool", content="stored", tool_call_id="toolu_1"),
        Message(role="tool", content="also indexed", tool_call_id="toolu_1"),
    )
    run(collect(llm.reply(Prompt(system="s", messages=messages, tools=(remember,)))))
    body = body_of(fake.posts[0])
    assert body["tools"] == [{"name": "remember", "description": "keep a fact", "input_schema": remember.parameters}]
    assert body["messages"] == [
        {"role": "user", "content": "remember I like tea"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Noted."},
                {"type": "tool_use", "id": "toolu_1", "name": "remember", "input": {"fact": "likes tea"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "stored"},
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "also indexed"},
            ],
        },
    ]


def test_a_proxy_url_is_honoured(monkeypatch):
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, url="https://proxy.local/anthropic/")
    run(collect(llm.reply(prompt("hello"))))
    assert str(fake.posts[0].url) == "https://proxy.local/anthropic/v1/messages"


# ----------------------------------------------------------------- start


def test_preflight_lists_models_once_and_nothing_else(monkeypatch):
    fake = FakeAnthropic()
    started(fake, monkeypatch)
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/v1/models")]
    assert fake.requests[0].headers["x-api-key"] == KEY


def test_preflight_can_be_turned_off(monkeypatch):
    fake = FakeAnthropic()
    started(fake, monkeypatch, preflight=False)
    assert fake.requests == []


def test_no_key_is_unhealthy_names_the_variable_and_sends_nothing(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    fake = FakeAnthropic()
    llm = make(fake)
    run(llm.start())
    assert not llm.describe().healthy
    assert DEFAULT_KEY_ENV in (llm.health().detail or "")
    assert "no_key" in llm.health().faults
    assert fake.requests == []


def test_the_souls_key_env_is_honoured(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    monkeypatch.setenv("MY_ANTHROPIC", KEY)
    fake = FakeAnthropic()
    llm = make(fake, config={"key_env": "MY_ANTHROPIC"})
    run(llm.start())
    assert llm.describe().healthy
    assert fake.requests[0].headers["x-api-key"] == KEY


def test_a_rejected_key_is_unhealthy_with_the_status(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, "wrong")
    llm = make(FakeAnthropic(models_status=401))
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert "401" in detail and DEFAULT_KEY_ENV in detail and "rejected" in detail


def test_an_unreachable_network_is_unhealthy_and_says_so(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(FakeAnthropic(raise_on_connect=httpx.ConnectError("name resolution failed")))
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert "could not reach" in detail and "network" in detail


def test_without_httpx_the_hint_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    monkeypatch.setitem(sys.modules, "httpx", None)
    llm = AnthropicLanguageModel({"provider": "anthropic"})
    run(llm.start())
    assert not llm.describe().healthy
    assert "emet-providers[anthropic]" in (llm.health().detail or "")


# ----------------------------------------------------------------- reply


def test_text_streams_as_deltas_and_the_final_carries_everything(monkeypatch):
    fake = FakeAnthropic(message_stream("Hello", ", world", "."))
    llm = started(fake, monkeypatch)
    events = run(collect(llm.reply(prompt("hi"))))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello", ", world", "."]
    done = events[-1]
    assert isinstance(done, ReplyDone)
    assert done.text == "Hello, world."
    assert done.stop_reason == "end"
    assert done.model == "claude-opus-5"
    assert (done.input_tokens, done.output_tokens) == (12, 7)
    assert done.error is None


def test_a_tool_call_is_assembled_from_json_pieces_and_stops_with_tool(monkeypatch):
    fake = FakeAnthropic(message_stream("Let me note that.", stop="tool_use", tool=("remember", {"fact": "likes tea", "n": 2})))
    llm = started(fake, monkeypatch)
    events = run(collect(llm.reply(prompt("remember I like tea"))))
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert calls == [ToolCall(id="toolu_1", name="remember", arguments={"fact": "likes tea", "n": 2})]
    done = events[-1]
    assert done.stop_reason == "tool" and done.tool_calls == tuple(calls)
    assert done.text == "Let me note that."


@pytest.mark.parametrize(
    ("vendor", "seam"),
    [("end_turn", "end"), ("stop_sequence", "end"), ("max_tokens", "length"), ("refusal", "refusal"), ("pause_turn", "end"), ("something_new", "end")],
)
def test_stop_reasons_are_translated(monkeypatch, vendor, seam):
    llm = started(FakeAnthropic(message_stream("x", stop=vendor)), monkeypatch)
    assert run(collect(llm.reply(prompt("q"))))[-1].stop_reason == seam


def test_an_http_error_ends_the_reply_with_the_vendors_message(monkeypatch):
    fake = FakeAnthropic(reply_status=429, reply_json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
    llm = started(fake, monkeypatch)
    events = run(collect(llm.reply(prompt("q"))))
    (done,) = events
    assert done.stop_reason == "error"
    assert "429" in (done.error or "") and "slow down" in (done.error or "")
    assert llm.last_error == done.error
    assert llm.describe().healthy, "a failed reply is not a broken plugin"


def test_an_error_event_mid_stream_keeps_the_text_so_far(monkeypatch):
    events_bytes = sse(
        ("message_start", {"type": "message_start", "message": {"model": "claude-opus-5", "usage": {"input_tokens": 3}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half a"}}),
        ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
    )
    llm = started(FakeAnthropic(events_bytes), monkeypatch)
    events = run(collect(llm.reply(prompt("q"))))
    done = events[-1]
    assert done.text == "Half a" and done.stop_reason == "error"
    assert "overloaded_error" in (done.error or "")


def test_a_stream_that_ends_early_still_returns_the_text(monkeypatch):
    llm = started(FakeAnthropic(message_stream("cut", close=False)), monkeypatch)
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert done.text == "cut" and done.stop_reason == "end"


def test_unknown_block_types_are_ignored(monkeypatch):
    events_bytes = sse(
        ("message_start", {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "fine"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    )
    llm = started(FakeAnthropic(events_bytes), monkeypatch)
    assert run(collect(llm.reply(prompt("q"))))[-1].text == "fine"


def test_replying_before_start_is_an_error_final_not_an_exception():
    llm = make(FakeAnthropic())
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.stop_reason == "error" and "not started" in (done.error or "")


def test_shutdown_closes_the_client_and_reports_unhealthy(monkeypatch):
    llm = started(FakeAnthropic(), monkeypatch)
    run(llm.shutdown())
    assert not llm.describe().healthy
    assert llm._client is None
