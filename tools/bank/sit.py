#!/usr/bin/env python3
"""Run a bank sitting from the laptop, with quiet tones in the laptop's earbuds.

    python tools/bank/sit.py --test-tone
    python tools/bank/sit.py tools/bank/plans/v1-sitting-1.json [--again FILE_ID] [-- more record.py options]

It starts the prompter, tools/bank/record.py, on the reference body over
`ssh -t`, with this terminal passed straight through, so the screen and the
keys are the prompter's own. Over a second SSH connection it follows the
cue file the prompter writes (`record.py --cues`, a line at each count and
each NOW) and plays a soft tick at each 3, 2, 1 and a higher tone at each
NOW on the laptop's default output. Nothing plays on the Pi.

**Why tones.** In the first sitting, 2026-11-25, the terminal bell of
`record.py --bell` went unheard in the maintainer's headphones. A terminal
sounds BEL only when it is set to: VS Code's integrated terminal and mintty
can each be set to none, and which this laptop uses was not checked (no
~/.minttyrc and no bell setting in VS Code's settings.json, 2026-11-25, so
both run at their defaults, unverified). The laptop plays these tones
itself.

**Earbuds or headphones.** Through the laptop's speakers the tones are in
the recording. `--test-tone` plays the four tones once, so the volume can be
set before a sitting.

The tones are wavs made in memory, short and at a low level with a fade at
each end, played by winsound with SND_MEMORY, which cannot set a volume, so
the level is in the samples. winsound is Windows only, as the laptop is.

The cues travel by `tail -n0 -F` over SSH: on the Pi (GNU tail 9.7) lines
written a second apart reached the laptop a second apart to within 7 ms,
one at a time, and the remote tail ended when the local SSH did (laptop and
Pi, 2026-11-25).

While the prompter runs, sit.py checks the cue connection each second.
One that ended is said on the terminal, sounds as two low tones, and is
started again on the same file; a tone that fails to play is said at once.
A cue that reaches the laptop late is dropped, so a stall never plays 3, 2,
1 and NOW as one burst. A dry run is refused, since it writes every cue at
once: `--test-tone` tries the tones, and record.py's `--dry-run` on the Pi
the schedule.

Standard library only. Tests replace ssh and the player.

Exit: the prompter's exit, or 2 when the cue file could not be made or a dry
run was asked for.
"""

from __future__ import annotations

import argparse
import datetime
import io
import math
import shlex
import struct
import subprocess
import sys
import threading
import time
import wave
from typing import Callable

HOST = "emet-pi"
#: Where the body runner's sync puts the tree on the Pi, and the Pi's bank.
REMOTE_TREE = "~/emet-ci/emet"
REMOTE_BANK = "~/voicebank"

EARBUDS = ("Wear earbuds or headphones on the laptop: through its speakers the tones are in the recording. "
           "Nothing plays on the Pi.")

TONE_RATE = 22050
#: (Hz, ms, peak level of full scale). A tick for each count, a higher and
#: longer tone at NOW. Peaks of 0.06 and 0.10 are about -24 and -20 dBFS.
TICK = (1000.0, 40, 0.06)
NOW = (1500.0, 160, 0.10)
#: Two of these, WARNING_GAP_MS apart, say the cues were lost.
WARNING = (400.0, 120, 0.10)
WARNING_GAP_MS = 80
FADE_MS = 5
#: A cue that reaches the laptop more than LATE_S later than the quickest
#: cue so far is dropped unplayed. After a stall the cues arrive together,
#: and 3, 2, 1, NOW played as one burst would read as NOW. The quickest is
#: the link's fastest delivery plus the two clocks' difference, so the
#: clocks need not agree. 0.3 s is a choice, far over the 7 ms spread the
#: cues showed on the home network (laptop and Pi, 2026-11-25).
LATE_S = 0.3
#: Times the cue connection is started again before the tones are given
#: up for the sitting, with a warning.
RESTARTS = 5


def sine(hz: float, ms: int, level: float, rate: int = TONE_RATE) -> bytes:
    """A sine as 16-bit mono samples, with a raised-cosine fade over FADE_MS
    at each end so it starts and stops without a click."""
    n = int(rate * ms / 1000)
    fade = max(1, int(rate * FADE_MS / 1000))
    frames = bytearray()
    for i in range(n):
        env = 1.0
        if i < fade:
            env = 0.5 - 0.5 * math.cos(math.pi * i / fade)
        elif i >= n - fade:
            env = 0.5 - 0.5 * math.cos(math.pi * (n - 1 - i) / fade)
        frames += struct.pack("<h", int(round(32767 * level * env * math.sin(2 * math.pi * hz * i / rate))))
    return bytes(frames)


def as_wav(frames: bytes, rate: int = TONE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def tone(hz: float, ms: int, level: float, rate: int = TONE_RATE) -> bytes:
    return as_wav(sine(hz, ms, level, rate), rate)


def warning(rate: int = TONE_RATE) -> bytes:
    """Two low tones with a gap, which say the cues were lost or the tones stopped."""
    one = sine(*WARNING, rate=rate)
    return as_wav(one + bytes(2 * int(rate * WARNING_GAP_MS / 1000)) + one, rate)


SOUNDS = {"count": tone(*TICK), "now": tone(*NOW), "warning": warning()}


def winsound_player() -> Callable[[bytes], None]:
    """Play a wav from memory on the default output, waiting for it to end.
    SND_MEMORY with SND_ASYNC raises RuntimeError: "This module does not
    support playing from a memory image asynchronously"
    (docs.python.org/3.13/library/winsound.html, read 2026-11-25)."""
    try:
        import winsound
    except ImportError:
        raise SystemExit("sit.py plays its tones with winsound, which only Windows has; "
                         "on another system use record.py's --bell") from None

    def play(wav: bytes) -> None:
        winsound.PlaySound(wav, winsound.SND_MEMORY)
    return play


class Cues:
    """The cue connection and the tones it sounds.

    A thread reads the connection's lines and sounds each cue, dropping one
    that comes late (LATE_S). A tone that fails is reported at once, the
    first time, and counted, and the cues go on. While the prompter runs,
    check() is called each second: a connection that ended is reported, the
    two low tones of the warning sound, and it is started again on the same
    file, up to RESTARTS times. The cues written while it was down are lost."""

    def __init__(self, start_tail: Callable[[], object], play: Callable[[bytes], None], err,
                 wall: Callable[[], float] = time.time) -> None:
        self.start_tail, self.play, self.err, self.wall = start_tail, play, err, wall
        self.played = self.failed = self.late = self.restarts = self.warned = 0
        self.fastest: float | None = None
        self.given_up = False
        self._begin()

    def _begin(self) -> None:
        self.tail = self.start_tail()
        self.thread = threading.Thread(target=self._follow, args=(self.tail,), daemon=True)
        self.thread.start()

    def _follow(self, tail) -> None:
        for line in tail.stdout:
            self.cue(line)

    def _sound(self, wav: bytes) -> bool:
        try:
            self.play(wav)
            return True
        except Exception as exc:  # noqa: BLE001  winsound raises RuntimeError, a bad argument TypeError
            self.failed += 1
            if self.failed == 1:
                print(f"sit: a tone failed to play ({type(exc).__name__}: {exc}); the cues go on",
                      file=self.err, flush=True)
            return False

    def cue(self, line: str) -> None:
        words = line.split()
        if not words or words[0] not in ("count", "now"):
            return
        due = None
        for w in words:
            if w.startswith("due="):
                try:
                    due = float(w[4:])
                except ValueError:
                    pass
        if due is not None:
            offset = self.wall() - due
            if self.fastest is None or offset < self.fastest:
                self.fastest = offset
            elif offset > self.fastest + LATE_S:
                self.late += 1
                return
        if self._sound(SOUNDS[words[0]]):
            self.played += 1

    def check(self) -> None:
        if self.given_up or self.thread.is_alive():
            return
        code = self.tail.poll()
        self.restarts += 1
        self.warned += self._sound(SOUNDS["warning"])
        if self.restarts > RESTARTS:
            self.given_up = True
            print(f"sit: the cue connection ended again (ssh exit {code}); no more tones in this sitting",
                  file=self.err, flush=True)
            return
        print(f"sit: the cue connection ended (ssh exit {code}); cues were lost, starting it again",
              file=self.err, flush=True)
        self._begin()

    def stop(self) -> None:
        self.tail.terminate()
        self.thread.join(timeout=5)

    def summary(self) -> str:
        return (f"{self.played} cue(s) sounded, {self.late} dropped as late, {self.failed} tone(s) failed, "
                f"the cue connection started again {self.restarts} time(s)")


def test_tone(play: Callable[[bytes], None], sleep: Callable[[float], None] = time.sleep,
              out=None) -> None:
    """The four tones once, a second apart, as a sitting sounds them."""
    out = out or sys.stdout
    print(EARBUDS, file=out)
    for n in (3, 2, 1):
        print(f"  {n}", file=out, flush=True)
        play(SOUNDS["count"])
        sleep(1.0)
    print("  NOW", file=out, flush=True)
    play(SOUNDS["now"])


def on_host(path: str) -> str:
    """A path for the Pi's shell: `~/x` as "$HOME"/x, so the shell expands
    the home and nothing else; any other path quoted whole."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def prompter_command(plan: str, bank: str, cues: str, extra: list[str], tree: str) -> str:
    """The prompter's command line on the Pi. record.py expands `~` in
    --bank and --cues itself."""
    words = ["python3", "tools/bank/record.py", plan, "--bank", bank, "--cues", cues] + list(extra)
    return f"cd {on_host(tree)} && " + " ".join(shlex.quote(w) for w in words)


def sit(plan: str, extra: list[str], *, host: str = HOST, tree: str = REMOTE_TREE, bank: str = REMOTE_BANK,
        ssh: str = "ssh", popen=subprocess.Popen, run=subprocess.run,
        play: Callable[[bytes], None] | None = None, now: Callable[[], datetime.datetime] = datetime.datetime.now,
        wall: Callable[[], float] = time.time, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    if "--dry-run" in extra:
        print("sit: a dry run writes all its cues at once, which would sound as one burst. Try the tones with "
              "--test-tone, and the schedule with record.py --dry-run on the Pi", file=err)
        return 2
    play = play or winsound_player()
    print(EARBUDS, file=out, flush=True)
    cues = f"{bank.rstrip('/')}/logs/cues-{now():%Y%m%d-%H%M%S}.txt"
    # stdin from nowhere: an ssh that runs a command forwards its stdin, and
    # would take keys typed for the prompter.
    made = run([ssh, "-o", "BatchMode=yes", host, f"mkdir -p {on_host(cues.rsplit('/', 1)[0])} && : >> {on_host(cues)}"],
               stdin=subprocess.DEVNULL)
    if made.returncode != 0:
        print(f"sit: could not make the cue file {cues} on {host}", file=err)
        return 2

    def start_tail():
        # ServerAliveInterval: a link that dies is noticed in about 15 s,
        # three unanswered probes 5 s apart (OpenSSH's ServerAliveCountMax
        # of 3, unverified on Git's ssh), well before TCP would give up.
        return popen([ssh, "-o", "BatchMode=yes", "-o", "ServerAliveInterval=5", host, f"tail -n0 -F {on_host(cues)}"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, bufsize=1)

    state = Cues(start_tail, play, err, wall=wall)
    prompter = popen([ssh, "-t", host, prompter_command(plan, bank, cues, extra, tree)])
    try:
        while True:
            try:
                code = prompter.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                state.check()
            except KeyboardInterrupt:
                # The Ctrl-C is the prompter's: it reaches it over the
                # terminal, and it stops the prompts while the recording
                # runs to its end. The connection to it stays open.
                continue
    finally:
        state.stop()
    print(f"sit: {state.summary()}; the cues are in {cues} on {host}", file=out)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", nargs="?", help="the plan, as a path inside the tree on the Pi")
    ap.add_argument("--test-tone", action="store_true", help="play the four tones once, to set the volume")
    ap.add_argument("--again", metavar="FILE_ID", help="passed to record.py: record this file again")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--tree", default=REMOTE_TREE, help=f"the tree on the Pi (default {REMOTE_TREE})")
    ap.add_argument("--bank", default=REMOTE_BANK, help=f"the bank on the Pi (default {REMOTE_BANK})")
    ap.add_argument("--ssh", default="ssh", help="the ssh program")
    args, extra = ap.parse_known_args(argv)
    extra = [a for a in extra if a != "--"]
    if args.test_tone:
        test_tone(winsound_player())
        return 0
    if not args.plan:
        ap.error("name a plan, or --test-tone")
    if args.again:
        extra = ["--again", args.again] + extra
    return sit(args.plan, extra, host=args.host, tree=args.tree, bank=args.bank, ssh=args.ssh)


if __name__ == "__main__":
    raise SystemExit(main())
