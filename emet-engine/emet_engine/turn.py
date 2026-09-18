"""Turn-taking: deciding when someone has finished speaking.

Turn-taking is what separates charming from infuriating, and no amount of
personality prompting fixes it. A robot that cuts you off mid-sentence reads as
rude however warmly it is written, and one that waits three seconds reads as
slow however clever the answer is.

`patience_ms` is therefore a **persona trait, not engine tuning**. It lives on
the soul, so Neuma, reflective and sparing with words, waits longer than Hugr,
who is opinionated and interrupts. That is the whole point of putting it there:
the same engine produces two different conversational temperaments from two
data files.

**On the default of 900 ms.** The numbers it trades against are measured rather
than guessed, and they are not ours: they come from the TurnBench corpus
analysis (Jiang et al., *TurnBench: A Multi-Domain Benchmark for Turn-Taking
Dynamics in Spoken Dialogue*, arXiv:2608.25218, 2026; see `CITATIONS.md`).

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

**The trailing clause.** `extend_on_incomplete`, the fourth decision in
`DESIGN.md` section 13, waited for a transcript to exist. It does now, so the
endpointer takes a hook: at the moment silence has run out the patience, it
asks whether the words so far look unfinished, and if they do it waits one
more patience window, once per turn. `looks_incomplete()` is the judgement,
made on the transcript rather than the audio: a last word that cannot end a
sentence ("where are my", "and then"), a trailing comma, or a clause with no
end mark from a provider that has been supplying them. A wrong guess costs
one more window of silence; a missed one cuts a person off mid-clause, which
is the rudeness this exists to avoid.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from emet_sdk.types import AudioFormat, Transcript

from emet_engine.vad import EnergyVad, VadTuning

__all__ = ["Endpointer", "Utterance", "EndReason", "DEFAULT_PATIENCE_MS", "DANGLING_WORDS", "looks_incomplete"]

#: Words a sentence does not end on. A transcript whose last word is one of
#: these was cut mid-clause, whatever the punctuation says: articles,
#: conjunctions, prepositions, the determiners that lead an object, the
#: auxiliaries that lead a verb, and the sounds a person makes while finding
#: the next word.
DANGLING_WORDS: frozenset[str] = frozenset(
    """
    a an the
    and or but so nor yet because if when while although though unless until
    whether that which who whom whose where how why what
    to of in on at by for with from into onto about over under after before
    between through without within like than as per
    my your his her its our their this these those some any every each no
    is are was were be been being am do does did can could will would shall
    should may might must have has had
    um uh er
    """.split()
)

_END_MARK = re.compile(r"[.?!](?=\s|$)")


def looks_incomplete(text: str) -> bool:
    """Whether a transcript reads as cut off mid-clause.

    Three signs, checked in order: an end mark on the last word means it
    is finished; a comma, colon or semicolon there, or a last word from
    `DANGLING_WORDS`, means it is not; and a last clause with no end mark
    from a provider that has been punctuating the earlier ones means it is
    not either. Nothing at all, or one clause with no punctuation anywhere,
    is taken as finished: a provider that never punctuates would otherwise
    make every turn one window longer.
    """
    words = text.split()
    if not words:
        return False
    last = words[-1].rstrip("\"')]")
    if last.endswith((".", "?", "!")):
        return False
    if last.endswith((",", ";", ":")):
        return True
    bare = last.strip("\"'([").lower()
    if bare in DANGLING_WORDS:
        return True
    return bool(_END_MARK.search(" ".join(words[:-1])))

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
    #: What speech recognition made of it, when a transcriber was listening.
    #: None means nobody asked, which is different from an empty final: that
    #: means a provider listened and heard no words it could make out.
    transcript: Transcript | None = None
    #: Whether the silence window was extended once because the words so
    #: far looked unfinished.
    extended: bool = False

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
        max_utterance_ms: int = 30000,
        preroll_frames: int = 4,
        vad: EnergyVad | None = None,
        tuning: VadTuning | None = None,
        extend_if: Callable[[], bool] | None = None,
    ) -> None:
        self.format = fmt
        self.patience_ms = patience_ms
        self.lead_in_ms = lead_in_ms
        self.max_utterance_ms = max_utterance_ms
        #: Asked once per turn, at the moment silence has run out the
        #: patience: True means wait one more window. The session answers it
        #: from the transcript so far. None means never extend.
        self.extend_if = extend_if
        # Detection lags onset by `onset_frames`, so without a pre-roll the
        # first consonant of the reply is clipped and the transcript starts
        # mid-word. Cheap to keep, expensive to lose.
        self._preroll: deque[bytes] = deque(maxlen=max(0, preroll_frames))
        self.vad = vad or EnergyVad(fmt, tuning)

        self._frames: list[bytes] = []
        self._started = False
        self._waited_ms = 0.0
        self._speech_ms = 0.0
        #: Whether this turn's one extension has been granted, and whether
        #: the silence it was granted in is still running.
        self._extended = False
        self._extending = False

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
        if speaking:
            # Speech resumed inside an extension: the extension did its job,
            # and the next silence is judged by plain patience again.
            self._extending = False
            return None
        needed = self.patience_ms * 2 if self._extending else self.patience_ms
        if self.vad.quiet_ms >= needed:
            if not self._extended and self.extend_if is not None and self.extend_if():
                self._extended = True
                self._extending = True
                return None
            return self._finish(EndReason.SILENCE)

        return None

    def close(self) -> Utterance:
        """The source ended mid-turn. Return whatever was captured.

        Always `SOURCE_ENDED`, even with nothing captured. `NO_SPEECH` means
        something specific: the robot waited the full lead-in and nobody
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
            extended=self._extended,
        )
        self._frames = []
        self._started = False
        self._waited_ms = 0.0
        self._speech_ms = 0.0
        self._extended = False
        self._extending = False
        self._preroll.clear()
        self.vad.reset()
        return utterance
