#!/usr/bin/env python3
"""Say fixed replies in the robot's voice into WAV files, and measure them.

    python tools/reply_audio.py MANIFEST SOUL [--replies FILE] [--out DIR] [--json]
    python tools/reply_audio.py --measure WAV [WAV ...] [--json]

The first form says each reply in FILE (default
`tools/reply_audio_replies.txt`, one reply a line; a line starting `#` and
a blank line are skipped) with `emet-say MANIFEST SOUL TEXT --to
DIR/reply-N.wav`, then measures each file. `emet-say` writes through the
`wav` sink, so no speaker plays and no microphone opens. DIR defaults to a
new folder under the system's temporary directory, printed first. The
second form measures WAV files it is given and says nothing.

**What it measures**, in frames of 10 ms, a frame's level being
`20*log10(rms/32768)` dBFS with a floor of -100:

    gap            a run of frames below -50 dBFS, 150 ms or longer, with
                   sound on both sides. The largest is reported, with the
                   time it starts.
    sentence       a sounding span between gaps, 200 ms or longer. The
                   first sentence's length is reported with no threshold.
    join step      the largest `|x[n] - x[n-1]| / 32768` at a sample whose
                   preceding 5 ms has an RMS below -55 dBFS: a step out of
                   near silence, which is how a click at a chunk's join
                   shows.
    loudness step  the largest difference between neighbouring sentences'
                   active levels, a sentence's active level being the RMS
                   over its frames at or above -50 dBFS.

**Held to what was said.** `emet-say` prints how many sentences and fillers
it said into each file. When the gaps find fewer sentences than that, the
voice paused for less than a gap between some of them, so the first
"sentence" is more than one and the loudness step compares the wrong
spans: both are then reported as not measured, and the loudness threshold
is not applied. `--measure` knows nothing of what was said and reads every
file as its gaps divide it.

**Why -55 dBFS.** The reference voice, Piper `en_US-ljspeech-medium`, opens
a consonant's release out of 5 ms at -42 to -47 dBFS. Over 20 of its
replies (the five below, four runs, laptop, 2026-11-27) a quiet window of
-40 dBFS read steps of 0.061 to 0.284 and crossed 0.10 in 19; -50 dBFS
crossed it in 1, at 0.120; -55 dBFS read at most 0.022. Piper's pauses
between sentences sit at -55 to -68 dBFS, and a sample after digital
silence is read at any window, so a join inside a pause is still found.

**Thresholds**, each a value to tune: the largest gap at most 700 ms, the
join step at most 0.10, the loudness step at most 6 dB. The goldens are
these thresholds and never a file's bytes, since a voice need not say a
text the same way twice. A crossed threshold prints a line naming the file,
the measure and the time in the file, and the exit status is 1.

Exit status 0 when every file is within the thresholds; 1 when one is
crossed or `emet-say` fails on a reply; 2 for a usage error or a file that
is not a mono 16-bit PCM WAV, which is refused naming the file.

`emet-say` is the console script beside this interpreter or on PATH, or
else `python -m emet_engine.say`. The measuring is the standard library
only (`wave`, `array`, `math`), so `--measure` runs on a bare Python.

**What it spends.** The voice is the one MANIFEST and SOUL choose. Piper
runs on the machine and costs nothing; a cloud voice is paid by the
character, and the shipped replies are 573 characters a run. The first
line printed gives the count before anything is said. On the reference
body that means `~/pi-speakerphone.yaml` (Piper), never the Aura manifest
unless the money is meant.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

__all__ = ["FRAME_MS", "SILENT_DBFS", "GAP_MS", "SENTENCE_MS", "JOIN_WINDOW_MS", "JOIN_QUIET_DBFS",
           "MAX_GAP_MS", "MAX_JOIN_STEP", "MAX_LOUDNESS_STEP_DB", "REPLIES", "AudioRefused", "Measure",
           "read_wav", "measure", "measure_file", "held_to_said", "crossings", "read_replies", "said_count",
           "say_command", "main"]

HERE = Path(__file__).resolve().parent
#: The replies said when --replies names no other file.
REPLIES = HERE / "reply_audio_replies.txt"

FRAME_MS = 10
FLOOR_DBFS = -100.0
#: A frame below this level is silent.
SILENT_DBFS = -50.0
#: A silent run this long or longer, with sound on both sides, is a gap.
GAP_MS = 150
#: A sounding span between gaps this long or longer is a sentence.
SENTENCE_MS = 200
#: A join step is read at a sample whose preceding JOIN_WINDOW_MS has an RMS
#: below JOIN_QUIET_DBFS, -55 dBFS by the measure in the module docstring.
JOIN_WINDOW_MS = 5
JOIN_QUIET_DBFS = -55.0

#: The thresholds, each a value to tune.
MAX_GAP_MS = 700
MAX_JOIN_STEP = 0.10
MAX_LOUDNESS_STEP_DB = 6.0

FULL_SCALE = 32768.0


class AudioRefused(ValueError):
    """A file this tool does not measure. The message names the file."""


@dataclass
class Measure:
    """What one file measured, times in seconds from the file's first sample."""

    file: str
    rate: int
    duration_s: float
    sentences: int
    first_sentence_s: float | None
    largest_gap_ms: float | None
    gap_at_s: float | None
    join_step: float
    join_at_s: float | None
    loudness_step_db: float | None
    loudness_at_s: float | None
    sentence_levels_dbfs: list[float] = field(default_factory=list)
    #: The sentences and fillers `emet-say` said into the file, or None when
    #: that is not known (`--measure`).
    said: int | None = None

    @property
    def short(self) -> bool:
        """Whether the gaps found fewer sentences than were said."""
        return self.said is not None and self.sentences < self.said


def held_to_said(m: Measure, said: int | None) -> Measure:
    """`m` with what was said recorded, and its first sentence and loudness
    step marked not measured (None) when the gaps found fewer sentences than
    were said, as the module docstring has it."""
    m.said = said
    if said is not None and m.sentences < said:
        m.first_sentence_s = None
        m.loudness_step_db = m.loudness_at_s = None
    return m


# ------------------------------------------------------------------ reading


def read_wav(path: str | Path) -> tuple[int, array.array]:
    """The rate and the samples of a mono 16-bit PCM WAV, or `AudioRefused`."""
    shown = str(path)
    try:
        with wave.open(shown, "rb") as w:
            channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if channels != 1 or width != 2:
                raise AudioRefused(f"{shown} is {channels} channel(s) of {8 * width}-bit samples; "
                                   "this tool measures mono 16-bit PCM only")
            data = w.readframes(n)
    except (wave.Error, EOFError) as exc:
        raise AudioRefused(f"{shown} is not a PCM WAV file this tool can read: {exc}") from exc
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return rate, samples


# ------------------------------------------------------------------ measuring


def _dbfs(sum_squares: float, count: int) -> float:
    if count <= 0 or sum_squares <= 0:
        return FLOOR_DBFS
    return max(FLOOR_DBFS, 20.0 * math.log10(math.sqrt(sum_squares / count) / FULL_SCALE))


def _join_step(samples: Sequence[int], rate: int) -> tuple[float, int | None]:
    """The largest step out of near silence, and the sample it lands on."""
    window = max(1, int(round(rate * JOIN_WINDOW_MS / 1000.0)))
    quiet = (FULL_SCALE * 10 ** (JOIN_QUIET_DBFS / 20.0)) ** 2
    best, at = 0.0, None
    total = 0
    for n in range(1, len(samples)):
        prev = samples[n - 1]
        total += prev * prev
        if n > window:
            gone = samples[n - 1 - window]
            total -= gone * gone
        if total / min(n, window) < quiet:
            step = abs(samples[n] - prev) / FULL_SCALE
            if step > best:
                best, at = step, n
    return best, at


def measure(samples: Sequence[int], rate: int, name: str = "") -> Measure:
    """Measure one reply's samples by the definitions in the module docstring."""
    frame = max(1, int(round(rate * FRAME_MS / 1000.0)))
    starts = list(range(0, len(samples), frame))
    energy = []
    for s in starts:
        chunk = samples[s:s + frame]
        energy.append((float(sum(x * x for x in chunk)), len(chunk)))
    loud = [_dbfs(e, c) >= SILENT_DBFS for e, c in energy]

    def at(i: int) -> int:
        return starts[i] if i < len(starts) else len(samples)

    # Silent runs with sound on both sides that last GAP_MS or more.
    sounding = [i for i, v in enumerate(loud) if v]
    gaps: list[tuple[int, int]] = []
    if sounding:
        i = sounding[0]
        while i <= sounding[-1]:
            if loud[i]:
                i += 1
                continue
            j = i
            while not loud[j]:
                j += 1
            if (at(j) - at(i)) * 1000.0 / rate >= GAP_MS:
                gaps.append((i, j))
            i = j
    # The spans between them, from the first sounding frame to the last.
    spans: list[tuple[int, int]] = []
    if sounding:
        begin = sounding[0]
        for a, b in gaps:
            spans.append((begin, a))
            begin = b
        spans.append((begin, sounding[-1] + 1))
    sentences = [(a, b) for a, b in spans if (at(b) - at(a)) * 1000.0 / rate >= SENTENCE_MS]

    levels = []
    for a, b in sentences:
        active = [energy[k] for k in range(a, b) if loud[k]]
        levels.append(round(_dbfs(sum(e for e, _ in active), sum(c for _, c in active)), 2))
    step_db, step_at = None, None
    for k in range(1, len(levels)):
        diff = abs(levels[k] - levels[k - 1])
        if step_db is None or diff > step_db:
            step_db, step_at = diff, at(sentences[k][0]) / rate

    widest = max(gaps, key=lambda g: at(g[1]) - at(g[0]), default=None)
    join, join_at = _join_step(samples, rate)
    return Measure(
        file=name, rate=rate, duration_s=round(len(samples) / rate, 3), sentences=len(sentences),
        first_sentence_s=round((at(sentences[0][1]) - at(sentences[0][0])) / rate, 3) if sentences else None,
        largest_gap_ms=round((at(widest[1]) - at(widest[0])) * 1000.0 / rate, 1) if widest else None,
        gap_at_s=round(at(widest[0]) / rate, 3) if widest else None,
        join_step=round(join, 4), join_at_s=round(join_at / rate, 4) if join_at is not None else None,
        loudness_step_db=round(step_db, 2) if step_db is not None else None,
        loudness_at_s=round(step_at, 3) if step_at is not None else None,
        sentence_levels_dbfs=levels)


def measure_file(path: str | Path) -> Measure:
    rate, samples = read_wav(path)
    return measure(samples, rate, str(path))


def crossings(m: Measure) -> list[str]:
    """One line for each threshold the file crosses, naming the file, the
    measure and the time in the file."""
    out = []
    if m.largest_gap_ms is not None and m.largest_gap_ms > MAX_GAP_MS:
        out.append(f"{m.file}: the largest gap is {m.largest_gap_ms:.0f} ms at {m.gap_at_s:.2f} s, "
                   f"over the {MAX_GAP_MS} ms threshold")
    if m.join_step > MAX_JOIN_STEP:
        out.append(f"{m.file}: a join step of {m.join_step:.3f} at {m.join_at_s:.3f} s, "
                   f"over the {MAX_JOIN_STEP:.2f} threshold")
    if m.loudness_step_db is not None and m.loudness_step_db > MAX_LOUDNESS_STEP_DB:
        out.append(f"{m.file}: a loudness step of {m.loudness_step_db:.1f} dB at {m.loudness_at_s:.2f} s, "
                   f"over the {MAX_LOUDNESS_STEP_DB:g} dB threshold")
    return out


def measure_line(m: Measure) -> str:
    first = f"first {m.first_sentence_s:.2f} s" if m.first_sentence_s is not None else "no sentence"
    gap = (f"largest gap {m.largest_gap_ms:.0f} ms at {m.gap_at_s:.2f} s" if m.largest_gap_ms is not None
           else "no gap")
    join = (f"join step {m.join_step:.3f} at {m.join_at_s:.3f} s" if m.join_at_s is not None
            else "no sample after near silence")
    loud = (f"loudness step {m.loudness_step_db:.1f} dB at {m.loudness_at_s:.2f} s" if m.loudness_step_db is not None
            else "no loudness step")
    if m.short:
        first = f"of {m.said} said; first sentence not measured"
        loud = "loudness step not measured"
    return f"{m.file}  {m.duration_s:.2f} s, {m.sentences} sentence(s), {first}; {gap}; {join}; {loud}"


# ------------------------------------------------------------------ saying


def read_replies(path: str | Path) -> list[str]:
    """The replies in a replies file, one a line, skipping blank lines and `#` lines."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def say_command() -> list[str]:
    """How to run `emet-say`, the script beside this interpreter first, then the one on PATH, then the module."""
    beside = Path(sys.executable).parent
    for name in ("emet-say.exe", "emet-say"):
        if (beside / name).is_file():
            return [str(beside / name)]
    found = shutil.which("emet-say")
    return [found] if found else [sys.executable, "-m", "emet_engine.say"]


#: `emet-say`'s `said` line: `  said     3 sentence(s) and 1 filler(s); ...`.
SAID_LINE = re.compile(r"^\s*said\s+(\d+) sentence\(s\)(?: and (\d+) filler\(s\))?", re.M)


def said_count(output: str) -> int | None:
    """The sentences and fillers `emet-say` printed it said, or None when
    its output holds no `said` line."""
    found = SAID_LINE.search(output or "")
    return int(found.group(1)) + int(found.group(2) or 0) if found else None


def say_all(manifest: str, soul: str, replies: list[str], out: Path,
            run=None) -> tuple[list[Path], str | None, list[int | None]]:
    """Say each reply into `out/reply-N.wav`. Returns the files written,
    what to print when `emet-say` failed on one, and how many sentences and
    fillers `emet-say` said into each file written."""
    run = run or subprocess.run
    out.mkdir(parents=True, exist_ok=True)
    command = say_command()
    written: list[Path] = []
    said: list[int | None] = []
    for n, text in enumerate(replies, 1):
        target = out / f"reply-{n}.wav"
        done = run([*command, manifest, soul, text, "--to", str(target)],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
        if done.returncode != 0:
            tail = "\n".join((done.stderr or done.stdout or "").strip().splitlines()[-6:])
            return written, f"emet-say exited {done.returncode} on reply {n}:\n{tail}", said
        written.append(target)
        said.append(said_count(done.stdout))
    return written, None, said


# ------------------------------------------------------------------ the command


def thresholds_line() -> str:
    return (f"thresholds: largest gap at most {MAX_GAP_MS} ms, join step at most {MAX_JOIN_STEP:.2f}, "
            f"loudness step at most {MAX_LOUDNESS_STEP_DB:g} dB")


def report(files: list[Path], as_json: bool, said: Sequence[int | None] | None = None) -> int:
    """Measure `files` and print the measures; `said` holds what `emet-say`
    said into each, or is None when nothing is known of it."""
    try:
        counts = list(said) if said is not None else [None] * len(files)
        measures = [held_to_said(measure_file(f), n) for f, n in zip(files, counts)]
    except AudioRefused as exc:
        print(f"reply_audio: {exc}", file=sys.stderr)
        return 2
    crossed = [line for m in measures for line in crossings(m)]
    short = sum(m.short for m in measures)
    if as_json:
        print(json.dumps({
            "format": "emet-reply-audio/1",
            "thresholds": {"max_gap_ms": MAX_GAP_MS, "max_join_step": MAX_JOIN_STEP,
                           "max_loudness_step_db": MAX_LOUDNESS_STEP_DB},
            "files": [dict(asdict(m), crossed=crossings(m),
                           not_measured=["first_sentence_s", "loudness_step_db"] if m.short else [])
                      for m in measures],
            "verdict": "crossed" if crossed else "within",
        }, indent=1))
    else:
        print(thresholds_line())
        for m in measures:
            print(measure_line(m))
        for line in crossed:
            print(f"  over: {line}")
        verdict = (f"verdict: {len(crossed)} threshold(s) crossed" if crossed
                   else f"verdict: {len(measures)} file(s) within every threshold")
        if short:
            verdict += (f"; the loudness step not measured in {short} file(s), "
                        "whose gaps found fewer sentences than were said")
        print(verdict)
    return 1 if crossed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reply_audio.py",
        description="Say fixed replies through emet-say into WAV files and measure their gaps, joins and "
                    "loudness steps, or measure WAV files given with --measure.")
    parser.add_argument("manifest", nargs="?", help="the body manifest emet-say boots the voice from")
    parser.add_argument("soul", nargs="?", help="the soul whose voice says the replies")
    parser.add_argument("--replies", metavar="FILE", default=str(REPLIES),
                        help="the replies, one a line (default tools/reply_audio_replies.txt)")
    parser.add_argument("--out", metavar="DIR", help="where the WAV files go (default a new temporary folder)")
    parser.add_argument("--measure", nargs="+", metavar="WAV", help="measure these files only, saying nothing")
    parser.add_argument("--json", action="store_true", help="print the measures as one JSON object")
    args = parser.parse_args(argv)

    if args.measure:
        if args.manifest or args.soul:
            parser.error("--measure takes WAV files only, with no manifest or soul")
        return report([Path(p) for p in args.measure], args.json)
    if not (args.manifest and args.soul):
        parser.error("give MANIFEST and SOUL to say the replies, or --measure WAV to measure files")
    try:
        replies = read_replies(args.replies)
    except OSError as exc:
        print(f"reply_audio: cannot read the replies file {args.replies!r}: {exc.strerror or exc}",
              file=sys.stderr)
        return 2
    if not replies:
        print(f"reply_audio: the replies file {args.replies!r} holds no reply", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="emet-reply-audio-"))
    print(f"saying {len(replies)} repl{'y' if len(replies) == 1 else 'ies'}, "
          f"{sum(len(r) for r in replies)} characters, into {out}",
          file=sys.stderr if args.json else sys.stdout, flush=True)
    written, failure, said = say_all(args.manifest, args.soul, replies, out)
    if failure is not None:
        print(f"reply_audio: {failure}", file=sys.stderr)
        return 1
    return report(written, args.json, said)


if __name__ == "__main__":
    raise SystemExit(main())
