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


# --------------------------------------------------------------- cassettes


import hashlib  # noqa: E402
import json  # noqa: E402

from emet_providers import cassette  # noqa: E402

REQUEST = {"provider": "deepgram", "model": "nova-3", "sample_rate": 16000, "query": {"smart_format": "false"}}


def pcm(n: int, size: int = FRAME_BYTES) -> bytes:
    """Bytes that spell nothing, so only the cassette can answer."""
    return bytes((n * 7 + i) % 200 + 1 if i % 2 else 0x80 + (n % 64) for i in range(size))


def tape(tmp_path, frames, partials: dict[int, str], final: str, request=REQUEST, name="c.json"):
    rec = cassette.TurnRecorder(request)
    for n, f in enumerate(frames, 1):
        rec.feed(f)
        if n in partials:
            rec.partial(partials[n])
    turn = rec.turn(final, confidence=0.99, model_version="2025-04-17.21547", recorded="2026-10-27", source="test_mock")
    return cassette.save(tmp_path / name, [turn])


def through(stt, frames):
    async def scenario():
        partials = [await stt.feed(f) for f in frames]
        return partials, await stt.finish()

    return run(scenario())


def test_a_cassette_gives_the_recorded_partials_and_final_for_the_same_bytes(tmp_path):
    frames = [pcm(1, 5440), pcm(2), pcm(3)]
    path = tape(tmp_path, frames, {2: "Where", 3: "Where are my keys?"}, "Where are my keys?")
    stt = started(cassette=str(path), cassette_request=REQUEST)
    assert stt.describe().healthy
    partials, final = through(stt, frames)
    assert partials[0] is None
    assert [p.text for p in partials[1:]] == ["Where", "Where are my keys?"]
    assert all(not p.final and p.confidence == 1.0 for p in partials[1:]), "a cassette keeps no partial's confidence"
    assert final == Transcript(text="Where are my keys?", final=True, confidence=0.99, error=None)


def test_other_bytes_are_a_miss_an_empty_final_with_the_reason_and_no_exception(tmp_path):
    frames = [pcm(1), pcm(2)]
    path = tape(tmp_path, frames, {1: "Where"}, "Where are my keys?")
    stt = started(cassette=str(path))
    partials, final = through(stt, [frames[0], pcm(9)])
    assert partials[0].text == "Where", "the prefix matched, so its partial was due"
    assert final.final and final.text == "" and final.error == cassette.MISS == "cassette miss"


def test_a_turn_recorded_for_another_request_is_a_miss(tmp_path):
    frames = [pcm(1)]
    path = tape(tmp_path, frames, {}, "What's 22", request={**REQUEST, "query": {"smart_format": "true"}})
    _, final = through(started(cassette=str(path), cassette_request=REQUEST), frames)
    assert final.error == "cassette miss"
    _, final = through(started(cassette=str(path)), frames)
    assert final.text == "What's 22", "without cassette_request every turn is a candidate"


def test_a_directory_of_cassettes_is_one_cassette(tmp_path):
    tape(tmp_path, [pcm(1)], {}, "one", name="a.json")
    tape(tmp_path, [pcm(2)], {}, "two", name="b.json")
    stt = started(cassette=str(tmp_path))
    assert through(stt, [pcm(2)])[1].text == "two"
    assert through(stt, [pcm(1)])[1].text == "one"


@pytest.mark.parametrize(
    "content, words",
    [
        (None, "no such file"),
        ("{oops", "not JSON"),
        (json.dumps({"format": "emet-bank/1"}), "format"),
    ],
)
def test_a_cassette_that_cannot_be_read_leaves_start_unhealthy_with_the_reason(tmp_path, content, words):
    path = tmp_path / "c.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    stt = started(cassette=str(path))
    assert not stt.describe().healthy
    health = stt.health()
    assert not health.ok and health.faults == ("start_failed",)
    assert words in (health.detail or "") and "cassette" in (health.detail or "")
    assert run(stt.feed(pcm(1))) is None


@pytest.mark.parametrize(
    "params, words",
    [
        ({"transcript": "hello"}, "exclude each other"),
        ({"cassette_request": {**REQUEST, "sample_rate": 8000}}, "8000 Hz"),
        ({"cassette_request": "deepgram"}, "mapping"),
        ({"cassette_request": {**REQUEST, "query": {"api_key": "x"}}}, "credential"),
    ],
)
def test_a_cassette_set_up_wrong_is_refused_at_start(tmp_path, params, words):
    path = tape(tmp_path, [pcm(1)], {}, "x")
    stt = started(cassette=str(path), **params)
    assert not stt.describe().healthy
    assert words in (stt.health().detail or "")


def test_a_cassette_request_without_a_cassette_is_refused():
    stt = started(cassette_request=REQUEST)
    assert not stt.describe().healthy
    assert "no cassette" in (stt.health().detail or "")


def test_record_turns_keeps_each_turns_bytes_exactly_as_fed(tmp_path):
    out = tmp_path / "turns"
    stt = started(record_turns=str(out))
    first = [frame(b"where"), frame(b"are my keys")[:2000], frame()]
    second = [frame(b"stop")]
    through(stt, first)
    through(stt, second)
    assert (out / "turn-0001.raw").read_bytes() == b"".join(first)
    assert (out / "turn-0002.raw").read_bytes() == b"".join(second)
    lines = [json.loads(line) for line in (out / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {
        "turn": 1,
        "bytes": 2560 + 2000 + 2560,
        "feed_sizes": [2560, 2000, 2560],
        "fed_sha256": hashlib.sha256(b"".join(first)).hexdigest(),
        "cassette": None,
        "final": "where are my keys",
        "partials": 2,
    }
    assert lines[1]["turn"] == 2 and lines[1]["final"] == "stop" and lines[1]["partials"] == 1


def test_record_turns_says_hit_or_miss_with_a_cassette(tmp_path):
    frames = [pcm(1), pcm(2)]
    path = tape(tmp_path, frames, {2: "Where are my keys?"}, "Where are my keys?")
    out = tmp_path / "turns"
    stt = started(cassette=str(path), record_turns=str(out))
    through(stt, frames)
    through(stt, [pcm(5)])
    lines = [json.loads(line) for line in (out / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(l["cassette"], l["final"], l["partials"]) for l in lines] == [("hit", "Where are my keys?", 1), ("miss", "", 0)]
    assert lines[1]["fed_sha256"] == hashlib.sha256(pcm(5)).hexdigest()
    assert (out / "turn-0002.raw").read_bytes() == pcm(5), "a miss keeps its bytes for the paid list"


def test_a_record_turns_write_that_fails_is_noted_and_the_turn_still_ends(tmp_path):
    """Losing a record is not the person's problem: the final comes back as
    it would have, and `record_error` says what was lost."""
    import shutil

    out = tmp_path / "turns"
    stt = started(record_turns=str(out))
    shutil.rmtree(out)
    out.write_text("a file where the directory was", encoding="utf-8")
    _, final = through(stt, [frame(b"hello")])
    assert final == Transcript(text="hello", final=True, confidence=1.0)
    assert (stt.record_error or "").startswith("record_turns: turn 1:")


def test_record_turns_refuses_a_directory_another_run_wrote(tmp_path):
    out = tmp_path / "turns"
    through(started(record_turns=str(out)), [frame(b"once")])
    again = started(record_turns=str(out))
    assert not again.describe().healthy
    assert "own" in (again.health().detail or "")


def test_without_the_new_params_nothing_is_written_and_nothing_is_hashed(tmp_path, monkeypatch):
    stt = started()
    monkeypatch.chdir(tmp_path)
    through(stt, [frame(b"hello")])
    assert list(tmp_path.iterdir()) == []
    assert stt._player is None and stt._turns_dir is None


# ----------------------------------------------------------------- version


def test_the_reported_version_matches_the_installed_distribution():
    """One declaration, in `pyproject.toml`, read back at import time, like
    the other three packages. See their tests for the drift that motivated
    it."""
    from importlib.metadata import version

    import emet_providers

    assert emet_providers.__version__ == version("emet-providers")
    assert emet_providers.__version__ != "0+unknown", "package is not installed"


# --------------------------------------------------------- language model


from emet_sdk.plugin import LanguageModelPlugin  # noqa: E402
from emet_sdk.types import Message, Prompt, ReplyDone, TextDelta, ToolCall, ToolSpec  # noqa: E402

from emet_providers.mock import MockLanguageModel  # noqa: E402


def llm(**params) -> MockLanguageModel:
    model = MockLanguageModel({"provider": "mock", "params": params})
    run(model.start())
    return model


async def collect(events) -> list:
    return [e async for e in events]


def prompt(*texts: str, tools: tuple = ()) -> Prompt:
    messages = tuple(Message(role="user" if i % 2 == 0 else "assistant", content=t) for i, t in enumerate(texts))
    return Prompt(system="Be brief.", messages=messages, tools=tools)


def test_the_mock_language_model_is_a_plugin_that_repeats_what_it_heard():
    model = llm()
    assert isinstance(model, LanguageModelPlugin)
    assert model.describe().healthy and model.describe().streaming and model.describe().tools
    events = run(collect(model.reply(prompt("what time is it"))))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["You", " said:", " what", " time", " is", " it"]
    done = events[-1]
    assert isinstance(done, ReplyDone)
    assert done.text == "You said: what time is it" and done.stop_reason == "end"
    assert done.model == "mock" and done.output_tokens == 6
    assert model.prompts[0].system == "Be brief."


def test_a_scripted_reply_is_said_whatever_was_asked():
    model = llm(reply="As you wish.")
    done = run(collect(model.reply(prompt("anything"))))[-1]
    assert done.text == "As you wish."


def test_the_mock_can_ask_for_a_tool_and_then_report_its_result():
    remember = ToolSpec(name="remember", description="keep a fact")
    model = llm(call_tool={"name": "remember", "arguments": {"fact": "likes tea"}})
    first = run(collect(model.reply(prompt("remember I like tea", tools=(remember,)))))
    call = first[0]
    assert call == ToolCall(id="call_1", name="remember", arguments={"fact": "likes tea"})
    assert first[-1].stop_reason == "tool" and first[-1].tool_calls == (call,)

    followed = Prompt(
        system="s",
        messages=(
            Message(role="user", content="remember I like tea"),
            Message(role="assistant", content="", tool_calls=(call,)),
            Message(role="tool", content="stored", tool_call_id=call.id),
        ),
        tools=(remember,),
    )
    done = run(collect(model.reply(followed)))[-1]
    assert done.text == "The tool said: stored" and done.stop_reason == "end"


def test_without_tools_on_offer_the_mock_answers_instead_of_calling():
    model = llm(call_tool={"name": "remember"})
    done = run(collect(model.reply(prompt("hello"))))[-1]
    assert done.stop_reason == "end" and done.text == "You said: hello"


def test_an_unstarted_or_unreachable_mock_model_says_so():
    unreachable = llm(fail_on_start=True)
    assert not unreachable.describe().healthy
    (done,) = run(collect(unreachable.reply(prompt("q"))))
    assert done.stop_reason == "error" and "not started" in (done.error or "")


def test_a_missing_key_is_reported_by_the_name_of_the_variable_for_the_model_too(monkeypatch):
    monkeypatch.delenv("EMET_MOCK_KEY", raising=False)
    model = MockLanguageModel({"provider": "mock", "key_env": "EMET_MOCK_KEY", "params": {"require_key": True}})
    run(model.start())
    assert not model.describe().healthy
    assert "EMET_MOCK_KEY" in (model.health().detail or "")


# ------------------------------------------------------------------ voice


from emet_sdk.plugin import PluginError, VoicePlugin  # noqa: E402
from emet_sdk.types import VoiceDescriptor  # noqa: E402

from emet_providers.mock import MockVoice, read_back  # noqa: E402


def voice(config: dict | None = None, soul_voice: dict | None = None, **params) -> MockVoice:
    cfg: dict = {"provider": "mock", "params": params}
    cfg.update(config or {})
    tts = MockVoice(cfg, soul_voice)
    run(tts.start())
    return tts


async def audio(tts: MockVoice, text: str) -> list[bytes]:
    return [c async for c in tts.speak(text)]


def test_the_mock_voice_is_a_voice_plugin_that_spells_the_words_into_the_audio():
    tts = voice()
    assert isinstance(tts, VoicePlugin)
    d = tts.describe()
    assert isinstance(d, VoiceDescriptor)
    assert d.healthy and d.streaming and d.sample_rate == 16000 and d.provider == "mock"
    chunks = run(audio(tts, "what time is it"))
    assert len(chunks) == 4, "one chunk a word, so the pipeline sees a stream"
    assert all(len(c) % 2 == 0 and len(c) >= MockVoice.CHUNK_BYTES for c in chunks)
    assert read_back(b"".join(chunks)) == "what time is it"
    assert tts.said == ["what time is it"]


def test_read_back_survives_any_chunking_and_keeps_short_words():
    tts = voice()
    pcm = b"".join(run(audio(tts, "I am a robot, it is true.")))
    assert read_back(pcm) == "I am a robot, it is true."
    assert read_back(pcm[:50] + pcm[50:]) == read_back(pcm)
    assert read_back(bytes(100)) == ""


def test_the_sample_rate_is_whatever_the_body_says():
    assert voice(sample_rate=8000).describe().sample_rate == 8000


def test_blank_text_makes_no_sound():
    tts = voice()
    assert run(audio(tts, "  ")) == []
    assert tts.said == []


def test_a_latency_is_waited_before_the_first_chunk():
    import time

    tts = voice(latency_ms=30)
    before = time.perf_counter()
    run(audio(tts, "hi"))
    assert time.perf_counter() - before >= 0.025


def test_a_poisoned_word_fails_the_sentence_after_its_first_chunk():
    tts = voice(fail_on="Ω")

    async def scenario():
        got = []
        with pytest.raises(PluginError, match="simulated"):
            async for chunk in tts.speak("the symbol Ω here"):
                got.append(chunk)
        return got

    got = run(scenario())
    assert [read_back(c) for c in got] == ["the", "symbol", "Ω".encode("ascii", "replace").decode()]
    assert run(audio(tts, "fine again")), "the plugin is not broken"


def test_a_missing_model_or_key_is_unhealthy_with_the_reason(monkeypatch):
    assert not voice(fail_on_start=True).describe().healthy
    assert "missing" in (voice(fail_on_start=True).health().detail or "")
    monkeypatch.delenv("EMET_MOCK_KEY", raising=False)
    tts = voice(config={"key_env": "EMET_MOCK_KEY"}, require_key=True)
    assert not tts.describe().healthy
    assert "EMET_MOCK_KEY" in (tts.health().detail or "")
    assert tts.health().faults == ("no_key",)


def test_an_unstarted_mock_voice_says_so():
    tts = MockVoice({"provider": "mock"})

    async def scenario():
        with pytest.raises(PluginError, match="not started"):
            await audio(tts, "hello")

    run(scenario())


def test_the_souls_rate_is_carried():
    assert voice(soul_voice={"rate": 1.5}).rate == 1.5
