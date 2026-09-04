"""The listen loop.

Runs entirely offline: a wav file standing in for a microphone and the mock
detector standing in for pocketsphinx. No hardware, no acoustic model, and
nothing recorded from whoever runs the suite.

Both of those stand-ins arrive through entry-point discovery, so these tests
exercise the same path the real thing uses. Only the names differ.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from emet_engine.session import EngineError, ListenSession
from emet_sdk.validate import MissingPluginError

PHRASE = "hey emet"


def run(coro):
    return asyncio.run(coro)


def write_wav(path: Path, pcm: bytes) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return str(path)


def silence(frames: int) -> bytes:
    return b"\x00\x00" * frames


def saying(times: int) -> bytes:
    """PCM whose bytes literally spell the phrase, which is what the mock
    detector listens for. Not audio anybody could hear; it exercises the loop."""
    return (silence(1280) + PHRASE.encode() + silence(1280)) * times


def body(
    path: str,
    *,
    engine: str = "mock",
    source: str = "wav",
    sink: str = "null",
    **wake_params,
) -> dict:
    """A body whose ears are a file and whose mouth is a bin.

    `sink="null"` is not incidental. The default is a real speaker, so a test
    body that said nothing about output would open whatever is plugged into the
    machine running the suite — which fails on a headless CI runner and is rude
    on a laptop. Tests state where their audio goes.
    """
    return {
        "audio": {
            "input": {"source": source, "device": "file", "params": {"path": path}},
            "output": {"sink": sink, "device": "none"},
            "wake": {"engine": engine, "params": wake_params},
        }
    }


def soul(wake_word: str | None = PHRASE) -> dict:
    identity = {"name": "Emet"}
    if wake_word is not None:
        identity["wake_word"] = wake_word
    return {"identity": identity}


# ------------------------------------------------------------------ startup


def test_a_session_brings_up_both_halves_of_the_floor(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "a.wav", silence(1280))), soul())

    async def scenario():
        async with session:
            return session.descriptor, session.format

    descriptor, fmt = run(scenario())
    assert descriptor is not None and descriptor.healthy
    assert fmt is not None
    assert session.engine_name == "mock"
    assert session.source_name == "wav"


def test_the_detector_states_the_format_and_the_source_is_made_to_fit(tmp_path):
    """The ordering that matters. Opening the microphone first and hoping the
    detector agrees is how a robot runs perfectly and hears nothing: feeding
    48 kHz audio to a 16 kHz model raises nothing at all."""
    session = ListenSession(body(write_wav(tmp_path / "b.wav", silence(1280))), soul())

    async def scenario():
        async with session:
            return session.descriptor, session.format

    descriptor, fmt = run(scenario())
    assert fmt.sample_rate == descriptor.sample_rate
    assert fmt.frame_samples == descriptor.frame_samples


def test_defaults_apply_when_the_manifest_says_nothing(tmp_path):
    """Most manifests will mention none of these, so the defaults are what
    actually runs most of the time. Constructing does not open anything."""
    session = ListenSession({"audio": {"input": {"device": "x"}}}, soul())
    assert session.engine_name == "pocketsphinx"
    assert session.source_name == "microphone"
    assert session.sink_name == "speaker"


# ------------------------------------------------- the check with no fallback


def test_a_detector_that_cannot_hear_the_name_refuses_to_start(tmp_path):
    """Every other missing piece degrades. This one has no next rung, so the
    session refuses rather than running deaf."""
    manifest = body(write_wav(tmp_path / "c.wav", silence(1280)), phrases=["hey jarvis"])
    session = ListenSession(manifest, soul())

    with pytest.raises(EngineError) as exc:
        run(session.start())
    message = str(exc.value)
    assert PHRASE in message
    assert "own name" in message
    run(session.stop())


def test_a_soul_with_no_wake_word_is_refused(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "d.wav", silence(1280))), soul(None))
    with pytest.raises(EngineError, match="wake_word"):
        run(session.start())


def test_an_uninstalled_engine_is_a_missing_plugin(tmp_path):
    manifest = body(write_wav(tmp_path / "e.wav", silence(1280)), engine="porcupine")
    with pytest.raises(MissingPluginError):
        run(ListenSession(manifest, soul()).start())


def test_an_uninstalled_source_is_a_missing_plugin(tmp_path):
    manifest = body(write_wav(tmp_path / "f.wav", silence(1280)), source="carrier_pigeon")
    session = ListenSession(manifest, soul())
    with pytest.raises(MissingPluginError):
        run(session.start())
    run(session.stop())


# --------------------------------------------------------------------- loop


def test_silence_yields_nothing(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "g.wav", silence(1280 * 10))), soul())

    async def scenario():
        async with session:
            return [e async for e in session.wakes()]

    assert run(scenario()) == []


def test_each_utterance_yields_exactly_one_event(tmp_path):
    """Once per wake, not once per frame. The detector's hypothesis persists
    until the utterance is reset, so the loop resets after every event."""
    session = ListenSession(body(write_wav(tmp_path / "h.wav", saying(3))), soul())

    async def scenario():
        async with session:
            return [e async for e in session.wakes()]

    events = run(scenario())
    assert len(events) == 3
    assert {e.phrase for e in events} == {PHRASE}


def test_the_loop_ends_when_the_source_does(tmp_path):
    """A file ends; a microphone never should. That the loop terminates at all
    is what makes `--replay` finish rather than hang."""
    session = ListenSession(body(write_wav(tmp_path / "i.wav", saying(1))), soul())

    async def scenario():
        async with session:
            n = 0
            async for _ in session.wakes():
                n += 1
            return n

    assert run(scenario()) == 1


# ---------------------------------------------------------------- turns


FRAME_BYTES = 2560


def frame(payload: bytes = b"") -> bytes:
    # bytes(n) rather than an escape: zero bytes without a backslash in sight.
    return payload + bytes(FRAME_BYTES - len(payload))


def loud_frame(amplitude: int = 4000) -> bytes:
    from array import array

    return array("h", [amplitude, -amplitude] * (FRAME_BYTES // 4)).tobytes()


def test_a_turn_is_a_wake_and_the_speech_after_it(tmp_path):
    """The 0.3 loop end to end: hear the name, capture what follows, stop when
    they stop."""
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 5 + frame() * 20
    manifest = body(write_wav(tmp_path / "turn.wav", pcm))
    session = ListenSession(manifest, soul())

    async def scenario():
        async with session:
            return [(e, u) async for e, u in session.turns()]

    turns = run(scenario())
    assert len(turns) == 1
    event, utterance = turns[0]
    assert event.phrase == PHRASE
    assert utterance.had_speech
    assert utterance.reason.value == "silence"
    assert utterance.audio


def test_a_wake_with_silence_after_it_is_reported_as_such(tmp_path):
    """A false wake is not the same as a question, and the engine above has to
    be able to tell them apart."""
    pcm = frame() + frame(PHRASE.encode()) + frame() * 60
    session = ListenSession(body(write_wav(tmp_path / "empty.wav", pcm)), soul())

    async def scenario():
        async with session:
            return [(e, u) async for e, u in session.turns()]

    turns = run(scenario())
    assert len(turns) == 1
    assert not turns[0][1].had_speech


def test_patience_comes_from_the_soul(tmp_path):
    session = ListenSession(
        body(write_wav(tmp_path / "p.wav", frame())),
        {"identity": {"wake_word": PHRASE}, "interaction": {"patience_ms": 2500}},
    )
    assert session.patience_ms == 2500


def test_patience_defaults_when_the_soul_is_silent_about_it(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "q.wav", frame())), soul())
    assert session.patience_ms == 900


def test_iterating_turns_before_starting_is_an_error(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "r.wav", frame())), soul())

    async def scenario():
        with pytest.raises(EngineError, match="not started"):
            async for _ in session.turns():
                pass

    run(scenario())


def test_iterating_before_starting_is_an_error(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "j.wav", silence(1280))), soul())

    async def scenario():
        with pytest.raises(EngineError, match="not started"):
            async for _ in session.wakes():
                pass

    run(scenario())


# ------------------------------------------------------------------ teardown


def test_stopping_twice_is_safe(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "k.wav", silence(1280))), soul())

    async def scenario():
        await session.start()
        await session.stop()
        await session.stop()

    run(scenario())


def test_stopping_after_a_failed_start_is_safe(tmp_path):
    """`start()` raises part way through, having brought up the detector but
    not the source. Teardown must cope with that."""
    manifest = body(write_wav(tmp_path / "l.wav", silence(1280)), phrases=["hey jarvis"])
    session = ListenSession(manifest, soul())

    async def scenario():
        with pytest.raises(EngineError):
            await session.start()
        await session.stop()

    run(scenario())


def test_a_file_drops_nothing(tmp_path):
    """`dropped` is read defensively: the Protocol does not require it, and a
    file cannot fall behind."""
    session = ListenSession(body(write_wav(tmp_path / "m.wav", silence(1280))), soul())

    async def scenario():
        async with session:
            return session.dropped

    assert run(scenario()) == 0
