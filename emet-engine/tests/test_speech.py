"""Speaking a reply as it is written.

Three things are proved here without a model, a voice or a speaker: that
sentences are found in a stream of deltas where a person would end them,
that the mouth plays them in order while the next is still being made, and
that a sentence the voice loses is one sentence lost and not the turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from emet_sdk.plugin import PluginError, VoicePlugin
from emet_sdk.types import VoiceDescriptor

from emet_engine.speech import Mouth, Sentences, split_sentences, speak_stream


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- sentences


@pytest.mark.parametrize(
    ("text", "done", "rest"),
    [
        ("Hello there. How are you? ", ["Hello there.", "How are you?"], ""),
        ("Hello there. How are", ["Hello there."], "How are"),
        ("No end yet", [], "No end yet"),
        ("Really?! Yes. ", ["Really?!", "Yes."], ""),
        ('He said "go." Then left. ', ['He said "go."', "Then left."], ""),
        ("It is 3.5 metres long. ", ["It is 3.5 metres long."], ""),
        ("Ask Dr. Who about it. Fine. ", ["Ask Dr. Who about it.", "Fine."], ""),
        ("Bring apples, pears, etc. and go. ", ["Bring apples, pears, etc. and go."], ""),
        ("Wait...", [], "Wait..."),
        ("Wait... then go. ", ["Wait... then go."], ""),
        ("Wait... ", [], "Wait... "),
        ("Wait... Then go. ", ["Wait...", "Then go."], ""),
        ("e.g. this one. Next. ", ["e.g. this one.", "Next."], ""),
    ],
)
def test_sentences_end_where_a_person_would(text, done, rest):
    assert split_sentences(text) == (done, rest)


def test_a_stream_of_deltas_yields_sentences_as_they_complete():
    s = Sentences()
    assert s.feed("It is") == []
    assert s.feed(" noon.") == []
    assert s.feed(" Go") == ["It is noon."]
    assert s.pending == "Go"
    assert s.feed(" now! And") == ["Go now!"]
    assert s.flush() == "And"
    assert s.flush() is None


def test_flush_normalises_whitespace_in_the_tail():
    s = Sentences()
    s.feed("  a  tail \n with  no end ")
    assert s.flush() == "a tail with no end"


# ----------------------------------------------------------------- stand-ins


class _Voice(VoicePlugin):
    """A voice that turns a sentence into its bytes, slowly if asked, and
    fails on a word if asked."""

    provider = "fake"

    def __init__(
        self,
        *,
        delay_ms: float = 0.0,
        fail_on: str | None = None,
        word_delay_ms: float = 0.0,
        log: list[str] | None = None,
    ) -> None:
        super().__init__({"provider": "fake"})
        self.delay_ms = delay_ms
        self.fail_on = fail_on
        self.word_delay_ms = word_delay_ms
        self.log = log
        self.asked: list[str] = []

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(provider="fake", sample_rate=8000, streaming=True)

    async def speak(self, text: str):
        self.asked.append(text)
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000.0)
        for word in text.split():
            if self.word_delay_ms:
                await asyncio.sleep(self.word_delay_ms / 1000.0)
            if self.fail_on and self.fail_on in word:
                raise PluginError(f"cannot say {word!r}")
            if self.log is not None:
                self.log.append(f"made:{word}")
            yield word.encode() + b"\x00\x00"


class _Sink:
    """Records what it was handed and when, relative to a shared log."""

    def __init__(self, log: list[str] | None = None, *, delay_ms: float = 0.0) -> None:
        self.format: Any = None
        self.played: list[bytes] = []
        self.log = log if log is not None else []
        self.delay_ms = delay_ms
        self.cancelled = 0

    async def start(self) -> None:
        pass

    async def play(self, pcm: bytes) -> None:
        words = pcm.replace(bytes(1), b" ").decode().strip()
        self.log.append(f"play:{words}")
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000.0)
        self.played.append(pcm)
        self.log.append(f"played:{words}")

    async def cancel(self) -> None:
        self.cancelled += 1

    async def stop(self) -> None:
        pass


# --------------------------------------------------------------------- mouth


def test_sentences_are_spoken_in_order_a_chunk_at_a_time(tmp_path):
    """Each chunk the voice produces goes to the sink as it arrives; the
    sink is what makes consecutive chunks one continuous sound."""
    voice, sink = _Voice(), _Sink()
    mouth = Mouth(voice, sink)

    async def scenario():
        mouth.open()
        await mouth.say("First one.")
        await mouth.say("Second one.")
        return await mouth.finish()

    spoken = run(scenario())
    assert voice.asked == ["First one.", "Second one."]
    assert sink.played == [b"First\x00\x00", b"one.\x00\x00", b"Second\x00\x00", b"one.\x00\x00"]
    assert [s.text for s in spoken] == ["First one.", "Second one."]
    assert all(s.ok for s in spoken)
    assert spoken[0].audio_bytes == len(sink.played[0]) + len(sink.played[1])


def test_the_next_sentence_is_synthesised_while_the_last_one_plays():
    """The pipeline. With a slow speaker, the voice is asked for the second
    sentence before the first has finished playing."""
    log: list[str] = []
    voice = _Voice()
    real_speak = voice.speak

    async def logged_speak(text):
        log.append(f"ask:{text}")
        async for chunk in real_speak(text):
            yield chunk

    voice.speak = logged_speak  # type: ignore[method-assign]
    sink = _Sink(log, delay_ms=40)
    mouth = Mouth(voice, sink)

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        await mouth.say("Two.")
        await mouth.finish()

    run(scenario())
    assert log.index("ask:Two.") < log.index("played:One."), "the second was being made while the first played"
    assert log.index("play:One.") < log.index("play:Two.")
    assert log[-1] == "played:Two."


def test_the_first_sound_is_reported_once_when_it_reaches_the_sink():
    marks: list[str] = []
    sink = _Sink(marks)
    mouth = Mouth(_Voice(), sink, on_first_audio=lambda: marks.append("first"))

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        await mouth.say("Two.")
        await mouth.finish()

    run(scenario())
    assert marks == ["first", "play:One.", "played:One.", "play:Two.", "played:Two."]


def test_a_sentence_the_voice_cannot_say_is_skipped_and_the_rest_is_heard():
    voice, sink = _Voice(fail_on="Ω"), _Sink()
    mouth = Mouth(voice, sink)

    async def scenario():
        mouth.open()
        await mouth.say("Fine.")
        await mouth.say("The symbol Ω here.")
        await mouth.say("Fine again.")
        return await mouth.finish()

    spoken = run(scenario())
    assert [s.ok for s in spoken] == [True, False, True]
    assert "cannot say" in (spoken[1].error or "")
    assert [p.split(b"\x00")[0] for p in sink.played] == [b"Fine.", b"Fine", b"again."]
    assert mouth.failures == [spoken[1]]
    assert spoken[1].audio_bytes > 0, (
        "the chunks before the failure are counted, and not played, because "
        "the failure was known before their turn came"
    )


def test_a_chunk_is_heard_before_the_voice_has_finished_the_sentence():
    """The point of the chunk path: with a voice that takes its time between
    words, the first word reaches the sink while the second is still being
    made. 0.4 gathered the sentence first."""
    log: list[str] = []
    voice, sink = _Voice(word_delay_ms=20, log=log), _Sink(log)
    mouth = Mouth(voice, sink)

    async def scenario():
        mouth.open()
        await mouth.say("One two.")
        return await mouth.finish()

    (spoken,) = run(scenario())
    assert spoken.ok and spoken.audio_bytes == len(b"One\x00\x00two.\x00\x00")
    assert log.index("played:One") < log.index("made:two."), "the first chunk was heard while the second was made"


def test_a_voice_that_fails_mid_sentence_keeps_what_was_heard_and_drops_the_rest():
    log: list[str] = []
    voice, sink = _Voice(word_delay_ms=10, fail_on="\u03a9", log=log), _Sink(log)
    mouth = Mouth(voice, sink)

    async def scenario():
        mouth.open()
        await mouth.say("Up to \u03a9 and beyond.")
        return await mouth.finish()

    (spoken,) = run(scenario())
    assert not spoken.ok and "cannot say" in (spoken.error or "")
    assert [p.split(b"\x00")[0] for p in sink.played] == [b"Up", b"to"]
    assert spoken.audio_bytes == len(b"Up\x00\x00to\x00\x00")


def test_a_voice_that_raises_the_wrong_thing_still_loses_only_one_sentence():
    voice = _Voice()

    async def broken(text):
        raise ValueError("not a PluginError")
        yield b""  # pragma: no cover

    voice.speak = broken  # type: ignore[method-assign]
    mouth = Mouth(voice, _Sink())

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        return await mouth.finish()

    (spoken,) = run(scenario())
    assert not spoken.ok and "ValueError" in (spoken.error or "")


def test_a_sink_that_fails_marks_the_sentence_and_goes_on():
    sink = _Sink()

    async def bad_play(pcm):
        raise RuntimeError("card unplugged")

    sink.play = bad_play  # type: ignore[method-assign]
    mouth = Mouth(_Voice(), sink)

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        await mouth.say("Two.")
        return await mouth.finish()

    spoken = run(scenario())
    assert [s.ok for s in spoken] == [False, False]
    assert "playback failed" in (spoken[0].error or "")


def test_blank_sentences_are_not_queued():
    mouth = Mouth(_Voice(), _Sink())

    async def scenario():
        mouth.open()
        await mouth.say("   ")
        return await mouth.finish()

    assert run(scenario()) == []


def test_hush_drops_the_queue_and_cancels_the_sink():
    sink = _Sink(delay_ms=200)
    mouth = Mouth(_Voice(delay_ms=20), sink)

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        await mouth.say("Two.")
        await mouth.say("Three.")
        await asyncio.sleep(0.05)  # the first is playing, the rest queued
        await mouth.hush()
        return sink.played, sink.cancelled

    played, cancelled = run(scenario())
    assert cancelled == 1
    assert len(played) <= 1


def test_saying_before_open_is_an_error():
    mouth = Mouth(_Voice(), _Sink())

    async def scenario():
        with pytest.raises(RuntimeError, match="not open"):
            await mouth.say("One.")

    run(scenario())


def test_finish_without_open_returns_nothing():
    assert run(Mouth(_Voice(), _Sink()).finish()) == []


def test_a_mouth_can_be_opened_again_for_the_next_reply():
    voice, sink = _Voice(), _Sink()
    mouth = Mouth(voice, sink)

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        first = await mouth.finish()
        mouth.open()
        await mouth.say("Two.")
        second = await mouth.finish()
        return first, second

    first, second = run(scenario())
    assert [s.text for s in first] == ["One."] and [s.text for s in second] == ["Two."]
    assert len(sink.played) == 2


# ----------------------------------------------------------- speak_stream


def test_a_text_stream_is_spoken_sentence_by_sentence_with_the_tail_last():
    voice, sink = _Voice(), _Sink()
    mouth = Mouth(voice, sink)

    async def deltas():
        for piece in ["It is", " noon.", " Go now", "! And", " then"]:
            yield piece

    spoken = run(speak_stream(mouth, deltas()))
    assert voice.asked == ["It is noon.", "Go now!", "And then"]
    assert [s.text for s in spoken] == voice.asked


# ----------------------------------------------- a voice that falls behind


class _PaddingSink(_Sink):
    """A sink that pads like the real speaker: it fills a block with silence
    whenever nothing was handed to it in time."""

    def __init__(self, log: list[str] | None = None) -> None:
        super().__init__(log)
        self.padded = 0

    def pad(self, blocks: int = 1) -> None:
        self.padded += blocks


def test_silence_inside_a_sentence_is_counted_and_silence_after_it_is_not():
    """The card is served on time either way, so PortAudio reports nothing.
    A gap inside a word is one a person hears; quiet between replies is not."""
    sink = _PaddingSink()
    mouth = Mouth(_Voice(), sink)
    real_play = sink.play

    async def play_then_fall_behind(pcm):
        await real_play(pcm)
        sink.pad()  # the voice was late with the next chunk

    sink.play = play_then_fall_behind  # type: ignore[method-assign]

    async def scenario():
        mouth.open()
        await mouth.say("One two.")
        spoken = await mouth.finish()
        sink.pad(5)  # nobody is speaking now
        return spoken

    spoken = run(scenario())
    assert [s.ok for s in spoken] == [True]
    assert mouth.starved == 2, "one per chunk played, and none of the five after"


def test_a_sink_that_cannot_pad_is_read_defensively():
    """A file has nothing to pad. `getattr`, like `dropped` on the source."""
    mouth = Mouth(_Voice(), _Sink())

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        return await mouth.finish()

    run(scenario())
    assert mouth.starved == 0


def test_hush_after_finish_leaves_the_sink_alone():
    """`ListenSession.stop()` hushes on the way out. A mouth that has already
    finished has nothing to interrupt, and cancelling then throws away the
    block the speaker keeps in hand, which is the end of the last word."""
    sink = _Sink()
    mouth = Mouth(_Voice(), sink)

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        await mouth.finish()
        await mouth.hush()

    run(scenario())
    assert sink.cancelled == 0


def test_hush_while_speaking_still_cancels_the_sink():
    sink = _Sink(delay_ms=200)
    mouth = Mouth(_Voice(delay_ms=20), sink)

    async def scenario():
        mouth.open()
        await mouth.say("One.")
        await mouth.say("Two.")
        await asyncio.sleep(0.05)
        await mouth.hush()

    run(scenario())
    assert sink.cancelled == 1
