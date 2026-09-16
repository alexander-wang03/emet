"""What the two HTTP language model providers share.

Both Anthropic and OpenAI speak JSON over HTTPS and stream replies as
Server-Sent Events, so the client, the event parser and the error reading are
written once here. The vendors differ in headers, body shape and event names,
and those stay in each provider's own file.

`httpx` is imported lazily, inside `open_client()`, so that a body with no
language model never needs it and the `anthropic` and `openai` extras stay
optional. Tests inject an `httpx.MockTransport` through `transport`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

__all__ = ["open_client", "sse_events", "api_error"]

log = logging.getLogger("emet_providers.http")


def open_client(base_url: str, *, timeout_s: float, transport: Any = None, extra: str) -> Any:
    """An `httpx.AsyncClient`, or a reason there cannot be one.

    Returns the client, or a string naming what to install. Two return types
    rather than an exception because the caller reports this through
    `health()`, the way every plugin reports a start failure.
    """
    try:
        import httpx
    except ImportError:
        return (
            f"the httpx library is not installed. It is an optional dependency: "
            f"install `emet-providers[{extra}]`."
        )
    return httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, Any]]:
    """Parse Server-Sent Events from a stream of lines.

    Yields `(event, data)` per event: `event` is the `event:` field, or the
    data's own `type` when the vendor sends none, or an empty string. `data`
    is the parsed JSON, or the raw text under `{"raw": ...}` when it is not
    JSON (`[DONE]`, for one vendor). Comment lines are skipped, multi-line
    `data:` fields are joined, and a final event without a trailing blank
    line is still delivered.
    """
    event: str | None = None
    data_lines: list[str] = []

    def flush() -> tuple[str, Any] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            data: Any = json.loads(raw)
        except ValueError:
            data = {"raw": raw}
        name = event or (data.get("type") if isinstance(data, dict) else None) or ""
        return str(name), data

    async for line in lines:
        if line == "":
            item = flush()
            event, data_lines = None, []
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    item = flush()
    if item is not None:
        yield item


def api_error(status: int, body: bytes | str) -> str:
    """One line for a non-200 response, quoting the vendor's message if any.

    Both vendors answer errors with `{"error": {"type"|"code", "message"}}`;
    anything else is quoted as text, trimmed.
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            kind = err.get("type") or err.get("code") or "error"
            return f"HTTP {status} {kind}: {err.get('message', '')}".rstrip(": ")
    except ValueError:
        pass
    return f"HTTP {status}: {text.strip()[:200]}"
