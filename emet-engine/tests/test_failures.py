"""The table of failure lines, `emet_engine/failures.py`, held to its rules.

Each rule here is one a row could break by a typo that no snapshot would
show until the failure happened on a body: two rows a turn could match, a
fault kind `Health` refuses, a spoken line the soul has no words for, a
field the session never fills.
"""

from __future__ import annotations

from collections import Counter

import pytest

from emet_sdk.intents import ALL_INTENTS
from emet_sdk.types import FAULT_KINDS

from emet_engine import failures
from emet_engine.failures import FAILURES, FIELDS, OUTCOMES, PROGRAMS, Failure, fields_of, find, row
from emet_engine.prompting import DEFAULT_LINES


def test_no_two_rows_answer_the_same_four_facts():
    keys = Counter((f.when, f.stage, f.outcome, f.fault) for f in FAILURES)
    assert [key for key, n in keys.items() if n > 1] == []


def test_no_two_rows_share_a_case():
    names = Counter(f.case for f in FAILURES)
    assert [name for name, n in names.items() if n > 1] == []


def test_every_fault_is_a_kind_health_accepts_or_none():
    assert [f.case for f in FAILURES if f.fault is not None and f.fault not in FAULT_KINDS] == []


def test_every_spoken_line_is_one_the_engine_has_words_for():
    assert [f.case for f in FAILURES if f.spoken is not None and f.spoken not in DEFAULT_LINES] == []


def test_every_console_text_reads_only_the_four_fields():
    for f in FAILURES:
        for program in sorted(f.programs):
            assert fields_of(f.template(program)) <= FIELDS, (f.case, program)


def test_every_signal_is_a_signal_intent_the_sdk_names():
    assert [f.case for f in FAILURES if f.signal is not None and f.signal not in ALL_INTENTS["signal"]] == []


def test_the_shape_of_every_row():
    """A boot row has no outcome and is printed as a refusal on stderr; a
    turn's row has an outcome from the per-turn record's vocabulary and is
    printed with the turn, on stdout."""
    for f in FAILURES:
        assert f.when in {"boot", "before", "during"}, f.case
        assert f.stage in {"stt", "llm", "voice", "source", "sink", "registry"}, f.case
        assert f.stream in {"stdout", "stderr"}, f.case
        assert f.signal in {None, "offline", "unavailable"}, f.case
        assert f.programs and f.programs <= PROGRAMS, f.case
        assert isinstance(f.programs, frozenset), f.case
        if f.when == "boot":
            assert (f.outcome, f.stream, f.spoken, f.signal) == (None, "stderr", None, None), f.case
        else:
            assert f.outcome in OUTCOMES and f.stream == "stdout", f.case


def test_offline_is_shown_for_network_and_server_errors_only():
    """Stage 0.5 (f) of 0.5.7: a retired model, an empty reply and an unpaid
    account each happen with the network up."""
    assert sorted(f.case for f in FAILURES if f.signal == "offline") == ["llm-failed", "stt-words-lost"]


def test_find_picks_the_row_for_each_stop():
    assert find("during", "llm", "failed", None) is row("llm-failed")
    assert find("before", "llm", "unavailable", "model_unavailable") is row("llm-unavailable-before")
    assert find("during", "stt", "words_lost", "model_unavailable") is row("stt-model-rejected")
    # A turn that ended well, or with today's lines, has no row.
    for outcome in ("answered", "declined", "nothing_heard", "no_speech", "not_answered"):
        assert find("during", "llm", outcome, None) is None


def test_a_row_is_found_by_its_case_and_an_unknown_case_names_the_rows():
    assert row("sink-cannot-start").stage == "sink"
    with pytest.raises(KeyError, match="no failure row 'sink-gone'; the rows are duplicate-plugin, "):
        row("sink-gone")


def test_a_line_is_filled_and_a_missing_detail_leaves_no_trailing_space():
    cannot = row("llm-cannot-start")
    assert cannot.text("emet-talk", name="openai", detail="no key.") == (
        "the language model 'openai' cannot start, so the robot would hear questions it can never answer. "
        "It will not run. no key."
    )
    assert cannot.text("emet-listen", name="openai").endswith("It will not run.")
    empty = row("llm-empty-at-cap")
    assert empty.text("emet-talk", cap=300).startswith("  (no words: the reply stopped at its cap of 300 tokens")
    assert empty.text("emet-listen", cap=300).startswith("    no words: the reply stopped at its cap of 300 tokens")
    assert empty.text("emet-talk", cap=300) == f"  ({empty.text('emet-listen', cap=300)[4:]})"


def test_a_line_refuses_a_field_and_a_program_it_does_not_know():
    failed = row("llm-failed")
    with pytest.raises(ValueError, match="no field 'reason' in a failure line; the fields are cap, detail, exc, name"):
        failed.text("emet-talk", reason="x")
    with pytest.raises(ValueError, match="no failure line for the program 'emet-say'"):
        failed.text("emet-say", detail="x")


def test_the_source_line_quotes_the_error():
    source = row("source-cannot-start")
    exc = "could not open 'nothere.wav': [Errno 2] No such file or directory: 'nothere.wav'"
    assert source.text("emet-listen", name="wav", exc=exc) == (
        "the audio source 'wav' cannot start, so the robot would listen to nothing. It will not run. " + exc
    )


def test_the_module_exports_what_it_names():
    assert set(failures.__all__) == {name for name in failures.__all__ if hasattr(failures, name)}
    assert isinstance(FAILURES[0], Failure)
