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
between chunks, and a cloud voice paid 1.5 s before its first sound for it.
`finish()` waits for everything queued to be heard; `hush()` throws it all
away and stops the sink mid-word, which is what barge-in will be made of.

**A sentence lost is a sentence lost.** A voice that fails on one sentence
(the network dropped, the model choked on a symbol) raises `PluginError`,
and the mouth records it, tells the caller through `failures`, and goes on
with the next sentence. Chunks of that sentence already handed to the sink
were heard; the ones still waiting are dropped rather than played as half a
sentence. The text was still printed, and a robot that skipped a sentence is
still a robot that answered.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from emet_sdk.plugin import PluginError
from emet_sdk.types import AudioSink

__all__ = ["Sentences", "Mouth", "SpokenSentence", "split_sentences", "speak_stream"]

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


@dataclass(frozen=True, slots=True)
class SpokenSentence:
    """What happened to one sentence: how long its audio was, and whether the
    voice managed it."""

    text: str
    audio_bytes: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _Job:
    text: str
    #: Bytes the voice produced for this sentence, whatever became of them.
    audio_bytes: int = 0
    error: str | None = None


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

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Start the two stages. Called once per reply."""
        self._sentences = asyncio.Queue()
        self._audio = asyncio.Queue()
        self._first_audio_reported = False
        self.spoken = []
        self._synth_task = asyncio.create_task(self._synthesise_all())
        self._play_task = asyncio.create_task(self._play_all())

    async def say(self, text: str) -> None:
        """Queue one sentence. Returns at once."""
        if self._sentences is None:
            raise RuntimeError("the mouth is not open")
        if text.strip():
            self._sentences.put_nowait(_Job(text=text.strip()))

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
        """Drop what is queued and stop the sink mid-word."""
        for task in (self._synth_task, self._play_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._synth_task, self._play_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        await self.sink.cancel()
        self._sentences = None
        self._audio = None
        self._synth_task = None
        self._play_task = None

    @property
    def failures(self) -> list[SpokenSentence]:
        return [s for s in self.spoken if not s.ok]

    # ---------------------------------------------------------------- stages

    async def _synthesise_all(self) -> None:
        assert self._sentences is not None and self._audio is not None
        while True:
            job = await self._sentences.get()
            if job is None:
                self._audio.put_nowait(None)
                return
            try:
                async for chunk in self.voice.speak(job.text):
                    if chunk:
                        job.audio_bytes += len(chunk)
                        self._audio.put_nowait((job, chunk))
            except PluginError as exc:
                job.error = str(exc)
                log.warning("voice: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a plugin that raises the wrong thing still loses one sentence
                job.error = f"{type(exc).__name__}: {exc}"
                log.warning("voice: %s", job.error)
            self._audio.put_nowait((job, None))

    async def _play_all(self) -> None:
        assert self._audio is not None
        while True:
            item = await self._audio.get()
            if item is None:
                return
            job, chunk = item
            if chunk is None:
                outcome = SpokenSentence(text=job.text, audio_bytes=job.audio_bytes, error=job.error)
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
                self._first_audio_reported = True
                self.on_first_audio()
            try:
                await self.sink.play(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the sink failed; say so, keep going
                job.error = f"playback failed: {type(exc).__name__}: {exc}"
                log.warning("speaker: %s", job.error)


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
