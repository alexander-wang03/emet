"""The mock transcriber, and through it the transcriber contract.

No network, no key, no audio anybody could hear. Words are written into
frames as bytes and read back out, which is what makes every test of the
speech path above this a one-liner.
"""

from __future__ import annotations

import asyncio

import pytest

from emet_sdk.plugin import TranscriberPlugin
from emet_sdk.types import AudioFormat, Transcript, TranscriberDescriptor

from emet_providers.mock import MIN_CHARS, MockTranscriber, spelled

FRAME_BYTES = 2560


def run(coro):
    return asyncio.run(coro)


def frame(payload: bytes = b"") -> bytes:
    # bytes(n) rather than an escape: zero bytes without a backslash in sight.
    return payload + bytes(FRAME_BYTES - len(payload))


def make(**params) -> MockTranscriber:
    return MockTranscriber({"provider": "mock", "params": params}, AudioFormat())


def started(**params) -> MockTranscriber:
    stt = make(**params)
    run(stt.start())
    return stt


# --------------------------------------------------------------- spelling


def test_a_frame_that_begins_with_text_spells_it():
    assert spelled(frame(b"what time is it")) == "what time is it"


def test_silence_spells_nothing():
    assert spelled(frame()) == ""


def test_loud_audio_spells_nothing():
    """int16 samples of a tone. Bytes outside the printable range appear at
    once, so this is audio rather than words."""
    from array import array

    pcm = array("h", [4000, -4000] * (FRAME_BYTES // 4)).tobytes()
    assert spelled(pcm) == ""


def test_a_short_run_is_audio_not_a_word():
    """A printable byte then a zero is an ordinary small sample."""
    assert spelled(frame(b"A")) == ""
    assert spelled(frame(b"ab")) == ""
    assert len("abc") == MIN_CHARS
    assert spelled(frame(b"abc")) == "abc"


# ---------------------------------------------------------------- contract


def test_the_mock_is_a_transcriber_plugin():
    stt = make()
    assert isinstance(stt, TranscriberPlugin)
    assert stt.provider == "mock"
    assert stt.format == AudioFormat()


def test_the_config_is_unpacked_the_way_the_engine_hands_it_over():
    stt = MockTranscriber(
        {"provider": "mock", "model": "tiny", "key_env": "EMET_MOCK_KEY", "params": {"x": 1}},
        AudioFormat(),
    )
    assert stt.model == "tiny"
    assert stt.key_env == "EMET_MOCK_KEY"
    assert stt.params == {"x": 1}


def test_a_missing_model_and_key_env_are_none_not_strings():
    stt = make()
    assert stt.model is None
    assert stt.key_env is None


def test_describe_reports_the_format_it_was_given_and_that_it_streams():
    stt = started()
    d = stt.describe()
    assert isinstance(d, TranscriberDescriptor)
    assert d.healthy
    assert d.streaming
    assert d.sample_rate == 16000
    assert d.provider == "mock"


# --------------------------------------------------------------- streaming


def test_words_arrive_as_growing_partials_then_one_final():
    stt = started()

    async def scenario():
        partials = []
        for f in (frame(b"what"), frame(), frame(b"time"), frame(b"is it")):
            if (t := await stt.feed(f)) is not None:
                partials.append(t)
        final = await stt.finish()
        return partials, final

    partials, final = run(scenario())
    assert [p.text for p in partials] == ["what", "what time", "what time is it"]
    assert all(not p.final for p in partials)
    assert final == Transcript(text="what time is it", final=True, confidence=1.0)
    assert stt.frames == 4
    assert stt.finished == 1


def test_silence_alone_gives_an_empty_final():
    """A real answer: they said nothing the provider could make out."""
    stt = started()

    async def scenario():
        assert await stt.feed(frame()) is None
        return await stt.finish()

    final = run(scenario())
    assert final.final
    assert final.text == ""


def test_finish_resets_for_the_next_utterance():
    stt = started()

    async def scenario():
        await stt.feed(frame(b"first"))
        first = await stt.finish()
        await stt.feed(frame(b"second"))
        second = await stt.finish()
        return first.text, second.text

    assert run(scenario()) == ("first", "second")


def test_a_scripted_transcript_streams_one_word_a_frame():
    """For a live microphone, whose bytes spell nothing: the mock says what it
    was told to, and still exercises the partial path."""
    stt = started(transcript="hello there emet")

    async def scenario():
        partials = [await stt.feed(frame()) for _ in range(5)]
        final = await stt.finish()
        return partials, final

    partials, final = run(scenario())
    texts = [p.text for p in partials if p is not None]
    assert texts == ["hello", "hello there", "hello there emet"]
    assert partials[3] is None and partials[4] is None
    assert final.text == "hello there emet" and final.final


# -------------------------------------------------------------- unhealthy


def test_an_unreachable_service_is_unhealthy_not_an_exception():
    stt = started(fail_on_start=True)
    assert not stt.describe().healthy
    health = stt.health()
    assert not health.ok
    assert "unreachable" in (health.detail or "")


def test_an_unhealthy_transcriber_hears_nothing():
    stt = started(fail_on_start=True)
    assert run(stt.feed(frame(b"what time is it"))) is None


def test_a_missing_key_is_reported_by_the_name_of_the_variable(monkeypatch):
    monkeypatch.delenv("EMET_MOCK_KEY", raising=False)
    stt = MockTranscriber(
        {"provider": "mock", "key_env": "EMET_MOCK_KEY", "params": {"require_key": True}},
        AudioFormat(),
    )
    run(stt.start())
    assert not stt.describe().healthy
    assert "EMET_MOCK_KEY" in (stt.health().detail or "")


def test_a_present_key_is_enough(monkeypatch):
    monkeypatch.setenv("EMET_MOCK_KEY", "anything")
    stt = MockTranscriber(
        {"provider": "mock", "key_env": "EMET_MOCK_KEY", "params": {"require_key": True}},
        AudioFormat(),
    )
    run(stt.start())
    assert stt.describe().healthy


def test_requiring_a_key_with_no_key_env_named_says_so():
    stt = started(require_key=True)
    assert not stt.describe().healthy
    assert "key_env" in (stt.health().detail or "")


def test_a_rate_override_shows_in_the_descriptor():
    """So a test above can watch the engine refuse a mismatch."""
    stt = started(sample_rate=8000)
    assert stt.describe().sample_rate == 8000


def test_shutdown_forgets_the_utterance_in_progress():
    stt = started()
    run(stt.feed(frame(b"half a")))
    run(stt.shutdown())
    assert not stt.describe().healthy
    assert run(stt.feed(frame(b"thought"))) is None


# ----------------------------------------------------------------- version


def test_the_reported_version_matches_the_installed_distribution():
    """One declaration, in `pyproject.toml`, read back at import time, like
    the other three packages. See their tests for the drift that motivated
    it."""
    from importlib.metadata import version

    import emet_providers

    assert emet_providers.__version__ == version("emet-providers")
    assert emet_providers.__version__ != "0+unknown", "package is not installed"
