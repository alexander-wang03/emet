"""Voice activity detection and endpointing.

Synthetic audio throughout: silence is zeroes and speech is a square wave at a
chosen amplitude, so every threshold in these tests is an exact number rather
than a recording somebody has to trust. That makes the failures legible: when
one breaks it says which parameter moved, not "the audio sounds different now".

The behaviours worth protecting here are the ones that read as rudeness when
they go wrong: cutting somebody off, or sitting silently after they finish.
"""

from __future__ import annotations

from array import array

import pytest

from emet_engine.turn import DEFAULT_PATIENCE_MS, Endpointer, EndReason
from emet_engine.vad import EnergyVad, VadTuning, rms
from emet_sdk.types import AudioFormat

FMT = AudioFormat()  # 16 kHz, 1280 samples, 80 ms


def quiet() -> bytes:
    return b"\x00\x00" * FMT.frame_samples


def loud(amplitude: int = 4000) -> bytes:
    """A square wave, so RMS is exactly `amplitude`."""
    return array("h", [amplitude, -amplitude] * (FMT.frame_samples // 2)).tobytes()


# ------------------------------------------------------------------- energy


def test_rms_of_silence_is_zero():
    assert rms(quiet()) == 0.0


def test_rms_of_a_square_wave_is_its_amplitude():
    assert rms(loud(4000)) == pytest.approx(4000.0)


def test_rms_of_an_empty_frame_does_not_divide_by_zero():
    assert rms(b"") == 0.0


# ---------------------------------------------------------------- hysteresis


def test_one_loud_frame_is_not_speech():
    """A door closing, a cup on a table. Onset needs corroboration."""
    vad = EnergyVad(FMT)
    assert vad.feed(loud()) is False


def test_two_loud_frames_are_speech():
    vad = EnergyVad(FMT)
    vad.feed(loud())
    assert vad.feed(loud()) is True


def test_speech_survives_a_gap_between_words():
    """The pause inside a sentence must not end the turn, or the robot talks
    over the second half of everything you say."""
    vad = EnergyVad(FMT)
    for _ in range(4):
        vad.feed(loud())
    assert vad.feed(quiet()) is True
    assert vad.feed(quiet()) is True


def test_speech_ends_after_the_hangover():
    vad = EnergyVad(FMT, VadTuning(hangover_frames=3))
    for _ in range(4):
        vad.feed(loud())
    assert vad.feed(quiet()) is True
    assert vad.feed(quiet()) is True
    assert vad.feed(quiet()) is False


def test_quiet_time_is_measured_from_the_last_loud_frame():
    """Not from when the debounced flag flipped. The difference is the
    hangover, and charging it to the persona's patience would make every soul
    slower than the number in its own bundle."""
    vad = EnergyVad(FMT)
    for _ in range(3):
        vad.feed(loud())
    for i in range(1, 4):
        vad.feed(quiet())
        assert vad.quiet_frames == i
        assert vad.quiet_ms == pytest.approx(i * 80.0)


# -------------------------------------------------------------- noise floor


def test_the_floor_rises_in_a_noisy_room():
    """A kitchen and a study differ by more than any fixed threshold spans."""
    vad = EnergyVad(FMT)
    start = vad.noise_floor
    hum = loud(300)  # below the initial threshold, so it counts as room noise
    for _ in range(200):
        vad.feed(hum)
    assert vad.noise_floor > start


def test_the_floor_does_not_learn_the_person_talking():
    """Adapting during speech would teach the detector that the speaker is
    the room, and it would go deaf to them."""
    vad = EnergyVad(FMT)
    settled = vad.noise_floor
    for _ in range(50):
        vad.feed(loud(8000))
    assert vad.noise_floor == pytest.approx(settled)


def test_the_floor_falls_faster_than_it_rises():
    """A fridge switching off must not deafen the robot for a minute."""
    t = VadTuning()
    assert t.adapt_down > t.adapt_up


# ------------------------------------------------------------- endpointing


def feed_all(ep: Endpointer, frames: list[bytes]):
    for frame in frames:
        result = ep.feed(frame)
        if result is not None:
            return result
    return None


def test_a_wake_with_nothing_after_it_ends_as_no_speech():
    """Probably a false wake. The engine above must be able to tell that from
    someone who spoke, so it is a distinct reason rather than an empty turn."""
    ep = Endpointer(FMT, lead_in_ms=400)
    utterance = feed_all(ep, [quiet()] * 20)
    assert utterance is not None
    assert utterance.reason is EndReason.NO_SPEECH
    assert not utterance.had_speech
    assert utterance.audio == b""


def test_speech_then_silence_ends_the_turn():
    ep = Endpointer(FMT, patience_ms=240)  # three frames
    utterance = feed_all(ep, [loud()] * 5 + [quiet()] * 10)
    assert utterance is not None
    assert utterance.reason is EndReason.SILENCE
    assert utterance.had_speech
    assert utterance.duration_ms > 0


def test_patience_is_honoured_rather_than_approximated():
    """The persona asked for a specific silence. Ending early is an interruption
    and ending late is a robot that seems slow, so the number has to mean what
    it says."""
    ep = Endpointer(FMT, patience_ms=800)  # exactly ten 80 ms frames
    for _ in range(4):
        assert ep.feed(loud()) is None
    for i in range(1, 10):
        assert ep.feed(quiet()) is None, f"ended early after {i} quiet frames"
    assert ep.feed(quiet()) is not None


def test_a_patient_soul_waits_longer_than_an_eager_one():
    """The point of putting `patience_ms` on the soul: one engine, two
    conversational temperaments, from two data files."""

    def quiet_frames_until_end(patience: int) -> int:
        ep = Endpointer(FMT, patience_ms=patience)
        for _ in range(4):
            ep.feed(loud())
        n = 0
        while ep.feed(quiet()) is None:
            n += 1
            assert n < 100
        return n

    assert quiet_frames_until_end(1600) > quiet_frames_until_end(400)


def test_the_start_of_the_first_word_is_not_clipped():
    """Detection lags onset by `onset_frames`, so without a pre-roll every
    transcript would begin mid-word."""
    ep = Endpointer(FMT, patience_ms=240, preroll_frames=4)
    utterance = feed_all(ep, [loud()] * 3 + [quiet()] * 10)
    assert utterance is not None
    # Three loud frames, but the captured audio starts before detection did.
    assert len(utterance.audio) > 3 * FMT.frame_bytes


def test_somebody_who_will_not_stop_is_cut_off_and_told_so():
    ep = Endpointer(FMT, max_utterance_ms=400)
    utterance = feed_all(ep, [loud()] * 30)
    assert utterance is not None
    assert utterance.reason is EndReason.TOO_LONG
    assert utterance.had_speech


def test_a_source_ending_mid_turn_returns_what_was_caught():
    """A truncated question is more useful than silence."""
    ep = Endpointer(FMT)
    for _ in range(5):
        ep.feed(loud())
    utterance = ep.close()
    assert utterance.reason is EndReason.SOURCE_ENDED
    assert utterance.audio


def test_a_source_ending_early_is_not_reported_as_a_false_wake():
    """`NO_SPEECH` means the robot waited its full lead-in and nobody spoke,
    which is evidence about the detector. A recording that simply stopped is
    evidence about the recording."""
    ep = Endpointer(FMT)
    ep.feed(quiet())
    utterance = ep.close()
    assert utterance.reason is EndReason.SOURCE_ENDED
    assert not utterance.had_speech


def test_an_endpointer_can_be_reused_for_the_next_turn():
    ep = Endpointer(FMT, patience_ms=160)
    first = feed_all(ep, [loud()] * 4 + [quiet()] * 8)
    assert first is not None and first.had_speech
    second = feed_all(ep, [loud()] * 4 + [quiet()] * 8)
    assert second is not None and second.had_speech


def test_the_documented_default_is_what_the_spec_says():
    """A bundle written against the specification must behave as it reads."""
    assert DEFAULT_PATIENCE_MS == 900
    assert Endpointer(FMT).patience_ms == 900
