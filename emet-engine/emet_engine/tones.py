"""System tones: the sounds the voice rung makes that are not words.

The chains name three (`emet_sdk/chains/core.yaml`): a rising pair when the
robot boots (`signal.booting`), a soft click when it wakes and starts to
listen (`signal.listening`), and a falling pair when it is muted
(`signal.muted`). They are the whole vocabulary of the `tone` voice action,
and a body with a speaker and nothing else shows those three states this way.

**Synthesised here, at the sink's rate.** A tone is a few sine notes,
rendered in pure Python when first asked for and cached after that. The
voice states the sample rate and the sink opens to match, so a tone has to
arrive at whatever rate that turned out to be; a recorded file would arrive
at one rate and be one more asset to license.

**The click is quiet and short.** It answers "hey emet", and on a body with
no echo cancellation (the reference body says `aec: none`) the robot's own
microphone hears it. So it is 25 ms long at a third of the pairs' level, and
`Endpointer.deafen` keeps the robot from taking its own click for the person
starting to speak.

Each note fades in and out over 4 ms, because a sine that starts or stops at
full height is heard as a click of its own. The buffer carries 10 ms of exact
silence at each end, so a tone never runs into the sound before or after it.

**No zero low byte inside a note.** Engine tests record the robot through a
`wav` sink and read the mock voice's words back out of the same file
(`emet_providers.mock.read_back`), which takes any run of printable bytes
between two zero bytes for a word. A sine crossing zero can write exactly
that: samples of 0, 65 and 0 are the bytes of the letter "A" with zeros on
either side. The three presets happen never to do it at the six rates the
tests use, from 8 to 48 kHz. Other notes do: 1000 Hz at 8 kHz lands on an
exact zero every half cycle and spells a line of nonsense at a dozen levels,
and the click's own 1500 Hz does at 24 kHz when played at 0.72 of full scale
(checked 2026-09-23: 186 of 8160 renders across ten pitches). So a sample
whose low byte would be zero is moved up by one step, about 90 dB below full
scale and far below hearing. No note then holds two zero bytes in a row, and
each note reads as one run containing the high bytes of negative samples,
which no word has. That holds for any preset at any rate.
"""

from __future__ import annotations

import math
import sys
from array import array
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

__all__ = ["UnknownTone", "PRESETS", "PAD_MS", "FADE_MS", "duration_ms", "render"]

#: Each preset as its notes in order, `(frequency_hz, ms)`. A frequency of 0
#: is a gap of exact silence.
PRESETS: Mapping[str, tuple[tuple[float, float], ...]] = MappingProxyType(
    {
        "rising_pair": ((660.0, 90.0), (0.0, 40.0), (880.0, 90.0)),
        "soft_click": ((1500.0, 25.0),),
        "falling_pair": ((880.0, 90.0), (0.0, 40.0), (660.0, 90.0)),
    }
)

#: A preset's loudness as a share of `level`, where it is not the whole of it.
#: The click is heard at every wake, a third as loud as the pairs.
_LOUDNESS: Mapping[str, float] = MappingProxyType({"soft_click": 1.0 / 3.0})

#: Exact silence before and after every tone.
PAD_MS = 10.0
#: The linear fade at each end of a note.
FADE_MS = 4.0

_FULL_SCALE = 32767


class UnknownTone(ValueError):
    """A tone preset this engine cannot play. The message names the ones it
    can, because a chain file is edited by hand."""


def _notes(preset: str) -> tuple[tuple[float, float], ...]:
    try:
        return PRESETS[preset]
    except KeyError:
        known = ", ".join(sorted(PRESETS))
        raise UnknownTone(f"no tone called {preset!r}; the tones are: {known}") from None


def duration_ms(preset: str) -> float:
    """How long the rendered buffer lasts, the silences at each end included.

    The same at every rate: a render is cut at sample boundaries rounded from
    this running total, so it is never more than half a sample away.
    """
    return sum(ms for _, ms in _notes(preset)) + 2 * PAD_MS


def render(preset: str, sample_rate: int, *, level: float = 0.25) -> bytes:
    """One preset as mono int16 little-endian PCM at `sample_rate`.

    `level` is the loudest note's peak as a share of full scale; the default,
    a quarter, is 12 dB below it. Renders are cached, so a click at every
    wake costs its arithmetic once.
    """
    _notes(preset)
    rate = int(sample_rate)
    if rate <= 0:
        raise ValueError(f"a tone needs a positive sample rate, got {sample_rate!r}")
    if not 0.0 <= level <= 1.0:
        raise ValueError(f"a tone's level is a share of full scale between 0 and 1, got {level!r}")
    return _render(preset, rate, float(level))


@lru_cache(maxsize=32)
def _render(preset: str, rate: int, level: float) -> bytes:
    peak = level * _LOUDNESS.get(preset, 1.0) * _FULL_SCALE
    fade = max(1, round(FADE_MS * rate / 1000))
    segments = ((0.0, PAD_MS), *PRESETS[preset], (0.0, PAD_MS))

    samples = array("h")
    elapsed_ms = 0.0
    start = 0
    for hz, ms in segments:
        # Boundaries from the running total, so rounding never accumulates
        # and the buffer's length agrees with `duration_ms` at any rate.
        elapsed_ms += ms
        end = round(elapsed_ms * rate / 1000)
        n = end - start
        start = end
        if hz <= 0:
            samples.frombytes(bytes(2 * n))
            continue
        step = 2 * math.pi * hz / rate
        for i in range(n):
            envelope = min(1.0, i / fade, (n - 1 - i) / fade)
            value = round(peak * envelope * math.sin(step * i))
            if value & 0xFF == 0:
                value += 1
            samples.append(value)

    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()
