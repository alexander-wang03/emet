"""`emet-talk`: bring a body up and let it talk.

The 0.4 gate, "It talks", as one command with no flags. Everything it needs
is in the two documents: the microphone and the wake engine from the body,
the wake phrase and the three providers from the soul, the body's say over
any of them. Wake, words, answer, voice, speaker, one turn after another,
until Ctrl-C.

`emet-listen` is the same loop with each stage behind a flag, for finding
out which one is wrong. This command is for the robot.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from emet_sdk.discovery import PluginRegistry
from emet_sdk.validate import MissingPluginError, ValidationError

from emet_engine.cli import _load, _replay, print_run_footer
from emet_engine.keys import load_keys
from emet_engine.session import EngineError, ListenSession
from emet_engine.turn import EndReason

__all__ = ["main"]


async def _run(args: argparse.Namespace) -> int:
    registry = PluginRegistry.discover()
    if not registry:
        print(
            "no plugins are installed. Install the hardware layer and the providers:\n"
            "    pip install 'emet-hal[audio,wake]' 'emet-providers[deepgram,openai,piper]'",
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
    if args.replay:
        manifest = _replay(manifest, args.replay)

    name = str((soul.get("identity") or {}).get("name") or "Emet")
    speaker_tag = f"  {name.lower()}: "

    session = ListenSession(
        manifest,
        soul,
        registry=registry,
        transcribe=True,
        reply=True,
        speak=True,
        on_wake=lambda event: print(f"\n  (heard {event.phrase!r})", flush=True),
        on_extended=lambda text: print("  (that sounded unfinished; waiting a little longer)", flush=True),
    )
    async with session:
        assert session.format is not None
        described = [
            f"{name} is listening for {session.phrase!r}",
            f"  wake     {session.engine_name} on {session.source_name}, "
            f"{session.format.sample_rate} Hz",
            f"  words    {session.stt_name}"
            + (f", {session.stt_descriptor.model}" if session.stt_descriptor and session.stt_descriptor.model else ""),
            f"  answers  {session.chat_name}"
            + (f", {session.llm_descriptor.model}" if session.llm_descriptor and session.llm_descriptor.model else ""),
            f"  voice    {session.tts_name}"
            + (f", {session.tts_descriptor.model}" if session.tts_descriptor and session.tts_descriptor.model else "")
            + (f" ({session.tts_descriptor.sample_rate} Hz)" if session.tts_descriptor else ""),
            f"  patience {session.patience_ms} ms"
            + (", extends once on a trailing clause" if session.extend_on_incomplete else ""),
        ]
        for entry in loaded:
            described.append(f"  keys     {', '.join(entry.names) or 'nothing new'} from {entry.path}")
        print("\n".join(described))
        print("  (ctrl-c to stop)" if not args.replay else "")

        exchanges = 0
        interrupted = False
        try:
            async for exchange in session.talk(
                on_said=lambda text: print(f"  you:  {text}\n{speaker_tag}", end="", flush=True),
                on_delta=lambda text: print(text, end="", flush=True),
            ):
                exchanges += 1
                text = exchange.utterance.transcript.text.strip() if exchange.utterance.transcript else ""
                if not text:
                    if exchange.utterance.reason is EndReason.SOURCE_ENDED:
                        print("  (the recording ended before anything followed)")
                    else:
                        print("  (then nothing)")
                    if exchange.line:
                        print(f"{speaker_tag}{exchange.line}")
                    continue
                # The `you:` line and the streamed reply were printed by the
                # two hooks above; this ends the reply's line.
                print()
                if exchange.reply is not None and exchange.reply.stop_reason == "refusal":
                    print("  (the provider declined)")
                elif exchange.reply is not None and exchange.reply.stop_reason == "error":
                    print(f"  (the language model failed: {exchange.reply.error})")
                elif exchange.reply is not None and exchange.reply.stop_reason == "length":
                    print("  (cut off at the reply's token cap)")
                if exchange.line:
                    print(f"{speaker_tag}{exchange.line}")
                for lost in (s for s in exchange.spoken if not s.ok):
                    print(f"  (the voice could not say {lost.text!r}: {lost.error})")
                if exchange.intents:
                    print("  (intents: " + ", ".join(i.name for i in exchange.intents) + ")")
        except asyncio.CancelledError:
            interrupted = True
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()

        print(f"\n{'stopped' if interrupted else 'source ended'}. {exchanges} exchange(s).")
        print_run_footer(session, stats=args.stats, busy=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emet-talk",
        description="Bring up a body and let it talk: wake, words, answer, voice, speaker.",
    )
    parser.add_argument("manifest", help="body manifest (yaml)")
    parser.add_argument("soul", help="soul bundle (yaml)")
    parser.add_argument(
        "--keys",
        metavar="FILE",
        help="read provider keys from this file, one NAME=value a line. Without "
        "it, /etc/emet/keys.env and ~/.config/emet/keys.env are read when they "
        "exist. An exported variable always wins. Values are never printed",
    )
    parser.add_argument(
        "--replay",
        metavar="WAV",
        help="read this 16-bit mono wav instead of the microphone",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="report the loop's timings at the end: the wait for the words, "
        "the first word, the first sound, and whether the loop kept up",
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
