"""The language model through OpenAI's Chat Completions API.

The second vendor behind the `emet.llm` seam, shipped beside Anthropic so that
the seam is proven by two implementations. Nothing above knows which one is
running.

**Why Chat Completions and not the newer Responses API.** Chat Completions is
the shape every other server speaks too: a local model behind Ollama, vLLM or
LM Studio, a gateway such as OpenRouter, a company's own proxy. `params.url`
points this plugin at any of them, which is how the offline mode the design
reserves for later arrives as a manifest line rather than a rewrite.

**Protocol, current as of 2026-09-15.** `POST {url}/chat/completions` with
`Authorization: Bearer <key>` and `stream: true`. Each Server-Sent Event is
one `chat.completion.chunk`: `choices[0].delta.content` carries text,
`choices[0].delta.tool_calls[]` carries a call's `id`, `type` and
`function.name` in its first chunk and `function.arguments` in pieces after,
keyed by `index`; `choices[0].finish_reason` closes the choice (`stop`,
`length`, `tool_calls`, `content_filter`); with `stream_options:
{"include_usage": true}` a final chunk with empty `choices` carries `usage`;
the stream ends with `data: [DONE]`. `GET {url}/models` lists what the key may
use, and `GET {url}/models/{model}` describes one model, with the
`shutdown_date` OpenAI has set for it or null (API reference, read
2026-11-27). The output cap is `max_completion_tokens`; servers that predate
the rename take `max_tokens`, and `params.max_tokens_field` says which.

Params, all optional, from the body's `models.chat.params`:

    url               the API root. Default https://api.openai.com/v1; any
                      compatible server works.
    timeout_s         seconds per request, connect and read. Default 60.
    preflight         bool, default true. `GET /models/{model}` in `start()`
                      to check the key, the network and the model, and to
                      read the date the model shuts down.
    max_tokens_field  "max_completion_tokens" (default) or "max_tokens".
    organization      sent as `OpenAI-Organization` when set.
    reasoning_effort  a word, sent as `reasoning_effort` only when set, so
                      that a server which refuses the field never sees it.
                      Unset, the server's default runs: `medium` for
                      gpt-5.6-terra, whose page lists none, low, medium,
                      high, xhigh and max (read 2026-11-27).
    warm_up           bool, default false. After a passing preflight,
                      `start()` sends one chat request, the user message
                      `hi` capped at one output token, so a local server
                      loads the model before the first question. It warms
                      the model's load only: the system prompt is built
                      after the plugin starts. The idea is Ollama's FAQ's,
                      "preload a model by sending the Ollama server an
                      empty request" (Ollama, MIT, read 2026-11-27).

**Why the reasoning effort matters to a speaking robot.** The cap the engine
sends counts "visible output tokens and reasoning tokens" (Chat Completions
reference, read 2026-11-27), and OpenAI's reasoning guide warns that a model
can spend the cap reasoning "before any visible output tokens are produced"
(read 2026-11-27). Such a reply stops at `length` with no words, and the
engine says the soul's `failed` line for it. A lower effort leaves room for
the words. One caution for tool use: "Starting with GPT-5.4, Chat
Completions does not support tool calling with `reasoning_effort` values
other than `none`" (OpenAI's guide to migrating to Responses, read
2026-11-27). The engine offers the model no tools in 0.5.

**What a failure says.** A failed response's kind comes from its status and
the vendor's code through `FAULT_CODES` and `_http.fault_kind`, never from
the words of its message. The sentence names OpenAI when `url` is
`DEFAULT_URL` and "the server at HOST" otherwise, since a local server
behind this plugin is not OpenAI. A model OpenAI does not know or has
retired is `model_unavailable`, for the rest of the run, and the robot
boots and says one line at each wake; an account out of credit is
`out_of_credit` until a request succeeds, or fails for a reason that names
no kind, such as a network that is down. A server that does not answer is
described where it runs: on this machine, on the local network, or across
the network.

Dependencies: `httpx` (BSD-3-Clause), through the `openai` extra. Not the
vendor SDK, for the reason given in `anthropic.py`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import quote

from emet_sdk.plugin import LanguageModelPlugin
from emet_sdk.types import (
    Health,
    LanguageModelDescriptor,
    Message,
    Prompt,
    ReplyDone,
    ReplyEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
)

from emet_providers._http import (
    LOCAL_NETWORK,
    THIS_MACHINE,
    api_error,
    endpoint_of,
    error_code,
    fault_kind,
    open_client,
    place_of,
    sse_events,
    url_problem,
    who,
)

__all__ = [
    "OpenAILanguageModel",
    "DEFAULT_MODEL",
    "DEFAULT_KEY_ENV",
    "DEFAULT_URL",
    "FAULT_CODES",
    "WARM_UP_MESSAGE",
]

log = logging.getLogger("emet_providers.openai")

DEFAULT_URL = "https://api.openai.com/v1"

#: The model OpenAI's list describes as balancing intelligence and cost, on
#: 2026-09-15. A speaking robot wants the answer soon more than it wants the
#: largest model; the soul names another when it disagrees.
DEFAULT_MODEL = "gpt-5.6-terra"

DEFAULT_KEY_ENV = "EMET_OPENAI_KEY"

#: The vendor's name in a failure sentence, when the plugin talks to it.
VENDOR = "OpenAI"

#: The one user message `params.warm_up` sends, capped at one output token.
WARM_UP_MESSAGE = "hi"

#: The vendor's finish reasons, in the seam's words.
FINISH_REASONS: Mapping[str, str] = {
    "stop": "end",
    "length": "length",
    "tool_calls": "tool",
    "function_call": "tool",
    "content_filter": "refusal",
}

#: What a failed response's status and code mean, read by `_http.fault_kind`.
#:
#: The 429 rows are OpenAI's error-codes guide
#: (developers.openai.com/api/docs/guides/error-codes, read 2026-11-27): for
#: a billing error, "inspect `error.code` to identify the specific cause. The
#: broader `error.type` can still be `insufficient_quota`", and "Retrying
#: billing, spend, or quota errors won't restore API access." Any other 429
#: is a rate limit, which waiting does fix, and has no row. The 404 row is
#: the code OpenAI's developer forum shows for a model that does not exist
#: or that the key cannot use; the guide does not list it (unverified against
#: OpenAI's documentation, 2026-11-27). A bare 404 has no row, since a wrong
#: path at a custom `url` answers 404 too.
FAULT_CODES: Mapping[tuple[int, str | None], str] = {
    (429, "insufficient_quota"): "out_of_credit",
    (429, "credit_balance_exhausted"): "out_of_credit",
    (429, "organization_spend_limit_exceeded"): "out_of_credit",
    (429, "project_spend_limit_exceeded"): "out_of_credit",
    (429, "organization_usage_limit_exceeded"): "out_of_credit",
    (404, "model_not_found"): "model_unavailable",
}


def utc_today() -> date:
    """Today's date in UTC, the calendar a vendor's shutdown date is read against."""
    return datetime.now(timezone.utc).date()


def _shutdown_date(content: bytes) -> date | None:
    """The `shutdown_date` of a model object, or None when it has none or cannot be read.

    The reference gives a date string, `"2026-10-23"`. A number is read as
    Unix seconds, for a server that writes dates the way it writes `created`.
    """
    try:
        data = json.loads(content)
    except ValueError:
        return None
    value = data.get("shutdown_date") if isinstance(data, dict) else None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))},
                    }
                    for c in m.tool_calls
                ]
            out.append(entry)
        else:  # tool
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
    return out


def _tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": dict(t.parameters)},
        }
        for t in tools
    ]


class OpenAILanguageModel(LanguageModelPlugin):
    """GPT, or anything that speaks Chat Completions, streamed."""

    provider = "openai"

    def __init__(self, config: Mapping[str, Any], *, transport: Any = None) -> None:
        super().__init__(config)
        self._transport = transport
        self._client: Any = None
        self._key: str | None = None
        self._started = False
        self._fault: str | None = None
        self._fault_kind = "start_failed"
        self._notice: str | None = None
        self.last_error: str | None = None
        #: Seconds the warm-up request took, once it has answered.
        self.warmed_s: float | None = None

        self.model_name: str = self.model or DEFAULT_MODEL
        self.key_env_name: str = self.key_env or DEFAULT_KEY_ENV
        self.url: str = str(self.params.get("url") or DEFAULT_URL).rstrip("/")
        self.endpoint: str = endpoint_of(self.url)
        self.host: str | None = LanguageModelDescriptor(provider=self.provider, endpoint=self.endpoint).host
        self.timeout_s = float(self.params.get("timeout_s", 60.0))
        self.preflight = bool(self.params.get("preflight", True))
        self.max_tokens_field: str = str(self.params.get("max_tokens_field") or "max_completion_tokens")
        self.organization: str | None = self.params.get("organization") or None
        effort = self.params.get("reasoning_effort")
        self.reasoning_effort: str | None = effort if isinstance(effort, str) and effort else None
        warm = self.params.get("warm_up", False)
        self.warm_up: bool = warm is True

    @property
    def at_vendor(self) -> bool:
        """Whether `url` is OpenAI's own, so a failure may name OpenAI and its pages."""
        return self.url == DEFAULT_URL

    # ------------------------------------------------------------- request

    def headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._key or ''}", "content-type": "application/json"}
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        return headers

    def body(self, prompt: Prompt) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": _messages(prompt.system, prompt.messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            self.max_tokens_field: prompt.max_tokens,
        }
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        if prompt.tools:
            body["tools"] = _tools(prompt.tools)
        return body

    # ------------------------------------------------------------ sentences

    def _who(self) -> str:
        return who(self.url, default_url=DEFAULT_URL, vendor=VENDOR, host=self.host)

    def _unknown_model(self, status: int) -> str:
        return (
            f"{self._who()} does not know the model {self.model_name!r} (HTTP {status}). "
            f"It is retired, misspelt, or not open to this key. Choose another under "
            f"models.chat.model."
        )

    def _retired(self, day: date) -> str:
        sentence = f"{self._who()} retired the model {self.model_name!r} on {day.isoformat()}. Choose another under models.chat.model"
        if self.at_vendor:
            sentence += f"; {VENDOR}'s deprecations page names its replacement"
        return sentence + "."

    def _retires(self, day: date) -> str:
        return (
            f"the language model {self.model_name!r} retires on {day.isoformat()} at {self._who()}. "
            f"Choose another under models.chat.model before then."
        )

    def _out_of_credit(self, status: int, body: bytes) -> str:
        where = " Add credit, or raise the project's budget, in OpenAI's billing settings." if self.at_vendor else ""
        code = error_code(body) or "no code given"
        return f"{self._who()} says the account is out of credit (HTTP {status}, {code}).{where} Retrying will not help."

    def _unreachable(self, exc: Exception) -> str:
        what = f"{type(exc).__name__}: {exc}"
        place = place_of(self.host) if self.host else None
        if place == THIS_MACHINE:
            return (
                f"Nothing answered at {self.endpoint} ({what}). That address is this machine: "
                f"start the model's server, or correct models.chat.params.url. "
                f"emet-providers' README has a recipe for each local server."
            )
        if place == LOCAL_NETWORK:
            return (
                f"Nothing answered at {self.endpoint} ({what}). That address is on the local "
                f"network: start the model's server, or correct models.chat.params.url."
            )
        return f"could not reach {self.endpoint}: {what}. The language model needs a network; the wake word does not."

    def _no_key(self) -> str:
        where = (
            "OpenAI keys come from platform.openai.com"
            if self.at_vendor
            else f"Set it to a key {self._who()} accepts, or to any value if it checks none"
        )
        return (
            f"no key: the environment variable {self.key_env_name} is not set. {where}; export it "
            f"in the shell that runs the robot. The key is never written into a soul or a manifest."
        )

    def _rejected(self, status: int) -> str:
        return (
            f"the server at {self.endpoint} rejected the key in {self.key_env_name} "
            f"(HTTP {status}). Check the key."
        )

    def _config_problem(self) -> str | None:
        unreadable = url_problem(self.url, "models.chat.params.url")
        if unreadable:
            return unreadable
        effort = self.params.get("reasoning_effort")
        if effort is not None and not isinstance(effort, str):
            return (
                f"models.chat.params.reasoning_effort is a word such as low or none, and it was "
                f"given {effort!r} ({type(effort).__name__}). Quote it in the manifest."
            )
        warm = self.params.get("warm_up", False)
        if not isinstance(warm, bool):
            return f"models.chat.params.warm_up is true or false, and it was given {warm!r} ({type(warm).__name__})."
        return None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._fault, self._fault_kind, self._notice = None, "start_failed", None
        problem = self._config_problem()
        if problem:
            self._fail(problem)
            return
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(self._no_key(), "no_key")
            return
        client = open_client(self.url, timeout_s=self.timeout_s, transport=self._transport, extra="openai")
        if isinstance(client, str):
            self._fail(client, "no_library")
            return
        self._client = client
        if self.preflight:
            await self._preflight()
            if self._fault is not None and self._fault_kind != "model_unavailable":
                return
        self._started = True
        if self._fault is None and self.warm_up:
            await self._warm()
        log.info("openai: ready, model %s at %s", self.model_name, self.endpoint)

    async def _preflight(self) -> None:
        """Ask for the soul's model by id, and read what the answer says of it.

        At OpenAI's own URL a 404 there means OpenAI does not know the model,
        since the request named it. At any other URL a server may not serve
        that path at all, so any answer but 200 and a rejected key falls back
        to the list, as the preflight did before it asked for one model.
        """
        try:
            response = await self._client.get(f"/models/{quote(self.model_name, safe='')}", headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            self._fail(self._unreachable(exc))
            return
        status = response.status_code
        if status == 200:
            self._read_lifecycle(response.content)
            return
        if status in (401, 403):
            self._fail(self._rejected(status))
            return
        if not self.at_vendor:
            await self._preflight_list()
            return
        if status == 404:
            self._fail(self._unknown_model(status), "model_unavailable")
            return
        self._fail_response(status, response.content)

    async def _preflight_list(self) -> None:
        try:
            response = await self._client.get("/models", headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            self._fail(self._unreachable(exc))
            return
        if response.status_code == 200:
            return
        if response.status_code in (401, 403):
            self._fail(self._rejected(response.status_code))
            return
        self._fail_response(response.status_code, response.content)

    def _fail_response(self, status: int, body: bytes) -> None:
        kind = fault_kind(status, body, FAULT_CODES)
        if kind == "out_of_credit":
            self._fail(self._out_of_credit(status, body), kind)
        elif kind == "model_unavailable":
            self._fail(self._unknown_model(status), kind)
        else:
            self._fail(f"the server at {self.endpoint} refused the connection: {api_error(status, body)}")

    def _read_lifecycle(self, content: bytes) -> None:
        day = _shutdown_date(content)
        if day is None:
            return
        if day < utc_today():
            self._fail(self._retired(day), "model_unavailable")
        else:
            self._notice = self._retires(day)
            log.info("openai: %s", self._notice)

    async def _warm(self) -> None:
        """Send the one capped request `params.warm_up` asks for, and never fail the start over it."""
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": WARM_UP_MESSAGE}],
            "stream": False,
            self.max_tokens_field: 1,
        }
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        began = time.monotonic()
        try:
            response = await self._client.post("/chat/completions", headers=self.headers(), json=body)
        except Exception as exc:  # noqa: BLE001 - a cold first question is the cost, not a refusal
            log.info("openai: the warm-up request to %s failed (%s: %s); the first question loads the model", self.endpoint, type(exc).__name__, exc)
            return
        if response.status_code != 200:
            log.info("openai: the warm-up request was refused (%s); the first question loads the model", api_error(response.status_code, response.content))
            return
        self.warmed_s = time.monotonic() - began
        log.info("openai: warmed %s at %s in %.1f s", self.model_name, self.endpoint, self.warmed_s)

    def _fail(self, reason: str, kind: str = "start_failed") -> None:
        # INFO: the engine prints `health().detail` itself, so a WARNING here
        # would print the same sentence twice through logging's last resort.
        self._fault, self._fault_kind = reason, kind
        log.info("openai: %s", reason)

    async def shutdown(self) -> None:
        self._started = False
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------ reporting

    def describe(self) -> LanguageModelDescriptor:
        return LanguageModelDescriptor(
            provider=self.provider,
            model=self.model_name,
            streaming=True,
            tools=True,
            healthy=self._started and self._fault is None,
            endpoint=self.endpoint,
            key_env=self.key_env_name,
        )

    def health(self) -> Health:
        if self._fault:
            return Health(ok=False, detail=self._fault, faults=(self._fault_kind,))
        if self._notice:
            return Health(ok=True, detail=self._notice)
        return Health()

    # ---------------------------------------------------------------- reply

    def _reply_failure(self, status: int, body: bytes) -> str:
        """The sentence for a reply's failed response, keeping its kind for `health()`."""
        kind = fault_kind(status, body, FAULT_CODES)
        if kind == "out_of_credit":
            self._fault, self._fault_kind = self._out_of_credit(status, body), kind
            return self._fault
        if kind == "model_unavailable":
            self._fault, self._fault_kind = self._unknown_model(status), kind
            return self._fault
        self._forget_credit()
        return api_error(status, body)

    def _forget_credit(self) -> None:
        """End a kept `out_of_credit` once a request fails for a reason that names no kind."""
        if self._fault is not None and self._fault_kind == "out_of_credit":
            # The failure is news of the network or the server, never of the
            # account, so the turn takes the failed row and its signal.
            self._fault, self._fault_kind = None, "start_failed"

    def _unsent(self) -> ReplyDone | None:
        """The final for a reply that is never sent, or None when it may be."""
        if not self._started or self._client is None:
            return ReplyDone(text="", stop_reason="error", error="the openai plugin was not started")
        if self._fault is not None and self._fault_kind == "model_unavailable":
            # Nothing is sent for a model the vendor has said it cannot serve.
            return ReplyDone(text="", stop_reason="error", model=self.model_name, error=self._fault)
        return None

    def _settle(self, error: str | None) -> None:
        """Log a sent reply's error, or end an `out_of_credit` once a reply succeeds."""
        if error:
            self.last_error = error
            # INFO: the engine prints the error with the turn.
            log.info("openai: %s", error)
        elif self._fault is not None and self._fault_kind == "out_of_credit":
            # A request went through, so the account has credit again.
            self._fault, self._fault_kind = None, "start_failed"

    async def reply(self, prompt: Prompt) -> AsyncIterator[ReplyEvent]:
        unsent = self._unsent()
        if unsent is not None:
            yield unsent
            return

        text: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        stop = "end"
        model: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        error: str | None = None

        try:
            async with self._client.stream(
                "POST", "/chat/completions", headers=self.headers(), json=self.body(prompt)
            ) as response:
                if response.status_code != 200:
                    error = self._reply_failure(response.status_code, await response.aread())
                else:
                    async for _event, data in sse_events(response.aiter_lines()):
                        if not isinstance(data, dict):
                            continue
                        if data.get("raw") == "[DONE]":
                            break
                        if "error" in data and "choices" not in data:
                            err = data.get("error") or {}
                            error = f"{err.get('type', 'error')}: {err.get('message', '')}"
                            break
                        model = data.get("model") or model
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            if isinstance(usage.get("prompt_tokens"), int):
                                input_tokens = usage["prompt_tokens"]
                            if isinstance(usage.get("completion_tokens"), int):
                                output_tokens = usage["completion_tokens"]
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            text.append(piece)
                            yield TextDelta(piece)
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index", 0))
                            entry = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if call.get("id"):
                                entry["id"] = str(call["id"])
                            function = call.get("function") or {}
                            if function.get("name"):
                                entry["name"] = str(function["name"])
                            entry["arguments"] += str(function.get("arguments") or "")
                        reason = choice.get("finish_reason")
                        if reason:
                            stop = FINISH_REASONS.get(str(reason), "end")
        except Exception as exc:  # noqa: BLE001 - the reply ends with what was heard
            error = f"{type(exc).__name__}: {exc}"
            self._forget_credit()

        calls: list[ToolCall] = []
        for index in sorted(pending):
            entry = pending[index]
            try:
                arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except ValueError:
                arguments = {}
            call = ToolCall(id=entry["id"] or f"call_{index}", name=entry["name"], arguments=arguments)
            calls.append(call)
            yield call

        self._settle(error)
        yield ReplyDone(
            text="".join(text),
            stop_reason="error" if error else stop,
            tool_calls=tuple(calls),
            model=model or self.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
        )
