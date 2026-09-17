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
    """`overflows` is read defensively like `dropped`: a file has no card."""
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
    assert "models.stt.provider" in message and "audio.stt.provider" in message
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
    assert "5 while speaking or thinking" in stats.report(live=False)
    assert not stats.kept_up, "three frames were lost while listening, and that still counts"


def test_a_run_that_only_dropped_frames_while_busy_kept_up():
    from emet_engine.metrics import SessionStats

    stats = SessionStats()
    for _ in range(10):
        stats.record_frame(1.0)
    stats.dropped = 11
    stats.dropped_busy = 11
    assert stats.kept_up
    assert "11 while speaking or thinking" in stats.report(live=False)


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
            return marks, session.last_spoken

    marks, spoken = run(scenario())
    assert marks.index("play") < marks.index("model done") < marks.index("reply done")
    assert [s.text for s in spoken] == ["First one.", "Second one."]
    assert marks.count("play") == 2, "the second sentence was spoken after the reply ended"


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


def test_tags_in_the_reply_are_lifted_out_before_the_voice_and_reported(tmp_path):
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
            return events, session.last_intents, session.last_spoken

    events, intents, spoken = run(scenario())
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Oh! Tell me more. "
    assert [i.name for i in intents] == ["express.curiosity", "attend.speaker"]
    assert [i.name for i in seen] == ["express.curiosity", "attend.speaker"]
    assert [s.text for s in spoken] == ["Oh!", "Tell me more."]
    from emet_providers.mock import read_back

    assert read_back(read_wav(out)[1]) == "Oh! Tell me more."
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
