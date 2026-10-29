"""One real sentence from the cloud voice, when a key is present and asked for.

Skipped in CI and on any machine without `EMET_LIVE_TESTS=1` and the key in
the environment. It spends a few dozen characters. What it proves is the
half the stand-in cannot: that the URL, the query and the header are what
the live service accepts today, and that raw linear16 comes back at the
rate asked for. Nothing is played; the audio is counted and its length
checked against the rate. Run by hand before a release, and whenever the
vendor announces a change.

With `EMET_LIVE_RECORD=<dir>` as well, the audio is kept chunk by chunk as
it came, with the `dg-model-name` header when the vendor sends one
(`conftest.py`; which headers Aura sends is unverified). The test below the
live one checks that mode against a stand-in, and runs wherever httpx is
installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

LIVE = os.environ.get("EMET_LIVE_TESTS") == "1"

SENTENCE = "Emet is ready."


def _speak(tts):
    async def scenario():
        await tts.start()
        assert tts.describe().healthy, tts.health().detail
        chunks = [c async for c in tts.speak(SENTENCE)]
        await tts.shutdown()
        return chunks

    return asyncio.run(scenario())


def _reported_model(record) -> str | None:
    """The model a speak response named in its headers, if any did."""
    for exchange in reversed(record.exchanges):
        name = (exchange.get("headers") or {}).get("dg-model-name")
        if name:
            return name
    return None


@pytest.mark.skipif(
    not (LIVE and os.environ.get("EMET_DEEPGRAM_KEY")),
    reason="set EMET_DEEPGRAM_KEY and EMET_LIVE_TESTS=1 to call Deepgram for real",
)
def test_deepgram_speaks(live_record):
    pytest.importorskip("httpx", reason="emet-providers[deepgram] is not installed")
    from emet_providers.deepgram_voice import DeepgramVoice

    transport = None
    if live_record is not None:
        live_record.vendor = "deepgram"
        transport = live_record.transport()
    tts = DeepgramVoice({"provider": "deepgram", "params": {"sample_rate": 24000}}, transport=transport)
    if live_record is not None:
        live_record.model_asked = tts.model_name

    try:
        chunks = _speak(tts)
    finally:
        if live_record is not None:
            live_record.model = _reported_model(live_record)
    pcm = b"".join(chunks)
    assert chunks, "no audio came back"
    assert all(len(c) % 2 == 0 for c in chunks)
    seconds = len(pcm) / 2 / 24000
    # Three words take about a second to say; a header, or the wrong rate,
    # would put this far outside the range.
    assert 0.4 < seconds < 4.0, f"{seconds:.2f} s of audio for {SENTENCE!r}"
    assert tts.last_error is None, tts.last_error


# ------------------------------------------------------- the recording mode


FAKE_KEY = "dg-recording-test-key-0123456789"


def test_the_recording_mode_keeps_the_audio_as_it_came_and_never_the_key(monkeypatch, tmp_path, live_recorder):
    httpx = pytest.importorskip("httpx", reason="emet-providers[deepgram] is not installed")
    from emet_providers.deepgram_voice import DeepgramVoice

    monkeypatch.setenv("EMET_DEEPGRAM_KEY", FAKE_KEY)
    audio = [bytes(range(256)) * 4, bytes(2048)]
    seen: list = []

    class Pieces(httpx.AsyncByteStream):
        async def __aiter__(self):
            for piece in audio:
                yield piece

    def server(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            stream=Pieces(),
            headers={"content-type": "audio/l16", "dg-model-name": "aura-2-test", "dg-auth": FAKE_KEY},
        )

    record = live_recorder(tmp_path)
    record.vendor = "deepgram"
    tts = DeepgramVoice(
        {"provider": "deepgram", "params": {"sample_rate": 24000}},
        transport=record.transport(httpx.MockTransport(server)),
    )
    chunks = _speak(tts)
    record.model = _reported_model(record)
    assert b"".join(chunks) == b"".join(audio)
    assert seen and all(h == f"Token {FAKE_KEY}" for h in seen), "the key still reached the server"

    text = record.save().read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    data = json.loads(text)
    assert data["model"] == "aura-2-test"
    speak = data["exchanges"][-1]
    assert speak["body"] == {"text": SENTENCE}
    assert "dg-auth" not in speak["headers"]
    assert [base64.b64decode(c["bytes_b64"]) for c in speak["chunks"]] == audio, "audio kept as bytes, chunk by chunk"
