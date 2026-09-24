"""System tones: the rising pair, the click and the falling pair.

Every number here is read off the rendered samples: the length, the silence
at each end, the fade, the pitch counted from zero crossings. A tone that
drifts from its preset says which part moved.

The test that matters most for everything else is the one against
`emet_providers.mock.read_back`. Engine tests record the robot through a
`wav` sink and read the mock voice's words back out of the file the tones
are written into, so a tone that happened to spell a letter would put a
word in the robot's mouth that nobody gave it.
"""

from __future__ import annotations

import math
from array import array
from types import MappingProxyType

import pytest

from emet_engine import tones
from emet_engine.tones import PRESETS, UnknownTone, duration_ms, render
from emet_providers.mock import read_back
from emet_sdk.resolve import load_chains

RATES = (8000, 16000, 22050, 24000, 44100, 48000)


def samples_of(pcm: bytes) -> array:
    out = array("h")
    out.frombytes(pcm)
    return out


def note_spans(samples) -> list[tuple[int, int]]:
    """(first sample, end sample) of each run of sound, found in the audio.

    The renderer never writes a zero sample inside a note, so the runs of
    nonzero samples are the notes, and a test that counts them checks that
    too.
    """
    spans = []
    start = None
    for i, value in enumerate(samples):
        if value and start is None:
            start = i
        elif not value and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(samples)))
    return spans


def notes(preset: str) -> list[tuple[float, float]]:
    """The preset's sounding notes, gaps left out."""
    return [(hz, ms) for hz, ms in PRESETS[preset] if hz > 0]


def spoken(word: str) -> bytes:
    """One word as the mock voice writes it: the bytes, then zeros."""
    return word.encode("ascii") + bytes(64 - len(word))


def crossings(samples) -> int:
    return sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))


# ------------------------------------------------------------ the vocabulary


def test_every_tone_the_chains_name_can_be_played():
    """The chains are data and this module is code. A preset renamed in one
    and not the other would leave the robot silent at boot."""
    named = {
        str(rung.params.get("preset"))
        for chain in load_chains().values()
        for rung in chain.rungs
        if rung.is_voice and rung.action == "tone"
    }
    assert named == {"rising_pair", "soft_click", "falling_pair"}
    assert named <= set(PRESETS)


def test_the_pairs_rise_and_fall_and_the_click_is_one_short_note():
    assert PRESETS["rising_pair"] == ((660.0, 90.0), (0.0, 40.0), (880.0, 90.0))
    assert PRESETS["falling_pair"] == tuple(reversed(PRESETS["rising_pair"]))
    assert PRESETS["soft_click"] == ((1500.0, 25.0),)


def test_the_presets_cannot_be_changed_at_run_time():
    with pytest.raises(TypeError):
        PRESETS["rising_pair"] = ((440.0, 1000.0),)  # type: ignore[index]


@pytest.mark.parametrize("preset", ["", "beep", "Rising_Pair"])
def test_an_unknown_tone_is_refused_and_the_known_ones_are_named(preset):
    with pytest.raises(UnknownTone) as caught:
        render(preset, 16000)
    for known in PRESETS:
        assert known in str(caught.value)
    with pytest.raises(UnknownTone):
        duration_ms(preset)


def test_an_unknown_tone_is_a_value_error():
    """So a caller that already handles bad chain values handles this one."""
    assert issubclass(UnknownTone, ValueError)


@pytest.mark.parametrize(("rate", "level"), [(0, 0.25), (-16000, 0.25), (16000, -0.1), (16000, 1.5)])
def test_a_rate_or_a_level_out_of_range_is_refused(rate, level):
    with pytest.raises(ValueError):
        render("soft_click", rate, level=level)


# ----------------------------------------------------------------- the shape


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_a_render_is_as_long_as_its_duration_says(preset, rate):
    pcm = render(preset, rate)
    assert len(pcm) % 2 == 0
    rendered = len(pcm) // 2
    assert abs(rendered - duration_ms(preset) * rate / 1000) <= 1


def test_the_durations_count_the_silences():
    """10 ms of silence at each end. The number is written out here, where
    the module's own `PAD_MS` would agree with any value it was changed to."""
    assert duration_ms("rising_pair") == 90 + 40 + 90 + 2 * 10
    assert duration_ms("falling_pair") == duration_ms("rising_pair")
    assert duration_ms("soft_click") == 25 + 2 * 10


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_tone_starts_and_ends_in_exact_silence(preset, rate):
    """Within a sample of 10 ms at each end, whatever the rate."""
    samples = samples_of(render(preset, rate))
    spans = note_spans(samples)
    assert len(spans) == len(notes(preset))
    pad = 10 * rate / 1000
    assert abs(spans[0][0] - pad) <= 1
    assert abs(len(samples) - spans[-1][1] - pad) <= 1


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_each_note_lasts_as_long_as_its_preset_says(preset, rate):
    spans = note_spans(samples_of(render(preset, rate)))
    for (start, end), (_, ms) in zip(spans, notes(preset), strict=True):
        assert abs((end - start) - ms * rate / 1000) <= 1


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("preset", ["rising_pair", "falling_pair"])
def test_the_gap_between_a_pairs_notes_is_exact_silence(preset, rate):
    samples = samples_of(render(preset, rate))
    (_, first_end), (second_start, _) = note_spans(samples)
    assert abs((second_start - first_end) - 40 * rate / 1000) <= 1
    assert set(samples[first_end:second_start]) == {0}


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_note_fades_in_and_out_over_four_milliseconds(preset):
    """A sine that starts or stops at full height clicks.

    The fade is a straight line from silence to the note's peak over 4 ms:
    no sample inside it rises above that line, which a shorter fade would,
    and the note is at full height within one cycle after it, which a
    longer fade would not be."""
    rate = 48000
    samples = samples_of(render(preset, rate))
    fade = round(4 * rate / 1000)
    for (start, end), (hz, _) in zip(note_spans(samples), notes(preset), strict=True):
        note = samples[start:end]
        peak = max(abs(s) for s in note)
        cycle = math.ceil(rate / hz)
        assert abs(note[0]) <= 1 and abs(note[-1]) <= 1
        for i in range(fade):
            # 2% over the line: the sampled peak can sit a little under the
            # sine's true one.
            line = 1.02 * peak * i / fade + 1
            assert abs(note[i]) <= line and abs(note[-1 - i]) <= line
        assert max(abs(s) for s in note[fade : fade + cycle]) >= 0.99 * peak
        assert max(abs(s) for s in note[-fade - cycle : -fade]) >= 0.99 * peak


@pytest.mark.parametrize(
    ("preset", "pitches"),
    [("rising_pair", (660, 880)), ("falling_pair", (880, 660)), ("soft_click", (1500,))],
)
def test_each_note_sounds_at_its_preset_pitch(preset, pitches):
    """Counted from zero crossings, two per cycle."""
    rate = 24000
    samples = samples_of(render(preset, rate))
    heard = []
    for start, end in note_spans(samples):
        seconds = (end - start) / rate
        heard.append(crossings(samples[start:end]) / 2 / seconds)
    assert len(heard) == len(pitches)
    for measured, expected in zip(heard, pitches):
        assert measured == pytest.approx(expected, rel=0.05)


# -------------------------------------------------------------- the loudness


def peak(preset: str, level: float = 0.25) -> int:
    return max(abs(s) for s in samples_of(render(preset, 48000, level=level)))


def test_the_default_level_is_a_quarter_of_full_scale():
    assert peak("rising_pair") == pytest.approx(0.25 * 32767, rel=0.01)


def test_the_click_is_a_third_as_loud_as_the_pairs():
    """It answers every wake, so it is the one heard most often."""
    assert peak("soft_click") == pytest.approx(peak("rising_pair") / 3, rel=0.02)


def test_the_level_scales_every_note():
    assert peak("falling_pair", 0.5) == pytest.approx(2 * peak("falling_pair", 0.25), rel=0.01)
    assert peak("falling_pair", 1.0) <= 32767


def test_a_render_is_cached_and_the_default_level_is_the_same_render():
    assert render("soft_click", 16000) is render("soft_click", 16000)
    assert render("soft_click", 16000) is render("soft_click", 16000, level=0.25)
    assert render("soft_click", 16000) != render("soft_click", 16000, level=0.5)


# ------------------------------------------------ what the mock voice reads


def test_a_sine_crossing_zero_can_spell_a_letter():
    """The hazard the renderer avoids: samples of 0, 65 and 0 are the
    letter "A" between runs of zero bytes, which `read_back` takes for a
    word."""
    assert read_back(array("h", [0, 0, 65, 0, 0]).tobytes()) == "A"


@pytest.mark.parametrize("level", [0.0, 0.01, 0.1, 0.25, 0.5, 1.0])
@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_no_tone_reads_back_as_words(preset, rate, level):
    assert read_back(render(preset, rate, level=level)) == ""


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_no_note_holds_two_zero_bytes_in_a_row(preset, rate):
    """Why no tone reads back as words: each note is one run of bytes to
    `read_back`, and every note holds the high byte of a negative sample,
    which no printable word has."""
    pcm = render(preset, rate)
    for start, end in note_spans(samples_of(pcm)):
        note = pcm[2 * start : 2 * end]
        assert b"\x00\x00" not in note
        assert any(b > 0x7E for b in note)


@pytest.mark.parametrize(("hz", "rate"), [(1000.0, 8000), (2000.0, 8000), (2000.0, 16000), (1500.0, 24000)])
def test_a_note_that_lands_on_exact_zeros_still_reads_as_nothing(monkeypatch, hz, rate):
    """Notes that spell words when every sample is rounded plainly (found by
    search, 2026-09-23): 1000 Hz at 8 kHz, for one, crosses zero exactly on
    a sample every half cycle. The rule holds for any note, so a preset
    added later cannot put words in the robot's mouth either."""
    # A name per note: renders are cached by name, rate and level.
    probe = f"probe {hz:g} Hz"
    monkeypatch.setattr(tones, "PRESETS", MappingProxyType({**PRESETS, probe: ((hz, 90.0),)}))
    for step in range(51):
        assert read_back(render(probe, rate, level=step / 50)) == ""


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_the_words_around_a_tone_are_read_back_unchanged(preset):
    """The way an engine test's recording looks: words, a tone, words."""
    pcm = spoken("hello") + spoken("there") + render(preset, 16000) + spoken("again")
    assert read_back(pcm) == "hello there again"


def test_the_engine_renders_exactly_the_tones_the_sdk_lets_a_chain_name():
    """The validator refuses a `tone` rung naming anything outside
    `TONE_PRESETS`; this module is what plays them. Add a preset to one
    without the other and a chain either cannot use it or goes quiet."""
    from emet_sdk.chains import TONE_PRESETS

    from emet_engine.tones import PRESETS

    assert set(PRESETS) == TONE_PRESETS
