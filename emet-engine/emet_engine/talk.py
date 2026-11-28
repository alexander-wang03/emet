"""`emet-talk`: bring a body up and let it talk.

The 0.4 gate, "It talks", as one command with no flags. Everything it needs
is in the two documents: the microphone and the wake engine from the body,
the wake phrase and the three providers from the soul, the body's say over
any of them. Wake, words, answer, voice, speaker, one turn after another,
until Ctrl-C.

`emet-listen` is the same loop with each stage behind a flag, for finding
out which one is wrong. This command is for the robot.

A turn that failed prints the line `emet_engine.failures` holds for its
row, which the session picked when it ended the turn, and the robot says
the row's line aloud. The speaker's tag (`  emet: `) is printed with the
reply's first word, so a reply that failed before any text leaves no empty
tag above its failure line (ledger L16 of 0.5.7).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from emet_sdk.discovery import PluginRegistry
from emet_sdk.validate import MissingPluginError, ValidationError

from emet_engine import board
from emet_engine.cli import (
    StatsLines,
    _load,
    _replay,
    add_body_flags,
    add_events_flag,
    boot_lines,
    events_refused,
    failure_text,
    identity_line,
    install_stop_signals,
    live_line,
    open_events,
    print_explanation,
    print_failure,
    print_run_footer,
    provider_lines,
    state_warnings,
)
from emet_engine.keys import load_keys
from emet_engine.session import EngineError, Exchange, ListenSession
from emet_engine.turn import EndReason

__all__ = ["main"]


class Speaker:
    """The `you:` line and the speaker's line of each exchange.

    The speaker's tag is printed with the reply's first word, never before
    it, so a reply that failed or was declined before any text leaves no
    empty `  emet: ` line above the line that says why.
    """

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.open = False

    def said(self, text: str) -> None:
        print(f"  you:  {text}", flush=True)

    def delta(self, text: str) -> None:
        if not text:
            return
        if not self.open:
            print(self.tag, end="", flush=True)
            self.open = True
        print(text, end="", flush=True)

    def close(self) -> None:
        """End the reply's line, if one was started."""
        if self.open:
            print()
            self.open = False


async def _run(args: argparse.Namespace) -> int:
    # Which tree ran, first, so a boot that fails still names it; and under
    # `--stats` the board as the run found it, read again at the footer.
    print(identity_line("emet-talk"), flush=True)
    board_start = board.read_board() if args.stats else None
    events = open_events(getattr(args, "events", None), "emet-talk")
    try:
        return await _talk(args, board_start, events)
    finally:
        if events is not None:
            events.close()


async def _talk(args: argparse.Namespace, board_start: board.BoardReading | None, events: Any) -> int:
    install_stop_signals()
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
    speaker = Speaker(f"  {name.lower()}: ")
    out = StatsLines("emet-talk", stats=args.stats, events=events)

    session = ListenSession(
        manifest,
        soul,
        registry=registry,
        transcribe=True,
        reply=True,
        speak=True,
        on_wake=lambda event: (print(f"\n  (heard {event.phrase!r})", flush=True), out.wake(event)),
        on_handover=out.handover if args.stats else None,
        on_extended=lambda text: print("  (that sounded unfinished; waiting a little longer)", flush=True),
        chains=args.chains or (),
        state=args.state,
    )
    with live_line(session, stats=args.stats):
        return await _talk_to(session, args, board_start, events, name=name, speaker=speaker, out=out, loaded=loaded)


async def _talk_to(
    session: ListenSession,
    args: argparse.Namespace,
    board_start: board.BoardReading | None,
    events: Any,
    *,
    name: str,
    speaker: Speaker,
    out: StatsLines,
    loaded: Any,
) -> int:
    async with session:
        assert session.format is not None
        state_warnings(session)
        described = [
            f"{name} is listening for {session.phrase!r}",
            f"  wake     {session.engine_name} on {session.source_name}, "
            f"{session.format.sample_rate} Hz",
            *provider_lines(session, "emet-talk"),
            f"  patience {session.patience_ms} ms"
            + (", extends once on a trailing clause" if session.extend_on_incomplete else ""),
        ]
        described += boot_lines(session, reply=True)
        for entry in loaded:
            described.append(f"  keys     {', '.join(entry.names) or 'nothing new'} from {entry.path}")
        print("\n".join(described))
        if args.explain:
            print_explanation(session, reply=True)
        print("  (ctrl-c to stop)" if not args.replay else "")

        exchanges = 0
        interrupted = False
        try:
            async for exchange in session.talk(on_said=speaker.said, on_delta=speaker.delta):
                exchanges += 1
                _print_exchange(session, exchange, speaker, out)
        except asyncio.CancelledError:
            interrupted = True
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()

        print(f"\n{'stopped' if interrupted else 'source ended'}. {exchanges} exchange(s).")
        print_run_footer(session, stats=args.stats, board_start=board_start, events=events)
    return 0


def _print_exchange(session: ListenSession, exchange: Exchange, speaker: Speaker, out: StatsLines) -> None:
    """One exchange's notes, after the `you:` line and the streamed reply
    the two hooks printed: the failure line of the turn's row, the line said
    aloud, the sentences lost, the intents, and under `--stats` the turn's
    own line, last."""
    speaker.close()
    transcript = exchange.utterance.transcript
    text = transcript.text.strip() if transcript else ""
    failed = (
        failure_text(session, exchange.failure, "emet-talk", exchange.utterance, exchange.reply)
        if exchange.failure is not None
        else None
    )
    if not text:
        if failed is not None and exchange.failure is not None:
            print_failure(failed, exchange.failure)
        elif exchange.utterance.reason is EndReason.SOURCE_ENDED:
            print("  (the recording ended before anything followed)")
        else:
            print("  (then nothing)")
    else:
        if transcript is not None and transcript.error:
            print(f"  (those words may be cut short: {transcript.error})")
        if exchange.reply is not None and exchange.reply.stop_reason == "refusal":
            print("  (the provider declined)")
        elif failed is not None and exchange.failure is not None:
            print_failure(failed, exchange.failure)
    if exchange.line:
        print(f"{speaker.tag}{exchange.line}")
    for lost in (s for s in exchange.spoken if not s.ok):
        print(f"  (the voice could not say {lost.text!r}: {lost.error})")
    if exchange.intents:
        print("  (intents: " + ", ".join(i.name for i in exchange.intents) + ")")
    out.turn(exchange.record)


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
        "the first word, the first sound, and whether the loop kept up; and "
        "under each wake that handed audio over, its lag, the end of the name "
        "written as silence, and what the level stop alone would have written; "
        "and the board's temperature, throttle flags and load average at the "
        "start and at the end, as far as the machine reports them",
    )
    add_events_flag(parser)
    add_body_flags(parser)
    args = parser.parse_args(argv)
    if events_refused(args, "emet-talk"):
        return 2

    try:
        return asyncio.run(_run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C during boot arrives as the first; SIGTERM or SIGHUP, which
        # cancel the task themselves, as the second. The same stop either way.
        print("\nstopped.")
        return 0
    except (EngineError, MissingPluginError, ValidationError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
