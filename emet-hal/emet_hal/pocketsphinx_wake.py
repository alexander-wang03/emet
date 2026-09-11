"""The shipped wake word engine: phonetic keyword spotting.

Emet wakes on a phrase, not on a name somebody trained a model for. That is a
deliberate reversal of how most wake word detection works, and it is what lets
`identity.wake_word` be free text.

A trained detector (openWakeWord, and Porcupine before its access keys were
disabled on 30 June 2026) learns one phrase from thousands of examples. It is
more accurate, and it means a soul can only answer to a name somebody has
already trained. Naming your robot Barnaby would make it deaf.

A phonetic spotter works the other way round. It knows how English *sounds*,
and a phrase is a sequence of phonemes to watch for. "hey barnaby" needs no
training at all, because `barnaby` is already in the pronunciation dictionary.
A name outside the dictionary, and `emet` is one, needs one line of
phonemes, which is what `SHIPPED_LEXICON` below is.

The tradeoff is real and worth stating plainly: this is less accurate in noise
than a trained model. It is the floor, not the ceiling. An openWakeWord plugin
is the upgrade for people who want that accuracy and will train for it, and
because wake is an open plugin category, installing one is a one-line change.

**On warm-up.** The decoder is least sensitive in the seconds after it starts,
before its cepstral mean has adapted. That is a real property, not a bug, and
it is why `DEFAULT_THRESHOLD` is chosen with the cold end in view: a value
that looks stricter on a warm recording costs wakes at boot.

**On confidence.** Keyword spotting reports no calibrated score, so every
`WakeEvent` from this engine carries `confidence=1.0`. That is honesty about
the absence of a number rather than a claim of certainty. Tune
`params.threshold` instead: higher is stricter, lower fires more easily.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from emet_sdk.plugin import WakePlugin
from emet_sdk.types import Health, WakeDescriptor, WakeEvent

__all__ = ["PocketSphinxWake", "SHIPPED_LEXICON", "DEFAULT_THRESHOLD"]

log = logging.getLogger("emet_hal.pocketsphinx")

#: Audio this engine expects. 16 kHz mono is what the acoustic model was
#: trained on; feeding it anything else degrades detection silently rather
#: than failing, which is why the descriptor states it and the engine obeys.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280

#: Keyword spotting threshold. Higher is stricter: fewer false wakes, more
#: missed ones. Lower fires more easily. pocketsphinx reports a keyphrase
#: when its score beats the background by at least log(threshold), so a
#: smaller number is an easier bar to clear (kws_search.c).
#:
#: This value was measured rather than guessed, and the measurement had a
#: wrinkle worth recording. pocketsphinx adapts its cepstral mean as audio
#: flows, so a decoder that has been listening for a while is more sensitive
#: than one that just booted. The two ends fail in opposite directions:
#:
#:     threshold   cold (at boot)      warm (listening a while)
#:     1e-20       misses "hey neuma"  clean
#:     1e-25       clean               clean
#:     1e-30       clean               false-fires
#:
#: 1e-25 was the only value clean at both ends of that table. It was measured
#: over four phrases against six synthesised clips, and a real room then
#: overturned it. A ten-minute recording on the reference body (Raspberry Pi 5,
#: USB microphone, one voice, one room, 2026-09-10) holds 49 wake phrases and
#: 9 decoys: six near-miss names ("hey emma", "hey Emily", "hey, met any"),
#: three plain sentences with no name in them ("a mess of cables", "meant to").
#: Replayed with the decoder warm:
#:
#:     threshold   wakes heard    decoys that fired
#:     1e-25       49 of 49       8 of 9, including all three plain sentences
#:     1e-22       48 of 49       3 of 9, all near-miss names
#:     1e-20       48 of 49       3 of 9, the same three
#:     1e-15       44 of 49       1 of 9
#:
#: 1e-22 is the default: it stops a robot waking on ordinary sentences at the
#: cost of one wake in forty-nine, and it sits nearer the value the cold table
#: cleared than 1e-20 does. Near-miss names fire at every threshold that keeps
#: recall; that is the floor of a phonetic spotter with a two-syllable name,
#: and the reason a trained model is the upgrade rather than a tweak here.
DEFAULT_THRESHOLD = 1e-22

#: Pronunciations for names that are not English words, in ARPAbet, which is
#: what the bundled dictionary uses. Multiple entries per name are alternate
#: pronunciations: `hugr` is Old Norse and nobody agrees how to say it, so the
#: engine listens for more than one.
#:
#: Every entry here was checked against synthesised speech: each fires on its
#: own phrase and on none of the others, including a conversational control
#: clip. Synthetic speech is a weak proxy for a room, so treat these as a
#: working default a builder may need to extend through `params.lexicon`.
SHIPPED_LEXICON: Mapping[str, tuple[str, ...]] = {
    "emet": ("EH M EH T", "EY M EH T", "IH M EH T"),
    "hugr": ("HH UH G ER", "HH UW G ER"),
    "neuma": ("N UW M AH", "N Y UW M AH"),
}


class PocketSphinxWake(WakePlugin):
    """Wake detection through pocketsphinx keyword spotting.

    Params, all optional:

        threshold   float, default DEFAULT_THRESHOLD. Higher is stricter.
        lexicon     word -> pronunciation, or word -> [pronunciations].
                    Merged over SHIPPED_LEXICON, so a body can override a
                    shipped name or teach the engine an entirely new one.
        model       path to an acoustic model directory. Defaults to the one
                    bundled with pocketsphinx.
        verbose     bool. Let the decoder log; off by default, it is chatty.
    """

    engine = "pocketsphinx"

    def __init__(self, config: Mapping[str, Any], phrase: str) -> None:
        super().__init__(config, phrase)
        self._decoder: Any = None
        self._fault: str | None = None
        self._unpronounceable: tuple[str, ...] = ()
        self._listening = False
        self._fired = False

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Build the decoder and teach it the phrase.

        Does not raise, on a missing dependency or an unsayable phrase alike.
        Both are reported through `describe()` as `healthy=False`, matching the
        other plugins: the boot check then says precisely what is wrong instead
        of unwinding a stack trace through a robot that was otherwise fine.
        """
        try:
            from pocketsphinx import Config, Decoder
        except ImportError:
            self._fail(
                "pocketsphinx is not installed. It is an optional dependency: "
                "install `emet-hal[wake]`, or point `audio.wake.engine` at an "
                "engine you do have."
            )
            return

        settings: dict[str, Any] = {
            "loglevel": "INFO" if self.params.get("verbose") else "FATAL",
            "kws_threshold": float(self.params.get("threshold", DEFAULT_THRESHOLD)),
        }
        if self.params.get("model"):
            settings["hmm"] = str(self.params["model"])

        try:
            self._decoder = Decoder(Config(**settings))
        except Exception as exc:  # noqa: BLE001 - the decoder raises broadly
            self._fail(f"could not start the pocketsphinx decoder: {exc}")
            return

        missing = self._teach(self.phrase)
        if missing:
            self._unpronounceable = missing
            words = ", ".join(repr(w) for w in missing)
            self._fail(
                f"no pronunciation for {words} in {self.phrase!r}. The dictionary "
                f"does not know the word and no lexicon entry supplies it. Add one "
                f"in ARPAbet under `audio.wake.params.lexicon`, or choose a phrase "
                f"of ordinary English words, which need nothing."
            )
            return

        self._decoder.add_keyphrase("emet_wake", self.phrase)
        self._decoder.activate_search("emet_wake")
        self._decoder.start_utt()
        self._listening = True

    def _fail(self, reason: str) -> None:
        self._fault = reason
        log.error("wake: %s", reason)

    def _teach(self, phrase: str) -> tuple[str, ...]:
        """Add pronunciations for any word the dictionary lacks.

        Returns the words it could not resolve. Alternates use pocketsphinx's
        `word(2)` convention, so a name with two plausible pronunciations is
        heard either way.
        """
        lexicon: dict[str, tuple[str, ...]] = dict(SHIPPED_LEXICON)
        for word, pron in (self.params.get("lexicon") or {}).items():
            lexicon[str(word).lower()] = (pron,) if isinstance(pron, str) else tuple(pron)

        missing: list[str] = []
        for word in phrase.lower().split():
            if self._decoder.lookup_word(word) is not None:
                continue
            prons = lexicon.get(word)
            if not prons:
                missing.append(word)
                continue
            for i, pron in enumerate(prons):
                self._decoder.add_word(word if i == 0 else f"{word}({i + 1})", pron, True)
        return tuple(missing)

    async def shutdown(self) -> None:
        if self._listening and self._decoder is not None:
            self._decoder.end_utt()
        self._listening = False

    # ------------------------------------------------------------ reporting

    def describe(self) -> WakeDescriptor:
        return WakeDescriptor(
            engine=self.engine,
            # Only what this instance actually loaded. `supports_custom_phrases`
            # says the engine *could* take any phrase; it is not a claim to be
            # listening for one.
            phrases=frozenset({self.phrase}) if self._listening else frozenset(),
            supports_custom_phrases=True,
            sample_rate=SAMPLE_RATE,
            frame_samples=FRAME_SAMPLES,
            healthy=self._listening,
        )

    def health(self) -> Health:
        if self._fault:
            fault = "unpronounceable_phrase" if self._unpronounceable else "start_failed"
            return Health(ok=False, detail=self._fault, faults=(fault,))
        return Health()

    # ------------------------------------------------------------ detection

    async def process(self, frame: bytes) -> WakeEvent | None:
        if not self._listening:
            return None
        self._decoder.process_raw(frame, False, False)
        if self._decoder.hyp() is None:
            return None
        if self._fired:
            # The hypothesis persists until the utterance is reset, so without
            # this a caller that forgets `reset()` gets one event per frame for
            # the rest of the conversation.
            return None
        self._fired = True
        return WakeEvent(phrase=self.phrase, confidence=1.0)

    async def reset(self) -> None:
        """Start a fresh utterance, discarding the hypothesis that just fired."""
        self._fired = False
        if self._listening:
            self._decoder.end_utt()
            self._decoder.start_utt()
