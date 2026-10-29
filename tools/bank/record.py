#!/usr/bin/env python3
"""Prompt and record one sitting of the voice bank on the reference body.

Run on the Pi over SSH from the laptop, with a terminal (`ssh -t`), from a
plan in `tools/bank/plans/`. For each file of the plan it starts the
recorder itself, shows each prompt in large text when the recording reaches
that prompt's sample, writes the take's slot and text into the file's
manifest as it goes (`tools/bank/FORMAT.md`), and hashes the file when the
recorder closes it.

Why each rule holds:

    a fixed -d      `arecord -d N` ends the recording by itself. In 0.3 a
                    SIGINT from `timeout` made arecord abort mid-read and
                    leave a bad file (RELEASE_PROCESS.md section 10), so the
                    recorder is never signalled: it runs in a session of its
                    own, a Ctrl-C at the terminal reaches only the prompter,
                    and the prompter waits for -d to run out.
    a 5 s lead      a clip made for --replay needs at least 5 s of room
                    before its first phrase, because the wake decoder first
                    updates its mean about 3 s in (the 0.5 row of the same
                    ledger).
    6 s and 8 s     a prompt every 6 s holds the name and a question; a
                    pause take holds up to 2.5 s more, so it gets 8 s. One
                    long take (a paragraph, talk, room tone, a room's
                    response) fills one slot of the length its plan gives.
    100 s at most   a spoiled file costs little.
    the first       prompts are placed in samples from the recording's first
    sample          sample, estimated from the file as the recorder writes
                    it, so a slot in the manifest is where the prompt
                    appeared in the audio, whatever the SSH link's delay.
    no cue          a sound would be in the recording. The prompter writes
                    no bell character and starts no player.
    retakes         `r` marks the take on screen as fluffed and `p` the one
                    before it. The take keeps its slot and gains an
                    exclusion with its reason, and its prompt goes again at
                    the end of the file, into a slot the plan held back for
                    it. A retake that finds no slot left is recorded at the
                    same place in a file of its own, straight after, whose
                    `made_with` names the takes it says again (manifest.py
                    lets `retake_of` name a take of the same file only).
    consent         a real run refuses a speaker whom the bank's `bank.json`
                    does not list with a consent file that exists.
    the lock        a real run holds the body runner's ~/emet-ci/lock for
                    the whole sitting, in the runner's format, and refuses
                    to start while anyone else holds it: the runner never
                    plays into a bank recording, nor a sitting into its run.
    append-only     a file whose recording or manifest exists is never
                    recorded again, so running a plan a second time resumes
                    it where it stopped.

How the first sample is estimated: the recorder writes the file a block at a
time, so `now - samples_on_disk / 16000` is never earlier than the true
start and comes closest to it just after a write. The estimate is the
smallest such value seen, and never earlier than the moment the recorder was
launched. How far arecord's blocks lag on the Pi is unmeasured until the
maintainer's 30 s trial, which `label.py` can check against the takes'
onsets.

`--dry-run` records nothing and opens no device: a stub writes a silent wav
of the requested length, a virtual clock runs the schedule at once, and the
schedule is printed while the manifests are written as in a real run.

Usage:
    python3 tools/bank/record.py PLAN [--bank ~/voicebank] [--only ID,...] [--from ID]
    python3 tools/bank/record.py PLAN --dry-run [--bank DIR] [--dry-keys FILE:SLOT:KEY,...]
    python3 tools/bank/record.py PLAN --check
    python3 tools/bank/record.py PLAN --close FILE_ID [--bank ~/voicebank]
Exit:   0 every file recorded or checked, 1 a problem (printed), 2 a refusal
        before anything was recorded, 130 stopped at the terminal.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as bank_files  # noqa: E402  the bank's one reader and writer, beside this file

PLAN_FORMAT = "emet-bank-plan/1"
BANK_FORMAT = "emet-bank/1"

RATE = 16000
CHANNELS = 1
BITS = 16
#: Bytes before the first sample in the canonical PCM wav header that
#: arecord writes. Only the start estimate uses it, and 44 bytes is 22
#: samples, so a longer header would move the estimate by under 2 ms.
HEADER_BYTES = 44

MAX_FILE_S = 100
MIN_LEAD_S = 5
EVERY_S = 6
PAUSE_EVERY_S = 8
#: After the last slot, so `-d` is a whole number of seconds with the last
#: words inside the file.
TAIL_S = 1
#: How often the prompter looks at the clock, the file and the keys.
POLL_S = 0.005
#: How long after a pause take's first part the prompter shows HOLD, unless
#: the plan's phrase says otherwise: about the time "hey emet, I want to"
#: takes to say.
PAUSE_SAY_S = 1.5

#: FORMAT.md's kinds of a take and of a recording.
TAKE_KINDS = ("wake_pause", "one_breath", "number", "short", "decoy", "pause",
              "long", "talk", "room_tone", "room_response")
RECORDING_KINDS = ("takes", "talk", "room_tone", "soak")
#: A take of these kinds fills one slot whose length the plan sets.
LONG_KINDS = frozenset({"long", "talk", "room_tone", "room_response"})
#: Takes that must not wake the robot.
NO_WAKE_KINDS = frozenset({"decoy", "talk", "room_tone", "room_response"})

DEFAULT_DEVICE = "plughw:3,0"
#: The recorder, as RELEASE_PROCESS.md section 10 has it: a fixed -d.
RECORDER = ("arecord", "-D", "{device}", "-f", "S16_LE", "-r", str(RATE), "-c", "1",
            "-d", "{seconds}", "{path}")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: A speaker is an id and never a name (FORMAT.md).
SPEAKER_RE = re.compile(r"^s[0-9]{2}$")

#: The line above each prompt, by the take's kind.
INSTRUCTIONS = {
    "wake_pause": "the name, a short pause, then the sentence",
    "one_breath": "in one breath: no pause after the name",
    "number": "in one breath: no pause after the name",
    "short": "in one breath: no pause after the name",
    "decoy": "say only this. It must not wake the robot",
    "pause": "read the first part, hold while HOLD shows, then read the rest",
    "long": "the name, then read the paragraph in one go",
    "talk": "talk as asked. Never say the name",
    "room_tone": "nobody speaks. Keep still",
    "room_response": "play the sweep on your phone at mouth height, then keep still",
}

KEYS_LINE = "r: say this one again at the end    p: the one before    Ctrl-C: stop (the recording runs to its end)"
EXCLUDE_REASON = "marked a retake at the prompter"


class PlanError(Exception):
    """A plan that cannot be recorded as written. The message lists every problem."""


@dataclass(frozen=True)
class PlanTake:
    phrase: str
    kind: str
    text: str
    question: str | None
    first_sound: str | None
    expect_wake: bool
    pause_s: float | None = None
    say_s: float | None = None


@dataclass
class PlanFile:
    id: str
    kind: str
    at: str
    where: dict
    every_s: float
    lead_s: float
    retake_slots: int
    takes: list[PlanTake]
    say: str | None = None
    #: A retake file's takes, each with the id of the take it says again.
    entries: list["Entry"] | None = None

    @property
    def slots(self) -> int:
        return len(self.takes) + self.retake_slots

    @property
    def seconds(self) -> int:
        return int(round(self.lead_s + self.every_s * self.slots + TAIL_S))


@dataclass
class Plan:
    id: str
    title: str
    speaker: str
    device: str
    positions: dict
    files: list[PlanFile]
    path: Path | None = None

    @property
    def seconds(self) -> int:
        return sum(f.seconds for f in self.files)

    def rooms(self) -> set[str]:
        return {f.where["room"] for f in self.files}


def _every_for(kinds: set[str], every: float | None, problems: list[str], fid: str) -> float:
    """The slot length a file's takes allow: 6 s, 8 s with a pause take, and
    a longer one only for a file of one long kind."""
    want = PAUSE_EVERY_S if "pause" in kinds else EVERY_S
    if every is None:
        if kinds & LONG_KINDS:
            problems.append(f"{fid}: a file of {sorted(kinds & LONG_KINDS)} takes names its every_s")
            return want
        return want
    if kinds & LONG_KINDS:
        if len(kinds) != 1:
            problems.append(f"{fid}: a long take ({sorted(kinds & LONG_KINDS)}) shares a file with {sorted(kinds - LONG_KINDS)}")
        return float(every)
    if float(every) != want:
        problems.append(f"{fid}: every_s {every}; takes of {sorted(kinds)} are prompted every {want} s")
    return float(every)


def parse_plan(doc: dict, path: Path | None = None) -> Plan:
    """Check a plan and expand its phrases into takes. Every rule in the
    module docstring that a plan can break is checked here, before any
    device opens."""
    problems: list[str] = []
    if doc.get("format") != PLAN_FORMAT:
        raise PlanError(f"format is {doc.get('format')!r}; this prompter reads {PLAN_FORMAT!r}")
    pid = doc.get("id", "")
    if not ID_RE.match(str(pid)):
        problems.append(f"plan id {pid!r} is not lowercase letters, digits and hyphens")
    speaker = doc.get("speaker", "")
    if not SPEAKER_RE.match(str(speaker)):
        problems.append(f"speaker {speaker!r} is not an id like s01: a speaker is an id and never a name")
    device = doc.get("device", DEFAULT_DEVICE)
    lead = float(doc.get("lead_s", MIN_LEAD_S))
    retake_default = int(doc.get("retake_slots", 1))
    positions = doc.get("positions") or {}
    for key, pos in positions.items():
        for need in ("room", "distance"):
            if not pos.get(need):
                problems.append(f"position {key}: no {need}")
    phrases = doc.get("phrases") or {}
    for key, ph in phrases.items():
        kind = ph.get("kind")
        if kind not in TAKE_KINDS:
            problems.append(f"phrase {key}: kind {kind!r} is not one of FORMAT.md's {TAKE_KINDS}")
        if kind == "pause":
            if "..." not in ph.get("text", ""):
                problems.append(f"phrase {key}: a pause take marks its pause with '...'")
            if not isinstance(ph.get("pause_s"), (int, float)):
                problems.append(f"phrase {key}: a pause take names its pause_s")
    files: list[PlanFile] = []
    seen: set[str] = set()
    for i, fd in enumerate(doc.get("files") or []):
        fid = fd.get("id", f"files[{i}]")
        if not ID_RE.match(str(fid)):
            problems.append(f"{fid}: a file id is lowercase letters, digits and hyphens")
        if fid in seen:
            problems.append(f"{fid}: two files have this id")
        seen.add(fid)
        at = fd.get("at")
        if at not in positions:
            problems.append(f"{fid}: position {at!r} is not in the plan's positions")
            continue
        kind = fd.get("kind", "takes")
        if kind not in RECORDING_KINDS:
            problems.append(f"{fid}: kind {kind!r} is not one of FORMAT.md's {RECORDING_KINDS}")
        takes: list[PlanTake] = []
        for ref in fd.get("takes") or []:
            ph = phrases.get(ref)
            if ph is None:
                problems.append(f"{fid}: phrase {ref!r} is not in the plan's phrases")
                continue
            tkind = ph.get("kind")
            takes.append(PlanTake(
                phrase=ref, kind=tkind, text=ph.get("text", ""), question=ph.get("question"),
                first_sound=ph.get("first_sound"),
                expect_wake=bool(ph.get("expect_wake", tkind not in NO_WAKE_KINDS)),
                pause_s=ph.get("pause_s"), say_s=ph.get("say_s")))
        if not takes:
            problems.append(f"{fid}: no takes")
            continue
        file_lead = float(fd.get("lead_s", lead))
        if file_lead < MIN_LEAD_S:
            problems.append(f"{fid}: lead {file_lead} s; a replay clip needs at least {MIN_LEAD_S} s of room first")
        every = _every_for({t.kind for t in takes}, fd.get("every_s"), problems, fid)
        retakes = int(fd.get("retake_slots", 0 if {t.kind for t in takes} & LONG_KINDS else retake_default))
        for t in takes:
            if t.kind == "pause" and t.pause_s is not None:
                say = t.say_s if t.say_s is not None else PAUSE_SAY_S
                if say + t.pause_s + 1.5 > every:
                    problems.append(f"{fid}: {t.phrase} holds {t.pause_s} s after {say} s, too long for a {every} s slot")
        pf = PlanFile(id=fid, kind=kind, at=at, where=dict(positions[at], position=at),
                      every_s=every, lead_s=file_lead, retake_slots=retakes, takes=takes, say=fd.get("say"))
        exact = pf.lead_s + pf.every_s * pf.slots + TAIL_S
        if abs(exact - round(exact)) > 1e-9:
            problems.append(f"{fid}: {exact} s is not a whole number of seconds, which arecord's -d needs")
        if pf.seconds > MAX_FILE_S:
            problems.append(f"{fid}: {pf.seconds} s; a file lasts at most {MAX_FILE_S} s")
        files.append(pf)
    if not files and not problems:
        problems.append("the plan has no files")
    if problems:
        raise PlanError("\n".join(problems))
    return Plan(id=pid, title=doc.get("title", pid), speaker=speaker, device=device,
                positions=positions, files=files, path=path)


def load_plan(path: Path) -> Plan:
    return parse_plan(json.loads(Path(path).read_text(encoding="utf-8")), Path(path))


# ---------------------------------------------------------------- the big font

#: A 5 by 7 dot font, one glyph a string, rows split by "/". Large enough to
#: read from about 3 m on a laptop screen at a normal terminal size; a printed
#: sheet (`--check` prints one) covers what the screen cannot.
FONT = {
    "A": ".###./#...#/#...#/#####/#...#/#...#/#...#",
    "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".###./#...#/#..../#..../#..../#...#/.###.",
    "D": "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E": "#####/#..../#..../####./#..../#..../#####",
    "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".###./#...#/#..../#.###/#...#/#...#/.####",
    "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I": ".###./..#../..#../..#../..#../..#../.###.",
    "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "L": "#..../#..../#..../#..../#..../#..../#####",
    "M": "#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#",
    "N": "#...#/#...#/##..#/#.#.#/#..##/#...#/#...#",
    "O": ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".####/#..../#..../.###./....#/....#/####.",
    "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#.#.#/#.#.#/#.#.#/.#.#.",
    "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z": "#####/....#/...#./..#../.#.../#..../#####",
    "0": ".###./#...#/#..##/#.#.#/##..#/#...#/.###.",
    "1": "..#../.##../..#../..#../..#../..#../.###.",
    "2": ".###./#...#/....#/...#./..#../.#.../#####",
    "3": "#####/...#./..#../...#./....#/#...#/.###.",
    "4": "...#./..##./.#.#./#..#./#####/...#./...#.",
    "5": "#####/#..../####./....#/....#/#...#/.###.",
    "6": "..##./.#.../#..../####./#...#/#...#/.###.",
    "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9": ".###./#...#/#...#/.####/....#/...#./.##..",
    " ": "...../...../...../...../...../...../.....",
    ",": "...../...../...../...../.##../..#../.#...",
    ".": "...../...../...../...../...../.##../.##..",
    "?": ".###./#...#/....#/...#./..#../...../..#..",
    "!": "..#../..#../..#../..#../..#../...../..#..",
    "'": "..#../..#../.#.../...../...../...../.....",
    "-": "...../...../...../#####/...../...../.....",
    "+": "...../..#../..#../#####/..#../..#../.....",
    ":": "...../.##../.##../...../.##../.##../.....",
    "(": "...#./..#../.#.../.#.../.#.../..#../...#.",
    ")": ".#.../..#../...#./...#./...#./..#../.#...",
}
GLYPH_W = 6  # five dots and a gap


def big(text: str, width: int) -> list[str]:
    """The text in the dot font, wrapped at words to `width` columns. A
    character the font lacks is drawn as a space."""
    per_line = max(1, width // GLYPH_W)
    lines: list[str] = []
    cur = ""
    for word in text.upper().split():
        while len(word) > per_line:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:per_line])
            word = word[per_line:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= per_line:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    out: list[str] = []
    for n, line in enumerate(lines):
        if n:
            out.append("")
        rows = [""] * 7
        for ch in line:
            glyph = FONT.get(ch, FONT[" "]).split("/")
            for r in range(7):
                rows[r] += glyph[r].replace("#", "█").replace(".", " ") + " "
        out.extend(r.rstrip() for r in rows)
    return out


# ---------------------------------------------------------------- what the prompter talks to

class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)


class VirtualClock:
    """The dry run's clock: sleeping moves it on at once."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)


class NoKeys:
    """No key is ever pressed; every question between files is answered Enter."""

    def poll(self, file_id: str, slot: int) -> str | None:
        return None

    def ask(self, prompt: str) -> str:
        return "go"


class ScriptedKeys(NoKeys):
    """Keys pressed while a given slot of a given file is on screen, each
    once: {(file_id, slot_index): key}. The dry run's --dry-keys and the
    tests use it."""

    def __init__(self, presses: dict[tuple[str, int], str], answers: list[str] | None = None) -> None:
        self.presses = dict(presses)
        self.answers = list(answers or [])

    def poll(self, file_id: str, slot: int) -> str | None:
        return self.presses.pop((file_id, slot), None)

    def ask(self, prompt: str) -> str:
        return self.answers.pop(0) if self.answers else "go"


class TerminalKeys:
    """Single keys from the SSH terminal, without waiting for Enter. The
    terminal is put in cbreak mode, which keeps Ctrl-C as an interrupt, and
    restored on exit."""

    def __enter__(self) -> "TerminalKeys":
        self.fd = sys.stdin.fileno()
        if os.name == "posix":
            import termios
            import tty
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc: object) -> None:
        if os.name == "posix":
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def _read(self, timeout: float | None) -> str | None:
        if os.name == "posix":
            import select
            ready, _, _ = select.select([self.fd], [], [], timeout)
            return os.read(self.fd, 1).decode(errors="replace") if ready else None
        import msvcrt  # the laptop, for trying the screen out
        end = None if timeout is None else time.monotonic() + timeout
        while not msvcrt.kbhit():
            if end is not None and time.monotonic() >= end:
                return None
            time.sleep(0.01)
        return msvcrt.getwch()

    def poll(self, file_id: str, slot: int) -> str | None:
        return self._read(0)

    def ask(self, prompt: str) -> str:
        print(prompt, flush=True)
        while True:
            ch = self._read(None)
            if ch in ("\r", "\n"):
                return "go"
            if ch in ("s", "q"):
                return {"s": "skip", "q": "quit"}[ch]


class Screen:
    """The SSH terminal: one prompt at a time, cleared between prompts."""

    CLEAR = "\x1b[2J\x1b[H"

    def __init__(self, out=None, plain: bool = False, width: int | None = None) -> None:
        self.out = out or sys.stdout
        self.plain = plain
        self.width = width

    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except OSError:
            pass  # the SSH session has gone; the recorder runs on regardless

    def _cols(self) -> int:
        return self.width or shutil.get_terminal_size((100, 30)).columns

    def show(self, f: PlanFile, slot: int, entry: "Entry | None", part: int, pos: int) -> None:
        head = f"{f.id}   prompt {slot + 1} of {f.slots}   {where_line(f)}   {pos / RATE:5.1f} s"
        if entry is None:
            words, instruction = "nothing", "keep still: this slot was kept for a retake"
        else:
            t = entry.take
            words = prompt_words(t, part)
            instruction = INSTRUCTIONS[t.kind] + ("   (again)" if entry.retake_of else "")
        body = words if self.plain else "\n".join(big(words, self._cols()))
        self._write(f"{self.CLEAR}{head}\n\n{instruction}\n\n{body}\n\n{KEYS_LINE}\n")

    def note(self, text: str) -> None:
        self._write(text + "\n")


class DryScreen:
    """The dry run's screen: one line a prompt, which is the schedule."""

    def __init__(self, out=None) -> None:
        self.out = out or sys.stdout

    def show(self, f: PlanFile, slot: int, entry: "Entry | None", part: int, pos: int) -> None:
        if entry is None:
            line = "(kept for a retake, unused)"
        else:
            t = entry.take
            line = f"{t.kind:<13} {prompt_words(t, part)}"
            if entry.retake_of:
                line += f"   (again: {entry.retake_of})"
        print(f"  {pos / RATE:6.1f} s  {pos:>8}  {slot + 1:02d}.{part}  {line}", file=self.out)

    def note(self, text: str) -> None:
        print(text, file=self.out)


def prompt_words(t: PlanTake, part: int) -> str:
    """A pause take shows its first part, then HOLD, then the rest."""
    if t.kind != "pause":
        return t.text
    first, _, rest = t.text.partition("...")
    return {1: first.strip(), 2: "HOLD", 3: rest.strip()}[part]


def where_line(f: PlanFile) -> str:
    w = f.where
    bits = [w["room"], w["distance"]]
    if w.get("facing"):
        bits.append(f"facing {w['facing']}")
    if w.get("noise"):
        bits.append(f"noise: {w['noise']}")
    return ", ".join(bits)


# ---------------------------------------------------------------- recording one file

@dataclass
class Entry:
    take: PlanTake
    retake_of: str | None = None


@dataclass
class FileResult:
    id: str
    manifest: Path
    wav: Path
    sha256: str | None = None
    takes: int = 0
    marked: int = 0
    leftover: list[Entry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stopped: bool = False
    unprompted: int = 0
    peak_dbfs: float | None = None
    clipped: int = 0


class Stopped(Exception):
    """Ctrl-C or a dropped SSH session while prompting."""


class StartEstimate:
    """When the recording's first sample was captured, on the prompter's clock."""

    def __init__(self, wav: Path, launched: float) -> None:
        self.wav = wav
        self.launched = launched
        self.best: float | None = None

    def update(self, now: float) -> None:
        try:
            size = os.stat(self.wav).st_size
        except OSError:
            return
        n = (size - HEADER_BYTES) // 2
        if n > 0:
            cand = now - n / RATE
            if self.best is None or cand < self.best:
                self.best = cand

    @property
    def t0(self) -> float:
        return self.launched if self.best is None else max(self.launched, self.best)

    def position(self, now: float) -> int:
        return int(round((now - self.t0) * RATE))


def recorder_command(template, device: str, seconds: int, path: Path) -> list[str]:
    return [part.format(device=device, seconds=seconds, path=str(path)) for part in template]


def write_json(path: Path, doc: dict) -> None:
    """Replace the file whole, so a reader never sees half a manifest. A
    manifest in progress has no hash yet, so `manifest.write`, which checks
    first, writes only the closed one; this lays it out the same way."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(bank_files.dump(doc), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _num(x: float) -> float | int:
    return int(x) if float(x).is_integer() else x


def read_capture_level(device: str) -> str | None:
    """The card's capture controls as amixer prints them, for the manifest.
    amixer reads the mixer only and opens no stream."""
    m = re.match(r"^(?:plug)?hw:(?:CARD=)?(\w+)", device)
    if not m or not shutil.which("amixer"):
        return None
    try:
        out = subprocess.run(["amixer", "-c", m.group(1), "scontents"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    parts, name = [], None
    for line in out.splitlines():
        hit = re.match(r"^Simple mixer control '(.+)',\d+", line)
        if hit:
            name = hit.group(1)
        elif name and re.match(r"^\s+(Mono|Front Left|Front Right):", line):
            parts.append(f"{name}: {line.split(':', 1)[1].strip()}")
    return "; ".join(parts) or None


def manifest_head(f: PlanFile, plan: Plan, device: str, level: str | None,
                  started: datetime.datetime) -> dict:
    w = f.where
    return {
        "format": BANK_FORMAT,
        "id": f.id,
        "path": f"audio/{f.id}.wav",
        "sha256": None,
        "sample_rate": RATE, "channels": CHANNELS, "bits": BITS,
        "frames": None, "duration_s": None,
        "recorded": started.date().isoformat(),
        "start_time": started.isoformat(timespec="seconds"),
        "device": device, "capture_level": level,
        "room": w["room"], "position": w["position"],
        "distance": w["distance"], "distance_m": w.get("distance_m"), "facing": w.get("facing"),
        "speaker": plan.speaker, "noise": w.get("noise"),
        "kind": f.kind,
        "lead_s": _num(f.lead_s), "prompt_every_s": _num(f.every_s),
        "made_with": f"tools/bank/record.py, plan {plan.id}" + (
            "; says again " + ", ".join(sorted({str(e.retake_of) for e in f.entries})) if f.entries else ""),
        "takes": [],
    }


def manifest_text(t: PlanTake) -> str:
    """The prompt as the manifest keeps it. A pause take's held pause is
    written into its text, as `[hold 1.5 s]` where the plan has `...`,
    since a take has no field for it; label.py drops the brackets before it
    compares words."""
    if t.kind != "pause":
        return t.text
    first, _, rest = t.text.partition("...")
    return f"{first.strip()} [hold {t.pause_s:g} s] {rest.strip()}"


def take_record(f: PlanFile, n: int, entry: Entry, start: int) -> dict:
    """A take as FORMAT.md has it. `retake_of` names only a take of this
    recording, as manifest.py requires, so a take said again in a retake
    file of its own is linked by that file's `made_with` instead."""
    t = entry.take
    same_file = entry.retake_of if entry.retake_of and entry.retake_of.startswith(f.id + "/") else None
    return {
        "id": f"{f.id}/{n:02d}",
        "kind": t.kind,
        "text": manifest_text(t),
        "question": t.question,
        "first_sound": t.first_sound,
        "slot": {"start": start, "end": None},
        "expect_wake": t.expect_wake,
        "retake_of": same_file,
        "exclude": None,
    }


def close_wav(path: Path) -> tuple[str, int, float, int]:
    """sha256, frames, peak in dBFS and samples at full scale, after a check
    that the recorder wrote what the bank holds."""
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, CHANNELS, BITS // 8):
            raise ValueError(f"{path.name}: {w.getframerate()} Hz, {w.getnchannels()} channel(s), "
                             f"{8 * w.getsampwidth()} bits; the bank holds {RATE} Hz mono 16-bit")
        frames = w.getnframes()
        x = array("h")
        x.frombytes(w.readframes(frames))
    if sys.byteorder == "big":
        x.byteswap()
    peak = max((max(x), -min(x))) if len(x) else 0
    peak_db = 20 * math.log10(peak / 32768) if peak else float("-inf")
    clipped = x.count(32767) + x.count(-32768)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, frames, peak_db, clipped


def record_file(f: PlanFile, plan: Plan, bank: Path, *, clock, screen, keys, recorder,
                popen=subprocess.Popen, device: str, level: Callable[[str], str | None],
                today: str, wall: Callable[[], datetime.datetime]) -> FileResult:
    wav = bank / "audio" / f"{f.id}.wav"
    man = bank / "manifests" / f"{f.id}.json"
    res = FileResult(id=f.id, manifest=man, wav=wav)
    for p in (wav, man):
        if p.exists():
            res.problems.append(f"{f.id}: {p} exists; the bank is append-only, so this file is not recorded again")
            return res
    for d in (wav.parent, man.parent, bank / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    cmd = recorder_command(recorder, device, f.seconds, wav)
    doc = manifest_head(f, plan, device, level(device), wall())
    log = open(bank / "logs" / f"{f.id}.txt", "w", encoding="utf-8")
    log.write(f"command: {' '.join(cmd)}\nlaunched: {doc['start_time']}\n")
    log.flush()
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": log}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # a Ctrl-C at the terminal never reaches the recorder
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    launched = clock.now()
    proc = popen(cmd, **kwargs)
    est = StartEstimate(wav, launched)
    queue = list(f.entries) if f.entries else [Entry(t) for t in f.takes]
    shown: dict[int, dict] = {}
    marked: set[str] = set()
    write_json(man, doc)

    def mark(slot: int) -> None:
        rec = shown.get(slot)
        if rec is None or rec["id"] in marked:
            return
        marked.add(rec["id"])
        rec["exclude"] = {"reason": EXCLUDE_REASON, "date": today}
        queue.append(Entry(rec["_entry"].take, retake_of=rec["id"]))
        res.marked += 1
        write_json(man, public(doc))
        screen.note(f"marked {rec['id']}: it is asked again at the end")

    def wait_until(sample: int, current: int) -> int:
        while True:
            now = clock.now()
            est.update(now)
            pos = est.position(now)
            key = keys.poll(f.id, current)
            if key == "r":
                mark(current)
            elif key == "p":
                earlier = [s for s in shown if s < current]
                if earlier:
                    mark(max(earlier))
            if pos >= sample:
                return pos
            clock.sleep(max(0.0, min(POLL_S, est.t0 + sample / RATE - now)))

    def public(d: dict) -> dict:
        return dict(d, takes=[{k: v for k, v in t.items() if k != "_entry"} for t in d["takes"]])

    last: dict | None = None
    n = 0
    try:
        for k in range(f.slots):
            start = int(round((f.lead_s + f.every_s * k) * RATE))
            pos = wait_until(start, k - 1)
            if last is not None:
                last["slot"]["end"] = pos
                last = None
            entry = queue[k] if k < len(queue) else None
            if entry is not None:
                n += 1
                rec = take_record(f, n, entry, pos)
                rec["_entry"] = entry
                doc["takes"].append(rec)
                shown[k] = rec
                last = rec
            write_json(man, public(doc))
            screen.show(f, k, entry, 1, pos)
            if entry is not None and entry.take.kind == "pause":
                say = entry.take.say_s if entry.take.say_s is not None else PAUSE_SAY_S
                wait_until(start + int(round(say * RATE)), k)
                screen.show(f, k, entry, 2, est.position(clock.now()))
                wait_until(start + int(round((say + entry.take.pause_s) * RATE)), k)
                screen.show(f, k, entry, 3, est.position(clock.now()))
        end = int(round((f.lead_s + f.every_s * f.slots) * RATE))
        pos = wait_until(end, f.slots - 1)
        if last is not None:
            last["slot"]["end"] = pos
    except (KeyboardInterrupt, Stopped):
        res.stopped = True
        if last is not None:
            last["slot"]["end"] = est.position(clock.now())
        screen.note(f"stopped: the recorder runs to its fixed {f.seconds} s and is never signalled; waiting for it")
    write_json(man, public(doc))
    res.takes = len(doc["takes"])
    res.leftover = queue[f.slots:]
    res.unprompted = len(queue[:f.slots]) - res.takes
    try:
        code = proc.wait(timeout=f.seconds + 30)
    except subprocess.TimeoutExpired:
        res.problems.append(f"{f.id}: the recorder still runs {f.seconds + 30} s after its start; "
                            "it is left alone, and the file is not hashed")
        log.close()
        return res
    except (KeyboardInterrupt, Stopped):
        res.stopped = True
        res.problems.append(f"{f.id}: stopped while waiting; the recorder finishes on its own, and the "
                            f"file is not hashed: close it with --close {f.id}")
        log.close()
        return res
    log.write(f"exit: {code}\nfirst sample: {1000 * (est.t0 - launched):.1f} ms after launch (estimated)\n")
    log.close()
    if code != 0:
        res.problems.append(f"{f.id}: the recorder exited {code}; its output is in logs/{f.id}.txt")
    try:
        digest, frames, peak, clipped = close_wav(wav)
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        res.problems.append(f"{f.id}: {exc}")
        return res
    doc["sha256"] = digest
    doc["frames"] = frames
    doc["duration_s"] = round(frames / RATE, 6)
    res.sha256, res.peak_dbfs, res.clipped = digest, peak, clipped
    try:
        bank_files.write(man, public(doc), "manifest")
    except bank_files.BankError as exc:
        write_json(man, public(doc))
        res.problems.extend(f"{f.id}: {p}" for p in exc.problems)
        return res
    res.problems.extend(add_to_index(bank, f"manifests/{f.id}.json", today))
    return res


def add_to_index(bank: Path, rel: str, today: str) -> list[str]:
    """List a closed manifest in bank.json, dated today, when the bank has
    an index; a dry run's new bank has none."""
    index = bank / "bank.json"
    if not index.exists():
        return []
    try:
        doc = bank_files.read(index, "bank")
        if rel not in doc["files"]:
            doc["files"].append(rel)
            doc["version"] = today
            bank_files.write(index, doc, "bank")
    except bank_files.BankError as exc:
        return [f"bank.json: {p}" for p in exc.problems]
    return []


def close_open(bank: Path, file_id: str, today: str) -> list[str]:
    """Close a manifest the prompter left open when it died before the
    recorder finished: the last slot ends where the file does, and the hash
    and length come from the file. The audio is read, never changed."""
    man = bank / "manifests" / f"{file_id}.json"
    doc = json.loads(man.read_text(encoding="utf-8"))
    if doc.get("sha256"):
        return [f"{file_id}: closed already"]
    digest, frames, _peak, _clipped = close_wav(bank / doc["path"])
    for t in doc["takes"]:
        if t["slot"]["end"] is None:
            t["slot"]["end"] = frames
    doc.update(sha256=digest, frames=frames, duration_s=round(frames / RATE, 6))
    try:
        bank_files.write(man, doc, "manifest")
    except bank_files.BankError as exc:
        return [f"{file_id}: {p}" for p in exc.problems]
    return add_to_index(bank, f"manifests/{file_id}.json", today)


def retake_files(f: PlanFile, entries: list[Entry], round_no: int) -> list[PlanFile]:
    """The takes left over after a file, recorded at the same place in files
    of their own, as many to a file as fit in 100 s."""
    out: list[PlanFile] = []
    rest = list(entries)
    part = 0
    while rest:
        part += 1
        fit = max(1, int((MAX_FILE_S - f.lead_s - TAIL_S) // f.every_s) - f.retake_slots)
        chunk, rest = rest[:fit], rest[fit:]
        suffix = f"-retake{round_no}" + (f"{chr(ord('a') + part - 1)}" if part > 1 or rest else "")
        out.append(PlanFile(id=f.id + suffix, kind=f.kind, at=f.at, where=f.where, every_s=f.every_s,
                            lead_s=f.lead_s, retake_slots=f.retake_slots, takes=[e.take for e in chunk],
                            say=f"retakes of {f.id}", entries=chunk))
    return out


# ---------------------------------------------------------------- the sitting

@dataclass
class SittingResult:
    files: list[FileResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    stopped: bool = False

    @property
    def problems(self) -> list[str]:
        return [p for r in self.files for p in r.problems]


def lock_path() -> Path:
    """The body runner's lock (tools/body/run.py). A sitting holds it, so the
    runner never opens the microphone or plays through the speaker into a
    bank recording, and a sitting never starts during the runner's run."""
    return Path.home() / "emet-ci" / "lock"


def lock_holder(path: Path) -> list[str] | None:
    """The lines of the lock when it is held, else None."""
    try:
        return path.read_text(encoding="utf-8").splitlines() or ["(an empty lock file)"]
    except FileNotFoundError:
        return None


def take_lock(path: Path, now: datetime.datetime) -> tuple[str | None, list[str]]:
    """(token, []) when the lock was free and is now ours, or (None, the
    holder's lines). The file is made only when none exists, as the runner's
    `set -C` makes it, and holds the same three lines."""
    token = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return None, lock_holder(path) or []
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"token {token}\nby tools/bank/record.py\nstarted {now.isoformat(timespec='seconds')}\n")
    return token, []


def release_lock(path: Path, token: str) -> bool:
    """Remove the lock only when it holds this token, as the runner does."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    if f"token {token}" not in lines:
        return False
    path.unlink()
    return True


def check_consent(plan: Plan, bank: Path) -> list[str]:
    """A real run records only a speaker whom bank.json lists with a consent
    file that is not empty (decided 2026-10-27, D7), as check.py holds it."""
    index = bank / "bank.json"
    if not index.exists():
        return [f"{index} does not exist; the bank's index lists each speaker and their consent file"]
    try:
        doc = bank_files.read(index, "bank")
    except bank_files.BankError as exc:
        return [f"{index}: {p}" for p in exc.problems]
    sp = doc["speakers"].get(plan.speaker)
    if not sp:
        return [f"{index}: speaker {plan.speaker} is not listed; nothing of theirs is recorded without consent"]
    consent = bank / sp["consent"]
    if not consent.is_file() or not consent.read_text(encoding="utf-8").strip():
        return [f"speaker {plan.speaker}'s consent file {consent} is missing or empty"]
    return []


def run_sitting(plan: Plan, bank: Path, *, clock, screen, keys, recorder=RECORDER,
                popen=subprocess.Popen, device: str | None = None,
                level: Callable[[str], str | None] = read_capture_level,
                only: set[str] | None = None, start_from: str | None = None,
                today: str | None = None,
                wall: Callable[[], datetime.datetime] | None = None) -> SittingResult:
    device = device or plan.device
    today = today or datetime.date.today().isoformat()
    wall = wall or (lambda: datetime.datetime.now().astimezone())
    out = SittingResult()
    files = plan.files
    if start_from:
        ids = [f.id for f in files]
        files = files[ids.index(start_from):]
    if only:
        files = [f for f in files if f.id in only]
    for i, f in enumerate(files):
        if (bank / "manifests" / f"{f.id}.json").exists():
            screen.note(f"{f.id}: recorded already, skipped")
            out.skipped.append(f.id)
            continue
        answer = keys.ask(f"\nnext: {f.id}, {f.seconds} s, {len(f.takes)} takes at {where_line(f)}"
                          + (f". {f.say}" if f.say else "")
                          + f"\nEnter starts it, s skips it, q ends the sitting ({i + 1} of {len(files)})")
        if answer == "quit":
            break
        if answer == "skip":
            out.skipped.append(f.id)
            continue
        pending = [f]
        round_no = 0
        while pending:
            cur = pending.pop(0)
            res = record_file(cur, plan, bank, clock=clock, screen=screen, keys=keys,
                              recorder=recorder, popen=popen, device=device, level=level,
                              today=today, wall=wall)
            out.files.append(res)
            report_file(res, screen)
            if res.stopped:
                out.stopped = True
                out.left.extend(str(e.retake_of) for e in res.leftover)
                return out
            if res.leftover:
                round_no += 1
                more = retake_files(cur, res.leftover, round_no)
                ans = keys.ask(f"{len(res.leftover)} retake(s) left over from {cur.id}. Stay where you are: "
                               f"Enter records them now ({', '.join(m.id for m in more)}), s leaves them")
                if ans == "go":
                    pending.extend(more)
                else:
                    out.left.extend(str(e.retake_of) for e in res.leftover)
    return out


def report_file(res: FileResult, screen) -> None:
    if res.sha256:
        peak = "silent" if res.peak_dbfs == float("-inf") else f"peak {res.peak_dbfs:.1f} dBFS"
        screen.note(f"{res.id}: {res.takes} take(s), {res.marked} marked for a retake, {peak}, "
                    f"{res.clipped} samples at full scale, sha256 {res.sha256[:12]}")
    if res.unprompted:
        screen.note(f"  {res.unprompted} take(s) of {res.id} were never prompted; a later plan asks them")
    for p in res.problems:
        screen.note(f"  problem: {p}")


def summary(plan: Plan) -> str:
    """The plan's files, takes and minutes, for --check and the dry run."""
    takes = sum(len(f.takes) for f in plan.files)
    moves = sum(1 for a, b in zip(plan.files, plan.files[1:]) if a.at != b.at)
    secs = plan.seconds
    by_kind: dict[str, int] = {}
    for f in plan.files:
        for t in f.takes:
            by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
    kinds = ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items()))
    return (f"{plan.id}: {len(plan.files)} files, {takes} takes ({kinds}), "
            f"{secs // 60} min {secs % 60} s of audio, {moves} move(s) between positions")


def write_silence(seconds: str, path: str) -> int:
    """The dry run's recorder: a silent wav of the requested length, written
    whole and then moved into place, so the start estimate sees all or
    nothing."""
    p = Path(path)
    tmp = p.with_name(p.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(BITS // 8)
        w.setframerate(RATE)
        w.writeframes(bytes(2 * RATE * int(seconds)))
    os.replace(tmp, p)
    return 0


def parse_dry_keys(text: str) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for item in filter(None, (s.strip() for s in text.split(","))):
        fid, slot, key = item.rsplit(":", 2)
        out[(fid, int(slot) - 1)] = key
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--write-silence"]:
        return write_silence(argv[1], argv[2])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path)
    ap.add_argument("--bank", type=Path, help="the bank's root (default ~/voicebank; a new temporary directory for --dry-run)")
    ap.add_argument("--device", help=f"the capture device (default: the plan's, else {DEFAULT_DEVICE})")
    ap.add_argument("--only", help="record only these file ids, comma-separated")
    ap.add_argument("--from", dest="start_from", help="start at this file id")
    ap.add_argument("--plain", action="store_true", help="prompts in ordinary text")
    ap.add_argument("--check", action="store_true", help="check the plan and print it as a sheet; record nothing")
    ap.add_argument("--dry-run", action="store_true", help="the stub recorder and a virtual clock: print the schedule, write the manifests")
    ap.add_argument("--dry-keys", default="", help="for --dry-run: FILE:PROMPT:KEY,... keys pressed at those prompts")
    ap.add_argument("--recorder", help="the recorder command as a JSON list with {device}, {seconds} and {path}")
    ap.add_argument("--close", metavar="ID", help="close a manifest the prompter left open: hash the file, end the last slot")
    args = ap.parse_args(argv)
    try:
        plan = load_plan(args.plan)
    except (OSError, ValueError, PlanError) as exc:
        print(f"record: {args.plan}:\n{exc}", file=sys.stderr)
        return 2
    print(summary(plan))
    if args.check:
        for f in plan.files:
            print(f"\n{f.id}  ({f.seconds} s, {where_line(f)})" + (f"\n      {f.say}" if f.say else ""))
            for n, t in enumerate(f.takes, 1):
                print(f"  {n:2d}  {manifest_text(t)}")
        return 0
    if args.close:
        bank = (args.bank or Path("~/voicebank")).expanduser()
        found = close_open(bank, args.close, datetime.date.today().isoformat())
        for p in found:
            print(f"problem: {p}", file=sys.stderr)
        return 1 if found else 0
    only = set(args.only.split(",")) if args.only else None
    recorder = json.loads(args.recorder) if args.recorder else list(RECORDER)
    if args.dry_run:
        bank = args.bank or Path(tempfile.mkdtemp(prefix="emet-bank-dry-"))
        bank.mkdir(parents=True, exist_ok=True)
        print(f"dry run: nothing is recorded; manifests go to {bank}; consent is not checked")
        if not args.recorder:
            recorder = [sys.executable, str(Path(__file__).resolve()), "--write-silence", "{seconds}", "{path}"]
        res = run_sitting(plan, bank, clock=VirtualClock(), screen=DryScreen(),
                          keys=ScriptedKeys(parse_dry_keys(args.dry_keys)), recorder=recorder,
                          device=args.device, level=lambda d: None, only=only, start_from=args.start_from)
    else:
        bank = (args.bank or Path("~/voicebank")).expanduser()
        refusals = check_consent(plan, bank)
        if not sys.stdin.isatty():
            refusals.append("no terminal for the keys: run it with ssh -t")
        if not shutil.which(recorder[0]):
            refusals.append(f"{recorder[0]} is not on PATH")
        need = plan.seconds * RATE * 2
        if bank.exists() and shutil.disk_usage(bank).free < 2 * need:
            refusals.append(f"{bank} has under twice the {need // 1_000_000} MB the plan needs")
        missing = plan.rooms() - set(_bank_rooms(bank))
        lock = lock_path()
        holder = lock_holder(lock)
        if holder is not None:
            refusals.append(f"{lock} is held: " + " / ".join(holder))
        if refusals:
            for r in refusals:
                print(f"record: {r}", file=sys.stderr)
            return 2
        token, holder = take_lock(lock, datetime.datetime.now().astimezone())
        if token is None:
            print(f"record: {lock} is held: " + " / ".join(holder), file=sys.stderr)
            return 2
        if missing:
            print(f"note: bank.json has no record of {sorted(missing)}; add one before check.py runs")
        if hasattr(signal, "SIGHUP"):
            # A dropped SSH session stops the prompting the way Ctrl-C does;
            # the recorder, in its own session, runs to its end.
            def _hup(*_: object) -> None:
                raise Stopped()
            signal.signal(signal.SIGHUP, _hup)
        try:
            with TerminalKeys() as keys:
                res = run_sitting(plan, bank, clock=RealClock(), screen=Screen(plain=args.plain), keys=keys,
                                  recorder=recorder, device=args.device, only=only, start_from=args.start_from)
        finally:
            release_lock(lock, token)
    print(f"\n{len(res.files)} file(s) written, {len(res.skipped)} skipped, "
          f"{sum(r.takes for r in res.files)} takes, {sum(r.marked for r in res.files)} marked for a retake")
    if res.left:
        print(f"not retaken: {', '.join(res.left)}")
    for p in res.problems:
        print(f"problem: {p}", file=sys.stderr)
    if res.stopped:
        return 130
    return 1 if res.problems else 0


def _bank_rooms(bank: Path) -> list[str]:
    try:
        return list(bank_files.read(bank / "bank.json", "bank")["rooms"])
    except bank_files.BankError:
        return []


if __name__ == "__main__":
    raise SystemExit(main())
