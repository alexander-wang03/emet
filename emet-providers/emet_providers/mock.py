"""A speech recogniser, a language model and a voice that pretend.

`MockTranscriber` reads words out of the bytes it is given. A frame that
begins with printable text, followed by zeros, is taken to be somebody saying
that text. It is the same trick `emet_hal.mock.MockWake` plays with the wake
phrase, and it exists for the same reason: the whole loop, wake to transcript,
runs on a laptop with no microphone, no network and no key, and a test writes
what it wants said into the audio and reads it back out.

It streams. Each frame that adds words produces a partial carrying everything
heard so far in the utterance, and `finish()` returns the same words as the
final. Silence adds nothing. So the shape of the real thing (frames in, a
growing partial, one final) is exercised end to end before any provider
exists, and the engine above cannot tell this from a vendor except by how
little it costs.

Params, all optional:

    transcript      str. Say this instead of reading the audio: one word per
                    frame as partials, then the whole line as the final. For
                    a live microphone or a replay of a real recording, whose
                    bytes spell nothing on purpose and the odd short word by
                    accident. It says its line after a false wake too, since
                    it hears no speech to wait for; the engine reports the
                    turn as a false wake beside it, and a real provider
                    returns an empty final there.
    fail_on_start   bool. Pretend the service is unreachable. `start()`
                    completes and `describe()` reports unhealthy, which is
                    how a real provider reports a dead network.
    require_key     bool. Insist that the environment variable named by
                    `key_env` is set, and report unhealthy naming it when it
                    is not. The BYOK failure path, without a vendor.
    sample_rate     int. Report this rate instead of the one given, so a test
                    can watch the engine refuse a transcriber that would hear
                    speech at the wrong speed.

`MockLanguageModel` answers without a model. Given nothing, it repeats the
last thing the person said; given `params.reply`, it says that line. Either
way it streams one word per delta, so a caller that speaks as it reads has
something to read. It records every prompt it was shown, which is how a test
checks what the engine actually told the model.

Params, all optional:

    reply           str. Say this, whatever was asked.
    call_tool       {"name": ..., "arguments": {...}}. When the prompt offers
                    tools and the last message is the person's, ask for this
                    tool instead of answering, and stop with `tool`. Given a
                    tool result next, say what the tool said. The tool round
                    trip, without a vendor.
    fail_on_start   bool. Pretend the service is unreachable.
    require_key     bool. Insist on the environment variable `key_env`
                    names.

`MockVoice` speaks without a voice. The words it is given become the audio:
one chunk per word, each chunk the word's bytes followed by zeros, which is
the transcriber's trick run backwards. `read_back()` turns that audio into
the words again, so a test that records the robot through the `wav` sink
can assert on what was said. It streams a chunk per word, so the engine's
sentence pipeline is exercised end to end with no model and no network.

Params, all optional:

    sample_rate     int. Report this rate, default 16000; the engine opens
                    the sink at whatever a voice reports.
    latency_ms      int. Wait this long before the first chunk of each
                    sentence, the way a real voice would.
    fail_on         str. A sentence containing this word raises after its
                    first chunk, which is how a test watches the engine
                    lose one sentence and keep the next.
    fail_on_start   bool. Pretend the model is missing.
    require_key     bool. Insist on the environment variable `key_env`
                    names, the way a cloud voice would.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, AsyncIterator, Mapping

from emet_sdk.plugin import LanguageModelPlugin, PluginError, TranscriberPlugin, VoicePlugin
from emet_sdk.types import (
    AudioFormat,
    Health,
    LanguageModelDescriptor,
    Prompt,
    ReplyDone,
    ReplyEvent,
    TextDelta,
    ToolCall,
    Transcript,
    TranscriberDescriptor,
    VoiceDescriptor,
)

__all__ = ["MockTranscriber", "MockLanguageModel", "MockVoice", "spelled", "read_back", "MIN_CHARS"]

log = logging.getLogger("emet_providers.mock")

#: Shortest run of text a frame must begin with to count as words. Real audio
#: is int16, so a printable byte followed by a zero byte is an ordinary small
#: sample; three printable bytes in a row before a zero is rare enough.
MIN_CHARS = 3


def spelled(frame: bytes) -> str:
    """The words a frame spells, or an empty string.

    Text is the run of bytes the frame starts with, up to the first zero byte.
    Anything shorter than `MIN_CHARS`, or holding a byte outside printable
    ASCII, is audio rather than words.
    """
    head, _, _ = frame.partition(b"\x00")
    if len(head) < MIN_CHARS or any(b < 0x20 or b > 0x7E for b in head):
        return ""
    return head.decode("ascii").strip()


def read_back(pcm: bytes) -> str:
    """The words the mock voice wrote into `pcm`, in order.

    The voice writes each word and then at least two zero bytes, so a word
    is a run of printable bytes between runs of zeros, whatever its length:
    "is" and "it" are words here, where `spelled()` would take them for
    audio, because this reads the mock's own output and nothing else. Works
    whatever the chunk size, so a wav the sink wrote can be read back
    without knowing how it was cut.
    """
    words: list[str] = []
    for run in re.split(rb"\x00{2,}", pcm):
        run = run.strip(b"\x00")
        if run and all(0x20 <= b <= 0x7E for b in run):
            words.append(run.decode("ascii").strip())
    return " ".join(w for w in words if w)


class MockTranscriber(TranscriberPlugin):
    """Speech recognition with no speech, no recognition and no network."""

    provider = "mock"

    def __init__(self, config: Mapping[str, Any], fmt: AudioFormat) -> None:
        super().__init__(config, fmt)
        #: Frames fed, across every utterance. Evidence the engine is feeding.
        self.frames = 0
        #: Utterances closed with `finish()`.
        self.finished = 0
        self._words: list[str] = []
        self._started = False
        self._fault: str | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.params.get("fail_on_start"):
            self._fail(str(self.params.get("fault_detail") or "simulated: the service is unreachable"))
            return
        if self.params.get("require_key"):
            if not self.key_env:
                self._fail("this provider needs a key and the soul names no key_env")
                return
            if not os.environ.get(self.key_env):
                self._fail(f"no key: the environment variable {self.key_env} is not set")
                return
        self._started = True

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.warning("mock stt: %s", reason)

    async def shutdown(self) -> None:
        self._started = False
        self._words = []

    # ------------------------------------------------------------ reporting

    def describe(self) -> TranscriberDescriptor:
        return TranscriberDescriptor(
            provider=self.provider,
            model=self.model,
            streaming=True,
            sample_rate=int(self.params.get("sample_rate") or self.format.sample_rate),
            healthy=self._started,
        )

    def health(self) -> Health:
        if self._fault:
            return Health(ok=False, detail=self._fault, faults=("start_failed",))
        return Health()

    # ---------------------------------------------------------- recognition

    @property
    def _script(self) -> list[str]:
        scripted = self.params.get("transcript")
        return str(scripted).split() if scripted is not None else []

    async def feed(self, frame: bytes) -> Transcript | None:
        self.frames += 1
        if not self._started:
            return None
        script = self._script
        if script:
            # One word a frame, so the partials grow the way a vendor's do.
            if len(self._words) >= len(script):
                return None
            self._words.append(script[len(self._words)])
        else:
            words = spelled(frame)
            if not words:
                return None
            self._words.append(words)
        return Transcript(text=" ".join(self._words), final=False, confidence=0.5)

    async def finish(self) -> Transcript:
        self.finished += 1
        script = self._script
        text = " ".join(script) if script else " ".join(self._words)
        self._words = []
        return Transcript(text=text, final=True, confidence=1.0)


def _words(text: str) -> list[str]:
    return text.split()


class MockLanguageModel(LanguageModelPlugin):
    """A language model with no model in it."""

    provider = "mock"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        #: Every prompt this instance was asked to answer, in order.
        self.prompts: list[Prompt] = []
        self._started = False
        self._fault: str | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.params.get("fail_on_start"):
            self._fail(str(self.params.get("fault_detail") or "simulated: the service is unreachable"))
            return
        if self.params.get("require_key"):
            if not self.key_env:
                self._fail("this provider needs a key and the soul names no key_env")
                return
            if not os.environ.get(self.key_env):
                self._fail(f"no key: the environment variable {self.key_env} is not set")
                return
        self._started = True

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.warning("mock llm: %s", reason)

    async def shutdown(self) -> None:
        self._started = False

    # ------------------------------------------------------------ reporting

    def describe(self) -> LanguageModelDescriptor:
        return LanguageModelDescriptor(
            provider=self.provider,
            model=self.model,
            streaming=True,
            tools=True,
            healthy=self._started,
        )

    def health(self) -> Health:
        if self._fault:
            fault = "no_key" if "no key" in self._fault else "start_failed"
            return Health(ok=False, detail=self._fault, faults=(fault,))
        return Health()

    # ---------------------------------------------------------------- reply

    async def reply(self, prompt: Prompt) -> AsyncIterator[ReplyEvent]:
        self.prompts.append(prompt)
        if not self._started:
            yield ReplyDone(text="", stop_reason="error", error="the mock language model was not started")
            return

        last = prompt.messages[-1] if prompt.messages else None
        input_tokens = len(_words(prompt.system)) + sum(len(_words(m.content)) for m in prompt.messages)

        wanted = self.params.get("call_tool")
        if wanted and prompt.tools and last is not None and last.role == "user":
            call = ToolCall(
                id=f"call_{len(self.prompts)}",
                name=str((wanted or {}).get("name") or prompt.tools[0].name),
                arguments=dict((wanted or {}).get("arguments") or {}),
            )
            yield call
            yield ReplyDone(text="", stop_reason="tool", tool_calls=(call,), model="mock", input_tokens=input_tokens, output_tokens=0)
            return

        if last is not None and last.role == "tool":
            text = f"The tool said: {last.content}"
        elif self.params.get("reply") is not None:
            text = str(self.params["reply"])
        else:
            heard = last.content if last is not None else ""
            text = f"You said: {heard}" if heard else "You said nothing."

        for i, word in enumerate(_words(text)):
            yield TextDelta(word if i == 0 else f" {word}")
        yield ReplyDone(
            text=" ".join(_words(text)),
            stop_reason="end",
            model="mock",
            input_tokens=input_tokens,
            output_tokens=len(_words(text)),
        )


class MockVoice(VoicePlugin):
    """A voice with no voice in it: the words become the audio."""

    provider = "mock"

    #: Bytes a word's chunk is padded to. Short, so a sentence is small.
    CHUNK_BYTES = 64

    def __init__(self, config: Mapping[str, Any], voice: Mapping[str, Any] | None = None) -> None:
        super().__init__(config, voice)
        #: Every sentence this instance was asked to say, in order.
        self.said: list[str] = []
        self._started = False
        self._fault: str | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.params.get("fail_on_start"):
            self._fail(str(self.params.get("fault_detail") or "simulated: the voice model is missing"))
            return
        if self.params.get("require_key"):
            if not self.key_env:
                self._fail("this voice needs a key and the soul names no key_env")
                return
            if not os.environ.get(self.key_env):
                self._fail(f"no key: the environment variable {self.key_env} is not set")
                return
        self._started = True

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.warning("mock tts: %s", reason)

    async def shutdown(self) -> None:
        self._started = False

    # ------------------------------------------------------------ reporting

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(
            provider=self.provider,
            model=self.model,
            sample_rate=int(self.params.get("sample_rate") or 16000),
            streaming=True,
            healthy=self._started,
        )

    def health(self) -> Health:
        if self._fault:
            fault = "no_key" if "no key" in self._fault else "start_failed"
            return Health(ok=False, detail=self._fault, faults=(fault,))
        return Health()

    # ---------------------------------------------------------------- speak

    def _chunk(self, word: str) -> bytes:
        payload = word.encode("ascii", "replace")
        size = max(self.CHUNK_BYTES, len(payload) + 2)
        size += size % 2
        return payload + bytes(size - len(payload))

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        words = text.split()
        if not words:
            return
        if not self._started:
            raise PluginError("the mock voice was not started")
        self.said.append(" ".join(words))
        latency = float(self.params.get("latency_ms") or 0)
        if latency:
            await asyncio.sleep(latency / 1000.0)
        poison = self.params.get("fail_on")
        for i, word in enumerate(words):
            yield self._chunk(word)
            if poison and poison in word:
                raise PluginError(f"simulated: the voice failed on {word!r}")
