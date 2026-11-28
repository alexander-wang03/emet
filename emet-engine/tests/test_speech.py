"""Speaking a reply as it is written.

Four things are proved here without a model, a voice or a speaker: that
sentences are found in a stream of deltas where a person would end them,
that the mouth plays them in order while the next is still being made, that
a sentence the voice loses is one sentence lost and not the turn, and that
the voice is handed only what a person would say aloud while the
conversation keeps what the model wrote.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import wave
from pathlib import Path
from typing import Any

import pytest

from emet_sdk.plugin import PluginError, VoicePlugin
from emet_sdk.types import TextDelta, VoiceDescriptor
from emet_sdk.validate import load_yaml

from emet_engine.intent_tags import IntentTags
from emet_engine.speech import Mouth, Sentences, SpokenSentence, speakable, split_sentences, speak_stream


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


# ------------------------------------------------------- what the voice hears

#: The thirty fixed strings of BVS1's first stage, word for word, in its
#: order (`releases/v0.5.7/evidence/B1/bvs1-strings.json`, outside this
#: repository, written 2026-11-27 14:03 PST by the evidence stream). Escaped
#: so this file stays ASCII. The digest below is that file's, so a copy that
#: drifts from the evidence fails here.
BVS1_STRINGS: tuple[tuple[str, str, str], ...] = (
    ("md-bold", "That is **really** important.", "markdown"),
    ("md-italic", "I think it's *probably* fine.", "markdown"),
    ("md-heading", "## Today's plan\nWe start with the timer.", "markdown"),
    ("md-code", "Run `emet-talk --stats` to see the timings.", "markdown"),
    ("md-link", "See [the guide](https://example.com/guide) for more.", "markdown"),
    ("bullet-dash", "Three things:\n- eggs\n- milk\n- bread", "bullet"),
    ("bullet-star", "* First, wake me.\n* Then ask.", "bullet"),
    ("bullet-numbered", "1. Turn left.\n2. Stop.", "bullet"),
    ("emoji-end", "Good morning! \U0001f60a", "emoji"),
    ("emoji-mid", "I love that \U0001f389 idea.", "emoji"),
    ("emoji-zwj", "Your family \U0001f468\u200d\U0001f469\u200d\U0001f467 sounds lovely.", "emoji"),
    ("emoji-vs16", "Thanks \u2764\ufe0f so much.", "emoji"),
    ("link-raw", "The page is https://www.example.com/a/b?c=1 today.", "link"),
    ("link-domain", "Look at example.org for the details.", "link"),
    ("link-email", "Write to someone@example.com today.", "link"),
    ("time-12h", "The timer ends at 3:45 PM.", "time"),
    ("time-24h", "We leave at 14:30.", "time"),
    ("time-duration", "That took 1h 20m.", "time"),
    ("unit-temp", "It is 21\xb0C outside, about 70\xb0F.", "unit"),
    ("unit-distance", "The park is 2.5 km away, or 1.6 mi.", "unit"),
    ("unit-mass", "Add 250 g of flour and 5 ml of milk.", "unit"),
    ("money-dollar", "It costs $4.99.", "money"),
    ("money-million", "They raised $1.2M last year.", "money"),
    ("money-euro-pound", "That is \u20ac20, or \xa317.50.", "money"),
    ("abbrev-titles", "Dr. Smith lives on Elm St. near the U.S. border.", "abbreviation"),
    ("abbrev-latin", "Bring hats, scarves, etc., e.g. the red one.", "abbreviation"),
    ("number-large", "There are 1,234,567 stars in that patch.", "number"),
    ("number-ordinal-percent", "You came 2nd with 87.5% of the vote.", "number"),
    ("number-date", "It runs from 2026-11-27 to 12/03.", "number"),
    ("mixed-reply", "**Sure!** Here's a quick list:\n\n1. **Eggs** \U0001f95a\n2. Milk, about $3.", "mixed"),
)
BVS1_SHA256 = "6dca24d95de352e5c40b82536e601fa1e4e0e81f6a481b0ac47a6c75a451d487"

#: What the voice is asked for, string by string, where the pass changes
#: something or the splitter does. An emptied sentence is absent here: the
#: voice is never asked for it. The list's dashes stay, because
#: `Sentences.flush()` has joined its lines and a dash inside a line is
#: punctuation (the evidence heard the voice pass over it); its numbered
#: sibling keeps its numbers.
HEARD: dict[str, list[str]] = {
    "md-bold": ["That is really important."],
    "md-italic": ["I think it's probably fine."],
    "md-heading": ["Today's plan We start with the timer."],
    "md-code": ["Run emet-talk --stats to see the timings."],
    "md-link": ["See the guide for more."],
    "bullet-dash": ["Three things: - eggs - milk - bread"],
    "bullet-star": ["First, wake me.", "Then ask."],
    "bullet-numbered": ["1. Turn left.", "2. Stop."],
    "emoji-end": ["Good morning!"],
    "emoji-mid": ["I love that idea."],
    "emoji-zwj": ["Your family sounds lovely."],
    "emoji-vs16": ["Thanks so much."],
    "link-raw": ["The page is example.com today."],
    "mixed-reply": ["Sure! Here's a quick list: 1. Eggs 2. Milk, about $3."],
}

#: The strings the pass changes, of the sixteen with a marker, an emoji or a
#: link. `bullet-dash` and `bullet-numbered` reach the voice as written.
CHANGED = {
    "md-bold", "md-italic", "md-heading", "md-code", "md-link", "bullet-star", "emoji-end",
    "emoji-mid", "emoji-zwj", "emoji-vs16", "link-raw", "mixed-reply",
}


def through_the_engine(text: str) -> tuple[list[str], list[SpokenSentence]]:
    """One reply, as a single delta, down the path `answer_aloud()` takes:
    the tag lifter, the splitter, then the mouth. Returns what the voice was
    asked for and what the mouth reports."""
    voice = _Voice()
    mouth = Mouth(voice, _Sink())

    async def scenario():
        tags, sentences = IntentTags(), Sentences()
        mouth.open()
        for piece in tags.pieces(text):
            if isinstance(piece, str):
                for sentence in sentences.feed(piece):
                    await mouth.say(sentence)
        for sentence in sentences.feed(tags.flush()):
            await mouth.say(sentence)
        rest = sentences.flush()
        if rest:
            await mouth.say(rest)
        return await mouth.finish()

    spoken = run(scenario())
    return voice.asked, spoken


def test_the_thirty_strings_are_the_evidence_streams_word_for_word():
    as_written = [{"id": i, "text": t, "kind": k} for i, t, k in BVS1_STRINGS]
    file_bytes = (json.dumps(as_written, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    assert hashlib.sha256(file_bytes).hexdigest() == BVS1_SHA256
    assert len(BVS1_STRINGS) == 30 and set(HEARD) | CHANGED <= {i for i, _, _ in BVS1_STRINGS}


@pytest.mark.parametrize(("key", "text"), [(i, t) for i, t, _ in BVS1_STRINGS], ids=[i for i, _, _ in BVS1_STRINGS])
def test_each_of_the_thirty_strings_reaches_the_voice_as_a_person_would_say_it(key, text):
    asked, spoken = through_the_engine(text)
    assert asked == HEARD.get(key, [text])
    assert any(s.cleaned for s in spoken) == (key in CHANGED)
    for s in spoken:
        assert s.ok and (s.audio_bytes > 0) == bool(s.text), s
        assert "*" not in s.text and "`" not in s.text and "#" not in s.text and "://" not in s.text


def test_a_string_with_nothing_to_remove_comes_back_as_the_same_object():
    """The fourteen strings with no marker, emoji or link, and the voice
    rung's fillers, are passed on exactly as written."""
    for key, text, _ in BVS1_STRINGS:
        if key not in HEARD:
            assert speakable(text) is text, key


@pytest.mark.parametrize(
    ("written", "said"),
    [
        # emphasis, nested, struck through, and the marks that are no emphasis
        ("A ***very*** big **bold *and italic* step**.", "A very big bold and italic step."),
        ("This is _quite_ __plain__ now.", "This is quite plain now."),
        ("~~Old~~ new plan.", "Old new plan."),
        ("Call snake_case_name and 5*3*2 by name.", "Call snake_case_name and 5*3*2 by name."),
        # headings, quotes, bullets and rules, at a line's start and joined
        ("### Next steps", "Next steps"),
        ("Done. ## Next", "Done. Next"),
        ("> Quoted words.", "Quoted words."),
        ("Three things: * eggs * milk", "Three things: eggs milk"),
        ("Steps:\n* one\n* two", "Steps: one two"),
        ("Intro text ## Section two", "Intro text Section two"),
        ("# Title", "Title"),
        ("\u2022 one \u2022 two", "one two"),
        ("Lists:\n- one\n+ two", "Lists: one two"),
        ("Above\n---\nbelow.", "Above below."),
        # code
        ("```python print(1) ```", "print(1)"),
        ("Press ``ctrl`` now.", "Press ctrl now."),
        # links, images and URLs
        ("See [https://example.com/x](https://example.com/x) now.", "See example.com now."),
        ("See [](https://www.example.com) now.", "See example.com now."),
        ('Read [the notes](https://example.com/n "Notes") first.', "Read the notes first."),
        ("![a red door](https://example.com/door.png)", "a red door"),
        ("Go to <https://example.com/start>.", "Go to example.com."),
        ("Try https://user:pw@Example.COM:8080/x?y=1, then rest.", "Try example.com, then rest."),
        ("Visit www.example.com/a, then rest.", "Visit example.com, then rest."),
        ("The docs (https://example.com/docs) say so.", "The docs (example.com) say so."),
        ("Open ftp://files.example.net/a.txt please.", "Open files.example.net please."),
        # emoji: a flag, a skin tone, a keycap, a picture made by its selector
        ("Bonjour \U0001f1eb\U0001f1f7 Paris.", "Bonjour Paris."),
        ("Nice \U0001f44d\U0001f3fd work.", "Nice work."),
        ("\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f Go.", "Go."),
        ("Step 1\ufe0f\u20e3 is first.", "Step 1 is first."),
        ("Press #\ufe0f\u20e3 to end.", "Press to end."),
        ("Made \u00a9\ufe0f 2026.", "Made 2026."),
        ("Done \u2705, and \u2b50 for you!", "Done, and for you!"),
        ("It is \u231a time \u23f3.", "It is time."),
        ("\U0001f60a, hello there.", "hello there."),
        # a stray selector after a digit takes the selector, and the digit stays
        ("Version 2.0️ is out.", "Version 2.0 is out."),
        # a tag written with words in its brackets (0.5.7's DL6, L51)
        ("[I express.curiosity] That is a good question.", "That is a good question."),
        ("[please move.approach now] Coming.", "Coming."),
    ],
)
def test_markers_emoji_and_urls_come_out(written, said):
    assert speakable(written) == said


@pytest.mark.parametrize(
    "text",
    [
        "2 plus 2 is 4.",
        "2 + 2 is 4, and 5 > 3.",
        "It was late - very late - and cold.",
        "Made \u00a9 2026, and it is the \u2122 of nobody.",
        "Two  spaces and a\nline break stay.",
        "Look at example.org, or write to someone@example.com.",
        "C# and F# are languages, and #1 is first.",
        "The Greek letter Ω and the arrow \u2192 stay.",
        "...",
        "hm?",
        "Wait... then go.",
        "[laughs] is lifted earlier, so a bracket alone is left.",
        "Prices: $4.99, \u20ac20, \xa317.50, 21\xb0C.",
        # a lone star or hash between words is said, as at 0.5.6
        "What is 6 * 7? It is 42.",
        "2 * 3 * 4 is 24.",
        "Price: $5 * 2.",
        "Press # then 1.",
        "Use the * key.",
        # A reply wrapped whole in brackets (0.5.7's DL6, L51) is left: it is
        # no tag, and the reference voice says nothing for a bracket.
        "[Sure, I can set a timer for ten minutes.]",
    ],
)
def test_text_with_nothing_to_remove_is_left_exactly_as_written(text):
    assert speakable(text) is text


def test_the_local_models_two_bracket_shapes_reach_the_voice_as_ruled():
    """DL6's two shapes (0.5.7, L51), down `answer_aloud()`'s path: a tag
    with a word inside its brackets is never said, and a reply wrapped in
    brackets is said as written."""
    asked, spoken = through_the_engine("[I express.curiosity] That is a good question.")
    assert asked == ["That is a good question."] and [s.cleaned for s in spoken] == [True]
    asked, spoken = through_the_engine("[Sure, I can set a timer for ten minutes.]")
    assert asked == ["[Sure, I can set a timer for ten minutes.]"] and [s.cleaned for s in spoken] == [False]


def test_a_sentence_the_sink_loses_is_logged_at_info_since_the_console_prints_it(caplog):
    """L16: the programs print each lost sentence with its reason, so the
    mouth logs it at INFO and logging's last resort, which prints WARNING
    and above, does not print it a second time."""

    class _Gone(_Sink):
        async def play(self, pcm: bytes) -> None:
            raise OSError("the card is gone")

    mouth = Mouth(_Voice(), _Gone())

    async def scenario():
        mouth.open()
        await mouth.say("Hello there.")
        return await mouth.finish()

    with caplog.at_level(logging.DEBUG, logger="emet_engine.speech"):
        (spoken,) = run(scenario())
    assert spoken.error == "playback failed: OSError: the card is gone"
    records = [r for r in caplog.records if r.name == "emet_engine.speech"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "speaker: playback failed: OSError: the card is gone")
    ]


@pytest.mark.parametrize(
    "written",
    ["\U0001f60a", "\U0001f60a\U0001f389.", "**!**", "---", "* * *", "[](https://)", "\u2764\ufe0f \u2b50"],
)
def test_a_sentence_with_nothing_left_to_say_is_emptied(written):
    assert speakable(written) == ""


@pytest.mark.parametrize("written", ["[" * 3000 + "](" * 3000, "*a " * 6000, "~~a " * 4000])
def test_a_wall_of_unmatched_markers_is_read_in_well_under_a_second(written):
    """The pass runs on the event loop for every sentence. Unbounded, the
    link and emphasis patterns searched from every bracket or star to the
    end of the text: the first case took 2.7 s on the laptop
    (2026-11-27), and it takes milliseconds bounded."""
    import time

    start = time.perf_counter()
    speakable(written)
    assert time.perf_counter() - start < 1.0


def test_an_emptied_sentence_is_never_sent_and_keeps_its_place():
    """`Good morning! [smile emoji]` is two sentences to the splitter. The
    second is empty after the pass: the voice is never asked for it, and the
    mouth reports it, in order, with no audio and `cleaned` set."""
    voice, sink = _Voice(), _Sink()
    heard: list[SpokenSentence] = []
    firsts: list[str] = []
    mouth = Mouth(voice, sink, on_spoken=heard.append, on_first_audio=lambda: firsts.append("first"))

    async def scenario():
        mouth.open()
        await mouth.say("\U0001f60a")
        await mouth.say("Good **morning**!")
        await mouth.say("\U0001f389", from_reply=False)
        await mouth.say("Bye.")
        return await mouth.finish()

    spoken = run(scenario())
    assert voice.asked == ["Good morning!", "Bye."]
    assert [(s.text, s.cleaned, s.from_reply) for s in spoken] == [
        ("", True, True),
        ("Good morning!", True, True),
        ("", True, False),
        ("Bye.", False, True),
    ]
    assert spoken[0] == SpokenSentence(text="", audio_bytes=0, cleaned=True)
    assert all(s.ok for s in spoken) and spoken[2].audio_bytes == 0
    assert heard == spoken, "the callback hears every sentence, an emptied one included"
    assert mouth.failures == [] and firsts == ["first"]


def test_a_reply_of_emoji_alone_reaches_no_voice_and_no_sink():
    voice, sink = _Voice(), _Sink()
    firsts: list[str] = []
    mouth = Mouth(voice, sink, on_first_audio=lambda: firsts.append("first"))

    async def scenario():
        mouth.open()
        await mouth.say("\U0001f60a \U0001f389")
        return await mouth.finish()

    (spoken,) = run(scenario())
    assert voice.asked == [] and sink.played == [] and firsts == []
    assert spoken == SpokenSentence(text="", audio_bytes=0, cleaned=True)


def test_a_voice_rungs_own_words_and_the_engines_lines_pass_through_untouched():
    """The survey's rule for the voice rung: its own words ("hm?") are never
    changed. Every filler in the shipped chains and every line the engine
    can say has nothing to remove."""
    from emet_sdk.resolve import load_chains

    from emet_engine.prompting import DEFAULT_LINES

    words = [line for line in DEFAULT_LINES.values() if line]
    for chain in load_chains().values():
        rung = getattr(chain, "voice_rung", None)
        fillers = rung.params.get("filler") if rung is not None else None
        words += [fillers] if isinstance(fillers, str) else list(fillers or [])
    assert len(words) > 10
    for said in words:
        assert speakable(said) is said, said


# ------------------------------------------- the history keeps the model's text

EXAMPLES = Path(__file__).resolve().parents[2] / "emet-sdk" / "examples"


def _talking(tmp_path: Path, reply: str):
    """The bodiless body on the mocks, its ears on a second of quiet and its
    mouth on the null sink, with the mock model scripted to `reply`."""
    from emet_engine.session import ListenSession

    wav = tmp_path / "quiet.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(32000))
    manifest = copy.deepcopy(load_yaml(EXAMPLES / "bodiless.yaml"))
    manifest["audio"]["input"] = {"source": "wav", "device": "file", "params": {"path": str(wav)}, "aec": "hardware"}
    manifest["audio"]["output"] = {"sink": "null", "device": "none"}
    manifest["audio"]["wake"] = {"engine": "mock", "params": {}}
    manifest["models"] = {
        "stt": {"provider": "mock"},
        "chat": {"provider": "mock", "params": {"reply": reply}},
        "tts": {"provider": "mock"},
    }
    soul = {"identity": {"name": "Emet", "wake_word": "hey emet"}}
    return ListenSession(manifest, soul, transcribe=True, reply=True, speak=True)


def test_the_conversation_keeps_the_models_text_and_only_the_voice_hears_it_cleaned(tmp_path):
    written = "**Sure!** See https://www.example.com/a?b=1 today. \U0001f60a"
    session = _talking(tmp_path, written)

    async def scenario():
        async with session:
            events = [e async for e in session.answer_aloud("hello")]
            return events, list(session.conversation.messages), list(session._tts.said), session.last_spoken

    events, messages, said, spoken = run(scenario())
    printed = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert printed == written, "what the person reads is what the model wrote"
    assert (messages[-1].role, messages[-1].content) == ("assistant", written)
    assert said == ["Sure! See example.com today."], "the emoji alone was never sent"
    assert [(s.text, s.cleaned, s.from_reply) for s in spoken] == [
        ("Sure! See example.com today.", True, True),
        ("", True, True),
    ]
    assert spoken[0].audio_bytes > 0 and spoken[1].audio_bytes == 0
