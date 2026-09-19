"""`emet-listen`: bring a body up and print what it hears.

The 0.3 milestone in one command, and the steps of 0.4 behind flags. It
brings up a microphone and a wake detector, and says so each time the robot
hears its name. With `--transcribe` it also hands what follows to the speech
recognition provider the soul names and prints what was said, partials as
they arrive and then the final. With `--reply` it hands what was said to the
language model the soul names and prints the answer as it streams. With
`--speak` the answer is also said, through the voice the soul names and out
of the speaker, a sentence at a time while the rest is still being written.

Useful before that, though. `--replay` points the same path at a recording
instead of a microphone, so a wake failure somebody reports can be reproduced
exactly, on a machine that was never in their room.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

from emet_sdk.discovery import PluginRegistry
from emet_sdk.types import ReplyDone, TextDelta, Transcript, WakeEvent
from emet_sdk.validate import (
    MissingPluginError,
    ValidationError,
    load_yaml,
    validate_manifest,
    validate_soul,
)

from emet_engine.keys import load_keys
from emet_engine.session import EngineError, ListenSession
from emet_engine.turn import EndReason

__all__ = ["main", "print_run_footer"]


def _load(path: Path, kind: str, registry: PluginRegistry) -> dict[str, Any]:
    try:
        doc = load_yaml(path)
    except Exception as exc:
        raise EngineError(f"could not read {path}: {exc}") from exc
    report = (
        validate_manifest(doc, registry=registry) if kind == "manifest" else validate_soul(doc)
    )
    for finding in report.warnings:
        print(f"  ! {finding.code} {finding.path}", file=sys.stderr)
    if not report.ok:
        for finding in report.errors:
            print(f"  x {finding.code} {finding.path}\n    {finding.message}", file=sys.stderr)
        raise EngineError(f"{path.name} is not a valid {kind}")
    return doc


def _replay(manifest: dict[str, Any], wav: str) -> dict[str, Any]:
    """Point the body at a recording instead of its microphone."""
    manifest = copy.deepcopy(manifest)
    manifest.setdefault("audio", {})["input"] = {
        "source": "wav",
        "params": {"path": wav},
        # `device` is required by the schema and meaningless for a file. The
        # session never reads it; it is here so the document still validates.
        "device": "file",
    }
    return manifest


def _on_wake(event: WakeEvent) -> None:
    """Printed the moment the name is heard, not when the turn ends: on a live
    run that is the difference between feedback and a second of doubt."""
    print(f"  heard {event.phrase!r}  (confidence {event.confidence:.2f})")


def _on_partial(transcript: Transcript) -> None:
    print(f"    hearing {transcript.text!r}")


def _on_extended(text: str) -> None:
    print("    (sounds unfinished; waiting one more window)")


async def _run(args: argparse.Namespace) -> int:
    registry = PluginRegistry.discover()
    if not registry:
        print(
            "no plugins are installed, so there is no microphone and no wake\n"
            "engine to use. Install the hardware layer:\n"
            "    pip install 'emet-hal[audio,wake]'",
            file=sys.stderr,
        )
        return 2

    # Keys before providers: the plugins read the environment in start().
    # Names are printed, values never are.
    try:
        loaded = load_keys(args.keys)
    except (FileNotFoundError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    for entry in loaded:
        if entry.warning:
            print(f"  ! {entry.warning}", file=sys.stderr)

    manifest = _load(Path(args.manifest), "manifest", registry)
    soul = _load(Path(args.soul), "soul", registry)
    if args.replay:
        manifest = _replay(manifest, args.replay)
    if args.echo:
        # Echo replays captured *input* audio, so the sink has to run at the
        # input's rate rather than the synthesis rate it would normally use.
        # Under --speak the session opens the sink at the voice's rate
        # instead, which is why the two flags exclude each other.
        manifest = copy.deepcopy(manifest)
        output = manifest.setdefault("audio", {}).setdefault("output", {})
        output["sample_rate"] = int(
            (manifest["audio"].get("input") or {}).get("sample_rate") or 16000
        )

    # Speaking needs a reply to speak, and a reply needs words to reply to,
    # so --speak implies --reply implies --transcribe.
    reply = args.reply or args.speak
    transcribe = args.transcribe or reply
    session = ListenSession(
        manifest,
        soul,
        registry=registry,
        transcribe=transcribe,
        reply=reply,
        speak=args.speak,
        on_wake=_on_wake,
        on_partial=_on_partial if transcribe else None,
        on_extended=_on_extended if transcribe else None,
    )
    async with session:
        assert session.descriptor is not None and session.format is not None
        lines = [
            f"listening for {session.phrase!r}",
            f"  engine   {session.engine_name}",
            f"  source   {session.source_name}",
            f"  audio    {session.format.sample_rate} Hz, {session.format.frame_ms:.0f} ms frames",
            f"  patience {session.patience_ms} ms"
            + (", extends once on a trailing clause" if transcribe and session.extend_on_incomplete else ""),
        ]
        if transcribe:
            described = session.stt_descriptor
            model = f", model {described.model}" if described and described.model else ""
            mode = "streaming" if described and described.streaming else "batch"
            lines.append(f"  stt      {session.stt_name}{model} ({mode})")
        if reply:
            described_llm = session.llm_descriptor
            model = f", model {described_llm.model}" if described_llm and described_llm.model else ""
            lines.append(f"  llm      {session.chat_name}{model}")
        if args.speak:
            described_tts = session.tts_descriptor
            model = f", model {described_tts.model}" if described_tts and described_tts.model else ""
            rate = f" ({described_tts.sample_rate} Hz)" if described_tts else ""
            lines.append(f"  voice    {session.tts_name}{model}{rate}")
        for entry in loaded:
            names = ", ".join(entry.names) or "nothing new"
            lines.append(f"  keys     {names} from {entry.path}")
        print("\n".join(lines))
        print("  (ctrl-c to stop)\n" if not args.replay else "")

        heard = 0
        interrupted = False
        try:
            async for event, utterance in session.turns():
                heard += 1
                if utterance.had_speech:
                    print(
                        f"    then {utterance.duration_ms / 1000:.1f}s of speech, "
                        f"ended on {utterance.reason.value}"
                    )
                    if args.echo:
                        await session.say(utterance.audio)
                        print("    played it back")
                elif utterance.reason is EndReason.SOURCE_ENDED:
                    print("    the recording ended before anything followed.")
                else:
                    print("    then nothing. probably a false wake.")
                if utterance.transcript is not None:
                    if utterance.transcript.text:
                        print(f"    said {utterance.transcript.text!r}")
                    else:
                        print("    said nothing the provider could make out")
                if reply and utterance.transcript is not None and utterance.transcript.text.strip():
                    await _print_reply(session, utterance.transcript.text, speak=args.speak)
        except asyncio.CancelledError:
            # Ctrl-C. `asyncio.run` answers SIGINT by cancelling this task, and
            # a microphone never ends on its own, so this is how every live
            # run finishes. The numbers gathered so far are the reason the run
            # happened, so they are reported rather than lost. A task that
            # handles its own cancellation uncancels itself, which is what
            # lets the session shut down cleanly below.
            interrupted = True
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()

        if interrupted:
            print(f"\nstopped. heard it {heard} time(s).")
        else:
            print(f"\nsource ended. heard it {heard} time(s).")
        _finish(session, args)
    return 0


async def _print_reply(session: ListenSession, text: str, *, speak: bool = False) -> None:
    """Stream the reply to the console as the model produces it, and with
    `speak`, out of the speaker as each sentence completes."""
    print("    reply: ", end="", flush=True)
    events = session.answer_aloud(text) if speak else session.answer(text)
    async for event in events:
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ReplyDone):
            print()
            if event.stop_reason == "error":
                print(f"    the language model failed: {event.error}")
            elif event.stop_reason == "refusal":
                print("    (the provider declined to answer)")
            elif event.stop_reason == "length":
                print("    (cut off at the reply's token cap)")
    if speak:
        spoken = session.last_spoken
        said = [s for s in spoken if s.ok and s.audio_bytes]
        print(f"    spoke {len(said)} sentence(s)")
        for lost in (s for s in spoken if not s.ok):
            print(f"    the voice could not say {lost.text!r}: {lost.error}")


def _finish(session: ListenSession, args: argparse.Namespace) -> None:
    print_run_footer(session, stats=args.stats, busy=bool(args.echo or args.reply or args.speak))


def print_run_footer(session: ListenSession, *, stats: bool, busy: bool) -> None:
    """Report what the run cost, if asked, and always report what it lost.

    `busy` says whether the run had reason to stop reading the microphone
    (playback, a reply, a voice), in which case dropped frames are explained
    rather than blamed on the loop. Shared by `emet-listen` and `emet-talk`.
    """
    if stats:
        # `live` from the source that actually ran, not from the flag: only a
        # real sound card can drift, and claiming otherwise for a file would
        # be inventing a measurement.
        print("\n" + session.stats.report(live=session.source_name != "wav"))
    hint = session.warm_start_hint()
    if hint:
        print("\n" + hint)
    if session.underflows:
        print(
            f"\nnote: the speaker reported {session.underflows} late callback(s): the "
            f"card played silence it was not given, so playback may have stuttered."
        )
    if session.dropped and busy:
        print(
            f"\nnote: {session.dropped} frame(s) were dropped while the robot was "
            f"speaking or thinking. The loop does not read the microphone during "
            f"playback, a reply, or the wait for a final transcript; barge-in, in "
            f"1.0, is what changes that."
        )
    elif session.dropped:
        print(
            f"\nwarning: {session.dropped} frame(s) were dropped, so wake words "
            f"may have been missed. The loop is not keeping up with the audio."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emet-listen",
        description="Bring up a body and print each time it hears its name.",
    )
    parser.add_argument("manifest", help="body manifest (yaml)")
    parser.add_argument("soul", help="soul bundle (yaml)")
    parser.add_argument(
        "--replay",
        metavar="WAV",
        help="read this 16-bit mono wav instead of the microphone, so a wake "
        "failure can be reproduced away from the room it happened in",
    )
    playback = parser.add_mutually_exclusive_group()
    playback.add_argument(
        "--echo",
        action="store_true",
        help="play each captured utterance back through the output: audio in, "
        "wake, endpoint, audio out, with no voice in between. Excludes --speak, "
        "which opens the output at the voice's rate rather than the input's",
    )
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="hand the speech after each wake to the speech recognition provider "
        "the soul names under models.stt (or the body takes over under its own "
        "models.stt block), and print what was said: partials as they arrive, then the "
        "final. The reference soul names deepgram, which needs "
        "emet-providers[deepgram] and a key in EMET_DEEPGRAM_KEY; `mock` reads "
        "words out of the bytes it is given and needs neither",
    )
    parser.add_argument(
        "--keys",
        metavar="FILE",
        help="read provider keys from this file, one NAME=value a line, into the "
        "environment before anything starts. Without it, /etc/emet/keys.env and "
        "~/.config/emet/keys.env are read when they exist. A variable already "
        "exported is never overwritten. Values are never printed",
    )
    parser.add_argument(
        "--reply",
        action="store_true",
        help="hand what was said to the language model the soul names under "
        "models.chat (or the body takes over) and print the reply as it streams. "
        "Implies --transcribe",
    )
    playback.add_argument(
        "--speak",
        action="store_true",
        help="say the reply through the voice the soul names under models.tts "
        "(or the body takes over) and out of the audio sink, a sentence at a "
        "time while the rest is still being written; the sink runs at the "
        "voice's sample rate. Implies --reply. The reference soul names piper, "
        "which needs emet-providers[piper] and a voice downloaded once; "
        "`mock` needs neither and makes a sound no one could mistake for speech",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="report whether the loop kept up: per-frame timings, frames over "
        "budget, and the real-time factor. This is how the ten-minute soak in "
        "the 0.3 acceptance criteria is actually checked",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0
    except (EngineError, MissingPluginError, ValidationError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
