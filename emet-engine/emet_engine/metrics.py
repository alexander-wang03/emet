"""Measuring whether the loop keeps up.

0.3 is not done when it works once. The acceptance bar is ten minutes without
drift, and "without drift" is not something you can watch for. It is a number
or it is a feeling. This module makes it a number.

**The one that decides everything is the real-time factor**: processing time
divided by the audio it processed. Audio arrives at a fixed rate whatever the
CPU is doing, so at an RTF of 1.0 the loop is exactly keeping up and has no
margin; above 1.0 it is falling behind and frames are being dropped somewhere.
This is the number that will decide whether a Raspberry Pi can run Emet at all,
and it cannot be guessed from a laptop. It can be *compared*, though, which is why
it is worth recording on both.

**Frames over budget matters more than the average.** A loop averaging 20 ms
per 80 ms frame is comfortable; the same loop is not comfortable if one frame
in fifty takes 200 ms, because that frame is a dropped word. Averages hide
exactly the failure that people notice, so this counts the overruns
separately.

**Drift means two different things and only one is measurable offline.**
Processing drift, the loop failing to keep pace, shows up in the RTF and can
be measured against a file. Clock drift, where the sound card's idea of a
second and the system's slowly diverge, only appears with real hardware. This
module reports the first honestly and refuses to invent the second: for a file
source, wall-clock comparisons are meaningless and are labelled as such.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

__all__ = ["SessionStats", "Stopwatch"]

#: How many per-frame timings to keep for percentiles. Ten minutes at 80 ms
#: frames is 7500, so this holds a whole soak run; a robot left on for a week
#: keeps the most recent hour and a half and its running totals stay exact.
SAMPLE_CAP = 8192


class Stopwatch:
    """Times a block, in milliseconds, without pretending to be precise about
    anything the operating system will not promise."""

    __slots__ = ("_start", "elapsed_ms")

    def __init__(self) -> None:
        self._start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Stopwatch":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0


@dataclass
class SessionStats:
    """What one run of the listen loop did, and whether it kept up."""

    #: Milliseconds of audio each frame represents. Set from the format so the
    #: budget is not hardcoded to one frame size.
    frame_ms: float = 80.0

    frames: int = 0
    wakes: int = 0
    turns: int = 0
    #: Frames the source discarded because the loop fell behind. Not the same
    #: as being slow: this is audio that was never seen at all.
    dropped: int = 0

    process_ms_total: float = 0.0
    process_ms_max: float = 0.0
    over_budget: int = 0

    _samples: deque[float] = field(default_factory=lambda: deque(maxlen=SAMPLE_CAP))
    _started: float = field(default_factory=time.perf_counter)

    # ----------------------------------------------------------- recording

    def record_frame(self, process_ms: float) -> None:
        self.frames += 1
        self.process_ms_total += process_ms
        self.process_ms_max = max(self.process_ms_max, process_ms)
        if process_ms > self.frame_ms:
            self.over_budget += 1
        self._samples.append(process_ms)

    # ------------------------------------------------------------ readings

    @property
    def audio_ms(self) -> float:
        """How much audio was seen. The denominator for everything below."""
        return self.frames * self.frame_ms

    @property
    def wall_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000.0

    @property
    def realtime_factor(self) -> float:
        """Processing time over audio duration. Below 1.0 keeps up.

        Deliberately excludes time spent waiting for audio to arrive: on a live
        microphone that wait is most of the wall clock and is not work. Timing
        it would flatter every measurement to about 1.0 and hide the thing this
        number exists to reveal.
        """
        if not self.audio_ms:
            return 0.0
        return self.process_ms_total / self.audio_ms

    @property
    def headroom(self) -> float:
        """How many times faster than real time. 12x is comfortable, 1x is not."""
        rtf = self.realtime_factor
        return (1.0 / rtf) if rtf else float("inf")

    @property
    def mean_ms(self) -> float:
        return self.process_ms_total / self.frames if self.frames else 0.0

    def percentile(self, p: float) -> float:
        """Nearest-rank over the retained samples. `p` in 0..100."""
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        i = min(len(ordered) - 1, max(0, round(p / 100.0 * len(ordered)) - 1))
        return ordered[i]

    @property
    def kept_up(self) -> bool:
        """Whether this run is evidence the loop is viable here.

        Three conditions, and all of them matter. Nothing was dropped, no frame
        blew the budget, and there is real margin rather than a bare pass. A
        run at 0.99 kept up on a quiet machine and will not on a busy one.
        """
        return self.dropped == 0 and self.over_budget == 0 and self.realtime_factor < 0.5

    # -------------------------------------------------------------- report

    def report(self, *, live: bool) -> str:
        """A human-readable summary.

        `live` says whether the audio came from hardware, because it changes
        what can honestly be claimed: clock drift is only meaningful against a
        real sound card, and printing a wall-clock comparison for a file would
        be inventing a measurement.
        """
        lines = [
            f"  frames        {self.frames}  ({self.audio_ms / 1000:.1f}s of audio)",
            f"  wakes         {self.wakes}   turns {self.turns}",
            f"  per frame     mean {self.mean_ms:.2f} ms   "
            f"p50 {self.percentile(50):.2f}   p95 {self.percentile(95):.2f}   "
            f"max {self.process_ms_max:.2f}",
            f"  budget        {self.frame_ms:.0f} ms/frame, "
            f"{self.over_budget} frame(s) over",
            f"  realtime      {self.realtime_factor:.4f}  "
            f"({self.headroom:.0f}x faster than realtime)",
            f"  dropped       {self.dropped}",
        ]
        if live:
            skew = self.wall_ms - self.audio_ms
            lines.append(
                f"  clock         wall {self.wall_ms / 1000:.1f}s vs audio "
                f"{self.audio_ms / 1000:.1f}s   skew {skew / 1000:+.2f}s"
            )
        else:
            lines.append(
                "  clock         not measured: a file has no sound card, so "
                "wall time says nothing about drift"
            )
        lines.append(f"  verdict       {'kept up' if self.kept_up else 'DID NOT KEEP UP'}")
        return "\n".join(lines)
