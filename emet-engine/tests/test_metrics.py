"""The instrument.

Worth testing carefully, because a measuring tool that is quietly wrong is
worse than no measurement: it produces a number, the number gets believed, and
the belief survives long after anyone remembers where it came from. These tests
feed known timings in and assert the arithmetic comes back out.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import fields

import pytest

from emet_engine.metrics import (
    OUTCOMES,
    SAMPLE_CAP,
    Handover,
    Sent,
    SessionStats,
    Spread,
    Stopwatch,
    Tokens,
    TurnRecord,
)


def stats(frame_ms: float = 80.0) -> SessionStats:
    return SessionStats(frame_ms=frame_ms)


# ------------------------------------------------------------------ counting


def test_the_name_cut_line_counts_the_handovers_and_the_cuts_that_moved():
    """The footer counts the handovers and the cuts that moved; the line
    `--stats` prints under each wake says which."""
    s = stats()
    assert "name cut" not in s.report(live=False), "nothing handed over, no line"
    s.name_cut_ms.extend([0.0, 130.0, 0.0, 90.0])
    line = next(row for row in s.report(live=False).splitlines() if "name cut" in row)
    assert "2 of 4 handover(s)" in line
    assert "mean 110 ms" in line and "max 130" in line


def test_a_name_cut_line_with_nothing_moved_says_so():
    s = stats()
    s.name_cut_ms.extend([0.0, 0.0])
    line = next(row for row in s.report(live=False).splitlines() if "name cut" in row)
    assert "0 of 2 handover(s)" in line
    assert "mean" not in line


def test_a_handover_line_reads_the_lag_and_both_cuts():
    """The line under each wake, so a first word that came back odd on the
    body can be read against its cut: on 2026-10-03 the footer counted 6
    moved cuts in 26 and named none."""
    assert Handover(190.0, 190.0, 130.0, 130.0).text() == "lag 190 ms, name cut 130 ms, level stop alone 130 ms"
    assert Handover(180.0, 180.0, 0.0, 150.0).text() == "lag 180 ms, name cut 0 ms, level stop alone 150 ms"


def test_a_tail_cut_short_says_it_was_handed_over_as_heard():
    """A lag past what the loop keeps hands over the last of it unmoved.
    The lag is printed as the wake engine reported it, and the line says
    how much went over."""
    line = Handover(700.0, 640.0, 0.0, 0.0, cut_short=True).text()
    assert line.startswith("lag 700 ms, ")
    assert line.endswith("; the last 640 ms handed over as heard")


def test_the_footer_counts_the_cut_made_and_never_the_level_stops():
    """The level stop alone is reported beside the cut and never applied, so
    the footer's count of cuts that moved reads the cut alone."""
    s = stats()
    for name, level in [(0.0, 0.0), (130.0, 130.0), (0.0, 150.0), (90.0, 90.0)]:
        s.record_handover(Handover(200.0, 200.0, name, level))
    line = next(row for row in s.report(live=False).splitlines() if "name cut" in row)
    assert "2 of 4 handover(s)" in line and "mean 110 ms" in line and "max 130" in line


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
    first frame, not the sound card drifting. Start-up is not drift.

    The clock carries one frame of offset, because the audio clock counts
    the first frame whole while the wall clock starts when that frame is
    handed over. Fifty milliseconds of start-up must not appear on top."""
    s = stats()
    assert s.wall_ms == 0.0
    time.sleep(0.05)
    s.record_frame(1.0)
    assert s.wall_ms < 30.0 + s.frame_ms


def test_the_verdict_appears_in_the_report():
    s = stats()
    for _ in range(10):
        s.record_frame(1.0)
    assert "kept up" in s.report(live=False)
    s.dropped = 3
    assert "DID NOT KEEP UP" in s.report(live=False)


def test_the_boards_lines_go_just_above_the_verdict():
    """The board is the first explanation for a run that did not keep up,
    and the verdict stays the last line, where the readers of a console
    stop."""
    s = stats()
    s.record_frame(1.0)
    board = ["  board         at start: load 0.07 0.06 0.05", "  board         at end:   load 0.35 0.12 0.08"]
    rows = s.report(live=False, board=board).splitlines()
    assert rows[-3:-1] == board
    assert rows[-1] == "  verdict       kept up", "the verdict stays in the label column"
    assert s.report(live=False).splitlines() == [row for row in rows if row not in board]


def test_without_the_board_the_report_has_no_board_line():
    s = stats()
    s.record_frame(1.0)
    assert "board" not in s.report(live=False) and "board" not in s.report(live=True)


# ---------------------------------------------------------------- stopwatch


def test_the_stopwatch_measures_elapsed_time():
    with Stopwatch() as watch:
        time.sleep(0.02)
    assert 10.0 < watch.elapsed_ms < 500.0


def test_the_stopwatch_reports_zero_before_it_is_used():
    assert Stopwatch().elapsed_ms == 0.0


def test_card_overflows_are_named_for_what_they_count():
    """PortAudio flags a callback, not a frame, and does not say how much
    audio it lost, so the count is given as callbacks."""
    s = stats()
    s.record_frame(1.0)
    s.overflows = 4
    report = s.report(live=True)
    assert "4 callback(s) PortAudio flagged as an input overflow" in report
    assert "how much each lost is not reported" in report


def test_a_file_has_no_card_to_overflow():
    s = stats()
    s.record_frame(1.0)
    assert "overflow" not in s.report(live=False)


def test_the_clock_line_gives_the_skew_as_a_percentage_too():
    """The reference body's microphone ran 0.2% slow against the system
    clock by arecord in 0.3, and 0.08% by both arecord and this line on
    2026-09-24. Seconds of skew depend on how long the run was; the
    percentage is the number that identifies the card."""
    s = stats()
    s.record_frame(1.0)
    assert "%)" in s.report(live=True)


def test_the_clock_line_says_how_much_skew_the_dropped_frames_explain():
    """141 frames dropped on the reference body showed as +11.38 s of skew,
    which is exactly 141 times 80 ms. The line now does that sum."""
    s = stats(frame_ms=80.0)
    for _ in range(10):
        s.record_frame(1.0)
    assert "dropped frame" not in s.report(live=True)
    s.dropped = 141
    line = next(l for l in s.report(live=True).splitlines() if "clock" in l)
    assert "11.28s of it is the 141 dropped frame(s)" in line


def test_a_run_that_lost_nothing_has_no_skew():
    """Frame 1 is handed over at t=0 and frame N at t=(N-1) frames, while the
    audio clock counts all N frames whole. Without an offset the wall clock
    reads one frame short for the whole run, so the skew is understated by
    80 ms and the dropped-frame component can exceed the total it explains:
    the reference body printed "skew +35.54s, of which 35.60s is the 445
    dropped frame(s)" on 2026-09-20, a part larger than the whole."""
    s = stats(frame_ms=80.0)
    for _ in range(100):
        s.record_frame(1.0)
    s._started = time.perf_counter() - (99 * 0.080)

    skew = s.wall_ms - s.audio_ms
    assert -20.0 < skew < 20.0, f"{skew:.1f} ms of skew on a run that lost nothing"


def test_the_clock_line_prints_what_is_left_after_the_dropped_frames():
    """Two figures on one line and the reader subtracting them by hand is how
    141 dropped frames read as 18.9% of drift on the body."""
    s = stats(frame_ms=80.0)
    for _ in range(100):
        s.record_frame(1.0)
    s.dropped = 50
    s._started = time.perf_counter() - 12.0

    line = next(row for row in s.report(live=True).splitlines() if "clock" in row)
    assert "50 dropped frame(s), leaving " in line


# ------------------------------------------------------------ the turn record

#: The contract's example turn (0.5.7 contract 3.4): 960 ms of quiet, 410 to
#: the final, 620 to the first word and 270 more to the first sound.
EXAMPLE = dict(
    turn=3,
    outcome="answered",
    tail_ms=960.0,
    final_ms=410.0,
    reply_first_ms=620.0,
    voice_first_ms=890.0,
    input_tokens=967,
    output_tokens=18,
    stt_audio_s=3.3,
    stt_host="api.deepgram.com",
    llm_chars=2071,
    llm_host="api.openai.com",
    voice_chars=78,
    voice_host=None,
    sentences=2,
    cleaned=0,
)

#: The readers of the line under each wake and of the footer's `name cut`
#: line, copied from `releases/v0.5.4/body_log_check.py` lines 64 to 66 and
#: `tools/bank/replay.py` lines 330 to 331, so a per-turn line or a footer
#: line they would mistake for one of theirs fails here.
PER_WAKE = re.compile(r"lag (\d+) ms, (?:name )?cut (\d+) ms, level stop alone (\d+) ms")
REPLAY_LINE = re.compile(r"^    lag (\d+) ms, name cut (\d+) ms, level stop alone (\d+) ms")


def example(**changes) -> TurnRecord:
    return TurnRecord(**{**EXAMPLE, **changes})


def test_the_record_keeps_the_contracts_fields_in_its_order():
    """The field order is the events record's key order."""
    assert [f.name for f in fields(TurnRecord)] == [
        "turn", "outcome", "tail_ms", "final_ms", "reply_first_ms", "voice_first_ms",
        "input_tokens", "output_tokens", "stt_audio_s", "stt_host", "llm_chars", "llm_host",
        "voice_chars", "voice_host", "sentences", "cleaned",
    ]


def test_a_new_record_holds_only_its_turn():
    record = TurnRecord(turn=1)
    assert record.outcome == "not_answered"
    assert record.gap_ms is None and record.sent() == [] and not record.ran_a_stage
    assert record.line() is None, "a turn that ran no stage and made no sound prints no line"


def test_the_gap_is_the_quiet_the_wait_for_the_words_and_the_wait_for_the_first_sound():
    assert example().gap_ms == 960.0 + 410.0 + 890.0
    assert example(final_ms=None).gap_ms == 960.0 + 890.0, "no transcriber, no wait for the words"
    assert example(voice_first_ms=None).gap_ms is None, "no sound, no gap"
    assert example(tail_ms=None).gap_ms is None


def test_the_per_turn_line_reads_as_the_contract_has_it():
    assert example().line() == (
        "turn gap 2260 ms = quiet 960 + stt final 410 + llm first 620 + voice 270; "
        "967 tokens in, 18 out; sent 3.3 s of audio to api.deepgram.com, 2071 characters to api.openai.com"
    )


def test_the_lines_terms_add_up_to_its_gap_however_the_milliseconds_fall():
    """Each term rounded alone would print 2262 = 960 + 410 + 620 + 270 for
    the first of these, a sum wrong by two."""
    rng = random.Random(57)
    cases = [example(tail_ms=960.4, final_ms=410.4, reply_first_ms=620.4, voice_first_ms=890.8)]
    for _ in range(500):
        reply = rng.choice([None, rng.uniform(0, 3000)])
        cases.append(example(
            tail_ms=rng.uniform(0, 2000),
            final_ms=rng.choice([None, rng.uniform(0, 3000)]),
            reply_first_ms=reply,
            voice_first_ms=(reply or 0.0) + rng.uniform(-200, 800),
        ))
    for record in cases:
        head = record.line().split(";")[0]
        total, terms = head.removeprefix("turn gap ").split(" ms = ")
        shown = [int(term.rsplit(" ", 1)[1]) for term in terms.split(" + ")]
        assert sum(shown) == int(total) == round(record.gap_ms), head
        # And no term moves by a whole millisecond (review finding
        # engine-core/tests#7 of 0.5.7).
        exact = [record.tail_ms, record.final_ms, record.reply_first_ms]
        exact = [term for term in exact if term is not None]
        exact.append(record.voice_first_ms - (record.reply_first_ms or 0.0))
        assert all(abs(seen - term) < 1 for seen, term in zip(shown, exact, strict=True)), (head, exact)


def test_a_term_with_no_value_is_left_out_with_its_plus():
    assert example(final_ms=None, reply_first_ms=None, voice_first_ms=300.0).line().startswith(
        "turn gap 1260 ms = quiet 960 + voice 300; "
    ), "emet-listen with no transcriber and no model"
    assert example(reply_first_ms=None, voice_first_ms=500.0).line().startswith(
        "turn gap 1870 ms = quiet 960 + stt final 410 + voice 500; "
    ), "a line said with no reply, such as the unavailable line"


def test_a_turn_that_made_no_sound_says_nothing_was_spoken():
    line = example(voice_first_ms=None).line()
    assert line.startswith("turn gap - (nothing was spoken); ")


def test_a_turn_with_no_speech_that_was_answered_does_not_say_nothing_was_spoken():
    """A wake, then silence, and the soul's line for it: a sound with no
    last loud frame to count the gap from."""
    line = example(tail_ms=None).line()
    assert line.startswith("turn gap - (no speech to count from); ")


def test_tokens_print_for_the_sides_that_were_counted():
    assert "; 967 tokens in, 18 out; " in example().line()
    assert "; 967 tokens in; " in example(output_tokens=None).line()
    assert "; 18 tokens out; " in example(input_tokens=None).line()
    assert "tokens" not in example(input_tokens=None, output_tokens=None).line()


def test_sent_names_each_host_off_the_body_that_was_handed_something():
    assert example(voice_host="api.deepgram.com").line().endswith(
        "sent 3.3 s of audio to api.deepgram.com, 2071 characters to api.openai.com, "
        "78 characters to api.deepgram.com"
    )
    assert example(stt_host=None).line().endswith("; sent 2071 characters to api.openai.com")
    assert example(stt_audio_s=0.0).line().endswith("; sent 2071 characters to api.openai.com"), (
        "a host handed nothing is not named"
    )


def test_a_turn_on_this_body_says_nothing_left_it():
    line = example(stt_host=None, llm_host=None, voice_host=None).line()
    assert line.endswith("; 967 tokens in, 18 out; nothing left the body")
    assert "sent" not in line


def test_the_line_is_none_only_for_a_turn_that_ran_no_stage_and_made_no_sound():
    quiet = TurnRecord(turn=1, tail_ms=900.0)
    assert quiet.line() is None, "emet-listen with no transcriber: a turn, and nothing to say of it"
    assert TurnRecord(turn=1, tail_ms=900.0, voice_first_ms=200.0).line() == (
        "turn gap 1100 ms = quiet 900 + voice 200; nothing left the body"
    )
    assert TurnRecord(turn=1, stt_audio_s=1.2, stt_host="api.deepgram.com").line() == (
        "turn gap - (nothing was spoken); sent 1.2 s of audio to api.deepgram.com"
    ), "a wake and then nothing still cost the transcriber its audio"


def test_the_line_never_reads_as_the_line_under_a_wake():
    for record in (example(), example(voice_first_ms=None), example(stt_host="cut.lag.example")):
        line = record.line()
        assert "name cut" not in line and not line.startswith("lag")
        assert not PER_WAKE.search(line) and not REPLAY_LINE.match("    " + line)


def test_every_outcome_is_accepted_and_any_other_refused_wherever_it_is_set():
    for outcome in OUTCOMES:
        assert TurnRecord(turn=1, outcome=outcome).outcome == outcome
    with pytest.raises(ValueError, match="unknown turn outcome 'answred'"):
        TurnRecord(turn=1, outcome="answred")
    record = TurnRecord(turn=1)
    with pytest.raises(ValueError, match="unknown turn outcome 'cut_at_limit'"):
        record.outcome = "cut_at_limit"
    assert record.outcome == "not_answered", "a refused outcome leaves the old one"
    record.outcome = "failed"
    assert record.outcome == "failed"


@pytest.mark.parametrize("turn", [0, -1, True, 1.0, "1"])
def test_a_turn_is_counted_from_one(turn):
    with pytest.raises(ValueError, match="a turn is counted from 1"):
        TurnRecord(turn=turn)


def test_record_turn_keeps_the_turns_in_order():
    s = stats()
    first, second = TurnRecord(turn=1), TurnRecord(turn=2)
    s.record_turn(first)
    s.record_turn(second)
    assert s.turn_records == [first, second]
    first.voice_first_ms = 10.0
    assert s.turn_records[0].voice_first_ms == 10.0, "the session fills the record in place"


# ------------------------------------------------------- the footer's sums


def footer(s: SessionStats, *, live: bool = False) -> list[str]:
    s.record_frame(1.0)
    return s.report(live=live).splitlines()


def labels(rows: list[str]) -> list[str]:
    return [row[2:16].strip() for row in rows]


def test_the_four_lines_go_after_voice_done_and_before_clock():
    s = stats()
    s.speech_first_ms.append(400.0)
    s.speech_done_ms.append(900.0)
    s.record_turn(example())
    for live in (False, True):
        rows = footer(s, live=live)
        order = labels(rows)
        at = order.index("voice done")
        assert order[at + 1:at + 6] == ["turn gap", "tokens", "sent", "cleaned", "clock"]
        assert rows[0].startswith("  frames ") and rows[-1].startswith("  verdict ")
        assert not any(row.startswith("  name cut") for row in rows[at + 1:at + 5])


def test_the_new_lines_keep_the_label_column():
    s = stats()
    s.record_turn(example())
    for row in footer(s):
        if row[2:16].strip() in ("turn gap", "tokens", "sent", "cleaned"):
            assert row[16] != " " and row[15] == " ", row


def test_a_run_with_no_turns_prints_the_footer_it_always_printed():
    s = stats()
    rows = footer(s)
    assert not {"turn gap", "tokens", "sent", "cleaned"} & set(labels(rows))
    s.record_turn(TurnRecord(turn=1, tail_ms=900.0))
    assert footer(s)[1:] == rows[1:], "a turn that ran no stage adds no line"


def test_the_turn_gap_line_has_the_median_the_mean_and_the_max_of_the_turns_with_a_gap():
    s = stats()
    for turn, voice in enumerate([300.0, 500.0, 1700.0, None], start=1):
        s.record_turn(TurnRecord(turn=turn, tail_ms=900.0, voice_first_ms=voice))
    line = next(row for row in footer(s) if row.startswith("  turn gap"))
    assert line == (
        "  turn gap      median 1400 ms   mean 1733   max 2600   "
        "(3 turn(s), last loud frame to first sound)"
    )


def test_the_tokens_line_sums_the_turns_that_counted_them():
    s = stats()
    s.record_turn(example())
    s.record_turn(example(turn=4, input_tokens=1000, output_tokens=20))
    s.record_turn(example(turn=5, input_tokens=None, output_tokens=None))
    line = next(row for row in footer(s) if row.startswith("  tokens"))
    assert line == (
        "  tokens        1967 in, 38 out   (2 turn(s) that counted them; cached tokens are counted from 0.6)"
    )
    one_side = stats()
    one_side.record_turn(example(input_tokens=None))
    assert next(row for row in footer(one_side) if row.startswith("  tokens")).startswith("  tokens        18 out   (")


def test_the_sent_line_sums_each_stage_and_host():
    s = stats()
    s.record_turn(example(turn=1, voice_host="api.deepgram.com"))
    s.record_turn(example(turn=2, stt_audio_s=1.2, llm_chars=29, llm_host="192.168.1.20:9380"))
    line = next(row for row in footer(s) if row.startswith("  sent"))
    assert line == (
        "  sent          4.5 s of audio to api.deepgram.com, 2071 characters to api.openai.com, "
        "29 characters to 192.168.1.20:9380, 78 characters to api.deepgram.com   (what the turns sent off the body)"
    ), "a server on the home network is off the body"


@pytest.mark.parametrize(
    "host", ["127.0.0.1:9380", "localhost:11434", "[::1]:8080", "0.0.0.0:8080", "127.0.1.1", "LOCALHOST"]
)
def test_a_server_on_this_machine_is_not_counted_as_having_left_the_body(host):
    """Review findings batch/critic#5 and engine-core/claims-scope#10 of
    0.5.7: a model server on the body (DL5's llama-server at 127.0.0.1) is
    named on the record and left out of what was sent."""
    record = example(stt_host=None, voice_host=None, llm_host=host)
    assert record.llm_host == host and record.sent() == []
    assert record.line().endswith("; nothing left the body")
    s = stats()
    s.record_turn(record)
    assert "  sent          nothing left the body" in footer(s)


@pytest.mark.parametrize("host", ["api.openai.com", "192.168.1.20:9380", "[2001:db8::1]:443", "127.example.com", "localhost.example.com"])
def test_any_other_host_is_counted_as_off_the_body(host):
    assert [entry.host for entry in example(stt_host=None, voice_host=None, llm_host=host).sent()] == [host]


def test_a_run_whose_stages_all_ran_on_the_body_says_nothing_left_it():
    s = stats()
    s.record_turn(example(stt_host=None, llm_host=None, voice_host=None))
    assert "  sent          nothing left the body" in footer(s)


def test_the_cleaned_line_counts_the_sentences_cleaning_changed():
    s = stats()
    s.record_turn(example(sentences=3, cleaned=1))
    s.record_turn(example(turn=4, sentences=2, cleaned=0))
    assert "  cleaned       1 of 5 sentence(s) changed before the voice" in footer(s)
    none = stats()
    none.record_turn(example(sentences=0, cleaned=0))
    assert "cleaned" not in labels(footer(none)), "no sentence reached the voice"


def test_the_sums_read_the_records_as_the_session_left_them():
    s = stats()
    record = TurnRecord(turn=1, tail_ms=900.0)
    s.record_turn(record)
    assert s.gap_spread() is None and s.token_totals() is None and s.cleaned_totals() is None
    record.voice_first_ms, record.output_tokens, record.sentences = 100.0, 7, 1
    assert s.gap_spread() == Spread(1000.0, 1000.0, 1000.0, 1)
    assert s.token_totals() == Tokens(None, 7, 1)
    assert s.cleaned_totals() == (0, 1)
    assert s.sent_totals() == [] and s.ran_a_stage
    record.llm_host, record.llm_chars = "api.openai.com", 12
    assert s.sent_totals() == [Sent("llm", "api.openai.com", 12)]
