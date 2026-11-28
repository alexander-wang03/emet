"""The language model through Anthropic's Messages API.

One of two vendors behind the `emet.llm` seam, shipped together so that the
seam is proven by its second implementation rather than shaped by its first.
Nothing above knows which one is running.

**Protocol, from Anthropic's API reference on 2026-09-15, its version read
again on 2026-11-27.** `POST /v1/messages` with `x-api-key`,
`anthropic-version: 2026-10-21` and `stream: true`. The reply is Server-Sent
Events: `message_start` (the model that answered, input tokens),
`content_block_start` for each text or `tool_use` block,
`content_block_delta` carrying `text_delta` or `input_json_delta` pieces,
`content_block_stop`, then `message_delta` with the `stop_reason` and the
usage so far, then `message_stop`. The usage on `message_delta` is
cumulative, so its counts replace those of `message_start`. Stop reasons:
`end_turn`, `stop_sequence`, `tool_use`, `max_tokens`, `refusal`,
`pause_turn`. Tool results go back as `tool_result` blocks in a user turn.
A bad key is HTTP 401 at any endpoint.

**The version.** 2026-10-21 answers a Messages request that carries
`temperature`, `top_p` or `top_k` with a 400 on every model, and refuses a
`document` block in an assistant turn and a `text` block with citations in a
user turn. This plugin sends none of the five, so the move from 2023-06-01
changes nothing it sends (Anthropic's versioning page, verified 2026-11-27).

**What is deliberately not sent.** No `temperature`, `top_p` or `top_k`,
for the reason above. No `thinking` block: Claude Opus 5 thinks adaptively
when the field is omitted, and the raw reasoning is never returned anyway.
Effort is the knob that matters for a speaking robot and it is a param,
unset by default because not every model accepts it.

**The model's lifecycle at boot.** The preflight asks for the soul's model
by id, `GET /v1/models/{id}`, which also checks the key and the network in
one request. Its `lifecycle` is `active`, `deprecated` or `retired`, and
`retires_at` is the scheduled retirement, null while the model is active
(Anthropic's Get a Model reference, verified 2026-11-27). A retired model,
or a 404 at Anthropic's own address, is the fault `model_unavailable`: the
robot boots and says one line at each wake. A scheduled date is a notice in
a healthy `health()`, printed once at boot. At any other address every
answer there but the model and a rejected key (401, 403) falls back to
`GET /v1/models`, as the OpenAI plugin's preflight does, so a proxy or a
local server that lists models and does not serve the per-model path
behaves as it did before 0.5.7, whatever status it answers that path with.

**A retirement while the robot runs.** Anthropic answers a request for a
retired model with a 404 `not_found_error` (its model deprecations page,
verified 2026-11-27). That type names no model, so after a 404 reply the
plugin asks the per-model path once more, and sets `model_unavailable` only
when that answer says the model is retired or unknown.

**Out of credit.** `FAULT_CODES` below maps the status and code of a failed
response to a fault kind, read by `_http.fault_kind`. A kind there makes the
reply's `error`, and `health().detail`, one whole sentence that says what
happened and what to do, and `health()` reports the kind until a reply
succeeds.

**Refusals.** A safety classifier may decline with HTTP 200 and
`stop_reason: "refusal"`. That is reported as `refusal`, and the engine says
something rather than nothing. Anthropic recommends opting into server-side
fallbacks so a declined request is re-run on another model inside the same
call; that is `params.fallbacks`, off unless the owner turns it on, because it
is a beta and it changes which model answers.

Params, all optional, from the body's `models.chat.params`:

    effort       "low" | "medium" | "high" | "xhigh" | "max". Sent as
                 `output_config.effort`. `low` suits a spoken reply; unset
                 leaves the model's default. `reasoning_effort` is the OpenAI
                 plugin's key and is never read here.
    fallbacks    "default" (Anthropic picks a fallback by refusal category,
                 beta header `server-side-fallback-2026-07-01`) or a list of
                 model names (beta header `server-side-fallback-2026-06-01`).
    url          the API root, for a proxy. Default https://api.anthropic.com
    timeout_s    seconds per request, connect and read. Default 60.
    preflight    bool, default true. `GET /v1/models/{id}` in `start()` to
                 check the key, the network and the model's lifecycle.

Dependencies: `httpx` (BSD-3-Clause), through the `anthropic` extra. Not the
vendor SDK: the wire protocol is small, the same client serves both vendors,
and a vendor SDK is the vendor's shape arriving by another door.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
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
    "AnthropicLanguageModel",
    "DEFAULT_MODEL",
    "DEFAULT_KEY_ENV",
    "DEFAULT_URL",
    "API_VERSION",
    "FAULT_CODES",
]

log = logging.getLogger("emet_providers.anthropic")

DEFAULT_URL = "https://api.anthropic.com"

#: Read at Anthropic's versioning page on 2026-11-27, which says "Anthropic
#: recommends using the latest API version whenever possible".
API_VERSION = "2026-10-21"

#: Anthropic's current flagship, per their model list on 2026-09-15. The soul
#: names another model when it wants one; a soul that says only
#: `provider: anthropic` gets this. Its row in `emet_providers/lifecycle.py`
#: holds the date Anthropic says it will not retire before.
DEFAULT_MODEL = "claude-opus-5"

DEFAULT_KEY_ENV = "EMET_ANTHROPIC_KEY"

FALLBACK_BETA_DEFAULT = "server-side-fallback-2026-07-01"
FALLBACK_BETA_ARRAY = "server-side-fallback-2026-06-01"

#: What a failed response's status and code mean, read by `_http.fault_kind`
#: (key `(status, code)`, then `(status, type)`, then `(status, None)`). From
#: Anthropic's errors page and its list of error codes, both verified
#: 2026-11-27: a 402 `billing_error` is "an issue with your billing or
#: payment information"; a 429 `rate_limit_error` is a rate limit unless
#: `error.details.error_code` names a spend cap, `enforced_spend_limit_reached`
#: (the tier's monthly cap) or `contract_spend_limit_reached`. A plain 429 is
#: a rate limit, which a later turn can pass, so it has no row. A 400 for a
#: spend limit the owner set carries no code, so it stays an ordinary failure.
#: No code here names a model: `model_unavailable` comes from the per-model
#: path only.
FAULT_CODES: dict[tuple[int, str | None], str] = {
    (402, "billing_error"): "out_of_credit",
    (402, None): "out_of_credit",
    (429, "enforced_spend_limit_reached"): "out_of_credit",
    (429, "contract_spend_limit_reached"): "out_of_credit",
}

#: The vendor's stop reasons, in the seam's words. Anything unlisted is a
#: natural end: it is how new reasons arrive without breaking a robot.
STOP_REASONS: Mapping[str, str] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "pause_turn": "end",
    "tool_use": "tool",
    "max_tokens": "length",
    "refusal": "refusal",
}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """The seam's messages in Anthropic's shape.

    Tool results ride in a `user` turn as `tool_result` blocks, and several
    results for one assistant turn share one user turn, because the API
    wants roles to alternate.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if not m.tool_calls:
                out.append({"role": "assistant", "content": m.content})
                continue
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for call in m.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": dict(call.arguments)}
                )
            out.append({"role": "assistant", "content": blocks})
        else:  # tool
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            last = out[-1] if out else None
            if (
                last is not None
                and last["role"] == "user"
                and isinstance(last["content"], list)
                and last["content"]
                and last["content"][0].get("type") == "tool_result"
            ):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def _tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
        for t in tools
    ]


def _day(value: Any) -> str | None:
    """The UTC day of an RFC 3339 time, as `YYYY-MM-DD`, or None.

    `2027-07-24T00:00:00Z` gives `2027-07-24`. A time with an offset is moved
    to UTC first, so the day printed is the one Anthropic's page gives.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        found = _DATE.match(value)
        return found.group(0) if found else None
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc)
    return moment.date().isoformat()


def _input_tokens(usage: Mapping[str, Any]) -> int | None:
    """Every input token the request read, cached ones included.

    Anthropic counts tokens read from or written to the prompt cache apart
    from `input_tokens`, so the three are summed here. That matches the
    OpenAI plugin, whose prompt count includes its cached tokens.
    """
    def count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    base = count("input_tokens")
    if base is None:
        return None
    return base + (count("cache_creation_input_tokens") or 0) + (count("cache_read_input_tokens") or 0)


class _Reading:
    """What one streamed reply has said so far, read one event at a time.

    `take()` returns the engine's event for a text piece or a finished tool
    call, and None for the rest. `ended` turns true at `message_stop` and at
    an `error` event, after which the stream is not read further. An event
    the table does not name, such as `ping`, is skipped.
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.calls: list[ToolCall] = []
        self.blocks: dict[int, dict[str, Any]] = {}
        self.stop = "end"
        self.model: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.error: str | None = None
        self.ended = False
        self._events: dict[str, Any] = {
            "message_start": self._begin,
            "content_block_start": self._open,
            "content_block_delta": self._delta,
            "content_block_stop": self._close,
            "message_delta": self._end,
            "error": self._error,
        }

    def take(self, event: str, data: Mapping[str, Any]) -> ReplyEvent | None:
        if event == "message_stop":
            self.ended = True
            return None
        handler = self._events.get(event)
        return handler(data) if handler is not None else None

    def _count(self, usage: Mapping[str, Any]) -> None:
        counted = _input_tokens(usage)
        if counted is not None:
            self.input_tokens = counted

    def _begin(self, data: Mapping[str, Any]) -> None:
        message = data.get("message") or {}
        self.model = message.get("model") or self.model
        self._count(message.get("usage") or {})

    def _open(self, data: Mapping[str, Any]) -> None:
        block = data.get("content_block") or {}
        index = int(data.get("index", 0))
        if block.get("type") == "tool_use":
            self.blocks[index] = {
                "type": "tool_use",
                "id": str(block.get("id", "")),
                "name": str(block.get("name", "")),
                "json": "",
            }
        elif block.get("type") == "text":
            self.blocks[index] = {"type": "text"}

    def _delta(self, data: Mapping[str, Any]) -> ReplyEvent | None:
        delta = data.get("delta") or {}
        index = int(data.get("index", 0))
        if delta.get("type") == "text_delta":
            piece = str(delta.get("text", ""))
            if piece:
                self.text.append(piece)
                return TextDelta(piece)
        elif delta.get("type") == "input_json_delta":
            if index in self.blocks and self.blocks[index]["type"] == "tool_use":
                self.blocks[index]["json"] += str(delta.get("partial_json", ""))
        return None

    def _close(self, data: Mapping[str, Any]) -> ReplyEvent | None:
        block = self.blocks.pop(int(data.get("index", 0)), None)
        if not block or block["type"] != "tool_use":
            return None
        try:
            arguments = json.loads(block["json"]) if block["json"] else {}
        except ValueError:
            arguments = {}
        call = ToolCall(id=block["id"], name=block["name"], arguments=arguments)
        self.calls.append(call)
        return call

    def _end(self, data: Mapping[str, Any]) -> None:
        reason = (data.get("delta") or {}).get("stop_reason")
        if reason:
            self.stop = STOP_REASONS.get(str(reason), "end")
        usage = data.get("usage") or {}
        # Cumulative: the last message_delta holds the run's counts.
        self._count(usage)
        if isinstance(usage.get("output_tokens"), int):
            self.output_tokens = usage["output_tokens"]

    def _error(self, data: Mapping[str, Any]) -> None:
        err = data.get("error") or {}
        self.error = f"{err.get('type', 'error')}: {err.get('message', '')}"
        self.ended = True

    def done(self, error: str | None, model: str) -> ReplyDone:
        """The reply's final, `error` being the request's failure or None."""
        return ReplyDone(
            text="".join(self.text),
            stop_reason="error" if error else self.stop,
            tool_calls=tuple(self.calls),
            model=self.model or model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            error=error,
        )


class AnthropicLanguageModel(LanguageModelPlugin):
    """Claude, streamed over the Messages API."""

    provider = "anthropic"

    def __init__(self, config: Mapping[str, Any], *, transport: Any = None) -> None:
        super().__init__(config)
        #: An `httpx` transport, injected by tests. None means the network.
        self._transport = transport
        self._client: Any = None
        self._key: str | None = None
        self._started = False
        #: Why `start()` failed, and its kind. None while the plugin is well.
        self._fault: str | None = None
        self._fault_kind: str = "start_failed"
        #: A kind learned from a failed reply, with its sentence: set by an
        #: unpaid account until a reply succeeds or a request fails for a
        #: reason that names no kind, by a retired model for good.
        self._reply_fault: tuple[str, str] | None = None
        #: The healthy report's one line for the boot, such as a retirement date.
        self._notice: str | None = None
        #: The last failure inside a reply, for diagnostics. Not a fault.
        self.last_error: str | None = None

        self.model_name: str = self.model or DEFAULT_MODEL
        self.key_env_name: str = self.key_env or DEFAULT_KEY_ENV
        self.url: str = str(self.params.get("url") or DEFAULT_URL).rstrip("/")
        self.endpoint: str = endpoint_of(self.url)
        self.timeout_s = float(self.params.get("timeout_s", 60.0))
        self.preflight = bool(self.params.get("preflight", True))
        self.effort: str | None = self.params.get("effort") or None
        self.fallbacks: Any = self.params.get("fallbacks") or None

    # ---------------------------------------------------------------- words

    @property
    def at_vendor(self) -> bool:
        """Whether requests go to Anthropic's own address."""
        return self.url == DEFAULT_URL

    @property
    def host(self) -> str | None:
        return self.describe().host

    def _who(self) -> str:
        return who(self.url, default_url=DEFAULT_URL, vendor="Anthropic", host=self.host)

    def _unknown(self, status: int) -> str:
        return (
            f"{self._who()} does not know the model {self.model_name!r} (HTTP {status}). "
            f"It is retired, misspelt, or not open to this key. Choose another under "
            f"models.chat.model."
        )

    def _retired(self, day: str | None) -> str:
        when = f" on {day}" if day else ""
        text = f"{self._who()} retired the model {self.model_name!r}{when}. Choose another under models.chat.model"
        if self.at_vendor:
            text += f"; {self._who()}'s deprecations page names its replacement"
        return text + "."

    def _out_of_credit(self, status: int, body: bytes) -> str:
        code = error_code(body)
        said = f"HTTP {status}, {code}" if code else f"HTTP {status}"
        where = " Add credit in the Claude Console's billing settings." if self.at_vendor else ""
        return f"{self._who()} says the account is out of credit ({said}).{where} Retrying will not help."

    def _unreached(self, exc: Exception) -> str:
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

    # ------------------------------------------------------------- request

    def headers(self) -> dict[str, str]:
        headers = {
            "x-api-key": self._key or "",
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        if self.fallbacks == "default":
            headers["anthropic-beta"] = FALLBACK_BETA_DEFAULT
        elif self.fallbacks:
            headers["anthropic-beta"] = FALLBACK_BETA_ARRAY
        return headers

    def body(self, prompt: Prompt) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": prompt.max_tokens,
            "stream": True,
            "messages": _messages(prompt.messages),
        }
        if prompt.system:
            body["system"] = prompt.system
        if prompt.tools:
            body["tools"] = _tools(prompt.tools)
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        if self.fallbacks == "default":
            body["fallbacks"] = "default"
        elif self.fallbacks:
            body["fallbacks"] = [{"model": str(m)} for m in self.fallbacks]
        return body

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        unreadable = url_problem(self.url, "models.chat.params.url")
        if unreadable:
            self._fail("start_failed", unreadable)
            return
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            where = (
                "Anthropic keys come from console.anthropic.com"
                if self.at_vendor
                else f"Set it to a key {self._who()} accepts, or to any value if it checks none"
            )
            self._fail(
                "no_key",
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"{where}; export it in the shell that runs the robot. The key is "
                f"never written into a soul or a manifest.",
            )
            return
        client = open_client(self.url, timeout_s=self.timeout_s, transport=self._transport, extra="anthropic")
        if isinstance(client, str):
            self._fail("no_library", client)
            return
        self._client = client
        if self.preflight:
            found = await self._preflight()
            if found:
                self._fail(*found)
                return
        self._started = True
        log.info("anthropic: ready, model %s", self.model_name)

    def _model_path(self) -> str:
        return f"/v1/models/{quote(self.model_name, safe='')}"

    async def _preflight(self) -> tuple[str, str] | None:
        """Ask for the model by id, which checks the key, the network and the lifecycle at once.

        Returns the fault's kind and its sentence, or None when the model
        can answer. A retirement date sets the boot's notice.
        """
        try:
            response = await self._client.get(self._model_path(), headers=self.headers())
        except Exception as exc:  # noqa: BLE001 - reported through health, not raised
            return "start_failed", self._unreached(exc)
        status = response.status_code
        if status == 200:
            return self._read_lifecycle(response)
        if status == 404 and self.at_vendor:
            return "model_unavailable", self._unknown(status)
        if status in (401, 403) or self.at_vendor:
            return self._refusal(status, response.content)
        # Another server may list models and not serve the per-model path,
        # answering 404, 405, 400 or 500 there, so any answer but the model
        # and a rejected key asks the list, as the OpenAI plugin does; before
        # 0.5.7 the preflight asked the list alone.
        return await self._preflight_list()

    async def _preflight_list(self) -> tuple[str, str] | None:
        try:
            response = await self._client.get("/v1/models", headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            return "start_failed", self._unreached(exc)
        if response.status_code == 200:
            return None
        return self._refusal(response.status_code, response.content)

    def _refusal(self, status: int, body: bytes) -> tuple[str, str]:
        """The kind and the sentence for a preflight answer that is no model."""
        if status in (401, 403):
            return (
                "start_failed",
                f"{self._who()} rejected the key in {self.key_env_name} (HTTP {status}). Check the key.",
            )
        if fault_kind(status, body, FAULT_CODES) == "out_of_credit":
            return "out_of_credit", self._out_of_credit(status, body)
        return "start_failed", f"{self._who()} refused the connection: {api_error(status, body)}"

    def _read_lifecycle(self, response: Any) -> tuple[str, str] | None:
        """Read one model's `lifecycle` and `retires_at`, as `GET /v1/models/{id}` gives them."""
        try:
            data = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        day = _day(data.get("retires_at"))
        if data.get("lifecycle") == "retired":
            return "model_unavailable", self._retired(day)
        if day:
            self._notice = (
                f"the language model {self.model_name!r} retires on {day} at {self._who()}. "
                f"Choose another under models.chat.model before then."
            )
        return None

    async def _confirm_gone(self) -> tuple[str, str] | None:
        """After a 404 reply, ask the per-model path whether the model is gone."""
        try:
            response = await self._client.get(self._model_path(), headers=self.headers())
        except Exception:  # noqa: BLE001 - the reply's own error stands
            return None
        if response.status_code == 404 and self.at_vendor:
            return "model_unavailable", self._unknown(404)
        if response.status_code != 200:
            return None
        return self._read_lifecycle(response)

    def _fail(self, kind: str, reason: str) -> None:
        self._fault = reason
        self._fault_kind = kind
        # INFO: the engine prints the reason once, from health().
        log.info("anthropic: %s", reason)

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
            healthy=self._started,
            endpoint=self.endpoint,
            key_env=self.key_env_name,
        )

    def health(self) -> Health:
        if self._fault:
            return Health(ok=False, detail=self._fault, faults=(self._fault_kind,))
        if self._reply_fault is not None:
            kind, detail = self._reply_fault
            return Health(ok=False, detail=detail, faults=(kind,))
        return Health(ok=True, detail=self._notice)

    # ---------------------------------------------------------------- reply

    def _unsent(self) -> ReplyDone | None:
        """The final for a reply that is never sent, or None when it may be."""
        gone = self._fault if self._fault_kind == "model_unavailable" else None
        if gone is None and self._reply_fault is not None and self._reply_fault[0] == "model_unavailable":
            gone = self._reply_fault[1]
        if gone is not None:
            # The model cannot answer, so nothing is sent.
            return ReplyDone(text="", stop_reason="error", model=self.model_name, error=gone)
        if not self._started or self._client is None:
            return ReplyDone(text="", stop_reason="error", error="the anthropic plugin was not started")
        return None

    def _settle(self, error: str | None) -> None:
        """Log a sent reply's error, or end an `out_of_credit` once a reply succeeds."""
        if error:
            self.last_error = error
            # INFO: the engine prints the error once, from ReplyDone.error.
            log.info("anthropic: %s", error)
        elif self._reply_fault is not None and self._reply_fault[0] == "out_of_credit":
            # A reply went through, so the account is paid for again.
            self._reply_fault = None

    async def reply(self, prompt: Prompt) -> AsyncIterator[ReplyEvent]:
        unsent = self._unsent()
        if unsent is not None:
            yield unsent
            return

        reading = _Reading()
        error: str | None = None
        failed: tuple[int, bytes] | None = None
        try:
            async with self._client.stream(
                "POST", "/v1/messages", headers=self.headers(), json=self.body(prompt)
            ) as response:
                if response.status_code != 200:
                    failed = (response.status_code, await response.aread())
                else:
                    async for event, data in sse_events(response.aiter_lines()):
                        if not isinstance(data, dict):
                            continue
                        given = reading.take(event, data)
                        if given is not None:
                            yield given
                        if reading.ended:
                            break
        except Exception as exc:  # noqa: BLE001 - the reply ends with what was heard
            error = f"{type(exc).__name__}: {exc}"
            self._forget_credit()

        if failed is not None:
            error = await self._failure(*failed)
        error = error or reading.error
        self._settle(error)
        yield reading.done(error, self.model_name)

    async def _failure(self, status: int, body: bytes) -> str:
        """The sentence for a failed request, and the kind it leaves in `health()`."""
        if fault_kind(status, body, FAULT_CODES) == "out_of_credit":
            detail = self._out_of_credit(status, body)
            self._reply_fault = ("out_of_credit", detail)
            return detail
        if status == 404:
            found = await self._confirm_gone()
            if found is not None and found[0] == "model_unavailable":
                self._reply_fault = found
                return found[1]
        self._forget_credit()
        return api_error(status, body)

    def _forget_credit(self) -> None:
        """End a kept `out_of_credit` once a request fails for a reason that names no kind."""
        if self._reply_fault is not None and self._reply_fault[0] == "out_of_credit":
            # News of the network or the server, never of the account.
            self._reply_fault = None
