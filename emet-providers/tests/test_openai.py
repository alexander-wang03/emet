"""The OpenAI language model, against a stand-in for Chat Completions.

No network and no key. An `httpx.MockTransport` answers in the shapes
OpenAI's streaming reference gives (verified 2026-09-15) and records what it
was sent. The same file stands for every server that speaks this protocol,
which is the reason the plugin speaks it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

httpx = pytest.importorskip("httpx", reason="emet-providers[openai] is not installed")

from emet_sdk.plugin import LanguageModelPlugin  # noqa: E402
from emet_sdk.types import Message, Prompt, ReplyDone, TextDelta, ToolCall, ToolSpec  # noqa: E402

from emet_providers.openai import DEFAULT_KEY_ENV, DEFAULT_MODEL, DEFAULT_URL, OpenAILanguageModel  # noqa: E402

KEY = "sk-test"


def run(coro):
    return asyncio.run(coro)


async def collect(events) -> list:
    return [e async for e in events]


# -------------------------------------------------------------- the fake


def chunk(delta: dict | None = None, *, finish: str | None = None, usage: dict | None = None, model: str = "gpt-5.6-terra") -> dict:
    data: dict[str, Any] = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": []}
    if delta is not None or finish is not None:
        data["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    if usage is not None:
        data["usage"] = usage
    return data


def stream(*chunks: dict, done: bool = True) -> bytes:
    text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        text += "data: [DONE]\n\n"
    return text.encode()


def reply_stream(*pieces: str, finish: str = "stop", usage: bool = True) -> bytes:
    chunks = [chunk({"role": "assistant", "content": ""})]
    chunks += [chunk({"content": p}) for p in pieces]
    chunks.append(chunk({}, finish=finish))
    if usage:
        chunks.append(chunk(usage={"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}))
    return stream(*chunks)


class FakeOpenAI:
    def __init__(
        self,
        body: bytes = b"",
        *,
        models_status: int = 200,
        reply_status: int = 200,
        reply_json: dict | None = None,
        raise_on_connect: Exception | None = None,
    ) -> None:
        self.body = body
        self.models_status = models_status
        self.reply_status = reply_status
        self.reply_json = reply_json
        self.raise_on_connect = raise_on_connect
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if request.method == "GET" and request.url.path.endswith("/models"):
            if self.models_status == 200:
                return httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-5.6-terra", "object": "model"}]})
            return httpx.Response(self.models_status, json={"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}})
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            if self.reply_status != 200:
                return httpx.Response(self.reply_status, json=self.reply_json or {"error": {"message": "boom", "type": "server_error"}})
            return httpx.Response(200, content=self.body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"error": {"message": request.url.path, "type": "not_found"}})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]


def make(fake: FakeOpenAI, *, config: dict | None = None, **params: Any) -> OpenAILanguageModel:
    cfg: dict[str, Any] = {"provider": "openai", "params": params}
    cfg.update(config or {})
    return OpenAILanguageModel(cfg, transport=httpx.MockTransport(fake))


def started(fake: FakeOpenAI, monkeypatch, **kw) -> OpenAILanguageModel:
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(fake, **kw)
    run(llm.start())
    assert llm.describe().healthy, llm.health().detail
    return llm


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def prompt(*texts: str, system: str = "Be brief.", tools: tuple = ()) -> Prompt:
    messages = tuple(Message(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts))
    return Prompt(system=system, messages=messages, tools=tools, max_tokens=300)


# --------------------------------------------------------------- request


def test_it_is_a_language_model_plugin_registered_as_openai():
    llm = make(FakeOpenAI())
    assert isinstance(llm, LanguageModelPlugin)
    assert llm.provider == "openai"
    assert llm.url == DEFAULT_URL


def test_the_request_carries_a_bearer_token_and_the_documented_fields(monkeypatch):
    fake = FakeOpenAI(reply_stream("hi"))
    llm = started(fake, monkeypatch)
    run(collect(llm.reply(prompt("hello"))))
    (post,) = fake.posts
    assert post.headers["authorization"] == f"Bearer {KEY}"
    assert str(post.url) == "https://api.openai.com/v1/chat/completions"
    body = body_of(post)
    assert body["model"] == DEFAULT_MODEL
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_completion_tokens"] == 300
    assert "max_tokens" not in body
    assert body["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hello"}]
    assert "tools" not in body and "temperature" not in body


def test_an_older_compatible_server_can_be_given_max_tokens(monkeypatch):
    fake = FakeOpenAI(reply_stream("hi"))
    llm = started(fake, monkeypatch, url="http://localhost:11434/v1", max_tokens_field="max_tokens")
    run(collect(llm.reply(prompt("hello"))))
    post = fake.posts[0]
    assert str(post.url) == "http://localhost:11434/v1/chat/completions"
    body = body_of(post)
    assert body["max_tokens"] == 300 and "max_completion_tokens" not in body


def test_the_souls_model_and_an_organization_are_sent(monkeypatch):
    fake = FakeOpenAI(reply_stream("hi"))
    llm = started(fake, monkeypatch, config={"model": "gpt-5.6-luna"}, organization="org-1")
    run(collect(llm.reply(prompt("hello"))))
    post = fake.posts[0]
    assert body_of(post)["model"] == "gpt-5.6-luna"
    assert post.headers["openai-organization"] == "org-1"
    assert llm.describe().model == "gpt-5.6-luna"


def test_tools_and_tool_traffic_take_the_vendors_shape(monkeypatch):
    fake = FakeOpenAI(reply_stream("done"))
    llm = started(fake, monkeypatch)
    remember = ToolSpec(name="remember", description="keep a fact", parameters={"type": "object", "properties": {"fact": {"type": "string"}}})
    call = ToolCall(id="call_1", name="remember", arguments={"fact": "likes tea"})
    messages = (
        Message(role="user", content="remember I like tea"),
        Message(role="assistant", content="", tool_calls=(call,)),
        Message(role="tool", content="stored", tool_call_id="call_1"),
    )
    run(collect(llm.reply(Prompt(system="s", messages=messages, tools=(remember,)))))
    body = body_of(fake.posts[0])
    assert body["tools"] == [{"type": "function", "function": {"name": "remember", "description": "keep a fact", "parameters": remember.parameters}}]
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "remember I like tea"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "remember", "arguments": json.dumps({"fact": "likes tea"})}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "stored"},
    ]


# ----------------------------------------------------------------- start


def test_preflight_lists_models_once(monkeypatch):
    fake = FakeOpenAI()
    started(fake, monkeypatch)
    assert [(r.method, r.url.path) for r in fake.requests] == [("GET", "/v1/models")]
    assert fake.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_no_key_is_unhealthy_names_the_variable_and_sends_nothing(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    fake = FakeOpenAI()
    llm = make(fake)
    run(llm.start())
    assert not llm.describe().healthy
    assert DEFAULT_KEY_ENV in (llm.health().detail or "")
    assert fake.requests == []


def test_a_rejected_key_is_unhealthy_with_the_status(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, "wrong")
    llm = make(FakeOpenAI(models_status=401))
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert "401" in detail and DEFAULT_KEY_ENV in detail and "rejected" in detail


def test_an_unreachable_server_is_unhealthy_and_names_it(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(FakeOpenAI(raise_on_connect=httpx.ConnectError("refused")), url="http://localhost:11434/v1")
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert "localhost:11434" in detail and "could not reach" in detail


def test_without_httpx_the_hint_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    monkeypatch.setitem(sys.modules, "httpx", None)
    llm = OpenAILanguageModel({"provider": "openai"})
    run(llm.start())
    assert not llm.describe().healthy
    assert "emet-providers[openai]" in (llm.health().detail or "")


# ----------------------------------------------------------------- reply


def test_text_streams_as_deltas_and_the_final_carries_usage(monkeypatch):
    llm = started(FakeOpenAI(reply_stream("Hello", ", world", ".")), monkeypatch)
    events = run(collect(llm.reply(prompt("hi"))))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello", ", world", "."]
    done = events[-1]
    assert isinstance(done, ReplyDone)
    assert done.text == "Hello, world." and done.stop_reason == "end"
    assert done.model == "gpt-5.6-terra"
    assert (done.input_tokens, done.output_tokens) == (9, 4)


def test_tool_calls_are_accumulated_by_index_across_chunks(monkeypatch):
    body = stream(
        chunk({"role": "assistant", "content": None}),
        chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "remember", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"fact": "li'}}]}),
        chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "lookup", "arguments": '{"q": 1}'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'kes tea"}'}}]}),
        chunk({}, finish="tool_calls"),
        chunk(usage={"prompt_tokens": 5, "completion_tokens": 9}),
    )
    llm = started(FakeOpenAI(body), monkeypatch)
    events = run(collect(llm.reply(prompt("remember I like tea"))))
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert calls == [
        ToolCall(id="call_a", name="remember", arguments={"fact": "likes tea"}),
        ToolCall(id="call_b", name="lookup", arguments={"q": 1}),
    ]
    done = events[-1]
    assert done.stop_reason == "tool" and done.tool_calls == tuple(calls) and done.text == ""


@pytest.mark.parametrize(
    ("vendor", "seam"),
    [("stop", "end"), ("length", "length"), ("tool_calls", "tool"), ("content_filter", "refusal"), ("function_call", "tool"), ("novel", "end")],
)
def test_finish_reasons_are_translated(monkeypatch, vendor, seam):
    llm = started(FakeOpenAI(reply_stream("x", finish=vendor)), monkeypatch)
    assert run(collect(llm.reply(prompt("q"))))[-1].stop_reason == seam


def test_a_stream_without_done_or_usage_still_finishes(monkeypatch):
    body = stream(chunk({"content": "ok"}), chunk({}, finish="stop"), done=False)
    llm = started(FakeOpenAI(body), monkeypatch)
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert done.text == "ok" and done.stop_reason == "end" and done.output_tokens is None


def test_an_http_error_ends_the_reply_with_the_vendors_message(monkeypatch):
    fake = FakeOpenAI(reply_status=429, reply_json={"error": {"message": "Rate limit reached", "type": "rate_limit_error", "code": "rate_limit_exceeded"}})
    llm = started(fake, monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.stop_reason == "error"
    assert "429" in (done.error or "") and "Rate limit reached" in (done.error or "")
    assert llm.describe().healthy


def test_an_error_object_in_the_stream_ends_the_reply(monkeypatch):
    body = stream(chunk({"content": "Half"}), {"error": {"message": "The server had an error", "type": "server_error"}})
    llm = started(FakeOpenAI(body), monkeypatch)
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert done.text == "Half" and done.stop_reason == "error"
    assert "server_error" in (done.error or "")


def test_replying_before_start_is_an_error_final_not_an_exception():
    (done,) = run(collect(make(FakeOpenAI()).reply(prompt("q"))))
    assert done.stop_reason == "error" and "not started" in (done.error or "")
