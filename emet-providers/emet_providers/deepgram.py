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

**Out of the Model Improvement Program, on every request.** Each
connection, the preflight's included, carries `mip_opt_out=true` (the
maintainer, 2026-11-24: "Yes"). Deepgram's page: "Add `mip_opt_out=true` as
a query parameter of all API requests that you want to be excluded from the
Model Improvement Program. Data from opted-out requests is retained only
for the duration necessary to process the request." It lists streaming
speech-to-text among the endpoints that take it, and the streaming
reference gives the parameter's default as `false`
(developers.deepgram.com, the Model Improvement Partnership Program page
and the listen-streaming reference, both read 2026-11-26). Deepgram's
changelog of 2026-03-05: "Pay as you Go and Growth customers can now opt in
or out of the Model Improvement Program with no impact on the rates listed
on deepgram.com/pricing." No body can turn it off: `params.mip_opt_out` and
`params.query.mip_opt_out` may say true, which changes nothing, and any
other value leaves the plugin refused (below). Sending it to a self-hosted
`url` is harmless as far as is known; whether a self-hosted server reads
it is unverified.

**Keyterms, a list, opt-in per body** (AS2; E11 (a), 2026-11-25). Each
entry of `params.keyterms` goes out as one `keyterm` parameter, in the
body's order. Deepgram's keyterm page (developers.deepgram.com/docs/keyterm,
read 2026-11-26): "To pass multiple separate keyterms, repeat the `keyterm`
parameter"; "Do not separate keyterms with commas, semicolons, or line
breaks", since the API "treats it as a single literal keyterm, so it
silently boosts nothing instead of failing"; keyterms take plain terms with
no weights; "Key Terms are limited to 500 tokens per request"; keyterm
prompting is "available for both monolingual and multilingual
transcription using the Nova-3 Models" and Flux, and a Nova-2 model is
pointed at the Keywords feature instead; "Keyterms appear in your usage
records". A space inside a term is one phrase and is sent as `+`, which the
page allows in a query. The page does not say what a token is, so the
count here is Emet's own and high on purpose: each term's UTF-8 bytes plus
one. It names no limit on the number of terms (it recommends "the most
important 20-50 terms") and none on one term's length. The streaming
reference says of keyterms sent mid-stream "Compatible with all Nova-3
models, monolingual and multilingual"
(developers.deepgram.com/reference/speech-to-text/listen-streaming, read
2026-11-26), so every model whose name is `nova-3` or starts `nova-3-` is
let through; no named variant (`nova-3-medical`, say) has been tried with
them. Each minute with keyterms costs $0.0013 more,
pay as you go (deepgram.com/pricing, "Speech-to-text add-ons", read
2026-11-26), so no list is sent unless the body writes one.

**Refused at construction.** The request is fixed when the plugin is
built: `request_url()` gives it before `start()`, and the bank's offline
checks (`paid_replay.offline_request` and `canary.plugin_for`, among others)
build it that way without starting the plugin. So a body whose keyterms,
opt-out or `url` cannot be sent is judged in `__init__`. The reason,
naming the entry, is `refusal`; `query()` and `request_url()` raise
`PluginError` with it, and `start()` reports unhealthy with it before it
reads the key or opens a connection, so the engine refuses to run
half-deaf and prints why. The engine builds the plugin outside any
handler, so raising in `__init__` would reach the owner as a traceback.

**What fails loudly, and where.** `start()` reads the key and refuses to
report healthy without one, naming the variable to set. It then opens and
closes one connection, so a rejected key or an unreachable network is known
at boot, in the terms the owner can act on, rather than at the first
question. A failure during an utterance is logged and the final carries what
was heard before it, plus the reason in `Transcript.error`; a transient
network fault costs one turn and leaves the plugin healthy. The engine reads
that reason and says the soul's failure line, so a robot whose hotspot died
mid-conversation tells the person instead of standing there.

**A refusal that names its kind.** A rejected handshake carries Deepgram's
answer: websockets keeps the response, its status and its body, on the
exception (`InvalidStatus.response`, websockets 17.1, read 2026-11-27).
`FAULT_CODES` is read against the status and the body's `err_code` through
`_http.fault_kind`, at boot and on each utterance's connection. Deepgram's
errors page (developers.deepgram.com/docs/errors and
developers.deepgram.com/reference/errors, both read 2026-11-27) documents
HTTP 402 with `err_code` `ASR_PAYMENT_REQUIRED`, "Project does not have
enough credits for an ASR request and does not have an overage agreement",
and HTTP 403 with `err_code` `INSUFFICIENT_PERMISSIONS`, "Project does not
have access to the requested model". The first is `out_of_credit` and the
second `model_unavailable`. The page does not say whether a streaming
handshake answers with the same body. The streaming troubleshooting page
says a failed upgrade sends its error "back in the body of the HTTP
response" and in the `dg-error` header, and does not show that body
(developers.deepgram.com/docs/stt-troubleshooting-websocket-data-and-net-errors,
read 2026-11-27), so that the handshake's body has the same shape is
unverified. Deepgram documents no code that names an unknown model: the
speak API answers one with HTTP 400 and the generic `Bad Request`, so a
model Deepgram has never heard of stays `start_failed`, and the reason
quotes Deepgram's own message. After a refusal of either kind `health()`
reports it: `model_unavailable` for the rest of the run, `out_of_credit`
until a connection opens again or one fails for a reason that names no
kind. The utterance's final carries the same
sentence in `Transcript.error`.

Params, all optional, from the body's `models.stt.params`:

    keyterms     list of terms for Deepgram to listen for, such as the
                 robot's name; absent by default, and an empty list sends
                 none. Each entry is non-empty text with no space at
                 either end, no comma, semicolon or line break, and no
                 weight (`Emet:2`); a colon after a digit, as in
                 `Room 4:30`, is part of the term. Only for a Nova-3
                 model. A body that writes `params.keyterm`, singular, is
                 refused, since it would otherwise boost nothing and say
                 nothing.
    mip_opt_out  true, or absent. The plugin sends `mip_opt_out=true`
                 whatever the body says, and any other value is refused.
    query        mapping of extra Deepgram query parameters (`language`,
                 `endpointing`, `redact_usage`, ...), passed through as
                 strings. The body's value replaces the plugin's own
                 `interim_results`, `punctuate` and `smart_format`, so
                 `smart_format: true` turns formatting back on, and with it
                 the finals Deepgram holds to format an entity;
                 `detect_entities: true` holds them the same way.
                 `encoding`, `sample_rate` and `channels` are fixed by the
                 audio format, and `model` by `models.stt.model`; none of
                 them can be overridden here. `mip_opt_out` may only say
                 true. `keyterm` and `keyterms` are refused here: the terms
                 go in `params.keyterms`, so each is checked and sent as a
                 parameter of its own.
    timeout_s    seconds `finish()` waits for the final after CloseStream,
                 and the whole of the person's wait: a socket that will not
                 unwind is abandoned rather than waited on. Default 5.
    open_timeout_s
                 seconds to allow the handshake. Default 10.
    preflight    bool, default true. Open and close a connection in
                 `start()` to check the key and the network.
    url          the WebSocket URL, for a self-hosted deployment. It holds
                 no query and no fragment: a parameter written there would
                 pass none of the rules above, so such a URL is refused.

Dependencies: `websockets` (BSD-3-Clause), through the `deepgram` extra.
Nothing from Deepgram's SDK; the wire protocol is small and this file speaks
it directly, which is also what keeps the dependency list to one package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from emet_sdk.plugin import PluginError, TranscriberPlugin
from emet_sdk.types import AudioFormat, Health, Transcript, TranscriberDescriptor

from emet_providers._http import api_error, endpoint_of, error_code, fault_kind, who

__all__ = [
    "DeepgramTranscriber",
    "DEFAULT_MODEL",
    "DEFAULT_KEY_ENV",
    "DEFAULT_URL",
    "FAULT_CODES",
    "fault_detail",
    "FIXED_QUERY",
    "FORMAT_KEYS",
    "OPT_OUT",
    "KEYTERM",
    "KEYTERM_TOKENS",
    "keyterm_count",
    "takes_keyterms",
]

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

#: What a refused handshake's status and `err_code` mean, read by
#: `_http.fault_kind`. From Deepgram's errors page
#: (developers.deepgram.com/docs/errors and /reference/errors, read
#: 2026-11-27): 402 `ASR_PAYMENT_REQUIRED` is a project with too few credits
#: and no overage agreement, and 402 is Payment Required whatever its code;
#: 403 `INSUFFICIENT_PERMISSIONS` is a project with no access to the model
#: asked for. The same code with 401 is a user's permissions, so it is keyed
#: with its status. A bare 403 stays the rejected key it is today.
FAULT_CODES: dict[tuple[int, str | None], str] = {
    (402, "ASR_PAYMENT_REQUIRED"): "out_of_credit",
    (402, None): "out_of_credit",
    (403, "INSUFFICIENT_PERMISSIONS"): "model_unavailable",
}

#: Where an owner adds credit, said only at Deepgram's own address.
_ADD_CREDIT = "Add credit to the project in Deepgram's console."


def fault_detail(
    kind: str,
    *,
    who: str,
    at_default: bool,
    status: int,
    code: str | None,
    model: str,
    setting: str,
    noun: str = "model",
) -> str:
    """The sentence an owner reads for a refusal of a known kind.

    `who` is "Deepgram" at Deepgram's own address and "the server at
    {host}" anywhere else, where the clause naming Deepgram's console is
    left out. `setting` is the manifest key that chooses the model, and
    `noun` what the model is called there.
    """
    if kind == "out_of_credit":
        what = f"HTTP {status}, {code}" if code else f"HTTP {status}"
        where = f" {_ADD_CREDIT}" if at_default else ""
        return f"{who} says the account is out of credit ({what}).{where} Retrying will not help."
    return f"{who} does not serve the {noun} {model!r} (HTTP {status}). Choose another under {setting}."


#: Query parameters this plugin always sends; a body's `params.query`
#: replaces any but the format keys. `interim_results` is the whole point of
#: a streaming seam, and `punctuate` writes the end marks and commas the
#: engine's trailing-clause check reads. `smart_format` is sent as false.
#: On, Deepgram may hold a final for up to 3 s of silence to format an
#: entity that looks unfinished (Deepgram's Smart Formatting page, read
#: 2026-10-04), and the engine closes the stream about a second after the
#: last word. On the 3 of 28 replayed turns where "what's two plus two"
#: read "What's 22", the only final was the flush after the close; with it
#: off, the one of the three replayed on the same bytes got Deepgram's own
#: final, "What's two plus two", before the close, and a lone "No" that had
#: waited 2.7 s for a flush got its own final too (laptop replays,
#: 2026-10-01 to 05). So numbers, dates, times, currency, phone numbers,
#: emails and URLs reach the model as words. `mip_opt_out` is sent as true
#: with every request (the module's docstring), and no body may change it.
FIXED_QUERY: Mapping[str, str] = {
    "encoding": "linear16",
    "channels": "1",
    "interim_results": "true",
    "punctuate": "true",
    "smart_format": "false",
    "mip_opt_out": "true",
}

#: Keys the body may not override through `params.query`: the audio format
#: fixes them, and a mismatch is the silent failure the descriptor exists to
#: prevent.
FORMAT_KEYS = frozenset({"encoding", "sample_rate", "channels"})

#: The Model Improvement Program's opt-out, on every request.
OPT_OUT = "mip_opt_out"

#: The query parameter each entry of `params.keyterms` is sent as.
KEYTERM = "keyterm"

#: Deepgram's limit, "Key Terms are limited to 500 tokens per request"
#: (developers.deepgram.com/docs/keyterm, read 2026-11-26), held against
#: `keyterm_count()`, which counts high because the page does not say how
#: Deepgram counts.
KEYTERM_TOKENS = 500

#: What a term may not hold: the separators Deepgram's keyterm page says
#: not to use ("commas, semicolons, or line breaks"), any other control
#: character, and a trailing weight such as `Emet:0.15`, which the page
#: lists among the forms that silently boost nothing. A line break is any
#: that `str.splitlines()` splits on, so U+0085, U+2028 and U+2029 count,
#: and every control character of Unicode category Cc is refused. A colon
#: and a number after a digit (`Room 4:30`, `Psalm 23:1`) are part of the
#: term, and only a colon after anything else starts a weight.
_SEPARATORS = ((",", "a comma"), (";", "a semicolon"))
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WEIGHT = re.compile(r":\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s*$")

_CLOSE_STREAM = json.dumps({"type": "CloseStream"})

_KEYTERM_PAGE = "developers.deepgram.com/docs/keyterm, read 2026-11-26"


def keyterm_count(terms: tuple[str, ...] | list[str]) -> int:
    """Emet's count of a keyterm list against `KEYTERM_TOKENS`: each term's
    UTF-8 bytes plus one.

    Deepgram does not publish what it counts as a token. A tokenizer whose
    every token is at least one byte makes no more tokens of a term than the
    term has bytes, and the one added to each term allows for a boundary
    between terms. So a list inside this count is inside Deepgram's 500
    under that assumption, and a list Deepgram would take may be refused
    here. The assumption is unverified.
    """
    return sum(len(term.encode("utf-8")) + 1 for term in terms)


def takes_keyterms(model: str) -> bool:
    """Whether a model is one Deepgram's keyterm page names: "the Nova-3
    Models". Flux takes them too, and is not reachable through this plugin."""
    return model == "nova-3" or model.startswith("nova-3-")


def _is_true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _weighted(term: str) -> bool:
    """Whether a term ends in a weight: a colon and a number, after text
    that does not end in a digit (`4:30` is a time, `Emet:2` a weight)."""
    found = _WEIGHT.search(term)
    return found is not None and not term[: found.start()].rstrip()[-1:].isdigit()


def _term_problem(term: Any) -> str | None:
    """What is wrong with one keyterm, as the end of a sentence, or None."""
    if not isinstance(term, str):
        return f"is {type(term).__name__}, and a keyterm is text."
    if not term.strip():
        return "is empty, and a keyterm is a word or a phrase."
    if term != term.strip():
        return "begins or ends with a space. Write the term as it is said, with no space at either end."
    for mark, what in _SEPARATORS:
        if mark in term:
            return _separated(what)
    if _CONTROL.search(term):
        return _separated("a line break or another control character")
    if _weighted(term):
        return (
            f"ends in a weight. A keyterm is a plain term, with no weight or intensifier, "
            f"and Deepgram takes `term:0.15` as one literal keyterm that boosts nothing "
            f"({_KEYTERM_PAGE}). Write the term alone, with no colon or number after it."
        )
    return None


def _separated(what: str) -> str:
    return (
        f"holds {what}. Deepgram's keyterm page says \"Do not separate keyterms with "
        f"commas, semicolons, or line breaks\", and takes a comma-joined value as one "
        f"literal keyterm that \"silently boosts nothing instead of failing\" "
        f"({_KEYTERM_PAGE}). Give each term an entry of its own."
    )


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


def _response_body(exc: BaseException) -> bytes:
    """The body of a rejected handshake's response, or no bytes.

    Duck-typed on `response.body`, which `websockets.http11.Response`
    carries, so a test's stand-in serves as well as the library's own.
    """
    body = getattr(getattr(exc, "response", None), "body", None)
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    return body.encode("utf-8") if isinstance(body, str) else b""


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
        self._fault_kind = "start_failed"
        #: A refusal of a known kind on an utterance's connection, as
        #: `(kind, detail)`: `model_unavailable` holds for the rest of the
        #: run, `out_of_credit` until a connection opens again or one fails
        #: for a reason that names no kind.
        self._run_fault: tuple[str, str] | None = None
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
        #: The terms sent as `keyterm`, in the body's order; empty when the
        #: body names none or its list was refused.
        self.keyterms: tuple[str, ...] = ()
        #: Why this body's request cannot be sent, naming the entry, or None.
        #: Judged here, since the request is fixed at construction.
        self.refusal: str | None = self._judge_params()

    # ------------------------------------------------------------- request

    def _judge_params(self) -> str | None:
        """Check the URL, the opt-out and the keyterms; set `keyterms`.
        Returns the first reason the request cannot be sent, or None."""
        if "?" in self.url or "#" in self.url:
            return (
                f"params.url is {self.url!r}, which holds a query or a fragment. Every query "
                f"parameter goes under params.query or params.keyterms, where each is checked, "
                f"so write the URL with nothing after its path."
            )
        query = self.params.get("query") or {}
        if not isinstance(query, Mapping):
            return f"params.query is a mapping of query parameters, not {type(query).__name__} {query!r}."
        for where, present, value in (
            ("params.mip_opt_out", OPT_OUT in self.params, self.params.get(OPT_OUT)),
            ("params.query.mip_opt_out", OPT_OUT in query, query.get(OPT_OUT)),
        ):
            if present and not _is_true(value):
                return (
                    f"{where} is {value!r}, and every Deepgram request carries "
                    f"mip_opt_out=true, which keeps it out of Deepgram's Model Improvement "
                    f"Program, so a body cannot turn it off. Take the line out of the body."
                )
        misplaced = [f"params.{KEYTERM}"] if KEYTERM in self.params else []
        misplaced += [f"params.query.{name}" for name in (KEYTERM, "keyterms") if name in query]
        if misplaced:
            return (
                f"{misplaced[0]} is not read: write the terms as a list under "
                f"params.keyterms, one entry a term, so each is checked and sent as "
                f"a keyterm parameter of its own. A comma-joined value is one literal "
                f"keyterm, which \"silently boosts nothing instead of failing\" "
                f"({_KEYTERM_PAGE})."
            )
        terms = self.params.get("keyterms")
        if terms is None:
            return None
        if not isinstance(terms, (list, tuple)):
            return (
                f"params.keyterms is a list of terms, one entry a term, not "
                f"{type(terms).__name__} {terms!r}."
            )
        for n, term in enumerate(terms, 1):
            problem = _term_problem(term)
            if problem:
                return f"params.keyterms entry {n}, {term!r}, {problem}"
        if terms and not takes_keyterms(self.model_name):
            return (
                f"params.keyterms is for a Nova-3 model, and models.stt.model is "
                f"{self.model_name!r}. Deepgram's keyterm page names the Nova-3 models "
                f"and points Nova-2 at its Keywords feature instead ({_KEYTERM_PAGE}). "
                f"Set models.stt.model to nova-3, or take params.keyterms out of the body."
            )
        count = keyterm_count(list(terms))
        if count > KEYTERM_TOKENS:
            return (
                f"params.keyterms comes to {count} by Emet's count, past Deepgram's limit "
                f"of {KEYTERM_TOKENS} tokens a request ({_KEYTERM_PAGE}). "
                f"Deepgram does not say how it counts a token, so Emet counts high: "
                f"each term's UTF-8 bytes plus one. The page recommends the most "
                f"important 20 to 50 terms."
            )
        self.keyterms = tuple(terms)
        return None

    def query(self) -> dict[str, Any]:
        """The query string, as a mapping. Format keys win over `params.query`,
        `mip_opt_out` is always "true", and `keyterm`, when the body has
        keyterms, is the list of them in order. Raises `PluginError` with
        `refusal` for a body whose request cannot be sent."""
        if self.refusal:
            raise PluginError(self.refusal)
        extra = {
            str(k): str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in (self.params.get("query") or {}).items()
            if str(k) not in FORMAT_KEYS and str(k) != OPT_OUT
        }
        out: dict[str, Any] = {
            **FIXED_QUERY,
            **extra,
            OPT_OUT: "true",
            "sample_rate": str(self.format.sample_rate),
            "model": self.model_name,
        }
        if self.keyterms:
            out[KEYTERM] = list(self.keyterms)
        return out

    def request_url(self) -> str:
        """The URL with its query. Each keyterm is a parameter of its own, and
        a space inside a term goes out as `+`."""
        return f"{self.url}?{urlencode(self.query(), doseq=True)}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._key}"}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        # First a body whose request cannot be sent: nothing is read and
        # nothing connects for it. Then the key, then the library: an owner
        # without a key is the common case, and the message for it should
        # not depend on which extras happen to be installed.
        if self.refusal:
            self._fail(self.refusal)
            return
        self._key = os.environ.get(self.key_env_name) or None
        if not self._key:
            self._fail(
                f"no key: the environment variable {self.key_env_name} is not set. "
                f"Deepgram keys come from console.deepgram.com; export it in the "
                f"shell that runs the robot. The key is never written into a soul "
                f"or a manifest.",
                kind="no_key",
            )
            return

        if self._connect is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                self._fail(
                    "the websockets library is not installed. It is an optional "
                    "dependency: install `emet-providers[deepgram]`.",
                    kind="no_library",
                )
                return
            self._connect = connect

        if self.preflight:
            refused = await self._preflight()
            if refused:
                reason, kind = refused
                self._fail(reason, kind=kind)
                return

        self._started = True
        log.info("deepgram: ready, model %s at %d Hz", self.model_name, self.format.sample_rate)

    def _who(self) -> str:
        """Deepgram at its own address, and the server at its host anywhere else."""
        return who(self.url, default_url=DEFAULT_URL, vendor="Deepgram", host=self.describe().host)

    def _judge(self, exc: BaseException) -> tuple[str, str] | None:
        """The kind and the owner's sentence for a refused handshake whose
        status and `err_code` are in `FAULT_CODES`, or None."""
        status = _status_code(exc)
        if status is None:
            return None
        body = _response_body(exc)
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
            setting="models.stt.model",
        )
        return kind, detail

    def _refused(self, exc: BaseException) -> str:
        """A refused handshake of no known kind, in the server's words when
        the response carried any. The sentence names Deepgram at its own
        address and the server at its host anywhere else (C7)."""
        code = _status_code(exc)
        body = _response_body(exc)
        named = self._who()
        named = named[:1].upper() + named[1:]
        if code is not None and body.strip():
            return f"{named} refused the connection: {api_error(code, body)}"
        return f"{named} refused the connection with HTTP {code}: {exc}"

    async def _preflight(self) -> tuple[str, str] | None:
        """Open one connection and close it. Returns the reason it could not,
        with its fault kind."""
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
                f"could not reach {endpoint_of(self.url)} within {self.open_timeout_s:.0f} s. "
                f"Speech recognition needs a network; the wake word does not."
            ), "start_failed"
        except Exception as exc:  # noqa: BLE001 - every failure here is reported, not raised
            judged = self._judge(exc)
            if judged is not None:
                kind, detail = judged
                return detail, kind
            code = _status_code(exc)
            if code in (401, 403):
                return (
                    f"Deepgram rejected the key in {self.key_env_name} (HTTP {code}). "
                    f"Check the key, and that it has the transcription scope."
                ), "start_failed"
            if code is not None:
                return self._refused(exc), "start_failed"
            return (
                f"could not reach {endpoint_of(self.url)}: {type(exc).__name__}: {exc}. "
                f"Speech recognition needs a network; the wake word does not."
            ), "start_failed"
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

    def _fail(self, reason: str, *, kind: str = "start_failed") -> None:
        # INFO: the engine prints the reason through `health().detail`, so
        # logging's last resort, which prints WARNING and above, would print
        # it twice (contract 0.5.7 section 3.3, L16).
        self._fault = reason
        self._fault_kind = kind
        log.info("deepgram: %s", reason)

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
                # INFO: the engine prints `Transcript.error` (L16).
                log.info("deepgram: %s; returning what was heard", error)
            else:
                try:
                    utterance.task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reported, and the words so far returned
                    error = self._utterance_error(exc)
                    self.last_error = error
                    log.info("deepgram: %s; returning what was heard", error)

        confidences = utterance.final_confidences or [utterance.interim_confidence]
        confidence = max(0.0, min(1.0, sum(confidences) / len(confidences)))
        return Transcript(text=utterance.text, final=True, confidence=confidence, error=error)

    def _utterance_error(self, exc: BaseException) -> str:
        """What a failed utterance's final says. A refusal of a known kind
        is the owner's sentence, and `health()` reports its kind from here;
        a refused handshake of no known kind quotes Deepgram; anything else
        is the exception, as it came."""
        judged = self._judge(exc)
        if judged is not None:
            self._run_fault = judged
            return judged[1]
        if self._run_fault is not None and self._run_fault[0] == "out_of_credit":
            # A failure that names no kind is news of the network, never of the account.
            self._run_fault = None
        if _status_code(exc) is not None:
            return self._refused(exc)
        return f"{type(exc).__name__}: {exc}"

    async def _pump(self, utterance: _Utterance) -> None:
        """Connect, then send frames and receive results until both sides end."""
        assert self._connect is not None
        ws = await self._connect(
            self.request_url(),
            additional_headers=self._headers(),
            open_timeout=self.open_timeout_s,
        )
        if self._run_fault is not None and self._run_fault[0] == "out_of_credit":
            # Deepgram took the connection, so the account pays again.
            self._run_fault = None
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
