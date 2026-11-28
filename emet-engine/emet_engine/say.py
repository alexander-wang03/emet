"""The `emet-say` command says a line in the soul's voice, with nothing listening.

`emet-say MANIFEST SOUL TEXT` boots the voice the two documents agree on and
the audio sink, and nothing else: no wake engine, no microphone, no
transcriber, no language model and no body part. The text is said as a
reply is said. Its tags are lifted out (`[express.curiosity]`) and each is
performed through its binding where it stood, and every sentence travels as
a `speak` intent. The chains resolve as for a body with no parts, so a tag
always reaches its chain's voice rung: a filler for an `express` tag, an
explanation for `move.approach`, `move.retreat` and `move.turn_to`, and
silence for `move.stop`, `move.wander` and an `attend` tag.

`--to FILE` writes what was said to a WAV file at the voice's rate through
the `wav` sink, whatever the manifest's `audio.output` names. Without it the
manifest's own sink plays, which on the reference body is its speaker.

It is for hearing the voice on its own: an owner trying a voice before a
robot uses it, and `tools/reply_audio.py`, which writes fixed replies to
files and measures their gaps, joins and loudness. The engine's own signals
are not performed, so the file holds the words and the tags' sounds and no
boot tone.

A command that speaks a given text and asks no model is edgevox's idea, its
`/say TEXT`, "Synthesize and play text directly (skip LLM)" (nrl-ai/edgevox
at 5fa958e, `docs/documentation/commands.md` line 23; its `LICENSE` file is
Apache-2.0 and its `pyproject.toml` and README say MIT); Home
Assistant's `assist_satellite.announce` and Linux Voice Assistant's
announcements have a speaker say a message the same way (home-assistant/core
and OHF-Voice/linux-voice-assistant, both Apache-2.0). An idea only, no
code. See CITATIONS.md.

What it prints, after the identity line every program prints first:

    voice    the provider, its model and rate, where the words go, its key
             variable and the distribution that offered it, as emet-talk's
             voice line reads
    sink     the sink, and the file for a WAV, and that no microphone opened
    keys     one line for each keys file read, as the other programs print
    said     how many sentences and fillers were heard, and how long the
             first sound took to reach the sink after the text was handed over
    lost     one line for each sentence the voice could not say; any such
             line makes the exit status 1

A voice that cannot start and a sink that cannot start are refused on stderr
with exit status 1, before anything is said. An empty text, a text with no
tag and nothing left to say once its marks are removed, and a wrong flag
exit with status 2.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from emet_sdk.discovery import GROUP_TTS, PluginOrigin, PluginRegistry
from emet_sdk.models import tts_selection
from emet_sdk.plugin import PluginError
from emet_sdk.resolve import load_chains
from emet_sdk.types import AudioFormat, AudioSink, Intent, SelfModel, VoiceDescriptor
from emet_sdk.validate import DEFAULT_AUDIO_SINK, MissingPluginError, ValidationError

from emet_engine import failures
from emet_engine.acting import Actor, speak
from emet_engine.body import Body
from emet_engine.cli import _load, identity_line, install_stop_signals, place_line
from emet_engine.intent_tags import IntentTags
from emet_engine.keys import load_keys
from emet_engine.prompting import persona_lines
from emet_engine.self_model import compile_self_model
from emet_engine.session import EngineError, read_health, sentences_of
from emet_engine.speech import Mouth, Sentences, SpokenSentence, speakable, split_sentences

__all__ = ["PROGRAM", "Said", "Saying", "main", "said_lines", "sink_line", "voice_line"]

log = logging.getLogger("emet_engine.say")

#: The name the program prints itself under.
PROGRAM = "emet-say"


# ----------------------------------------------------------------- lines


def voice_line(name: str, descriptor: VoiceDescriptor, origin: PluginOrigin | None) -> str:
    """The voice's boot line, as emet-talk prints it.

    `  voice    piper, en_US-ljspeech-medium (22050 Hz), on this body, from
    emet-providers 0.5.7`. The place is `cli.place_line`'s: `at` the
    descriptor's host when the voice sends its words anywhere, `on this
    body` for one of Emet's own voices that does not, and `where it sends is
    not stated` for another package's voice that names no address. The
    key's variable follows when the voice reads one, never its value.
    `origin` is the plugin's origin, whose distribution and version end the
    line, or None for a plugin discovery does not know.
    """
    line = f"  voice    {name}"
    if descriptor.model:
        line += f", {descriptor.model}"
    line += f" ({descriptor.sample_rate} Hz)"
    line += place_line(descriptor, origin)
    if descriptor.key_env:
        line += f", key {descriptor.key_env}"
    label = origin.label if origin is not None else None
    if label:
        line += f", from {label}"
    return line


def sink_line(name: str, block: Mapping[str, Any]) -> str:
    """The sink's boot line, which names the file a WAV sink writes."""
    path = (block.get("params") or {}).get("path") if name == "wav" else None
    where = f", {path}" if path else ""
    return f"  sink     {name}{where}; no microphone opened"


@dataclass(frozen=True, slots=True)
class Said:
    """What became of one text.

    `spoken` is every sentence handed to the voice, in the order it was
    heard, the words a tag added included (`from_reply` False). `first_ms`
    is the time from handing the text over to the first sound reaching the
    sink, or None when no sound did. `intents` are the tags lifted out of
    the text, in the order they were written. `added` is how many sentences
    each tag's words became, in order: one for a filler, one or more for an
    explanation.
    """

    spoken: tuple[SpokenSentence, ...]
    first_ms: float | None
    intents: tuple[Intent, ...] = ()
    added: tuple[int, ...] = ()

    @property
    def sentences(self) -> int:
        """Sentences of the text that were heard."""
        return sum(1 for s in self.spoken if s.ok and s.audio_bytes and s.from_reply)

    @property
    def fillers(self) -> int:
        """Tags whose words were heard, a filler or an explanation, one each
        however many sentences an explanation took."""
        heard = [bool(s.ok and s.audio_bytes) for s in self.spoken if not s.from_reply]
        count = at = 0
        for size in self.added:
            count += any(heard[at : at + size])
            at += size
        return count

    @property
    def lost(self) -> list[SpokenSentence]:
        """Sentences the voice or the sink could not finish."""
        return [s for s in self.spoken if not s.ok]


def said_lines(said: Said) -> list[str]:
    """The `said` line and a `lost` line for each sentence that was lost."""
    line = f"  said     {said.sentences} sentence(s)"
    if said.fillers:
        line += f" and {said.fillers} filler(s)"
    if said.first_ms is not None:
        line += f"; voice first {said.first_ms:.0f} ms"
    else:
        line += "; no sound reached the sink"
    lines = [line]
    lines += [f"  lost     the voice could not say {s.text!r}: {s.error}" for s in said.lost]
    return lines


# ---------------------------------------------------------------- saying


class Saying:
    """One voice and one sink, and a text said through them with its tags performed.

    `voice` is a started `VoicePlugin` and `sink` a started `AudioSink` at
    the voice's rate. `body` is a `Body` with no parts, whose table binds
    every intent to its voice rung, and `self_model` gives an `explain`
    rung its words. `clock` is read in seconds, `time.perf_counter` unless a
    test hands it another.
    """

    def __init__(
        self,
        voice: Any,
        sink: AudioSink,
        *,
        body: Body,
        self_model: SelfModel,
        sample_rate: int,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.sink = sink
        self.clock = clock
        self.mouth = Mouth(voice, sink, on_first_audio=self._first_sound)
        self.actor = Actor(body, self_model, say=self._say, play=self._play, sample_rate=sample_rate)
        self._heard: list[SpokenSentence] = []
        self._added: list[int] = []
        self._handed_at: float | None = None
        self._first_ms: float | None = None
        self._toned = False

    def _first_sound(self) -> None:
        if self._first_ms is None and self._handed_at is not None:
            self._first_ms = (self.clock() - self._handed_at) * 1000.0

    async def _say(self, text: str, *, from_reply: bool = True) -> Sequence[SpokenSentence]:
        """The actor's way into the voice: each sentence queued in order
        behind what is already being said, and nothing waited for here.
        The words a tag adds are noted as one group, so the count says how
        many tags were heard."""
        said = sentences_of(text)
        for sentence in said:
            await self.mouth.say(sentence, from_reply=from_reply)
        if not from_reply and said:
            self._added.append(len(said))
        return []

    async def _play(self, pcm: bytes) -> None:
        """A tone, in its place among the words: what is queued ahead of it
        is said first, then the tone plays, then the words go on."""
        self._heard += await self.mouth.finish()
        try:
            self._first_sound()
            await self.sink.play(pcm)
            self._toned = self._toned or bool(pcm)
        finally:
            self.mouth.open()

    async def say(self, text: str) -> Said:
        """Say `text`, its tags performed where they stood, and wait for the last sound."""
        tags = IntentTags()
        sentences = Sentences()
        intents: list[Intent] = []
        self._heard = []
        self._added = []
        self._first_ms = None
        self._toned = False
        self._handed_at = self.clock()
        self.mouth.open()
        try:
            for piece in [*tags.pieces(text), tags.flush()]:
                if isinstance(piece, str):
                    for sentence in sentences.feed(piece):
                        await self.actor.perform(speak(sentence))
                    continue
                intents.append(piece)
                # A sentence that has its end mark and waits only for the next
                # word to confirm it is said before the tag, which opens the
                # next one. The splitter decides as if that word had come, as
                # `ListenSession.answer_aloud` does for a streamed reply.
                closed, _ = split_sentences(sentences.pending.rstrip() + " X")
                if closed:
                    rest = sentences.flush()
                    if rest:
                        await self.actor.perform(speak(rest))
                await self.actor.perform(piece)
            rest = sentences.flush()
            if rest:
                await self.actor.perform(speak(rest))
            self._heard += await self.mouth.finish()
        except BaseException:
            await self.mouth.hush()
            raise
        heard = tuple(self._heard)
        # A chunk the sink then refused leaves the first-sound mark behind,
        # so it counts only when something was heard.
        sounded = self._toned or any(s.ok and s.audio_bytes for s in heard)
        return Said(heard, self._first_ms if sounded else None, tuple(intents), tuple(self._added))


# ------------------------------------------------------------------ boot


async def _quietly(name: str, action: Callable[[], Awaitable[Any]]) -> None:
    """Stop one piece, logging a failure so the next piece still stops."""
    try:
        await action()
    except Exception as exc:  # noqa: BLE001 - logged, and the rest still stops
        log.warning("stop: %s failed: %s", name, exc)


async def _start_voice(registry: PluginRegistry, selection: Mapping[str, Any],
                       voice_block: Mapping[str, Any]) -> tuple[Any, VoiceDescriptor]:
    """Build and start the voice, or raise `EngineError` with its reason."""
    name = str(selection["provider"])
    voice = registry.load_tts(name)(selection, voice_block)
    fault: str | None = None
    try:
        await voice.start()
    except PluginError as exc:
        fault = str(exc) or type(exc).__name__
    descriptor = voice.describe() if fault is None else None
    if descriptor is None or not descriptor.healthy:
        health = read_health(voice)
        detail = fault or (health.detail if not health.ok else None)
        await _quietly("the voice", voice.shutdown)
        raise EngineError(
            f"the voice {name!r} cannot start, so there is nothing to say the words with. "
            f"It will not run." + (f" {detail}" if detail else "")
        )
    health = read_health(voice)
    if health.ok and health.detail:
        # A healthy voice with something to say about itself, a model that
        # retires on a date, says it once, as the other programs' notices do.
        print(f"  ! {health.detail}", file=sys.stderr)
    return voice, descriptor


def _sink_refusal(name: str, exc: BaseException) -> str:
    """The sink's refusal, from the one table of failure lines."""
    row = failures.find("boot", "sink", None, None)
    if row is None:  # pragma: no cover - the table is held by its own tests
        raise RuntimeError("emet_engine.failures has no row for a sink that cannot start")
    return row.console_talk.format(name=name, detail="", cap="", exc=str(exc) or type(exc).__name__)


async def _start_sink(registry: PluginRegistry, name: str, block: Mapping[str, Any], rate: int) -> AudioSink:
    """Build and start the sink at the voice's rate, or raise `EngineError`."""
    sink = registry.load_audio_out(name)(dict(block), AudioFormat(sample_rate=rate))
    try:
        await sink.start()
    except PluginError as exc:
        await _quietly("the sink", sink.stop)
        raise EngineError(_sink_refusal(name, exc)) from exc
    return sink


def _output(manifest: Mapping[str, Any], to: str | None) -> tuple[str, dict[str, Any]]:
    """The sink to say it into, a WAV file when `--to` names one and the manifest's otherwise."""
    if to is not None:
        return "wav", {"sink": "wav", "device": "file", "params": {"path": to}}
    block = dict((manifest.get("audio") or {}).get("output") or {})
    return str(block.get("sink") or DEFAULT_AUDIO_SINK), block


async def _run(args: argparse.Namespace) -> int:
    print(identity_line(PROGRAM), flush=True)
    install_stop_signals()
    registry = PluginRegistry.discover()
    if not registry:
        print(
            "no plugins are installed, so there is no voice to say it with and no sink to say it\n"
            "into. Install the hardware layer and the providers:\n"
            "    pip install 'emet-hal[audio]' 'emet-providers[piper]'",
            file=sys.stderr,
        )
        return 2

    try:
        loaded = load_keys(args.keys)
    except (FileNotFoundError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    for entry in loaded:
        if entry.warning:
            print(f"  ! {entry.warning}", file=sys.stderr)

    manifest = _load(Path(args.manifest), "manifest", registry)
    soul = _load(Path(args.soul), "soul", registry)
    selection = tts_selection(manifest, soul)
    if selection is None:
        installed = ", ".join(registry.tts_names) or "(none)"
        raise EngineError(
            "speech was asked for and no voice is configured. Name one under "
            "`models.tts.provider` in the soul, or take it over with "
            f"`models.tts.provider` in the body. Installed: {installed}."
        )
    try:
        chains = load_chains(args.chains or ())
    except Exception as exc:  # noqa: BLE001 - a YAML error and a bad chain read the same to the person
        raise EngineError(f"the chains could not be loaded: {type(exc).__name__}: {exc}") from exc
    # No part is started: a body with none binds every intent to its voice
    # rung, and the self-model it is told about has none either.
    bodiless = {**dict(manifest), "capabilities": []}
    body = Body(bodiless, registry, chains=chains)
    self_model = compile_self_model(bodiless, capabilities=(), locomotion=None, lines=persona_lines(soul))

    name = str(selection["provider"])
    voice, descriptor = await _start_voice(registry, selection, soul.get("voice") or {})
    try:
        sink_name, block = _output(manifest, args.to)
        sink = await _start_sink(registry, sink_name, block, descriptor.sample_rate)
        try:
            lines = [voice_line(name, descriptor, registry.origin(GROUP_TTS, name)), sink_line(sink_name, block)]
            for entry in loaded:
                lines.append(f"  keys     {', '.join(entry.names) or 'nothing new'} from {entry.path}")
            print("\n".join(lines), flush=True)
            saying = Saying(voice, sink, body=body, self_model=self_model, sample_rate=descriptor.sample_rate)
            said = await saying.say(args.text)
        finally:
            await _quietly("the sink", sink.stop)
    finally:
        await _quietly("the voice", voice.shutdown)
    print("\n".join(said_lines(said)))
    return 1 if said.lost else 0


def _nothing_to_say(text: str) -> bool:
    """Whether `text` holds no tag and no word the voice would be handed:
    emoji or markdown marks alone (`**`), or a stage direction the tag
    lifter drops. Such a text would boot the voice and write an empty file,
    which a reader of the file would take for a voice that made no sound."""
    tags = IntentTags()
    pieces = [*tags.pieces(text), tags.flush()]
    if any(not isinstance(piece, str) for piece in pieces):
        return False
    words = " ".join(piece for piece in pieces if isinstance(piece, str))
    return not speakable(words)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Say a line in the soul's voice, with no microphone, no wake engine and no model.",
    )
    parser.add_argument("manifest", help="body manifest (yaml)")
    parser.add_argument("soul", help="soul bundle (yaml)")
    parser.add_argument(
        "text",
        help="what to say. Tags such as [express.curiosity] are performed where they "
        "stand, through the voice rung of their chain, and never read out",
    )
    parser.add_argument(
        "--to",
        metavar="FILE",
        help="write what is said to this WAV file, mono 16-bit at the voice's rate, "
        "through the wav sink, whatever the manifest's audio.output names. Without "
        "it the manifest's sink plays",
    )
    parser.add_argument(
        "--keys",
        metavar="FILE",
        help="read provider keys from this file, one NAME=value a line. Without "
        "it, /etc/emet/keys.env and ~/.config/emet/keys.env are read when they "
        "exist. An exported variable always wins. Values are never printed",
    )
    parser.add_argument(
        "--chains",
        action="append",
        metavar="FILE",
        help="lay this chain file over the SDK's, as `emet explain --chains` does; "
        "may be repeated, later files win",
    )
    args = parser.parse_args(argv)
    if not args.text.strip():
        print(f"{PROGRAM}: the text to say is empty", file=sys.stderr)
        return 2
    if _nothing_to_say(args.text):
        print(f"{PROGRAM}: the text to say is empty once its marks are removed", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopped.")
        return 0
    except (EngineError, MissingPluginError, ValidationError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
