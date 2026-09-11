"""The instrument.

Worth testing carefully, because a measuring tool that is quietly wrong is
worse than no measurement: it produces a number, the number gets believed, and
the belief survives long after anyone remembers where it came from. These tests
feed known timings in and assert the arithmetic comes back out.
"""

from __future__ import annotations

import time

from emet_engine.metrics import SAMPLE_CAP, SessionStats, Stopwatch


def stats(frame_ms: float = 80.0) -> SessionStats:
    return SessionStats(frame_ms=frame_ms)


# ------------------------------------------------------------------ counting


def test_a_fresh_run_claims_nothing():
    s = stats()
    assert s.frames == 0
    assert s.realtime_factor == 0.0
    assert s.mean_ms == 0.0
    assert s.percentile(50) == 0.0


def test_frames_and_audio_duration_track_each_other():
    s = stats(frame_ms=80.0)
    for _ in range(50):
        s.record_frame(1.0)
    assert s.frames == 50
    assert s.audio_ms == 4000.0


# -------------------------------------------------------------- the budget


def test_a_frame_inside_the_budget_is_not_an_overrun():
    s = stats(frame_ms=80.0)
    s.record_frame(79.9)
    assert s.over_budget == 0


def test_a_frame_over_the_budget_is_counted():
    s = stats(frame_ms=80.0)
    s.record_frame(80.1)
    assert s.over_budget == 1


def test_the_budget_follows_the_frame_size():
    """A body using shorter frames has less time per frame, and the budget has
    to move with it rather than being hardcoded to 80 ms."""
    s = stats(frame_ms=20.0)
    s.record_frame(30.0)
    assert s.over_budget == 1


def test_one_bad_frame_survives_a_good_average():
    """The failure people actually notice. A loop averaging 2 ms is
    comfortable; the same loop dropping a word once a minute is not, and an
    average will not show it."""
    s = stats(frame_ms=80.0)
    for _ in range(999):
        s.record_frame(2.0)
    s.record_frame(500.0)
    assert s.mean_ms < 3.0
    assert s.over_budget == 1
    assert s.process_ms_max == 500.0
    assert not s.kept_up


# --------------------------------------------------------- the real number


def test_the_realtime_factor_is_work_over_audio():
    s = stats(frame_ms=80.0)
    for _ in range(10):
        s.record_frame(8.0)          # 80 ms of work for 800 ms of audio
    assert s.realtime_factor == 0.1
    assert s.headroom == 10.0


def test_exactly_keeping_up_is_a_factor_of_one():
    s = stats(frame_ms=80.0)
    for _ in range(10):
        s.record_frame(80.0)
    assert s.realtime_factor == 1.0
    assert not s.kept_up, "no margin is not the same as keeping up"


def test_a_bare_pass_is_not_treated_as_success():
    """0.99 kept up on an idle machine and will not on a busy one. The verdict
    wants margin, not a photo finish."""
    s = stats(frame_ms=80.0)
    for _ in range(100):
        s.record_frame(79.0)
    assert s.realtime_factor < 1.0
    assert not s.kept_up


def test_comfortable_work_keeps_up():
    s = stats(frame_ms=80.0)
    for _ in range(100):
        s.record_frame(4.0)
    assert s.kept_up


def test_dropped_frames_fail_the_verdict_however_fast_the_loop_is():
    """Dropped audio is not slowness, it is audio nobody ever saw. A fast loop
    that lost a wake word did not keep up."""
    s = stats()
    for _ in range(100):
        s.record_frame(1.0)
    assert s.kept_up
    s.dropped = 1
    assert not s.kept_up


# ------------------------------------------------------------- percentiles


def test_percentiles_over_a_known_distribution():
    s = stats()
    for v in range(1, 101):
        s.record_frame(float(v))
    assert s.percentile(50) == 50.0
    assert s.percentile(95) == 95.0
    assert s.percentile(100) == 100.0


def test_percentiles_survive_the_sample_cap():
    """Totals stay exact forever; percentiles cover the recent past. A robot
    left on for a week must not grow a list until it dies."""
    s = stats()
    for _ in range(SAMPLE_CAP + 500):
        s.record_frame(5.0)
    assert s.frames == SAMPLE_CAP + 500
    assert len(s._samples) == SAMPLE_CAP
    assert s.percentile(50) == 5.0
    assert s.mean_ms == 5.0


# ----------------------------------------------------------------- honesty


def test_a_file_run_refuses_to_report_clock_drift():
    """A file has no sound card. Comparing wall time to audio time would
    produce a number that looks like drift and means nothing."""
    s = stats()
    s.record_frame(1.0)
    report = s.report(live=False)
    assert "not measured" in report
    assert "skew" not in report


def test_a_live_run_reports_the_clock():
    s = stats()
    s.record_frame(1.0)
    report = s.report(live=True)
    assert "skew" in report


def test_the_wall_clock_starts_at_the_first_frame():
    """The first live run on the reference body reported +1.69 s of skew
    over eleven minutes. That was the acoustic model loading before the
    first frame, not the sound card drifting. Start-up is not drift."""
    s = stats()
    assert s.wall_ms == 0.0
    time.sleep(0.05)
    s.record_frame(1.0)
    assert s.wall_ms < 30.0


def test_the_verdict_appears_in_the_report():
    s = stats()
    for _ in range(10):
        s.record_frame(1.0)
    assert "kept up" in s.report(live=False)
    s.dropped = 3
    assert "DID NOT KEEP UP" in s.report(live=False)


# ---------------------------------------------------------------- stopwatch


def test_the_stopwatch_measures_elapsed_time():
    with Stopwatch() as watch:
        time.sleep(0.02)
    assert 10.0 < watch.elapsed_ms < 500.0


def test_the_stopwatch_reports_zero_before_it_is_used():
    assert Stopwatch().elapsed_ms == 0.0
