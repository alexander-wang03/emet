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
running. Since 0.5.5 the first line of a run, before anything boots, is its
identity: the four packages' versions and the revision of the tree they ran
from (`identity_line`).

**Since 0.5.7.** Each provider line also says where the stage runs (`at`
the host it sends to, or `on this body`), which key it read by the
variable's name, and which installed package offered the plugin
(`provider_lines`). A failure a person must be told about prints the line
`emet_engine.failures` holds for it, chosen by the session from what
failed and never from an error's wording. Under `--stats` each turn ends
with its own line, `turn gap ... ms`, and at a terminal a live line under
the header shows what the ears hear now (`live_line`). `--events PATH`
writes what `--stats` prints as JSON records, one a line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Iterator, TextIO

from emet_sdk.discovery import GROUP_LLM, GROUP_STT, GROUP_TTS, PluginRegistry
from emet_sdk.types import ReplyDone, TextDelta, Transcript, WakeEvent
from emet_sdk.validate import (
    MissingPluginError,
    ValidationError,
    load_yaml,
    validate_manifest,
    validate_soul,
)

from emet_engine import board
from emet_engine.body import format_bindings
from emet_engine.failures import Failure
from emet_engine.keys import load_keys
from emet_engine.live import LiveLine
from emet_engine.metrics import Handover, TurnRecord
from emet_engine.session import EngineError, ListenSession, TurnEnd
from emet_engine.turn import EndReason, Utterance

__all__ = [
    "main",
    "print_run_footer",
    "boot_lines",
    "place_line",
    "provider_lines",
    "failure_text",
    "print_failure",
    "identity_line",
    "revision",
    "Caption",
    "StatsLines",
    "add_events_flag",
    "events_refused",
    "live_line",
    "open_events",
]

#: The four distributions a run is made of, in the order the identity line
#: names them.
DISTRIBUTIONS = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")

#: How long git may take to say which commit the source is at. `revision()`
#: took a median 31.28 ms on the maintainer's laptop (Windows 11, Python
#: 3.13.2) and 9.31 ms on the reference body (Raspberry Pi 5, Python 3.13.5,
#: a copy of the tree with its .git, load 0.05), 20 calls each on
#: 2026-10-28, in `releases/v0.5.5/timings/` outside the repository. Past
#: this the revision is printed as unknown and the run goes on.
GIT_TIMEOUT_S = 2.0

#: The engine's own source directory, where git is asked.
_SOURCE = Path(__file__).resolve().parent


def _version(distribution: str) -> str:
    """A distribution's installed version, or `not-installed`, one token
    either way, as the Reachy LiteRT sample's environment record writes a
    missing package (`emulator/env.py` line 41, Apache-2.0)."""
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _run_git(args: list[str], directory: Path) -> subprocess.CompletedProcess[str]:
    """Run git in `directory`, the one place the identity line starts a
    process.

    `GIT_OPTIONAL_LOCKS=0` keeps `git status` from writing the index, so a
    boot never holds the lock while the maintainer stages a change in the
    same tree. `LC_ALL=C` keeps git's messages in English, so a directory
    that is not a checkout is told apart from git failing. The output is
    read as UTF-8 with anything else replaced. Read strictly with the
    locale's codec, a byte that codec rejects raised on the reference body
    and stopped emet-listen before boot, and on Windows a branch named with
    an "Á" gave a false "no commit yet" (review, 2026-10-28).
    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def revision(source: Path | None = None) -> str:
    """The commit the engine's source is at and whether the working tree
    differs from it, as `eb5574854148, working tree matches`; or
    `unknown: <why>`.

    One `git status`: its porcelain v2 header names the commit, and any
    other line is a change, untracked files included, since a new file in
    the tree is part of what ran. A distribution installed from a wheel
    lives under `site-packages`, inside whatever checkout its venv sits in,
    and git there would name a commit the installed copy was never built
    from; that path is never asked. Git missing, slow or refusing says so,
    and the run goes on.
    """
    here = _SOURCE if source is None else source
    if any(part in ("site-packages", "dist-packages") for part in here.parts):
        return "unknown: installed, not a checkout"
    try:
        done = _run_git(
            ["status", "--porcelain=v2", "--branch", "--no-ahead-behind", "--untracked-files=normal"],
            here,
        )
    except subprocess.TimeoutExpired:
        return f"unknown: git took longer than {GIT_TIMEOUT_S:g} s"
    except FileNotFoundError:
        return "unknown: git not found"
    except (OSError, subprocess.SubprocessError) as exc:
        # A git that will not start, or fails some other way, costs the
        # line its revision and never the run.
        return f"unknown: git could not run ({type(exc).__name__})"
    if done.returncode != 0:
        if "not a git repository" in (done.stderr or ""):
            return "unknown: not a git checkout"
        return f"unknown: git exited {done.returncode}"
    commit, changed = None, False
    for line in (done.stdout or "").splitlines():
        if line.startswith("# branch.oid "):
            commit = line.split(" ", 2)[2].strip()
        elif line and not line.startswith("#"):
            changed = True
    if commit is None or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        # "(initial)": a repository with no commit yet.
        return "unknown: no commit yet"
    return f"{commit[:12]}, working tree {'differs' if changed else 'matches'}"


def identity_line(program: str) -> str:
    """The first line a run prints: which tree ran.

    `emet-listen: emet-sdk 0.5.5, emet-hal 0.5.5, emet-providers 0.5.5,
    emet-engine 0.5.5; revision eb5574854148, working tree differs`. The
    four versions always in that order, each a single token; after
    `revision `, either twelve hex digits of the commit and `working tree
    matches` or `working tree differs`, or `unknown: ` and the reason. It
    is printed before discovery, so a boot that fails still names its tree,
    and it never names the machine. The idea is microduck's, whose every
    daemon writes its identity as its first line and says `rev unknown`
    honestly when it has none (pollen-robotics/microduck at 942e58e,
    `docs/design/architecture.md` lines 531 to 536 and 591 to 592; an
    idea only, Apache-2.0). microduck compiles the revision in. Emet asks
    git at boot, which names the commit of a checkout such as the
    maintainer's; the body runner's tree is unpacked from an archive, so
    its runs print `revision unknown: not a git checkout`, and the runner's
    `source.json` records what it synced.
    """
    versions = ", ".join(f"{name} {_version(name)}" for name in DISTRIBUTIONS)
    return f"{program}: {versions}; revision {revision()}"


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
        self._stream = stream
        isatty = getattr(self.stream, "isatty", None)
        self.tty = bool(isatty()) if tty is None and callable(isatty) else bool(tty)
        self._width = 0

    @property
    def stream(self) -> TextIO:
        """The stream given, or `sys.stdout` as it is at each write, so a
        caption drawn while the live line wraps stdout clears that line
        first."""
        return self._stream if self._stream is not None else sys.stdout

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


#: The distributions Emet's own plugins come in, as PEP 503 normalises a
#: name. Every plugin of theirs fills its descriptor's `endpoint`, so one
#: with none runs on the body. A plugin of another distribution may have been
#: written before the field existed (0.5.6's SDK), and its None says nothing.
OWN_DISTRIBUTIONS = frozenset({"emet-hal", "emet-providers"})


def place_line(described: Any, origin: Any) -> str:
    """Where a provider's stage runs, as its boot line says it.

    `, at HOST` for a provider that sends to an address; `, on this body`
    for one of Emet's own plugins that sends nowhere; `, where it sends is
    not stated` for a plugin from any other distribution that names no
    address, since the engine cannot tell whether it sends anything.
    `origin` is the plugin's `PluginOrigin`, or None when discovery does not
    know it.
    """
    endpoint = getattr(described, "endpoint", None)
    if endpoint:
        return f", at {getattr(described, 'host', None) or endpoint}"
    distribution = getattr(origin, "distribution", None)
    if distribution and re.sub(r"[-_.]+", "-", distribution).lower() in OWN_DISTRIBUTIONS:
        return ", on this body"
    return ", where it sends is not stated"


def _where(session: ListenSession, described: Any, group: str, name: str | None) -> str:
    """How a provider line ends, saying where its stage runs and what it was
    started with.

    The place (`place_line`), then `, key NAME` when it read a key (the
    variable's name; a key's value is never printed), then `, from
    DISTRIBUTION VERSION`, the installed package that offered the plugin,
    when discovery knows it.
    """
    origin = session.registry.origin(group, name) if name else None
    place = place_line(described, origin)
    key_env = getattr(described, "key_env", None)
    if key_env:
        place += f", key {key_env}"
    if origin is not None and origin.label is not None:
        place += f", from {origin.label}"
    return place


def _provider_line(
    session: ListenSession, label: str, name: str | None, described: Any, group: str, named: str,
    *, mode: str = "", rate: str = "",
) -> str:
    model = getattr(described, "model", None)
    text = f"  {label:<8} {name}{mode}" + (f"{named}{model}" if model else "") + rate
    return text + _where(session, described, group, name)


def provider_lines(
    session: ListenSession, program: str, *, transcribe: bool = True, reply: bool = True, speak: bool = True
) -> list[str]:
    """The boot lines that name each provider stage (SF2 stage 1 of 0.5.7).

    Each says which plugin runs the stage and which model, then where it
    runs, which key it read and which package offered it (`_where`).
    `emet-listen` labels them `stt`, `llm` and `voice` and writes `, model
    M`; `emet-talk` labels them `words`, `answers` and `voice` and writes
    `, M`. On `emet-listen`'s `stt` line the mode comes straight after the
    provider's name, so a space follows the name and never a comma:
    `tools/bank/replay.py` reads the line's second word as the provider and
    refuses any but `mock`. Values start at column 11.
    """
    listen = program == "emet-listen"
    labels = ("stt", "llm", "voice") if listen else ("words", "answers", "voice")
    named = ", model " if listen else ", "
    lines: list[str] = []
    if transcribe:
        stt = session.stt_descriptor
        mode = f" ({'streaming' if stt is not None and stt.streaming else 'batch'})" if listen else ""
        lines.append(_provider_line(session, labels[0], session.stt_name, stt, GROUP_STT, named, mode=mode))
    if reply:
        lines.append(_provider_line(session, labels[1], session.chat_name, session.llm_descriptor, GROUP_LLM, named))
    if speak:
        tts = session.tts_descriptor
        rate = f" ({tts.sample_rate} Hz)" if tts is not None else ""
        lines.append(_provider_line(session, labels[2], session.tts_name, tts, GROUP_TTS, named, rate=rate))
    return lines


def failure_text(
    session: ListenSession, failure: Failure, program: str, utterance: Utterance, reply: ReplyDone | None
) -> str:
    """The line `program` prints for a turn's failure row.

    Its `{detail}` is the failed stage's own sentence, written whole by the
    plugin: the reply's `error` for the language model (or, for a wake the
    model could not answer, the detail it was found unavailable with), the
    final transcript's `error` for the transcriber. Nothing here reads the
    sentence; the row was chosen before it was printed.
    """
    detail: str | None = None
    if failure.stage == "llm":
        detail = (reply.error if reply is not None else None) or session.chat_unavailable
    elif failure.stage == "stt" and utterance.transcript is not None:
        detail = utterance.transcript.error
    names = {"stt": session.stt_name, "llm": session.chat_name, "voice": session.tts_name}
    return failure.text(program, name=names.get(failure.stage) or "", detail=detail or "", cap=session.reply_cap)


def print_failure(text: str, failure: Failure) -> None:
    """A failure line, on the stream its row names."""
    print(text, file=sys.stderr if failure.stream == "stderr" else sys.stdout, flush=True)


class StatsLines:
    """What `--stats` prints under each wake and at the end of each turn,
    and, with `--events`, the same as records in the events file.

    The per-wake line is the session's `on_handover`; the per-turn line is
    the turn's last, from its `TurnRecord`. `emet-listen` indents both as
    notes, `emet-talk` puts them in brackets. A wake or turn record is
    written only with the line it repeats, and the console prints the same
    with the file or without it. A record holds every field of its
    `TurnRecord` or `Handover`, so the file keeps figures the line rounds
    or leaves out, as `emet_engine.events` says.
    """

    def __init__(self, program: str, *, stats: bool, events: Any = None) -> None:
        self.listen = program == "emet-listen"
        self.stats = stats
        self.events = events
        self._confidence: float | None = None
        self._wakes = 0

    def _show(self, text: str) -> str:
        return f"    {text}" if self.listen else f"  ({text})"

    def wake(self, event: WakeEvent) -> None:
        """Count the wake and note its score, which its events record carries."""
        self._wakes += 1
        self._confidence = event.confidence

    def handover(self, handover: Handover) -> None:
        print(self._show(handover.text()), flush=True)
        if self.events is not None:
            self.events.wake(handover, self._confidence, number=self._wakes)

    def turn(self, record: TurnRecord | None) -> None:
        line = record.line() if self.stats and record is not None else None
        if line is None:
            return
        print(self._show(line), flush=True)
        if self.events is not None:
            self.events.turn(record)


def add_events_flag(parser: argparse.ArgumentParser) -> None:
    """`--events PATH`, shared by `emet-listen` and `emet-talk`."""
    parser.add_argument(
        "--events",
        metavar="PATH",
        help="with --stats, also write what it prints to PATH as JSON records, one a "
        "line: a header naming the packages and the platform, a record for each "
        "line under a wake and each turn's line, and the footer's numbers. Numbers "
        "and verdicts only, never a word anybody said. The file is written afresh "
        "each run",
    )


def events_refused(args: argparse.Namespace, program: str) -> bool:
    """`--events` without `--stats`, refused on stderr: the file writes what
    `--stats` prints, so on its own it would stay empty."""
    if getattr(args, "events", None) and not args.stats:
        print(f"{program}: --events writes what --stats prints; add --stats", file=sys.stderr)
        return True
    return False


def open_events(path: str | None, program: str) -> Any:
    """The events file `--events` names, opened afresh, or None without the
    flag. A file that cannot be written is the writer's refusal, an
    `EngineError` naming the program, the path and the reason, printed
    before anything boots.

    The writer is imported here, for a run that asked for one: a run
    without the flag never loads it.
    """
    if not path:
        return None
    from emet_engine.events import EventWriter

    return EventWriter(path, program)


def _isatty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def live_line(session: ListenSession, *, stats: bool) -> Iterator[LiveLine]:
    """The live line under `--stats` at a terminal (BVB3 stage 1 of 0.5.7),
    for the length of the run.

    Built once. The session's `on_frame` draws it, and its `on_wake` notes
    each wake's score after the program's own line. For the run
    `sys.stdout` is the line's guarded stdout, so every other write clears
    it first, and so is `sys.stderr` when stderr is a terminal too, since
    logging's last resort reads `sys.stderr` at each record. Both are put
    back at exit, after the line is cleared. Off a terminal or without
    `--stats` nothing is drawn or wrapped, and the session computes no
    level.
    """
    live = LiveLine(
        sys.stdout,
        patience_ms=session.patience_ms,
        enabled=stats,
        err=sys.stderr if _isatty(sys.stderr) else None,
    )
    if not live.drawing:
        yield live
        return
    heard = session.on_wake

    def on_wake(event: WakeEvent) -> None:
        if heard is not None:
            heard(event)
        live.wake(event)

    session.on_wake = on_wake
    session.on_frame = live.frame
    saved = sys.stdout, sys.stderr
    sys.stdout = live.out  # type: ignore[assignment]
    if live.err is not None:
        sys.stderr = live.err  # type: ignore[assignment]
    try:
        yield live
    finally:
        live.close()
        sys.stdout, sys.stderr = saved


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
    """Print what the robot was told, word for word, for `--explain`."""
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
    """The boot's warnings, on stderr before it listens: what the state
    file said, then each notice a provider gave at boot (a model's
    retirement date, a chat model that cannot answer), one `  ! ` line
    each."""
    state = session.body_state
    for warning in (state.warnings if state is not None else []):
        print(f"  ! {warning}", file=sys.stderr)
    for notice in session.notices:
        print(notice if notice.startswith("  ! ") else f"  ! {notice}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    print(identity_line("emet-listen"), flush=True)
    # The board as the run found it, before the models load. The footer
    # reads it again; nothing reads it in between.
    board_start = board.read_board() if args.stats else None
    events = open_events(getattr(args, "events", None), "emet-listen")
    try:
        return await _listen(args, board_start, events)
    finally:
        if events is not None:
            events.close()


async def _listen(args: argparse.Namespace, board_start: board.BoardReading | None, events: Any) -> int:
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
    out = StatsLines("emet-listen", stats=args.stats, events=events)
    session = ListenSession(
        manifest,
        soul,
        registry=registry,
        transcribe=transcribe,
        reply=reply,
        speak=args.speak,
        on_wake=lambda event: (_on_wake(event), out.wake(event)),
        on_handover=out.handover if args.stats else None,
        on_partial=caption.show if transcribe else None,
        on_extended=_on_extended(caption) if transcribe else None,
        chains=getattr(args, "chains", None) or (),
        state=getattr(args, "state", None),
    )
    with live_line(session, stats=args.stats):
        return await _listen_to(session, args, board_start, events, caption=caption, out=out, loaded=loaded)


async def _listen_to(
    session: ListenSession,
    args: argparse.Namespace,
    board_start: board.BoardReading | None,
    events: Any,
    *,
    caption: Caption,
    out: StatsLines,
    loaded: Any,
) -> int:
    reply = args.reply or args.speak
    transcribe = args.transcribe or reply
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
        lines += provider_lines(session, "emet-listen", transcribe=transcribe, reply=reply, speak=args.speak)
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
                await _print_turn(session, utterance, args, reply=reply, out=out)
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
        print_run_footer(session, stats=args.stats, board_start=board_start, events=events)
    return 0


async def _print_heard(session: ListenSession, utterance: Utterance, *, echo: bool) -> None:
    """What followed the wake, played back with `--echo`, and the words the
    transcriber made of it."""
    if utterance.had_speech:
        print(f"    then {utterance.duration_ms / 1000:.1f}s of speech, ended on {utterance.reason.value}")
        if echo:
            await session.say(utterance.audio)
            print("    played it back")
    elif utterance.reason is EndReason.SOURCE_ENDED:
        print("    the recording ended before anything followed.")
    else:
        print("    then nothing. probably a false wake.")
    transcript = utterance.transcript
    if transcript is None:
        return
    if transcript.text:
        print(f"    said {transcript.text!r}")
    elif not transcript.error:
        # Words that never arrived are the failure line's to say.
        print("    said nothing the provider could make out")
    if transcript.text and transcript.error:
        print(f"    and may be cut short: {transcript.error}")


async def _print_turn(
    session: ListenSession, utterance: Utterance, args: argparse.Namespace, *, reply: bool, out: StatsLines
) -> None:
    """One turn, printed as its notes: what was heard, the reply, the turn's
    end as the session found it, and under `--stats` the turn's own line.

    The session ends every turn (`finish_turn`), with or without `--reply`:
    it names the outcome, picks the failure row, and with `--speak` says
    the row's line, as `emet-talk` does. The failure line comes after the
    reply, then the sentences spoken and lost, then the line said aloud.
    """
    await _print_heard(session, utterance, echo=args.echo)
    transcript = utterance.transcript
    done: ReplyDone | None = None
    spoken: list[Any] = []
    if reply and transcript is not None and transcript.text.strip():
        done, spoken = await _print_reply(session, transcript.text, speak=args.speak)
    end: TurnEnd = await session.finish_turn(utterance, done)
    if end.failure is not None:
        print_failure(failure_text(session, end.failure, "emet-listen", utterance, done), end.failure)
    _print_spoken(spoken if done is not None and args.speak else None, end)
    out.turn(session.last_turn)


def _said(spoken: list[Any]) -> int:
    """The reply's sentences the speaker played; a filler for a tag was
    heard and is not one."""
    return sum(1 for s in spoken if s.ok and s.audio_bytes and s.from_reply)


def _print_spoken(spoken: list[Any] | None, end: TurnEnd) -> None:
    """The reply's sentences said, when a reply was spoken; every sentence
    the voice lost, the reply's and the line's; and the line said aloud."""
    if spoken is not None:
        print(f"    spoke {_said(spoken)} sentence(s)")
    for lost in (s for s in (*(spoken or ()), *end.spoken) if not s.ok):
        print(f"    the voice could not say {lost.text!r}: {lost.error}")
    if end.line:
        print(f"    spoke the line: {end.line}")


async def _print_reply(session: ListenSession, text: str, *, speak: bool = False) -> tuple[ReplyDone | None, list[Any]]:
    """Stream the reply to the console as the model produces it, and with
    `speak`, out of the speaker as each sentence completes.

    The `    reply: ` tag is printed with the first word, so a reply that
    failed before any text leaves no empty tag above its failure line.
    Returns the reply's end and what was said of it; the failure line, if
    any, is the turn's end to print.
    """
    started = False
    done: ReplyDone | None = None
    events = session.answer_aloud(text) if speak else session.answer_acted(text)
    async for event in events:
        if isinstance(event, TextDelta) and event.text:
            if not started:
                print("    reply: ", end="", flush=True)
                started = True
            print(event.text, end="", flush=True)
        elif isinstance(event, ReplyDone):
            done = event
            if started:
                print()
            if event.stop_reason == "refusal":
                print("    (the provider declined to answer)")
    return done, (list(session.last_spoken) if speak else [])


def print_run_footer(
    session: ListenSession, *, stats: bool, board_start: board.BoardReading | None = None, events: Any = None
) -> None:
    """Report what the run cost, if asked, and always report what it lost.

    Frames dropped while the loop was deliberately not reading (playback, a
    reply, the wait for a final transcript) are explained; frames dropped
    while it was listening are blamed on the loop. The session charges each
    frame to one or the other as it is lost (`SessionStats.dropped_busy`).
    Which flags the run had said nothing reliable about it: `--transcribe`
    alone waits for every final and was never counted busy. Shared by
    `emet-listen` and `emet-talk`.

    `board_start` is the board as the run found it; with `stats`, the board
    is read a second time here and both readings go above the verdict.
    `events`, the `--events` writer, gets the footer's record as the
    footer is printed.
    """
    if stats:
        board_end = board.read_board() if board_start is not None else None
        lines = board.footer_lines(board_start, board_end) if board_start is not None else []
        # `live` from the source that actually ran, not from the flag: only a
        # real sound card can drift, and claiming otherwise for a file would
        # be inventing a measurement.
        live = session.source_name != "wav"
        print("\n" + session.stats.report(live=live, board=lines))
        if events is not None:
            events.footer(session.stats, live=live, board_start=board_start, board_end=board_end)
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
    ignored = int(getattr(session, "own_voice_wakes", 0) or 0)
    if ignored:
        print(
            f"\nnote: {ignored} wake(s) were ignored because the phrase ended in the tail of "
            f"the robot's own voice, still in the air as it listened again."
        )
    dropped = int(session.dropped or 0)
    charged = getattr(session, "stats", None)
    busy = min(dropped, int(getattr(charged, "dropped_busy", 0) or 0))
    if busy:
        print(
            f"\nnote: {busy} frame(s) were dropped while the robot was speaking, "
            f"thinking or waiting for the words. The loop does not read the "
            f"microphone during playback, a reply, or the wait for a final "
            f"transcript; barge-in, in 1.0, is what changes that."
        )
    if dropped - busy:
        print(
            f"\nwarning: {dropped - busy} frame(s) were dropped while it was listening, "
            f"so wake words may have been missed. The loop is not keeping up with the audio."
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
        "the 0.3 acceptance criteria is actually checked. With a transcriber, "
        "also a line under each wake that handed audio over: its lag, the end "
        "of the name written as silence, and what the level stop alone would "
        "have written. Above the verdict, the board's temperature, throttle "
        "flags and load average at the start and at the end, as far as the "
        "machine reports them",
    )
    add_events_flag(parser)
    add_body_flags(parser)
    args = parser.parse_args(argv)
    if events_refused(args, "emet-listen"):
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
