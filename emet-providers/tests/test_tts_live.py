"""One real sentence from the cloud voice, when a key is present and asked for.

Skipped in CI and on any machine without `EMET_LIVE_TESTS=1` and the key in
the environment. It spends a few dozen characters. What it proves is the
half the stand-in cannot: that the URL, the query and the header are what
the live service accepts today, and that raw linear16 comes back at the
rate asked for. Nothing is played; the audio is counted and its length
checked against the rate. Run by hand before a release, and whenever the
vendor announces a change.
"""

from __future__ import annotations

import asyncio
import os

import pytest

LIVE = os.environ.get("EMET_LIVE_TESTS") == "1"

SENTENCE = "Emet is ready."


@pytest.mark.skipif(
    not (LIVE and os.environ.get("EMET_DEEPGRAM_KEY")),
    reason="set EMET_DEEPGRAM_KEY and EMET_LIVE_TESTS=1 to call Deepgram for real",
)
def test_deepgram_speaks():
    pytest.importorskip("httpx", reason="emet-providers[deepgram] is not installed")
    from emet_providers.deepgram_voice import DeepgramVoice

    tts = DeepgramVoice({"provider": "deepgram", "params": {"sample_rate": 24000}})

    async def scenario():
        await tts.start()
        assert tts.describe().healthy, tts.health().detail
        chunks = [c async for c in tts.speak(SENTENCE)]
        await tts.shutdown()
        return chunks

    chunks = asyncio.run(scenario())
    pcm = b"".join(chunks)
    assert chunks, "no audio came back"
    assert all(len(c) % 2 == 0 for c in chunks)
    seconds = len(pcm) / 2 / 24000
    # Three words take about a second to say; a header, or the wrong rate,
    # would put this far outside the range.
    assert 0.4 < seconds < 4.0, f"{seconds:.2f} s of audio for {SENTENCE!r}"
    assert tts.last_error is None, tts.last_error
