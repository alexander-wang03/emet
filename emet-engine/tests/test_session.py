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
