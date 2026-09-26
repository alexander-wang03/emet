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

**The boot log.** Since 0.5 the header also says which body this is, the
binding table the engine resolved against the parts that started (grouped by
what performs each intent; `emet explain --why` is the per-intent view), where
the body's state file is and what it carried over, the speaker's own
output latency once its stream is open, and, with a language model running,
how many sentences the robot was told about its body. `--explain` prints
those sentences, and the whole system prompt when a language model is
running.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import shutil
import sys
from pathlib import Path
from typing import Any, TextIO

from emet_sdk.discovery import PluginRegistry
from emet_sdk.types import ReplyDone, TextDelta, Transcript, WakeEvent
from emet_sdk.validate import (
    MissingPluginError,
    ValidationError,
    load_yaml,
    validate_manifest,
    validate_soul,
)

from emet_engine.body import format_bindings
from emet_engine.keys import load_keys
from emet_engine.session import EngineError, ListenSession
from emet_engine.turn import EndReason

__all__ = ["main", "print_run_footer", "boot_lines", "Caption"]


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
    """Point the body at a recording instead of its microphone.

    Only the source changes. The rest of `audio.input` (the capsule count,
    direction-finding, echo cancellation) still describes the body, and the
    self-model reads it, so a replay tells the model the same body the live
    run did.
    """
    manifest = copy.deepcopy(manifest)
    audio = manifest.setdefault("audio", {})
    audio["input"] = {
        **dict(audio.get("input") or {}),
        "source": "wav",
        "params": {"path": wav},
        # `device` is required by the schema and meaningless for a file. The
        # session never reads it; it is here so the document still validates.
        "device": "file",
    }
    return manifest


def install_stop_signals() -> None:
    """End the run the way Ctrl-C does on SIGTERM and SIGHUP, where the
    platform has them.

    A run ended by `systemctl stop`, or by an SSH session that dropped with
    the hotspot, would otherwise die without its footer, without keeping
    what the wake engine learned, and without putting its parts to rest.
    Cancelling the running task sends both signals down Ctrl-C's path, and
    `main()` reports a cancellation that lands during boot as Ctrl-C's.

    A hangup that is already ignored stays ignored: `nohup` asked for a run
    that outlives the terminal, which is the usual Unix courtesy of a
    program that handles SIGHUP.
    """
    import signal as _signal

    task = asyncio.current_task()
    if task is None:
        return
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(_signal, name, None)
        if number is None:
            continue
        if name == "SIGHUP" and _signal.getsignal(number) is _signal.SIG_IGN:
            continue
        try:
            loop.add_signal_handler(number, task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows' event loop takes no signal handlers


def _on_wake(event: WakeEvent) -> None:
    """Printed the moment the name is heard, not when the turn ends: on a live
    run that is the difference between feedback and a second of doubt."""
    print(f"  heard {event.phrase!r}  (confidence {event.confidence:.2f})")


class Caption:
    """The live caption while somebody is speaking.

    Every partial carries the whole utterance heard so far, which is what
    lets a caption replace its line rather than splice fragments (`DESIGN.md`
    section 12.3). On a terminal each partial redraws the one line in place;
    written to a file, where a carriage return would leave a line of
    overwritten text, each partial gets a line of its own, as before 0.5.
    `close()` ends the line before anything else is printed.
    """

    def __init__(self, stream: TextIO | None = None, *, tty: bool | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        isatty = getattr(self.stream, "isatty", None)
        self.tty = bool(isatty()) if tty is None and callable(isatty) else bool(tty)
        self._width = 0

    def show(self, transcript: Transcript) -> None:
        line = f"    hearing {transcript.text!r}"
        if not self.tty:
            print(line, file=self.stream, flush=True)
            return
        room = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        if len(line) > room:
            # Keep the newest words: the start of the sentence has been read.
            line = "    hearing '..." + line[len(line) - (room - 16):]
        self.stream.write("\r" + line + " " * max(0, self._width - len(line)))
        self.stream.flush()
        self._width = len(line)

    def close(self) -> None:
        if self.tty and self._width:
            self.stream.write("\n")
            self.stream.flush()
        self._width = 0


def _on_extended(caption: Caption) -> Any:
    def announce(text: str) -> None:
        caption.close()
        print("    (sounds unfinished; waiting one more window)")

    return announce


def boot_lines(session: ListenSession, *, reply: bool) -> list[str]:
    """What the header says about the body: its name, the binding table the
    engine resolved at boot, the state file, and the self-model's size.

    Shared by `emet-listen` and `emet-talk`, so the boot log is the same
    wherever the robot is started.
    """
    lines: list[str] = []
    label = session.body_name or "(unnamed body)"
    lines.append(f"  body     {label}" + (f" ({session.body_id})" if session.body_id else ""))
    body = session.body
    if body is not None:
        table = body.table
        parts = len(body.capabilities) + len(body.reserved)
        lines.append(
            f"  chains   {len(table)} resolved at boot against {parts} part(s): "
            f"{len(table.hardware_bound)} to hardware, {len(table.voice_bound)} to voice"
        )
        lines.extend(f"             {row}" for row in format_bindings(table))
        for cap_id, detail in sorted(body.faults.items()):
            lines.append(f"  fault    {cap_id}: {detail}")
    state = session.body_state
    if state is not None and state.enabled:
        if not session.carry:
            carried = ", nothing carried in or out: a recording is not this body's room"
        elif session.wake_carried:
            carried = f", wake engine given {', '.join(session.wake_carried)} from it over the manifest's"
        else:
            carried = ""
        lines.append(f"  state    {state.path}{carried}")
    else:
        lines.append("  state    none: the manifest has no body.id to keep it under")
    latency = getattr(session._sink, "stream_latency_s", None)
    if latency is not None:
        lines.append(f"  output   {float(latency) * 1000:.0f} ms of stream latency, as the speaker reports it")
    if reply and session.self_model is not None:
        facts = session.self_model.facts
        absences = len(session.self_model.absences)
        lines.append(
            f"  self     {len(facts)} sentence(s) about its body, {absences} of them "
            f"what it cannot do; tags offered: {len(session.tags)}"
        )
    return lines


def print_explanation(session: ListenSession, *, reply: bool) -> None:
    """`--explain`: what the robot was told, word for word."""
    if reply:
        print("\n  SYSTEM PROMPT\n")
        text = session.system_prompt
    else:
        print("\n  SELF-MODEL\n")
        text = session.self_model.text if session.self_model is not None else "(none)"
    for line in text.splitlines():
        print(f"    {line}" if line else "")
    print()


def state_warnings(session: ListenSession) -> None:
    state = session.body_state
    for warning in (state.warnings if state is not None else []):
        print(f"  ! {warning}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    install_stop_signals()
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
    caption = Caption()
    session = ListenSession(
        manifest,
        soul,
        registry=registry,
        transcribe=transcribe,
        reply=reply,
        speak=args.speak,
        on_wake=_on_wake,
        on_partial=caption.show if transcribe else None,
        on_extended=_on_extended(caption) if transcribe else None,
        chains=getattr(args, "chains", None) or (),
        state=getattr(args, "state", None),
    )
    async with session:
        assert session.descriptor is not None and session.format is not None
        state_warnings(session)
        lines = [
            f"listening for {session.phrase!r}",
            f"  engine   {session.engine_name}",
            f"  source   {session.source_name}",
            f"  audio    {session.format.sample_rate} Hz, {session.format.frame_ms:.0f} ms frames",
            f"  patience {session.patience_ms} ms"
            + (", extends once on a trailing clause" if transcribe and session.extend_on_incomplete else ""),
        ]
        lines += boot_lines(session, reply=reply)
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
        if getattr(args, "explain", False):
            print_explanation(session, reply=reply)
        print("  (ctrl-c to stop)\n" if not args.replay else "")

        heard = 0
        interrupted = False
        try:
            async for event, utterance in session.turns():
                heard += 1
                caption.close()
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
                    elif utterance.transcript.error:
                        print(f"    the words never arrived: {utterance.transcript.error}")
                    else:
                        print("    said nothing the provider could make out")
                    if utterance.transcript.text and utterance.transcript.error:
                        print(f"    and may be cut short: {utterance.transcript.error}")
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

        caption.close()
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
    events = session.answer_aloud(text) if speak else session.answer_acted(text)
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
        # The reply's sentences; a filler for a tag was heard and is not one.
        said = [s for s in spoken if s.ok and s.audio_bytes and s.from_reply]
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
    # What this run learned about this body goes into its state file; only
    # when there is none to keep it in is the person asked to carry it.
    state = getattr(session, "body_state", None)
    seen = len(state.warnings) if state is not None else 0
    remember = getattr(session, "remember", None)
    kept = remember() if callable(remember) else None
    for warning in (state.warnings[seen:] if state is not None else []):
        print(f"\nwarning: {warning}")
    if kept is not None and state is not None:
        what = ", ".join(
            f"{key} ({', '.join(sorted(state.carried(key)))})" for key in session.kept
        )
        print(f"\nstate: kept {what} for the next boot on this body, in {kept}")
    elif getattr(session, "carry", True):
        # A replay's mean belongs to the recording's room, so it is neither
        # kept nor offered for pasting.
        hint = session.warm_start_hint()
        if hint:
            print("\n" + hint)
    if session.starved:
        print(
            f"\nnote: the voice fell behind real time {session.starved} time(s) mid-sentence, "
            f"so the speaker played silence inside a word. The card was served on time, so "
            f"this is the voice or the network, not the loop."
        )
    if session.underflows:
        print(
            f"\nnote: PortAudio reported {session.underflows} late callback(s): this process "
            f"did not hand the card audio in time and it inserted a gap."
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


def add_body_flags(parser: argparse.ArgumentParser) -> None:
    """The flags about the body, shared by `emet-listen` and `emet-talk`."""
    parser.add_argument(
        "--chains",
        action="append",
        metavar="FILE",
        help="lay this chain file over the SDK's, as `emet explain --chains` does; "
        "may be repeated, later files win. A chain changed here changes what an "
        "intent does on this body, down to the voice rung",
    )
    parser.add_argument(
        "--state",
        metavar="FILE",
        help="keep this body's state in FILE (or in <body.id>.json inside an existing "
        "directory): its binding table, its audio devices, each part's health, joint "
        "trims, and what its plugins carry over. Without it, a file already kept for "
        "this body wins; otherwise /etc/emet/state/<body.id>.json when that directory "
        "exists and is writable; otherwise ~/.local/state/emet/<body.id>.json. A replay "
        "carries no plugin's params in or out, and still records the bindings, the "
        "devices and each part's health",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print what the robot is told about its body at boot, and with a "
        "language model running the whole system prompt",
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
    add_body_flags(parser)
    args = parser.parse_args(argv)

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
