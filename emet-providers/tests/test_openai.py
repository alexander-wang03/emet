"""The OpenAI language model, against a stand-in for Chat Completions.

No network and no key. An `httpx.MockTransport` answers in the shapes
OpenAI's streaming reference gives (verified 2026-09-15) and records what it
was sent. The same file stands for every server that speaks this protocol,
which is the reason the plugin speaks it.

The model object, with its `shutdown_date`, is the API reference's for
`GET /models/{model}`, and the billing bodies are the codes OpenAI's
error-codes guide names (both read 2026-11-27). The guide gives no example
body, so each body here carries the documented code and type in the
`{"error": {...}}` shape the plugin already reads. The 404 body for an
unknown model is the one OpenAI's developer forum shows, unverified against
OpenAI's documentation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest

httpx = pytest.importorskip("httpx", reason="emet-providers[openai] is not installed")

from emet_sdk.models import model_selection  # noqa: E402
from emet_sdk.plugin import LanguageModelPlugin  # noqa: E402
from emet_sdk.validate import load_yaml  # noqa: E402
from emet_sdk.types import FAULT_KINDS, Message, Prompt, ReplyDone, TextDelta, ToolCall, ToolSpec  # noqa: E402

from emet_providers import openai as openai_plugin  # noqa: E402
from emet_providers.openai import (  # noqa: E402
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_URL,
    FAULT_CODES,
    WARM_UP_MESSAGE,
    OpenAILanguageModel,
)

KEY = "sk-test"

#: The day the tests pretend it is, so a shutdown date is past or to come.
TODAY = date(2026, 11, 27)

LOCAL = "http://127.0.0.1:9380/v1"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(openai_plugin, "utc_today", lambda: TODAY)


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


def billing(code: str | None, kind: str = "insufficient_quota") -> dict:
    """A billing error in the documented shape, whose code names the cause while the type may stay insufficient_quota."""
    return {"error": {"message": "You exceeded your current quota.", "type": kind, "param": None, "code": code}}


UNKNOWN_MODEL = {
    "error": {
        "message": "The model `gpt-5.6-tera` does not exist or you do not have access to it.",
        "type": "invalid_request_error",
        "param": None,
        "code": "model_not_found",
    }
}


class FakeOpenAI:
    """Chat Completions and the Models API, answering as OpenAI documents them.

    `known` are the ids `GET /models/{id}` describes, each with
    `shutdown_date`; any other id is a 404 naming `model_not_found`.
    `per_model=False` answers that path with a bare 404, as a server that
    serves only the list does. `models_status` other than 200 answers both
    GETs with it, as a rejected key does everywhere.
    """

    def __init__(
        self,
        body: bytes = b"",
        *,
        models_status: int = 200,
        model_status: int | None = None,
        model_json: dict | None = None,
        known: tuple[str, ...] = ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-4-0314", "qwen3"),
        shutdown_date: Any = None,
        per_model: bool = True,
        reply_status: int = 200,
        reply_json: dict | None = None,
        warm_status: int = 200,
        raise_on_connect: Exception | None = None,
        raise_on_post: Exception | None = None,
    ) -> None:
        self.body = body
        self.models_status = models_status
        self.model_status = model_status
        self.model_json = model_json
        self.known = known
        self.shutdown_date = shutdown_date
        self.per_model = per_model
        self.reply_status = reply_status
        self.reply_json = reply_json
        self.warm_status = warm_status
        self.raise_on_connect = raise_on_connect
        self.raise_on_post = raise_on_post
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        path = request.url.path
        if request.method == "GET" and self.models_status != 200 and "/models" in path:
            return httpx.Response(self.models_status, json={"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}})
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in self.known]})
        if request.method == "GET" and "/models/" in path:
            model = path.rsplit("/models/", 1)[1]
            if self.model_status is not None:
                return httpx.Response(self.model_status, json=self.model_json or {"error": {"message": "nope", "type": "server_error"}})
            if not self.per_model:
                return httpx.Response(404, text="404 page not found")
            if model in self.known:
                return httpx.Response(200, json={"id": model, "object": "model", "created": 1686935002, "owned_by": "openai", "shutdown_date": self.shutdown_date})
            return httpx.Response(404, json=UNKNOWN_MODEL)
        if request.method == "POST" and path.endswith("/chat/completions"):
            if self.raise_on_post is not None:
                raise self.raise_on_post
            if json.loads(request.content).get("stream") is False:
                if self.warm_status != 200:
                    return httpx.Response(self.warm_status, json={"error": {"message": "warming failed", "type": "server_error"}})
                return httpx.Response(200, json={"id": "chatcmpl-w", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "H"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 8, "completion_tokens": 1}})
            if self.reply_status != 200:
                return httpx.Response(self.reply_status, json=self.reply_json or {"error": {"message": "boom", "type": "server_error"}})
            return httpx.Response(200, content=self.body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"error": {"message": request.url.path, "type": "not_found"}})

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


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


def booted(fake: FakeOpenAI, monkeypatch, **kw) -> OpenAILanguageModel:
    """Started with a key, healthy or not."""
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    llm = make(fake, **kw)
    run(llm.start())
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
    assert "reasoning_effort" not in body, "unset, the server's own default runs"


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


# -------------------------------------------------- DL1: reasoning effort


def test_reasoning_effort_is_sent_as_the_body_names_it(monkeypatch):
    fake = FakeOpenAI(reply_stream("hi"))
    llm = started(fake, monkeypatch, reasoning_effort="low")
    run(collect(llm.reply(prompt("hello"))))
    assert body_of(fake.posts[0])["reasoning_effort"] == "low"


def test_anthropics_effort_is_left_to_anthropic(monkeypatch):
    """The reference manifest names both plugins' keys, and each reads only its own."""
    fake = FakeOpenAI(reply_stream("hi"))
    llm = started(fake, monkeypatch, reasoning_effort="low", effort="high")
    run(collect(llm.reply(prompt("hello"))))
    body = body_of(fake.posts[0])
    assert body["reasoning_effort"] == "low" and "effort" not in body and "output_config" not in body

    fake = FakeOpenAI(reply_stream("hi"))
    llm = started(fake, monkeypatch, effort="low")
    run(collect(llm.reply(prompt("hello"))))
    body = body_of(fake.posts[0])
    assert "effort" not in body and "reasoning_effort" not in body


def test_the_reference_body_sends_low_effort_to_the_reference_souls_model(monkeypatch):
    """The Pi's manifest tunes the soul's chat with params alone, and the plugin the soul names reads its own key (L35)."""
    examples = Path(__file__).resolve().parents[2] / "emet-sdk" / "examples"
    body, soul = load_yaml(examples / "pi-speakerphone.yaml"), load_yaml(examples / "emet-soul.yaml")
    chosen = model_selection(body, soul, "chat")
    assert chosen is not None and chosen["provider"] == "openai"
    assert chosen["params"] == {"reasoning_effort": "low", "effort": "low"}
    monkeypatch.setenv(chosen["key_env"], KEY)
    fake = FakeOpenAI(reply_stream("hi"))
    llm = OpenAILanguageModel(chosen, transport=httpx.MockTransport(fake))
    run(llm.start())
    run(collect(llm.reply(prompt("hello"))))
    sent = body_of(fake.posts[0])
    assert sent["model"] == chosen["model"] and sent["reasoning_effort"] == "low" and "effort" not in sent


def test_a_reasoning_effort_that_is_not_a_word_refuses_the_start_and_sends_nothing(monkeypatch):
    fake = FakeOpenAI()
    llm = booted(fake, monkeypatch, reasoning_effort=False)
    health = llm.health()
    assert not llm.describe().healthy and health.faults == ("start_failed",)
    assert health.detail == (
        "models.chat.params.reasoning_effort is a word such as low or none, and it was given False (bool). "
        "Quote it in the manifest."
    )
    assert fake.requests == []


def test_a_reply_that_spends_the_cap_reasoning_stops_at_length_with_no_words_and_its_tokens(monkeypatch):
    """DL1's empty reply carries no error, so the engine says the `failed` line for the stop and names no fault."""
    body = stream(
        chunk({"role": "assistant", "content": ""}),
        chunk({}, finish="length"),
        chunk(usage={"prompt_tokens": 967, "completion_tokens": 300, "completion_tokens_details": {"reasoning_tokens": 300}}),
    )
    llm = started(FakeOpenAI(body), monkeypatch, reasoning_effort="medium")
    events = run(collect(llm.reply(prompt("why is the sky blue but the sunset red"))))
    assert not [e for e in events if isinstance(e, TextDelta)]
    (done,) = events
    assert (done.text, done.stop_reason, done.error) == ("", "length", None)
    assert (done.input_tokens, done.output_tokens) == (967, 300)
    assert llm.health().ok and llm.describe().healthy


# ----------------------------------------------------------- DL5: warm-up


def test_warm_up_off_by_default_sends_no_chat_request_at_start(monkeypatch):
    fake = FakeOpenAI()
    llm = started(fake, monkeypatch)
    assert fake.posts == [] and llm.warmed_s is None


def test_warm_up_sends_one_capped_request_after_the_preflight(monkeypatch):
    fake = FakeOpenAI()
    llm = started(fake, monkeypatch, url=LOCAL, warm_up=True, max_tokens_field="max_tokens", reasoning_effort="none")
    assert fake.calls == [("GET", "/v1/models/gpt-5.6-terra"), ("POST", "/v1/chat/completions")]
    assert body_of(fake.posts[0]) == {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": WARM_UP_MESSAGE}],
        "stream": False,
        "max_tokens": 1,
        "reasoning_effort": "none",
    }
    assert llm.warmed_s is not None and llm.health().ok


def test_warm_up_runs_with_the_preflight_off(monkeypatch):
    fake = FakeOpenAI()
    started(fake, monkeypatch, url=LOCAL, warm_up=True, preflight=False)
    assert fake.calls == [("POST", "/v1/chat/completions")]
    assert body_of(fake.posts[0])["max_completion_tokens"] == 1


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
def test_a_warm_up_that_fails_is_logged_at_info_and_leaves_the_plugin_healthy(monkeypatch, caplog, failure):
    fake = FakeOpenAI(warm_status=503) if failure == "refused" else FakeOpenAI(raise_on_post=httpx.ReadTimeout("loading"))
    with caplog.at_level(logging.DEBUG, logger="emet_providers.openai"):
        llm = started(fake, monkeypatch, url=LOCAL, warm_up=True)
    assert len(fake.posts) == 1 and llm.warmed_s is None
    assert llm.health().ok and llm.health().detail is None
    warm = [r for r in caplog.records if "warm-up" in r.getMessage()]
    assert warm and all(r.levelno == logging.INFO for r in warm)


@pytest.mark.parametrize("fake", [FakeOpenAI(models_status=401), FakeOpenAI(shutdown_date="2026-03-26")], ids=["rejected", "retired"])
def test_no_warm_up_after_a_preflight_that_did_not_pass(monkeypatch, fake):
    booted(fake, monkeypatch, warm_up=True)
    assert fake.posts == []


def test_warm_up_must_be_true_or_false(monkeypatch):
    fake = FakeOpenAI()
    llm = booted(fake, monkeypatch, warm_up="yes")
    assert not llm.describe().healthy and fake.requests == []
    assert llm.health().detail == "models.chat.params.warm_up is true or false, and it was given 'yes' (str)."


# ----------------------------------------------------------------- start


def test_preflight_asks_for_the_souls_model_by_id(monkeypatch):
    fake = FakeOpenAI()
    started(fake, monkeypatch)
    assert fake.calls == [("GET", "/v1/models/gpt-5.6-terra")]
    assert fake.requests[0].headers["authorization"] == f"Bearer {KEY}"

    fake = FakeOpenAI()
    started(fake, monkeypatch, config={"model": "gpt-5.6-luna"})
    assert fake.calls == [("GET", "/v1/models/gpt-5.6-luna")]


def test_no_key_is_unhealthy_names_the_variable_and_sends_nothing(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    fake = FakeOpenAI()
    llm = make(fake)
    run(llm.start())
    assert not llm.describe().healthy
    assert DEFAULT_KEY_ENV in (llm.health().detail or "")
    assert llm.health().faults == ("no_key",)
    assert fake.requests == []


def test_no_key_at_a_local_server_names_that_server_and_not_openais_console(monkeypatch):
    """Review finding providers/claims-scope#6: the README's recipe with its
    key line forgotten, C7's rule for the no-key sentence."""
    monkeypatch.delenv("EMET_LOCAL_KEY", raising=False)
    llm = make(FakeOpenAI(), config={"key_env": "EMET_LOCAL_KEY"}, url="http://localhost:11434/v1")
    run(llm.start())
    assert llm.health().detail == (
        "no key: the environment variable EMET_LOCAL_KEY is not set. Set it to a key the server at "
        "localhost:11434 accepts, or to any value if it checks none; export it in the shell that runs "
        "the robot. The key is never written into a soul or a manifest."
    )


def test_a_rejected_key_is_unhealthy_with_the_status(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, "wrong")
    llm = make(FakeOpenAI(models_status=401))
    run(llm.start())
    detail = llm.health().detail or ""
    assert not llm.describe().healthy
    assert "401" in detail and DEFAULT_KEY_ENV in detail and "rejected" in detail
    assert llm.health().faults == ("start_failed",)


def test_without_httpx_the_hint_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    monkeypatch.setitem(sys.modules, "httpx", None)
    llm = OpenAILanguageModel({"provider": "openai"})
    run(llm.start())
    assert not llm.describe().healthy
    assert "emet-providers[openai]" in (llm.health().detail or "")
    assert llm.health().faults == ("no_library",)


def test_a_start_refusal_is_logged_at_info_since_the_engine_prints_it(monkeypatch, caplog):
    """Logging's last resort prints WARNING and up, so a WARNING here printed the refusal twice (L16)."""
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    with caplog.at_level(logging.DEBUG, logger="emet_providers.openai"):
        run(make(FakeOpenAI()).start())
    assert caplog.records and all(r.levelno < logging.WARNING for r in caplog.records)


# ----------------------------------- the seventh list: a server that is down


@pytest.mark.parametrize(
    ("url", "said"),
    [
        (
            "http://127.0.0.1:9380/v1",
            "Nothing answered at http://127.0.0.1:9380/v1 (ConnectError: refused). That address is this machine: "
            "start the model's server, or correct models.chat.params.url. emet-providers' README has a recipe "
            "for each local server.",
        ),
        (
            "http://localhost:11434/v1",
            "Nothing answered at http://localhost:11434/v1 (ConnectError: refused). That address is this machine: "
            "start the model's server, or correct models.chat.params.url. emet-providers' README has a recipe "
            "for each local server.",
        ),
        (
            "http://[::1]:8080/v1",
            "Nothing answered at http://[::1]:8080/v1 (ConnectError: refused). That address is this machine: "
            "start the model's server, or correct models.chat.params.url. emet-providers' README has a recipe "
            "for each local server.",
        ),
        (
            "http://192.168.1.20:11434/v1",
            "Nothing answered at http://192.168.1.20:11434/v1 (ConnectError: refused). That address is on the local "
            "network: start the model's server, or correct models.chat.params.url.",
        ),
        (
            "http://emet-pi.local:8080/v1",
            "Nothing answered at http://emet-pi.local:8080/v1 (ConnectError: refused). That address is on the local "
            "network: start the model's server, or correct models.chat.params.url.",
        ),
        (
            DEFAULT_URL,
            "could not reach https://api.openai.com/v1: ConnectError: refused. The language model needs a network; "
            "the wake word does not.",
        ),
        (
            "https://openrouter.ai/api/v1",
            "could not reach https://openrouter.ai/api/v1: ConnectError: refused. The language model needs a network; "
            "the wake word does not.",
        ),
    ],
    ids=["loopback", "localhost", "ipv6-loopback", "private", "dot-local", "openai", "cloud-gateway"],
)
def test_a_server_that_does_not_answer_is_described_where_it_runs(monkeypatch, url, said):
    llm = booted(FakeOpenAI(raise_on_connect=httpx.ConnectError("refused")), monkeypatch, url=url)
    health = llm.health()
    assert not llm.describe().healthy
    assert health.detail == said and health.faults == ("start_failed",)


def test_a_password_in_the_url_reaches_no_sentence_and_no_descriptor(monkeypatch):
    llm = booted(FakeOpenAI(raise_on_connect=httpx.ConnectError("refused")), monkeypatch, url="http://me:hunter2@127.0.0.1:9380/v1?k=hunter2")
    described = llm.describe()
    assert described.endpoint == "http://127.0.0.1:9380/v1" and described.host == "127.0.0.1:9380"
    assert "hunter2" not in (llm.health().detail or "")


def test_a_password_in_a_url_with_no_scheme_reaches_no_sentence_and_no_descriptor(monkeypatch):
    """Review findings providers/tests#5 and providers/code#4: `urlsplit`
    reads `me` as the scheme and the rest as a path, and the refusal names
    the endpoint."""
    llm = booted(FakeOpenAI(raise_on_connect=httpx.ConnectError("refused")), monkeypatch, url="me:hunter2@127.0.0.1:9380/v1")
    described = llm.describe()
    assert described.endpoint == "127.0.0.1:9380/v1" and described.host == "127.0.0.1:9380"
    assert llm.health().faults == ("start_failed",)
    assert (llm.health().detail or "").startswith("Nothing answered at 127.0.0.1:9380/v1 (")
    assert "hunter2" not in (llm.health().detail or "")


def test_the_descriptor_names_where_the_prompt_goes_and_which_key_pays(monkeypatch):
    llm = started(FakeOpenAI(), monkeypatch)
    described = llm.describe()
    assert (described.endpoint, described.key_env, described.host) == (DEFAULT_URL, DEFAULT_KEY_ENV, "api.openai.com")
    monkeypatch.setenv("EMET_LOCAL_KEY", KEY)
    llm = started(FakeOpenAI(), monkeypatch, config={"key_env": "EMET_LOCAL_KEY"}, url=LOCAL + "/")
    described = llm.describe()
    assert (described.endpoint, described.key_env, described.host) == (LOCAL, "EMET_LOCAL_KEY", "127.0.0.1:9380")


# ------------------------------------------------- MC1: the model's lifecycle


def test_a_model_with_no_shutdown_date_is_healthy_with_nothing_to_say(monkeypatch):
    llm = started(FakeOpenAI(shutdown_date=None), monkeypatch)
    assert llm.health().ok and llm.health().detail is None


@pytest.mark.parametrize("given", ["2027-03-01", 1803902400], ids=["date", "unix-seconds"])
def test_a_shutdown_date_to_come_is_a_notice_and_the_model_stays_healthy(monkeypatch, given):
    llm = started(FakeOpenAI(shutdown_date=given), monkeypatch)
    health = llm.health()
    assert health.ok and health.faults == ()
    assert health.detail == (
        "the language model 'gpt-5.6-terra' retires on 2027-03-01 at OpenAI. Choose another under models.chat.model before then."
    )


def test_the_shutdown_day_itself_is_still_a_notice(monkeypatch):
    llm = started(FakeOpenAI(shutdown_date=TODAY.isoformat()), monkeypatch)
    assert llm.health().ok and "retires on 2026-11-27" in (llm.health().detail or "")


@pytest.mark.parametrize(
    "given", ["soon", "2026-13-40", True, False, 1e20, {}], ids=["word", "no-such-day", "true", "false", "overflow", "object"]
)
def test_a_shutdown_date_that_cannot_be_read_is_no_date(monkeypatch, given):
    """Review finding providers/tests#4: a local server's odd value boots
    healthy with nothing to say, and never raises out of `start()`, which
    the session does not catch. A bool is no Unix time."""
    llm = started(FakeOpenAI(shutdown_date=given), monkeypatch, url=LOCAL, config={"model": "qwen3"})
    assert llm.health().ok and llm.health().detail is None and llm.health().faults == ()


def test_a_past_shutdown_date_is_a_retired_model_that_boots_and_is_sent_nothing(monkeypatch):
    fake = FakeOpenAI(reply_stream("hi"), shutdown_date="2026-03-26")
    llm = booted(fake, monkeypatch, config={"model": "gpt-4-0314"})
    health = llm.health()
    said = (
        "OpenAI retired the model 'gpt-4-0314' on 2026-03-26. Choose another under models.chat.model; "
        "OpenAI's deprecations page names its replacement."
    )
    assert not llm.describe().healthy
    assert (health.ok, health.detail, health.faults) == (False, said, ("model_unavailable",))
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert (done.stop_reason, done.error) == ("error", said)
    assert fake.posts == [], "nothing is sent for a model the vendor has retired"


def test_an_id_openai_does_not_know_is_model_unavailable_at_boot(monkeypatch):
    fake = FakeOpenAI()
    llm = booted(fake, monkeypatch, config={"model": "gpt-5.6-tera"})
    health = llm.health()
    assert fake.calls == [("GET", "/v1/models/gpt-5.6-tera")]
    assert health.faults == ("model_unavailable",)
    assert health.detail == (
        "OpenAI does not know the model 'gpt-5.6-tera' (HTTP 404). It is retired, misspelt, or not open to this key. "
        "Choose another under models.chat.model."
    )


@pytest.mark.parametrize("answer", [{"per_model": False}, {"model_status": 405}, {"model_status": 500}], ids=["404", "405", "500"])
def test_another_server_that_does_not_serve_one_model_falls_back_to_the_list(monkeypatch, answer):
    fake = FakeOpenAI(**answer)
    llm = started(fake, monkeypatch, url=LOCAL, config={"model": "qwen3"})
    assert fake.calls == [("GET", "/v1/models/qwen3"), ("GET", "/v1/models")]
    assert llm.health().ok


def test_a_server_error_on_the_per_model_path_at_openai_refuses_without_the_list(monkeypatch):
    """At OpenAI's own URL a server error refuses with no fallback, review finding providers/tests#7."""
    fake = FakeOpenAI(model_status=500)
    llm = booted(fake, monkeypatch)
    assert fake.calls == [("GET", "/v1/models/gpt-5.6-terra")]
    assert llm.health().faults == ("start_failed",)
    assert llm.health().detail == "the server at https://api.openai.com/v1 refused the connection: HTTP 500 server_error: nope"


def test_another_servers_list_still_refuses_as_it_did(monkeypatch):
    class Gone(FakeOpenAI):
        def __call__(self, request):
            if request.url.path.endswith("/models"):
                self.requests.append(request)
                return httpx.Response(502, json={"error": {"message": "bad gateway", "type": "server_error"}})
            return super().__call__(request)

    llm = booted(Gone(per_model=False), monkeypatch, url=LOCAL)
    assert llm.health().detail == "the server at http://127.0.0.1:9380/v1 refused the connection: HTTP 502 server_error: bad gateway"
    assert llm.health().faults == ("start_failed",)


def test_another_servers_dates_name_the_server_and_no_vendor_page(monkeypatch):
    llm = booted(FakeOpenAI(shutdown_date="2026-01-02"), monkeypatch, url=LOCAL, config={"model": "qwen3"})
    assert llm.health().detail == "the server at 127.0.0.1:9380 retired the model 'qwen3' on 2026-01-02. Choose another under models.chat.model."
    llm = started(FakeOpenAI(shutdown_date="2027-01-02"), monkeypatch, url=LOCAL, config={"model": "qwen3"})
    assert llm.health().detail == (
        "the language model 'qwen3' retires on 2027-01-02 at the server at 127.0.0.1:9380. Choose another under models.chat.model before then."
    )


def test_a_reply_naming_an_unknown_model_makes_it_unavailable_for_the_rest_of_the_run(monkeypatch):
    fake = FakeOpenAI(reply_status=404, reply_json=UNKNOWN_MODEL)
    llm = started(fake, monkeypatch)
    said = (
        "OpenAI does not know the model 'gpt-5.6-terra' (HTTP 404). It is retired, misspelt, or not open to this key. "
        "Choose another under models.chat.model."
    )
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert (done.stop_reason, done.error) == ("error", said)
    assert llm.health().faults == ("model_unavailable",) and llm.health().detail == said
    (again,) = run(collect(llm.reply(prompt("q"))))
    assert again.error == said and len(fake.posts) == 1
    assert llm.health().faults == ("model_unavailable",)


def test_a_bare_404_on_a_reply_is_no_unknown_model(monkeypatch):
    fake = FakeOpenAI(reply_status=404, reply_json={"error": {"message": "Not found", "type": "invalid_request_error"}})
    llm = started(fake, monkeypatch, url=LOCAL)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == "HTTP 404 invalid_request_error: Not found"
    assert llm.health().ok


def test_another_server_naming_the_model_unknown_is_named_as_that_server(monkeypatch):
    fake = FakeOpenAI(reply_status=404, reply_json=UNKNOWN_MODEL)
    llm = started(fake, monkeypatch, url=LOCAL, config={"model": "qwen3"})
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == (
        "the server at 127.0.0.1:9380 does not know the model 'qwen3' (HTTP 404). It is retired, misspelt, or not open "
        "to this key. Choose another under models.chat.model."
    )


# ------------------------------------------------------ OC3: out of credit


BILLING_CODES = ["credit_balance_exhausted", "organization_spend_limit_exceeded", "project_spend_limit_exceeded", "organization_usage_limit_exceeded"]


@pytest.mark.parametrize("code", BILLING_CODES)
def test_a_billing_429_on_a_reply_is_out_of_credit_with_its_code(monkeypatch, code):
    fake = FakeOpenAI(reply_status=429, reply_json=billing(code))
    llm = started(fake, monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    said = (
        f"OpenAI says the account is out of credit (HTTP 429, {code}). Add credit, or raise the project's budget, "
        f"in OpenAI's billing settings. Retrying will not help."
    )
    assert (done.stop_reason, done.error) == ("error", said)
    assert (llm.health().ok, llm.health().detail, llm.health().faults) == (False, said, ("out_of_credit",))


def test_the_type_alone_still_names_the_billing_case(monkeypatch):
    llm = started(FakeOpenAI(reply_status=429, reply_json=billing(None)), monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert "(HTTP 429, insufficient_quota)" in (done.error or "")
    assert llm.health().faults == ("out_of_credit",)


def test_out_of_credit_lasts_until_a_request_succeeds_or_fails_naming_no_kind(monkeypatch):
    """Review finding providers/code#1 (contract 0.5.7 section 9, C15): a
    failure that names no kind is news of the network or the server, so the
    turn after it takes the failed row and its offline signal."""
    fake = FakeOpenAI(reply_stream("back"), reply_status=429, reply_json=billing("credit_balance_exhausted"))
    llm = started(fake, monkeypatch)
    run(collect(llm.reply(prompt("q"))))
    assert llm.health().faults == ("out_of_credit",)
    fake.reply_status = 200
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert done.text == "back" and llm.health().ok and llm.describe().healthy
    fake.reply_status = 429
    run(collect(llm.reply(prompt("q"))))
    assert llm.health().faults == ("out_of_credit",)
    fake.reply_status = 500
    run(collect(llm.reply(prompt("q"))))
    assert llm.health().ok and llm.health().faults == (), "a failure that names no kind ends it"


def test_a_rate_limit_is_no_billing_failure(monkeypatch):
    fake = FakeOpenAI(reply_status=429, reply_json={"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}})
    llm = started(fake, monkeypatch)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == "HTTP 429 requests, rate_limit_exceeded: Rate limit reached"
    assert llm.health().ok


def test_another_servers_billing_failure_names_it_and_no_vendor_page(monkeypatch):
    llm = started(FakeOpenAI(reply_status=429, reply_json=billing("credit_balance_exhausted")), monkeypatch, url=LOCAL)
    (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.error == "the server at 127.0.0.1:9380 says the account is out of credit (HTTP 429, credit_balance_exhausted). Retrying will not help."


def test_a_billing_answer_to_the_preflight_is_out_of_credit_at_start(monkeypatch):
    llm = booted(FakeOpenAI(model_status=429, model_json=billing("credit_balance_exhausted")), monkeypatch)
    assert not llm.describe().healthy and llm.health().faults == ("out_of_credit",)


def test_every_row_of_the_code_table_names_a_kind_and_a_code():
    """A bare status never names a kind, since a wrong path at a custom url answers 404 as well."""
    assert FAULT_CODES
    for (status, code), kind in FAULT_CODES.items():
        assert kind in FAULT_KINDS and isinstance(status, int)
        assert code, f"{status} has a row with no code"


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


def test_usage_on_the_finishing_chunk_and_null_usage_on_the_others_are_both_read(monkeypatch):
    """OpenAI sends `usage: null` on every chunk but the last, and a local server may put the counts beside the finish (OC1)."""
    pieces = [dict(chunk({"content": "Hi"}), usage=None), chunk({}, finish="stop", usage={"prompt_tokens": 512, "completion_tokens": 2})]
    llm = started(FakeOpenAI(stream(*pieces)), monkeypatch, url=LOCAL)
    done = run(collect(llm.reply(prompt("hi"))))[-1]
    assert (done.text, done.input_tokens, done.output_tokens) == ("Hi", 512, 2)


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
    assert (done.input_tokens, done.output_tokens) == (5, 9)


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


def test_an_http_error_ends_the_reply_with_the_vendors_message(monkeypatch, caplog):
    fake = FakeOpenAI(reply_status=429, reply_json={"error": {"message": "Rate limit reached", "type": "rate_limit_error", "code": "rate_limit_exceeded"}})
    llm = started(fake, monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="emet_providers.openai"):
        (done,) = run(collect(llm.reply(prompt("q"))))
    assert done.stop_reason == "error"
    assert "429" in (done.error or "") and "Rate limit reached" in (done.error or "")
    assert llm.describe().healthy
    assert caplog.records and all(r.levelno < logging.WARNING for r in caplog.records), "the engine prints the error"


def test_an_error_object_in_the_stream_ends_the_reply(monkeypatch):
    body = stream(chunk({"content": "Half"}), {"error": {"message": "The server had an error", "type": "server_error"}})
    llm = started(FakeOpenAI(body), monkeypatch)
    done = run(collect(llm.reply(prompt("q"))))[-1]
    assert done.text == "Half" and done.stop_reason == "error"
    assert "server_error" in (done.error or "")


def test_replying_before_start_is_an_error_final_not_an_exception():
    (done,) = run(collect(make(FakeOpenAI()).reply(prompt("q"))))
    assert done.stop_reason == "error" and "not started" in (done.error or "")


def test_a_url_that_cannot_be_read_is_refused_at_start_in_one_line_with_no_password(monkeypatch):
    """Review finding sdk/code#1, the providers' half: an open bracket raised
    ValueError from the plugin's constructor, before any refusal could be
    written. Now the plugin is built and refuses at `start()`, naming the
    field, with nothing sent."""
    fake = FakeOpenAI()
    llm = booted(fake, monkeypatch, url="http://me:hunter2@[::1/v1")
    health = llm.health()
    assert not health.ok and health.faults == ("start_failed",)
    assert health.detail.startswith("models.chat.params.url cannot be read as a URL (")
    assert "http://[::1/v1" in health.detail and "hunter2" not in health.detail
    assert "hunter2" not in (llm.describe().endpoint or "")
    assert len(fake.requests) == 0, "nothing is sent for a URL that cannot be read"
