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
use. The output cap is `max_completion_tokens`; servers that predate the
rename take `max_tokens`, and `params.max_tokens_field` says which.

Params, all optional, from the body's `models.chat.params`:

    url               the API root. Default https://api.openai.com/v1; any
                      compatible server works.
    timeout_s         seconds per request, connect and read. Default 60.
    preflight         bool, default true. `GET /models` in `start()` to check
                      the key and the network.
    max_tokens_field  "max_completion_tokens" (default) or "max_tokens".
    organization      sent as `OpenAI-Organization` when set.

Dependencies: `httpx` (BSD-3-Clause), through the `openai` extra. Not the
vendor SDK, for the reason given in `anthropic.py`.
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

__all__ = ["OpenAILanguageModel", "DEFAULT_MODEL", "DEFAULT_KEY_ENV", "DEFAULT_URL"]

log = logging.getLogger("emet_providers.openai")

DEFAULT_URL = "https://api.openai.com/v1"

#: The model OpenAI's list describes as balancing intelligence and cost, on
#: 2026-09-15. A speaking robot wants the answer soon more than it wants the
#: largest model; the soul names another when it disagrees.
DEFAULT_MODEL = "gpt-5.6-terra"

DEFAULT_KEY_ENV = "EMET_OPENAI_KEY"

#: The vendor's finish reasons, in the seam's words.
FINISH_REASONS: Mapping[str, str] = {
    "stop": "end",
    "length": "length",
    "tool_calls": "tool",
    "function_call": "tool",
    "content_filter": "refusal",
}


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
        self.last_error: str | None = None

        self.model_name: str = self.model or DEFAULT_MODEL
        self.key_env_name: str = self.key_env or DEFAULT_KEY_ENV
        self.url: str = str(self.params.get("url") or DEFAULT_URL).rstrip("/")
        self.timeout_s = float(self.params.get("timeout_s", 60.0))
        self.preflight = bool(self.params.get("preflight", True))
        self.max_tokens_field: str = str(self.params.get("max_tokens_field") or "max_completion_tokens")
        self.organization: str | None = self.params.get("organization") or None

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
        if prompt.tools:
            body["tools"] = _tools(prompt.tools)
        return body

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"OpenAI keys come from platform.openai.com; export it in the shell "
                f"that runs the robot. The key is never written into a soul or a "
                f"manifest."
            )
            return
        client = open_client(self.url, timeout_s=self.timeout_s, transport=self._transport, extra="openai")
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
        log.info("openai: ready, model %s at %s", self.model_name, self.url)

    async def _preflight(self) -> str | None:
        try:
            response = await self._client.get("/models", headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            return (
                f"could not reach {self.url}: {type(exc).__name__}: {exc}. "
                f"The language model needs a network; the wake word does not."
            )
        if response.status_code == 200:
            return None
        if response.status_code in (401, 403):
            return (
                f"the server at {self.url} rejected the key in {self.key_env_name} "
                f"(HTTP {response.status_code}). Check the key."
            )
        return f"the server at {self.url} refused the connection: {api_error(response.status_code, response.content)}"

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.error("openai: %s", reason)

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
            yield ReplyDone(text="", stop_reason="error", error="the openai plugin was not started")
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
                    error = api_error(response.status_code, await response.aread())
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

        if error:
            stop = "error"
            self.last_error = error
            log.warning("openai: %s", error)
        yield ReplyDone(
            text="".join(text),
            stop_reason=stop,
            tool_calls=tuple(calls),
            model=model or self.model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
        )
