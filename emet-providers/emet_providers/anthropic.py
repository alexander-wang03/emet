"""The language model through Anthropic's Messages API.

One of two vendors behind the `emet.llm` seam, shipped together so that the
seam is proven by its second implementation rather than shaped by its first.
Nothing above knows which one is running.

**Protocol, from Anthropic's API reference on 2026-09-15.** `POST
/v1/messages` with `x-api-key`, `anthropic-version: 2023-06-01` and
`stream: true`. The reply is Server-Sent Events: `message_start` (the model
that answered, input tokens), `content_block_start` for each text or
`tool_use` block, `content_block_delta` carrying `text_delta` or
`input_json_delta` pieces, `content_block_stop`, then `message_delta` with the
`stop_reason` and output tokens, then `message_stop`. Stop reasons: `end_turn`,
`stop_sequence`, `tool_use`, `max_tokens`, `refusal`, `pause_turn`. Tool
results go back as `tool_result` blocks in a user turn. `GET /v1/models` lists
what the key may use, and a bad key is HTTP 401 at any endpoint.

**What is deliberately not sent.** No `temperature`: the current models
reject it. No `thinking` block: Claude Opus 5 thinks adaptively when the
field is omitted, and the raw reasoning is never returned anyway. Effort is
the knob that matters for a speaking robot and it is a param, unset by
default because not every model accepts it.

**Refusals.** A safety classifier may decline with HTTP 200 and
`stop_reason: "refusal"`. That is reported as `refusal`, and the engine says
something rather than nothing. Anthropic recommends opting into server-side
fallbacks so a declined request is re-run on another model inside the same
call; that is `params.fallbacks`, off unless the owner turns it on, because it
is a beta and it changes which model answers.

Params, all optional, from the body's `models.chat.params`:

    effort       "low" | "medium" | "high" | "xhigh" | "max". Sent as
                 `output_config.effort`. `low` suits a spoken reply; unset
                 leaves the model's default.
    fallbacks    "default" (Anthropic picks a fallback by refusal category,
                 beta header `server-side-fallback-2026-07-01`) or a list of
                 model names (beta header `server-side-fallback-2026-06-01`).
    url          the API root, for a proxy. Default https://api.anthropic.com
    timeout_s    seconds per request, connect and read. Default 60.
    preflight    bool, default true. `GET /v1/models` in `start()` to check
                 the key and the network.

Dependencies: `httpx` (BSD-3-Clause), through the `anthropic` extra. Not the
vendor SDK: the wire protocol is small, the same client serves both vendors,
and a vendor SDK is the vendor's shape arriving by another door.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Mapping, Sequence

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

from emet_providers._http import api_error, open_client, sse_events

__all__ = ["AnthropicLanguageModel", "DEFAULT_MODEL", "DEFAULT_KEY_ENV", "DEFAULT_URL", "API_VERSION"]

log = logging.getLogger("emet_providers.anthropic")

DEFAULT_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"

#: Anthropic's current flagship, per their model list on 2026-09-15. The soul
#: names another model when it wants one; a soul that says only
#: `provider: anthropic` gets this.
DEFAULT_MODEL = "claude-opus-5"

DEFAULT_KEY_ENV = "EMET_ANTHROPIC_KEY"

FALLBACK_BETA_DEFAULT = "server-side-fallback-2026-07-01"
FALLBACK_BETA_ARRAY = "server-side-fallback-2026-06-01"

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
        self._fault: str | None = None
        #: The last failure inside a reply, for diagnostics. Not a fault.
        self.last_error: str | None = None

        self.model_name: str = self.model or DEFAULT_MODEL
        self.key_env_name: str = self.key_env or DEFAULT_KEY_ENV
        self.url: str = str(self.params.get("url") or DEFAULT_URL).rstrip("/")
        self.timeout_s = float(self.params.get("timeout_s", 60.0))
        self.preflight = bool(self.params.get("preflight", True))
        self.effort: str | None = self.params.get("effort") or None
        self.fallbacks: Any = self.params.get("fallbacks") or None

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
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"Anthropic keys come from console.anthropic.com; export it in the "
                f"shell that runs the robot. The key is never written into a soul "
                f"or a manifest."
            )
            return
        client = open_client(self.url, timeout_s=self.timeout_s, transport=self._transport, extra="anthropic")
        if isinstance(client, str):
            self._fail(client)
            return
        self._client = client
        if self.preflight:
            reason = await self._preflight()
            if reason:
                self._fail(reason)
                return
        self._started = True
        log.info("anthropic: ready, model %s", self.model_name)

    async def _preflight(self) -> str | None:
        try:
            response = await self._client.get("/v1/models", headers=self.headers())
        except Exception as exc:  # noqa: BLE001 - reported through health, not raised
            return (
                f"could not reach {self.url}: {type(exc).__name__}: {exc}. "
                f"The language model needs a network; the wake word does not."
            )
        if response.status_code == 200:
            return None
        if response.status_code in (401, 403):
            return (
                f"Anthropic rejected the key in {self.key_env_name} "
                f"(HTTP {response.status_code}). Check the key."
            )
        return f"Anthropic refused the connection: {api_error(response.status_code, response.content)}"

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.error("anthropic: %s", reason)

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
        )

    def health(self) -> Health:
        if self._fault:
            fault = "no_key" if "no key" in self._fault else "start_failed"
            return Health(ok=False, detail=self._fault, faults=(fault,))
        return Health()

    # ---------------------------------------------------------------- reply

    async def reply(self, prompt: Prompt) -> AsyncIterator[ReplyEvent]:
        if not self._started or self._client is None:
            yield ReplyDone(text="", stop_reason="error", error="the anthropic plugin was not started")
            return

        text: list[str] = []
        calls: list[ToolCall] = []
        blocks: dict[int, dict[str, Any]] = {}
        stop = "end"
        model: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        error: str | None = None

        try:
            async with self._client.stream(
                "POST", "/v1/messages", headers=self.headers(), json=self.body(prompt)
            ) as response:
                if response.status_code != 200:
                    error = api_error(response.status_code, await response.aread())
                else:
                    async for event, data in sse_events(response.aiter_lines()):
                        if not isinstance(data, dict):
                            continue
                        if event == "message_start":
                            message = data.get("message") or {}
                            model = message.get("model") or model
                            usage = message.get("usage") or {}
                            if isinstance(usage.get("input_tokens"), int):
                                input_tokens = usage["input_tokens"]
                        elif event == "content_block_start":
                            block = data.get("content_block") or {}
                            index = int(data.get("index", 0))
                            if block.get("type") == "tool_use":
                                blocks[index] = {
                                    "type": "tool_use",
                                    "id": str(block.get("id", "")),
                                    "name": str(block.get("name", "")),
                                    "json": "",
                                }
                            elif block.get("type") == "text":
                                blocks[index] = {"type": "text"}
                        elif event == "content_block_delta":
                            delta = data.get("delta") or {}
                            index = int(data.get("index", 0))
                            if delta.get("type") == "text_delta":
                                piece = str(delta.get("text", ""))
                                if piece:
                                    text.append(piece)
                                    yield TextDelta(piece)
                            elif delta.get("type") == "input_json_delta":
                                if index in blocks and blocks[index]["type"] == "tool_use":
                                    blocks[index]["json"] += str(delta.get("partial_json", ""))
                        elif event == "content_block_stop":
                            index = int(data.get("index", 0))
                            block = blocks.pop(index, None)
                            if block and block["type"] == "tool_use":
                                try:
                                    arguments = json.loads(block["json"]) if block["json"] else {}
                                except ValueError:
                                    arguments = {}
                                call = ToolCall(id=block["id"], name=block["name"], arguments=arguments)
                                calls.append(call)
                                yield call
                        elif event == "message_delta":
                            delta = data.get("delta") or {}
                            reason = delta.get("stop_reason")
                            if reason:
                                stop = STOP_REASONS.get(str(reason), "end")
                            usage = data.get("usage") or {}
                            if isinstance(usage.get("output_tokens"), int):
                                output_tokens = usage["output_tokens"]
                        elif event == "message_stop":
                            break
                        elif event == "error":
                            err = data.get("error") or {}
                            error = f"{err.get('type', 'error')}: {err.get('message', '')}"
                            break
        except Exception as exc:  # noqa: BLE001 - the reply ends with what was heard
            error = f"{type(exc).__name__}: {exc}"

        if error:
            stop = "error"
            self.last_error = error
            log.warning("anthropic: %s", error)
        yield ReplyDone(
            text="".join(text),
            stop_reason=stop,
            tool_calls=tuple(calls),
            model=model or self.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
        )
