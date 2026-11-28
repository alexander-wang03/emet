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
    machine running the suite, which fails on a headless CI runner and is rude
    on a laptop. Tests state where their audio goes.
    """
    return {
        "audio": {
            "input": {"source": source, "device": "file", "params": {"path": path}},
            "output": {"sink": sink, "device": "none"},
            "wake": {"engine": engine, "params": wake_params},
        }
    }


def soul(wake_word: str | None = PHRASE, chat: dict | None = None, **stt) -> dict:
    identity = {"name": "Emet"}
    if wake_word is not None:
        identity["wake_word"] = wake_word
    doc: dict = {"identity": identity}
    models: dict = {}
    if stt:
        models["stt"] = stt
    if chat is not None:
        models["chat"] = chat
    if models:
        doc["models"] = models
    return doc


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


# ----------------------------------------------------------------- version


def test_the_reported_version_matches_the_installed_distribution():
    """One declaration, in `pyproject.toml`, read back at import time.

    This test exists because the two used to be written separately and drifted:
    the installed metadata said 0.1.0 while the source said 0.2.0, and
    nothing noticed because nothing compared them. A version that is quietly
    wrong is worse than no version, because it gets reported in bug reports as
    fact.
    """
    from importlib.metadata import version

    import emet_engine

    assert emet_engine.__version__ == version("emet-engine")
    assert emet_engine.__version__ != "0+unknown", "package is not installed"


def test_a_file_overflows_nothing(tmp_path):
    """`overflows` is read defensively like `dropped`, since a file has no card."""
    session = ListenSession(body(write_wav(tmp_path / "o.wav", silence(1280))), soul())

    async def scenario():
        async with session:
            return session.overflows

    assert run(scenario()) == 0



def test_a_plugin_with_nothing_to_remember_yields_no_hint(tmp_path):
    """The mock has no warm state and no method for it. The session asks by
    duck typing and gets None, which is the whole layering rule in miniature:
    the engine never names an engine."""
    session = ListenSession(body(write_wav(tmp_path / "h.wav", silence(1280))), soul())

    async def scenario():
        async with session:
            return session.warm_start_hint()

    assert run(scenario()) is None


# ------------------------------------------------------------ transcription


def spoken(*words: bytes) -> bytes:
    """The phrase, then frames that spell words, then silence. The mock
    transcriber reads the words; the energy detector hears the same bytes as
    loud, so the endpointer captures them and closes on the silence after."""
    return frame() + frame(PHRASE.encode()) + b"".join(frame(w) for w in words) + frame() * 20


def transcribing(tmp_path, name: str, pcm: bytes, *, soul_doc=None, manifest=None, **kw):
    manifest = manifest or body(write_wav(tmp_path / name, pcm))
    return ListenSession(manifest, soul_doc or soul(provider="mock"), transcribe=True, **kw)


def test_a_turn_is_transcribed_when_asked(tmp_path):
    """0.4's first step end to end: hear the name, feed what follows to a
    provider the engine never imported, get the words back on the turn."""
    session = transcribing(tmp_path, "t.wav", spoken(b"what", b"time", b"is it"))

    async def scenario():
        async with session:
            return [(e, u) async for e, u in session.turns()]

    turns = run(scenario())
    assert len(turns) == 1
    _, utterance = turns[0]
    assert utterance.had_speech
    assert utterance.transcript is not None
    assert utterance.transcript.final
    assert utterance.transcript.text == "what time is it"
    assert session.stt_name == "mock"
    assert session.stt_descriptor is not None and session.stt_descriptor.streaming


def test_partials_arrive_while_the_person_is_still_talking(tmp_path):
    """Streaming through the seam. Each partial carries everything so far,
    and the last one is what the final confirms."""
    partials = []
    session = transcribing(
        tmp_path, "p.wav", spoken(b"what", b"time", b"is it"), on_partial=partials.append
    )

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert [p.text for p in partials] == ["what", "what time", "what time is it"]
    assert all(not p.final for p in partials)
    assert utterance.transcript is not None
    assert partials[-1].text == utterance.transcript.text


def test_the_wake_callback_fires_before_the_turn_ends(tmp_path):
    seen: list[str] = []
    session = transcribing(
        tmp_path,
        "w.wav",
        spoken(b"what"),
        on_wake=lambda e: seen.append("wake"),
        on_partial=lambda t: seen.append("partial"),
    )

    async def scenario():
        async with session:
            async for _ in session.turns():
                seen.append("turn")

    run(scenario())
    assert seen == ["wake", "partial", "turn"]


def test_without_asking_nothing_is_built_and_the_transcript_is_none(tmp_path):
    """The 0.3 loop, unchanged, even for a soul that names a provider. The
    selection is still reported so a caller can say what would run."""
    session = ListenSession(body(write_wav(tmp_path / "n.wav", spoken(b"what"))), soul(provider="mock"))

    async def scenario():
        async with session:
            assert session._stt is None
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert utterance.transcript is None
    assert session.stt_name == "mock"


def test_asking_with_no_provider_anywhere_names_the_fields_to_set(tmp_path):
    session = transcribing(tmp_path, "x.wav", frame(), soul_doc=soul())
    with pytest.raises(EngineError) as exc:
        run(session.start())
    message = str(exc.value)
    assert "models.stt.provider" in message and "in the body" in message
    assert "mock" in message  # what is installed
    run(session.stop())


def test_an_uninstalled_provider_is_a_missing_plugin(tmp_path):
    session = transcribing(tmp_path, "d.wav", frame(), soul_doc=soul(provider="whisper"))
    with pytest.raises(MissingPluginError):
        run(session.start())
    run(session.stop())


def test_a_provider_that_cannot_start_is_refused_with_its_own_reason(tmp_path):
    """A dead network or a missing key. The plugin's health detail is more
    specific than anything the engine could invent, so it is quoted."""
    manifest = body(write_wav(tmp_path / "f.wav", frame()))
    manifest["models"] = {"stt": {"provider": "mock", "params": {"fail_on_start": True}}}
    session = transcribing(tmp_path, "f.wav", frame(), manifest=manifest)
    with pytest.raises(EngineError) as exc:
        run(session.start())
    assert "unreachable" in str(exc.value)
    assert "will not run" in str(exc.value)
    run(session.stop())


def test_a_missing_key_is_named_at_boot(tmp_path, monkeypatch):
    monkeypatch.delenv("EMET_MOCK_KEY", raising=False)
    session = transcribing(
        tmp_path,
        "k.wav",
        frame(),
        soul_doc=soul(provider="mock", key_env="EMET_MOCK_KEY"),
    )
    session.stt["params"]["require_key"] = True
    with pytest.raises(EngineError, match="EMET_MOCK_KEY"):
        run(session.start())
    run(session.stop())


def test_a_provider_at_the_wrong_rate_is_refused_before_the_microphone_opens(tmp_path):
    """One microphone feeds the wake engine and the transcriber. A recogniser
    hearing 16 kHz speech at 8 kHz does not fail; it produces nonsense."""
    manifest = body(write_wav(tmp_path / "r.wav", frame()))
    manifest["models"] = {"stt": {"provider": "mock", "params": {"sample_rate": 8000}}}
    session = transcribing(tmp_path, "r.wav", frame(), manifest=manifest)
    with pytest.raises(EngineError) as exc:
        run(session.start())
    message = str(exc.value)
    assert "8000" in message and "16000" in message
    assert session._audio is None, "the microphone was opened before the refusal"
    run(session.stop())


def test_the_body_takes_over_from_the_soul(tmp_path):
    """The reference soul names deepgram; a mocked rig names mock and runs."""
    manifest = body(write_wav(tmp_path / "o.wav", spoken(b"hello")))
    manifest["models"] = {"stt": {"provider": "mock"}}
    session = transcribing(
        tmp_path,
        "o.wav",
        frame(),
        manifest=manifest,
        soul_doc=soul(provider="deepgram", model="nova-2", key_env="EMET_DEEPGRAM_KEY"),
    )
    assert session.stt_name == "mock"
    assert session.stt is not None and session.stt["key_env"] is None

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert utterance.transcript is not None and utterance.transcript.text == "hello"


def test_a_scripted_mock_says_its_line_over_real_looking_audio(tmp_path):
    """How the seam is exercised on a body: audio the mock cannot read, and a
    line it was told to say."""
    manifest = body(write_wav(tmp_path / "s.wav", frame() + frame(PHRASE.encode()) + loud_frame() * 5 + frame() * 20))
    manifest["models"] = {"stt": {"provider": "mock", "params": {"transcript": "testing the seam"}}}
    partials = []
    session = transcribing(tmp_path, "s.wav", frame(), manifest=manifest, on_partial=partials.append)

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert utterance.transcript is not None
    assert utterance.transcript.text == "testing the seam"
    assert [p.text for p in partials] == ["testing", "testing the", "testing the seam"]


def test_a_false_wake_yields_an_empty_final_not_none(tmp_path):
    """Nobody spoke, the provider listened, and it says so. None would mean
    nobody asked."""
    session = transcribing(tmp_path, "e.wav", frame() + frame(PHRASE.encode()) + frame() * 60)

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert not utterance.had_speech
    assert utterance.transcript is not None
    assert utterance.transcript.final and utterance.transcript.text == ""


def test_a_source_that_ends_mid_turn_still_hands_over_the_transcript(tmp_path):
    pcm = frame() + frame(PHRASE.encode()) + frame(b"cut") + frame(b"off")
    session = transcribing(tmp_path, "c.wav", pcm)

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert utterance.reason.value == "source_ended"
    assert utterance.transcript is not None and utterance.transcript.text == "cut off"


def test_each_turn_gets_its_own_transcript(tmp_path):
    # Two words a turn: the energy detector needs two loud frames in a row
    # before it calls it speech, and a turn has to end by silence for the
    # next wake to be heard.
    session = transcribing(tmp_path, "two.wav", spoken(b"first", b"one") + spoken(b"second", b"two"))

    async def scenario():
        async with session:
            return [u.transcript.text async for _, u in session.turns()]

    assert run(scenario()) == ["first one", "second two"]


def test_a_turn_the_recording_closes_is_counted(tmp_path):
    """`--stats` printed "wakes 2 turns 1" beside "heard it 2 time(s)" on a
    15 s slice that ended mid-turn, on 0.4.1 and 0.5.0 alike."""
    pcm = spoken(b"first", b"one") + frame() + frame(PHRASE.encode()) + frame(b"cut")
    session = transcribing(tmp_path, "n.wav", pcm)

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()], session.stats

    utterances, stats = run(scenario())
    assert [u.reason.value for u in utterances] == ["silence", "source_ended"]
    assert stats.wakes == stats.turns == 2


# ------------------------------------------------- words said with the name


def test_what_the_wake_engine_heard_after_the_phrase_reaches_the_transcriber(tmp_path):
    """A detector fires a moment after the phrase ends. "hey emet, what time
    is it" in one breath lost "what" on the reference body 11 times in 12:
    the transcriber started at the frame after the wake. The engine now
    hands it the frames the wake engine reports it had already heard."""
    # The word fills its frame, as speech does. Four bytes and then digital
    # silence would sound like the end of the name falling quiet, which the
    # cut leaves out (`past_the_name`).
    said = b"what".ljust(FRAME_BYTES, b" ")
    pcm = frame() + frame(PHRASE.encode()) + said + frame(b"time") + frame(b"is it") + frame() * 20
    manifest = body(write_wav(tmp_path / "w.wav", pcm), lag_frames=1)
    session = transcribing(tmp_path, "w.wav", pcm, manifest=manifest)

    async def scenario():
        async with session:
            return [(e, u) async for e, u in session.turns()]

    ((event, utterance),) = run(scenario())
    assert event.lag_ms == pytest.approx(80.0)
    assert utterance.transcript is not None and utterance.transcript.text == "what time is it"


def test_an_engine_that_reports_no_lag_hands_over_nothing_before_the_wake(tmp_path):
    """The phrase is never handed over: with no lag the transcriber starts
    at the frame after the wake, as in 0.5.0, and the footer has no
    handover to count."""
    pcm = frame() + frame(PHRASE.encode()) + frame(b"time") + frame(b"is it") + frame() * 20
    fired = []
    session = transcribing(tmp_path, "z.wav", pcm, on_handover=fired.append)

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()], session.stats.name_cut_ms

    (utterance,), counted = run(scenario())
    assert utterance.transcript is not None and utterance.transcript.text == "time is it"
    assert counted == [], "nothing handed over, so nothing for the footer to count"
    assert fired == [], "and no line under the wake"


def test_the_audio_before_the_wake_is_cut_where_the_phrase_ended():
    """Half a frame of lag is half a frame of audio, at the end of the last
    frame heard, with silence written where the phrase was so the name is
    never transcribed."""
    from emet_engine.session import said_with_the_name
    from emet_sdk.types import AudioFormat

    fmt = AudioFormat()
    phrase_then_words = b"P" * (FRAME_BYTES // 2) + b"W" * (FRAME_BYTES // 2)
    (only,) = said_with_the_name(40.0, [frame(), phrase_then_words], fmt)
    assert only == bytes(FRAME_BYTES // 2) + b"W" * (FRAME_BYTES // 2)

    two = said_with_the_name(120.0, [b"A" * FRAME_BYTES, b"B" * FRAME_BYTES], fmt)
    assert two == [bytes(FRAME_BYTES // 2) + b"A" * (FRAME_BYTES // 2), b"B" * FRAME_BYTES]

    assert said_with_the_name(0.0, [b"B" * FRAME_BYTES], fmt) == []
    assert said_with_the_name(80.0, [], fmt) == []
    assert said_with_the_name(5000.0, [b"B" * FRAME_BYTES], fmt) == [b"B" * FRAME_BYTES], "no more than was heard"


def square(amplitude: int, ms: float) -> bytes:
    """A square wave whose RMS is its amplitude, at 16 kHz."""
    from array import array

    samples = int(16000 * ms / 1000)
    return array("h", [amplitude if i % 2 else -amplitude for i in range(samples)]).tobytes()


def handed_over(phrase: bytes, after: bytes) -> bytes:
    """What said_with_the_name hands over when the wake engine heard
    `phrase` and then `after`, and fired at the end of `after`."""
    from emet_engine.session import said_with_the_name
    from emet_sdk.types import AudioFormat

    heard = phrase + after
    assert len(heard) % FRAME_BYTES == 0, "whole frames, as the loop keeps them"
    frames = [heard[i:i + FRAME_BYTES] for i in range(0, len(heard), FRAME_BYTES)]
    lag_ms = len(after) / 2 / 16000 * 1000
    return b"".join(said_with_the_name(lag_ms, frames, AudioFormat()))


def test_the_end_of_the_name_is_left_out_when_it_falls_quiet_first():
    """The wake engine can say the phrase ended while the vowel of "-met"
    still sounds. When that sound falls quiet before the question starts,
    the cut moves to where it fell: Deepgram dropped "What's" behind 80 ms
    of the vowel on all three replays at that cut, of a take recorded on
    the reference body (laptop replays, 2026-10-01 and 02)."""
    vowel, pause, question = square(8000, 80), square(50, 100), square(6000, 20)
    out = handed_over(square(8000, 40), vowel + pause + question)
    assert out == bytes(2 * 640 + len(vowel)) + pause + question


def test_a_question_that_rises_out_of_the_name_is_handed_over_whole():
    """No fall, no move: the question rises out of the end of "emet" with
    nothing 20 dB down in the handover, so the cut stays where the wake
    engine put it and the question's first sound is handed over."""
    after = square(3000, 30) + square(8000, 170)
    assert handed_over(square(8000, 40), after) == bytes(2 * 640) + after


def test_a_quiet_moment_after_a_rise_is_never_taken_for_the_end_of_the_name():
    """The closure of a stop inside the question is as quiet as a pause.
    Once something new has started, nothing after it is cut."""
    after = square(300, 30) + square(8000, 50) + square(5, 40) + square(8000, 80)
    assert handed_over(square(8000, 40), after) == bytes(2 * 640) + after


def test_a_fall_short_of_the_threshold_moves_nothing():
    """A vowel that fades by less than the threshold is left as it is."""
    from emet_engine.session import NAME_TAIL_DROP_DB

    assert NAME_TAIL_DROP_DB == 20.0
    after = square(8000, 80) + square(1000, 100) + square(6000, 20)  # 18 dB down
    assert handed_over(square(8000, 40), after) == bytes(2 * 640) + after


def test_a_vowel_that_dies_away_is_cut_where_it_has_fallen_far_enough():
    """A vowel dies away over several windows. On take 04 of the
    two-plus-two recording "-met" fell from -18.5 to -38.7 dBFS over
    130 ms, at most 8.2 dB in one window, and rose 3.1 dB on the way
    (laptop, 2026-10-02). Measured window to window, or from the quietest
    window so far, that fall never reaches 20 dB, and the cut stays where
    Deepgram dropped "What's". Here no step is over 5.8 dB, one window
    rises 3.1 dB, and the eleventh is the first 20 dB down."""
    steps = [8000, 7000, 5600, 4500, 3500, 2600, 1900, 2720, 1400, 1000, 790]
    after = b"".join(square(a, 10) for a in steps) + square(600, 70) + square(6000, 20)
    cut = 10 * 320  # ten 10 ms windows of the vowel, in bytes
    assert handed_over(square(8000, 40), after) == bytes(2 * 640 + cut) + after[cut:]


def test_a_dip_in_the_fading_name_does_not_stop_the_search():
    """The sound of "-met" can dip for one window as it fades and come
    back: before the cut that lost "What's" it came back 3.1 dB above its
    quietest window, and before the other cuts that move on five
    recordings it rose up to 5.3 dB above the quietest window before it
    (laptop, 2026-10-01 to 03). A stop that low would leave the end of
    the name in front of the question."""
    fading = square(8000, 20) + square(4300, 10) + square(8000, 20)  # back up 5.4 dB
    pause, question = square(50, 100), square(6000, 50)
    out = handed_over(square(8000, 40), fading + pause + question)
    assert out == bytes(2 * 640 + len(fading)) + pause + question


def test_a_question_that_rises_out_of_the_fade_stops_the_search():
    """A question can start while the name is still fading, a few dB a
    window and still below where the handover started. The rise is
    measured from the quietest window so far, so the search stops there.
    Measured from the start, or from the window before, it goes unseen,
    and the closure of a stop inside the question is taken for the end of
    the name. The room's hiss in every window holds the first difference
    flat, so only the level stop sees the rise."""
    after = (mix(tone(8000, 200, 20), hiss(20)) + mix(tone(1600, 200, 30), hiss(30))
             + mix(tone(2400, 200, 10), hiss(10)) + mix(tone(3600, 200, 10), hiss(10))
             + mix(tone(5000, 200, 50), hiss(50)) + hiss(40) + mix(tone(6000, 200, 40), hiss(40)))
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def test_a_rise_of_7_db_out_of_the_fade_ends_the_search():
    """A question that starts softly rises only a few dB out of the fading
    name. 7 dB above the quietest window ends the search; with the line
    any higher, the closure after it would be taken for the end of the
    name and the question's first syllable written as silence. The room's
    hiss in every window holds the first difference flat, so only the
    level stop sees the rise."""
    after = (mix(tone(8000, 200, 20), hiss(20)) + mix(tone(1600, 200, 30), hiss(30))
             + mix(tone(3640, 200, 100), hiss(100)) + hiss(30) + mix(tone(6000, 200, 20), hiss(20)))
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


@pytest.mark.parametrize("louder", ["first", "second"])
def test_the_fall_is_measured_from_the_louder_of_the_first_two_windows(louder):
    """Either of the first two windows can be the louder. Measured from the
    quieter, this 21 dB fall reads as 17 dB and leaves 80 ms of the name in
    front of the question."""
    vowel = {
        "first": square(8000, 10) + square(5000, 70),
        "second": square(5000, 10) + square(8000, 70),
    }[louder]
    pause, question = square(700, 100), square(6000, 20)
    out = handed_over(square(8000, 40), vowel + pause + question)
    assert out == bytes(2 * 640 + len(vowel)) + pause + question


def test_the_edges_of_the_measured_audio_are_never_cut():
    """pocketsphinx reports its lag in whole 10 ms steps; another wake
    engine can report a shorter lag, or one that ends inside a window. A
    tail under two windows is handed over as it is, since there is no
    second window to measure. A part window at the end is never measured:
    measured, the 5 ms of silence in it would write the steady vowel
    before it as silence."""
    from emet_engine.session import said_with_the_name
    from emet_sdk.types import AudioFormat

    vowel = square(8000, 80)
    (only,) = said_with_the_name(10.0, [vowel], AudioFormat())
    assert only == bytes(FRAME_BYTES - 320) + vowel[-320:]

    after = square(8000, 200) + bytes(160)
    assert handed_over(square(8000, 35), after) == bytes(2 * 560) + after


def test_a_tail_cut_short_by_the_longest_lag_is_never_moved():
    """A lag reported past MAX_WAKE_LAG_MS hands over only the last 640 ms,
    which start after the phrase was said to end: here 160 ms after, inside
    "what's". A pause between two words there is the question's own, and
    taken for the end of the name it would cost the 240 ms of "what's" the
    tail still holds."""
    after = square(8000, 80) + square(50, 70) + square(6000, 250) + square(190, 80) + square(6000, 320)
    tail = after[-2 * 10240:]  # 640 ms, eight whole frames: nothing to pad
    assert handed_over(square(8000, 160), after) == tail


def test_a_tail_cut_short_by_what_was_kept_is_never_moved():
    """The loop keeps what the wake engine heard since the start or the last
    wake, and a lag reported past that puts the start of what it kept after
    the phrase was said to end, inside whatever followed. Measured there,
    the pause would pass for the end of the name and the word before it
    would be written as silence. pocketsphinx and the mock report only
    audio they heard since their last reset, which the loop makes at the
    wake where it clears what it kept, so neither reports such a lag; the
    rule is for a wake engine that does."""
    from emet_engine.session import said_with_the_name
    from emet_sdk.types import AudioFormat

    kept = square(8000, 80) + square(50, 80) + square(6000, 80)
    frames = [kept[i:i + FRAME_BYTES] for i in range(0, len(kept), FRAME_BYTES)]
    assert b"".join(said_with_the_name(400.0, frames, AudioFormat())) == kept


def tone(amplitude: int, hz: float, ms: float) -> bytes:
    """A sine whose RMS is the amplitude, at 16 kHz: 200 Hz for a vowel's
    voicing, 6 kHz for the hiss of an "s". Every use fills its 10 ms
    windows with whole periods."""
    import math
    from array import array

    samples = int(16000 * ms / 1000)
    peak = amplitude * math.sqrt(2.0)
    return array("h", [round(peak * math.sin(2 * math.pi * hz * i / 16000)) for i in range(samples)]).tobytes()


def mix(*sounds: bytes) -> bytes:
    """Sounds of one length, heard together."""
    from array import array

    parts = []
    for sound in sounds:
        samples = array("h")
        samples.frombytes(sound)
        parts.append(samples)
    assert len({len(p) for p in parts}) == 1
    return array("h", [sum(v) for v in zip(*parts)]).tobytes()


def hiss(ms: float) -> bytes:
    """A room's steady hiss, the same in every window."""
    return square(300, ms)


def test_an_s_said_straight_on_from_the_name_is_handed_over_whole():
    """In "hey emet, stop" said in one breath, the "s" runs on from the
    fading vowel with no pause and no rise in level, and the closure of the
    "t" behind it falls far enough to pass for the end of the name. The
    first difference rises 26 dB at the "s" here. On the reference body,
    with the phrase's end placed 15 to 79 ms earlier, the level rule alone
    wrote some or all of the hiss of an "s" as silence at 37 placements,
    and at each the first difference had risen 13 to 21 dB (laptop
    replays, 2026-10-02 and 03)."""
    vowel, s = tone(8000, 200, 20) + tone(3000, 200, 30), tone(2500, 6000, 60)
    after = vowel + s + square(50, 40) + tone(6000, 200, 50)
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def test_a_rise_of_6_1_db_in_the_first_difference_ends_the_search():
    """A fricative can start faint under the fading vowel and add only
    hiss: 6.1 dB up in the first difference with the level flat ends the
    search. The narrowest such rise on the reference body was 6.13 dB, in
    front of the "s" of one "stop" with the phrase end placed 44 ms
    earlier, where the level rule's cut fell at the end of a 1 ms burst
    before its hiss; the "h" of a "how" in the soak recording rose 6.2 to
    7.7 dB where the cut fell past its start by eye (laptop replays over
    every phase of a 1 ms shift, 2026-10-02 and 03). With the line at
    6.2 dB, that cut would go through, and so would three cuts 15 ms
    into that "h"."""
    faint = mix(tone(2000, 200, 10), tone(149, 6000, 10))
    after = (tone(8000, 200, 20) + tone(2000, 200, 30) + faint + tone(2000, 200, 20)
             + square(50, 40) + tone(6000, 200, 80))
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def test_a_fading_name_that_brightens_5_db_is_still_cut():
    """The end of "emet" can brighten a little as it fades: before every
    cut the level rule made short of the question's first sound, on five
    recordings with the phrase end placed up to 100 ms later or 70 ms
    earlier, the first difference came back at most 4.5 dB above the
    lowest before it at the window phase replayed (laptop, 2026-10-02).
    At 5 dB the cut still moves to the pause."""
    faint = mix(tone(2000, 200, 10), tone(125, 6000, 10))
    vowel = tone(8000, 200, 20) + tone(2000, 200, 30) + faint + tone(2000, 200, 20)
    pause, question = square(50, 40), tone(6000, 200, 80)
    out = handed_over(tone(8000, 200, 40), vowel + pause + question)
    assert out == bytes(2 * 640 + len(vowel)) + pause + question


def test_a_quiet_s_at_the_line_is_never_the_cut():
    """An "s" can start late in one window and be quiet enough to reach the
    line in the next. Read after the fall, that window would be the cut and
    the start of the "s" in the window before it written as silence: on the
    reference body one "s" dipped to the line 14 ms after it began, and a
    first-difference test read after the fall let the cut through there
    once that "s" was 1 to 5 dB quieter (laptop, 2026-10-02). Read before
    the fall, the rise stops the search."""
    onset = mix(tone(1000, 200, 10), bytes(2 * 128) + tone(700, 6000, 2), hiss(10))
    after = (mix(tone(8000, 200, 20), hiss(20)) + mix(tone(1000, 200, 30), hiss(30)) + onset
             + mix(tone(700, 6000, 50), hiss(50)) + hiss(40) + mix(tone(6000, 200, 50), hiss(50)))
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def test_the_rooms_own_hiss_is_never_a_rise():
    """A room's steady hiss is in every window, the name's included, so
    measured from the lowest first difference before it, it never reads as
    a new sound: here the hiss is louder in the first difference than the
    fading vowel, and the cut still moves to the pause."""
    vowel = mix(tone(8000, 200, 30), hiss(30)) + mix(tone(2500, 200, 40), hiss(40))
    pause, question = hiss(100), mix(tone(6000, 200, 30), hiss(30))
    out = handed_over(tone(8000, 200, 40), vowel + pause + question)
    assert out == bytes(2 * 640 + len(vowel)) + pause + question


def test_a_rise_in_the_first_difference_is_measured_from_the_lowest_before_it():
    """The window quietest by level need not be the one with the lowest
    first difference: a faint hiss can lift the second while the level
    falls. Here a sound that brightens 8 dB above the lowest first
    difference so far, and only 4 dB above that of the quietest window,
    stops the search; measured from the quietest window's, it would not,
    and the closure after it would write it as silence."""
    low = tone(1300, 200, 10)
    quietest = mix(tone(1033, 200, 10), tone(77, 6000, 10))
    brighter = mix(tone(1636, 200, 10), tone(123, 6000, 10))
    after = tone(3277, 200, 20) + low + quietest + brighter + square(50, 40) + tone(6000, 200, 30)
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def test_a_voiced_sound_rising_out_of_the_fade_in_a_hissing_room_ends_the_search():
    """The level stop still matters where the first difference cannot see
    the rise: a voiced sound rising 7 dB over two windows out of the fading
    name, with the room's hiss in every window holding the first difference
    flat. Missed, the hissing pause after it would pass for the end of the
    name."""
    after = (mix(tone(8000, 200, 20), hiss(20)) + mix(tone(1500, 200, 10), hiss(10))
             + mix(tone(850, 200, 10), hiss(10)) + mix(tone(1315, 200, 10), hiss(10))
             + mix(tone(1997, 200, 10), hiss(10)) + hiss(40) + mix(tone(6000, 200, 20), hiss(20)))
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def bright_vowel(amplitude: int, ms: float) -> bytes:
    """A vowel's voicing at 200 Hz with a formant's worth of 2 kHz, 6.3 times
    weaker: 5.2 dB brighter in the first difference than a 200 Hz tone of
    the same level, so 7.3 dB above an "m" 2 dB quieter, as below."""
    return mix(tone(amplitude, 200, ms), tone(round(amplitude * 0.16), 2000, ms))


def test_a_handover_that_starts_in_the_m_is_cut_where_the_name_falls():
    """The "m" of "emet" is dark: its first difference is low. Where it opens
    into the vowel the first difference rises 7.3 dB and the level 2 dB, as
    the vowel's onset did at 337 placements of the laptop sweep (6.0 to
    11.6 dB, the level at most 5.97 dB below the start). Read there, the
    rise would hand the whole "-met" over, which put a word in front of the
    question on 4 of 10 takes Deepgram replayed (laptop, 2026-10-05); read only
    once the sound has fallen, the cut moves to where it falls 20 dB."""
    m = tone(4000, 200, 30)
    held = bright_vowel(5000, 60)
    fade = bright_vowel(2500, 10) + bright_vowel(1250, 10) + bright_vowel(625, 10) + bright_vowel(312, 10)
    pause, question = square(50, 30), tone(6000, 200, 40)
    after = m + held + fade + pause + question
    cut = 2 * 16 * (30 + 60 + 30)  # the m, the vowel and three windows of its fade
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640 + cut) + after[cut:]


def test_a_brightening_before_the_name_has_fallen_6_db_is_not_read():
    """On the laptop sweep the level had fallen at most 5.97 dB below the
    start where the "m" opened into the vowel (one soak take, with the
    phrase end placed 140 ms early). A rise of 8 dB in the first
    difference with the level 5.97 dB down is still the name; the cut moves
    to where the sound falls 20 dB. The test below holds the line from
    above."""
    start = tone(4000, 200, 20)
    down = tone(2011, 200, 10)  # 5.97 dB below the start
    bright = mix(tone(1990, 200, 10), tone(481, 2000, 10))  # level flat, first difference up 8 dB
    fall = tone(500, 200, 20) + tone(300, 200, 10)
    pause, question = square(50, 30), tone(6000, 200, 100)
    after = start + down + bright + fall + pause + question
    cut = 2 * 16 * (20 + 10 + 10 + 20)
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640 + cut) + after[cut:]


def test_a_brightening_once_the_name_has_fallen_6_db_ends_the_search():
    """The same 8 dB brightening as the test above, with the quietest window
    before it 6.1 dB below the start, is read: the search ends and the
    handover goes over whole. The two tests hold `NAME_TAIL_HIGH_FALL_DB` at
    6 dB from both sides. Without this one, the line could rise to just
    under 10 dB and every other test would pass."""
    start = tone(4000, 200, 20)
    down = tone(1980, 200, 10)  # 6.1 dB below the start
    bright = mix(tone(1944, 200, 10), tone(470, 2000, 10))  # level flat, first difference up 8 dB
    fall = tone(500, 200, 20) + tone(300, 200, 10)
    pause, question = square(50, 30), tone(6000, 200, 100)
    after = start + down + bright + fall + pause + question
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def test_the_fall_before_a_brightening_is_measured_at_the_quietest_window():
    """An "s" can come back louder than the faded vowel before it, within
    the 6 dB of the level stop. Here the vowel dips 10 dB, comes back to
    5 dB below the start, and an "s" follows at that level. The quietest
    window before the "s" has fallen 10 dB, so the rise of its first
    difference ends the search and the "s" goes over whole. Measured on the
    "s" window itself, or on the window just before it, the fall is 5 dB,
    the first difference is not read, and the pause after the "s" writes it
    as silence."""
    name = tone(8000, 200, 20) + tone(2530, 200, 10) + tone(4500, 200, 20)
    s = mix(tone(900, 200, 60), tone(4400, 6000, 60))  # 5 dB below the start, 5 dB above the dip
    after = name + s + square(50, 40) + tone(6000, 200, 50)
    assert handed_over(tone(8000, 200, 40), after) == bytes(2 * 640) + after


def name_cut_after(tmp_path, name: str, handed: bytes) -> list[float]:
    """The footer's record of the cut, for one wake whose engine fires a
    frame after the phrase and hands `handed` over."""
    pcm = frame() + frame(PHRASE.encode()) + handed + frame(b"time") + frame(b"is it") + frame() * 20
    manifest = body(write_wav(tmp_path / name, pcm), lag_frames=1)
    session = transcribing(tmp_path, name, pcm, manifest=manifest)

    async def scenario():
        async with session:
            [u async for _, u in session.turns()]
            return session.stats.name_cut_ms

    return run(scenario())


def test_the_stats_count_how_much_of_the_name_was_left_out(tmp_path):
    """The footer counts how much of the name was left out: 30 ms of the
    name's tail left out of one handover, none out of a handover whose
    words fill it."""
    assert name_cut_after(tmp_path, "cut.wav", square(8000, 30) + square(50, 50)) == [30.0]
    assert name_cut_after(tmp_path, "whole.wav", b"what".ljust(FRAME_BYTES, b" ")) == [0.0]


def test_the_footer_counts_only_the_end_of_the_name_written_as_silence(tmp_path):
    """pocketsphinx reports its lag in 10 ms steps and the loop hands over
    whole 80 ms frames, so wherever the lag is not a whole number of
    frames the first frame handed over starts with the end of the phrase
    written as silence: on 28 of the 29 wakes of four recordings (laptop
    replays, 2026-10-03). Counted with the cut, that silence would have
    the footer read 28 of the 29 as moved, where the cut moved 7. Here
    the lag is 130 ms: 30 ms of the phrase, then the 50 ms of the name
    the cut leaves out."""
    from dataclasses import replace

    vowel, pause, question = square(8000, 50), square(50, 40), square(6000, 40)
    pcm = frame() + frame(PHRASE.encode()) + square(8000, 30) + vowel + pause + question + frame() * 20
    manifest = body(write_wav(tmp_path / "p.wav", pcm), lag_frames=2)
    session = transcribing(tmp_path, "p.wav", pcm, manifest=manifest)

    async def scenario():
        async with session:
            whole_frames = session._wake.process

            async def in_10_ms_steps(chunk):  # the mock's lag is whole frames, which leave no pad
                event = await whole_frames(chunk)
                return None if event is None else replace(event, lag_ms=130.0)

            session._wake.process = in_10_ms_steps
            [u async for _, u in session.turns()]
            return session.stats.name_cut_ms

    assert run(scenario()) == [50.0]


def test_the_count_is_the_end_of_the_name_alone():
    """The footer counts what the cut took for the end of the name. The
    phrase written as silence to fill the first frame is the wake engine's
    own cut: counted, every wake whose lag is not whole frames would read
    as a cut that moved, whether the cut moved or not."""
    from emet_engine.session import cut_before_the_question
    from emet_sdk.types import AudioFormat

    vowel, pause, question = square(8000, 80), square(50, 100), square(6000, 20)
    for heard, name in [(square(8000, 40) + vowel + pause + question, len(vowel) // 2), (square(8000, 240), 0)]:
        frames = [heard[i:i + FRAME_BYTES] for i in range(0, len(heard), FRAME_BYTES)]
        assert cut_before_the_question(200.0, frames, AudioFormat())[1] == name


# ------------------------------------------------------ the line under a wake


def record_for(phrase: bytes, after: bytes):
    """What `--stats` prints under a wake whose engine heard `phrase` and
    then `after`, and fired at the end of `after`."""
    from emet_engine.session import cut_before_the_question, handover_record
    from emet_sdk.types import AudioFormat

    heard = phrase + after
    assert len(heard) % FRAME_BYTES == 0, "whole frames, as the loop keeps them"
    frames = [heard[i:i + FRAME_BYTES] for i in range(0, len(heard), FRAME_BYTES)]
    lag_ms = len(after) / 2 / 16000 * 1000
    _, name = cut_before_the_question(lag_ms, frames, AudioFormat())
    return handover_record(lag_ms, frames, AudioFormat(), name)


def test_each_handover_is_recorded_with_its_lag_and_both_cuts(tmp_path):
    """The session hands `on_handover` one record per handover, the one
    `--stats` prints: 30 ms of the name's tail written as silence, where
    the level stop alone cuts the same, and a handover whose words fill
    it, cut by neither."""
    from emet_engine.metrics import Handover

    for name, handed, want in [
        ("cut.wav", square(8000, 30) + square(50, 50), Handover(80.0, 80.0, 30.0, 30.0)),
        ("whole.wav", b"what".ljust(FRAME_BYTES, b" "), Handover(80.0, 80.0, 0.0, 0.0)),
    ]:
        pcm = frame() + frame(PHRASE.encode()) + handed + frame(b"time") + frame(b"is it") + frame() * 20
        fired = []
        session = transcribing(
            tmp_path, name, pcm, manifest=body(write_wav(tmp_path / name, pcm), lag_frames=1),
            on_handover=fired.append,
        )

        async def scenario():
            async with session:
                [u async for _, u in session.turns()]

        run(scenario())
        assert fired == [want]


def test_the_level_stop_alone_reads_where_the_first_difference_kept_the_cut():
    """An "s" said straight on from the name keeps the wake engine's cut, and
    the line says the level stop alone would have cut 110 ms, into the "s":
    the case the first-difference stop exists for, now visible per wake."""
    vowel, s = tone(8000, 200, 20) + tone(3000, 200, 30), tone(2500, 6000, 60)
    record = record_for(tone(8000, 200, 40), vowel + s + square(50, 40) + tone(6000, 200, 50))
    assert (record.name_ms, record.level_ms, record.heard_ms) == (0.0, 110.0, 200.0)


def test_the_record_under_a_wake_says_where_the_level_stop_alone_would_cut(tmp_path):
    """The record the session hands `on_handover`, the one `--stats`
    prints, where the two cuts differ: an "s" said straight on from a name
    whose sound has fallen 8.5 dB keeps the wake engine's cut, where the
    level stop alone would have written the first 50 ms as silence, the
    "s" with them."""
    from emet_engine.metrics import Handover

    handed = tone(8000, 200, 20) + tone(3000, 200, 10) + tone(2500, 6000, 20) + square(50, 30)
    assert len(handed) == FRAME_BYTES, "one frame, the lag the mock engine reports"
    pcm = frame() + frame(PHRASE.encode()) + handed + frame(b"time") + frame(b"is it") + frame() * 20
    fired = []
    session = transcribing(
        tmp_path, "s.wav", pcm, manifest=body(write_wav(tmp_path / "s.wav", pcm), lag_frames=1),
        on_handover=fired.append,
    )

    async def scenario():
        async with session:
            [u async for _, u in session.turns()]

    run(scenario())
    assert fired == [Handover(80.0, 80.0, 0.0, 50.0)]


def test_a_cut_that_moved_is_the_level_stops_cut_too():
    """Where the cut moves, the level stop alone cuts at the same window: the
    two can only part as a cut kept where the level stop would move it. Here
    the name dies away over many windows, as in the test of a vowel that dies
    away, so a level stop alone with its line 2 dB lower, or 0.2 dB higher,
    would cut at another window."""
    steps = [8000, 7000, 5600, 4500, 3500, 2600, 1900, 2720, 1400, 1000, 790]
    after = b"".join(square(a, 10) for a in steps) + square(600, 70) + square(6000, 20)
    record = record_for(square(8000, 40), after)
    assert (record.name_ms, record.level_ms) == (100.0, 100.0)


def test_a_tail_cut_short_has_no_level_stop_alone():
    """A tail cut short is never moved by either rule, and the line says how
    much of it went over: the last 640 ms of an 800 ms lag."""
    after = square(8000, 80) + square(50, 70) + square(6000, 250) + square(190, 80) + square(6000, 320)
    record = record_for(square(8000, 160), after)
    assert record.cut_short and (record.name_ms, record.level_ms, record.heard_ms) == (0.0, 0.0, 640.0)
    assert record.lag_ms == 800.0, "the lag as the wake engine reported it"


def test_the_line_under_a_wake_comes_before_its_first_partial(tmp_path):
    """The record is made after the cut and before any of the handover is
    fed, so on a terminal the line never lands inside a redrawn caption."""
    seen: list[str] = []
    said = b"what".ljust(FRAME_BYTES, b" ")
    pcm = frame() + frame(PHRASE.encode()) + said + frame(b"time") + frame(b"is it") + frame() * 20
    session = transcribing(
        tmp_path, "o.wav", pcm, manifest=body(write_wav(tmp_path / "o.wav", pcm), lag_frames=1),
        on_wake=lambda event: seen.append("wake"),
        on_handover=lambda handover: seen.append("handover"),
        on_partial=lambda partial: seen.append("partial"),
    )

    async def scenario():
        async with session:
            [u async for _, u in session.turns()]

    run(scenario())
    assert seen[:3] == ["wake", "handover", "partial"]


def test_stopping_after_a_failed_transcriber_start_is_safe(tmp_path):
    manifest = body(write_wav(tmp_path / "z.wav", frame()))
    manifest["models"] = {"stt": {"provider": "mock", "params": {"fail_on_start": True}}}
    session = transcribing(tmp_path, "z.wav", frame(), manifest=manifest)

    async def scenario():
        with pytest.raises(EngineError):
            await session.start()
        await session.stop()
        await session.stop()

    run(scenario())


# ----------------------------------------------------------------- answers


from emet_sdk.types import ReplyDone, TextDelta  # noqa: E402


def answering(tmp_path, name: str, pcm: bytes, *, chat: dict | None = None, manifest=None, **kw):
    manifest = manifest or body(write_wav(tmp_path / name, pcm))
    soul_doc = soul(provider="mock", chat=chat if chat is not None else {"provider": "mock"})
    return ListenSession(manifest, soul_doc, transcribe=True, reply=True, **kw)


async def drain(events):
    return [e async for e in events]


def test_a_turn_can_be_answered_by_a_model_the_engine_never_imported(tmp_path):
    """0.4's second seam end to end: hear the name, transcribe the words,
    hand them to the language model with the persona, stream the reply."""
    session = answering(tmp_path, "a.wav", spoken(b"what", b"time", b"is it"))

    async def scenario():
        async with session:
            turns = [(e, u) async for e, u in session.turns()]
            (_, utterance) = turns[0]
            events = await drain(session.answer(utterance.transcript.text))
            return utterance, events

    utterance, events = run(scenario())
    assert utterance.transcript.text == "what time is it"
    deltas = [e for e in events if isinstance(e, TextDelta)]
    done = events[-1]
    assert isinstance(done, ReplyDone)
    assert done.stop_reason == "end"
    assert done.text == "You said: what time is it"
    assert "".join(d.text for d in deltas) == done.text
    assert session.chat_name == "mock"
    assert session.llm_descriptor is not None and session.llm_descriptor.healthy


def test_the_persona_becomes_the_system_prompt_and_the_conversation_accumulates(tmp_path):
    session = answering(tmp_path, "p.wav", frame())
    session.soul["persona"] = {"system_prompt": "You are Emet. Be honest."}
    from emet_engine.prompting import system_prompt

    session.system_prompt = system_prompt(session.soul)

    async def scenario():
        async with session:
            await drain(session.answer("hello"))
            await drain(session.answer("and again"))
            return session._llm.prompts

    prompts = run(scenario())
    assert prompts[0].system.startswith("You are Emet. Be honest.")
    assert [m.role for m in prompts[1].messages] == ["user", "assistant", "user"]
    assert prompts[1].messages[1].content == "You said: hello"
    assert prompts[1].messages[2].content == "and again"


def test_a_failed_reply_is_returned_and_kept_out_of_the_conversation(tmp_path):
    manifest = body(write_wav(tmp_path / "f.wav", frame()))
    manifest["models"] = {"chat": {"provider": "mock"}}
    session = answering(tmp_path, "f.wav", frame(), manifest=manifest)

    async def scenario():
        async with session:
            session._llm._started = False  # the vendor went away mid-run
            events = await drain(session.answer("hello"))
            return events, list(session.conversation.messages)

    events, messages = run(scenario())
    (done,) = events
    assert done.stop_reason == "error" and done.error
    assert [m.role for m in messages] == ["user"]


def test_answering_records_the_two_latencies(tmp_path):
    session = answering(tmp_path, "s.wav", frame())

    async def scenario():
        async with session:
            await drain(session.answer("hello"))
            return session.stats

    stats = run(scenario())
    assert len(stats.reply_first_ms) == 1 and len(stats.reply_done_ms) == 1
    assert stats.reply_first_ms[0] <= stats.reply_done_ms[0]
    assert "llm first" in stats.report(live=False) and "llm done" in stats.report(live=False)


def test_asking_for_replies_with_no_language_model_names_the_field(tmp_path):
    session = answering(tmp_path, "n.wav", frame(), chat={})
    session.chat = None
    session.chat_name = None
    with pytest.raises(EngineError) as exc:
        run(session.start())
    assert "models.chat.provider" in str(exc.value)
    assert "mock" in str(exc.value)
    run(session.stop())


def test_an_uninstalled_language_model_is_a_missing_plugin(tmp_path):
    session = answering(tmp_path, "u.wav", frame(), chat={"provider": "abacus"})
    with pytest.raises(MissingPluginError):
        run(session.start())
    run(session.stop())


def test_a_language_model_that_cannot_start_is_refused_with_its_own_reason(tmp_path):
    manifest = body(write_wav(tmp_path / "x.wav", frame()))
    manifest["models"] = {"chat": {"provider": "mock", "params": {"fail_on_start": True}}}
    session = answering(tmp_path, "x.wav", frame(), manifest=manifest)
    with pytest.raises(EngineError) as exc:
        run(session.start())
    assert "unreachable" in str(exc.value) and "will not run" in str(exc.value)
    assert session._audio is None, "the microphone was opened before the refusal"
    run(session.stop())


def test_answering_without_reply_enabled_is_an_error(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "r.wav", frame())), soul())

    async def scenario():
        async with session:
            with pytest.raises(EngineError, match="reply=True"):
                await drain(session.answer("hello"))

    run(scenario())


def test_the_body_takes_the_language_model_over_from_the_soul(tmp_path):
    manifest = body(write_wav(tmp_path / "o.wav", frame()))
    manifest["models"] = {"stt": {"provider": "mock"}, "chat": {"provider": "mock", "params": {"reply": "As you wish."}}}
    session = answering(
        tmp_path, "o.wav", frame(), manifest=manifest, chat={"provider": "openai", "model": "x", "key_env": "EMET_OPENAI_KEY"}
    )
    assert session.chat_name == "mock"

    async def scenario():
        async with session:
            return (await drain(session.answer("anything")))[-1].text

    assert run(scenario()) == "As you wish."


# ------------------------------------------------------- honest verdicts


def test_frames_dropped_while_answering_are_charged_to_the_loops_own_choice(tmp_path):
    """The reference body dropped 11 frames during a two-second reply and
    the verdict said the loop did not keep up. It kept up fine; it was not
    reading, on purpose. Those frames are counted, labelled, and left out of
    the verdict."""
    session = answering(tmp_path, "d.wav", frame())

    async def scenario():
        async with session:
            session._audio.dropped = 3  # lost while listening: the loop's fault
            real_reply = session._llm.reply

            async def slow_reply(prompt):
                session._audio.dropped = 8  # five more, lost while the model thought
                async for event in real_reply(prompt):
                    yield event

            session._llm.reply = slow_reply
            await drain(session.answer("hello"))
            return session.stats

    stats = run(scenario())
    assert stats.dropped == 8 and stats.dropped_busy == 5
    assert "5 while speaking, thinking or waiting for the words" in stats.report(live=False)
    assert not stats.kept_up, "three frames were lost while listening, and that still counts"


def test_a_run_that_only_dropped_frames_while_busy_kept_up():
    from emet_engine.metrics import SessionStats

    stats = SessionStats()
    for _ in range(10):
        stats.record_frame(1.0)
    stats.dropped = 11
    stats.dropped_busy = 11
    assert stats.kept_up
    assert "11 while speaking, thinking or waiting for the words" in stats.report(live=False)


# ------------------------------------------------------------------ speech


from emet_sdk.plugin import PluginError  # noqa: E402


def speaking(tmp_path, name: str, pcm: bytes, *, tts: dict | None = None, manifest=None, sink: str = "null", **kw):
    manifest = manifest or body(write_wav(tmp_path / name, pcm), sink=sink)
    soul_doc = soul(provider="mock", chat={"provider": "mock"})
    soul_doc["models"]["tts"] = tts if tts is not None else {"provider": "mock"}
    return ListenSession(manifest, soul_doc, transcribe=True, reply=True, speak=True, **kw)


def wav_body(path: str, out: str, **wake_params) -> dict:
    doc = body(path, **wake_params)
    doc["audio"]["output"] = {"sink": "wav", "device": "none", "params": {"path": out}}
    return doc


def read_wav(path: str) -> tuple[int, bytes]:
    with wave.open(path, "rb") as w:
        return w.getframerate(), w.readframes(w.getnframes())


def test_a_turn_can_be_answered_aloud_through_a_voice_the_engine_never_imported(tmp_path):
    """0.4's third seam end to end: hear the name, transcribe, reply, and
    speak the reply through a voice and a sink, both reached by name. The
    wav the sink wrote reads back as the words the model said."""
    out = str(tmp_path / "said.wav")
    manifest = wav_body(write_wav(tmp_path / "a.wav", spoken(b"what", b"time", b"is it")), out)
    session = speaking(tmp_path, "a.wav", b"", manifest=manifest)

    async def scenario():
        async with session:
            turns = [(e, u) async for e, u in session.turns()]
            (_, utterance) = turns[0]
            events = await drain(session.answer_aloud(utterance.transcript.text))
            return events, session.last_spoken

    events, outcome = run(scenario())
    done = events[-1]
    assert isinstance(done, ReplyDone) and done.text == "You said: what time is it"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == done.text
    assert [s.text for s in outcome] == ["You said: what time is it"] and outcome[0].ok
    from emet_providers.mock import read_back  # a test may name the layer above; the engine may not

    rate, pcm = read_wav(out)
    assert read_back(pcm) == "You said: what time is it"
    assert rate == 16000, "the sink was opened at the rate the voice stated"
    assert session.tts_name == "mock"
    assert session.tts_descriptor is not None and session.tts_descriptor.healthy


def test_the_voice_states_the_rate_and_the_sink_is_opened_to_match(tmp_path):
    manifest = body(write_wav(tmp_path / "r.wav", frame()))
    manifest["audio"]["output"]["sample_rate"] = 48000
    manifest["models"] = {"tts": {"provider": "mock", "params": {"sample_rate": 8000}}}
    session = speaking(tmp_path, "r.wav", frame(), manifest=manifest)

    async def scenario():
        async with session:
            return session.sink_format, session._sink.format

    sink_format, opened = run(scenario())
    assert sink_format.sample_rate == 8000 and opened.sample_rate == 8000


def test_without_a_voice_the_sink_keeps_the_manifests_rate(tmp_path):
    manifest = body(write_wav(tmp_path / "m.wav", frame()))
    manifest["audio"]["output"]["sample_rate"] = 48000
    session = ListenSession(manifest, soul())

    async def scenario():
        async with session:
            return session.sink_format.sample_rate

    assert run(scenario()) == 48000


def test_the_first_sentence_plays_before_the_reply_is_done(tmp_path):
    """The reason everything streams. A slow model writes two sentences;
    the first is at the sink before the model has finished the second."""
    marks: list[str] = []
    session = speaking(tmp_path, "s.wav", frame())

    async def scenario():
        async with session:
            real_sink_play = session._sink.play

            async def logged_play(pcm):
                marks.append("play")
                await real_sink_play(pcm)

            session._sink.play = logged_play

            async def slow_reply(prompt):
                # A model's tokens carry their leading space, so the first
                # sentence is confirmed by the token that starts the second.
                yield TextDelta("First one.")
                yield TextDelta(" Second")
                await asyncio.sleep(0.05)
                yield TextDelta(" one.")
                marks.append("model done")
                yield ReplyDone(text="First one. Second one.", stop_reason="end", model="mock")

            session._llm.reply = slow_reply
            async for event in session.answer_aloud("hello"):
                if isinstance(event, ReplyDone):
                    marks.append("reply done")
            # A copy: the session plays its `muted` tone on the way out,
            # through the same patched sink, after this reply is over.
            return list(marks), session.last_spoken

    marks, spoken = run(scenario())
    assert marks.index("play") < marks.index("model done") < marks.index("reply done")
    assert [s.text for s in spoken] == ["First one.", "Second one."]
    # The mock voice makes a chunk a word, and each chunk reaches the sink
    # as it is made: the first sentence's two before the model is done, the
    # second sentence's two after.
    assert marks[: marks.index("model done")].count("play") == 2
    assert marks.count("play") == 4, "the second sentence was spoken after the reply ended"


def test_a_reply_without_a_full_stop_is_still_spoken(tmp_path):
    manifest = body(write_wav(tmp_path / "t.wav", frame()))
    manifest["models"] = {"chat": {"provider": "mock", "params": {"reply": "no end mark here"}}, "tts": {"provider": "mock"}}
    session = speaking(tmp_path, "t.wav", frame(), manifest=manifest)

    async def scenario():
        async with session:
            await drain(session.answer_aloud("hello"))
            return session.last_spoken

    (spoken,) = run(scenario())
    assert spoken.text == "no end mark here" and spoken.ok


def test_a_sentence_the_voice_loses_is_counted_and_the_rest_is_heard(tmp_path):
    manifest = body(write_wav(tmp_path / "l.wav", frame()))
    manifest["models"] = {
        "chat": {"provider": "mock", "params": {"reply": "Fine. The symbol POISON here. Fine again."}},
        "tts": {"provider": "mock", "params": {"fail_on": "POISON"}},
    }
    session = speaking(tmp_path, "l.wav", frame(), manifest=manifest)

    async def scenario():
        async with session:
            await drain(session.answer_aloud("hello"))
            return session.last_spoken, session.stats

    spoken, stats = run(scenario())
    assert [s.ok for s in spoken] == [True, False, True]
    assert stats.sentences_spoken == 2 and stats.sentences_lost == 1
    assert "1 lost" in stats.report(live=False)


def test_answering_aloud_records_the_two_latencies_a_person_feels(tmp_path):
    session = speaking(tmp_path, "m.wav", frame())

    async def scenario():
        async with session:
            await drain(session.answer_aloud("hello"))
            return session.stats

    stats = run(scenario())
    assert len(stats.speech_first_ms) == 1 and len(stats.speech_done_ms) == 1
    assert stats.speech_first_ms[0] <= stats.speech_done_ms[0]
    assert len(stats.reply_first_ms) == 1, "the model's own numbers are still recorded"
    report = stats.report(live=False)
    assert "voice first" in report and "voice done" in report


def test_speak_text_says_a_fixed_line(tmp_path):
    out = str(tmp_path / "line.wav")
    manifest = wav_body(write_wav(tmp_path / "f.wav", frame()), out)
    session = speaking(tmp_path, "f.wav", frame(), manifest=manifest)

    async def scenario():
        async with session:
            return await session.speak_text("Hello there. I am awake.")

    spoken = run(scenario())
    assert [s.text for s in spoken] == ["Hello there.", "I am awake."]
    from emet_providers.mock import read_back

    assert read_back(read_wav(out)[1]) == "Hello there. I am awake."


def test_asking_for_speech_with_no_voice_names_the_field(tmp_path):
    session = speaking(tmp_path, "n.wav", frame(), tts={})
    session.tts = None
    session.tts_name = None
    with pytest.raises(EngineError) as exc:
        run(session.start())
    assert "models.tts.provider" in str(exc.value)
    assert "mock" in str(exc.value)
    run(session.stop())


def test_an_uninstalled_voice_is_a_missing_plugin(tmp_path):
    session = speaking(tmp_path, "u.wav", frame(), tts={"provider": "gramophone"})
    with pytest.raises(MissingPluginError):
        run(session.start())
    run(session.stop())


def test_a_voice_that_cannot_start_is_refused_with_its_own_reason(tmp_path):
    manifest = body(write_wav(tmp_path / "x.wav", frame()))
    manifest["models"] = {"tts": {"provider": "mock", "params": {"fail_on_start": True}}}
    session = speaking(tmp_path, "x.wav", frame(), manifest=manifest)
    with pytest.raises(EngineError) as exc:
        run(session.start())
    assert "never be heard" in str(exc.value) and "missing" in str(exc.value)
    assert session._audio is None, "the microphone was opened before the refusal"
    run(session.stop())
    run(session.stop())


def test_the_souls_voice_block_reaches_the_plugin(tmp_path):
    session = speaking(tmp_path, "v.wav", frame())
    session.soul["voice"] = {"rate": 1.3}
    session._voice_block = session.soul["voice"]

    async def scenario():
        async with session:
            return session._tts.rate

    assert run(scenario()) == 1.3


def test_the_body_takes_the_voice_over_from_the_soul(tmp_path):
    manifest = body(write_wav(tmp_path / "o.wav", frame()))
    manifest["models"] = {"tts": {"provider": "mock", "params": {"sample_rate": 8000}}}
    session = speaking(
        tmp_path, "o.wav", frame(), manifest=manifest,
        tts={"provider": "deepgram", "model": "aura-2-thalia-en", "key_env": "EMET_DEEPGRAM_KEY"},
    )
    assert session.tts_name == "mock"

    async def scenario():
        async with session:
            return session.tts_descriptor.sample_rate

    assert run(scenario()) == 8000


def test_speaking_without_speak_enabled_is_an_error(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "w.wav", frame())), soul(provider="mock", chat={"provider": "mock"}), transcribe=True, reply=True)

    async def scenario():
        async with session:
            with pytest.raises(EngineError, match="speak=True"):
                await drain(session.answer_aloud("hello"))
            with pytest.raises(EngineError, match="speak=True"):
                await session.speak_text("hello")

    run(scenario())


def test_hush_with_a_voice_drops_the_queue_and_cancels_the_sink(tmp_path):
    session = speaking(tmp_path, "h.wav", frame())

    async def scenario():
        async with session:
            session._mouth.open()
            await session._mouth.say("One.")
            await session.hush()
            return session._sink.cancelled

    assert run(scenario()) == 1


def test_without_asking_no_voice_is_built(tmp_path):
    soul_doc = soul(provider="mock", chat={"provider": "mock"})
    soul_doc["models"]["tts"] = {"provider": "mock"}
    session = ListenSession(body(write_wav(tmp_path / "q.wav", frame())), soul_doc)

    async def scenario():
        async with session:
            return session._tts, session._mouth

    assert run(scenario()) == (None, None)
    assert session.tts_name == "mock", "the selection is still reported"


def test_frames_dropped_while_waiting_for_the_final_are_the_loops_own_choice(tmp_path):
    """On the reference body the transcriber once waited five seconds for a
    final that never came. The loop does not read the microphone through
    that wait, 37 frames were lost, and the verdict blamed the listening
    loop for them. They are charged to the wait, like a reply's."""
    session = transcribing(tmp_path, "w.wav", spoken(b"hello", b"there"))

    async def scenario():
        async with session:
            real_finish = session._stt.finish

            async def slow_finish():
                session._audio.dropped = 37  # lost while the provider stalled
                return await real_finish()

            session._stt.finish = slow_finish
            turns = [u async for _, u in session.turns()]
            return turns, session.stats

    turns, stats = run(scenario())
    assert turns[0].transcript is not None and turns[0].transcript.text == "hello there"
    assert stats.dropped == 37 and stats.dropped_busy == 37
    assert stats.kept_up, "the loop kept up while it was listening"


# --------------------------------------------------------- trailing clause


def test_a_transcript_ending_mid_clause_gets_one_more_window(tmp_path):
    """The mock hears "tell me about the"; "the" ends no sentence, so the
    endpointer waits one more window, and the utterance says so."""
    soul_doc = soul(provider="mock")
    soul_doc["interaction"] = {"extend_on_incomplete": True}
    pcm = spoken(b"tell me", b"about the") + frame() * 20  # room for the second window
    extended: list[str] = []
    session = ListenSession(
        body(write_wav(tmp_path / "x.wav", pcm)), soul_doc, transcribe=True, on_extended=extended.append
    )

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()], session.stats

    (utterance,), stats = run(scenario())
    assert utterance.extended
    assert extended == ["tell me about the"]
    assert stats.extended == 1
    assert "extended 1" in stats.report(live=False)


def test_a_finished_transcript_is_not_extended(tmp_path):
    soul_doc = soul(provider="mock")
    soul_doc["interaction"] = {"extend_on_incomplete": True}
    session = ListenSession(
        body(write_wav(tmp_path / "y.wav", spoken(b"what", b"time"))), soul_doc, transcribe=True
    )

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert not utterance.extended


def test_the_soul_has_to_ask_for_the_trailing_clause(tmp_path):
    session = ListenSession(
        body(write_wav(tmp_path / "z.wav", spoken(b"about the"))), soul(provider="mock"), transcribe=True
    )
    assert not session.extend_on_incomplete

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    assert not run(scenario())[0].extended


def test_without_a_transcriber_nothing_is_judged(tmp_path):
    """The heuristic reads the transcript. With no transcriber there is
    none, and the soul's flag changes nothing."""
    soul_doc = soul()
    soul_doc["interaction"] = {"extend_on_incomplete": True}
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 5 + frame() * 20
    session = ListenSession(body(write_wav(tmp_path / "v.wav", pcm)), soul_doc)

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    assert not run(scenario())[0].extended


# ------------------------------------------------------------ intent tags


def test_tags_in_the_reply_are_lifted_out_before_the_voice_reported_and_acted_on(tmp_path):
    """A tag is never read aloud. It is reported, and since 0.5 performed
    through its binding where it stood: on this bodiless body curiosity is a
    filler in the reply's voice before the sentence it opened, and attending
    to the speaker is silence."""
    out = str(tmp_path / "tagged.wav")
    manifest = wav_body(write_wav(tmp_path / "i.wav", frame()), out)
    manifest["models"] = {
        "chat": {"provider": "mock", "params": {"reply": "[express.curiosity] Oh! [laughs] Tell me more. [attend.speaker]"}},
        "tts": {"provider": "mock"},
    }
    seen = []
    session = speaking(tmp_path, "i.wav", frame(), manifest=manifest, on_intent=seen.append)

    async def scenario():
        async with session:
            events = await drain(session.answer_aloud("hello"))
            return events, session.last_intents, session.last_spoken, session.performed

    events, intents, spoken, performed = run(scenario())
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Oh! Tell me more. "
    assert [i.name for i in intents] == ["express.curiosity", "attend.speaker"]
    assert [i.name for i in seen] == ["express.curiosity", "attend.speaker"]
    assert [s.text for s in spoken] == ["hm?", "Oh!", "Tell me more."]
    from emet_providers.mock import read_back

    assert read_back(read_wav(out)[1]) == "hm? Oh! Tell me more."
    attended = next(p for p in performed if p.intent.name == "attend.speaker")
    assert attended.outcome == "silent" and attended.ok
    done = events[-1]
    assert isinstance(done, ReplyDone) and "[express.curiosity]" in done.text, "the model's own text is kept whole"


# ------------------------------------------------------------------ talk


def talking(tmp_path, name: str, pcm: bytes, *, manifest=None, soul_doc=None, out: str | None = None, **kw):
    manifest = manifest or (wav_body(write_wav(tmp_path / name, pcm), out) if out else body(write_wav(tmp_path / name, pcm)))
    manifest.setdefault("models", {})
    for stage in ("stt", "chat", "tts"):
        manifest["models"].setdefault(stage, {"provider": "mock"})
    return ListenSession(manifest, soul_doc or soul(), transcribe=True, reply=True, speak=True, **kw)


def test_talk_runs_the_whole_loop_and_reports_each_exchange(tmp_path):
    out = str(tmp_path / "talk.wav")
    session = talking(tmp_path, "t.wav", spoken(b"what", b"time") + false_wake_pcm(), out=out)
    said: list[str] = []
    deltas: list[str] = []

    async def scenario():
        async with session:
            return [x async for x in session.talk(on_said=said.append, on_delta=deltas.append)]

    exchanges = run(scenario())
    assert [x.heard for x in exchanges] == ["what time", ""]
    first, second = exchanges
    assert first.reply is not None and first.reply.text == "You said: what time"
    assert [s.text for s in first.spoken] == ["You said: what time"] and first.line is None
    assert second.reply is None and second.spoken == () and second.line is None
    assert said == ["what time"] and "".join(deltas) == "You said: what time"


def false_wake_pcm() -> bytes:
    return frame() + frame(PHRASE.encode()) + frame() * 50


def test_talk_says_the_souls_line_for_a_false_wake(tmp_path):
    soul_doc = soul()
    soul_doc["persona"] = {"lines": {"nothing_heard": "Yes?"}}
    session = talking(tmp_path, "f.wav", false_wake_pcm(), soul_doc=soul_doc)

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.line == "Yes?" and [s.text for s in exchange.spoken] == ["Yes?"]


def test_talk_says_the_souls_line_after_a_refusal_and_after_a_failure(tmp_path):
    soul_doc = soul()
    soul_doc["persona"] = {"lines": {"declined": "Not that one.", "failed": "Lost it."}}
    # Two words a turn: the energy detector needs two loud frames in a row,
    # and a turn that never counted as speech waits out the lead-in, which
    # would swallow the second wake.
    session = talking(tmp_path, "r.wav", spoken(b"hello", b"there") + spoken(b"once", b"again"), soul_doc=soul_doc)

    async def scenario():
        async with session:
            real_reply = session._llm.reply
            calls = 0

            async def moody_reply(prompt):
                nonlocal calls
                calls += 1
                if calls == 1:
                    yield ReplyDone(text="", stop_reason="refusal", model="mock")
                    return
                yield TextDelta("Half a")
                yield ReplyDone(text="Half a", stop_reason="error", error="boom", model="mock")

            session._llm.reply = moody_reply
            return [x async for x in session.talk()]

    refused, failed = run(scenario())
    assert refused.line == "Not that one." and [s.text for s in refused.spoken] == ["Not that one."]
    assert failed.line == "Lost it." and [s.text for s in failed.spoken] == ["Half a", "Lost it."]


def test_talk_needs_every_stage(tmp_path):
    session = ListenSession(body(write_wav(tmp_path / "s.wav", frame())), soul(provider="mock", chat={"provider": "mock"}), transcribe=True, reply=True)

    async def scenario():
        async with session:
            with pytest.raises(EngineError, match="speak=True"):
                async for _ in session.talk():
                    pass

    run(scenario())

# ------------------------------------------------------ its own voice, heard


#: Marks where the capture queue ends in a test recording: the frames up to
#: and including it waited while the robot spoke, the ones after it arrive
#: once listening resumes. The mock wake engine ignores it.
QUEUE_END = b"QUEUE-END"


def discarding(source):
    """A stand-in for the microphone's `discard_queued()` on the wav source:
    throw away the frames up to and including the next `QUEUE_END`, and
    count them as dropped, as the microphone does."""
    calls = []

    def discard_queued():
        thrown = 0
        while True:
            raw = source._wav.readframes(source.format.frame_samples)
            if len(raw) < source.format.frame_bytes:
                break
            thrown += 1
            if QUEUE_END in raw:
                break
        source.dropped = getattr(source, "dropped", 0) + thrown
        calls.append(thrown)
        return thrown

    return discard_queued, calls


#: A turn that ends on silence after twelve quiet frames at the default
#: patience, then what waited while the robot answered: a little of the
#: room, then its own voice saying the phrase.
TURN = frame() + frame(PHRASE.encode()) + loud_frame() * 5 + frame() * 12
OWN_VOICE = TURN + frame() * 4 + frame(PHRASE.encode()) + frame(QUEUE_END) + frame() * 40


def own_voice_run(
    tmp_path, pcm, *, speak_between=True, aec=None, live=True, latency_s=0.05, discard=True, lag_frames=0,
    transcribe=False,
):
    wake = {"lag_frames": lag_frames} if lag_frames else {}
    manifest = body(write_wav(tmp_path / "own.wav", pcm), **wake)
    if aec is not None:
        manifest["audio"]["input"]["aec"] = aec
    session = ListenSession(manifest, soul(provider="mock") if transcribe else soul(), transcribe=transcribe)

    async def scenario():
        calls = []
        async with session:
            if live:
                session.source_name = "microphone"  # the file stands in for a live card
            if latency_s is not None:
                session._sink.stream_latency_s = latency_s  # a sink playing into a room
            if discard:
                session._audio.discard_queued, calls = discarding(session._audio)
            turns = []
            async for event, utterance in session.turns():
                turns.append(utterance)
                if speak_between and len(turns) == 1:
                    await session.say(loud_frame())  # the reply, played into the room
            return turns, calls, session.stats, session.own_voice_wakes

    return run(scenario())


def test_what_it_said_while_not_listening_is_thrown_away_before_it_listens(tmp_path):
    """On the reference body it heard "hey emet" in its own "Say, 'Emet, wake
    up.'" and answered its own "wake up", twice in two (2026-09-27). The
    capture queue held its voice when it listened again."""
    turns, calls, stats, _ = own_voice_run(tmp_path, OWN_VOICE)
    assert len(turns) == 1, "it did not wake on its own voice"
    assert calls == [6]
    assert stats.dropped == 6 and stats.dropped_busy == 6, "lost while busy, and counted"


def test_nothing_is_thrown_away_when_it_did_not_speak(tmp_path):
    """The queue then holds the room, and a person may already be saying
    its name again."""
    turns, calls, _, _ = own_voice_run(tmp_path, OWN_VOICE, speak_between=False)
    assert len(turns) == 2 and calls == []


@pytest.mark.parametrize("case", ["aec", "replay", "no room"])
def test_a_body_that_cannot_hear_itself_keeps_everything(tmp_path, case):
    """Echo cancellation takes its voice out, a recording never heard it,
    and a sink that plays into no room made no sound to hear."""
    kwargs = {
        "aec": {"aec": "hardware"},
        "replay": {"live": False},
        "no room": {"latency_s": None},
    }[case]
    turns, calls, _, _ = own_voice_run(tmp_path, OWN_VOICE, **kwargs)
    assert len(turns) == 2 and calls == []


@pytest.mark.parametrize(
    ("at", "own"),
    [(1, True), (2, True), (3, False)],
)
def test_the_tail_is_whole_frames_and_a_person_just_after_it_is_heard(tmp_path, at, own):
    """The speaker is still sounding the last of the reply for its output
    latency when the loop listens again. At 50 ms and an 80 ms frame that is
    130 ms, rounded up to two whole frames: a phrase ending in either is its
    own voice, and one ending in the third frame is a person."""
    pcm = TURN + frame(QUEUE_END) + frame() * (at - 1) + frame(PHRASE.encode()) + frame() * 40
    turns, _, stats, ignored = own_voice_run(tmp_path, pcm)
    assert ignored == (1 if own else 0)
    assert len(turns) == (1 if own else 2)


@pytest.mark.parametrize(
    ("at", "own"),
    [(2, True), (3, False)],
)
def test_a_phrase_that_ended_in_the_tail_is_its_own_voice_however_late_it_is_reported(tmp_path, at, own):
    """A wake engine reports a phrase late: pocketsphinx 150 to 220 ms after
    it ended. Here two frames late, so a phrase that ended in the tail's
    second frame is reported in the fourth, after the tail, and is still its
    own voice; one that ended in the third frame is a person."""
    pcm = TURN + frame() * 2 + frame(QUEUE_END) + frame() * (at - 1) + frame(PHRASE.encode()) + frame() * 40
    turns, _, _, ignored = own_voice_run(tmp_path, pcm, lag_frames=2)
    assert ignored == (1 if own else 0)
    assert len(turns) == (1 if own else 2)


def test_a_wake_ignored_as_its_own_voice_hands_nothing_to_the_transcriber(tmp_path):
    """The words said with the name reach the transcriber only for a wake
    that counts. Handed over for a wake ignored as the robot's own voice,
    they would start the person's next question with its own words."""
    # Each word fills its frame, as speech does. A word and then digital
    # silence would be cut as the end of the name (`past_the_name`), so the
    # robot's word would reach the transcriber as silence, and this test
    # would still pass with the handover moved ahead of the own-voice check.
    robot, what = (word.ljust(FRAME_BYTES, b" ") for word in (b"robot", b"what"))
    pcm = (
        TURN
        + frame(QUEUE_END)
        + frame(PHRASE.encode()) + robot  # its own voice, ending in the tail
        + frame() * 4
        + frame(PHRASE.encode()) + what + frame(b"time")  # a person
        + frame() * 40
    )
    turns, _, _, ignored = own_voice_run(tmp_path, pcm, lag_frames=1, transcribe=True)
    assert ignored == 1
    assert [u.transcript.text for u in turns] == ["", "what time"]


def test_wakes_gets_the_same_guard(tmp_path):
    """`wakes()`, the two-line way to use a session, and a caller that has
    the robot speak between its events."""
    pcm = frame() + frame(PHRASE.encode()) + frame() * 5 + frame(PHRASE.encode()) + frame(QUEUE_END) + frame() * 10
    manifest = body(write_wav(tmp_path / "w.wav", pcm))
    session = ListenSession(manifest, soul())

    async def scenario():
        async with session:
            session.source_name = "microphone"
            session._sink.stream_latency_s = 0.05
            session._audio.discard_queued, calls = discarding(session._audio)
            events = []
            async for event in session.wakes():
                events.append(event)
                await session.say(loud_frame())
            return events, calls

    events, calls = run(scenario())
    assert len(events) == 1 and calls == [7], "the five quiet frames, its own voice, and the marker"


def test_a_fresh_iterator_after_speaking_gets_the_same_guard(tmp_path):
    """The check lives on the session, so a caller that takes one turn,
    speaks, and starts `turns()` again is covered too."""
    manifest = body(write_wav(tmp_path / "f.wav", OWN_VOICE))
    session = ListenSession(manifest, soul())

    async def scenario():
        async with session:
            session.source_name = "microphone"
            session._sink.stream_latency_s = 0.05
            session._audio.discard_queued, calls = discarding(session._audio)
            first = session.turns()
            await first.__anext__()
            await first.aclose()
            await session.say(loud_frame())
            rest = [u async for _, u in session.turns()]
            return rest, calls

    rest, calls = run(scenario())
    assert rest == [] and calls == [6]


def test_a_source_that_cannot_throw_its_queue_away_is_not_protected(tmp_path, caplog):
    """`discard_queued()` is optional in the contract. Without it nothing is
    thrown away, the tail covers only the oldest frames of the backlog, and
    the log says so once: a phrase in those frames is ignored, one later in
    the backlog wakes it."""
    import logging

    caplog.set_level(logging.WARNING, logger="emet_engine.session")
    early = TURN + frame(PHRASE.encode()) + frame() * 40
    turns, _, stats, ignored = own_voice_run(tmp_path, early, discard=False)
    assert len(turns) == 1 and ignored == 1 and stats.dropped == 0
    assert sum("cannot throw away" in r.getMessage() for r in caplog.records) == 1

    late = TURN + frame() * 4 + frame(PHRASE.encode()) + frame() * 40
    turns, _, _, ignored = own_voice_run(tmp_path, late, discard=False)
    assert len(turns) == 2 and ignored == 0, "later in the backlog, its own voice still wakes it"


def test_a_sentence_counts_as_sound_when_any_of_its_audio_played():
    """A sentence the voice broke off part way already played its first
    chunks; only one that produced no audio at all made no sound."""
    from emet_engine.speech import SpokenSentence

    session = ListenSession(body("unused.wav"), soul())
    session._count_playback([SpokenSentence("Lost.", 0, error="the voice failed")])
    assert session._playbacks == 0
    session._count_playback([SpokenSentence("Say, Emet, wake up.", 2048, error="ReadError mid-sentence")])
    assert session._playbacks == 1
    session._count_playback([SpokenSentence("Hi.", 3200)])
    assert session._playbacks == 2


# ------------------------------------------------- how a turn ends (0.5.7)


from emet_sdk.discovery import PluginRegistry  # noqa: E402
from emet_sdk.types import Health, Transcript  # noqa: E402

from emet_engine.prompting import DEFAULT_LINES  # noqa: E402
from emet_engine.session import TurnEnd, read_health  # noqa: E402

#: A chat model's detail as a plugin writes it (contract 0.5.7 section 3.3).
UNKNOWN_MODEL = (
    "OpenAI does not know the model 'gpt-4-0314' (HTTP 404). It is retired, misspelt, "
    "or not open to this key. Choose another under models.chat.model."
)
OUT_OF_CREDIT = "OpenAI says the account is out of credit (HTTP 429, insufficient_quota). Retrying will not help."
TWO_TURNS = spoken(b"hello", b"there") + spoken(b"once", b"again")


def unavailable_talk(tmp_path, pcm: bytes, **kw) -> ListenSession:
    """Every stage a mock, the chat model one its vendor does not know."""
    manifest = body(write_wav(tmp_path / "u.wav", pcm))
    manifest["models"] = {
        "stt": {"provider": "mock"},
        "chat": {"provider": "mock", "params": {
            "fail_on_start": True, "fault": "model_unavailable", "fault_detail": UNKNOWN_MODEL}},
        "tts": {"provider": "mock"},
    }
    return ListenSession(manifest, soul(), transcribe=True, reply=True, speak=True, **kw)


def spy_on_the_transcriber(session: ListenSession) -> dict:
    """Count what reaches the started transcriber."""
    seen = {"bytes": 0, "finals": 0}
    feed, finish = session._stt.feed, session._stt.finish

    async def fed(frame):
        seen["bytes"] += len(frame)
        return await feed(frame)

    async def finished():
        seen["finals"] += 1
        return await finish()

    session._stt.feed, session._stt.finish = fed, finished
    return seen


def said(spoken_sentences) -> str:
    return " ".join(s.text for s in spoken_sentences)


def test_a_model_its_vendor_does_not_know_boots_and_each_wake_gets_one_line(tmp_path):
    """E17 (c) with 0.5.7-D4: the robot boots, says why at each wake once the
    person stops, and sends nothing to the transcriber or the model."""
    performed = []
    session = unavailable_talk(tmp_path, TWO_TURNS, on_performed=performed.append)

    async def scenario():
        async with session:
            seen = spy_on_the_transcriber(session)
            exchanges = [x async for x in session.talk()]
            return exchanges, seen, list(session._llm.prompts)

    exchanges, seen, prompts = run(scenario())
    assert session.chat_unavailable == UNKNOWN_MODEL
    assert session.notices == [
        "the language model 'mock' cannot answer, so each wake gets one line and nothing is sent to the "
        f"transcriber or the model. {UNKNOWN_MODEL}"
    ]
    assert len(exchanges) == 2
    for exchange in exchanges:
        assert exchange.outcome == "unavailable" and exchange.failure.case == "llm-unavailable-before"
        assert exchange.reply is None and exchange.utterance.transcript is None
        assert exchange.line == DEFAULT_LINES["unavailable"] == said(exchange.spoken)
        record = exchange.record
        assert record.outcome == "unavailable" and record.final_ms is None and record.reply_first_ms is None
        assert record.stt_audio_s == 0 and record.llm_chars == 0
        assert record.voice_chars == sum(len(s.text) for s in exchange.spoken) > 0
        assert record.voice_first_ms is not None and record.gap_ms is not None, "the wait to the line is the gap"
    assert seen == {"bytes": 0, "finals": 0}, "no transcription spent on a turn no model can answer"
    assert prompts == [], "and nothing sent to the model"
    assert [p.intent.name for p in performed].count("signal.unavailable") == 2
    assert "signal.offline" not in [p.intent.name for p in performed]


def test_on_a_body_with_a_status_light_the_unavailable_line_is_still_said(tmp_path):
    """Finding A7 F1 of 0.5.7: a status light takes `signal.unavailable`, so
    the chain's voice rung never runs, and the session says the line itself
    after the light has shown it."""
    performed = []
    session = unavailable_talk(tmp_path, spoken(b"hello", b"there"), on_performed=performed.append)
    session.manifest["capabilities"] = [{
        "id": "ring", "type": "light", "role": "status", "form": "ring", "count": 16,
        "driver": {"plugin": "emet_hal.mock", "params": {}},
    }]

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    shown = [p for p in performed if p.intent.name == "signal.unavailable"]
    assert len(shown) == 1 and shown[0].outcome == "done" and not shown[0].binding.is_voice
    assert shown[0].binding.capability_id == "ring" and shown[0].said is None
    assert exchange.line == DEFAULT_LINES["unavailable"] == said(exchange.spoken)


def test_the_wake_is_answered_at_its_endpoint_after_the_person_stops(tmp_path):
    """Contract 0.5.7 section 9, C5 (a): at the end of the turn, never over
    the question."""
    session = unavailable_talk(tmp_path, spoken(b"hello", b"there"))

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.utterance.had_speech and exchange.utterance.tail_ms == 960.0
    assert exchange.record.tail_ms == 960.0


def test_a_reply_that_finds_the_model_unavailable_sends_nothing_after_it(tmp_path):
    performed = []
    session = talking(tmp_path, "m.wav", TWO_TURNS, on_performed=performed.append)
    calls = []

    async def scenario():
        async with session:
            async def retired(prompt):
                calls.append(prompt)
                session._llm.health = lambda: Health(ok=False, detail=UNKNOWN_MODEL, faults=("model_unavailable",))
                yield ReplyDone(text="", stop_reason="error", error=UNKNOWN_MODEL, model="mock")

            session._llm.reply = retired
            return [x async for x in session.talk()]

    first, second = run(scenario())
    assert first.outcome == "unavailable" and first.failure.case == "llm-unavailable-on-reply"
    assert first.line == DEFAULT_LINES["unavailable"]
    assert first.record.stt_audio_s > 0 and first.record.llm_chars > 0
    assert session.chat_unavailable == UNKNOWN_MODEL
    assert second.failure.case == "llm-unavailable-before" and second.reply is None
    assert second.utterance.transcript is None and second.record.stt_audio_s == 0
    assert len(calls) == 1
    assert [p.intent.name for p in performed].count("signal.unavailable") == 2


def failing_reply(session: ListenSession, faults: tuple[str, ...], detail: str = "boom"):
    """Every reply fails, and the model's report names `faults`."""

    async def reply(prompt):
        yield ReplyDone(text="", stop_reason="error", error=detail, model="mock")

    session._llm.reply = reply
    session._llm.health = lambda: Health(ok=not faults, detail=detail if faults else None, faults=faults)


def test_an_unpaid_account_gets_its_own_line_and_no_offline_signal(tmp_path):
    """OC3: "ask me again" asks for a retry that cannot help, and the network
    is up, so `signal.offline` would say the wrong thing."""
    performed = []
    session = talking(tmp_path, "c.wav", TWO_TURNS, on_performed=performed.append)

    async def scenario():
        async with session:
            failing_reply(session, ("out_of_credit",), OUT_OF_CREDIT)
            return [x async for x in session.talk()]

    first, second = run(scenario())
    for exchange in (first, second):
        assert exchange.outcome == "out_of_credit" and exchange.failure.case == "llm-out-of-credit"
        assert exchange.line == DEFAULT_LINES["out_of_credit"] == said(exchange.spoken)
    assert second.record.llm_chars > 0, "credit can come back, so the next turn is still asked"
    assert session.chat_unavailable is None
    assert "signal.offline" not in [p.intent.name for p in performed]


def test_a_plain_failure_is_still_offline_with_the_souls_line(tmp_path):
    performed = []
    soul_doc = soul()
    soul_doc["persona"] = {"lines": {"failed": "Lost it."}}
    session = talking(tmp_path, "f.wav", spoken(b"hello", b"there"), soul_doc=soul_doc, on_performed=performed.append)

    async def scenario():
        async with session:
            failing_reply(session, ())
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.outcome == "failed" and exchange.failure.case == "llm-failed"
    assert exchange.line == "Lost it."
    assert "signal.offline" in [p.intent.name for p in performed]


def test_a_report_that_raises_after_a_failed_reply_is_a_plain_failure(tmp_path):
    session = talking(tmp_path, "h.wav", spoken(b"hello", b"there"))

    async def scenario():
        async with session:
            failing_reply(session, ())

            def broken():
                raise ValueError("unknown fault kind 'overcurrent'")

            session._llm.health = broken
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.outcome == "failed" and exchange.failure.case == "llm-failed"


def test_a_model_whose_report_names_both_kinds_is_unavailable(tmp_path):
    """Contract 0.5.7 section 3.2: `model_unavailable` before `out_of_credit`,
    since a retired model stays retired once the account is paid."""
    session = talking(tmp_path, "b.wav", spoken(b"hello", b"there"))

    async def scenario():
        async with session:
            failing_reply(session, ("out_of_credit", "model_unavailable"), UNKNOWN_MODEL)
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.outcome == "unavailable" and exchange.failure.case == "llm-unavailable-on-reply"
    assert session.chat_unavailable == UNKNOWN_MODEL


@pytest.mark.parametrize(
    "spent, outcome, case, line",
    [(300, "empty_at_cap", "llm-empty-at-cap", "failed"), (299, "cut_at_cap", "llm-cut-at-cap", None)],
)
def test_a_reply_stopped_at_its_cap(tmp_path, spent, outcome, case, line):
    """DL1: all of the cap spent thinking leaves no words, and the person is
    told something; a reply cut part way keeps its words and says nothing more."""
    performed = []
    manifest = body(write_wav(tmp_path / "cap.wav", spoken(b"hello", b"there")))
    manifest["models"] = {"chat": {"provider": "mock", "params": {"reasoning_tokens": spent}}}
    session = talking(tmp_path, "cap.wav", b"", manifest=manifest, on_performed=performed.append)

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert session.reply_cap == 300
    assert exchange.reply.stop_reason == "length"
    assert exchange.outcome == outcome and exchange.failure.case == case
    assert exchange.line == (DEFAULT_LINES[line] if line else None)
    assert "signal.offline" not in [p.intent.name for p in performed]


@pytest.mark.parametrize("written", ["[express.curiosity]", "**", "\N{SLIGHTLY SMILING FACE}"])
def test_a_reply_at_its_cap_with_no_word_to_hear_is_empty(tmp_path, written):
    """Review finding engine-core/code#6 of 0.5.7: a reply that wrote only a
    tag, or only marks the cleaning empties, before its cap has no words, so
    the person is told why in place of hearing a filler or nothing."""
    session = talking(tmp_path, "e.wav", spoken(b"hello", b"there"))

    async def scenario():
        async with session:
            async def capped(prompt):
                yield TextDelta(written)
                yield ReplyDone(text=written, stop_reason="length", model="mock")

            session._llm.reply = capped
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.outcome == "empty_at_cap" and exchange.failure.case == "llm-empty-at-cap"
    assert exchange.line == DEFAULT_LINES["failed"]


@pytest.mark.parametrize(
    "faults, outcome, case, offline",
    [
        ((), "words_lost", "stt-words-lost", True),
        (("out_of_credit",), "out_of_credit", "stt-out-of-credit", False),
        (("model_unavailable",), "words_lost", "stt-model-rejected", False),
        # Contract 0.5.7 section 3.2: `model_unavailable` is read first,
        # wherever the report lists it.
        (("out_of_credit", "model_unavailable"), "words_lost", "stt-model-rejected", False),
    ],
)
def test_words_that_never_arrived_take_their_row_from_the_transcribers_report(tmp_path, faults, outcome, case, offline):
    performed = []
    session = talking(tmp_path, "w.wav", spoken(b"hello", b"there"), on_performed=performed.append)

    async def scenario():
        async with session:
            async def lost():
                return Transcript(text="", final=True, error="the words went nowhere")

            session._stt.finish = lost
            session._stt.health = lambda: Health(ok=not faults, detail="x" if faults else None, faults=faults)
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.outcome == outcome and exchange.failure.case == case
    assert exchange.line == DEFAULT_LINES["failed"], "the hearing's own words wait on the maintainer (C6)"
    assert ("signal.offline" in [p.intent.name for p in performed]) is offline


def test_without_a_voice_a_turn_still_ends_and_says_nothing(tmp_path):
    """`emet-listen --reply`: the outcome and the row, the signal's other
    rungs, and no line."""
    performed = []
    session = answering(tmp_path, "q.wav", TWO_TURNS, on_performed=performed.append)

    async def scenario():
        async with session:
            ends = []
            async for _, utterance in session.turns():
                if not ends:
                    failing_reply(session, ())
                done = [e async for e in session.answer_acted(utterance.transcript.text)][-1]
                ends.append(await session.finish_turn(utterance, done))
            return ends

    first, second = run(scenario())
    assert first == TurnEnd(outcome="failed", failure=first.failure, line=None, spoken=())
    assert first.failure.case == "llm-failed"
    assert "signal.offline" in [p.intent.name for p in performed]
    assert session.stats.turn_records[0].outcome == "failed"


def test_a_turn_with_no_row_is_named_and_left_as_it_was(tmp_path):
    """No words after speech, no speech at all, and words with no reply
    asked: three outcomes, no failure line, no spoken line."""
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 4 + frame() * 20 + false_wake_pcm() + spoken(b"what")
    session = transcribing(tmp_path, "n.wav", pcm)

    async def scenario():
        async with session:
            return [await session.finish_turn(u, None) async for _, u in session.turns()]

    ends = run(scenario())
    assert [e.outcome for e in ends] == ["nothing_heard", "no_speech", "not_answered"]
    assert all(e.failure is None and e.line is None for e in ends)
    assert [r.outcome for r in session.stats.turn_records] == ["nothing_heard", "no_speech", "not_answered"]


def test_a_model_that_cannot_answer_is_sent_nothing_even_when_asked(tmp_path):
    session = answering(tmp_path, "g.wav", frame())

    async def scenario():
        async with session:
            session.chat_unavailable = UNKNOWN_MODEL
            return await drain(session.answer("hello"))

    (done,) = run(scenario())
    assert done.stop_reason == "error" and done.error == UNKNOWN_MODEL
    assert session.conversation.messages == []


# ------------------------------------------------------------ read_health


def test_read_health_passes_a_report_through():
    class Plugin:
        def health(self):
            return Health(ok=False, detail="no key", faults=("no_key",))

    assert read_health(Plugin()) == Health(ok=False, detail="no key", faults=("no_key",))


def test_read_health_turns_a_report_that_raises_into_a_refusal():
    class Plugin:
        def health(self):
            return Health(ok=False, faults=("overcurrent",))

    health = read_health(Plugin())
    assert not health.ok and health.faults == ("health",)
    assert health.detail.startswith("ValueError: unknown fault kind 'overcurrent'.")


def test_a_language_model_whose_report_raises_at_boot_is_refused_in_one_line(tmp_path, monkeypatch):
    """An outside plugin that named its own kind without `x_`: the boot's
    refusal, with the `ValueError`'s words, and no traceback."""
    cls = PluginRegistry.discover().load_llm("mock")
    monkeypatch.setattr(cls, "health", lambda self: Health(ok=False, faults=("overcurrent",)))
    manifest = body(write_wav(tmp_path / "r.wav", frame()))
    manifest["models"] = {"chat": {"provider": "mock", "params": {"fail_on_start": True}}}
    session = answering(tmp_path, "r.wav", frame(), manifest=manifest)

    async def scenario():
        async with session:
            pass

    with pytest.raises(EngineError) as raised:
        run(scenario())
    assert str(raised.value).startswith(
        "the language model 'mock' cannot start, so the robot would hear questions it can never answer. "
        "It will not run. ValueError: unknown fault kind 'overcurrent'."
    )


@pytest.mark.parametrize(
    "stage, refusal",
    [
        ("wake", "the wake engine 'mock' cannot hear 'hey emet', so this robot would never answer to its own name."),
        ("stt", "the speech recognition provider 'mock' cannot start, so what people say would go nowhere."),
        ("tts", "the voice 'mock' cannot start, so the robot would answer and never be heard."),
    ],
)
def test_a_report_that_raises_at_boot_is_refused_in_one_line_at_every_stage(tmp_path, monkeypatch, stage, refusal):
    """Contract 0.5.7 section 3.2 (C9): every boot report is read through
    `read_health`, the wake engine's, the transcriber's and the voice's as
    well as the language model's (review finding engine-core/tests#3)."""
    registry = PluginRegistry.discover()
    cls = {"wake": registry.load_wake, "stt": registry.load_stt, "tts": registry.load_tts}[stage]("mock")
    monkeypatch.setattr(cls, "health", lambda self: Health(ok=False, faults=("overcurrent",)))
    pcm = frame()
    if stage == "wake":
        manifest = body(write_wav(tmp_path / "r.wav", pcm), fail_on_start=True)
    else:
        manifest = body(write_wav(tmp_path / "r.wav", pcm))
        manifest["models"] = {stage: {"provider": "mock", "params": {"fail_on_start": True}}}
    session = ListenSession(manifest, soul(), transcribe=stage == "stt", speak=stage == "tts")

    async def scenario():
        async with session:
            pass

    with pytest.raises(EngineError) as raised:
        run(scenario())
    assert str(raised.value).startswith(refusal)
    assert "ValueError: unknown fault kind 'overcurrent'." in str(raised.value)


@pytest.mark.parametrize("load", ["load_stt", "load_llm", "load_tts"])
def test_a_healthy_providers_notice_is_kept_for_the_boot_to_print(tmp_path, monkeypatch, load):
    """Contract 0.5.7 section 3.1 (a): every provider's, the transcriber's and
    the voice's as well as the language model's (review finding
    engine-core/tests#8)."""
    notice = "the language model 'mock-1' retires on 2027-07-24 at Anthropic. Choose another under models.chat.model before then."
    cls = getattr(PluginRegistry.discover(), load)("mock")
    monkeypatch.setattr(cls, "health", lambda self: Health(ok=True, detail=notice))
    session = talking(tmp_path, "o.wav", frame())

    async def scenario():
        async with session:
            return list(session.notices), session.chat_unavailable

    assert run(scenario()) == ([notice], None)


# --------------------------------------------------------- the turn record


def test_a_turn_fills_its_record_where_each_figure_is_made(tmp_path):
    """TT8 and OC1: one record a turn, appended at the endpoint and filled in
    place; the exchange carries it."""
    session = talking(tmp_path, "rec.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            seen = spy_on_the_transcriber(session)
            exchanges = [x async for x in session.talk()]
            return exchanges, seen, list(session._llm.prompts)

    (exchange,), seen, (prompt,) = run(scenario())
    record = exchange.record
    assert record is session.last_turn is session.stats.turn_records[0]
    assert record.turn == 1 and record.outcome == "answered" == exchange.outcome and exchange.failure is None
    assert record.tail_ms == exchange.utterance.tail_ms == 960.0
    assert record.final_ms is not None and record.reply_first_ms is not None and record.voice_first_ms is not None
    assert record.input_tokens == exchange.reply.input_tokens and record.output_tokens == exchange.reply.output_tokens
    assert record.stt_audio_s == seen["bytes"] / (2 * 16000) > 0
    assert record.llm_chars == len(prompt.system) + sum(len(m.content) for m in prompt.messages)
    assert record.voice_chars == len("You said: what time")
    assert (record.sentences, record.cleaned) == (1, 0)
    assert (record.stt_host, record.llm_host, record.voice_host) == (None, None, None), "every mock runs on this body"


def test_the_transcribers_seconds_include_the_audio_handed_over_with_the_name(tmp_path):
    said_with_it = b"what".ljust(FRAME_BYTES, b" ")
    pcm = frame() + frame(PHRASE.encode()) + said_with_it + frame(b"time") + frame() * 20
    manifest = body(write_wav(tmp_path / "w.wav", pcm), lag_frames=1)
    session = transcribing(tmp_path, "w.wav", pcm, manifest=manifest)

    async def scenario():
        async with session:
            seen = spy_on_the_transcriber(session)
            turns = [u async for _, u in session.turns()]
            return turns, seen

    (utterance,), seen = run(scenario())
    assert utterance.transcript.text == "what time"
    record = session.last_turn
    assert record.stt_audio_s == seen["bytes"] / 32000
    turn_frames = len(utterance.audio) // FRAME_BYTES
    assert seen["bytes"] > turn_frames * FRAME_BYTES, "the handover is counted with the turn's own frames"


def test_a_host_off_the_body_is_named_on_the_record(tmp_path):
    manifest = body(write_wav(tmp_path / "e.wav", spoken(b"hi", b"there")))
    manifest["models"] = {"chat": {"provider": "mock", "params": {"endpoint": "https://api.example.com/v1"}}}
    session = talking(tmp_path, "e.wav", b"", manifest=manifest)

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert exchange.record.llm_host == "api.example.com"


def test_every_stage_off_the_body_is_named_on_the_record_and_its_line(tmp_path):
    """Review finding engine-core/tests#1 of 0.5.7: the transcriber's and the
    voice's hosts, which the sent part of the line is built from."""
    manifest = body(write_wav(tmp_path / "h.wav", spoken(b"hi", b"there")))
    manifest["models"] = {
        "stt": {"provider": "mock", "params": {"endpoint": "wss://stt.example.com/v1/listen"}},
        "chat": {"provider": "mock", "params": {"endpoint": "https://llm.example.com/v1"}},
        "tts": {"provider": "mock", "params": {"endpoint": "https://tts.example.com/v1/speak"}},
    }
    session = talking(tmp_path, "h.wav", b"", manifest=manifest)

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    record = exchange.record
    assert (record.stt_host, record.llm_host, record.voice_host) == (
        "stt.example.com", "llm.example.com", "tts.example.com"
    )
    assert record.stt_audio_s > 0 and record.llm_chars > 0 and record.voice_chars > 0
    assert record.line().split("; sent ")[1] == (
        f"{record.stt_audio_s:.1f} s of audio to stt.example.com, "
        f"{record.llm_chars} characters to llm.example.com, {record.voice_chars} characters to tts.example.com"
    )


def slow_final(session: ListenSession, seconds: float, transcript: Transcript | None = None) -> None:
    """The started transcriber's final takes `seconds`, and is `transcript`
    when one is given."""
    finish = session._stt.finish

    async def slow():
        final = await finish()
        await asyncio.sleep(seconds)
        return transcript or final

    session._stt.finish = slow


def test_a_line_after_a_slow_final_is_timed_from_the_final(tmp_path):
    """Contract 0.5.7 section 2.1: the first sound is counted from the final
    transcript, so a slow final is not counted twice in the gap (review
    finding engine-core/tests#2)."""
    session = talking(tmp_path, "s.wav", spoken(b"hello", b"there"))

    async def scenario():
        async with session:
            slow_final(session, 0.2, Transcript(text="", final=True, error="the words went nowhere"))
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    record = exchange.record
    assert exchange.failure.case == "stt-words-lost" and exchange.line
    assert record.final_ms >= 190 and record.reply_first_ms is None
    assert record.voice_first_ms < record.final_ms - 100, (record.final_ms, record.voice_first_ms)


def test_a_reply_is_timed_from_the_final_and_its_first_sound_from_the_final_too(tmp_path):
    session = talking(tmp_path, "r.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            slow_final(session, 0.2)

            async def slow_reply(prompt):
                await asyncio.sleep(0.1)
                yield TextDelta("It is noon.")
                yield ReplyDone(text="It is noon.", model="mock")

            session._llm.reply = slow_reply
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    record = exchange.record
    assert record.final_ms >= 190
    assert 90 <= record.reply_first_ms < record.final_ms
    assert record.reply_first_ms <= record.voice_first_ms < record.final_ms


def test_a_filler_is_counted_with_what_the_voice_was_handed(tmp_path):
    """Contract 0.5.7 section 2.1: lines and fillers included (review finding
    engine-core/tests#4). On the bodiless test body a tag is a filler."""
    session = talking(tmp_path, "f.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            async def tagged(prompt):
                yield TextDelta("[express.curiosity] Hello there.")
                yield ReplyDone(text="[express.curiosity] Hello there.", model="mock")

            session._llm.reply = tagged
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert [s.from_reply for s in exchange.spoken] == [False, True]
    assert exchange.record.voice_chars == sum(len(s.text) for s in exchange.spoken) > len("Hello there.")
    assert (exchange.record.sentences, exchange.record.cleaned) == (1, 0)


def test_a_sentence_the_cleaning_changed_is_counted_on_the_record(tmp_path):
    session = talking(tmp_path, "m.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            async def marked(prompt):
                yield TextDelta("It is **noon**. Fine.")
                yield ReplyDone(text="It is **noon**. Fine.", model="mock")

            session._llm.reply = marked
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert (exchange.record.sentences, exchange.record.cleaned) == (2, 1)
    assert exchange.record.voice_chars == len("It is noon.") + len("Fine.")


def test_a_turn_is_closed_once_it_is_finished(tmp_path):
    """`finish_turn`'s docstring: the turn is over when it returns, so speech
    after it, a goodbye or a shutdown's signal, joins no printed record
    (review finding engine-core/tests#11)."""
    session = talking(tmp_path, "c.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            async for exchange in session.talk():
                before = exchange.record.voice_chars
                await session.speak_text("Goodbye for now.")
                return exchange.record, before

    record, before = run(scenario())
    assert record.voice_chars == before > 0


def test_the_tokens_of_every_reply_in_a_turn_are_summed(tmp_path):
    """And the characters of every prompt; the first reply's first word is
    the turn's (review finding engine-core/tests#5)."""
    session = answering(tmp_path, "k.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            async for _, utterance in session.turns():
                first = (await drain(session.answer(utterance.transcript.text)))[-1]
                second = (await drain(session.answer("and again")))[-1]
                return first, second, list(session._llm.prompts)

    first, second, prompts = run(scenario())
    record = session.last_turn
    assert record.input_tokens == first.input_tokens + second.input_tokens
    assert record.output_tokens == first.output_tokens + second.output_tokens
    assert len(prompts) == 2
    assert record.llm_chars == sum(len(p.system) + sum(len(m.content) for m in p.messages) for p in prompts)
    assert len(session.stats.reply_first_ms) == 2 and record.reply_first_ms == session.stats.reply_first_ms[0]


def test_a_turn_no_reply_counted_keeps_no_token_count(tmp_path):
    session = answering(tmp_path, "z.wav", spoken(b"what", b"time"))

    async def scenario():
        async with session:
            async def uncounted(prompt):
                yield TextDelta("Hi.")
                yield ReplyDone(text="Hi.", model="mock")

            session._llm.reply = uncounted
            async for _, utterance in session.turns():
                await drain(session.answer(utterance.transcript.text))

    run(scenario())
    assert (session.last_turn.input_tokens, session.last_turn.output_tokens) == (None, None)


# ---------------------------------------------------------- the live hook


def test_the_frame_hook_fires_once_a_frame_with_the_turn_so_far(tmp_path):
    calls = []
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 4 + frame() * 20
    session = ListenSession(
        body(write_wav(tmp_path / "live.wav", pcm)), soul(), on_frame=lambda *a: calls.append(a)
    )

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    (utterance,) = run(scenario())
    assert len(calls) == session.stats.frames
    silence, phrase = calls[:2]
    assert silence == (-99.9, None, None), "digital silence, outside a turn, at the floor"
    assert -99.9 < phrase[0] < 0 and phrase[1:] == (None, None), "the wake's own frame is still outside the turn"
    inside, after = calls[2:18], calls[18:]
    assert len(inside) == 4 + 12, "four loud frames, then twelve quiet ones to the endpoint at 900 ms"
    assert inside[0][0] == pytest.approx(20 * __import__("math").log10(4000 / 32768))
    assert all(isinstance(speech, float) and isinstance(quiet, float) for _, speech, quiet in inside)
    assert inside[3][1:] == (4 * 80.0, 0.0) and inside[-2][1:] == (4 * 80.0 + 11 * 80.0, 11 * 80.0)
    assert inside[-1][1:] == (utterance.duration_ms, utterance.tail_ms), "the last frame shows the turn as it ended"
    assert after and all(c[1:] == (None, None) for c in after), "waiting for the name again"


def test_the_frame_hook_runs_outside_the_per_frame_timing(tmp_path, monkeypatch):
    """Its cost never reaches `per frame` or the budget rows."""
    import emet_engine.session as session_module

    open_watches = [0]

    class Watch(session_module.Stopwatch):
        def __enter__(self):
            open_watches[0] += 1
            return super().__enter__()

        def __exit__(self, *exc):
            open_watches[0] -= 1
            return super().__exit__(*exc)

    monkeypatch.setattr(session_module, "Stopwatch", Watch)
    inside = []
    pcm = spoken(b"what", b"time")
    session = transcribing(tmp_path, "t.wav", pcm, on_frame=lambda *a: inside.append(open_watches[0]))

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()]

    run(scenario())
    assert inside and set(inside) == {0}


def test_without_the_hook_no_level_is_worked_out(tmp_path, monkeypatch):
    import emet_engine.session as session_module

    measured = []
    real = session_module._frame_level
    monkeypatch.setattr(session_module, "_frame_level", lambda f: measured.append(1) or real(f))
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 4 + frame() * 20
    session = ListenSession(body(write_wav(tmp_path / "q.wav", pcm)), soul())

    async def scenario():
        async with session:
            return [u async for _, u in session.turns()], [e async for e in session.wakes()]

    run(scenario())
    assert measured == []


def test_the_frame_hook_fires_in_wakes_too_outside_the_per_frame_timing(tmp_path, monkeypatch):
    """The class's docstring: once for every frame the loop reads, `wakes()`
    as well as `turns()` (review finding engine-core/tests#10)."""
    import emet_engine.session as session_module

    open_watches = [0]

    class Watch(session_module.Stopwatch):
        def __enter__(self):
            open_watches[0] += 1
            return super().__enter__()

        def __exit__(self, *exc):
            open_watches[0] -= 1
            return super().__exit__(*exc)

    monkeypatch.setattr(session_module, "Stopwatch", Watch)
    calls = []
    session = ListenSession(
        body(write_wav(tmp_path / "w.wav", saying(2))), soul(),
        on_frame=lambda *a: calls.append((open_watches[0], *a)),
    )

    async def scenario():
        async with session:
            return [e async for e in session.wakes()]

    events = run(scenario())
    assert len(events) == 2 and len(calls) == session.stats.frames > 0
    assert all(c[0] == 0 and c[2:] == (None, None) for c in calls)


def test_a_wake_with_no_transcriber_names_no_words(tmp_path):
    """Contract 0.5.7 section 2.1: `nothing_heard` is an empty final, and with
    no transcriber there is none, so speech after the wake is `not_answered`
    (review finding engine-core/claims-scope#11)."""
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 4 + frame() * 20 + false_wake_pcm()
    session = ListenSession(body(write_wav(tmp_path / "x.wav", pcm)), soul())

    async def scenario():
        async with session:
            return [await session.finish_turn(u, None) async for _, u in session.turns()]

    ends = run(scenario())
    assert [e.outcome for e in ends] == ["not_answered", "no_speech"]
    assert all(e.failure is None and e.line is None for e in ends)


# -------------------------------------------------- the audio plugins' start


def test_a_source_that_cannot_start_is_refused_in_one_line(tmp_path):
    """A missing replay file printed a traceback of about 40 lines (L11)."""
    missing = str(tmp_path / "nothere.wav")
    session = ListenSession(body(missing), soul())

    async def scenario():
        async with session:
            pass

    with pytest.raises(EngineError) as raised:
        run(scenario())
    assert str(raised.value).startswith(
        "the audio source 'wav' cannot start, so the robot would listen to nothing. It will not run. "
        f"could not open {missing!r}"
    )
    assert isinstance(raised.value.__cause__, PluginError)


def test_a_sink_that_cannot_start_is_refused_in_one_line(tmp_path):
    out = str(tmp_path / "no" / "such" / "folder" / "out.wav")
    session = ListenSession(wav_body(write_wav(tmp_path / "s.wav", frame()), out), soul())

    async def scenario():
        async with session:
            pass

    with pytest.raises(EngineError) as raised:
        run(scenario())
    assert str(raised.value).startswith(
        "the audio sink 'wav' cannot start, so nothing the robot says could be heard. It will not run. "
    )
    assert repr(out) in str(raised.value)


def test_a_plugin_error_with_no_words_is_named_by_its_type(tmp_path, monkeypatch):
    cls = PluginRegistry.discover().load_audio("wav")

    async def silent(self):
        raise PluginError()

    monkeypatch.setattr(cls, "start", silent)
    session = ListenSession(body(write_wav(tmp_path / "p.wav", frame())), soul())

    async def scenario():
        async with session:
            pass

    with pytest.raises(EngineError, match=r"It will not run\. PluginError$"):
        run(scenario())


@pytest.mark.parametrize("load, name", [("load_audio", "wav"), ("load_audio_out", "null")])
def test_an_audio_plugins_own_bug_keeps_its_traceback(tmp_path, monkeypatch, load, name):
    """Only a `PluginError` is the plugin saying the hardware will not start;
    anything else is a bug, and a one-line refusal would hide where it is.
    The sink's as well as the source's (review finding engine-core/tests#9)."""
    cls = getattr(PluginRegistry.discover(), load)(name)

    async def buggy(self):
        raise KeyError("frame_samples")

    monkeypatch.setattr(cls, "start", buggy)
    session = ListenSession(body(write_wav(tmp_path / "b.wav", frame())), soul())

    async def scenario():
        async with session:
            pass

    with pytest.raises(KeyError):
        run(scenario())


@pytest.mark.parametrize(
    "load, name, refusal",
    [
        ("load_audio", "wav", "the audio source 'wav' cannot start, so the robot would listen to nothing. It will not run. "),
        ("load_audio_out", "null", "the audio sink 'null' cannot start, so nothing the robot says could be heard. It will not run. "),
    ],
)
def test_an_audio_plugin_that_refuses_its_block_as_it_is_built_is_refused_in_one_line(
    tmp_path, monkeypatch, load, name, refusal
):
    """Review finding engine-core/code#2 of 0.5.7: the speaker refuses a
    `gain_db` that is not a finite number as it is built, and the refusal
    printed a traceback of 38 lines."""
    cls = getattr(PluginRegistry.discover(), load)(name)
    reason = "audio.output.gain_db is inf; it has to be a finite number of decibels, such as -6.0"

    def refuses(self, config=None, fmt=None):
        raise PluginError(reason)

    monkeypatch.setattr(cls, "__init__", refuses)
    session = ListenSession(body(write_wav(tmp_path / "g.wav", frame())), soul())

    async def scenario():
        async with session:
            pass

    with pytest.raises(EngineError) as raised:
        run(scenario())
    assert str(raised.value) == refusal + reason
    assert isinstance(raised.value.__cause__, PluginError)


def a_second(group: str, name: str) -> PluginRegistry:
    """The installed plugins, and a second `name` in `group` from an entry
    point no distribution declared, so the name has two claimants."""
    from importlib import metadata
    from importlib.metadata import EntryPoint

    from emet_sdk import discovery

    groups = [value for key, value in vars(discovery).items() if key.startswith("GROUP_")]
    assert len(groups) == 9
    installed = [ep for group_name in groups for ep in metadata.entry_points(group=group_name)]
    return PluginRegistry.from_entry_points([*installed, EntryPoint(name, "emet_fork:Plugin", group)])


@pytest.mark.parametrize("group", ["emet.audio", "emet.audio_out"])
def test_an_audio_plugin_two_distributions_offer_is_refused_before_anything_starts(tmp_path, group):
    """Contract 0.5.7 section 1: the clash is refused before any device
    opens. Review finding engine-core/code#1: the source's was found after
    the sink had opened and the boot tone had played, and the sink's after
    the providers and the body had started."""
    from emet_sdk.validate import DuplicatePluginError

    out = tmp_path / "out.wav"
    manifest = wav_body(write_wav(tmp_path / "d.wav", frame()), str(out))
    manifest["models"] = {"tts": {"provider": "mock"}}
    performed = []
    session = ListenSession(
        manifest, soul(), registry=a_second(group, "wav"), speak=True, on_performed=performed.append
    )

    async def scenario():
        async with session:
            pass

    with pytest.raises(DuplicatePluginError, match=f"in the group {group}"):
        run(scenario())
    assert performed == [] and not out.exists(), "no tone played, and the sink never opened"
    assert session.descriptor is None and session._tts is None and session.body is None


@pytest.mark.parametrize(("group", "stage"), [("emet.stt", "transcriber"), ("emet.llm", "model")])
def test_a_provider_two_distributions_offer_is_refused_before_any_stage_starts(tmp_path, group, stage):
    """Review finding sdk/code#7: the language model's name was found only
    after the transcriber had started and sent its preflight, and every
    provider's after the wake engine. Now every name the run loads is asked
    for its origin first, so nothing starts and nothing is sent."""
    from emet_sdk.validate import DuplicatePluginError

    session = answering(tmp_path, "c.wav", frame(), registry=a_second(group, "mock"))

    async def scenario():
        async with session:
            pass

    with pytest.raises(DuplicatePluginError, match=f"in the group {group}"):
        run(scenario())
    assert session._stt is None and session._llm is None, f"no stage started before the {stage}'s clash"
    assert session.descriptor is None, "the wake engine did not start either"


def test_a_driver_two_distributions_offer_is_refused_before_any_stage_starts(tmp_path):
    """sdk/code#7's driver half: a part's driver is found among the
    actuators, then the sensors, before the providers start."""
    from emet_sdk.validate import DuplicatePluginError

    manifest = body(write_wav(tmp_path / "p.wav", frame()))
    manifest["capabilities"] = [{"id": "lamp", "type": "light", "role": "ambient", "driver": {"plugin": "emet_hal.mock"}}]
    session = answering(tmp_path, "p.wav", frame(), manifest=manifest, registry=a_second("emet.actuators", "emet_hal.mock"))

    async def scenario():
        async with session:
            pass

    with pytest.raises(DuplicatePluginError, match="in the group emet.actuators"):
        run(scenario())
    assert session._stt is None and session.body is None
