"""Voice activity detection: is anybody talking right now.

Deliberately not a plugin category. There is effectively one good answer in the
world (Silero), it is MIT so it carries none of the rug-pull risk that made
wake a category, and its output feeds turn-taking (`patience_ms`, the trailing
clause), which is personality rather than hardware. A seam there would decouple
nothing. Adding a category later is a minor version bump and removing one is a
major bump, so under uncertainty this stays a component.

**Why arithmetic rather than Silero, for now.** Silero was the intended choice
and did not survive being checked:

* `silero-vad` on PyPI hard-depends on `torch` and `torchaudio`. Putting
  PyTorch on a Raspberry Pi to decide whether someone is speaking is not a
  trade worth making.
* `silero-vad-lite`, the dependency-free wrapper, publishes no aarch64 Linux
  wheel (x86_64 Linux, macOS and Windows only) and declares no licence.
* Running the ONNX model directly means `onnxruntime` (20.8 MB on ARM) plus a
  vendored model file.

So this ships with no new dependencies at all, and is honest about being worse:
it is a floor, not a ceiling. It hears energy, not speech, and a fan or a
washing machine will fool it in a way a neural model would not. When Silero is
worth its weight, it replaces the guts of this file and nothing above changes,
which is exactly what "not a swap point" was supposed to buy.

**The design is the standard one** and its parts are all load-bearing:

* an adaptive noise floor, because a quiet study and a kitchen differ by more
  than any fixed threshold can span;
* hysteresis, so one loud frame is not speech and one quiet frame is not
  silence;
* asymmetric adaptation, because the floor should rise slowly and fall fast:
  a fridge switching on must not be learned as speech, and a fridge switching
  off must not deafen the robot for a minute.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass

from emet_sdk.types import AudioFormat

__all__ = ["EnergyVad", "VadTuning", "rms"]


def rms(frame: bytes) -> float:
    """Root mean square of one frame of mono int16."""
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


@dataclass(frozen=True, slots=True)
class VadTuning:
    """Knobs, with defaults that work in a normal room.

    These are engine tuning rather than persona: how *loud* speech has to be is
    a property of the room and the microphone, not of the character. How *long*
    the robot waits afterwards is the persona's business, and lives in
    `patience_ms` on the soul instead.
    """

    #: Speech must exceed the noise floor by this factor.
    ratio: float = 3.0
    #: ...and this absolute level, so a silent room with a floor near zero does
    #: not treat its own dither as conversation.
    absolute_floor: float = 180.0
    #: Consecutive loud frames before speech is declared. Rejects a door.
    onset_frames: int = 2
    #: Consecutive quiet frames before speech is over. At 80 ms frames this is
    #: about a third of a second, which is roughly the gap inside a sentence;
    #: shorter and the robot interrupts you mid-thought.
    hangover_frames: int = 4
    #: How fast the floor rises toward a louder room, per frame.
    adapt_up: float = 0.02
    #: How fast it falls toward a quieter one. Deliberately faster: a machine
    #: switching off should not leave the robot deaf.
    adapt_down: float = 0.25


class EnergyVad:
    """Per-frame speech decision with hysteresis and an adapting noise floor.

    Stateful and cheap: one pass over each frame, no allocation beyond the
    sample view. Feed it every frame in order.
    """

    def __init__(self, fmt: AudioFormat, tuning: VadTuning | None = None) -> None:
        self.format = fmt
        self.tuning = tuning or VadTuning()
        self.noise_floor: float = self.tuning.absolute_floor
        self._speaking = False
        self._loud_run = 0
        self._quiet_run = 0
        #: Frames seen. Useful to callers deciding whether the floor has had
        #: time to settle.
        self.frames = 0

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def quiet_frames(self) -> int:
        """Consecutive frames below the threshold, right now.

        Exposed because the endpointer measures trailing silence from the last
        genuinely loud frame, not from when the debounced flag flipped. The
        difference is `hangover_frames`, a third of a second that would
        otherwise be silently charged to the persona's `patience_ms`, making
        every soul slower than the number written in its own bundle.
        """
        return self._quiet_run

    @property
    def quiet_ms(self) -> float:
        return self._quiet_run * self.format.frame_ms

    @property
    def threshold(self) -> float:
        t = self.tuning
        return max(self.noise_floor * t.ratio, t.absolute_floor)

    def feed(self, frame: bytes) -> bool:
        """Consume one frame; return whether speech is in progress.

        The return value is the *debounced* state, not whether this particular
        frame was loud. A caller asking "is someone talking" wants the former.
        """
        t = self.tuning
        self.frames += 1
        level = rms(frame)

        loud = level > self.threshold

        # Adapt only while quiet. Learning the floor during speech would
        # teach the detector that the person talking is the room.
        if not loud:
            rate = t.adapt_up if level > self.noise_floor else t.adapt_down
            self.noise_floor += (level - self.noise_floor) * rate

        if loud:
            self._loud_run += 1
            self._quiet_run = 0
        else:
            self._quiet_run += 1
            self._loud_run = 0

        if not self._speaking and self._loud_run >= t.onset_frames:
            self._speaking = True
        elif self._speaking and self._quiet_run >= t.hangover_frames:
            self._speaking = False

        return self._speaking

    def reset(self) -> None:
        """Forget the utterance, keep what has been learned about the room."""
        self._speaking = False
        self._loud_run = 0
        self._quiet_run = 0
