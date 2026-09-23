"""Speech recognition through Deepgram's streaming API.

The first real provider, and the second thing behind the `emet.stt` seam:
the mock shipped first so that this file would be written against the
contract rather than the contract against this file. Nothing above it knows
it exists. A soul says `provider: deepgram`, the engine asks the registry,
and frames flow.

**Protocol, verified against Deepgram's documentation on 2026-09-12.**
Audio goes to `wss://api.deepgram.com/v1/listen` as binary WebSocket frames
of 16-bit little-endian PCM, with `encoding=linear16` and the `sample_rate`
that parameter requires. The key travels in the handshake as
`Authorization: Token <key>`; a bad key is refused at the handshake with
HTTP 401. Results come back as JSON text frames of `"type": "Results"`, each
carrying one alternative's `transcript` and `confidence` and an `is_final`
flag. A segment is revised through interim messages until one arrives with
`is_final: true`, and the utterance is the finalised segments joined in order
plus the latest interim of the segment still open. `{"type": "CloseStream"}`
makes the server flush what it holds, send the remaining finals, send one
`"type": "Metadata"` message and close. A connection that receives neither
audio nor `{"type": "KeepAlive"}` for ten seconds is closed by the server.

**One connection per utterance.** The engine feeds this plugin only between
a wake and an endpoint, and Deepgram closes an idle connection after ten
seconds, so a session-long socket would need a keepalive task ticking through
every silence. Opening a socket when the first frame of an utterance arrives
costs a handshake at the start of each turn, and it costs it in the
background: `feed()` queues frames and returns, a task connects and drains
the queue, and nothing in the capture loop waits on the network. The engine
does the endpointing, so Deepgram's `speech_final` is read and ignored.

**What fails loudly, and where.** `start()` reads the key and refuses to
report healthy without one, naming the variable to set. It then opens and
closes one connection, so a rejected key or an unreachable network is known
at boot, in the terms the owner can act on, rather than at the first
question. A failure during an utterance is logged and the final carries what
was heard before it, plus the reason in `Transcript.error`; a transient
network fault costs one turn and leaves the plugin healthy. The engine reads
that reason and says the soul's failure line, so a robot whose hotspot died
mid-conversation tells the person instead of standing there.

Params, all optional, from the body's `models.stt.params`:

    query        mapping of extra Deepgram query parameters (`language`,
                 `keyterm`, `endpointing`, ...), passed through as strings.
                 `encoding`, `sample_rate` and `channels` are fixed by the
                 audio format and cannot be overridden here.
    timeout_s    seconds `finish()` waits for the final after CloseStream,
                 and the whole of the person's wait: a socket that will not
                 unwind is abandoned rather than waited on. Default 5.
    open_timeout_s
                 seconds to allow the handshake. Default 10.
    preflight    bool, default true. Open and close a connection in
                 `start()` to check the key and the network.
    url          the WebSocket URL, for a self-hosted deployment.

Dependencies: `websockets` (BSD-3-Clause), through the `deepgram` extra.
Nothing from Deepgram's SDK; the wire protocol is small and this file speaks
it directly, which is also what keeps the dependency list to one package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from emet_sdk.plugin import TranscriberPlugin
from emet_sdk.types import AudioFormat, Health, Transcript, TranscriberDescriptor

__all__ = ["DeepgramTranscriber", "DEFAULT_MODEL", "DEFAULT_KEY_ENV", "DEFAULT_URL"]

log = logging.getLogger("emet_providers.deepgram")

DEFAULT_URL = "wss://api.deepgram.com/v1/listen"

#: Deepgram's current general model, verified against their model list on
#: 2026-09-12. `nova-2` and its variants are still served; `flux-general-en`
#: is a different protocol (`/v2/listen`, its own turn events) and is not
#: reachable through this plugin.
DEFAULT_MODEL = "nova-3"

#: Where the key is read from when the soul names no `key_env`. The name the
#: reference soul uses, so a soul that says only `provider: deepgram` works.
DEFAULT_KEY_ENV = "EMET_DEEPGRAM_KEY"

#: Query parameters this plugin always sends. `interim_results` is the whole
#: point of a streaming seam; `punctuate` and `smart_format` are what a
#: language model downstream wants to read.
FIXED_QUERY: Mapping[str, str] = {
    "encoding": "linear16",
    "channels": "1",
    "interim_results": "true",
    "punctuate": "true",
    "smart_format": "true",
}

#: Keys the body may not override through `params.query`: the audio format
#: fixes them, and a mismatch is the silent failure the descriptor exists to
#: prevent.
FORMAT_KEYS = frozenset({"encoding", "sample_rate", "channels"})

_CLOSE_STREAM = json.dumps({"type": "CloseStream"})


@dataclass
class _Utterance:
    """Everything one turn's connection accumulates."""

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    finals: list[str] = field(default_factory=list)
    final_confidences: list[float] = field(default_factory=list)
    interim: str = ""
    interim_confidence: float = 1.0
    reported: str = ""
    error: BaseException | None = None
    ws: Any = None

    @property
    def text(self) -> str:
        parts = [*self.finals]
        if self.interim:
            parts.append(self.interim)
        return " ".join(p.strip() for p in parts if p.strip())


def _swallow(task: "asyncio.Task") -> None:
    """Read a cut-loose task's exception so asyncio does not warn about it.

    The task was abandoned on purpose, its words are already lost, and the
    turn has moved on. Nobody is going to look at what it raised.
    """
    if not task.cancelled():
        task.exception()


def _is_closed(exc: BaseException) -> bool:
    """Whether an exception is the connection closing.

    Duck-typed on the `rcvd` and `sent` attributes every
    `websockets.exceptions.ConnectionClosed` carries, so this module never
    imports the library at module scope and a test can stand in for it.
    """
    return hasattr(exc, "rcvd") and hasattr(exc, "sent")


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status of a rejected handshake, if this exception has one."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return int(code) if isinstance(code, int) else None


class DeepgramTranscriber(TranscriberPlugin):
    """Streaming recognition through Deepgram, one connection per utterance."""

    provider = "deepgram"

    def __init__(
        self,
        config: Mapping[str, Any],
        fmt: AudioFormat,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(config, fmt)
        #: Something with the shape of `websockets.asyncio.client.connect`:
        #: `await connect(url, additional_headers=..., open_timeout=...)`
        #: returns a connection with `send`, `recv` and `close`. Injected by
        #: tests; found by `start()` otherwise.
        self._connect = connect
        self._key: str | None = None
        self._started = False
        self._fault: str | None = None
        self._utterance: _Utterance | None = None
        #: The last failure inside an utterance, for diagnostics. Not a fault:
        #: a dropped connection is a bad moment, not a broken plugin.
        self.last_error: str | None = None

        self.model_name: str = self.model or DEFAULT_MODEL
        self.key_env_name: str = self.key_env or DEFAULT_KEY_ENV
        self.url: str = str(self.params.get("url") or DEFAULT_URL)
        self.timeout_s = float(self.params.get("timeout_s", 5.0))
        self.open_timeout_s = float(self.params.get("open_timeout_s", 10.0))
        self.preflight = bool(self.params.get("preflight", True))

    # ------------------------------------------------------------- request

    def query(self) -> dict[str, str]:
        """The query string, as a mapping. Format keys win over `params.query`."""
        extra = {
            str(k): str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in (self.params.get("query") or {}).items()
            if str(k) not in FORMAT_KEYS
        }
        return {
            **FIXED_QUERY,
            **extra,
            "sample_rate": str(self.format.sample_rate),
            "model": self.model_name,
        }

    def request_url(self) -> str:
        return f"{self.url}?{urlencode(self.query())}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._key}"}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        # The key first, then the library: an owner without a key is the
        # common case, and the message for it should not depend on which
        # extras happen to be installed.
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"Deepgram keys come from console.deepgram.com; export it in the "
                f"shell that runs the robot. The key is never written into a soul "
                f"or a manifest."
            )
            return

        if self._connect is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                self._fail(
                    "the websockets library is not installed. It is an optional "
                    "dependency: install `emet-providers[deepgram]`."
                )
                return
            self._connect = connect

        if self.preflight:
            reason = await self._preflight()
            if reason:
                self._fail(reason)
                return

        self._started = True
        log.info("deepgram: ready, model %s at %d Hz", self.model_name, self.format.sample_rate)

    async def _preflight(self) -> str | None:
        """Open one connection and close it. Returns the reason it could not."""
        assert self._connect is not None
        try:
            ws = await asyncio.wait_for(
                self._connect(
                    self.request_url(),
                    additional_headers=self._headers(),
                    open_timeout=self.open_timeout_s,
                ),
                timeout=self.open_timeout_s + 1.0,
            )
        except asyncio.TimeoutError:
            return (
                f"could not reach {self.url} within {self.open_timeout_s:.0f} s. "
                f"Speech recognition needs a network; the wake word does not."
            )
        except Exception as exc:  # noqa: BLE001 - every failure here is reported, not raised
            code = _status_code(exc)
            if code in (401, 403):
                return (
                    f"Deepgram rejected the key in {self.key_env_name} (HTTP {code}). "
                    f"Check the key, and that it has the transcription scope."
                )
            if code is not None:
                return f"Deepgram refused the connection with HTTP {code}: {exc}"
            return (
                f"could not reach {self.url}: {type(exc).__name__}: {exc}. "
                f"Speech recognition needs a network; the wake word does not."
            )
        try:
            await ws.send(_CLOSE_STREAM)
            try:
                await asyncio.wait_for(self._drain(ws), timeout=3.0)
            except asyncio.TimeoutError:
                pass
        finally:
            await self._close(ws)
        return None

    async def _drain(self, ws: Any) -> None:
        """Read until the server closes. Used after CloseStream."""
        while True:
            try:
                await ws.recv()
            except Exception as exc:  # noqa: BLE001
                if _is_closed(exc):
                    return
                raise

    async def _close(self, ws: Any) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - closing a closed socket is not news
            pass

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.error("deepgram: %s", reason)

    async def shutdown(self) -> None:
        self._started = False
        utterance, self._utterance = self._utterance, None
        if utterance is not None and utterance.task is not None and not utterance.task.done():
            utterance.task.cancel()
            try:
                await utterance.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------ reporting

    def describe(self) -> TranscriberDescriptor:
        return TranscriberDescriptor(
            provider=self.provider,
            model=self.model_name,
            streaming=True,
            sample_rate=self.format.sample_rate,
            healthy=self._started,
        )

    def health(self) -> Health:
        if self._fault:
            fault = "no_key" if "no key" in self._fault else "start_failed"
            return Health(ok=False, detail=self._fault, faults=(fault,))
        return Health()

    # ---------------------------------------------------------- recognition

    async def feed(self, frame: bytes) -> Transcript | None:
        if not self._started:
            return None
        utterance = self._utterance
        if utterance is None:
            utterance = self._utterance = _Utterance()
            utterance.task = asyncio.create_task(self._pump(utterance))
        if utterance.task is not None and not utterance.task.done():
            utterance.queue.put_nowait(frame)
        text = utterance.text
        if text == utterance.reported:
            return None
        utterance.reported = text
        return Transcript(text=text, final=False, confidence=utterance.interim_confidence)

    async def finish(self) -> Transcript:
        utterance, self._utterance = self._utterance, None
        if utterance is None:
            # Nothing was fed, so nothing was opened. An empty final is the
            # honest answer and costs no round trip.
            return Transcript(text="", final=True, confidence=1.0)

        # This turn's failure, apart from `last_error`, which outlives the
        # utterance. The transcript says what went wrong with *these* words,
        # so a turn that went fine after one that did not is not blamed for it.
        error: str | None = None
        utterance.queue.put_nowait(None)
        if utterance.task is not None:
            # `asyncio.wait`, not `wait_for`. `wait_for` cancels the task on a
            # timeout and then waits for that cancellation to finish, and a
            # socket with no network behind it does not unwind promptly: on
            # the reference body with mobile data off, five seconds of waiting
            # for the final became fifteen before the robot said anything.
            # The person's wait is bounded here and the socket is cut loose to
            # die on its own time.
            done, _ = await asyncio.wait({utterance.task}, timeout=self.timeout_s)
            if not done:
                utterance.task.cancel()
                utterance.task.add_done_callback(_swallow)
                error = f"no final within {self.timeout_s:.0f} s of CloseStream"
                self.last_error = error
                log.warning("deepgram: %s; returning what was heard", error)
            else:
                try:
                    utterance.task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported, and the words so far returned
                    error = f"{type(exc).__name__}: {exc}"
                    self.last_error = error
                    log.warning("deepgram: %s; returning what was heard", error)

        confidences = utterance.final_confidences or [utterance.interim_confidence]
        confidence = max(0.0, min(1.0, sum(confidences) / len(confidences)))
        return Transcript(text=utterance.text, final=True, confidence=confidence, error=error)

    async def _pump(self, utterance: _Utterance) -> None:
        """Connect, then send frames and receive results until both sides end."""
        assert self._connect is not None
        ws = await self._connect(
            self.request_url(),
            additional_headers=self._headers(),
            open_timeout=self.open_timeout_s,
        )
        utterance.ws = ws
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._send_all(utterance, ws))
                group.create_task(self._receive_all(utterance, ws))
        finally:
            await self._close(ws)

    async def _send_all(self, utterance: _Utterance, ws: Any) -> None:
        while True:
            frame = await utterance.queue.get()
            if frame is None:
                # A text frame, as the documentation insists. Binary would be
                # taken for audio.
                await ws.send(_CLOSE_STREAM)
                return
            await ws.send(frame)

    async def _receive_all(self, utterance: _Utterance, ws: Any) -> None:
        while True:
            try:
                message = await ws.recv()
            except Exception as exc:  # noqa: BLE001
                if _is_closed(exc):
                    return
                raise
            if isinstance(message, bytes):
                continue
            try:
                data = json.loads(message)
            except ValueError:
                log.debug("deepgram: ignoring a frame that is not JSON")
                continue
            kind = data.get("type")
            if kind == "Metadata":
                # The last thing the server says after CloseStream.
                return
            if kind != "Results":
                continue
            alternatives = (data.get("channel") or {}).get("alternatives") or []
            if not alternatives:
                continue
            best = alternatives[0]
            transcript = str(best.get("transcript") or "")
            confidence = best.get("confidence")
            confidence = float(confidence) if isinstance(confidence, (int, float)) else 1.0
            if data.get("is_final"):
                if transcript.strip():
                    utterance.finals.append(transcript)
                    utterance.final_confidences.append(confidence)
                utterance.interim = ""
                utterance.interim_confidence = 1.0
            else:
                utterance.interim = transcript
                utterance.interim_confidence = confidence
