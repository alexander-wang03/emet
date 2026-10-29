#!/usr/bin/env python3
"""Run the body's checks on the reference body over SSH, from the laptop.

The agent, or the maintainer by hand, runs this on the laptop. It drives the
reference body, a Raspberry Pi 5 reached as `emet-pi`, and brings the
evidence home:

    preflight   uptime, load, disk, temperature and throttling, recorded;
                it refuses while an emet-listen, emet-talk, arecord, aplay
                or the bank's record.py runs on the body, while a sound
                card's PCM is open, or while ~/emet-ci/lock exists, so the
                runner never takes the robot from the maintainer
    sync        the code under test into ~/emet-ci/emet: the laptop's
                working tree (`--tree`), or a pushed commit fetched
                anonymously from the public repository (`--commit REF`)
    install     the release process's install line into ~/emet-ci/.venv,
                again whenever a pyproject.toml has changed
    suites      the four packages' tests, collected, passed and skipped
                counted per package
    checks      tools/check_layering.py, release_check.py, check_style.py
    bank        the voice bank's replays, tools/bank/replay.py over
                ~/voicebank/
    speaker     the real speaker with no microphone: --replay with --echo,
                --replay with --speak, and emet-talk --replay
    live        the microphone: a plain run of 60 s, and --stats for ten
                minutes with nobody speaking
    collect     the run's text files to releases/vX.Y.Z/body/<date>/,
                body_log_check.py on each typescript, and the date each body
                flag of RELEASE_PROCESS.md §4 last ran, read from the logs

**The speaker and the microphone** are used only inside a window the
maintainer allowed in a reply beforehand (his decision of 2026-10-27), named
with `--window START END`. Each such step refuses when it would start before
the window or end after it, and when any minute of it falls between 22:00
and 08:00 unless `--allow-night` is given. The steps keep no audio on disk
and send none off the body: the runner's manifest gives every model stage to
a mock (or the voice to local Piper with `--voice piper`), `--keys` names an
empty file, and after each step the runner looks for an audio file written
since the step began and fails the step if it finds one. `--state` names a
fresh copy of the carried state, so the maintainer's own file is read and
never written; its hash is compared before and after.

**What it never does.** It changes no git state on either machine, apart from
its own bare clone at ~/emet-ci/repo.git. It never touches the maintainer's
checkout or venv on the body (~/Documents/emet). It never reads or prints a
keys file, and every command on the body starts by unsetting each EMET_
variable, so a key exported in a shell there cannot reach a run, and neither
can EMET_LIVE_TESTS or the CI-only EMET_VIRTUAL_AUDIO. An unreachable body
is reported as "body run not done", with exit code 2.

Standard library only: Python 3.12 or newer on the laptop, and on the body
bash, Python 3 with venv, git, util-linux's `script` and coreutils'
`timeout`. `ssh` must reach the body without a password
(`ssh -o BatchMode=yes emet-pi true`).

Usage:
    python tools/body/run.py --release 0.5.5 --tree
    python tools/body/run.py --release 0.5.5 --commit 1a2b3c4
    python tools/body/run.py --release 0.5.5 --tree --window 2026-10-28T14:00 2026-10-28T15:00
    python tools/body/run.py --release 0.5.5 --collect-only --fetch '~/sitting-0.5.5.log'
Exit:   0 every step that ran passed; 1 a step failed; 2 the body run was
        not done: no answer over SSH, refused at preflight, or the code could
        not be synced or installed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

REPO = Path(__file__).resolve().parents[2]
#: `Project Emet/`, where the maintainer keeps `releases/` beside the repo.
TOP = REPO.parent

HOST = "emet-pi"
#: BatchMode fails at once instead of asking for a password nobody is there
#: to type; ConnectTimeout bounds the wait for a body that is off.
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
UPSTREAM = "https://github.com/alexander-wang03/emet"

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")
#: RELEASE_PROCESS.md §3's install line, the same on every machine.
INSTALL = ("emet-sdk[dev]", "emet-hal[dev,audio,wake]", "emet-providers[dev]", "emet-engine[dev]")
CHECKS = ("check_layering.py", "release_check.py", "check_style.py")
STEPS = ("sync", "install", "suites", "checks", "bank", "speaker", "live")
HARDWARE_STEPS = ("speaker", "live")

NIGHT_STARTS = dt.time(22, 0)
NIGHT_ENDS = dt.time(8, 0)
PLAIN_S = 60
STATS_S = 600
#: Seconds for emet-listen to boot before its audio clock starts, so a
#: `--stats` run stopped by `timeout` still reads ten minutes of audio.
BOOT_S = 20
#: A replay ends by itself; this bounds one that does not.
REPLAY_LIMIT_S = 300
#: Added to each hardware step's bound before it is held against the window.
MARGIN_S = 30

MANIFEST = "~/pi-speakerphone.yaml"
SOUL = "emet-sdk/examples/emet-soul.yaml"
SPEAKER_WAV = "~/one-breath.wav"
BANK = "~/voicebank"
LOG_CHECK = TOP / "releases" / "v0.5.4" / "body_log_check.py"

#: What the body runs with on the speaker and the microphone. The body takes
#: every model stage over, so the soul's vendors are never reached; the mock
#: transcriber says a fixed line and records nothing (`record_turns` is
#: absent), and the mock voice plays the words' bytes as clicks.
MODELS: dict[str, dict[str, Any]] = {
    "stt": {"provider": "mock", "params": {"transcript": "testing the speaker"}},
    "chat": {"provider": "mock", "params": {"reply": "This is the body runner, checking the speaker."}},
    "tts": {"provider": "mock"},
}
#: `--voice piper`: the reference voice, local and free, through the `piper`
#: extra in the runner's venv. It reads the voice the maintainer downloaded.
PIPER_TTS = {"provider": "piper", "model": "en_US-ljspeech-medium"}

AUDIO_SUFFIXES = (".wav", ".raw", ".pcm", ".flac", ".ogg", ".mp3", ".opus")

#: Every command on the body starts here. The EMET_ names that were set are
#: kept for the preflight record, names only, and then unset. Temporary
#: files and pip's cache go inside ~/emet-ci, so the runner writes nowhere
#: else on the body.
PRELUDE = r"""set -u
EMET_SET=$(env | sed -n 's/^\(EMET_[A-Za-z0-9_]*\)=.*/\1/p' | tr '\n' ' ')
for name in $EMET_SET; do unset "$name"; done
CI="$HOME/emet-ci"
PY="$CI/.venv/bin/python"
export PYTHONDONTWRITEBYTECODE=1 GIT_TERMINAL_PROMPT=0 TMPDIR="$CI/tmp" PIP_CACHE_DIR="$CI/cache/pip"
mkdir -p "$TMPDIR"
"""

#: A process that means the robot is in use: emet-listen or emet-talk (the
#: console script, the `script -c` wrapping one, or the module run
#: directly), the voice bank's prompter, or arecord and aplay. A grep for
#: this pattern does not match its own command line.
BUSY = re.compile(r"(?:^|[\s/])(?:emet-listen|emet-talk|arecord|aplay)(?:\s|$)"
                  r"|\bemet_engine\.(?:cli|talk)\b|(?:^|[\s/])tools/bank/record\.py\b")

#: Every PCM substream the kernel knows, open or closed. A closed one's
#: status reads `closed`; an open one's starts `state:` (sound/core/pcm.c,
#: snd_pcm_substream_proc_status_read), whoever opened it, so a recording
#: started any other way is seen too. `{root}` is /proc/asound on the body.
PCM_CHECK = r"""for f in {root}/card*/pcm*/sub*/status; do
  [ -r "$f" ] || continue
  first=$(head -n 1 "$f")
  [ "$first" = "closed" ] || echo "pcm=$f: $first, $(grep -m 1 owner_pid "$f" | tr -s ' ')"
done
"""
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
#: What `--fetch` may copy home: the typescripts the maintainer's body runs
#: and the release sitting write in the home folder, and nothing else. The
#: name has no directory in it, so nothing under ~/.config or ~/.ssh can
#: match, and it ends in .log, so no recording can; a name holding key, env,
#: secret or token is refused besides.
FETCHABLE = re.compile(r"^~/(?:pi|sitting)-[A-Za-z0-9._-]+\.log$")
SECRET = re.compile(r"key|env|secret|token", re.IGNORECASE)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")

#: vcgencmd get_throttled's bits (Raspberry Pi documentation, "get_throttled",
#: verified 2026-10-27), named as emet_engine/board.py names them for the
#: `--stats` footer, so the report and the footer use the same words for one
#: bit. A copy, since this script imports nothing outside the standard
#: library; a test holds the two equal.
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "frequency capped has occurred",
    18: "throttled has occurred",
    19: "soft temperature limit has occurred",
}


class RunnerError(Exception):
    """A step cannot go on, with the reason a person reads."""


class Unreachable(Exception):
    """ssh got no answer from the body: off, asleep, or off the network."""


# --------------------------------------------------------------------------
# Talking to the body
# --------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str
    err: str = ""
    seconds: float = 0.0
    raw: bytes = b""

    def values(self) -> dict[str, list[str]]:
        """The `key=value` lines a script printed, every value of each key."""
        found: dict[str, list[str]] = {}
        for line in self.out.splitlines():
            key, sep, value = line.partition("=")
            if sep and re.fullmatch(r"[a-z_]+", key):
                found.setdefault(key, []).append(value)
        return found

    def value(self, key: str, default: str = "") -> str:
        return (self.values().get(key) or [default])[-1]


class Remote:
    """The body, over ssh. Each call is one ssh session."""

    def __init__(self, host: str) -> None:
        self.host = host

    def _ssh(self, command: str, stdin: bytes, timeout: float) -> Result:
        args = ["ssh", *SSH_OPTIONS, self.host, command]
        start = time.monotonic()
        try:
            done = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise Unreachable("ssh is not on this machine's PATH") from exc
        except subprocess.TimeoutExpired:
            return Result(124, "", f"no answer within {timeout:.0f} s", timeout)
        err = done.stderr.decode("utf-8", "replace")
        if done.returncode == 255:
            # ssh's own failure; no script here exits 255.
            raise Unreachable(err.strip().splitlines()[-1] if err.strip() else "ssh exited 255")
        return Result(done.returncode, done.stdout.decode("utf-8", "replace"), err,
                      time.monotonic() - start, done.stdout)

    def run(self, script: str, *, timeout: float = 600.0) -> Result:
        """Run a bash script on the body, sent on stdin after the prelude."""
        return self._ssh("bash -s", (PRELUDE + script).encode("utf-8"), timeout)

    def send(self, command: str, data: bytes, *, timeout: float = 600.0) -> Result:
        """Run a one-line command on the body with `data` on its stdin."""
        if "\n" in command:
            raise ValueError("send() takes a one-line command")
        return self._ssh(command, data, timeout)


def marked(step: str, script: str) -> str:
    """A script with its step named in its first line, for the record and for
    the tests' stand-in body."""
    return f"# body-run: {step}\n{script}"


def remote_path(path: str, home: str, tree: str) -> str:
    """A path on the body as an absolute path: `~/` is the home directory and
    a relative path is inside the synced tree."""
    if path.startswith("~/"):
        return f"{home}/{path[2:]}"
    if path.startswith("/"):
        return path
    return f"{tree}/{path}"


# --------------------------------------------------------------------------
# The laptop's tree
# --------------------------------------------------------------------------


class Laptop:
    """Read-only questions to the laptop's git. Nothing here writes to the
    index or the refs."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def git(self, *args: str) -> bytes:
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True)
        if done.returncode != 0:
            raise RunnerError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
        return done.stdout

    def config(self, key: str) -> str:
        done = subprocess.run(["git", "-C", str(self.repo), "config", "--get", key], capture_output=True)
        return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""


def as_committed(data: bytes) -> bytes:
    """The bytes git would commit from a file checked out under
    `core.autocrlf=true`: CRLF becomes LF in a text file. Git treats a file
    holding a NUL or a lone CR as binary and leaves it alone (convert.c,
    convert_is_binary), and so does this; git's printable-ratio test is not
    copied, so the comparison with HEAD's blobs is what shows the two agree."""
    if b"\r\n" not in data or b"\0" in data:
        return data
    if data.count(b"\r") != data.count(b"\r\n"):
        return data
    return data.replace(b"\r\n", b"\n")


def blob_id(data: bytes) -> str:
    """Git's object id for a file with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_digest(entries: Iterable[tuple[str, bytes]]) -> str:
    """The sha256 of `sha256sum`'s lines for every file, sorted by path, which
    the body recomputes with find, sort and sha256sum after unpacking."""
    lines = [f"{hashlib.sha256(data).hexdigest()}  {path}\n"
             for path, data in sorted(entries, key=lambda entry: entry[0].encode("utf-8"))]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


#: The body's half of `tree_digest`, run in the unpacked tree. sha256sum
#: marks a path with "  " on Linux and " *" under Git for Windows, where the
#: tests run it, so the sed takes either.
REMOTE_DIGEST = (r"find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum "
                 r"| sed 's|^\([0-9a-f]*\) [ *]\./|\1  |' | sha256sum | cut -c1-64")


@dataclass
class Packed:
    data: bytes
    files: int
    size: int
    digest: str
    converted: int
    blobs: dict[str, str]
    skipped: list[str]


def pack(repo: Path, names: Iterable[str], *, autocrlf: bool, mtime: float) -> Packed:
    """A gzipped tar of the named files as git would commit them."""
    buf = io.BytesIO()
    entries: list[tuple[str, bytes]] = []
    blobs: dict[str, str] = {}
    skipped: list[str] = []
    converted = size = 0
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(set(n for n in names if n)):
            path = repo / name
            if path.is_symlink() or not path.is_file():
                skipped.append(name)  # deleted since it was staged, or not a file
                continue
            data = path.read_bytes()
            if autocrlf:
                fixed = as_committed(data)
                converted += fixed is not data
                data = fixed
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, int(mtime)
            tar.addfile(info, io.BytesIO(data))
            entries.append((name, data))
            blobs[name] = blob_id(data)
            size += len(data)
    return Packed(buf.getvalue(), len(entries), size, tree_digest(entries), converted, blobs, skipped)


def head_blobs(laptop: Laptop) -> dict[str, str]:
    """Path to blob id for every file in HEAD."""
    found: dict[str, str] = {}
    for entry in laptop.git("ls-tree", "-r", "-z", "HEAD").decode("utf-8").split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            found[path] = parts[2]
    return found


# --------------------------------------------------------------------------
# Windows, night and the models
# --------------------------------------------------------------------------


def parse_moment(text: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DDTHH:MM, the laptop's local time") from exc


def at_night(moment: dt.datetime) -> bool:
    clock = moment.time()
    return clock >= NIGHT_STARTS or clock < NIGHT_ENDS


def window_refusal(now: dt.datetime, seconds: float, window: tuple[dt.datetime, dt.datetime] | None,
                   *, allow_night: bool) -> str | None:
    """Why a hardware step of `seconds` may not start now, or None.

    A step that starts at 21:55 and ends at 22:06 is refused as one starting
    at 23:00 is. A step lasts minutes and the night ten hours, so a step with
    any minute between 22:00 and 08:00 has its start or its end there.
    """
    if window is None:
        return "no --window named: the microphone and the speaker are used only inside a window the maintainer allowed"
    start, end = window
    finish = now + dt.timedelta(seconds=seconds)
    if now < start:
        return f"the window opens at {start:%Y-%m-%d %H:%M}; it is {now:%H:%M}"
    if finish > end:
        return (f"the step would end at {finish:%H:%M:%S}, after the window closes at "
                f"{end:%Y-%m-%d %H:%M}")
    if not allow_night:
        for moment in (now, finish):
            if at_night(moment):
                return f"{moment:%H:%M} is between 22:00 and 08:00, and --allow-night was not given"
    return None


def check_models(models: Any) -> None:
    """Refuse a manifest that could send audio off the body or keep it.

    The transcriber and the language model must be the mocks; the voice the
    mock or local Piper. `record_turns` would write each utterance's bytes to
    disk, so it is refused wherever it appears."""
    if not isinstance(models, dict):
        raise RunnerError("the runner's manifest has no models block")
    for stage, allowed in (("stt", {"mock"}), ("chat", {"mock"}), ("tts", {"mock", "piper"})):
        block = models.get(stage)
        provider = block.get("provider") if isinstance(block, dict) else None
        if provider not in allowed:
            raise RunnerError(f"models.{stage} is {provider!r}; the body runner allows only {sorted(allowed)}")
    if "record_turns" in json.dumps(models):
        raise RunnerError("the runner's manifest sets record_turns, which keeps audio on disk")


# --------------------------------------------------------------------------
# Reading what came back
# --------------------------------------------------------------------------


@dataclass
class Counts:
    collected: int
    passed: int
    skipped: int
    failed: int
    seconds: float


def junit_counts(xml_text: str) -> Counts | None:
    """Collected, passed, skipped and failed from pytest's --junitxml."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        return None
    tests = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    seconds = sum(float(s.get("time", 0.0)) for s in suites)
    return Counts(tests, tests - skipped - failed, skipped, failed, seconds)


def throttle_meaning(text: str) -> list[str]:
    match = re.search(r"0x([0-9a-fA-F]+)", text)
    if match is None:
        return ["unread"]
    bits = int(match.group(1), 16)
    return [THROTTLE_BITS.get(bit, f"bit {bit}") for bit in range(bits.bit_length()) if bits >> bit & 1] or ["none"]


def busy_lines(ps_text: str) -> list[str]:
    """The processes that mean the robot is in use."""
    return [line.strip() for line in ps_text.splitlines() if BUSY.search(line)]


def busy_reasons(values: dict[str, list[str]]) -> list[str]:
    """Why the robot is in use, from the preflight's `ps=` and `pcm=` lines."""
    return busy_lines("\n".join(values.get("ps", []))) + [
        f"a sound card is open: {pcm}" for pcm in values.get("pcm", [])]


ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SCRIPT_START = re.compile(r"^Script started on (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
HEADER_COMMAND = re.compile(r'COMMAND="([^"]*)"')
PROMPT_COMMAND = re.compile(r"\$ (\S*\bemet-(?:listen|talk)\b.*)$")
LISTENING = re.compile(r"""^(?:(?P<name>.+?) is )?listening for (?:'[^']*'|"[^"]*")\s*$""")
ENDED = re.compile(r"^(?:stopped|source ended)\.")
AUDIO_SECONDS = re.compile(r"^  frames +\d+ +\((\d+(?:\.\d+)?)s of audio\)")
PROVIDER = re.compile(r"^  (stt|llm|voice|words|answers) +([^\s,]+)")
SOURCE = re.compile(r"^  (?:source +(\S+)|wake +\S+ on ([^\s,]+))")
PROGRAM = re.compile(r"(?:^|[\s/])(emet-listen|emet-talk)(?=\s|$)")


@dataclass
class LoggedRun:
    """One run of emet-listen or emet-talk in a typescript."""
    where: str
    line: int
    date: str | None
    clock: str | None
    program: str
    flags: frozenset[str]
    replay: bool
    ended: bool = False
    audio_s: float | None = None
    providers: dict[str, str] = field(default_factory=dict)
    in_header: bool = True  # until the blank line after the boot header


def command_flags(command: str | None) -> frozenset[str]:
    if not command:
        return frozenset()
    found = PROGRAM.search(command)
    rest = command[found.end():] if found else command
    try:
        words = shlex.split(rest)
    except ValueError:
        words = rest.split()
    return frozenset(w.split("=", 1)[0] for w in words if w.startswith("--"))


def logged_runs(text: str, where: str) -> list[LoggedRun]:
    """Every run in a typescript: its date from script's header, its command
    from the header or from the prompt line above it, and whether it reached
    its end line."""
    # A pty ends each line with CR LF, and a caption's redraw is a lone CR:
    # the line's own CR is dropped and each redraw becomes a line of its own.
    pairs = [(n, piece) for n, line in enumerate(text.split("\n"), 1)
             for piece in ESCAPE.sub("", line).rstrip("\r").split("\r")]
    numbers = [n for n, _ in pairs]
    lines = [piece for _, piece in pairs]
    runs: list[LoggedRun] = []
    date = clock = header = prompt = None
    current: LoggedRun | None = None
    for index, line in enumerate(lines):
        started = SCRIPT_START.match(line)
        if started:
            date, clock = started.group(1), started.group(2)
            found = HEADER_COMMAND.search(line)
            header, prompt, current = (found.group(1) if found else None), None, None
            continue
        typed = PROMPT_COMMAND.search(line)
        if typed and not LISTENING.match(line):
            prompt, current = typed.group(1), None
            continue
        heard = LISTENING.match(line)
        if heard:
            command = header or prompt
            program = "emet-talk" if heard.group("name") else "emet-listen"
            flags = command_flags(command)
            current = LoggedRun(where, numbers[index], date, clock, program, flags, "--replay" in flags)
            runs.append(current)
            header = prompt = None
            continue
        if current is None:
            continue
        if current.in_header:
            # The boot header names the source and the providers; the footer
            # has lines of the same shape ("  stt final ..."), so only the
            # header is read for them.
            if not line.strip():
                current.in_header = False
            elif (named := PROVIDER.match(line)) is not None:
                current.providers[named.group(1)] = named.group(2)
            elif (source := SOURCE.match(line)) is not None:
                current.replay = (source.group(1) or source.group(2)) == "wav"
            continue
        if ENDED.match(line):
            current.ended = True
        elif (seconds := AUDIO_SECONDS.match(line)) is not None:
            current.audio_s = float(seconds.group(1))
    return runs


#: Flags that make a run something other than "a plain run" or "emet-talk
#: with no flags". --keys and --state say where things are, and every runner
#: run names both.
BEHAVIOUR = frozenset({"--replay", "--echo", "--speak", "--reply", "--transcribe", "--stats",
                       "--explain", "--chains"})


@dataclass(frozen=True)
class FlagRow:
    label: str
    program: str
    wants: Callable[[frozenset[str]], bool]
    live_only: bool = False
    replay_only: bool = False
    min_audio_s: float | None = None


#: RELEASE_PROCESS.md §4: "a plain run, `--replay`, `--stats` live to the end
#: of ten minutes, `--echo`, `--transcribe`, `--reply`, `--speak`; and
#: `emet-talk` with no flags". --speak implies --reply, which implies
#: --transcribe (emet-listen's help).
FLAG_ROWS = (
    FlagRow("a plain run", "emet-listen", lambda f: not f & BEHAVIOUR, live_only=True),
    FlagRow("--replay", "emet-listen", lambda f: "--replay" in f, replay_only=True),
    FlagRow("--stats, live, ten minutes", "emet-listen", lambda f: "--stats" in f,
            live_only=True, min_audio_s=float(STATS_S)),
    FlagRow("--echo", "emet-listen", lambda f: "--echo" in f),
    FlagRow("--transcribe", "emet-listen", lambda f: bool(f & {"--transcribe", "--reply", "--speak"})),
    FlagRow("--reply", "emet-listen", lambda f: bool(f & {"--reply", "--speak"})),
    FlagRow("--speak", "emet-listen", lambda f: "--speak" in f),
    FlagRow("emet-talk with no flags", "emet-talk", lambda f: not f & BEHAVIOUR, live_only=True),
)


def latest(runs: Iterable[LoggedRun]) -> LoggedRun | None:
    best: LoggedRun | None = None
    for run in runs:
        if best is None or (run.date, run.clock) > (best.date, best.clock):
            best = run
    return best


def flag_table(runs: list[LoggedRun]) -> list[dict[str, Any]]:
    """Each flag's last run on the body, live and from a file, from the runs
    that booted, reached their end line and carry script's date."""
    usable = [r for r in runs if r.ended and r.date]
    rows = []
    for row in FLAG_ROWS:
        hits = [r for r in usable if r.program == row.program and row.wants(r.flags)
                and (row.min_audio_s is None or (r.audio_s or 0.0) >= row.min_audio_s)]
        live = latest(r for r in hits if not r.replay) if not row.replay_only else None
        replay = latest(r for r in hits if r.replay) if not row.live_only else None
        cited = latest(r for r in (live, replay) if r is not None)
        rows.append({
            "flag": row.label,
            "live": live.date if live else None,
            "replay": replay.date if replay else None,
            "where": f"{cited.where}:{cited.line}" if cited else None,
            "with": ", ".join(f"{k} {v}" for k, v in sorted(cited.providers.items())) if cited else "",
        })
    return rows


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


@dataclass
class Step:
    name: str
    status: str  # passed, failed, not run, refused
    reason: str = ""
    seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


class BodyRun:
    def __init__(self, args: argparse.Namespace, *, remote: Any, laptop: Any,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 log_check: Path = LOG_CHECK, history: list[Path] | None = None) -> None:
        self.args = args
        self.remote = remote
        self.laptop = laptop
        self.clock = clock
        self.log_check = log_check
        self.history = history
        self.started = clock()
        self.stamp = self.started.strftime("%Y%m%d-%H%M%S")
        self.token = uuid.uuid4().hex
        self.steps: list[Step] = []
        self.preflight: dict[str, Any] = {}
        self.source: dict[str, Any] = {}
        self.home = ""
        self.locked = False
        self.verdict = ""
        self.flags: list[dict[str, Any]] = []
        self.out_dir: Path | None = None

    # paths on the body, known once preflight has read $HOME
    @property
    def ci(self) -> str:
        return f"{self.home}/emet-ci"

    @property
    def run_dir(self) -> str:
        return f"{self.ci}/runs/{self.stamp}"

    @property
    def tree(self) -> str:
        return f"{self.ci}/emet"

    def script(self, step: str, body: str) -> str:
        return marked(step, f'RUN={shlex.quote(self.run_dir)}\nmkdir -p "$RUN"\n{body}')

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    # ------------------------------------------------------------ preflight

    PREFLIGHT = r"""
echo "home=$HOME"
echo "date=$(date -Iseconds)"
echo "uptime=$(uptime)"
echo "loadavg=$(cat /proc/loadavg)"
echo "disk=$(df -P "$HOME" | tail -n 1)"
if command -v vcgencmd >/dev/null 2>&1; then
  echo "temp=$(vcgencmd measure_temp 2>&1)"
  echo "throttled=$(vcgencmd get_throttled 2>&1)"
else
  echo "temp=no vcgencmd"
  echo "throttled=no vcgencmd"
fi
echo "kernel=$(uname -srm)"
echo "python=$(python3 --version 2>&1)"
echo "emet_env=$EMET_SET"
ps -eo pid=,args= | sed 's/^ */ps=/'
""" + PCM_CHECK.format(root="/proc/asound") + r"""
if [ -e "$CI/lock" ]; then echo "lock=held"; sed 's/^/lock_line=/' "$CI/lock"; else echo "lock=free"; fi
"""

    def check_busy(self) -> str | None:
        """Why the robot is not free right now, or None."""
        done = self.remote.run(marked("busy", self.PREFLIGHT), timeout=60)
        values = done.values()
        busy = busy_reasons(values)
        if busy:
            return "the robot is in use: " + "; ".join(busy[:3])
        if values.get("lock", ["free"])[-1] == "held" and not self.locked:
            return "~/emet-ci/lock is held: " + " / ".join(values.get("lock_line", []))
        return None

    def run_preflight(self) -> str | None:
        done = self.remote.run(marked("preflight", self.PREFLIGHT), timeout=60)
        if done.code != 0:
            return f"the preflight script exited {done.code}: {done.err.strip()[:200]}"
        values = done.values()
        self.home = done.value("home")
        if not self.home.startswith("/"):
            return "the body did not say where its home directory is"
        throttled = done.value("throttled")
        self.preflight = {
            "date": done.value("date"),
            "uptime": done.value("uptime").strip(),
            "loadavg": done.value("loadavg"),
            "disk": done.value("disk"),
            "temp": done.value("temp"),
            "throttled": throttled,
            "throttled_means": throttle_meaning(throttled),
            "kernel": done.value("kernel"),
            "python": done.value("python"),
            "emet_variables_unset": done.value("emet_env").split(),
            "lock": done.value("lock", "free"),
        }
        busy = busy_reasons(values)
        self.preflight["busy"] = busy
        if busy:
            return "the robot is in use: " + "; ".join(busy[:3])
        if self.preflight["lock"] == "held":
            return "~/emet-ci/lock is held: " + " / ".join(values.get("lock_line", []))
        return None

    def take_lock(self) -> str | None:
        body = (f'mkdir -p "$CI" && ( set -C; printf "token %s\\nby tools/body/run.py\\nstarted %s\\n" '
                f'{self.token} "$(date -Iseconds)" > "$CI/lock" ) 2>/dev/null '
                '&& echo "lock=taken" || { echo "lock=held"; sed "s/^/lock_line=/" "$CI/lock"; }\n')
        done = self.remote.run(marked("lock", body), timeout=60)
        if done.value("lock") != "taken":
            return "~/emet-ci/lock is held: " + " / ".join(done.values().get("lock_line", []))
        self.locked = True
        return None

    def release_lock(self) -> None:
        if not self.locked:
            return
        body = (f'if grep -qx "token {self.token}" "$CI/lock" 2>/dev/null; then rm -f "$CI/lock"; '
                'echo "lock=released"; else echo "lock=not ours"; fi\n')
        try:
            self.remote.run(marked("unlock", body), timeout=60)
            self.locked = False
        except Unreachable:
            pass

    # ------------------------------------------------------------- sync

    def sync(self) -> Step:
        start = time.monotonic()
        if self.args.commit:
            step = self.sync_commit()
        else:
            step = self.sync_tree()
        step.seconds = time.monotonic() - start
        return self.add(step)

    def sync_tree(self) -> Step:
        names = self.laptop.git("ls-files", "-co", "--exclude-standard", "-z").decode("utf-8").split("\0")
        autocrlf = self.laptop.config("core.autocrlf") == "true"
        packed = pack(self.laptop.repo, names, autocrlf=autocrlf, mtime=time.time())
        head = self.laptop.git("rev-parse", "HEAD").decode().strip()
        subject = self.laptop.git("log", "-1", "--format=%s").decode("utf-8", "replace").strip()
        known = head_blobs(self.laptop)
        changed = sorted(p for p, b in packed.blobs.items() if p in known and known[p] != b)
        added = sorted(p for p in packed.blobs if p not in known)
        removed = sorted(p for p in known if p not in packed.blobs)
        self.source = {
            "kind": "working tree", "head": head, "subject": subject, "files": packed.files,
            "bytes": packed.size, "digest": packed.digest, "crlf_converted": packed.converted,
            "changed": changed, "added": added, "removed": removed,
        }
        up = self.remote.send('rm -rf "$HOME/emet-ci/incoming" && mkdir -p "$HOME/emet-ci/incoming" '
                              '&& tar -xzf - -C "$HOME/emet-ci/incoming"', packed.data, timeout=600)
        if up.code != 0:
            return Step("sync", "failed", f"the tree did not unpack on the body: {up.err.strip()[:200]}")
        return self.swap(packed.digest)

    def sync_commit(self) -> Step:
        """Fetch the repository's branches and tags, and take the commit only
        when one of them holds it.

        Pull requests are never fetched. A pull request is anyone's code, and
        the body runs it as the maintainer's user, who can read the keys
        file; a commit reached only through one, or through a ref an older
        clone kept, is refused."""
        ref = self.args.commit
        body = f"""set -e
GIT="git -C $CI/repo.git"
mkdir -p "$CI"
[ -d "$CI/repo.git" ] || git init -q --bare "$CI/repo.git"
$GIT fetch -q --prune --no-tags {UPSTREAM} '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
sha=$($GIT rev-parse --verify --quiet {shlex.quote(ref + '^{commit}')} || $GIT rev-parse --verify --quiet {shlex.quote('refs/remotes/origin/' + ref + '^{commit}')} || true)
[ -n "$sha" ] || {{ echo "unknown=yes"; exit 3; }}
held=$($GIT for-each-ref --contains "$sha" --format='%(refname)' refs/remotes/origin refs/tags | grep -v '^refs/remotes/origin/pull/' || true)
[ -n "$held" ] || {{ echo "unheld=yes"; exit 3; }}
echo "sha=$sha"
echo "subject=$($GIT log -1 --format=%s "$sha")"
rm -rf "$CI/incoming" && mkdir -p "$CI/incoming"
$GIT archive --format=tar "$sha" | tar -x -C "$CI/incoming"
cd "$CI/incoming" && echo "digest=$({REMOTE_DIGEST})"
"""
        done = self.remote.run(marked("fetch", body), timeout=900)
        if done.value("unknown"):
            return Step("sync", "failed", f"{ref} is not a commit the public repository has")
        if done.value("unheld"):
            return Step("sync", "failed", f"{ref} is on no branch or tag of the repository; "
                                          "pull requests are not run on the body")
        if done.code != 0:
            return Step("sync", "failed", f"the fetch exited {done.code}: {done.err.strip()[-200:]}")
        self.source = {"kind": "pushed commit", "ref": ref, "head": done.value("sha"),
                       "subject": done.value("subject"), "upstream": UPSTREAM, "digest": done.value("digest")}
        return self.swap(done.value("digest"))

    def swap(self, digest: str) -> Step:
        record = json.dumps(self.source, sort_keys=True)
        body = f"""cd "$CI/incoming" || exit 3
got=$({REMOTE_DIGEST})
echo "digest=$got"
[ "$got" = {shlex.quote(digest)} ] || exit 4
cd "$CI" && rm -rf "$CI/emet" && mv "$CI/incoming" "$CI/emet"
printf '%s\\n' {shlex.quote(record)} > "$CI/source.json"
echo "swapped=yes"
"""
        done = self.remote.run(marked("swap", body), timeout=300)
        if done.value("swapped") != "yes":
            return Step("sync", "failed", f"the body's digest {done.value('digest')[:12] or 'none'} is not the "
                                          f"tree's {digest[:12]}; the tree was not swapped in")
        return Step("sync", "passed", f"{self.source.get('files', 'all')} files, digest {digest[:12]} on both machines",
                    details=self.source)

    def read_source(self) -> None:
        done = self.remote.run(marked("source", 'cat "$CI/source.json" 2>/dev/null || true\n'), timeout=60)
        try:
            self.source = json.loads(done.out.strip() or "{}")
        except json.JSONDecodeError:
            self.source = {}
        self.source.setdefault("kind", "the tree last synced (no sync this run)")

    # ----------------------------------------------------------- install

    def install(self) -> Step:
        extras = list(INSTALL)
        if self.args.voice == "piper":
            extras[2] = "emet-providers[dev,piper]"
        quoted = " ".join(f"-e {shlex.quote(e)}" for e in extras)
        body = f"""cd "$CI/emet" || exit 3
key=$( {{ printf '%s\\n' {shlex.quote(' '.join(extras))}; cat {' '.join(p + '/pyproject.toml' for p in PACKAGES)}; }} | sha256sum | cut -c1-64)
echo "key=$key"
if [ ! -x "$PY" ]; then python3 -m venv "$CI/.venv" || exit 5; echo "venv=made"; fi
if [ "$(cat "$CI/installed.key" 2>/dev/null)" != "$key" ]; then
  rm -f "$CI/installed.key"
  "$PY" -m pip install --disable-pip-version-check -q {quoted} > "$RUN/install.txt" 2>&1 || {{ echo "pip=failed"; tail -n 5 "$RUN/install.txt" | sed 's/^/tail=/'; exit 6; }}
  echo "$key" > "$CI/installed.key"
  echo "installed=yes"
else
  echo "installed=no"
fi
"$PY" -m pip freeze --disable-pip-version-check > "$RUN/pip-freeze.txt" 2>&1
"$PY" -c "import emet_sdk, emet_hal, emet_providers, emet_engine as e; print('versions=' + ' '.join(m.__version__ for m in (emet_sdk, emet_hal, emet_providers, e)))"
"""
        start = time.monotonic()
        done = self.remote.run(self.script("install", body), timeout=2400)
        seconds = time.monotonic() - start
        if done.code != 0 or not done.value("versions"):
            tail = " / ".join(done.values().get("tail", [])) or done.err.strip()[-200:]
            return self.add(Step("install", "failed", f"the install exited {done.code}: {tail}", seconds))
        how = "installed, a pyproject.toml or the extras changed" if done.value("installed") == "yes" \
            else "kept, no pyproject.toml changed"
        return self.add(Step("install", "passed", f"{how}; versions {done.value('versions')}", seconds,
                             {"key": done.value("key"), "versions": done.value("versions").split(),
                              "extras": extras}))

    # ------------------------------------------------------------ suites

    def suites(self) -> Step:
        counts: dict[str, Any] = {}
        failed: list[str] = []
        start = time.monotonic()
        for package in PACKAGES:
            body = f"""cd "$CI/emet/{package}" || exit 3
s=$(date +%s%N)
timeout 1800 "$PY" -m pytest -q -p no:cacheprovider --junitxml="$RUN/junit-{package}.xml" > "$RUN/suite-{package}.txt" 2>&1
echo "code=$?"
echo "ms=$(( ($(date +%s%N) - s) / 1000000 ))"
tail -n 1 "$RUN/suite-{package}.txt" | sed 's/^/summary=/'
echo "--- junit"
cat "$RUN/junit-{package}.xml" 2>/dev/null
"""
            done = self.remote.run(self.script(f"suite {package}", body), timeout=2000)
            xml_text = done.out.partition("--- junit\n")[2]
            found = junit_counts(xml_text)
            code = int(done.value("code", "1") or 1)
            entry: dict[str, Any] = {"code": code, "summary": done.value("summary"),
                                     "wall_s": round(int(done.value("ms", "0") or 0) / 1000, 1)}
            if found is not None:
                entry.update(collected=found.collected, passed=found.passed, skipped=found.skipped,
                             failed=found.failed, pytest_s=round(found.seconds, 1))
            counts[package] = entry
            if code != 0 or found is None or found.failed:
                failed.append(package)
        seconds = time.monotonic() - start
        line = "; ".join(f"{p.removeprefix('emet-')} {c.get('collected', '?')} collected, "
                         f"{c.get('passed', '?')} passed, {c.get('skipped', '?')} skipped"
                         for p, c in counts.items())
        if failed:
            return self.add(Step("suites", "failed", f"failed: {', '.join(failed)}. {line}", seconds, counts))
        return self.add(Step("suites", "passed", line, seconds, counts))

    def checks(self) -> Step:
        body = 'cd "$CI/emet" || exit 3\n' + "".join(
            f'"$PY" tools/{c} . > "$RUN/check-{c[:-3]}.txt" 2>&1; echo "{c[:-3]}=$?"\n'
            f'tail -n 1 "$RUN/check-{c[:-3]}.txt" | sed "s/^/{c[:-3]}_said=/"\n' for c in CHECKS)
        start = time.monotonic()
        done = self.remote.run(self.script("checks", body), timeout=600)
        results = {c[:-3]: {"code": done.value(c[:-3], "missing"), "said": done.value(c[:-3] + "_said")}
                   for c in CHECKS}
        bad = [name for name, r in results.items() if r["code"] != "0"]
        line = "; ".join(f"{name.removeprefix('check_')} {'ok' if r['code'] == '0' else 'exit ' + r['code']}"
                         for name, r in results.items())
        return self.add(Step("checks", "failed" if bad else "passed", line, time.monotonic() - start, results))

    # -------------------------------------------------------------- bank

    def bank(self) -> Step:
        bank = remote_path(self.args.bank, self.home, self.tree)
        extra = " ".join(shlex.quote(a) for a in self.args.bank_arg)
        body = f"""cd "$CI/emet" || exit 3
if [ ! -f tools/bank/replay.py ]; then echo "missing=tools/bank/replay.py is not in the synced tree"; exit 0; fi
if [ ! -f {shlex.quote(bank)}/bank.json ]; then echo "missing=there is no bank at {bank}"; exit 0; fi
# On the body the recordings sit in the bank's audio/ folder, away from the
# paths the manifests give, which are the laptop's (tools/tests/conftest.py).
set --
if [ -d {shlex.quote(bank)}/audio ]; then set -- --search {shlex.quote(bank)}/audio; fi
timeout 3600 "$PY" tools/bank/replay.py {shlex.quote(bank)} "$@" --report "$RUN/bank-report.json" {extra} > "$RUN/bank-replay.txt" 2>&1
echo "code=$?"
tail -n 5 "$RUN/bank-replay.txt" | sed 's/^/tail=/'
"""
        start = time.monotonic()
        done = self.remote.run(self.script("bank", body), timeout=3700)
        seconds = time.monotonic() - start
        if done.value("missing"):
            return self.add(Step("bank", "not run", done.value("missing"), seconds))
        code = done.value("code", "missing")
        # replay.py's last line is its total; the lines before name files.
        tail = done.values().get("tail", [])
        return self.add(Step("bank", "passed" if code == "0" else "failed",
                             f"replay.py exit {code}: {tail[-1] if tail else 'no output'}", seconds,
                             {"code": code, "tail": tail}))

    # ------------------------------------------------- speaker and microphone

    def hardware_plan(self) -> list[tuple[str, str, list[str], float]]:
        """(step, what, command, bound in seconds) for each hardware step."""
        bin_dir = f"{self.ci}/.venv/bin"
        body = f"{self.run_dir}/body.yaml"
        soul = remote_path(self.args.soul, self.home, self.tree)
        wav = remote_path(self.args.speaker_wav, self.home, self.tree)
        keys = f"{self.run_dir}/empty-keys.env"
        state = f"{self.run_dir}/state"
        tail = ["--keys", keys, "--state", state]
        listen, talk = f"{bin_dir}/emet-listen", f"{bin_dir}/emet-talk"

        def bounded(seconds: int, *command: str) -> list[str]:
            return ["timeout", "-s", "INT", "-k", "30", "--preserve-status", str(seconds), *command]

        plan = []
        if "speaker" in self.wanted:
            plan += [
                ("speaker-echo", "--replay --echo, on the speaker",
                 bounded(REPLAY_LIMIT_S, listen, body, soul, "--replay", wav, "--echo", "--stats", *tail),
                 REPLAY_LIMIT_S),
                ("speaker-speak", "--replay --speak, on the speaker",
                 bounded(REPLAY_LIMIT_S, listen, body, soul, "--replay", wav, "--speak", "--stats", *tail),
                 REPLAY_LIMIT_S),
                ("speaker-talk", "emet-talk --replay, on the speaker",
                 bounded(REPLAY_LIMIT_S, talk, body, soul, "--replay", wav, "--stats", *tail),
                 REPLAY_LIMIT_S),
            ]
        if "live" in self.wanted:
            plan += [
                ("live-plain", "a plain run, 60 s on the microphone",
                 bounded(PLAIN_S + BOOT_S, listen, body, soul, *tail), PLAIN_S + BOOT_S),
                ("live-stats", "--stats, ten minutes on the microphone",
                 bounded(STATS_S + BOOT_S, listen, body, soul, "--stats", *tail), STATS_S + BOOT_S),
            ]
        return plan

    def models(self) -> dict[str, Any]:
        models = json.loads(json.dumps(MODELS))
        if self.args.voice == "piper":
            models["tts"] = dict(PIPER_TTS)
        return models

    def prepare_body(self) -> tuple[str, str]:
        """Write the runner's manifest on the body: the maintainer's manifest
        with every model stage taken over. Returns the body id and the
        carried state's path."""
        models = self.models()
        check_models(models)
        manifest = remote_path(self.args.manifest, self.home, self.tree)
        body = f"""cd "$CI/emet" || exit 3
: > "$RUN/empty-keys.env"
mkdir -p "$RUN/state"
"$PY" - {shlex.quote(manifest)} "$RUN/body.yaml" {shlex.quote(json.dumps(models))} <<'EMET_PY'
import json, sys
from pathlib import Path
import yaml
from emet_engine.state import state_path
source, target = Path(sys.argv[1]), Path(sys.argv[2])
doc = yaml.safe_load(source.read_text(encoding="utf-8"))
doc["models"] = json.loads(sys.argv[3])
target.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
written = yaml.safe_load(target.read_text(encoding="utf-8"))
print("body=" + written["body"]["id"])
print("state=" + str(state_path(written["body"]["id"])))
print("models=" + json.dumps(written.get("models"), sort_keys=True))
EMET_PY
"""
        done = self.remote.run(self.script("prepare", body), timeout=120)
        if done.code != 0 or not done.value("body"):
            raise RunnerError(f"the runner's manifest could not be written: {done.err.strip()[-200:]}")
        check_models(json.loads(done.value("models") or "null"))
        body_id, state = done.value("body"), done.value("state")
        if not re.fullmatch(r"[a-z0-9_]+", body_id) or not state.startswith("/"):
            raise RunnerError(f"the body id {body_id!r} or its state path {state!r} is not usable")
        return body_id, state

    def hardware(self) -> None:
        plan = self.hardware_plan()
        if not plan:
            return
        try:
            body_id, state = self.prepare_body()
        except RunnerError as exc:
            for name, *_ in plan:
                self.add(Step(name, "failed", str(exc)))
            return
        for name, what, command, bound in plan:
            refusal = window_refusal(self.clock(), bound + MARGIN_S, self.args.window,
                                     allow_night=self.args.allow_night)
            if refusal is None:
                refusal = self.check_busy()
            if refusal is not None:
                self.add(Step(name, "refused", refusal, details={"what": what}))
                continue
            self.add(self.hardware_step(name, what, command, body_id, state))

    def hardware_step(self, name: str, what: str, command: list[str], body_id: str, state: str) -> Step:
        mark = f"$RUN/.mark-{name}"
        audio = " -o ".join(f"-iname '*{s}'" for s in AUDIO_SUFFIXES)
        body = f"""cd "$CI/emet" || exit 3
STATE={shlex.quote(state)}
COPY="$RUN/state/{body_id}.json"
rm -f "$COPY"
if [ -f "$STATE" ]; then echo "state_before=$(sha256sum < "$STATE" | cut -c1-64)"; cp "$STATE" "$COPY"; else echo "state_before=none"; fi
mixer() {{ for c in /proc/asound/card[0-9]*; do [ -d "$c" ] || continue; n=${{c##*card}}; echo "card $n"; amixer -c "$n" scontents 2>&1; done; }}
mixer > "$RUN/{name}-mixer-before.txt"
touch "{mark}"
script -qae "$RUN/{name}.typescript" -c {shlex.quote(shlex.join(command))} < /dev/null > /dev/null 2>&1
echo "code=$?"
mixer > "$RUN/{name}-mixer-after.txt"
cmp -s "$RUN/{name}-mixer-before.txt" "$RUN/{name}-mixer-after.txt" && echo "mixer=same" || echo "mixer=changed"
if [ -f "$STATE" ]; then echo "state_after=$(sha256sum < "$STATE" | cut -c1-64)"; else echo "state_after=none"; fi
find "$CI" -newer "{mark}" -type f \\( {audio} \\) -print 2>/dev/null | sed 's/^/audio=/'
find "$HOME" -maxdepth 1 -newer "{mark}" -type f \\( {audio} \\) -print 2>/dev/null | sed 's/^/audio=/'
sed 's/\\x1b\\[[0-9;?]*[A-Za-z]//g' "$RUN/{name}.typescript" | tr '\\r' '\\n' | grep -E -c '^(stopped|source ended)\\.' | sed 's/^/ended=/'
"""
        start = time.monotonic()
        done = self.remote.run(self.script(name, body), timeout=STATS_S + BOOT_S + 300)
        seconds = time.monotonic() - start
        values = done.values()
        details = {"what": what, "command": shlex.join(command), "code": done.value("code"),
                   "state_before": done.value("state_before"), "state_after": done.value("state_after"),
                   "mixer": done.value("mixer"), "audio_files": values.get("audio", [])}
        problems = []
        if values.get("audio"):
            problems.append("audio was written during the step: " + ", ".join(values["audio"][:3]))
        if done.value("state_before") != done.value("state_after"):
            problems.append("the carried state file changed; the runner should only have read it")
        if done.value("mixer") != "same":
            problems.append("the mixer settings changed during the step")
        if done.value("code") != "0":
            problems.append(f"the command exited {done.value('code') or 'without a code'}")
        if (done.value("ended", "0") or "0") == "0":
            problems.append("the typescript has no end line")
        if problems:
            return Step(name, "failed", "; ".join(problems), seconds, details)
        return Step(name, "passed", what, seconds, details)

    # ------------------------------------------------------------- collect

    def output_dir(self) -> Path:
        if self.args.out:
            return Path(self.args.out)
        base = TOP / "releases" / f"v{self.args.release}" / "body"
        day = self.started.strftime("%Y-%m-%d")
        candidate, n = base / day, 2
        while (candidate / "run.json").exists():
            candidate, n = base / f"{day}-{n}", n + 1
        return candidate

    def collect(self) -> Step:
        start = time.monotonic()
        out = self.out_dir = self.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        problems: list[str] = []
        kept: list[str] = []
        if self.home:
            body = (f'cd "$RUN" && tar -czf - --exclude=state --exclude=".mark-*" '
                    '--exclude=empty-keys.env .\n')
            done = self.remote.run(self.script("collect", body), timeout=600)
            if done.code == 0 and done.raw:
                with tarfile.open(fileobj=io.BytesIO(done.raw), mode="r:gz") as tar:
                    for member in tar.getmembers():
                        if not member.isfile():
                            continue
                        if member.name.lower().endswith(AUDIO_SUFFIXES):
                            problems.append(f"{member.name} is audio and was not copied")
                            continue
                        tar.extract(member, out, filter="data")
                        kept.append(member.name.removeprefix("./"))
            else:
                problems.append(f"the run's files did not come back: exit {done.code}")
            for path in self.args.fetch:
                name = self.fetch(path, out, problems)
                if name:
                    kept.append(name)
        logs = sorted(p for p in out.rglob("*") if p.suffix in (".typescript", ".log"))
        checks: dict[str, Any] = {}
        for log in logs:
            checks[log.relative_to(out).as_posix()] = verdict = self.check_log(log)
            if verdict["code"] == 1:
                problems.append(f"body_log_check.py fails {log.name}")
        runs = []
        for where, text in self.history_texts(out):
            runs += logged_runs(text, where)
        self.flags = flag_table(runs)
        details = {"files": sorted(kept), "log_checks": checks, "out": str(out)}
        status = "failed" if problems else "passed"
        reason = "; ".join(problems) or f"{len(kept)} file(s) to {out}"
        return self.add(Step("collect", status, reason, time.monotonic() - start, details))

    def fetch(self, path: str, out: Path, problems: list[str]) -> str | None:
        """Copy one typescript from the body's home folder, such as the
        sitting's. The name is checked before anything is read: only
        `~/pi-*.log` and `~/sitting-*.log`, so a keys file, an SSH key or a
        recording never reaches releases/."""
        if not FETCHABLE.match(path) or SECRET.search(path):
            problems.append(f"{path} was not fetched: a fetch names ~/pi-*.log or ~/sitting-*.log")
            return None
        target = remote_path(path, self.home, self.tree)
        body = (f'f={shlex.quote(target)}; [ -f "$f" ] || exit 3; '
                f'[ "$(stat -c %s "$f")" -le 20000000 ] || exit 4; cat "$f"\n')
        done = self.remote.run(marked("fetch-file", body), timeout=300)
        if done.code != 0:
            problems.append(f"{path} was not fetched: exit {done.code}")
            return None
        name = Path(path).name
        (out / name).write_bytes(done.raw)
        return name

    def check_log(self, log: Path) -> dict[str, Any]:
        if not self.log_check.is_file():
            return {"code": None, "said": f"not run: {self.log_check} is not there"}
        done = subprocess.run([sys.executable, "-B", str(self.log_check), str(log)], capture_output=True)
        said = done.stdout.decode("utf-8", "replace")
        log.with_name(log.name + ".check.txt").write_text(said, encoding="utf-8")
        last = [line for line in said.splitlines() if line.startswith("exit")]
        return {"code": done.returncode, "said": last[-1] if last else said.strip()[-120:]}

    def history_texts(self, out: Path) -> list[tuple[str, str]]:
        """Every typescript the flag table reads: this run's, the laptop's
        under releases/, and the maintainer's ~/pi-*.log and ~/sitting-*.log
        on the body, read and not copied."""
        seen: set[Path] = set()
        found: list[tuple[str, str]] = []
        paths = sorted(p for p in out.rglob("*") if p.suffix in (".typescript", ".log"))
        if self.history is None:
            releases = TOP / "releases"
            paths += sorted(releases.glob("v*/pi-*.log")) + sorted(releases.glob("v*/sitting*.log"))
            paths += sorted(releases.glob("v*/body/*/*.typescript")) + sorted(releases.glob("v*/body/*/*.log"))
        else:
            paths += list(self.history)
        for path in paths:
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            try:
                where = path.relative_to(TOP).as_posix()
            except ValueError:
                where = path.as_posix()
            found.append((where, path.read_bytes().decode("utf-8", "replace")))
        if self.home and not self.args.no_body_history:
            fence = f"=== {self.token} "
            body = (f'for f in "$HOME"/pi-*.log "$HOME"/sitting-*.log; do [ -f "$f" ] || continue; '
                    f'echo "{fence}$f"; cat "$f"; echo; done\n')
            try:
                done = self.remote.run(marked("history", body), timeout=300)
            except Unreachable:
                return found
            for chunk in done.out.split(fence)[1:]:
                name, _, text = chunk.partition("\n")
                found.append((f"{self.args.host}:~/{Path(name.strip()).name}", text))
        return found

    # ---------------------------------------------------------------- run

    @property
    def wanted(self) -> list[str]:
        if self.args.collect_only:
            return []
        if self.args.steps:
            return [s for s in STEPS if s in self.args.steps]
        return [s for s in STEPS if s not in HARDWARE_STEPS or self.args.window is not None]

    def run(self) -> int:
        try:
            return self._run()
        finally:
            self.release_lock()
            self.write()

    def _run(self) -> int:
        try:
            refusal = self.run_preflight()
        except Unreachable as exc:
            return self.not_done(f"the body did not answer over SSH ({exc})")
        if refusal:
            self.add(Step("preflight", "refused", refusal, details=self.preflight))
            return self.not_done(refusal)
        self.add(Step("preflight", "passed", self.board_line(), details=self.preflight))
        try:
            refusal = self.take_lock()
            if refusal:
                return self.not_done(refusal)
            if "sync" in self.wanted:
                if self.sync().status != "passed":
                    return self.not_done("the code under test did not reach the body")
            elif not self.args.collect_only:
                self.read_source()
            if "install" in self.wanted and self.install().status != "passed":
                return self.not_done("the code under test could not be installed on the body")
            if "suites" in self.wanted:
                self.suites()
            if "checks" in self.wanted:
                self.checks()
            if "bank" in self.wanted:
                self.bank()
            for name in HARDWARE_STEPS:
                if name not in self.wanted:
                    reason = ("no --window named: the microphone and the speaker are used only inside a "
                              "window the maintainer allowed") if self.args.window is None else "not asked for"
                    if not self.args.collect_only:
                        self.add(Step(name, "not run", reason))
            self.hardware()
            self.collect()
        except Unreachable as exc:
            return self.not_done(f"the body stopped answering over SSH ({exc})")
        failed = [s for s in self.steps if s.status in ("failed", "refused")]
        ran = [s for s in self.steps if s.status == "passed"]
        skipped = [s for s in self.steps if s.status == "not run"]
        self.verdict = (f"body run done: {len(ran)} step(s) passed, {len(failed)} failed or refused, "
                        f"{len(skipped)} not run")
        return 1 if failed else 0

    def not_done(self, why: str) -> int:
        self.verdict = f"body run not done: {why}"
        return 2

    def board_line(self) -> str:
        p = self.preflight
        load = " ".join(p.get("loadavg", "").split()[:3])
        temp = p.get("temp", "").removeprefix("temp=")
        throttled = p.get("throttled", "").removeprefix("throttled=")
        line = f"load {load}; {temp}; throttled {throttled} ({', '.join(p.get('throttled_means', []))})"
        disk = p.get("disk", "").split()
        if len(disk) >= 4 and disk[1].isdigit() and disk[3].isdigit():
            # df -P counts 1024-byte blocks.
            line += f"; {int(disk[3]) / 2**20:.1f} GiB free of {int(disk[1]) / 2**20:.1f}"
        return line

    # -------------------------------------------------------------- report

    def report(self) -> str:
        lines = [f"body run on {self.args.host}, started {self.started:%Y-%m-%d %H:%M:%S} (laptop clock)"]
        if self.preflight:
            lines.append(f"  board    {self.preflight.get('uptime', '')} (the body's clock, "
                         f"{self.preflight.get('date', '')})")
            lines.append(f"           {self.board_line()}")
            if self.preflight.get("emet_variables_unset"):
                lines.append(f"  unset    {' '.join(self.preflight['emet_variables_unset'])}")
        if self.source:
            s = self.source
            what = s.get("kind", "")
            if s.get("head"):
                what += f" at {s['head'][:7]} ({s.get('subject', '')})"
            if "changed" in s:
                what += (f"; against it {len(s['changed'])} changed, {len(s['added'])} added, "
                         f"{len(s['removed'])} removed; {s.get('crlf_converted', 0)} file(s) CRLF to LF")
            lines.append(f"  source   {what}")
        lines.append("")
        lines.append(f"  {'step':<15} {'status':<8} {'s':>6}  detail")
        for step in self.steps:
            lines.append(f"  {step.name:<15} {step.status:<8} {step.seconds:>6.0f}  {step.reason}")
        suites = next((s for s in self.steps if s.name == "suites"), None)
        if suites and suites.details:
            lines.append("")
            lines.append(f"  {'package':<16} {'collected':>9} {'passed':>7} {'skipped':>8} {'failed':>7} "
                         f"{'pytest s':>9} {'wall s':>7}")
            for package, c in suites.details.items():
                lines.append(f"  {package:<16} {c.get('collected', '?'):>9} {c.get('passed', '?'):>7} "
                             f"{c.get('skipped', '?'):>8} {c.get('failed', '?'):>7} "
                             f"{c.get('pytest_s', '?'):>9} {c.get('wall_s', '?'):>7}")
        if self.flags:
            lines.append("")
            lines.append(f"  {'flag':<28} {'last live':<11} {'last replay':<12} log")
            for row in self.flags:
                where = row["where"] or "not in the logs read"
                note = f"  ({row['with']})" if row["with"] else ""
                lines.append(f"  {row['flag']:<28} {row['live'] or '-':<11} {row['replay'] or '-':<12} "
                             f"{where}{note}")
        lines.append("")
        lines.append(self.verdict)
        return "\n".join(lines)

    def write(self) -> None:
        text = self.report()
        print(text)
        if self.out_dir is None:
            try:
                self.out_dir = self.output_dir()
                self.out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        record = {
            "format": "emet-body-run/1",
            "started": self.started.isoformat(timespec="seconds"),
            "ended": self.clock().isoformat(timespec="seconds"),
            "host": self.args.host,
            "release": self.args.release,
            "window": [w.isoformat(timespec="minutes") for w in self.args.window] if self.args.window else None,
            "allow_night": self.args.allow_night,
            "preflight": self.preflight,
            "source": self.source,
            "steps": [vars(s) for s in self.steps],
            "flags": self.flags,
            "verdict": self.verdict,
        }
        (self.out_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        (self.out_dir / "report.txt").write_text(text + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run.py", description="Run the body's checks over SSH.")
    parser.add_argument("--release", required=True, help="X.Y.Z, the release this run is evidence for")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--tree", action="store_true", help="send the laptop's working tree")
    what.add_argument("--commit", metavar="REF", help="a pushed commit, branch or tag of the public repository")
    what.add_argument("--collect-only", action="store_true",
                      help="run nothing; fetch, check and tabulate (the release sitting's typescript)")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--steps", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                        help=f"a comma list from {','.join(STEPS)}; the default is every step but the "
                             "speaker and live ones, which --window adds")
    parser.add_argument("--window", nargs=2, metavar=("START", "END"), type=parse_moment,
                        help="the window the maintainer allowed for the microphone and the speaker, "
                             "YYYY-MM-DDTHH:MM in the laptop's local time")
    parser.add_argument("--allow-night", action="store_true",
                        help="let a hardware step run between 22:00 and 08:00, on the maintainer's separate yes")
    parser.add_argument("--voice", choices=("mock", "piper"), default="mock")
    parser.add_argument("--manifest", default=MANIFEST, help="the body's manifest on the Pi")
    parser.add_argument("--soul", default=SOUL, help="the soul, relative to the synced tree")
    parser.add_argument("--speaker-wav", default=SPEAKER_WAV, help="the replay for the speaker steps")
    parser.add_argument("--bank", default=BANK)
    parser.add_argument("--bank-arg", action="append", default=[], help="passed on to replay.py")
    parser.add_argument("--fetch", action="append", default=[], metavar="~/FILE",
                        help="copy this typescript from the body into the output: ~/sitting-X.Y.Z.log, or "
                             "another ~/sitting-*.log or ~/pi-*.log; nothing else is fetched")
    parser.add_argument("--no-body-history", action="store_true",
                        help="read only the laptop's logs for the flag table")
    parser.add_argument("--out", help="the output directory; the default is releases/vX.Y.Z/body/<date>/")
    args = parser.parse_args(argv)
    if not VERSION.match(args.release):
        parser.error(f"--release {args.release!r} is not X.Y.Z")
    if args.commit and (not REF.match(args.commit) or ".." in args.commit):
        parser.error(f"--commit {args.commit!r} is not a plain commit, branch or tag name")
    if args.commit and re.match(r"(?:refs/)?pulls?/", args.commit):
        parser.error(f"--commit {args.commit!r} names a pull request; the body runs the repository's "
                     "own branches and tags")
    if args.steps and set(args.steps) - set(STEPS):
        parser.error(f"--steps names {sorted(set(args.steps) - set(STEPS))}; the steps are {', '.join(STEPS)}")
    if args.window is not None:
        args.window = tuple(args.window)
        if args.window[1] <= args.window[0]:
            parser.error("--window ends before it starts")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except AttributeError:
        pass
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return BodyRun(args, remote=Remote(args.host), laptop=Laptop(REPO)).run()


if __name__ == "__main__":
    raise SystemExit(main())
