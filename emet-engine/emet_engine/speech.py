"""Speaking a reply as it is written: sentences out of a stream, audio out
of sentences, sound out of the audio, each stage running while the last one
is still busy.

A language model streams words. A voice takes a sentence. A speaker takes a
buffer. Nothing above this module wants to know that, so this is where the
three shapes meet: text deltas go in one end and the robot is heard at the
other, and the first sentence is playing while the model is still writing
the second. That overlap is the whole reason `LanguageModelPlugin.reply()`
streams and `VoicePlugin.speak()` takes one sentence rather than a
paragraph.

**Sentences, not words and not the whole reply.** A voice given one word at
a time produces choppy prosody, because a sentence's melody depends on
knowing where it ends. A voice given the whole reply cannot start until the
model has finished, which on the reference body is a two-second silence a
person hears as the robot not having understood. A sentence is the unit that
sounds right and arrives early, and `Sentences` finds them in a stream of
deltas by the plain rule: a full stop, a question mark or an exclamation
mark, followed by whitespace or the end, closes one. Abbreviations and
decimals are handled by the short list a spoken reply actually contains,
rather than by a parser; a wrong split costs a pause, never a word.

**Three stages, two queues.** `Mouth.say()` queues a sentence and returns at
once. A synthesis task takes sentences in order and asks the voice for each,
passing every chunk of audio on the moment it arrives; a playback task takes
those chunks in order and hands each to the sink. So the voice is
synthesising sentence two while sentence one plays, the first chunk of a
sentence is heard before its last has been made, and the engine loop that
called `say()` is back reading the language model. The sink is what makes
consecutive chunks one continuous sound: `emet_hal.audio.Speaker` keeps one
stream open across calls. 0.4 gathered each sentence into one buffer before
playing it, because the speaker then opened a stream per buffer and clicked
between chunks, and on the reference body a cloud voice paid 1762 ms before
its first sound where a local one paid 456 ms. `finish()` waits until
everything queued has reached the card, which is one block short of heard;
`hush()` throws it all away and stops the sink mid-word, which is what
barge-in will be made of.

**A voice that falls behind is a gap in a word.** Chunks play as they
arrive, so a voice slower than real time leaves the card with nothing and it
plays silence. That is inaudible to PortAudio, which was served on time, so
the mouth counts it instead: `starved` is the silence the sink filled
between a sentence's first chunk and its last, and `emet-talk --stats`
prints it.

**A sentence lost is a sentence lost.** A voice that fails on one sentence
(the network dropped, the model choked on a symbol) raises `PluginError`,
and the mouth records it, tells the caller through `failures`, and goes on
with the next sentence. Chunks of that sentence already handed to the sink
were heard; the ones still waiting are dropped rather than played as half a
sentence. The text was still printed, and a robot that skipped a sentence is
still a robot that answered.

**Only what a person would say aloud.** The prompt asks for plain speech
and nothing made a model obey it. The reference voice reads `**` as
"asterisk asterisk", `#` as "hash", an emoji by its Unicode name ("smiling
face with smiling eyes") and a URL letter by letter, nine seconds for one
sentence (thirty fixed strings through Piper's LJ Speech voice on
2026-11-27, in `releases/v0.5.7/evidence/B1/` outside this repository). So
`Mouth.say()` passes every sentence through `speakable()`, after the tag
lifter has taken the tags out: markdown markers and emoji are removed and a
URL is read as its host. A sentence the pass empties (an emoji on its own)
is never handed to the voice. A sentence with nothing to remove is passed
on exactly as written, so a filler such as "hm?" reaches the voice as the
chain wrote it. The person reads the model's own text and the conversation
keeps it; only the voice hears the cleaned one, and `SpokenSentence.cleaned`
says which sentences the pass changed. The idea of a markdown and emoji
pass in front of the voice is LiveKit Agents' (`filter_markdown` and
`filter_emoji`, Apache-2.0) and Pipecat's (`MarkdownTextFilter`,
BSD-2-Clause, which keeps numbered lists, as this pass does), both read in
their documentation and their licences read through GitHub's API (verified
2026-11-27); no code of either is used. See CITATIONS.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

from emet_sdk import intents as _intents
from emet_sdk.plugin import PluginError
from emet_sdk.types import AudioSink

__all__ = ["Sentences", "Mouth", "SpokenSentence", "speakable", "split_sentences", "speak_stream"]

log = logging.getLogger("emet_engine.speech")

#: Text that ends a sentence when followed by whitespace: the three end
#: marks, optionally with a closing quote or bracket after them.
_END = re.compile(r'[.!?]+["\')\]]?(?=\s)')

#: An ellipsis is a pause unless what follows starts a new sentence. In a
#: stream the next word may not have arrived yet, in which case the
#: decision waits.
_ELLIPSIS = re.compile(r"\.\.\.")
_NEW_SENTENCE = re.compile(r'\s+["\'(]?[A-Z0-9]')

#: Endings that look like a sentence's and are not. Titles and the
#: abbreviations a spoken reply is likely to contain; a wrong guess here
#: costs a pause at worst.
_NOT_AN_END = re.compile(
    r"(?:\b(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|e\.g|i\.e|no|inc|ltd|co)\.|\b[a-z]\.|\d\.)$",
    re.IGNORECASE,
)


def split_sentences(text: str) -> tuple[list[str], str]:
    """The complete sentences at the front of `text`, and the remainder.

    A sentence is complete when its end mark is followed by whitespace. The
    remainder is whatever follows the last complete sentence, which may be
    the start of the next one or nothing.
    """
    out: list[str] = []
    start = 0
    for m in _END.finditer(text):
        candidate = text[start : m.end()]
        if _NOT_AN_END.search(candidate.rstrip('"\')]')):
            continue
        if _ELLIPSIS.search(m.group()) and not _NEW_SENTENCE.match(text, m.end()):
            continue
        sentence = candidate.strip()
        if sentence:
            out.append(sentence)
        start = m.end()
    rest = text[start:]
    return out, rest.lstrip() if out else rest


class Sentences:
    """Sentences out of a stream of text deltas.

    `feed()` returns the sentences the delta completed, in order, and keeps
    the rest; `flush()` returns whatever is left when the stream ends, which
    a model that stops without a full stop always leaves.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        done, self._buffer = split_sentences(self._buffer)
        return done

    def flush(self) -> str | None:
        rest = " ".join(self._buffer.split())
        self._buffer = ""
        return rest or None

    @property
    def pending(self) -> str:
        return self._buffer


# ------------------------------------------------------------ speakable text

#: A markdown image, `![a red door](https://...)`, and a link,
#: `[the guide](https://...)`, each with an optional title after the URL.
#: Every part is bounded and none can hold a bracket, so a reply that is a
#: wall of brackets costs a scan and never a search from every one of them.
_IMAGE = re.compile(r'!\[([^\[\]\n]{0,300})\]\(\s*([^()\[\]\s]*)(?:\s+"[^"\n]{0,300}")?\s*\)')
_LINK = re.compile(r'\[([^\[\]\n]{0,300})\]\(\s*([^()\[\]\s]*)(?:\s+"[^"\n]{0,300}")?\s*\)')

#: A URL written out: a scheme, or `www.` at a word's start. It never ends on
#: the punctuation of the sentence around it, and an autolink's angle
#: brackets go with it.
_URL_BODY = r"[^\s<>\"'`\[\]]*[^\s<>\"'`\[\].,;:!?)]"
_URL = re.compile(rf"<?(?:\b(?:https?|ftp)://|(?<![\w@./-])www\.){_URL_BODY}>?", re.IGNORECASE)

#: Emphasis: `*a*`, `**a**`, `***a***`, the same with `_`, and `~~a~~`.
#: The marker hugs the words on both sides, so `2 * 3 * 4` is no emphasis,
#: and one glued to a word (`snake_case`, `5*3*2`) is part of it. The words
#: inside are bounded at 300 characters, longer than any emphasis a reply
#: holds, so a sentence of unmatched stars is never searched to its end
#: from every star.
_STARS = re.compile(r"(?<![\w*\\])(\*{1,3})(?=[^\s*])(.{1,300}?)(?<=[^\s*])\1(?![\w*])", re.DOTALL)
_LOW_LINES = re.compile(r"(?<![\w\\])(_{1,3})(?=[^\s_])(.{1,300}?)(?<=[^\s_])\1(?!\w)", re.DOTALL)
_STRIKE = re.compile(r"(?<!~)~~(?=\S)(.{1,300}?)(?<=\S)~~(?!~)", re.DOTALL)

#: A code fence with its language, and any backtick left after it: code is
#: read as its words and the ticks are never said.
_FENCE = re.compile(r"`{3,}[\w+.-]*")
_TICKS = re.compile(r"`+")

#: Markers that stand on their own, with whitespace (or nothing) on both
#: sides: a heading's hashes, a bullet, a rule. A bullet sign (U+2022,
#: U+2023, U+25E6), a rule and a heading's two to six hashes go wherever they
#: stand. A lone `*` or `#` is also a word a person says ("the * key",
#: "press # then 1", "6 * 7"), so it goes only where a bullet or a heading
#: would: at the start of the text or of a line, after a sentence's end or
#: a colon, or after another such marker (`* * *`). A sentence with one star
#: there is a list whose line breaks `Sentences.flush()` has joined, so its
#: other lone stars go too. A dash or a plus is taken only at a line's
#: start, as is a quote's `>`: inside a line a dash is punctuation the voice
#: passes over, and `2 + 2` and `5 > 3` are arithmetic.
_LONE = re.compile(r"(?<!\S)(?:#{1,6}|\*+|[\u2022\u2023\u25e6]|-{3,}|_{3,})(?!\S)")
_LINE_MARKS = re.compile(r"^[ \t]*(?:[-+>][ \t]+|>)", re.MULTILINE)
_AFTER_A_BREAK = frozenset(".!?:;")

#: A tag the model wrote with words inside its brackets,
#: `[I express.curiosity]`, which the tag lifter passes on as prose since a
#: tag holds no space. The brackets hold an intent's dotted name, so it is
#: no aside a person wrote, and it goes whole (0.5.7's DL6 found one in 147
#: replies of a local model). Bracketed prose with no intent in it stays.
_STRAY_TAG = re.compile(
    r"\[[^\[\]\n]{0,60}?(?<![\w.])(?:" + "|".join(sorted(_intents.ALL_INTENTS)) +
    r")\.[a-z_]+(?![\w.])[^\[\]\n]{0,60}\]"
)

#: Emoji. Unicode has no emoji property that `unicodedata` can read, so
#: these are the blocks emoji are drawn from: U+1F000 to U+1FAFF (the
#: pictographs, emoticons, transport, flags' regional letters and skin tones
#: among them), the miscellaneous symbols and dingbats (U+2600 to U+27BF),
#: and the few single emoji outside them (a watch, an hourglass, a star).
#: A sequence goes whole: the joiner (U+200D), the presentation selectors
#: (U+FE0E, U+FE0F), the keycap mark (U+20E3) and the tag letters of a
#: subdivision flag travel with it. A character the selector U+FE0F turns
#: into an emoji goes with its selector (the copyright sign as a picture);
#: the plain copyright sign stays, since the voice says "copyright". A keycap
#: digit keeps its digit, and so does a digit a stray selector follows
#: (`2.0` then U+FE0F), which is no picture.
_EMOJI_BASE = (
    "\U0001f000-\U0001faff\u2600-\u27bf\u231a\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa"
    "\u25aa\u25ab\u25b6\u25c0\u25fb-\u25fe\u2934\u2935\u2b05-\u2b07\u2b1b\u2b1c\u2b50\u2b55"
    "\u3030\u303d\u3297\u3299"
)
_EMOJI_JOINERS = "\u200d\ufe0e\ufe0f\u20e3\U000e0020-\U000e007f"
_KEYCAP_DIGIT = re.compile("([0-9])\ufe0f?\u20e3")
_EMOJI = re.compile(
    f"(?:[{_EMOJI_BASE}]|[#*]\ufe0f?\u20e3|[^\\s\ufe0f0-9]\ufe0f)[{_EMOJI_BASE}{_EMOJI_JOINERS}]*"
)
_SELECTORS = re.compile("[\ufe0e\ufe0f]")

#: Tidying after a removal: the space left before a mark, and a mark left
#: at the start.
_SPACE_BEFORE_MARK = re.compile(r"\s+([,.;:!?])")
_LEADING_MARK = re.compile(r"^[,;:]+\s*")


def _host(url: str) -> str:
    """A URL as it is said: its host, lower case, with no `www.`, no port and
    no user name. An empty string when it has no host."""
    url = url.strip("<>")
    if not re.match(r"(?i)[a-z][a-z0-9+.-]*://", url):
        url = "http://" + url
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _lone_marks(text: str) -> tuple[str, int]:
    """`text` without the markers that stand on their own where a bullet,
    a heading or a rule would, and how many went (the rule at `_LONE`)."""
    found = list(_LONE.finditer(text))
    if not found:
        return text, 0
    gone: list[re.Match[str]] = []
    listed = False
    for m in found:
        mark = m.group()
        if mark[0] not in "#*" or (mark[0] == "#" and len(mark) > 1):
            gone.append(m)
            continue
        before = text[: m.start()]
        kept = before.rstrip()
        placed = (
            not kept
            or "\n" in before[len(kept):]
            or kept[-1] in _AFTER_A_BREAK
            or bool(gone and gone[-1].end() == len(kept))
        )
        if placed:
            gone.append(m)
            listed = listed or mark[0] == "*"
    if listed:
        # A joined list: every lone star in it was a bullet.
        taken = {id(m) for m in gone}
        gone += [m for m in found if m.group()[0] == "*" and id(m) not in taken]
        gone.sort(key=lambda m: m.start())
    if not gone:
        return text, 0
    pieces: list[str] = []
    at = 0
    for m in gone:
        pieces.append(text[at : m.start()])
        at = m.end()
    pieces.append(text[at:])
    return "".join(pieces), len(gone)


def _linked(match: re.Match[str]) -> str:
    """A markdown link as it is said: its text, or its host when the text
    is empty or is itself a URL."""
    text, url = match.group(1).strip(), match.group(2)
    if not text or _URL.fullmatch(text):
        return _host(text or url)
    return text


def speakable(text: str) -> str:
    """The words of a sentence the voice should say, without the marks a
    reader sees.

    Markdown's markers go (emphasis, headings, bullets, code ticks, rules and
    a link's brackets, the link read as its text), emoji go, a tag written
    with words inside its brackets goes, and a URL is read as its host, so
    `https://www.example.com/a?b=1` is "example.com". A lone `*` or `#`
    between words stays, since "the * key" and "6 * 7" say it. A
    numbered list keeps its numbers, which a person reads out too. A bare
    domain and an email address are left as written: the voice reads them
    well. Text with nothing to remove comes back exactly as given, spaces
    and all. When something was removed the spaces are tidied, and when no
    letter or digit is left the result is the empty string, which
    `Mouth.say()` never hands to the voice.
    """
    removed = 0
    out = text
    out, n = _IMAGE.subn(lambda m: m.group(1).strip(), out)
    removed += n
    out, n = _LINK.subn(_linked, out)
    removed += n
    out, n = _URL.subn(lambda m: _host(m.group()), out)
    removed += n
    out, n = _FENCE.subn("", out)
    removed += n
    out, n = _TICKS.subn("", out)
    removed += n
    for _ in range(3):
        # Nested emphasis (`**bold *and* italic**`) comes off a layer a pass.
        found = 0
        for pattern in (_STARS, _LOW_LINES):
            out, n = pattern.subn(lambda m: m.group(2), out)
            found += n
        out, n = _STRIKE.subn(lambda m: m.group(1), out)
        found += n
        removed += found
        if not found:
            break
    out, n = _LINE_MARKS.subn("", out)
    removed += n
    out, n = _lone_marks(out)
    removed += n
    out, n = _STRAY_TAG.subn("", out)
    removed += n
    out, n = _KEYCAP_DIGIT.subn(lambda m: m.group(1), out)
    removed += n
    out, n = _EMOJI.subn("", out)
    removed += n
    out, n = _SELECTORS.subn("", out)
    removed += n
    if not removed:
        return text
    out = " ".join(out.split())
    out = _SPACE_BEFORE_MARK.sub(r"\1", out)
    out = _LEADING_MARK.sub("", out)
    return out if any(ch.isalnum() for ch in out) else ""


@dataclass(frozen=True, slots=True)
class SpokenSentence:
    """What happened to one sentence: how long its audio was, and whether the
    voice managed it.

    `from_reply` is False for words the body added to a reply, a filler for
    a tag or an explanation, which were heard and are not among the
    sentences the model wrote. `text` is what the voice was handed, after
    `speakable()`. `cleaned` is True when that differs from the sentence as
    written; a sentence the pass emptied is never handed to the voice, and
    is kept as `SpokenSentence(text="", audio_bytes=0, cleaned=True)` in its
    place in the order."""

    text: str
    audio_bytes: int
    error: str | None = None
    from_reply: bool = True
    cleaned: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _Job:
    text: str
    from_reply: bool = True
    #: Whether `speakable()` changed the sentence. An empty `text` with this
    #: set is a sentence the pass emptied, which the voice is never asked for.
    cleaned: bool = False
    #: Bytes the voice produced for this sentence, whatever became of them.
    audio_bytes: int = 0
    error: str | None = None
    #: The sink's count of silence-filled blocks when this sentence's first
    #: chunk was played. The difference at its last chunk is the silence a
    #: person heard inside it.
    padded_at: int | None = None


#: What the synthesis stage hands the playback stage: a chunk of a
#: sentence's audio, or None to say that sentence is complete.
_Chunk = tuple[_Job, bytes | None]


class Mouth:
    """The output side of a turn: sentences in, sound out, in order.

    `voice` is a started `VoicePlugin`; `sink` a started `AudioSink` opened
    at the voice's sample rate. `on_first_audio`, when given, is called once
    per `open()`, the moment the first chunk reaches the sink: the number a
    person waits for, measured where they hear it.
    """

    def __init__(
        self,
        voice: Any,
        sink: AudioSink,
        *,
        on_first_audio: Callable[[], None] | None = None,
        on_spoken: Callable[[SpokenSentence], None] | None = None,
    ) -> None:
        self.voice = voice
        self.sink = sink
        self.on_first_audio = on_first_audio
        self.on_spoken = on_spoken
        self._sentences: asyncio.Queue[_Job | None] | None = None
        self._audio: asyncio.Queue[_Chunk | None] | None = None
        self._synth_task: asyncio.Task | None = None
        self._play_task: asyncio.Task | None = None
        self._first_audio_reported = False
        #: Every sentence handed over since `open()`, with its outcome, in
        #: the order it was heard.
        self.spoken: list[SpokenSentence] = []
        #: Blocks of silence the sink played inside a sentence because the
        #: voice had not produced the next chunk yet. Zero when the voice
        #: keeps up with real time. Silence between sentences is not counted:
        #: nobody is speaking then.
        self.starved = 0

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Start the two stages. Called once per reply."""
        self._sentences = asyncio.Queue()
        self._audio = asyncio.Queue()
        self._first_audio_reported = False
        self.spoken = []
        self.starved = 0
        self._synth_task = asyncio.create_task(self._synthesise_all())
        self._play_task = asyncio.create_task(self._play_all())

    async def say(self, text: str, *, from_reply: bool = True) -> None:
        """Queue one sentence, as `speakable()` leaves it. Returns at once.

        A sentence the pass empties keeps its place in `spoken`, with no
        text and no audio, and the voice is never asked for it.
        """
        if self._sentences is None:
            raise RuntimeError("the mouth is not open")
        written = text.strip()
        if written:
            said = speakable(written)
            self._sentences.put_nowait(_Job(text=said, from_reply=from_reply, cleaned=said != written))

    async def finish(self) -> list[SpokenSentence]:
        """Wait until everything queued has been heard, and close."""
        if self._sentences is None:
            return list(self.spoken)
        self._sentences.put_nowait(None)
        if self._synth_task is not None:
            await self._synth_task
        if self._play_task is not None:
            await self._play_task
        self._sentences = None
        self._audio = None
        self._synth_task = None
        self._play_task = None
        return list(self.spoken)

    async def hush(self) -> None:
        """Drop what is queued and stop the sink mid-word.

        Barge-in, and only that. A mouth that has already finished speaking
        has nothing to interrupt, and cancelling the sink then would throw
        away the block `play()` leaves in hand, which is the end of the last
        word. `ListenSession.stop()` calls this on the way out.
        """
        running = self._synth_task is not None or self._play_task is not None
        for task in (self._synth_task, self._play_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._synth_task, self._play_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if running:
            await self.sink.cancel()
        self._sentences = None
        self._audio = None
        self._synth_task = None
        self._play_task = None

    @property
    def failures(self) -> list[SpokenSentence]:
        return [s for s in self.spoken if not s.ok]

    @property
    def is_open(self) -> bool:
        """Whether a reply is being spoken, which `say()` queues behind."""
        return self._sentences is not None

    def _padded(self) -> int:
        """Blocks the sink has filled with silence so far.

        Read defensively, like `dropped` on the source: only a sink that
        keeps one stream open has anything to pad, and a file has nothing.
        """
        return int(getattr(self.sink, "padded", 0) or 0)

    # ---------------------------------------------------------------- stages

    async def _synthesise_all(self) -> None:
        assert self._sentences is not None and self._audio is not None
        while True:
            job = await self._sentences.get()
            if job is None:
                self._audio.put_nowait(None)
                return
            if not job.text:
                # Emptied by `speakable()`: an empty text is never sent, and
                # the sentence still takes its place in the order.
                self._audio.put_nowait((job, None))
                continue
            try:
                async for chunk in self.voice.speak(job.text):
                    if chunk:
                        job.audio_bytes += len(chunk)
                        self._audio.put_nowait((job, chunk))
            except PluginError as exc:
                job.error = str(exc)
                # INFO: the console prints each lost sentence with its reason (L16).
                log.info("voice: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a plugin that raises the wrong thing still loses one sentence
                job.error = f"{type(exc).__name__}: {exc}"
                log.info("voice: %s", job.error)
            self._audio.put_nowait((job, None))

    async def _play_all(self) -> None:
        assert self._audio is not None
        while True:
            item = await self._audio.get()
            if item is None:
                return
            job, chunk = item
            if chunk is None:
                if job.padded_at is not None:
                    # Silence the card was given between this sentence's
                    # first chunk and its last: the voice fell behind real
                    # time and a person heard a gap inside a word. Silence
                    # after a sentence ends is not counted, because nobody is
                    # speaking then.
                    self.starved += max(0, self._padded() - job.padded_at)
                outcome = SpokenSentence(
                    text=job.text,
                    audio_bytes=job.audio_bytes,
                    error=job.error,
                    from_reply=job.from_reply,
                    cleaned=job.cleaned,
                )
                self.spoken.append(outcome)
                if self.on_spoken is not None:
                    self.on_spoken(outcome)
                continue
            if job.error is not None:
                # The voice gave up on this sentence, or the sink did, before
                # this chunk's turn came. What was heard stays heard; the
                # rest is dropped rather than played as half a sentence.
                continue
            if not self._first_audio_reported and self.on_first_audio is not None:
                # Before the sink, not after: `Speaker.play()` returns only
                # once the card has the audio, so reporting afterwards would
                # add the playback to the number that measures the wait
                # before it. A chunk the sink then refuses leaves this mark
                # early, which the session drops when nothing was spoken.
                self._first_audio_reported = True
                self.on_first_audio()
            if job.padded_at is None:
                # Taken before this sentence's first chunk goes over, so the
                # window covers the whole time somebody was listening to it
                # and none of the quiet that came before.
                job.padded_at = self._padded()
            try:
                await self.sink.play(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the sink failed; say so, keep going
                job.error = f"playback failed: {type(exc).__name__}: {exc}"
                # INFO: the console prints each lost sentence with its reason (L16).
                log.info("speaker: %s", job.error)


async def speak_stream(
    mouth: Mouth,
    deltas: AsyncIterator[str],
) -> list[SpokenSentence]:
    """Feed a stream of text deltas through a mouth, sentence by sentence.

    A convenience for callers that hold a plain text stream; the session
    does the same by hand so that it can yield the reply's events as they
    pass through.
    """
    sentences = Sentences()
    mouth.open()
    async for delta in deltas:
        for sentence in sentences.feed(delta):
            await mouth.say(sentence)
    rest = sentences.flush()
    if rest:
        await mouth.say(rest)
    return await mouth.finish()
