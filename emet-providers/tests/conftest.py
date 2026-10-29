"""What every provider test may assume about its surroundings.

**No real voices.** The Piper plugin looks for a voice in `voices_dir`, then
in `$XDG_DATA_HOME/emet/voices` or `~/.local/share/emet/voices`, then in
`/etc/emet/voices`. On the reference body the reference voice sits in the
second of those, and a test asserting "no model, so unhealthy" found it,
loaded it through the stand-in library and failed there alone, since no CI
machine has a voice downloaded. Both default places are pointed at the
test's own temporary directory, so a test sees only the voices it made.

**What a live test received, kept.** With `EMET_LIVE_RECORD=<dir>` beside
`EMET_LIVE_TESTS=1`, the `live_record` fixture hands a live test a
`LiveRecord`, and the test routes its plugin through it: `transport()` for
the HTTP providers, `connect()` for the Deepgram transcriber. Each test
leaves one JSON file in the directory (`"format": "emet-live-record/1"`),
written at teardown whether the test passed or not, since a vendor that
changed its stream is the case worth keeping. The file holds what came back
as it came: every WebSocket message with its time, or the response's bytes
chunk by chunk. Request headers are never read, response headers are kept
only from `KEPT_HEADERS`, and a file that would hold the value of any
`*_KEY` variable in the environment is refused before a byte is written.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from emet_providers import cassette, piper

LIVE_RECORD = "EMET_LIVE_RECORD"
RECORD_FORMAT = "emet-live-record/1"

#: Response headers worth keeping: what the body is, when, which request,
#: and which model the vendor says answered. None of them carries a
#: credential; cookies and everything else are dropped. Which of these each
#: vendor sends is unverified: the list only keeps what is there.
KEPT_HEADERS = frozenset(
    {
        "content-type",
        "content-encoding",
        "date",
        "request-id",
        "x-request-id",
        "openai-model",
        "openai-version",
        "openai-processing-ms",
        "dg-model-name",
        "dg-model-uuid",
        "dg-char-count",
        "dg-request-id",
    }
)

#: A key shorter than this is too short to search for without matching by
#: chance. Every vendor key seen is far longer.
_SHORTEST_KEY = 8


@pytest.fixture(autouse=True)
def no_developer_voices(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setattr(piper, "SYSTEM_VOICES", tmp_path / "machine-voices")


def _chunk(data: bytes, t: float, audio: bool) -> dict[str, Any]:
    if not audio:
        try:
            return {"t": t, "text": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    return {"t": t, "bytes_b64": base64.b64encode(data).decode("ascii")}


def _request_body(request: Any) -> Any:
    try:
        content = request.content
    except Exception:  # noqa: BLE001 - a streamed body is not kept
        return None
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        return content.decode("utf-8", "replace")


class LiveRecord:
    """Everything one live test received from a vendor."""

    def __init__(self, directory: Path, test: str) -> None:
        self.directory = directory
        self.test = test
        #: Set by the test: whose service this is, and the model the vendor
        #: reported. A Deepgram connection's model is read from its own
        #: messages when the test sets none.
        self.vendor: str | None = None
        self.model: Any = None
        self.model_asked: str | None = None
        self.exchanges: list[dict[str, Any]] = []
        self.connections: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()

    def _t(self) -> float:
        return round(time.perf_counter() - self._t0, 4)

    @property
    def used(self) -> bool:
        return bool(self.exchanges or self.connections)

    # ------------------------------------------------------------ websocket

    def connect(self, inner: Any) -> Any:
        """A `connect` for `DeepgramTranscriber` that keeps every message."""
        return cassette.recording_connect(inner, self.connections)

    # ----------------------------------------------------------------- http

    def transport(self, inner: Any = None) -> Any:
        """An `httpx` transport that keeps every response as it came.

        `inner` is the transport that really sends: the network's own by
        default, a `MockTransport` in the tests of this mode.
        """
        import httpx

        record = self
        inner = inner if inner is not None else httpx.AsyncHTTPTransport()

        class Tee(httpx.AsyncByteStream):
            def __init__(self, stream: Any, chunks: list, audio: bool) -> None:
                self._stream = stream
                self._chunks = chunks
                self._audio = audio

            async def __aiter__(self):
                async for data in self._stream:
                    self._chunks.append(_chunk(bytes(data), record._t(), self._audio))
                    yield data

            async def aclose(self) -> None:
                await self._stream.aclose()

        class Recording(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                exchange: dict[str, Any] = {
                    "t": record._t(),
                    "method": request.method,
                    "url": cassette.safe_url(str(request.url)),
                    "body": _request_body(request),
                }
                record.exchanges.append(exchange)
                response = await inner.handle_async_request(request)
                headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in KEPT_HEADERS}
                chunks: list[dict[str, Any]] = []
                exchange.update(status=response.status_code, headers=headers, chunks=chunks)
                audio = headers.get("content-type", "").startswith("audio/")
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    stream=Tee(response.stream, chunks, audio),
                    extensions=response.extensions,
                )

            async def aclose(self) -> None:
                await inner.aclose()

        return Recording()

    # ----------------------------------------------------------------- save

    def save(self) -> Path:
        """Write the record, or refuse when it holds a key's value."""
        model = self.model
        if model is None and self.connections:
            model = cassette.model_version(m for c in self.connections for m in c["received"])
        now = datetime.datetime.now().astimezone()
        data = {
            "format": RECORD_FORMAT,
            "test": self.test,
            "vendor": self.vendor,
            "recorded": now.date().isoformat(),
            "time": now.isoformat(timespec="seconds"),
            "model": model,
            "model_asked": self.model_asked,
            "exchanges": self.exchanges,
            "connections": self.connections,
        }
        text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        for name, value in os.environ.items():
            if name.upper().endswith("_KEY") and len(value) >= _SHORTEST_KEY and value in text:
                raise RuntimeError(f"the live record of {self.test} holds the value of {name}; nothing was written")
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.test.split("::")[-1])
        path = self.directory / f"{stem}-{now.strftime('%Y%m%dT%H%M%S')}.json"
        path.write_text(text, encoding="utf-8")
        return path


@pytest.fixture
def live_record(request):
    """A `LiveRecord` when `EMET_LIVE_RECORD` names a directory, else None.

    Saved at teardown when the test reached a vendor, pass or fail.
    """
    directory = os.environ.get(LIVE_RECORD)
    if not directory:
        yield None
        return
    record = LiveRecord(Path(directory).expanduser(), request.node.nodeid)
    yield record
    if record.used:
        record.save()


@pytest.fixture
def live_recorder(request):
    """Make a `LiveRecord` for any directory: the offline tests of this mode."""
    return lambda directory: LiveRecord(Path(directory), request.node.nodeid)
