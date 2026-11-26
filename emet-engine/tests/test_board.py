"""The board's state in the `--stats` footer.

Every reading here is a captured string or an injected reader: no test runs
this machine's `vcgencmd` or reads its load, so the suite gives the same
answer on Windows, in CI and on the reference body. The strings are what
the reference body printed on 2026-10-27 ("temp=49.9'C", "throttled=0x0")
and what the Raspberry Pi documentation's table makes of each bit.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from emet_engine import board
from emet_engine.board import BoardReading, footer_lines, parse_reading, throttle_names

LOAD = (0.07, 0.06, 0.05)


def pi(temp: float | None = 49.9, throttled: int | None = 0, load=LOAD) -> BoardReading:
    return BoardReading(vcgencmd=True, temp_c=temp, throttled=throttled, load=load)


# ------------------------------------------------------------------ parsing


def test_what_the_reference_body_printed_reads_back():
    reading = parse_reading("temp=49.9'C\n", "throttled=0x0\n", LOAD)
    assert reading == BoardReading(vcgencmd=True, temp_c=49.9, throttled=0, load=LOAD)


@pytest.mark.parametrize(
    "bit, name",
    [
        (0, "under-voltage now"),
        (1, "frequency capped now"),
        (2, "throttled now"),
        (3, "soft temperature limit now"),
        (16, "under-voltage has occurred"),
        (17, "frequency capped has occurred"),
        (18, "throttled has occurred"),
        (19, "soft temperature limit has occurred"),
    ],
)
def test_each_bit_is_named_as_the_documentation_names_it(bit, name):
    """The Raspberry Pi documentation's `get_throttled` table, read
    2026-10-27: bits 0 to 3 now, bits 16 to 19 has occurred."""
    assert throttle_names(1 << bit) == [name]


def test_a_bit_the_documentation_does_not_list_is_named_by_number():
    assert throttle_names(1 << 20) == ["bit 20"]


def test_a_value_names_its_bits_lowest_first():
    assert throttle_names(0x50005) == [
        "under-voltage now",
        "throttled now",
        "under-voltage has occurred",
        "throttled has occurred",
    ]
    assert throttle_names(0) == []


@pytest.mark.parametrize("temp_out, throttled_out", [(None, None), ("bad argument\n", "bad argument\n"), ("", "")])
def test_output_that_is_not_a_reading_is_unknown(temp_out, throttled_out):
    """`vcgencmd measure_temp get_throttled` printed "bad argument" and exited
    0 on the reference body (2026-10-27), so the shape is checked rather
    than the exit status."""
    reading = parse_reading(temp_out, throttled_out)
    assert reading.vcgencmd and reading.temp_c is None and reading.throttled is None
    assert reading.parts() == ["temp unknown", "throttled unknown"]


# ------------------------------------------------------------- the lines


def test_a_clean_run_on_the_body_prints_three_lines():
    lines = footer_lines(pi(), pi(temp=51.2, load=(0.35, 0.12, 0.08)))
    assert lines == [
        "  board         at start: temp 49.9'C, throttled 0x0, load 0.07 0.06 0.05",
        "  board         at end:   temp 51.2'C, throttled 0x0, load 0.35 0.12 0.08",
        "  board         during the run: no throttle flag set",
    ]


def test_flags_left_from_before_the_run_do_not_mark_it():
    """The sample's reason for comparing two readings: the high bits stay
    set until a reboot, and one hot afternoon would otherwise mark every
    run after it."""
    lines = footer_lines(pi(throttled=0x50000), pi(throttled=0x50000))
    assert lines[0].endswith("throttled 0x50000 (under-voltage has occurred, throttled has occurred), load 0.07 0.06 0.05")
    assert lines[2] == "  board         during the run: no throttle flag set"


def test_a_flag_raised_during_the_run_is_named_and_the_old_ones_are_not():
    lines = footer_lines(pi(throttled=0x10000), pi(throttled=0x50004))
    assert lines[2] == "  board         during the run: throttled now, throttled has occurred"


def test_a_condition_present_at_the_start_counts_even_when_it_cleared():
    lines = footer_lines(pi(throttled=0x10001), pi(throttled=0x10000))
    assert lines[2] == "  board         during the run: under-voltage now"


def test_a_condition_still_present_at_the_end_counts():
    lines = footer_lines(pi(throttled=0x80008), pi(throttled=0x80008))
    assert lines[2] == "  board         during the run: soft temperature limit now"


def test_a_failed_reading_is_unknown_and_never_a_verdict():
    """The sample counts a failed reading as a spoiled run. Nothing was
    measured, so the line says unknown."""
    lines = footer_lines(pi(), parse_reading(None, None, LOAD))
    assert lines[1] == "  board         at end:   temp unknown, throttled unknown, load 0.07 0.06 0.05"
    assert lines[2] == "  board         during the run: unknown, vcgencmd gave no flags"


def test_without_vcgencmd_or_a_load_average_there_is_no_line():
    """Windows: neither reader exists, so the footer is as it was."""
    nothing = BoardReading(vcgencmd=False)
    assert footer_lines(nothing, nothing) == []


def test_without_vcgencmd_only_the_load_is_printed():
    """A Linux laptop or CI: no temperature, no flags, no verdict line."""
    lines = footer_lines(BoardReading(vcgencmd=False, load=LOAD), BoardReading(vcgencmd=False, load=(1.0, 0.5, 0.25)))
    assert lines == [
        "  board         at start: load 0.07 0.06 0.05",
        "  board         at end:   load 1.00 0.50 0.25",
    ]


def test_no_line_starts_with_name_cut():
    """Two scripts outside the repository tell the footer's `name cut` line
    from the line under each wake by how the line starts."""
    readings = [
        pi(),
        pi(throttled=0xF000F),
        pi(throttled=None, temp=None),
        pi(load=None),
        BoardReading(vcgencmd=False, load=LOAD),
        BoardReading(vcgencmd=False),
    ]
    lines = [line for a in readings for b in readings for line in footer_lines(a, b)]
    assert lines and all(line.startswith("  board         ") for line in lines)
    assert not any(line.strip().startswith(("name cut", "lag ", "wakes ", "sink ", "verdict")) for line in lines)


# ------------------------------------------------------------ the readers


def test_read_board_asks_vcgencmd_for_each_reading_once(monkeypatch):
    asked: list[str] = []
    printed = {"measure_temp": "temp=49.9'C\n", "get_throttled": "throttled=0x0\n"}

    def run(command: str) -> str:
        asked.append(command)
        return printed[command]

    monkeypatch.setattr(board, "find_vcgencmd", lambda: run)
    monkeypatch.setattr(board, "load_average", lambda: LOAD)

    assert board.read_board() == pi()
    assert asked == ["measure_temp", "get_throttled"]


def test_read_board_without_vcgencmd_reads_only_the_load(monkeypatch):
    monkeypatch.setattr(board, "find_vcgencmd", lambda: None)
    monkeypatch.setattr(board, "load_average", lambda: None)
    assert board.read_board() == BoardReading(vcgencmd=False)


def test_there_is_no_runner_where_there_is_no_vcgencmd(monkeypatch):
    monkeypatch.setattr(board.shutil, "which", lambda name: None)
    assert board.find_vcgencmd() is None


def fake_run(result):
    calls: list[dict] = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if isinstance(result, BaseException):
            raise result
        return result

    return run, calls


@pytest.mark.parametrize(
    "result",
    [
        subprocess.TimeoutExpired(["vcgencmd"], board.VCGENCMD_TIMEOUT_S),
        OSError("permission denied"),
        subprocess.CompletedProcess(["vcgencmd"], 255, "", "VCHI initialization failed\n"),
    ],
    ids=["slow", "will not start", "exits 255"],
)
def test_a_vcgencmd_that_fails_gives_no_reading(monkeypatch, result):
    monkeypatch.setattr(board.shutil, "which", lambda name: "/usr/bin/vcgencmd")
    run, _ = fake_run(result)
    monkeypatch.setattr(board.subprocess, "run", run)
    runner = board.find_vcgencmd()
    assert runner is not None and runner("measure_temp") is None


def test_vcgencmd_is_asked_with_a_time_limit_and_no_input(monkeypatch):
    monkeypatch.setattr(board.shutil, "which", lambda name: "/usr/bin/vcgencmd")
    run, calls = fake_run(subprocess.CompletedProcess(["vcgencmd"], 0, "temp=49.9'C\n", ""))
    monkeypatch.setattr(board.subprocess, "run", run)

    assert board.find_vcgencmd()("measure_temp") == "temp=49.9'C\n"
    assert calls[0]["argv"] == ["/usr/bin/vcgencmd", "measure_temp"]
    assert calls[0]["timeout"] == board.VCGENCMD_TIMEOUT_S
    assert calls[0]["stdin"] is subprocess.DEVNULL


def test_no_load_average_where_the_platform_has_none(monkeypatch):
    monkeypatch.delattr(os, "getloadavg", raising=False)
    assert board.load_average() is None


def test_no_load_average_when_the_platform_cannot_give_one(monkeypatch):
    def unobtainable():
        raise OSError("Load averages are unobtainable")

    monkeypatch.setattr(os, "getloadavg", unobtainable, raising=False)
    assert board.load_average() is None


def test_the_load_average_is_the_three_figures(monkeypatch):
    monkeypatch.setattr(os, "getloadavg", lambda: (0.5, 0.25, 0.125), raising=False)
    assert board.load_average() == (0.5, 0.25, 0.125)
