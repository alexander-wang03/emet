"""The Anthropic language model, against a stand-in for the Messages API.

No network and no key. An `httpx.MockTransport` answers every request the
way the API would, in the shapes Anthropic's reference gives (verified
2026-09-15; the Get a Model object, the error bodies and the version
verified 2026-11-27), and records what it was sent. What these tests prove
is the plugin's half of the protocol: headers, body, the mapping of the
seam's messages and tools into the vendor's, how a streamed reply is
assembled back into text, tool calls and a stop reason, and which fault kind
each failure leaves in `health()`. The live test beside this file proves the
other half, when a key is present.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    FAULT_CODES,
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


def model_object(model_id: str = "claude-opus-5", *, lifecycle: str = "active", retires_at: str | None = None) -> dict:
    """One model as `GET /v1/models/{id}` returns it, cut to the fields the plugin reads."""
    return {
        "type": "model",
        "id": model_id,
        "display_name": "Claude Opus 5",
        "created_at": "2026-07-24T00:00:00Z",
        "lifecycle": lifecycle,
        "deprecated_at": None if lifecycle == "active" else "2026-09-30T00:00:00Z",
        "retires_at": retires_at,
        "line": "opus",
    }


def error_body(kind: str, message: str, code: str | None = None) -> dict:
    """An error in the shape Anthropic's errors page gives, verified 2026-11-27."""
    err: dict[str, Any] = {"type": kind, "message": message}
    if code is not None:
        err["details"] = {"error_code": code}
    return {"type": "error", "error": err, "request_id": "req_011"}


class FakeAnthropic:
    """Answers the models paths and `POST /v1/messages`, recording each request.

    `models_status` answers both models paths (a rejected key fails at any
    endpoint); `model_status` and `model_json` answer the per-model path
    alone, and `list_status` the list. `reply_statuses` answers the posts in
    turn, the last repeated, so a test can fail one reply and pass the next.
    """

    def __init__(
        self,
        stream: bytes = b"",
        *,
        models_status: int = 200,
        model_status: int | None = None,
        model_json: dict | None = None,
        list_status: int | None = None,
        reply_status: int = 200,
        reply_statuses: tuple[int, ...] | None = None,
        reply_json: dict | None = None,
        raise_on_connect: Exception | None = None,
    ) -> None:
        self.stream = stream
        self.models_status = models_status
        self.model_status = model_status
        self.model_json = model_json
        self.list_status = list_status
        self.reply_statuses = list(reply_statuses or (reply_status,))
        self.reply_json = reply_json
        self.raise_on_connect = raise_on_connect
        self.requests: list[httpx.Request] = []

    def _refusal(self, status: int) -> httpx.Response:
        if status == 404:
            return httpx.Response(404, json=error_body("not_found_error", "model: claude-nonesuch"))
        if status == 402:
            return httpx.Response(402, json=error_body("billing_error", "Billing error"))
        if status in (400, 405) or status >= 500:
            return httpx.Response(status, json=error_body("api_error", "Internal server error"))
        return httpx.Response(status, json=error_body("authentication_error", "invalid x-api-key"))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        path = request.url.path
        if request.method == "GET" and "/v1/models/" in path:
            status = self.model_status if self.model_status is not None else self.models_status
            if status != 200:
                return self._refusal(status)
            model_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=self.model_json or model_object(model_id))
        if request.method == "GET" and path.endswith("/v1/models"):
            status = self.list_status if self.list_status is not None else self.models_status
            if status != 200:
                return self._refusal(status)
            return httpx.Response(200, json={"data": [{"id": "claude-opus-5"}]})
        if request.method == "POST" and path.endswith("/v1/messages"):
            status = self.reply_statuses.pop(0) if len(self.reply_statuses) > 1 else self.reply_statuses[0]
            if status != 200:
                return httpx.Response(status, json=self.reply_json or error_body("api_error", "boom"))
            return httpx.Response(200, content=self.stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"error": {"type": "not_found_error", "message": path}})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


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


def booted(fake: FakeAnthropic, monkeypatch, **kw) -> AnthropicLanguageModel:
    """Start the plugin whatever its health, for the tests of an unhealthy start."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(fake, **kw)
    run(llm.start())
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


def test_the_api_version_is_2026_10_21_on_every_request(monkeypatch):
    """MC5. The version the plugin was checked against, read 2026-11-27."""
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch)
    run(collect(llm.reply(prompt("hello"))))
    assert API_VERSION == "2026-10-21"
    assert [r.headers["anthropic-version"] for r in fake.requests] == ["2026-10-21", "2026-10-21"]


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
    # 2026-10-21 answers the first three with a 400 on every model.
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tools", "output_config", "fallbacks"):
        assert forbidden not in body, forbidden


def test_no_message_carries_a_block_2026_10_21_refuses(monkeypatch):
    """No `document` block in an assistant turn, no `citations` on user text."""
    fake = FakeAnthropic(message_stream("done"))
    llm = started(fake, monkeypatch)
    call = ToolCall(id="toolu_1", name="remember", arguments={"fact": "tea"})
    messages = (
        Message(role="user", content="remember tea"),
        Message(role="assistant", content="Noted.", tool_calls=(call,)),
        Message(role="tool", content="stored", tool_call_id="toolu_1"),
        Message(role="assistant", content="Done."),
        Message(role="user", content="thanks"),
    )
    run(collect(llm.reply(Prompt(system="s", messages=messages))))
    encoded = json.dumps(body_of(fake.posts[0]))
    assert '"document"' not in encoded and '"citations"' not in encoded


def test_the_souls_model_and_the_bodys_effort_are_sent(monkeypatch):
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, config={"model": "claude-sonnet-5"}, effort="low")
    run(collect(llm.reply(prompt("hello"))))
    body = body_of(fake.posts[0])
    assert body["model"] == "claude-sonnet-5"
    assert body["output_config"] == {"effort": "low"}
    assert llm.describe().model == "claude-sonnet-5"


def test_the_openai_plugins_reasoning_effort_is_never_read(monkeypatch):
    """DL1. Both keys sit in one `params` block; each plugin reads its own."""
    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, reasoning_effort="high")
    run(collect(llm.reply(prompt("hello"))))
    body = body_of(fake.posts[0])
    assert "output_config" not in body and "reasoning_effort" not in body
    assert llm.effort is None

    fake = FakeAnthropic(message_stream("hi"))
    llm = started(fake, monkeypatch, reasoning_effort="high", effort="low")
    run(collect(llm.reply(prompt("hello"))))
    body = body_of(fake.posts[0])
    assert body["output_config"] == {"effort": "low"} and "reasoning_effort" not in body


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


# ------------------------------------------------------------ descriptor


def test_the_descriptor_names_where_the_prompt_goes_and_which_key_pays():
    llm = make(FakeAnthropic())
    d = llm.describe()
    assert (d.endpoint, d.key_env, d.host) == (DEFAULT_URL, DEFAULT_KEY_ENV, "api.anthropic.com")


def test_the_descriptor_never_carries_a_password_or_a_query_from_the_url():
    llm = make(FakeAnthropic(), config={"key_env": "MY_KEY"}, url="https://user:secret@example.com:8443/claude/?token=abc")
    d = llm.describe()
    assert d.endpoint == "https://example.com:8443/claude/"
    assert "secret" not in d.endpoint and "token" not in d.endpoint
    assert (d.host, d.key_env) == ("example.com:8443", "MY_KEY")


# ----------------------------------------------------------------- start


def test_preflight_asks_for_the_souls_model_by_id_once_and_nothing_else(monkeypatch):
    """MC1. One request at boot, as before, and now it names the model."""
    fake = FakeAnthropic()
    llm = started(fake, monkeypatch, config={"model": "claude-sonnet-5"})
    assert fake.paths == [("GET", "/v1/models/claude-sonnet-5")]
    assert fake.requests[0].headers["x-api-key"] == KEY
    assert llm.health().ok and llm.health().detail is None


def test_preflight_can_be_turned_off(monkeypatch):
    fake = FakeAnthropic()
    started(fake, monkeypatch, preflight=False)
    assert fake.requests == []


def test_a_retired_model_is_model_unavailable_with_its_date_and_sends_nothing(monkeypatch):
    fake = FakeAnthropic(
        message_stream("never"),
        model_json=model_object("claude-opus-4-1-20250805", lifecycle="retired", retires_at="2026-08-05T00:00:00Z"),
    )
    llm = booted(fake, monkeypatch, config={"model": "claude-opus-4-1-20250805"})
    health = llm.health()
    assert not llm.describe().healthy and not health.ok
    assert health.faults == ("model_unavailable",)
    assert health.detail == (
        "Anthropic retired the model 'claude-opus-4-1-20250805' on 2026-08-05. Choose another "
        "under models.chat.model; Anthropic's deprecations page names its replacement."
    )
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.stop_reason == "error" and done.error == health.detail
    assert fake.posts == [], "a model that cannot answer is sent nothing"


def test_a_retired_model_with_no_date_is_named_retired_all_the_same(monkeypatch):
    fake = FakeAnthropic(model_json=model_object(lifecycle="retired", retires_at=None))
    llm = booted(fake, monkeypatch)
    assert llm.health().faults == ("model_unavailable",)
    assert llm.health().detail.startswith("Anthropic retired the model 'claude-opus-5'. Choose another")


def test_an_unknown_id_at_anthropic_is_model_unavailable(monkeypatch):
    fake = FakeAnthropic(model_status=404)
    llm = booted(fake, monkeypatch, config={"model": "claude-opus-55"})
    health = llm.health()
    assert health.faults == ("model_unavailable",)
    assert health.detail == (
        "Anthropic does not know the model 'claude-opus-55' (HTTP 404). It is retired, "
        "misspelt, or not open to this key. Choose another under models.chat.model."
    )
    assert fake.paths == [("GET", "/v1/models/claude-opus-55")], "no fallback at Anthropic's own address"


def test_a_scheduled_retirement_is_a_healthy_notice(monkeypatch):
    fake = FakeAnthropic(model_json=model_object(lifecycle="deprecated", retires_at="2027-07-24T00:00:00Z"))
    llm = started(fake, monkeypatch)
    health = llm.health()
    assert health.ok and health.faults == ()
    assert health.detail == (
        "the language model 'claude-opus-5' retires on 2027-07-24 at Anthropic. "
        "Choose another under models.chat.model before then."
    )


def test_a_retirement_time_with_an_offset_prints_its_utc_day(monkeypatch):
    fake = FakeAnthropic(model_json=model_object(lifecycle="deprecated", retires_at="2027-07-23T20:00:00-07:00"))
    llm = started(fake, monkeypatch)
    assert "retires on 2027-07-24 at Anthropic" in (llm.health().detail or "")


def test_a_retirement_time_fromisoformat_refuses_still_gives_its_leading_day(monkeypatch):
    """`_day`'s fallback, review finding providers/tests#8."""
    fake = FakeAnthropic(model_json=model_object(lifecycle="deprecated", retires_at="2027-07-24 00:00 UTC"))
    llm = started(fake, monkeypatch)
    assert "retires on 2027-07-24 at Anthropic" in (llm.health().detail or "")


@pytest.mark.parametrize("status", [404, 405, 400, 500])
def test_another_server_that_serves_no_per_model_path_falls_back_to_the_list(monkeypatch, status):
    """Review findings providers/code#2 and providers/tests#1: a proxy that
    lists models booted at 8de8152 whatever its per-model path answers, as
    it does behind the OpenAI plugin."""
    fake = FakeAnthropic(model_status=status)
    llm = started(fake, monkeypatch, url="http://127.0.0.1:9380")
    assert fake.paths == [("GET", "/v1/models/claude-opus-5"), ("GET", "/v1/models")]
    assert llm.health().ok and llm.health().detail is None


def test_a_server_error_on_the_per_model_path_at_anthropic_refuses_without_the_list(monkeypatch):
    llm = booted(FakeAnthropic(model_status=500), monkeypatch)
    assert llm.health().faults == ("start_failed",)
    assert llm.health().detail == "Anthropic refused the connection: HTTP 500 api_error: Internal server error"


def test_another_server_names_itself_and_leaves_out_anthropics_page(monkeypatch):
    fake = FakeAnthropic(model_json=model_object(lifecycle="retired", retires_at="2026-08-05T00:00:00Z"))
    llm = booted(fake, monkeypatch, url="https://proxy.example/claude")
    assert llm.health().faults == ("model_unavailable",)
    assert llm.health().detail == (
        "the server at proxy.example retired the model 'claude-opus-5' on 2026-08-05. "
        "Choose another under models.chat.model."
    )


def test_another_server_whose_list_rejects_the_key_says_so(monkeypatch):
    fake = FakeAnthropic(model_status=404, list_status=401)
    llm = booted(fake, monkeypatch, url="https://proxy.example/claude")
    assert llm.health().faults == ("start_failed",)
    assert llm.health().detail == "the server at proxy.example rejected the key in EMET_ANTHROPIC_KEY (HTTP 401). Check the key."


def test_no_key_is_unhealthy_names_the_variable_and_sends_nothing(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    fake = FakeAnthropic()
    llm = make(fake)
    run(llm.start())
    assert not llm.describe().healthy
    assert DEFAULT_KEY_ENV in (llm.health().detail or "")
    assert llm.health().faults == ("no_key",)
    assert fake.requests == []


def test_no_key_at_another_server_names_that_server_and_not_anthropics_console(monkeypatch):
    """Review finding providers/claims-scope#6, C7's rule for the no-key sentence."""
    monkeypatch.delenv("EMET_LOCAL_KEY", raising=False)
    llm = make(FakeAnthropic(), config={"key_env": "EMET_LOCAL_KEY"}, url="http://127.0.0.1:9380")
    run(llm.start())
    assert llm.health().detail == (
        "no key: the environment variable EMET_LOCAL_KEY is not set. Set it to a key the server at "
        "127.0.0.1:9380 accepts, or to any value if it checks none; export it in the shell that runs "
        "the robot. The key is never written into a soul or a manifest."
    )


def test_the_souls_key_env_is_honoured(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    monkeypatch.setenv("MY_ANTHROPIC", KEY)
    fake = FakeAnthropic()
    llm = make(fake, config={"key_env": "MY_ANTHROPIC"})
    run(llm.start())
    assert llm.describe().healthy
    assert llm.describe().key_env == "MY_ANTHROPIC"
    assert fake.requests[0].headers["x-api-key"] == KEY


def test_a_rejected_key_is_unhealthy_with_the_status(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, "wrong")
    llm = make(FakeAnthropic(models_status=401))
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert llm.health().faults == ("start_failed",)
    assert detail == "Anthropic rejected the key in EMET_ANTHROPIC_KEY (HTTP 401). Check the key."


def test_an_unpaid_account_at_boot_is_out_of_credit(monkeypatch):
    llm = booted(FakeAnthropic(model_status=402), monkeypatch)
    assert llm.health().faults == ("out_of_credit",)
    assert llm.health().detail == (
        "Anthropic says the account is out of credit (HTTP 402, billing_error). Add credit "
        "in the Claude Console's billing settings. Retrying will not help."
    )


def test_an_unreachable_network_is_unhealthy_and_says_so(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(FakeAnthropic(raise_on_connect=httpx.ConnectError("name resolution failed")))
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert llm.health().faults == ("start_failed",)
    assert detail == (
        "could not reach https://api.anthropic.com: ConnectError: name resolution failed. "
        "The language model needs a network; the wake word does not."
    )


@pytest.mark.parametrize(
    ("url", "start"),
    [
        ("http://127.0.0.1:9380", "Nothing answered at http://127.0.0.1:9380 (ConnectError: refused). That address is this machine: start the model's server, or correct models.chat.params.url. emet-providers' README has a recipe for each local server."),
        ("http://localhost:11434", "Nothing answered at http://localhost:11434 (ConnectError: refused). That address is this machine:"),
        ("http://192.168.1.20:8080", "Nothing answered at http://192.168.1.20:8080 (ConnectError: refused). That address is on the local network: start the model's server, or correct models.chat.params.url."),
        ("http://emet-box.local:8080", "Nothing answered at http://emet-box.local:8080 (ConnectError: refused). That address is on the local network:"),
    ],
)
def test_a_local_server_that_does_not_answer_is_named_by_where_it_is(monkeypatch, url, start):
    """The seventh list's line (C8), through `_http.place_of`."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(FakeAnthropic(raise_on_connect=httpx.ConnectError("refused")), url=url)
    run(llm.start())
    assert (llm.health().detail or "").startswith(start)
    assert llm.health().faults == ("start_failed",)


def test_without_httpx_the_hint_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    monkeypatch.setitem(sys.modules, "httpx", None)
    llm = AnthropicLanguageModel({"provider": "anthropic"})
    run(llm.start())
    assert not llm.describe().healthy
    assert "emet-providers[anthropic]" in (llm.health().detail or "")
    assert llm.health().faults == ("no_library",)


def test_a_start_failure_is_logged_at_info_so_the_engine_prints_it_once(monkeypatch, caplog):
    """L16. The engine prints `health().detail`; logging's last resort prints WARNING and up."""
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    caplog.set_level(logging.DEBUG, logger="emet_providers.anthropic")
    run(make(FakeAnthropic()).start())
    assert caplog.records and all(r.levelno <= logging.INFO for r in caplog.records)


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


def test_the_last_message_delta_holds_the_counts_and_cached_tokens_count_as_input(monkeypatch):
    """OC1. `message_delta`'s usage is cumulative (Anthropic's streaming page, 2026-11-27)."""
    events_bytes = sse(
        ("message_start", {"type": "message_start", "message": {"model": "claude-opus-5", "usage": {"input_tokens": 100, "cache_read_input_tokens": 0, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"input_tokens": 120, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 50, "output_tokens": 9}}),
        ("message_stop", {"type": "message_stop"}),
    )
    llm = started(FakeAnthropic(events_bytes), monkeypatch)
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert (done.input_tokens, done.output_tokens) == (200, 9)


def test_a_reply_that_counted_nothing_reports_no_counts(monkeypatch):
    events_bytes = sse(
        ("message_start", {"type": "message_start", "message": {"model": "m"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        ("message_stop", {"type": "message_stop"}),
    )
    llm = started(FakeAnthropic(events_bytes), monkeypatch)
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert (done.input_tokens, done.output_tokens) == (None, None)


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


def test_a_reply_that_spent_its_cap_thinking_ends_empty_at_length(monkeypatch):
    """DL1. A reply that spent its cap thinking ends with no text and the stop reason `length`."""
    events_bytes = sse(
        ("message_start", {"type": "message_start", "message": {"model": "claude-opus-5", "usage": {"input_tokens": 40}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ""}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 300}}),
        ("message_stop", {"type": "message_stop"}),
    )
    llm = started(FakeAnthropic(events_bytes), monkeypatch, effort="high")
    events = run(collect(llm.reply(prompt("why is the sky blue"))))
    (done,) = events
    assert (done.text, done.stop_reason, done.output_tokens, done.error) == ("", "length", 300, None)


def test_an_http_error_ends_the_reply_with_the_vendors_message(monkeypatch):
    fake = FakeAnthropic(reply_status=429, reply_json=error_body("rate_limit_error", "slow down"))
    llm = started(fake, monkeypatch)
    events = run(collect(llm.reply(prompt("q"))))
    (done,) = events
    assert done.stop_reason == "error"
    assert "429" in (done.error or "") and "slow down" in (done.error or "")
    assert llm.last_error == done.error
    assert llm.describe().healthy, "a failed reply is not a broken plugin"
    assert llm.health().ok, "a rate limit names no kind: a later turn can pass"


def test_a_failed_reply_is_logged_at_info(monkeypatch, caplog):
    llm = started(FakeAnthropic(reply_status=500), monkeypatch)
    caplog.set_level(logging.DEBUG, logger="emet_providers.anthropic")
    run(collect(llm.reply(prompt("q"))))
    assert caplog.records and all(r.levelno <= logging.INFO for r in caplog.records)


def test_an_unpaid_account_is_out_of_credit_until_a_reply_succeeds(monkeypatch):
    """OC3. A 402 `billing_error`, in the body Anthropic's errors page gives."""
    fake = FakeAnthropic(
        message_stream("back"),
        reply_statuses=(402, 200, 402, 500),
        reply_json=error_body("billing_error", "Your credit balance is too low."),
    )
    llm = started(fake, monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    detail = (
        "Anthropic says the account is out of credit (HTTP 402, billing_error). Add credit "
        "in the Claude Console's billing settings. Retrying will not help."
    )
    assert (done.stop_reason, done.error) == ("error", detail)
    assert (llm.health().ok, llm.health().faults, llm.health().detail) == (False, ("out_of_credit",), detail)
    assert llm.describe().healthy, "the plugin still runs; the account is what failed"

    again = run(collect(llm.reply(prompt("q"))))[-1]
    assert again.stop_reason == "end" and again.text == "back"
    assert llm.health().ok and llm.health().faults == ()

    # Review finding providers/code#1 (contract 0.5.7 section 9, C15): a
    # failure that names no kind is news of the network or the server, so it
    # ends the kind, and the turn takes the failed row and its signal.
    run(collect(llm.reply(prompt("q"))))
    assert llm.health().faults == ("out_of_credit",)
    fake.reply_json = None
    (other,) = run(collect(llm.reply(prompt("q"))))
    assert other.error == "HTTP 500 api_error: boom"
    assert llm.health().ok and llm.health().faults == ()


@pytest.mark.parametrize("code", ["enforced_spend_limit_reached", "contract_spend_limit_reached"])
def test_a_tier_spend_cap_is_out_of_credit_by_its_code(monkeypatch, code):
    """A 429 whose `error.details.error_code` names a spend cap (errors page,
    2026-11-27); both documented codes, review finding providers/tests#8."""
    fake = FakeAnthropic(
        reply_status=429,
        reply_json=error_body("rate_limit_error", "You have reached your API usage limits.", code),
    )
    llm = started(fake, monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert llm.health().faults == ("out_of_credit",)
    assert done.error == (
        f"Anthropic says the account is out of credit (HTTP 429, {code}). "
        "Add credit in the Claude Console's billing settings. Retrying will not help."
    )


def test_another_server_out_of_credit_leaves_out_the_console(monkeypatch):
    fake = FakeAnthropic(reply_status=402, reply_json=error_body("billing_error", "pay"))
    llm = started(fake, monkeypatch, url="https://proxy.example/claude")
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == "the server at proxy.example says the account is out of credit (HTTP 402, billing_error). Retrying will not help."


def test_the_fault_table_keys_billing_by_status_and_code_and_names_no_model():
    assert FAULT_CODES[(402, "billing_error")] == "out_of_credit"
    assert FAULT_CODES[(429, "enforced_spend_limit_reached")] == "out_of_credit"
    assert (429, None) not in FAULT_CODES and (429, "rate_limit_error") not in FAULT_CODES
    assert "model_unavailable" not in FAULT_CODES.values(), "no status or type names a model at Anthropic"


def test_a_model_retired_mid_run_is_model_unavailable_for_the_rest_of_the_run(monkeypatch):
    """A 404 reply, then the per-model path says the model is retired."""
    fake = FakeAnthropic(message_stream("never"), reply_status=404, reply_json=error_body("not_found_error", "model: claude-opus-5"))
    llm = started(fake, monkeypatch)
    fake.model_json = model_object(lifecycle="retired", retires_at="2026-11-27T00:00:00Z")
    (done,) = run(collect(llm.reply(prompt("q"))))
    detail = (
        "Anthropic retired the model 'claude-opus-5' on 2026-11-27. Choose another under "
        "models.chat.model; Anthropic's deprecations page names its replacement."
    )
    assert done.error == detail
    assert llm.health().faults == ("model_unavailable",) and llm.health().detail == detail
    assert fake.paths[-2:] == [("POST", "/v1/messages"), ("GET", "/v1/models/claude-opus-5")]

    posts = len(fake.posts)
    (later,) = run(collect(llm.reply(prompt("q"))))
    assert later.error == detail and len(fake.posts) == posts, "nothing more is sent"


def test_a_404_reply_for_a_model_still_active_stays_an_ordinary_failure(monkeypatch):
    fake = FakeAnthropic(reply_status=404, reply_json=error_body("not_found_error", "not here"))
    llm = started(fake, monkeypatch, url="https://proxy.example/claude")
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == "HTTP 404 not_found_error: not here"
    assert llm.health().ok


def test_a_model_anthropic_no_longer_knows_mid_run_is_model_unavailable_and_sent_nothing_more(monkeypatch):
    """Review finding providers/tests#2 (a): a 404 reply, then the per-model
    path answers 404 at Anthropic's own address, the "unknown" branch."""
    fake = FakeAnthropic(message_stream("never"), reply_status=404, reply_json=error_body("not_found_error", "model: claude-opus-5"))
    llm = started(fake, monkeypatch)
    fake.model_status = 404
    (done,) = run(collect(llm.reply(prompt("q"))))
    detail = (
        "Anthropic does not know the model 'claude-opus-5' (HTTP 404). It is retired, "
        "misspelt, or not open to this key. Choose another under models.chat.model."
    )
    assert done.error == detail
    assert llm.health().faults == ("model_unavailable",) and llm.health().detail == detail
    posts = len(fake.posts)
    (later,) = run(collect(llm.reply(prompt("q"))))
    assert later.error == detail and len(fake.posts) == posts, "nothing more is sent"


def test_a_404_reply_at_anthropic_for_a_model_still_active_stays_an_ordinary_failure(monkeypatch):
    """Review finding providers/tests#2 (b): C4, no `model_unavailable` from a
    bare status, at Anthropic's own address, where the per-model path is read."""
    fake = FakeAnthropic(message_stream("back"), reply_statuses=(404, 200), reply_json=error_body("not_found_error", "not here"))
    llm = started(fake, monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == "HTTP 404 not_found_error: not here"
    assert fake.paths[-2:] == [("POST", "/v1/messages"), ("GET", "/v1/models/claude-opus-5")]
    assert llm.health().ok and llm.health().faults == ()
    again = run(collect(llm.reply(prompt("q"))))[-1]
    assert again.text == "back" and len(fake.posts) == 2, "the next reply goes out"


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
    assert done.input_tokens == 3
    assert llm.health().ok


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


def test_a_url_that_cannot_be_read_is_refused_at_start_in_one_line_with_no_password(monkeypatch):
    """Review finding sdk/code#1, the providers' half: an open bracket raised
    ValueError from the plugin's constructor, before any refusal could be
    written. Now the plugin is built and refuses at `start()`, naming the
    field, with nothing sent."""
    fake = FakeAnthropic()
    llm = booted(fake, monkeypatch, url="http://me:hunter2@[::1/v1")
    health = llm.health()
    assert not health.ok and health.faults == ("start_failed",)
    assert health.detail.startswith("models.chat.params.url cannot be read as a URL (")
    assert "http://[::1/v1" in health.detail and "hunter2" not in health.detail
    assert "hunter2" not in (llm.describe().endpoint or "")
    assert len(fake.requests) == 0, "nothing is sent for a URL that cannot be read"
