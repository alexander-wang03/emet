"""The live line, drawn on a fake terminal.

Each fake terminal records every write in order on one shared screen, so a
test can show that the line was cleared before a printed line on stdout and
before a warning, logging's last-resort line or a traceback on stderr. No
real terminal, sound device or clock is used: the clock is a list the test
moves by hand.
"""

from __future__ import annotations

import io
import logging
import re
import sys

import pytest

from emet_sdk.types import WakeEvent

from emet_engine.live import CLEAR, ERASE, FLOOR_DBFS, INTERVAL_S, LiveLine


class Terminal(io.StringIO):
    """A text stream that says it is a terminal, and logs each write on a shared screen."""

    def __init__(self, screen: list[tuple[str, str]], name: str, *, tty: bool = True) -> None:
        super().__init__()
        self.screen = screen
        self.name = name
        self.tty = tty
        self.flushes = 0

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        if text:
            self.screen.append((self.name, text))
        return super().write(text)

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    # shutil.get_terminal_size reads COLUMNS first, so the width is the test's.
    monkeypatch.setenv("COLUMNS", "120")


def build(*, tty: bool = True, err_tty: bool | None = None, enabled: bool = True, patience_ms: int = 900):
    screen: list[tuple[str, str]] = []
    out = Terminal(screen, "stdout", tty=tty)
    err = None if err_tty is None else Terminal(screen, "stderr", tty=err_tty)
    clock = Clock()
    live = LiveLine(out, patience_ms=patience_ms, enabled=enabled, err=err, clock=clock)
    return live, screen, clock


def draws(screen: list[tuple[str, str]]) -> list[str]:
    return [text for name, text in screen if name == "stdout" and text.startswith("\r    ears")]


# ------------------------------------------------------------------ the words


def test_the_line_waiting_for_the_name_before_any_wake():
    live, screen, _ = build()
    live.frame(-51.83, None, None)
    assert screen == [("stdout", "\r    ears  -51.8 dBFS   waiting for the name   no wake yet" + ERASE)]


def test_inside_a_turn_the_line_shows_speech_and_the_quiet_against_the_patience():
    live, _, _ = build()
    assert live.text(-30.62, 1400.0, 320.0) == (
        "    ears  -30.6 dBFS   speech 1.4 s, quiet 320 of 900 ms   no wake yet"
    )


def test_one_value_missing_is_read_as_outside_a_turn():
    live, _, _ = build()
    assert "waiting for the name" in live.text(-40.0, 1400.0, None)
    assert "waiting for the name" in live.text(-40.0, None, 320.0)


def test_pocketsphinx_score_of_one_is_shown_as_it_comes_with_its_age():
    """pocketsphinx reports every wake at 1.00, and the line shows 1.00 (0.5.7-D7)."""
    live, _, clock = build()
    live.wake(WakeEvent("emet"))
    clock.now += 14.2
    assert live.text(-51.8, None, None).endswith("   last wake 1.00, 14 s ago")
    live.wake(WakeEvent("emet", confidence=0.734))
    assert live.text(-51.8, 0.0, 0.0).endswith("   last wake 0.73, 0 s ago")


@pytest.mark.parametrize("level", [float("-inf"), float("nan"), -150.0, -99.95, FLOOR_DBFS])
def test_the_level_is_floored_at_minus_99_9(level):
    live, _, _ = build()
    assert live.text(level, None, None).startswith("    ears  -99.9 dBFS   ")


def test_a_full_scale_frame_reads_zero_in_the_same_width():
    live, _, _ = build()
    assert live.text(0.0, None, None).startswith("    ears    0.0 dBFS   ")
    assert live.text(-7.25, None, None).startswith("    ears   -7.2 dBFS   ")


def test_the_patience_is_the_one_the_line_was_built_with():
    live, _, _ = build(patience_ms=1200)
    assert "quiet 900 of 1200 ms" in live.text(-30.0, 2000.0, 900.0)


def test_a_quiet_past_the_patience_is_shown_against_the_extension():
    """An extended turn waits twice the patience (`turn.py`), so its quiet
    counts on past 900 towards 1800, never "1500 of 900"."""
    live, _, _ = build(patience_ms=900)
    assert "quiet 900 of 900 ms" in live.text(-30.0, 2000.0, 900.0)
    assert "quiet 1500 of 1800 ms" in live.text(-30.0, 2000.0, 1500.0)


def test_the_patience_can_be_set_once_the_session_has_read_it():
    """The program hands `frame` to the session before the session reads the soul."""
    live, _, _ = build(patience_ms=900)
    live.patience_ms = 1500
    assert "quiet 600 of 1500 ms" in live.text(-30.0, 2000.0, 600.0)


def test_the_line_is_cut_to_the_terminal_width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    live, screen, _ = build()
    live.frame(-30.0, 12_300.0, 1800.0)
    (drawn,) = draws(screen)
    assert drawn.endswith(ERASE)
    assert len(drawn) - len("\r") - len(ERASE) == 39


# ------------------------------------------------------------------ when it draws


def test_at_most_twice_a_second():
    """Ten seconds of 20 ms frames draw 20 times, no two draws under half a second apart."""
    live, screen, clock = build()
    at: list[float] = []
    for i in range(500):
        clock.now = i * 20 / 1000
        before = len(screen)
        live.frame(-45.0, None, None)
        if len(screen) > before:
            at.append(clock.now)
    assert len(at) == 20
    assert all(b - a >= INTERVAL_S for a, b in zip(at, at[1:]))


def test_nothing_is_drawn_when_stdout_is_no_terminal():
    """`replay.py` sends stdout to a file, and the file gets the run's lines and nothing else."""
    live, screen, clock = build(tty=False, err_tty=False)
    assert live.drawing is False
    for i in range(100):
        clock.now += 0.6
        live.frame(-30.0, 500.0, 100.0)
    live.wake(WakeEvent("emet"))
    print("  heard 'emet'  (confidence 1.00)", file=live.out)
    print("  ! a warning", file=live.err)
    live.close()
    assert screen == [
        ("stdout", "  heard 'emet'  (confidence 1.00)"), ("stdout", "\n"),
        ("stderr", "  ! a warning"), ("stderr", "\n"),
    ]


def test_nothing_is_drawn_without_stats_even_on_a_terminal():
    live, screen, clock = build(enabled=False)
    assert live.drawing is False
    live.frame(-30.0, None, None)
    clock.now += 1.0
    live.frame(-30.0, None, None)
    assert screen == []


def test_the_line_waits_while_a_line_is_open():
    """After emet-talk's speaker tag or a caption's redraw, the line waits for the line to end."""
    live, screen, clock = build()
    live.out.write("  emet: ")
    clock.now += 1.0
    live.frame(-30.0, None, None)
    assert draws(screen) == []
    live.out.write("Hello.\n")
    live.frame(-30.0, None, None)
    assert len(draws(screen)) == 1
    clock.now += 1.0
    live.out.write("\r    hearing 'what is'")
    clock.now += 1.0
    live.frame(-30.0, 800.0, 0.0)
    assert len(draws(screen)) == 1, "a caption holds the line"
    live.out.write("\n")
    live.frame(-30.0, 800.0, 0.0)
    assert len(draws(screen)) == 2


def test_no_draw_after_close():
    live, screen, clock = build()
    live.close()
    clock.now += 1.0
    live.frame(-30.0, None, None)
    assert screen == []


# ------------------------------------------------------------------ clearing


def test_the_line_is_cleared_before_a_printed_line_on_stdout():
    live, screen, _ = build()
    live.frame(-51.8, None, None)
    print("  heard 'emet'  (confidence 1.00)", file=live.out)
    assert screen[1:] == [
        ("stdout", CLEAR),
        ("stdout", "  heard 'emet'  (confidence 1.00)"),
        ("stdout", "\n"),
    ]


def test_one_clear_for_each_draw():
    live, screen, _ = build()
    live.frame(-51.8, None, None)
    print("  heard 'emet'  (confidence 1.00)", file=live.out)
    print("    lag 380 ms, name cut 0 ms, level stop alone 20 ms", file=live.out)
    assert [text for _, text in screen].count(CLEAR) == 1


def test_the_line_is_cleared_before_a_write_to_stderr_when_stderr_is_a_terminal():
    live, screen, _ = build(err_tty=True)
    live.frame(-51.8, None, None)
    print("  ! the voice is slow", file=live.err)
    assert screen[1:] == [
        ("stdout", CLEAR),
        ("stderr", "  ! the voice is slow"),
        ("stderr", "\n"),
    ]


def test_stderr_off_a_terminal_leaves_the_line_alone():
    """stderr sent to a file never reaches the screen, so the line stays until stdout writes."""
    live, screen, _ = build(err_tty=False)
    live.frame(-51.8, None, None)
    print("  ! the voice is slow", file=live.err)
    assert CLEAR not in [text for _, text in screen]
    print("stopped. heard it 0 time(s).", file=live.out)
    assert screen[-3] == ("stdout", CLEAR)


def test_no_err_gives_no_err_wrapper():
    live, _, _ = build()
    assert live.err is None


def test_logging_last_resort_line_lands_after_the_clear(monkeypatch):
    """The programs install no handler, so a WARNING reaches `sys.stderr` through logging's last resort."""
    live, screen, _ = build(err_tty=True)
    monkeypatch.setattr(sys, "stderr", live.err)
    logger = logging.getLogger("emet.test_live.no_handler")
    monkeypatch.setattr(logger, "propagate", False)
    assert not logger.handlers and logging.lastResort is not None
    live.frame(-51.8, None, None)
    logger.warning("openai: HTTP 404 model_not_found")
    assert screen[1] == ("stdout", CLEAR)
    assert screen[2:] == [("stderr", "openai: HTTP 404 model_not_found\n")]


def test_a_traceback_lands_after_the_clear(monkeypatch):
    live, screen, _ = build(err_tty=True)
    monkeypatch.setattr(sys, "stderr", live.err)
    monkeypatch.setenv("NO_COLOR", "1")
    live.frame(-51.8, None, None)
    try:
        raise ValueError("boom")
    except ValueError:
        sys.__excepthook__(*sys.exc_info())
    assert screen[1] == ("stdout", CLEAR)
    printed = "".join(text for name, text in screen[2:] if name == "stderr")
    assert printed.startswith("Traceback") and printed.rstrip().endswith("ValueError: boom")
    assert all(name == "stderr" for name, _ in screen[2:])


def test_close_clears_a_drawn_line_once():
    live, screen, _ = build()
    live.frame(-51.8, None, None)
    live.close()
    live.close()
    assert screen[1:] == [("stdout", CLEAR)]


def test_close_writes_nothing_when_no_line_is_drawn():
    live, screen, _ = build()
    live.frame(-51.8, None, None)
    print("stopped. heard it 0 time(s).", file=live.out)
    count = len(screen)
    live.close()
    assert len(screen) == count


def test_every_draw_and_clear_is_flushed():
    """The line has no newline, and a line-buffered terminal would hold it back."""
    live, _, _ = build()
    out = live.out._stream
    live.frame(-51.8, None, None)
    assert out.flushes == 1
    live.close()
    assert out.flushes == 2


# ------------------------------------------------------------------ the wrappers


def test_the_wrappers_answer_as_the_streams_they_wrap():
    live, screen, _ = build(err_tty=False)
    assert live.out.isatty() is True and live.err.isatty() is False
    assert live.out.getvalue() == ""  # any other attribute is the stream's own
    live.out.writelines(["one\n", "two\n"])
    assert screen == [("stdout", "one\n"), ("stdout", "two\n")]


def test_a_redrawn_run_reads_as_the_body_runner_reads_a_typescript():
    """run.py strips escapes and splits each line at a carriage return
    (`tools/body/run.py` `logged_runs`, as `body_log_check.py` does). The
    heard line stays a piece of its own and the per-wake line stays the next
    piece after it, and the live line's pieces hold none of the parsers' words."""
    live, screen, clock = build()
    live.frame(-51.8, None, None)
    clock.now += 0.5
    live.frame(-50.1, None, None)
    print("  heard 'emet'  (confidence 1.00)", file=live.out)
    print("    lag 380 ms, name cut 0 ms, level stop alone 20 ms", file=live.out)
    live.wake(WakeEvent("emet"))
    clock.now += 0.5
    live.frame(-30.6, 400.0, 0.0)
    print("stopped. heard it 1 time(s).", file=live.out)
    live.close()
    typescript = "".join(text for _, text in screen).replace("\n", "\r\n")
    escape = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    pieces = [piece for line in typescript.split("\n")
              for piece in escape.sub("", line).rstrip("\r").split("\r")]
    filled = [p for p in pieces if p.strip()]
    heard = filled.index("  heard 'emet'  (confidence 1.00)")
    assert filled[heard + 1] == "    lag 380 ms, name cut 0 ms, level stop alone 20 ms"
    assert filled[-1] == "stopped. heard it 1 time(s)."
    live_pieces = [p for p in filled if p.startswith("    ears ")]
    assert len(live_pieces) == 3
    for piece in live_pieces:
        for word in ("heard", "hearing", "lag ", "cut ", "listening for", "stopped", "frames", "verdict"):
            assert word not in piece
