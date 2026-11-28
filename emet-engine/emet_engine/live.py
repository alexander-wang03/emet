"""The live line under `--stats`, drawn in place on a terminal.

While `emet-listen` or `emet-talk` runs with `--stats` at a terminal, one
line under the header shows what the ears hear now:

    ears  -51.8 dBFS   waiting for the name   last wake 1.00, 14 s ago
    ears  -30.6 dBFS   speech 1.4 s, quiet 320 of 900 ms   last wake 1.00, 2 s ago

The level is the last frame's, `20*log10(rms/32768)`, floored at -99.9. The
middle part is the endpointer's state: waiting for the name, or inside a turn
how long the person has spoken and how much of the patience the quiet has
used, against twice the patience once the quiet runs past it inside an
extended turn. Before the person speaks the quiet counts toward the 2.5 s
lead-in, and the line still reads it against the patience. The last part is the wake engine's last score and how long ago it came.
pocketsphinx reports every wake at confidence 1.00, and the line shows that
score as it comes (0.5.7-D7).

**Where it draws.** Only on a terminal: the stream's own `isatty()` decides,
because `tools/bank/replay.py` sends stdout to a file, and a run written to a
file gets nothing. It is redrawn at most once every half second by the
monotonic clock, because `script -qa` keeps every redraw in the body
runner's typescript, and only when the last thing written ended a line, so it
never lands inside a caption or after `  emet: `.

**Why it wraps the streams.** Every other write to stdout clears the line
first, and so does every write to stderr when stderr is a terminal too, so a
warning, logging's last-resort line or a traceback never lands on top of it.
The program sets `sys.stdout` to `.out` and `sys.stderr` to `.err` for the
run, since logging's last resort reads `sys.stderr` at each record, and puts
both back at exit after `close()`.

A line is drawn as a carriage return, the text, and ECMA-48's ERASE IN LINE
with its default parameter, `CSI K` (`ESC [ K`), which erases from the
cursor to the end of the line (ECMA-48, 5th edition, June 1991, section
8.3.41, read 2026-11-27). It is cleared by a carriage return and the same
erase. The text is cut to the terminal's width, so it never wraps onto a row
the carriage return cannot reach.

The idea of watching the running robot in place at a terminal, and of a
value never yet read saying so (`no wake yet`), is microduck's `robotctl
monitor` (Pollen Robotics, Apache-2.0, `docs/robot/cheatsheet.md` "Watching
the loop" at 942e58e). The line and its rules are Emet's.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from emet_sdk.types import WakeEvent

__all__ = ["CLEAR", "ERASE", "FLOOR_DBFS", "INTERVAL_S", "LiveLine"]

#: The fewest seconds between two draws, so the line is drawn at most twice
#: a second and a ten-minute typescript holds at most 1200 redraws.
INTERVAL_S = 0.5

#: The lowest level the line prints. A silent frame's level is minus
#: infinity, and a fixed width keeps the rest of the line still.
FLOOR_DBFS = -99.9

#: ECMA-48 ERASE IN LINE with its default parameter: the cursor to the end.
ERASE = "\x1b[K"

#: What clears a drawn line: back to its start, then erase it.
CLEAR = "\r" + ERASE


def _isatty(stream: Any) -> bool:
    """Whether the stream says it is a terminal, False when it cannot say."""
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


class _Guarded:
    """A stream whose every write clears the live line first.

    Anything else, such as `encoding`, `fileno` or `buffer`, is the wrapped
    stream's own. A write to `buffer` goes around the guard; nothing in the
    engine writes bytes to the console.
    """

    def __init__(self, live: LiveLine, stream: TextIO, *, on_terminal: bool) -> None:
        self._live = live
        self._stream = stream
        self._on_terminal = on_terminal

    def write(self, text: str) -> int:
        return self._live._write(self._stream, text, on_terminal=self._on_terminal)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return _isatty(self._stream)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __repr__(self) -> str:
        return f"<live line guard over {self._stream!r}>"


class LiveLine:
    """One line redrawn in place on a terminal while the loop listens.

    `stream` is where the line is drawn, stdout. It is drawn only when
    `enabled` (the program's `--stats`) and `stream.isatty()` are both true;
    `drawing` says which, so a caller passes `frame` to the session only when
    it is True and a run without the line computes no level. `err`, when
    given, is stderr: a write to `.err` clears the line first when `err` is a
    terminal, and passes straight through when it is not. `patience_ms` is
    the soul's, the figure the quiet counts towards; the session reads it
    from the soul after the hook is handed over, so a caller may set the
    attribute once the session is built. `clock` is the monotonic clock,
    replaced only by tests.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        patience_ms: float,
        enabled: bool,
        err: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.patience_ms = patience_ms
        self.drawing = bool(enabled) and _isatty(stream)
        self._stream = stream
        self._clock = clock
        self._lock = threading.RLock()
        self._drawn = False
        self._line_ended = True
        self._last_draw: float | None = None
        self._wake: tuple[float, float] | None = None
        self._closed = False
        #: stdout, wrapped: every write clears the line first.
        self.out = _Guarded(self, stream, on_terminal=True)
        #: stderr, wrapped, or None when no `err` was given.
        self.err = (
            None if err is None
            else _Guarded(self, err, on_terminal=self.drawing and _isatty(err))
        )

    def frame(self, level_dbfs: float, speech_ms: float | None, quiet_ms: float | None) -> None:
        """The session's hook, called once for each frame the loop reads.

        `speech_ms` and `quiet_ms` are None outside a turn. The line is
        redrawn when half a second has passed since the last draw and the
        last thing written ended a line; otherwise the values are dropped,
        since the next frame brings newer ones.
        """
        if not self.drawing or self._closed:
            return
        now = self._clock()
        if self._last_draw is not None and now - self._last_draw < INTERVAL_S:
            return
        with self._lock:
            if not self._line_ended or self._closed:
                return
            text = self.text(level_dbfs, speech_ms, quiet_ms, now=now)
            room = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
            self._stream.write("\r" + text[:room] + ERASE)
            self._stream.flush()
            self._drawn = True
            self._last_draw = now

    def wake(self, event: WakeEvent) -> None:
        """Note the wake's score and the moment it came."""
        self._wake = (float(event.confidence), self._clock())

    def text(
        self,
        level_dbfs: float,
        speech_ms: float | None,
        quiet_ms: float | None,
        *,
        now: float | None = None,
    ) -> str:
        """The line's words for these values, with no carriage return or erase."""
        # A NaN compares false as well, so it takes the floor with minus infinity.
        level = level_dbfs if level_dbfs >= FLOOR_DBFS else FLOOR_DBFS
        if speech_ms is None or quiet_ms is None:
            state = "waiting for the name"
        else:
            # Once speech has started, a turn ends when its quiet reaches the
            # patience, unless the words look unfinished and the endpointer
            # waits twice as long, so a quiet past the patience is inside
            # that extension.
            extended = speech_ms > 0 and quiet_ms > self.patience_ms
            needed = self.patience_ms * 2 if extended else self.patience_ms
            state = f"speech {speech_ms / 1000:.1f} s, quiet {quiet_ms:.0f} of {needed:.0f} ms"
        if self._wake is None:
            wake = "no wake yet"
        else:
            confidence, at = self._wake
            ago = max(0.0, (self._clock() if now is None else now) - at)
            wake = f"last wake {confidence:.2f}, {ago:.0f} s ago"
        return f"    ears {level:6.1f} dBFS   {state}   {wake}"

    def close(self) -> None:
        """Clear the line if it is drawn, and draw it no more. Safe to call twice."""
        with self._lock:
            self._closed = True
            try:
                self._clear()
            except (OSError, ValueError):
                # At exit a terminal that has gone away must not hide the
                # error the program is about to print.
                self._drawn = False

    def _clear(self) -> None:
        if self._drawn:
            self._drawn = False
            self._stream.write(CLEAR)
            self._stream.flush()

    def _write(self, stream: TextIO, text: str, *, on_terminal: bool) -> int:
        if not text or not on_terminal:
            return stream.write(text)
        with self._lock:
            self._clear()
            self._line_ended = text.endswith("\n")
            return stream.write(text)
