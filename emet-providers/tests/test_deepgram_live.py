"""One real round trip to Deepgram, when a key is present and asked for.

Skipped in CI and on any machine without both `EMET_DEEPGRAM_KEY` and
`EMET_LIVE_TESTS=1` in the environment. It sends two seconds of silence, so
the transcript is expected to be empty; what it proves is the half the fake
cannot: that the handshake, the header, the query string and the CloseStream
sequence are what the live service accepts today. Run it by hand before a
release, and whenever Deepgram announces a change.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from emet_sdk.types import AudioFormat

from emet_providers.deepgram import DEFAULT_KEY_ENV, DeepgramTranscriber

pytestmark = pytest.mark.skipif(
    not (os.environ.get(DEFAULT_KEY_ENV) and os.environ.get("EMET_LIVE_TESTS") == "1"),
    reason=f"set {DEFAULT_KEY_ENV} and EMET_LIVE_TESTS=1 to call Deepgram for real",
)


def test_a_live_round_trip_returns_a_final():
    pytest.importorskip("websockets", reason="emet-providers[deepgram] is not installed")
    stt = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat())

    async def scenario():
        await stt.start()
        assert stt.describe().healthy, stt.health().detail
        for _ in range(25):  # two seconds of silence at 80 ms frames
            await stt.feed(bytes(2560))
            await asyncio.sleep(0.08)
        final = await stt.finish()
        await stt.shutdown()
        return final

    final = asyncio.run(scenario())
    assert final.final
    assert isinstance(final.text, str)
    assert stt.last_error is None, stt.last_error
