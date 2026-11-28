"""What the HTTP providers share.

Anthropic and OpenAI speak JSON over HTTPS and stream replies as Server-Sent
Events, and Deepgram's voice streams raw audio over the same client, so the
client, the event parser and the error reading are written once here. The
vendors differ in headers, body shape and event names, and those stay in
each provider's own file.

`httpx` is imported lazily, inside `open_client()`, so that a body with no
cloud provider never needs it and the `anthropic`, `openai` and `deepgram`
extras stay optional. Tests inject an `httpx.MockTransport` through
`transport`.

Three readings live here too, so that every plugin reads a failure the same
way. `fault_kind()` turns a failed response into one of `emet_sdk`'s fault
kinds through a table each plugin keeps of its own vendor's codes.
`place_of()` says where a host is (this machine, the local network, or
neither), so a server that does not answer is described where it runs.
`endpoint_of()` gives the URL a descriptor reports, with any credentials and
query taken out.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "open_client",
    "sse_events",
    "api_error",
    "error_code",
    "fault_kind",
    "place_of",
    "endpoint_of",
    "who",
    "THIS_MACHINE",
    "LOCAL_NETWORK",
]

log = logging.getLogger("emet_providers.http")

#: What `place_of()` returns for a host on the machine that runs the robot.
THIS_MACHINE = "this machine"

#: What `place_of()` returns for a host on the network the robot is on.
LOCAL_NETWORK = "the local network"


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


def _error_fields(body: bytes | str) -> tuple[str | None, str | None, str | None, bool]:
    """The `type`, `code` and message of a vendor's error body, and whether it had any.

    Anthropic and OpenAI answer `{"error": {"type", "code", "message"}}`, and
    Deepgram `{"err_code", "err_msg"}`, whose `err_code` is read as the code.
    Anthropic puts its code in `error.details.error_code`, which is read when
    `error.code` gives none.
    A field that is absent, empty or not a string reads as None.
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
    except ValueError:
        return None, None, None, False

    def word(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    if not isinstance(data, dict):
        return None, None, None, False
    err = data.get("error")
    if isinstance(err, dict):
        message = err.get("message")
        details = err.get("details")
        code = word(err.get("code")) or (word(details.get("error_code")) if isinstance(details, dict) else None)
        return word(err.get("type")), code, message if isinstance(message, str) else "", True
    if data.get("err_code") or data.get("err_msg"):
        message = data.get("err_msg")
        return None, word(data.get("err_code")), message if isinstance(message, str) else "", True
    return None, None, None, False


def api_error(status: int, body: bytes | str) -> str:
    """One line for a non-200 response, quoting the vendor's message if any.

    Anthropic and OpenAI answer errors with `{"error": {"type", "code",
    "message"}}`; Deepgram with `{"err_code", "err_msg"}`. The line names the
    error's type and then its code when the two differ, as in
    `HTTP 404 invalid_request_error, model_not_found: ...`, and one of them
    when they are the same. Anything else is quoted as text, trimmed.
    """
    kind_type, code, message, found = _error_fields(body)
    if found:
        names = [n for n in (kind_type, code) if n]
        if len(names) == 2 and names[0] == names[1]:
            names = names[:1]
        kind = ", ".join(names) or "error"
        return f"HTTP {status} {kind}: {message or ''}".rstrip(": ")
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return f"HTTP {status}: {text.strip()[:200]}"


def error_code(body: bytes | str) -> str | None:
    """The vendor's code in an error body, or its type when it gives no code.

    A failure sentence quotes it, as in `(HTTP 429, credit_balance_exhausted)`.
    None when the body names neither.
    """
    kind_type, code, _message, _found = _error_fields(body)
    return code or kind_type


def fault_kind(status: int, body: bytes | str, codes: Mapping[tuple[int, str | None], str]) -> str | None:
    """The fault kind a failed response names, read through a plugin's own table.

    `codes` maps `(status, code)` to a kind from `emet_sdk.types.FAULT_KINDS`,
    each plugin writing its table from its vendor's error page. The body is
    read as `api_error()` reads it. The first of `codes[(status, code)]`,
    `codes[(status, type)]` and `codes[(status, None)]` that exists is the
    answer, and None when none does. The kind comes from the vendor's code,
    never from the words of its message, which change.
    """
    kind_type, code, _message, _found = _error_fields(body)
    for key in (code, kind_type):
        if key is not None and (status, key) in codes:
            return codes[(status, key)]
    return codes.get((status, None))


def _bare_host(host: str) -> str:
    """The name or address in `host`, with any port and IPv6 brackets taken off."""
    host = host.strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def _address(name: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address `name` writes, an IPv4 address mapped into IPv6 read as IPv4, or None for a name."""
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def place_of(host: str) -> str | None:
    """Say whether a host is this machine, the local network, or neither.

    The answer is `THIS_MACHINE`, `LOCAL_NETWORK` or None. `host` is a
    descriptor's `host`, a name or an address, with a port and IPv6
    brackets allowed (`127.0.0.1:9380`, `[::1]:8080`, `localhost`).
    `localhost`, a loopback address and the unspecified address (`0.0.0.0`,
    `::`) are this machine. A private or link-local address, and a name
    ending `.local` (multicast DNS, RFC 6762), are the local network. The
    address classes are the standard library's `ipaddress`. Nothing is
    looked up, so a name that resolves to this machine through a hosts file
    or a DNS server is reported as None, which is how a cloud host reads.
    """
    name = _bare_host(host).lower().rstrip(".")
    if name == "localhost":
        return THIS_MACHINE
    address = _address(name) if name else None
    if address is None:
        return LOCAL_NETWORK if name.endswith(".local") else None
    if address.is_loopback or address.is_unspecified:
        # 0.0.0.0 is where a server is told to listen on every address, and
        # a URL copied from its log reaches this machine.
        return THIS_MACHINE
    if address.is_private or address.is_link_local:
        return LOCAL_NETWORK
    return None


def endpoint_of(url: str) -> str:
    """The URL a descriptor reports as `endpoint`, with credentials, query and fragment removed.

    The rest is kept as configured. `http://user:secret@127.0.0.1:9380/v1?k=1` gives `http://127.0.0.1:9380/v1`,
    so a password written into `params.url` never reaches a boot line. A URL
    with no scheme loses what comes before its last `@` ahead of the first
    `/`, as the SDK's `host` reads it, so `me:secret@127.0.0.1:9380/v1`
    gives `127.0.0.1:9380/v1`. A URL `urlsplit` cannot read keeps its
    scheme and loses everything up to its last `@`, so
    `http://me:secret@[::1/v1` gives `http://[::1/v1` and never raises.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        scheme, sep, rest = url.partition("://")
        if not sep:
            scheme, rest = "", url
        return scheme + sep + rest.rsplit("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    if not parts.netloc:
        head, slash, tail = url.split("?", 1)[0].split("#", 1)[0].partition("/")
        return head.rsplit("@", 1)[-1] + slash + tail
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def url_problem(url: str, field: str) -> str | None:
    """The refusal for a URL that cannot be read, naming the manifest's `field`, or None.

    A plugin calls it at `start()`, before any client is built, so an open
    bracket or a port that is no number is one line at boot and never a
    traceback. The URL is quoted as `endpoint_of()` gives it.
    """
    try:
        urlsplit(url).port
    except ValueError as exc:
        return f"{field} cannot be read as a URL ({exc}): {endpoint_of(url)}. Correct it in the manifest."
    return None


def who(url: str, *, default_url: str, vendor: str, host: str | None) -> str:
    """Who a plugin's failure sentence names as having answered.

    The vendor's name (`OpenAI`) when the plugin's `url` is its module's
    `DEFAULT_URL`, both read with any trailing `/` stripped, and
    `the server at {host}` for any other URL, since a local server or a
    gateway behind the same plugin is not the vendor. With no host it names
    the URL as `endpoint_of()` gives it, so no password is printed.
    """
    if url.rstrip("/") == default_url.rstrip("/"):
        return vendor
    return f"the server at {host or endpoint_of(url)}"
