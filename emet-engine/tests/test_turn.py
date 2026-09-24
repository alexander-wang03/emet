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


# ------------------------------------------------------------ trailing clause


from emet_engine.turn import looks_incomplete  # noqa: E402


@pytest.mark.parametrize(
    ("text", "incomplete"),
    [
        ("", False),
        ("Where are my keys?", False),
        ("where are my keys", False),
        ("where are my", True),
        ("Can you tell me where the", True),
        ("I would like tea,", True),
        ("Fine. And then the", True),
        ("Fine. And then the cat", True),
        ("what time is it", False),
        ("Tell me.", False),
        ("Hmm, um", True),
        ("It costs 3.5 dollars", False),
        ("Thank you", False),
        ('He said "and', True),
    ],
)
def test_a_transcript_reads_as_finished_or_not(text, incomplete):
    assert looks_incomplete(text) is incomplete


def quiet_frames_until_end(ep: Endpointer) -> int:
    """Speak, then count the quiet frames until the turn ends."""
    for _ in range(5):
        assert ep.feed(loud()) is None
    n = 0
    while True:
        n += 1
        if ep.feed(quiet()) is not None:
            return n


def test_an_unfinished_transcript_earns_one_more_window():
    plain = quiet_frames_until_end(Endpointer(FMT, patience_ms=900))
    extended = quiet_frames_until_end(Endpointer(FMT, patience_ms=900, extend_if=lambda: True))
    assert extended == 2 * plain or extended == 2 * plain - 1 or extended == 2 * plain + 1
    assert extended > plain


def test_a_finished_transcript_earns_nothing():
    plain = quiet_frames_until_end(Endpointer(FMT, patience_ms=900))
    asked: list[int] = []

    def no() -> bool:
        asked.append(1)
        return False

    assert quiet_frames_until_end(Endpointer(FMT, patience_ms=900, extend_if=no)) == plain
    assert len(asked) == 1, "asked once, at the moment silence ran out the patience"


def test_the_extension_is_granted_once_per_turn():
    """Speech resumes inside the extension, then stops: the next silence is
    judged by plain patience, with no second extension."""
    ep = Endpointer(FMT, patience_ms=900, extend_if=lambda: True)
    for _ in range(5):
        ep.feed(loud())
    patience_frames = quiet_frames_until_end(Endpointer(FMT, patience_ms=900))
    for _ in range(patience_frames):
        assert ep.feed(quiet()) is None, "the first silence was extended"
    for _ in range(5):
        assert ep.feed(loud()) is None
    n = 0
    utterance = None
    while utterance is None:
        n += 1
        utterance = ep.feed(quiet())
    assert n == patience_frames
    assert utterance.extended
    assert utterance.reason is EndReason.SILENCE


def test_the_utterance_says_whether_it_was_extended():
    ep = Endpointer(FMT, patience_ms=900)
    for _ in range(5):
        ep.feed(loud())
    utterance = None
    while utterance is None:
        utterance = ep.feed(quiet())
    assert not utterance.extended


# ------------------------------------------------------------- its own click


def click() -> list[bytes]:
    """The listening click as the robot's own microphone hears it on a body
    with no echo cancellation: loud, and long enough to be speech to the
    detector, which wants two loud frames."""
    return [loud(8000)] * 2


def said(n: int) -> list[bytes]:
    """`n` loud frames, each a different amplitude, so a test can find each
    one in the captured audio."""
    return [loud(3000 + 100 * i) for i in range(n)]


def frames_until_end(ep: Endpointer, frames: list[bytes]):
    """Feed until the turn ends; the utterance and how many frames it took."""
    for n, frame in enumerate(frames, 1):
        utterance = ep.feed(frame)
        if utterance is not None:
            return utterance, n
    return None, len(frames)


def test_the_click_without_deafen_starts_speech():
    """The fault `deafen` exists for: the robot's own click opens the turn,
    and the patience then runs from a sound nobody made."""
    ep = Endpointer(FMT, patience_ms=240, lead_in_ms=800)
    for frame in click():
        ep.feed(frame)
    assert ep.started
    utterance, _ = frames_until_end(ep, [quiet()] * 20)
    assert utterance is not None
    assert utterance.reason is EndReason.SILENCE
    assert utterance.had_speech


def test_a_click_inside_the_deaf_window_starts_nothing():
    """Nobody spoke, so the turn ends as a false wake at the full lead-in,
    exactly as it would have with no click at all."""
    ep = Endpointer(FMT, patience_ms=240, lead_in_ms=800)  # ten frames
    ep.deafen(2 * FMT.frame_ms)
    for frame in click():
        assert ep.feed(frame) is None
    assert not ep.started
    utterance, n = frames_until_end(ep, [quiet()] * 20)
    assert utterance is not None
    assert utterance.reason is EndReason.NO_SPEECH
    assert not utterance.had_speech
    assert len(click()) + n == 10, "the deaf frames count toward the lead-in"


def test_the_detector_never_hears_the_deaf_window():
    """So the click cannot teach the noise floor that the room is loud."""
    ep = Endpointer(FMT)
    floor = ep.vad.noise_floor
    ep.deafen(2 * FMT.frame_ms)
    for frame in click():
        ep.feed(frame)
    assert ep.vad.frames == 0
    assert ep.vad.noise_floor == floor


def test_speech_that_runs_past_the_window_is_caught_with_its_pre_roll():
    """Somebody who starts talking over the click loses nothing: the frames
    inside the window wait in the pre-roll, and the turn starts with them."""
    words = said(6)
    ep = Endpointer(FMT, patience_ms=240, preroll_frames=4)
    ep.deafen(2 * FMT.frame_ms)
    utterance, _ = frames_until_end(ep, words + [quiet()] * 10)
    assert utterance is not None
    assert utterance.reason is EndReason.SILENCE
    assert utterance.audio.startswith(b"".join(words))


def test_after_the_window_the_endpointer_behaves_exactly_as_before():
    """A window over frames that were quiet anyway changes nothing: the same
    audio is captured and the turn ends on the same frame."""
    frames = [quiet()] * 3 + said(5) + [quiet()] * 10
    plain, plain_n = frames_until_end(Endpointer(FMT, patience_ms=240), frames)

    ep = Endpointer(FMT, patience_ms=240)
    ep.deafen(3 * FMT.frame_ms)
    deafened, deaf_n = frames_until_end(ep, frames)

    assert plain is not None and plain.reason is EndReason.SILENCE
    assert deafened == plain
    assert deaf_n == plain_n


@pytest.mark.parametrize("ms", [0, -80])
def test_deafen_zero_changes_nothing(ms):
    frames = click() + said(3) + [quiet()] * 10
    plain, plain_n = frames_until_end(Endpointer(FMT, patience_ms=240), frames)
    ep = Endpointer(FMT, patience_ms=240)
    ep.deafen(ms)
    deafened, deaf_n = frames_until_end(ep, frames)
    assert deafened == plain
    assert deaf_n == plain_n


def test_a_frame_that_begins_inside_the_window_is_ignored_whole():
    """A window that ends partway through a frame still covers that frame:
    the click may be in its first few milliseconds."""
    ep = Endpointer(FMT)
    ep.deafen(FMT.frame_ms + 1)
    for frame in click():
        ep.feed(frame)
    assert ep.vad.frames == 0
    ep.feed(loud())
    assert ep.vad.frames == 1


@pytest.mark.parametrize(("rate", "samples"), [(11025, 160), (16000, 1280), (44100, 1024)])
def test_a_window_of_whole_frames_deafens_exactly_that_many(rate, samples):
    """At any frame length. Found 2026-09-23: at 11025 Hz with 160-sample
    frames, a three-frame window spent one frame at a time in floating point
    left a crumb, and a fourth frame went unheard."""
    fmt = AudioFormat(sample_rate=rate, frame_samples=samples)
    frame = array("h", [8000, -8000] * (samples // 2)).tobytes()
    for k in (1, 3, 5, 7):
        ep = Endpointer(fmt, lead_in_ms=60000)
        ep.deafen(k * fmt.frame_ms)
        for _ in range(k):
            ep.feed(frame)
        assert ep.vad.frames == 0
        ep.feed(frame)
        assert ep.vad.frames == 1, f"{k} frames of window deafened {k + 1}"


def loud_frames_until_speech(ep: Endpointer) -> int:
    n = 0
    while not ep.started:
        n += 1
        assert n < 50
        ep.feed(loud())
    return n


def test_deafen_twice_keeps_the_larger_remaining_window():
    onset = loud_frames_until_speech(Endpointer(FMT))

    # Five frames, two spent, then a shorter call: three frames remain.
    ep = Endpointer(FMT)
    ep.deafen(5 * FMT.frame_ms)
    ep.feed(quiet())
    ep.feed(quiet())
    ep.deafen(2 * FMT.frame_ms)
    assert loud_frames_until_speech(ep) == 3 + onset

    # The same, then a longer call: the longer one wins.
    ep = Endpointer(FMT)
    ep.deafen(5 * FMT.frame_ms)
    ep.feed(quiet())
    ep.feed(quiet())
    ep.deafen(6 * FMT.frame_ms)
    assert loud_frames_until_speech(ep) == 6 + onset


def test_the_window_ends_with_the_turn():
    """A window longer than the lead-in does not carry into the next turn,
    which has its own click and its own window."""
    ep = Endpointer(FMT, lead_in_ms=400)
    ep.deafen(5000)
    first, _ = frames_until_end(ep, [quiet()] * 20)
    assert first is not None and first.reason is EndReason.NO_SPEECH
    assert loud_frames_until_speech(ep) == loud_frames_until_speech(Endpointer(FMT))


def test_deafen_once_speech_has_started_does_nothing():
    """The frames after the start are the person's, and none of them may be
    lost or kept from the silence that ends the turn."""
    frames = said(4) + [quiet()] * 10
    plain, plain_n = frames_until_end(Endpointer(FMT, patience_ms=240), frames)

    ep = Endpointer(FMT, patience_ms=240)
    for frame in frames[:3]:
        ep.feed(frame)
    assert ep.started
    ep.deafen(800)
    late, late_n = frames_until_end(ep, frames[3:])
    assert late == plain
    assert late_n + 3 == plain_n


def test_the_room_heard_inside_the_window_still_sets_the_floor():
    """The frames straight after a wake are often the quietest of the turn,
    and the only chance a new turn's detector has to learn the room before
    the person starts. Kept out of the floor, they left it at the absolute
    floor, well above a quiet room, and a soft voice read as silence:
    replaying the reference body's ten-minute recording on the laptop with a
    three-frame window, 20 of 45 turns ended at a different frame
    (2026-09-23). A quiet frame in the window still lowers the floor; the
    click does not raise it."""
    room = loud(60)
    ep = Endpointer(FMT)
    ep.deafen(3 * FMT.frame_ms)
    for frame in [room] * 3:
        ep.feed(frame)
    assert ep.vad.noise_floor < ep.vad.tuning.absolute_floor
    assert not ep.started


def test_soft_speech_after_the_window_ends_where_it_would_have_without_it():
    """The regression the floor fix is for, in miniature: the room, a word
    at full voice, then the rest of the sentence softly. The window over the
    room must not move the end of the turn."""
    frames = [loud(60)] * 3 + [loud(2000)] * 4 + [loud(400)] * 14 + [quiet()] * 30

    def end(deaf_frames: int) -> int:
        ep = Endpointer(FMT, patience_ms=900)
        if deaf_frames:
            ep.deafen(deaf_frames * FMT.frame_ms)
        _, n = frames_until_end(ep, frames)
        return n

    assert end(3) == end(0)
