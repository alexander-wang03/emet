"""The Deepgram voice, against a stand-in for the speak API.

No network and no key. An `httpx.MockTransport` answers in the shapes
Deepgram's API reference gives (verified 2026-09-15; the 401 body was
checked against the live endpoint with a bogus key the same day) and
records what it was sent. What these tests prove is the plugin's half of the
protocol: the URL, the query, the header, the body, and what it does with
the bytes that come back and with the errors. The live test beside this
file proves the other half, when a key is present.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import pytest

httpx = pytest.importorskip("httpx", reason="emet-providers[deepgram] is not installed")

from emet_sdk.plugin import PluginError, VoicePlugin  # noqa: E402

from emet_providers.deepgram_voice import (  # noqa: E402
    DEFAULT_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_URL,
    MAX_CHARS,
    PREFLIGHT_TEXT,
    DeepgramVoice,
    pieces,
)

KEY = "dg_test_key"


def run(coro):
    return asyncio.run(coro)


async def collect(chunks) -> list[bytes]:
    return [c async for c in chunks]


# -------------------------------------------------------------- the fake


class _Pieces(httpx.AsyncByteStream):
    """A response body delivered in the pieces a network would."""

    def __init__(self, parts: list[bytes]) -> None:
        self.parts = parts

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            yield part


class FakeSpeak:
    def __init__(
        self,
        *,
        status: int = 200,
        parts: list[bytes] | None = None,
        audio: bytes | None = None,
        error: dict | None = None,
        raise_on_connect: Exception | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.status = status
        self.parts = parts
        self.audio = audio
        self.error = error
        self.raise_on_connect = raise_on_connect
        self.fail_after = fail_after
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            return httpx.Response(500, json={"err_code": "INTERNAL", "err_msg": "boom"})
        if self.status != 200:
            return httpx.Response(self.status, json=self.error or {"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials."})
        if self.parts is not None:
            return httpx.Response(200, stream=_Pieces(self.parts), headers={"content-type": "audio/l16"})
        text = json.loads(request.content)["text"]
        body = self.audio if self.audio is not None else text.encode() + bytes(2)
        return httpx.Response(200, content=body, headers={"content-type": "audio/l16"})

    @property
    def bodies(self) -> list[str]:
        return [json.loads(r.content)["text"] for r in self.requests]


def make(fake: FakeSpeak, *, config: dict | None = None, voice: dict | None = None, **params: Any) -> DeepgramVoice:
    cfg: dict[str, Any] = {"provider": "deepgram", "params": params}
    cfg.update(config or {})
    return DeepgramVoice(cfg, voice, transport=httpx.MockTransport(fake))


def started(fake: FakeSpeak, monkeypatch, **kw) -> DeepgramVoice:
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    tts = make(fake, **kw)
    run(tts.start())
    assert tts.describe().healthy, tts.health().detail
    return tts


# --------------------------------------------------------------- request


def test_it_is_a_voice_plugin_registered_as_deepgram():
    tts = make(FakeSpeak())
    assert isinstance(tts, VoicePlugin)
    assert tts.provider == "deepgram"
    assert tts.url == DEFAULT_URL and tts.model_name == DEFAULT_MODEL


def test_the_request_is_the_documented_one(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch, preflight=False)
    run(collect(tts.speak("Hello there.")))
    (request,) = fake.requests
    assert request.method == "POST"
    assert request.url.scheme == "https" and request.url.host == "api.deepgram.com"
    assert request.url.path == "/v1/speak"
    assert dict(request.url.params) == {
        "model": DEFAULT_MODEL,
        "encoding": "linear16",
        "container": "none",
        "sample_rate": str(DEFAULT_SAMPLE_RATE),
    }
    assert request.headers["authorization"] == f"Token {KEY}"
    assert request.headers["content-type"] == "application/json"
    assert fake.bodies == ["Hello there."]


def test_the_souls_voice_and_the_bodys_rate_are_sent_and_reported(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch, config={"model": "aura-2-apollo-en"}, sample_rate=16000, preflight=False)
    run(collect(tts.speak("Hi.")))
    params = dict(fake.requests[0].url.params)
    assert params["model"] == "aura-2-apollo-en" and params["sample_rate"] == "16000"
    d = tts.describe()
    assert d.model == "aura-2-apollo-en" and d.sample_rate == 16000 and d.streaming


def test_a_rate_linear16_does_not_offer_falls_back_to_the_default(monkeypatch):
    tts = make(FakeSpeak(), sample_rate=22050)
    assert tts.sample_rate == DEFAULT_SAMPLE_RATE


def test_the_souls_speaking_rate_is_the_speed_parameter(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch, voice={"rate": 1.2}, preflight=False)
    run(collect(tts.speak("Hi.")))
    assert dict(fake.requests[0].url.params)["speed"] == "1.2"


def test_a_rate_outside_the_range_is_clamped(monkeypatch):
    assert make(FakeSpeak(), voice={"rate": 3.0}).speed == 1.5
    assert make(FakeSpeak(), voice={"rate": 0.1}).speed == 0.7
    assert "speed" not in make(FakeSpeak()).query()


def test_body_query_params_ride_along_but_cannot_change_the_format(monkeypatch):
    fake = FakeSpeak()
    tts = started(
        fake,
        monkeypatch,
        preflight=False,
        query={"tag": "emet", "mip_opt_out": True, "encoding": "mp3", "container": "wav", "model": "x"},
    )
    run(collect(tts.speak("Hi.")))
    params = dict(fake.requests[0].url.params)
    assert params["tag"] == "emet" and params["mip_opt_out"] == "true"
    assert params["encoding"] == "linear16" and params["container"] == "none" and params["model"] == DEFAULT_MODEL


def test_the_url_can_point_at_a_self_hosted_deployment(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch, preflight=False, url="https://speak.example.test/v1/speak")
    run(collect(tts.speak("Hi.")))
    assert str(fake.requests[0].url).startswith("https://speak.example.test/v1/speak?")


# -------------------------------------------------------------- lifecycle


def test_preflight_says_one_word_and_discards_it(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch)
    assert fake.bodies == [PREFLIGHT_TEXT]
    assert len(PREFLIGHT_TEXT) <= 6, "the preflight is meant to cost nothing"
    assert tts.describe().healthy


def test_preflight_can_be_turned_off(monkeypatch):
    fake = FakeSpeak()
    started(fake, monkeypatch, preflight=False)
    assert fake.requests == []


def test_no_key_is_unhealthy_names_the_variable_and_sends_nothing(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    fake = FakeSpeak()
    tts = make(fake)
    run(tts.start())
    assert not tts.describe().healthy
    assert DEFAULT_KEY_ENV in (tts.health().detail or "")
    assert tts.health().faults == ("no_key",)
    assert fake.requests == []


def test_the_souls_key_env_is_honoured(monkeypatch):
    monkeypatch.delenv(DEFAULT_KEY_ENV, raising=False)
    monkeypatch.setenv("EMET_OTHER_KEY", "other")
    fake = FakeSpeak()
    tts = make(fake, config={"key_env": "EMET_OTHER_KEY"})
    run(tts.start())
    assert tts.describe().healthy
    assert fake.requests[0].headers["authorization"] == "Token other"


def test_a_rejected_key_is_unhealthy_with_the_status(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, "bad")
    tts = make(FakeSpeak(status=401))
    run(tts.start())
    assert not tts.describe().healthy
    detail = tts.health().detail or ""
    assert "rejected the key" in detail and "401" in detail and DEFAULT_KEY_ENV in detail


def test_an_unknown_voice_is_refused_at_boot_in_the_vendors_words(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    tts = make(FakeSpeak(status=400, error={"err_code": "Bad Request", "err_msg": "Model not found"}), config={"model": "aura-2-nobody-en"})
    run(tts.start())
    assert not tts.describe().healthy
    detail = tts.health().detail or ""
    assert "aura-2-nobody-en" in detail and "Model not found" in detail


def test_an_unreachable_network_is_unhealthy_and_says_so(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    tts = make(FakeSpeak(raise_on_connect=httpx.ConnectError("no route")))
    run(tts.start())
    assert not tts.describe().healthy
    assert "could not reach" in (tts.health().detail or "")


def test_without_httpx_the_hint_names_the_extra(monkeypatch):
    import sys

    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    monkeypatch.setitem(sys.modules, "httpx", None)
    tts = DeepgramVoice({"provider": "deepgram"})
    run(tts.start())
    assert "emet-providers[deepgram]" in (tts.health().detail or "")


def test_shutdown_closes_the_client_and_reports_unhealthy(monkeypatch):
    tts = started(FakeSpeak(), monkeypatch)
    run(tts.shutdown())
    assert not tts.describe().healthy and tts._client is None


# ----------------------------------------------------------------- audio


def test_audio_streams_back_in_whole_samples(monkeypatch):
    """A chunk boundary between the two bytes of a sample is carried over."""
    fake = FakeSpeak(parts=[b"\x01\x02\x03", b"\x04\x05\x06\x07", b"\x08"])
    tts = started(fake, monkeypatch, preflight=False)
    chunks = run(collect(tts.speak("Hi.")))
    assert chunks == [b"\x01\x02", b"\x03\x04\x05\x06", b"\x07\x08"]
    assert all(len(c) % 2 == 0 for c in chunks)


def test_a_dangling_byte_at_the_end_is_dropped_not_played(monkeypatch):
    fake = FakeSpeak(parts=[b"\x01\x02\x03"])
    tts = started(fake, monkeypatch, preflight=False)
    assert run(collect(tts.speak("Hi."))) == [b"\x01\x02"]


def test_blank_text_sends_nothing(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch, preflight=False)
    assert run(collect(tts.speak("   "))) == []
    assert fake.requests == []


def test_a_long_reply_is_split_at_sentences_and_sent_in_order(monkeypatch):
    fake = FakeSpeak()
    tts = started(fake, monkeypatch, preflight=False)
    first = "A" * (MAX_CHARS - 10) + "."
    second = "B" * 50 + "."
    run(collect(tts.speak(f"{first} {second}")))
    assert fake.bodies == [first, second]


def test_pieces_splits_at_sentences_then_words_then_characters():
    assert pieces("short") == ["short"]
    assert pieces("  spaced   out  ") == ["spaced out"]
    assert pieces("") == []
    assert pieces("one two three four", limit=9) == ["one two", "three", "four"]
    assert pieces("First one. Second one! Third?", limit=12) == ["First one.", "Second one!", "Third?"]
    assert pieces("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]
    for piece in pieces("word " * 1000):
        assert len(piece) <= MAX_CHARS


def test_an_http_error_mid_reply_loses_the_sentence_not_the_plugin(monkeypatch):
    fake = FakeSpeak(fail_after=1)
    tts = started(fake, monkeypatch, preflight=False)
    assert run(collect(tts.speak("First."))) == [b"First.\x00\x00"]
    with pytest.raises(PluginError) as exc:
        run(collect(tts.speak("Second.")))
    assert "HTTP 500 INTERNAL: boom" in str(exc.value)
    assert tts.last_error == "HTTP 500 INTERNAL: boom"
    assert tts.describe().healthy


def test_a_network_failure_mid_reply_is_a_plugin_error(monkeypatch):
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    tts = make(FakeSpeak(raise_on_connect=httpx.ReadTimeout("slow")), preflight=False)
    run(tts.start())
    with pytest.raises(PluginError, match="ReadTimeout"):
        run(collect(tts.speak("Hello.")))


def test_speaking_before_start_is_an_error():
    tts = make(FakeSpeak())

    async def scenario():
        with pytest.raises(PluginError, match="not started"):
            await collect(tts.speak("Hello."))

    run(scenario())


def test_the_module_names_its_verification_and_prices():
    from emet_providers import deepgram_voice

    doc = deepgram_voice.__doc__ or ""
    assert "2026-09-15" in doc
    assert "$0.030" in doc and "2000 characters" in doc
    assert "/v2/speak" in doc, "Flux TTS is named as the protocol this file does not speak"
