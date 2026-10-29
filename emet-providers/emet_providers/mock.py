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
    cassette        path to a cassette file, or a directory of them
                    (`emet_providers.cassette`; the format is
                    `tools/bank/FORMAT.md`, "Cassettes"). Answer with what a
                    real transcriber said for exactly these bytes: a recorded
                    partial on the `feed()` call whose bytes so far match it,
                    and on `finish()` the recorded final when every byte of
                    the utterance matches a turn. Anything else is a miss: an
                    empty final whose `error` reads "cassette miss", never an
                    exception. A cassette that cannot be read leaves `start()`
                    unhealthy with the reason, as `fail_on_start` does. It
                    replaces reading the audio, so it excludes `transcript`.
    cassette_request
                    mapping, the request the run stands for (`provider`,
                    `model`, `sample_rate`, `query`, as a cassette's turns
                    record it). Only turns recorded for this request are
                    candidates; without it, every turn is.
    record_turns    path to a directory. Write each utterance's bytes,
                    exactly as fed, to `turn-0001.raw` and on, and one line a
                    turn to `turns.jsonl` when it finishes: its number, bytes,
                    the size of every feed, the sha256, "hit", "miss" or null
                    without a cassette, the final and the number of partials.
                    A directory that already holds a `turns.jsonl` is
                    refused, so two runs never share one.

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
import hashlib
import json
import logging
import os
import re
from pathlib import Path
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

from emet_providers import cassette as _cassette

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
        #: The last failure writing `record_turns`, for diagnostics. The turn
        #: itself still ends normally: losing a record is not the person's
        #: problem.
        self.record_error: str | None = None
        self._words: list[str] = []
        self._started = False
        self._fault: str | None = None
        self._player: _cassette.Player | None = None
        self._turns_dir: Path | None = None
        self._turn = 0
        self._fed = bytearray()
        self._feed_sizes: list[int] = []
        self._partials = 0

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
        reason = self._open_cassette() or self._open_turns_dir()
        if reason:
            self._fail(reason)
            return
        self._started = True

    def _open_cassette(self) -> str | None:
        """Load `params.cassette`. Returns the reason it cannot be used."""
        path = self.params.get("cassette")
        request = self.params.get("cassette_request")
        if not path:
            if request is not None:
                return "cassette_request names the request a cassette was recorded for, and no cassette is given"
            return None
        if self.params.get("transcript") is not None:
            return "cassette and transcript exclude each other: one answers from recorded bytes, the other from a script"
        if request is not None:
            if not isinstance(request, Mapping):
                return f"cassette_request is a mapping, not {type(request).__name__}"
            rate = request.get("sample_rate")
            if rate is not None and str(rate) != str(self.format.sample_rate):
                return (
                    f"cassette_request is for {rate} Hz and the audio is {self.format.sample_rate} Hz, "
                    f"so no turn could match"
                )
        try:
            player = _cassette.Player(_cassette.load(path), request)
        except _cassette.CassetteError as exc:
            return f"cassette: {exc}"
        if not player.turns:
            log.warning("mock stt: no turn in %s was recorded for this request; every turn will miss", path)
        self._player = player
        return None

    def _open_turns_dir(self) -> str | None:
        """Make `params.record_turns`. Returns the reason it cannot be used."""
        path = self.params.get("record_turns")
        if not path:
            return None
        directory = Path(str(path)).expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"record_turns: cannot make {directory}: {exc.strerror or exc}"
        if (directory / "turns.jsonl").exists():
            return f"record_turns: {directory} already holds a run's turns.jsonl; give each run a directory of its own"
        self._turns_dir = directory
        return None

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.warning("mock stt: %s", reason)

    async def shutdown(self) -> None:
        self._started = False
        self._words = []
        self._forget_turn()

    def _forget_turn(self) -> None:
        self._fed = bytearray()
        self._feed_sizes = []
        self._partials = 0
        if self._player is not None:
            self._player.begin()

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
        if self._turns_dir is not None:
            self._fed += frame
            self._feed_sizes.append(len(frame))
        partial = self._partial(frame)
        if partial is not None:
            self._partials += 1
        return partial

    def _partial(self, frame: bytes) -> Transcript | None:
        if self._player is not None:
            recorded = self._player.feed(frame)
            if recorded is None:
                return None
            # A cassette keeps no partial's confidence, and 1.0 is what a
            # provider says when it has no number.
            return Transcript(text=recorded["text"], final=False, confidence=1.0)
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
        outcome: str | None = None
        if self._started and self._player is not None:
            played = self._player.finish()
            if played.turn is None:
                outcome = "miss"
                final = Transcript(text="", final=True, confidence=1.0, error=_cassette.MISS)
            else:
                outcome = "hit"
                said = played.turn["final"]
                confidence = 1.0 if said["confidence"] is None else said["confidence"]
                final = Transcript(text=said["text"], final=True, confidence=confidence, error=said["error"])
        else:
            script = self._script
            text = " ".join(script) if script else " ".join(self._words)
            final = Transcript(text=text, final=True, confidence=1.0)
        if self._started and self._turns_dir is not None:
            self._write_turn(final, outcome)
        self._words = []
        self._forget_turn()
        return final

    def _write_turn(self, final: Transcript, outcome: str | None) -> None:
        """One turn's bytes and its line in `turns.jsonl`. The raw file is
        written first, so a line always has its bytes beside it."""
        assert self._turns_dir is not None
        self._turn += 1
        data = bytes(self._fed)
        line = {
            "turn": self._turn,
            "bytes": len(data),
            "feed_sizes": list(self._feed_sizes),
            "fed_sha256": hashlib.sha256(data).hexdigest(),
            "cassette": outcome,
            "final": final.text,
            "partials": self._partials,
        }
        try:
            (self._turns_dir / f"turn-{self._turn:04d}.raw").write_bytes(data)
            with open(self._turns_dir / "turns.jsonl", "a", encoding="utf-8") as out:
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.record_error = f"record_turns: turn {self._turn}: {exc.strerror or exc}"
            log.error("mock stt: %s", self.record_error)


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
