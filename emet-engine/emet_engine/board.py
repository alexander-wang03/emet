"""The board a run ran on: its temperature, its throttle flags and its load.

`--stats` reads the board once at boot and once at the footer, and prints
both readings above the verdict, so a slow run explains itself: throttled
during this run, or a load of 0.7 before it started. The 0.3 headroom (19x
to 40x) and the card-clock work rest on runs whose temperature nobody wrote
down. Nothing here is read per frame: four `vcgencmd` calls a run on a
Raspberry Pi, and none anywhere else.

**The method is the Reachy LiteRT sample's**, ported from
`samples/litert_lm/reachy-voice-robot/emulator/env.py` in
google-ai-edge/litert-samples at 99fd82f (2026-10-24), Copyright 2026 The
Google AI Edge Authors, Apache-2.0: the two `vcgencmd` commands parsed as
pure functions (lines 107 to 130), the comparison of the flags before and
after a run, because the high bits stay set until a reboot (lines 133 to
151), and the load average, because a board with someone else's service
running on it produces numbers that look valid and are skewed (lines 174
to 192). The sample's hostname and kernel command line are left out: a
footer is pasted into public pull requests. `CITATIONS.md` has the entry.

**What the flags mean** is the Raspberry Pi documentation's table for
`vcgencmd get_throttled` (raspberrypi.com/documentation/computers/os.html,
read 2026-10-27): bits 0 to 3 are under-voltage, Arm frequency capped,
throttled and the soft temperature limit as they are now, and bits 16 to 19
are the same four as "has occurred". The documentation does not say how
long "has occurred" lasts; the sample's comment says until reboot.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "BoardReading",
    "VCGENCMD_TIMEOUT_S",
    "find_vcgencmd",
    "footer_lines",
    "load_average",
    "parse_reading",
    "read_board",
    "throttle_names",
]

#: How long one `vcgencmd` call may take before its reading is given up as
#: unknown. A whole reading, both calls and the load, took a median 2.56 ms
#: on the reference body (Raspberry Pi 5, Python 3.13.5, 20 readings at a
#: load of 0.05, 2026-10-28, in `releases/v0.5.5/timings/` outside the
#: repository); the limit is there so that a firmware that stops answering
#: costs a boot two seconds rather than the run.
VCGENCMD_TIMEOUT_S = 2.0

#: Each bit of `vcgencmd get_throttled`, named the way the footer prints it.
#: The Raspberry Pi documentation's table, read 2026-10-27; a bit it does not
#: list is printed by number.
THROTTLE_BITS: dict[int, str] = {
    0: "under-voltage now",
    1: "frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "frequency capped has occurred",
    18: "throttled has occurred",
    19: "soft temperature limit has occurred",
}

#: The four bits that say how the board is right now, the sample's
#: `_THROTTLE_NOW_MASK` (env.py line 110). The rest say "has occurred", so
#: a value of 0x50000 at the start of a run says the board was short of
#: power some time before it. The sample's comment says they stay set until
#: a reboot (env.py lines 107 to 109); the documentation does not say.
NOW_MASK = 0xF

_TEMP = re.compile(r"temp=(-?\d+(?:\.\d+)?)'C")
_THROTTLED = re.compile(r"throttled=0x([0-9a-fA-F]+)")

#: Runs one `vcgencmd` command and returns what it printed, or None when it
#: failed or took too long.
Runner = Callable[[str], "str | None"]


@dataclass(frozen=True)
class BoardReading:
    """One reading of the board.

    `vcgencmd` says whether the board has the command at all; with it,
    `temp_c` and `throttled` are None when it gave no reading. `load` is the
    1, 5 and 15 minute load average, None where the platform has none.
    """

    vcgencmd: bool
    temp_c: float | None = None
    throttled: int | None = None
    load: tuple[float, float, float] | None = None

    def parts(self) -> list[str]:
        """What one footer line says about this reading.

        A part the platform cannot read at all is left out, where a reader
        that exists and failed says "unknown". Missing is a property of the
        machine and would print the same words in every footer on Windows
        and in CI; the identity line at boot names the version that ran, so
        a footer from 0.5.5 on with no board line says the platform has
        neither reader. A failure on a board that has `vcgencmd` changes
        from run to run, and is printed.
        """
        out: list[str] = []
        if self.vcgencmd:
            out.append(f"temp {self.temp_c:.1f}'C" if self.temp_c is not None else "temp unknown")
            if self.throttled is None:
                out.append("throttled unknown")
            else:
                names = throttle_names(self.throttled)
                out.append(f"throttled {self.throttled:#x}" + (f" ({', '.join(names)})" if names else ""))
        if self.load is not None:
            out.append("load " + " ".join(f"{x:.2f}" for x in self.load))
        return out


def throttle_names(value: int) -> list[str]:
    """The names of the bits set in a `get_throttled` value, lowest first."""
    return [THROTTLE_BITS.get(bit, f"bit {bit}") for bit in range(value.bit_length()) if value >> bit & 1]


def parse_reading(
    temp_out: str | None,
    throttled_out: str | None,
    load: tuple[float, float, float] | None = None,
) -> BoardReading:
    """A reading from what the two `vcgencmd` commands printed: "temp=49.9'C"
    and "throttled=0x0". None for either means the command failed.

    A pure function, as the sample's `parse_throttle_outputs` is, so the
    parsing is tested on captured strings and does not first run for real on
    a Raspberry Pi. Output that does not have the expected shape is unknown:
    `vcgencmd measure_temp get_throttled` prints "bad argument" and exits 0
    on the reference body (2026-10-27), so an exit status alone does not say
    the reading is good.
    """
    temp = _TEMP.search(temp_out or "")
    flags = _THROTTLED.search(throttled_out or "")
    return BoardReading(
        vcgencmd=True,
        temp_c=float(temp.group(1)) if temp else None,
        throttled=int(flags.group(1), 16) if flags else None,
        load=load,
    )


def find_vcgencmd() -> Runner | None:
    """A runner for `vcgencmd`, or None where there is none: Windows, a
    laptop, CI, and any Linux that is not Raspberry Pi OS."""
    path = shutil.which("vcgencmd")
    if path is None:
        return None

    def run(command: str) -> str | None:
        try:
            done = subprocess.run(
                [path, command],
                capture_output=True,
                text=True,
                timeout=VCGENCMD_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    return run


def load_average() -> tuple[float, float, float] | None:
    """The 1, 5 and 15 minute load average, or None where the platform has
    none. Windows has no `os.getloadavg` (Python 3.13, 2026-10-27); on Linux
    it reads `/proc/loadavg`, which the sample reads by hand."""
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    try:
        one, five, fifteen = getloadavg()
    except OSError:
        return None
    return (one, five, fifteen)


def read_board() -> BoardReading:
    """Read the board now: two `vcgencmd` calls where it exists, and the load
    average. Never per frame."""
    load = load_average()
    run = find_vcgencmd()
    if run is None:
        return BoardReading(vcgencmd=False, load=load)
    return parse_reading(run("measure_temp"), run("get_throttled"), load)


def footer_lines(start: BoardReading, end: BoardReading) -> list[str]:
    """The `--stats` footer's lines for one run, in its label column.

    Every line starts with `board`, and a reader outside the repository
    tells the footer's `name cut` line from the line under each wake by how
    the line starts, so nothing here may start with those words. Nothing at
    all where the platform has neither reader.
    """
    lines = []
    for label, reading in (("at start:", start), ("at end:  ", end)):
        parts = reading.parts()
        if parts:
            lines.append(f"  board         {label} {', '.join(parts)}")
    during = _during(start, end)
    if during is not None:
        lines.append(f"  board         during the run: {during}")
    return lines


def _during(start: BoardReading, end: BoardReading) -> str | None:
    """Which throttle flags the run itself saw.

    Comparing each reading with 0x0 does not work: the high bits stay set
    after the board recovers, and one hot afternoon would mark every run
    after it as throttled. So the two readings are compared, as the sample's
    `throttled_during_run` does: the run saw a flag when the value changed
    or a bit that means "now" was set. Its names are those bits, and each
    sticky bit that was clear at the start. The sample counts a reading
    `vcgencmd` failed to give as a spoiled run; this says unknown, because
    nothing was measured.
    """
    if not (start.vcgencmd and end.vcgencmd):
        return None
    if start.throttled is None or end.throttled is None:
        return "unknown, vcgencmd gave no flags"
    before, after = start.throttled, end.throttled
    if before == after and not after & NOW_MASK:
        return "no throttle flag set"
    names = throttle_names(((before | after) & NOW_MASK) | (after & ~before))
    return ", ".join(names) if names else f"the flags went from {before:#x} to {after:#x}"
