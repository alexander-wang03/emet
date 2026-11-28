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

**Out of the Model Improvement Program, on every request.** Every request,
the preflight's included, carries `mip_opt_out=true` (the maintainer,
2026-11-24: "Yes"). Deepgram's Model Improvement Partnership Program page
(developers.deepgram.com, read 2026-11-26) names the text-to-speech REST
API among the endpoints that take it, with the example
`https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&mip_opt_out=true`,
and says "Data from opted-out requests is retained only for the duration
necessary to process the request." Deepgram's changelog of 2026-03-05 says
pay as you go customers opt out "with no impact on the rates listed on
deepgram.com/pricing". No body can turn it off: `params.mip_opt_out` and
`params.query.mip_opt_out` may say true, which changes nothing, and any
other value is judged at construction and held in `refusal`. `query()`
raises `PluginError` with it and `start()` reports unhealthy with it, before
the key is read or a request is made.

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

**A refusal that names its kind.** `FAULT_CODES` is read against each
refused request's status and `err_code` through `_http.fault_kind`, at boot
and on every sentence. Deepgram's errors page
(developers.deepgram.com/docs/errors and /reference/errors, both read
2026-11-27) documents HTTP 403 with `err_code` `INSUFFICIENT_PERMISSIONS`,
"Project does not have access to the requested model", which is
`model_unavailable`. It documents HTTP 402 for speech-to-text only
(`ASR_PAYMENT_REQUIRED`); a 402 is Payment Required, so the voice reads any
402 as `out_of_credit`, and what the speak API sends with it is
unverified. An unknown voice is HTTP 400 with the generic `Bad Request`
and the message "No such model/language/tier combination found", so it
stays `start_failed`, quoted in Deepgram's words. After a refusal of either
kind `health()` reports it: `model_unavailable` for the rest of the run,
`out_of_credit` until a request succeeds or fails for a reason that names
no kind. After `model_unavailable` each
sentence is lost with that sentence and nothing more is sent, as the chat
plugins send nothing for a model that cannot answer. The engine reads only
the language model's and the transcriber's `health()` after boot, so the
kind is the voice's own record of why.

Params, all optional, from the body's `models.tts.params`:

    sample_rate   8000, 16000, 24000, 32000 or 48000. Default 24000. The
                  engine opens the speaker at this rate.
    query         mapping of extra query parameters, passed through as
                  strings. `model`, `encoding`, `container` and
                  `sample_rate` are fixed by this plugin and cannot be
                  overridden here; `mip_opt_out` may only say true.
    mip_opt_out   true, or absent. The plugin sends `mip_opt_out=true`
                  whatever the body says, and any other value is refused.
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

from emet_providers._http import api_error, endpoint_of, error_code, fault_kind, open_client, who
from emet_providers.deepgram import fault_detail

__all__ = [
    "DeepgramVoice",
    "DEFAULT_MODEL",
    "DEFAULT_KEY_ENV",
    "DEFAULT_URL",
    "FAULT_CODES",
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

#: What a refused request's status and `err_code` mean, read by
#: `_http.fault_kind`. From Deepgram's errors page
#: (developers.deepgram.com/docs/errors and /reference/errors, read
#: 2026-11-27): 403 `INSUFFICIENT_PERMISSIONS` is a project with no access to
#: the model asked for, and 402 is Payment Required; the page documents a
#: 402 body for speech-to-text only, so the speak API's is unverified.
FAULT_CODES: dict[tuple[int, str | None], str] = {
    (402, None): "out_of_credit",
    (403, "INSUFFICIENT_PERMISSIONS"): "model_unavailable",
}

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

#: The Model Improvement Program's opt-out, sent as true on every request.
OPT_OUT = "mip_opt_out"

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
        self._fault_kind = "start_failed"
        #: A refusal of a known kind on a sentence's request, as
        #: `(kind, detail)`: `model_unavailable` holds for the rest of the
        #: run, `out_of_credit` until a request succeeds or fails for a reason
        #: that names no kind.
        self._run_fault: tuple[str, str] | None = None
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
        #: Why this body's requests cannot be sent, or None (the docstring).
        self.refusal: str | None = self._judge_params()

    # ------------------------------------------------------------- request

    def _judge_params(self) -> str | None:
        """The first reason the body's params cannot be sent, or None."""
        query = self.params.get("query") or {}
        if not isinstance(query, Mapping):
            return f"params.query is a mapping of query parameters, not {type(query).__name__} {query!r}."
        for where, present, value in (
            ("params.mip_opt_out", OPT_OUT in self.params, self.params.get(OPT_OUT)),
            ("params.query.mip_opt_out", OPT_OUT in query, query.get(OPT_OUT)),
        ):
            if present and not (value is True or (isinstance(value, str) and value.strip().lower() == "true")):
                return (
                    f"{where} is {value!r}, and every Deepgram request carries "
                    f"mip_opt_out=true, which keeps it out of Deepgram's Model Improvement "
                    f"Program, so a body cannot turn it off. Take the line out of the body."
                )
        return None

    def query(self) -> dict[str, str]:
        """The query string, as a mapping. Format keys win over `params.query`,
        and `mip_opt_out` is always "true". Raises `PluginError` with
        `refusal` for a body whose requests cannot be sent."""
        if self.refusal:
            raise PluginError(self.refusal)
        extra = {
            str(k): str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in (self.params.get("query") or {}).items()
            if str(k) not in FORMAT_KEYS and str(k) != OPT_OUT
        }
        out = {
            **extra,
            "model": self.model_name,
            "encoding": "linear16",
            "container": "none",
            "sample_rate": str(self.sample_rate),
            OPT_OUT: "true",
        }
        if self.speed != 1.0:
            out["speed"] = f"{self.speed:g}"
        return out

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._key or ''}", "Content-Type": "application/json"}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.refusal:
            self._fail(self.refusal)
            return
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"Deepgram keys come from console.deepgram.com; put it in the keys "
                f"file. The key is never written into a soul or a manifest.",
                kind="no_key",
            )
            return
        client = open_client(self.url, timeout_s=self.timeout_s, transport=self._transport, extra="deepgram")
        if isinstance(client, str):
            self._fail(client, kind="no_library")
            return
        self._client = client
        if self.preflight:
            refused = await self._preflight()
            if refused:
                reason, kind = refused
                self._fail(reason, kind=kind)
                return
        self._started = True
        log.info("deepgram voice: ready, %s at %d Hz", self.model_name, self.sample_rate)

    def _who(self) -> str:
        """Deepgram at its own address, and the server at its host anywhere else."""
        return who(self.url, default_url=DEFAULT_URL, vendor="Deepgram", host=self.describe().host)

    def _judge(self, status: int, body: bytes) -> tuple[str, str] | None:
        """The kind and the owner's sentence for a refused request whose
        status and `err_code` are in `FAULT_CODES`, or None."""
        kind = fault_kind(status, body, FAULT_CODES)
        if kind is None:
            return None
        named = self._who()
        detail = fault_detail(
            kind,
            who=named,
            at_default=named == "Deepgram",
            status=status,
            code=error_code(body),
            model=self.model_name,
            setting="models.tts.model",
            noun="voice",
        )
        return kind, detail

    async def _preflight(self) -> tuple[str, str] | None:
        """Say one word into the void. Returns the reason it could not, with
        its fault kind."""
        try:
            response = await self._client.post(
                self.url, params=self.query(), headers=self.headers(), json={"text": PREFLIGHT_TEXT}
            )
        except Exception as exc:  # noqa: BLE001 - reported through health, not raised
            return (
                f"could not reach {endpoint_of(self.url)}: {type(exc).__name__}: {exc}. "
                f"A cloud voice needs a network; the local one does not."
            ), "start_failed"
        if response.status_code == 200:
            return None
        judged = self._judge(response.status_code, response.content)
        if judged is not None:
            kind, detail = judged
            return detail, kind
        if response.status_code in (401, 403):
            return (
                f"Deepgram rejected the key in {self.key_env_name} "
                f"(HTTP {response.status_code}). Check the key, and that it has the "
                f"text-to-speech scope."
            ), "start_failed"
        return (
            f"Deepgram refused to speak as {self.model_name!r}: "
            f"{api_error(response.status_code, response.content)}. Check the voice "
            f"name against Deepgram's model list."
        ), "start_failed"

    def _fail(self, reason: str, *, kind: str = "start_failed") -> None:
        # INFO: the engine prints the reason through `health().detail`, so
        # logging's last resort would print it twice (L16).
        self._fault = reason
        self._fault_kind = kind
        log.info("deepgram voice: %s", reason)

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
            endpoint=endpoint_of(self.url),
            key_env=self.key_env_name,
        )

    def health(self) -> Health:
        if self._fault:
            return Health(ok=False, detail=self._fault, faults=(self._fault_kind,))
        if self._run_fault is not None:
            kind, detail = self._run_fault
            return Health(ok=False, detail=detail, faults=(kind,))
        return Health()

    # ---------------------------------------------------------------- speak

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        if not self._started or self._client is None:
            raise PluginError("the deepgram voice was not started")
        if self._run_fault is not None and self._run_fault[0] == "model_unavailable":
            # Deepgram has said this project cannot use the voice, so nothing
            # is sent for the rest of the run, as the chat plugins do.
            raise PluginError(f"Deepgram could not say {text!r}: {self._run_fault[1]}")
        for piece in pieces(text):
            async for chunk in self._request(piece):
                yield chunk

    def _refusal(self, piece: str, status: int, body: bytes) -> PluginError:
        """The error for a sentence's refused request, keeping a known kind for `health()`."""
        judged = self._judge(status, body)
        if judged is not None:
            self._run_fault = judged
            self.last_error = judged[1]
        else:
            self._forget_credit()
            self.last_error = api_error(status, body)
        # INFO: the engine prints the lost sentence (L16).
        log.info("deepgram voice: %s", self.last_error)
        return PluginError(f"Deepgram could not say {piece!r}: {self.last_error}")

    def _forget_credit(self) -> None:
        """End a kept `out_of_credit` once a request fails for a reason that names no kind."""
        if self._run_fault is not None and self._run_fault[0] == "out_of_credit":
            # News of the network or the server, never of the account.
            self._run_fault = None

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
                    raise self._refusal(piece, response.status_code, await response.aread())
                if self._run_fault is not None and self._run_fault[0] == "out_of_credit":
                    # Deepgram spoke, so the account pays again.
                    self._run_fault = None
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
            self._forget_credit()
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.info("deepgram voice: %s", self.last_error)
            raise PluginError(f"Deepgram could not say {piece!r}: {self.last_error}") from exc
        if carry:
            log.debug("deepgram voice: dropped a dangling byte at the end of a sentence")
