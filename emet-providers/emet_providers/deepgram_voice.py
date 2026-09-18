"""The cloud voice, through Deepgram's Aura text-to-speech API.

The opt-in the design reserves (`DESIGN.md` section 14): synthesis is local
by default, and a soul that wants a better voice than a Pi can produce names
this one and pays per character. It ships beside the local voice for the
same reason the language models shipped as a pair: a seam with one
implementation is untested as a seam, and a cloud voice is the shape most
likely to pull the engine out of true if nothing else stood against it.

**Protocol, verified against Deepgram's API reference on 2026-09-15.**
`POST https://api.deepgram.com/v1/speak` with `Authorization: Token <key>`
and a JSON body `{"text": ...}`. The voice is the `model` query parameter
(`aura-2-thalia-en` here unless the soul says otherwise; Aura-2 is the
current family, Aura-1 names such as `aura-asteria-en` still answer).
`encoding=linear16` with `container=none` returns raw 16-bit little-endian
mono PCM with no header, at a `sample_rate` of 8000, 16000, 24000 (the
default), 32000 or 48000. `speed` is a multiplier on the speaking rate,
which is where the soul's `voice.rate` goes. The audio is streamed back as
it is produced, so playback can start on the first bytes. Text is limited
to 2000 characters a request (HTTP 413 beyond it); a bad key is HTTP 401
with `{"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials."}`,
checked live with a bogus key on 2026-09-15.

**Pricing, from the pricing page on 2026-09-15.** Aura-2 is $0.030 per
thousand characters pay as you go, Aura-1 $0.015. A two-sentence reply is
around 150 characters, so a thousand replies cost about $4.50. Flux TTS, the
conversation-aware family launched 2026-08-12, is $0.045 per thousand and
speaks a different protocol (`/v2/speak`, raw audio over WebSocket only, with
its own turn and interrupt messages); it is in the deferred register rather
than in this file.

**One request per sentence.** The engine hands over a sentence at a time and
plays each as its audio completes, so a request per sentence is what lets
the first sentence be heard while the language model is still writing the
second. A reply longer than the limit is split at sentence and then word
boundaries and sent as several requests in order.

**What fails loudly, and where.** `start()` reads the key and refuses to
report healthy without one, naming the variable. It then synthesises one
short word and discards the audio, so a rejected key, an unreachable
network, or a voice name that does not exist is known at boot in the owner's
terms rather than at the first reply. That preflight costs six characters,
which is to say nothing; `params.preflight: false` skips it. A failure
during a sentence raises `PluginError` after the audio so far, and the
engine goes on with the next sentence.

Params, all optional, from the body's `models.tts.params`:

    sample_rate   8000, 16000, 24000, 32000 or 48000. Default 24000. The
                  engine opens the speaker at this rate.
    query         mapping of extra query parameters, passed through as
                  strings. `model`, `encoding`, `container` and
                  `sample_rate` are fixed by this plugin and cannot be
                  overridden here.
    url           the endpoint, for a self-hosted deployment.
                  Default https://api.deepgram.com/v1/speak
    timeout_s     seconds per request, connect and read. Default 30.
    preflight     bool, default true.

Dependencies: `httpx` (BSD-3-Clause), through the `deepgram` extra, which
the transcriber next door shares. Nothing from Deepgram's SDK.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, AsyncIterator, Mapping

from emet_sdk.plugin import PluginError, VoicePlugin
from emet_sdk.types import Health, VoiceDescriptor

from emet_providers._http import api_error, open_client

__all__ = [
    "DeepgramVoice",
    "DEFAULT_MODEL",
    "DEFAULT_KEY_ENV",
    "DEFAULT_URL",
    "DEFAULT_SAMPLE_RATE",
    "SAMPLE_RATES",
    "MAX_CHARS",
    "pieces",
]

log = logging.getLogger("emet_providers.deepgram_voice")

DEFAULT_URL = "https://api.deepgram.com/v1/speak"

#: An Aura-2 voice, the current family on Deepgram's model list (2026-09-15).
#: The soul names another when it wants one.
DEFAULT_MODEL = "aura-2-thalia-en"

#: The same variable the transcriber reads: one Deepgram key serves both.
DEFAULT_KEY_ENV = "EMET_DEEPGRAM_KEY"

#: What linear16 may be asked for, and the API's own default.
SAMPLE_RATES: tuple[int, ...] = (8000, 16000, 24000, 32000, 48000)
DEFAULT_SAMPLE_RATE = 24000

#: Characters a request may carry. Beyond it the API answers HTTP 413.
MAX_CHARS = 2000

#: The speed range the streaming reference documents (0.7 to 1.5); a soul
#: asking for more is clamped and told so once.
SPEED_MIN, SPEED_MAX = 0.7, 1.5

#: Query keys the body may not override: the format is this plugin's to
#: state, and the engine opens the speaker to match it.
FORMAT_KEYS = frozenset({"model", "encoding", "container", "sample_rate"})

#: What the preflight says. Six characters.
PREFLIGHT_TEXT = "Ready."

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def pieces(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split text into request-sized pieces at sentence, then word, boundaries.

    Almost every sentence the engine hands over fits in one piece; this
    exists so that the one that does not is spoken in order rather than
    refused.
    """
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for unit in _SENTENCE_END.split(text):
        words = unit.split(" ") if len(unit) > limit else [unit]
        for word in words:
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= limit:
                current = f"{current} {word}"
            else:
                out.append(current)
                current = word
            while len(current) > limit:
                out.append(current[:limit])
                current = current[limit:]
    if current:
        out.append(current)
    return out


class DeepgramVoice(VoicePlugin):
    """Aura, streamed over HTTPS, one request per sentence."""

    provider = "deepgram"

    def __init__(
        self,
        config: Mapping[str, Any],
        voice: Mapping[str, Any] | None = None,
        *,
        transport: Any = None,
    ) -> None:
        super().__init__(config, voice)
        #: An `httpx` transport, injected by tests. None means the network.
        self._transport = transport
        self._client: Any = None
        self._key: str | None = None
        self._started = False
        self._fault: str | None = None
        #: The last failure inside a sentence, for diagnostics. Not a fault.
        self.last_error: str | None = None

        self.model_name: str = self.model or DEFAULT_MODEL
        self.key_env_name: str = self.key_env or DEFAULT_KEY_ENV
        self.url: str = str(self.params.get("url") or DEFAULT_URL)
        self.timeout_s = float(self.params.get("timeout_s", 30.0))
        self.preflight = bool(self.params.get("preflight", True))
        rate = int(self.params.get("sample_rate") or DEFAULT_SAMPLE_RATE)
        if rate not in SAMPLE_RATES:
            log.warning(
                "deepgram voice: sample_rate %d is not one linear16 offers %s; using %d",
                rate,
                SAMPLE_RATES,
                DEFAULT_SAMPLE_RATE,
            )
            rate = DEFAULT_SAMPLE_RATE
        self.sample_rate: int = rate
        speed = self.rate
        if not SPEED_MIN <= speed <= SPEED_MAX:
            clamped = min(SPEED_MAX, max(SPEED_MIN, speed))
            log.warning("deepgram voice: rate %.2f is outside %.1f to %.1f; using %.2f", speed, SPEED_MIN, SPEED_MAX, clamped)
            speed = clamped
        self.speed: float = speed

    # ------------------------------------------------------------- request

    def query(self) -> dict[str, str]:
        """The query string, as a mapping. Format keys win over `params.query`."""
        extra = {
            str(k): str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in (self.params.get("query") or {}).items()
            if str(k) not in FORMAT_KEYS
        }
        out = {
            **extra,
            "model": self.model_name,
            "encoding": "linear16",
            "container": "none",
            "sample_rate": str(self.sample_rate),
        }
        if self.speed != 1.0:
            out["speed"] = f"{self.speed:g}"
        return out

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._key or ''}", "Content-Type": "application/json"}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"Deepgram keys come from console.deepgram.com; put it in the keys "
                f"file. The key is never written into a soul or a manifest."
            )
            return
        client = open_client(self.url, timeout_s=self.timeout_s, transport=self._transport, extra="deepgram")
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
        log.info("deepgram voice: ready, %s at %d Hz", self.model_name, self.sample_rate)

    async def _preflight(self) -> str | None:
        """Say one word into the void. Returns the reason it could not."""
        try:
            response = await self._client.post(
                self.url, params=self.query(), headers=self.headers(), json={"text": PREFLIGHT_TEXT}
            )
        except Exception as exc:  # noqa: BLE001 - reported through health, not raised
            return (
                f"could not reach {self.url}: {type(exc).__name__}: {exc}. "
                f"A cloud voice needs a network; the local one does not."
            )
        if response.status_code == 200:
            return None
        if response.status_code in (401, 403):
            return (
                f"Deepgram rejected the key in {self.key_env_name} "
                f"(HTTP {response.status_code}). Check the key, and that it has the "
                f"text-to-speech scope."
            )
        return (
            f"Deepgram refused to speak as {self.model_name!r}: "
            f"{api_error(response.status_code, response.content)}. Check the voice "
            f"name against Deepgram's model list."
        )

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.error("deepgram voice: %s", reason)

    async def shutdown(self) -> None:
        self._started = False
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ------------------------------------------------------------ reporting

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(
            provider=self.provider,
            model=self.model_name,
            sample_rate=self.sample_rate,
            streaming=True,
            healthy=self._started,
        )

    def health(self) -> Health:
        if self._fault:
            fault = "no_key" if "no key" in self._fault else "start_failed"
            return Health(ok=False, detail=self._fault, faults=(fault,))
        return Health()

    # ---------------------------------------------------------------- speak

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        if not self._started or self._client is None:
            raise PluginError("the deepgram voice was not started")
        for piece in pieces(text):
            async for chunk in self._request(piece):
                yield chunk

    async def _request(self, piece: str) -> AsyncIterator[bytes]:
        """One request, streamed. Chunks are kept to whole samples: a chunk
        boundary that falls between the two bytes of a sample would otherwise
        put a click in the audio and a byte in the wrong place."""
        carry = b""
        try:
            async with self._client.stream(
                "POST", self.url, params=self.query(), headers=self.headers(), json={"text": piece}
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    self.last_error = api_error(response.status_code, body)
                    log.warning("deepgram voice: %s", self.last_error)
                    raise PluginError(f"Deepgram could not say {piece!r}: {self.last_error}")
                async for raw in response.aiter_bytes():
                    data = carry + raw
                    if len(data) % 2:
                        data, carry = data[:-1], data[-1:]
                    else:
                        carry = b""
                    if data:
                        yield data
        except PluginError:
            raise
        except Exception as exc:  # noqa: BLE001 - one sentence lost, reported
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("deepgram voice: %s", self.last_error)
            raise PluginError(f"Deepgram could not say {piece!r}: {self.last_error}") from exc
        if carry:
            log.debug("deepgram voice: dropped a dangling byte at the end of a sentence")
