"""`emet-listen`: bring a body up and print what it hears.

The 0.3 milestone in one command, and the first step of 0.4 behind a flag. It
brings up a microphone and a wake detector, and says so each time the robot
hears its name. With `--transcribe` it also hands what follows to the speech
recognition provider the soul names and prints what was said, partials as
they arrive and then the final. With `--reply` it hands what was said to the
language model the soul names and prints the answer as it streams. Nothing
is spoken yet: that is the next seam.

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

__all__ = ["main"]


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
        # Once there is speech synthesis this goes away: the sink will run at
        # whatever the voice produces and nothing will need to match.
        manifest = copy.deepcopy(manifest)
        output = manifest.setdefault("audio", {}).setdefault("output", {})
        output["sample_rate"] = int(
            (manifest["audio"].get("input") or {}).get("sample_rate") or 16000
        )

    # A reply needs words to reply to, so --reply implies --transcribe.
    transcribe = args.transcribe or args.reply
    session = ListenSession(
        manifest,
        soul,
        registry=registry,
        transcribe=transcribe,
        reply=args.reply,
        on_wake=_on_wake,
        on_partial=_on_partial if transcribe else None,
    )
    async with session:
        assert session.descriptor is not None and session.format is not None
        lines = [
            f"listening for {session.phrase!r}",
            f"  engine   {session.engine_name}",
            f"  source   {session.source_name}",
            f"  audio    {session.format.sample_rate} Hz, {session.format.frame_ms:.0f} ms frames",
            f"  patience {session.patience_ms} ms",
        ]
        if transcribe:
            described = session.stt_descriptor
            model = f", model {described.model}" if described and described.model else ""
            mode = "streaming" if described and described.streaming else "batch"
            lines.append(f"  stt      {session.stt_name}{model} ({mode})")
        if args.reply:
            described_llm = session.llm_descriptor
            model = f", model {described_llm.model}" if described_llm and described_llm.model else ""
            lines.append(f"  llm      {session.chat_name}{model}")
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
                if args.reply and utterance.transcript is not None and utterance.transcript.text.strip():
                    await _print_reply(session, utterance.transcript.text)
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


async def _print_reply(session: ListenSession, text: str) -> None:
    """Stream the reply to the console as the model produces it."""
    print("    reply: ", end="", flush=True)
    async for event in session.answer(text):
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


def _finish(session: ListenSession, args: argparse.Namespace) -> None:
    """Report what the run cost, if asked, and always report what it lost."""
    if args.stats:
        # `live` from the source that actually ran, not from the flag: only a
        # real sound card can drift, and claiming otherwise for a file would
        # be inventing a measurement.
        print("\n" + session.stats.report(live=session.source_name != "wav"))
    hint = session.warm_start_hint()
    if hint:
        print("\n" + hint)
    if session.dropped and (args.echo or args.reply):
        print(
            f"\nnote: {session.dropped} frame(s) were dropped while the robot was "
            f"speaking or thinking. The loop does not read the microphone during "
            f"playback or a reply; barge-in, in 1.0, is what changes that."
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
    parser.add_argument(
        "--echo",
        action="store_true",
        help="play each captured utterance back through the output. There is "
        "no speech synthesis yet, so this is what proves the whole duplex path "
        "works: audio in, wake, endpoint, audio out",
    )
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="hand the speech after each wake to the speech recognition provider "
        "the soul names under models.stt (or the body takes over under "
        "audio.stt), and print what was said: partials as they arrive, then the "
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
        "Implies --transcribe. Nothing is spoken yet",
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
