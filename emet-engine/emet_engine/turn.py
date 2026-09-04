"""Turn-taking: deciding when someone has finished speaking.

Turn-taking is what separates charming from infuriating, and no amount of
personality prompting fixes it. A robot that cuts you off mid-sentence reads as
rude however warmly it is written, and one that waits three seconds reads as
slow however clever the answer is.

`patience_ms` is therefore a **persona trait, not engine tuning**. It lives on
the soul, so Neuma — reflective, sparing with words — waits longer than Hugr,
who is opinionated and interrupts. That is the whole point of putting it there:
the same engine produces two different conversational temperaments from two
data files.

**On the default of 900 ms.** The numbers it trades against are measured rather
than guessed, and they are not ours: they come from the TurnBench corpus
analysis (Jiang et al., *TurnBench: A Multi-Domain Benchmark for Turn-Taking
Dynamics in Spoken Dialogue*, arXiv:2608.25218, 2026 — see `CITATIONS.md`).

    floor transfer offset, median        -151 ms   (before the turn ends)
    inter-speaker gap, median             380 ms
    pause *within* one speaker's turn      510 ms

The third number is why a silence threshold cannot be made good. A pause inside
somebody's own turn and a gap between speakers overlap heavily, so no threshold
separates "still thinking" from "finished". 900 ms sits above both: it will
rarely cut anybody off, and it will always feel slower than a person, who
starts speaking before you have finished. That is a deliberate trade, not a
tuned value, and it is the right way round while the only evidence available is
energy.

**Scope.** `extend_on_incomplete`, the trailing-clause heuristic, is not here.
The specification is precise that it triggers when *the transcript* looks
unfinished, and there is no transcript until speech recognition exists. Trying
to guess incompleteness from audio alone would be a different and much worse
heuristic wearing the same name.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from emet_sdk.types import AudioFormat

from emet_engine.vad import EnergyVad, VadTuning

__all__ = ["Endpointer", "Utterance", "EndReason", "DEFAULT_PATIENCE_MS"]

#: `interaction.patience_ms` when a soul does not say. Matches the documented
#: default so that a bundle written against the spec behaves as it reads.
DEFAULT_PATIENCE_MS = 900


class EndReason(StrEnum):
    """Why the turn ended. The engine above should not treat these alike: a
    person who trailed off is not a person who said nothing."""

    SILENCE = "silence"          # they stopped talking. The normal case.
    NO_SPEECH = "no_speech"      # woken, then nothing. A false wake, probably.
    TOO_LONG = "too_long"        # still going at the cap. Take what we have.
    SOURCE_ENDED = "source_ended"  # the file ran out mid-turn.


@dataclass(frozen=True, slots=True)
class Utterance:
    """One turn of speech, captured between a wake and an endpoint."""

    audio: bytes
    duration_ms: float
    reason: EndReason

    @property
    def had_speech(self) -> bool:
        """Whether anything was actually captured.

        Read from the audio rather than inferred from the reason: a turn can
        end because the source ran out having caught plenty, or having caught
        nothing, and those are not the same event.
        """
        return bool(self.audio)


class Endpointer:
    """Feed frames after a wake; get an `Utterance` when the turn is over.

    Returns None while the turn is still in progress, so a caller can pump it
    from the same loop that reads audio without needing a second thread.
    """

    def __init__(
        self,
        fmt: AudioFormat,
        *,
        patience_ms: int = DEFAULT_PATIENCE_MS,
        lead_in_ms: int = 2500,
        max_utterance_ms: int = 15000,
        preroll_frames: int = 4,
        vad: EnergyVad | None = None,
        tuning: VadTuning | None = None,
    ) -> None:
        self.format = fmt
        self.patience_ms = patience_ms
        self.lead_in_ms = lead_in_ms
        self.max_utterance_ms = max_utterance_ms
        # Detection lags onset by `onset_frames`, so without a pre-roll the
        # first consonant of the reply is clipped and the transcript starts
        # mid-word. Cheap to keep, expensive to lose.
        self._preroll: deque[bytes] = deque(maxlen=max(0, preroll_frames))
        self.vad = vad or EnergyVad(fmt, tuning)

        self._frames: list[bytes] = []
        self._started = False
        self._waited_ms = 0.0
        self._speech_ms = 0.0

    @property
    def frame_ms(self) -> float:
        return self.format.frame_ms

    @property
    def started(self) -> bool:
        """Whether speech has begun. Drives `signal.listening` later on."""
        return self._started

    def feed(self, frame: bytes) -> Utterance | None:
        speaking = self.vad.feed(frame)

        if not self._started:
            self._preroll.append(frame)
            if speaking:
                self._started = True
                # The pre-roll already contains this frame.
                self._frames.extend(self._preroll)
                self._speech_ms = len(self._frames) * self.frame_ms
                return None
            self._waited_ms += self.frame_ms
            if self._waited_ms >= self.lead_in_ms:
                return self._finish(EndReason.NO_SPEECH)
            return None

        self._frames.append(frame)
        self._speech_ms += self.frame_ms

        if self._speech_ms >= self.max_utterance_ms:
            return self._finish(EndReason.TOO_LONG)

        # Measured from the last frame that was actually loud, not from when
        # the debounced flag flipped: the VAD's hangover would otherwise be
        # charged to the persona's patience, making every soul slower than the
        # number in its own bundle.
        if not speaking and self.vad.quiet_ms >= self.patience_ms:
            return self._finish(EndReason.SILENCE)

        return None

    def close(self) -> Utterance:
        """The source ended mid-turn. Return whatever was captured.

        Always `SOURCE_ENDED`, even with nothing captured. `NO_SPEECH` means
        something specific — the robot waited the full lead-in and nobody
        spoke, which is evidence of a false wake. A recording that stopped
        early is not evidence of anything, and labelling it the same way would
        make a replay look like a detector fault.
        """
        return self._finish(EndReason.SOURCE_ENDED)

    def _finish(self, reason: EndReason) -> Utterance:
        audio = b"".join(self._frames)
        utterance = Utterance(
            audio=audio,
            duration_ms=len(self._frames) * self.frame_ms,
            reason=reason,
        )
        self._frames = []
        self._started = False
        self._waited_ms = 0.0
        self._speech_ms = 0.0
        self._preroll.clear()
        self.vad.reset()
        return utterance
