"""The shipped wake engine hears its name in a generated clip.

`test_pocketsphinx_wake.py` records a gap: nothing in the repository showed
the engine firing on the phrase spoken, because the audio it was checked
against by hand had no provenance worth committing. `clips/` closes it
(decision D8 of 2026-10-27) with a clip of known origin: the public-domain
reference voice, LJ Speech's `en_US-ljspeech-medium`, saying "Hey emet."
half a metre from the microphone in a simulated small room, after 5.5 s of
quiet pink noise so the decoder's mean settles first (about 3 s,
RELEASE_PROCESS section 10's 0.5 row). `clips/hey-emet-ljspeech.json` beside
it holds what made it: the voice and its hashes, Piper's version and
arguments, the room, the noise and its seed, and the command, which is

    python tools/bank/generate.py --clip emet-hal/tests/clips/hey-emet-ljspeech.wav --voices VOICES --piper PIPER

Generated speech in a simulated room shows the engine works end to end. It
sets no default and gives no recall for a real room. The clip is clean: on
the laptop (pocketsphinx 5.1.1, 2026-11-26) it woke at every threshold from
1e-30 to 1e10 and at none from 1e20 up, so it says nothing of a margin.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from pathlib import Path

import pytest

pytest.importorskip("pocketsphinx", reason="emet-hal[wake] is not installed")

from emet_hal.pocketsphinx_wake import DEFAULT_THRESHOLD, PocketSphinxWake  # noqa: E402

CLIPS = Path(__file__).resolve().parent / "clips"
CLIP = CLIPS / "hey-emet-ljspeech.wav"
RECORD = json.loads((CLIPS / "hey-emet-ljspeech.json").read_text(encoding="utf-8"))


def frames_of(path: Path, size: int) -> list[bytes]:
    with wave.open(str(path), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        pcm = w.readframes(w.getnframes())
    step = 2 * size
    return [pcm[i:i + step] for i in range(0, len(pcm) - step + 1, step)]


def wakes(threshold: float) -> tuple[list[int], int]:
    """The frame numbers the engine woke on, fed the clip as the engine
    feeds it, and the frame's size in samples that the plugin stated."""
    plugin = PocketSphinxWake({"engine": "pocketsphinx", "params": {"threshold": threshold}}, "hey emet")

    async def listen() -> tuple[list[int], int]:
        await plugin.start()
        assert plugin.describe().healthy, plugin.health().detail
        size = plugin.describe().frame_samples
        heard = []
        for n, frame in enumerate(frames_of(CLIP, size)):
            event = await plugin.process(frame)
            if event is not None:
                assert event.phrase == "hey emet"
                heard.append(n)
        await plugin.shutdown()
        return heard, size

    return asyncio.run(listen())


def test_the_clip_is_the_one_its_record_describes():
    data = CLIP.read_bytes()
    assert hashlib.sha256(data).hexdigest() == RECORD["sha256"]
    assert len(data) == RECORD["bytes"] < 1_000_000, "the clip stays under 1 MB"
    assert RECORD["voice"]["name"] == "en_US-ljspeech-medium" and RECORD["voice"]["licence"] == "public domain"
    assert RECORD["phrase_at_sample"] >= 5 * 16000, "at least 5 s of quiet before the phrase"


def test_the_engine_hears_its_name_in_the_clip_at_the_shipped_threshold():
    heard, size = wakes(DEFAULT_THRESHOLD)
    assert heard, f"no wake at the shipped threshold, {DEFAULT_THRESHOLD:g}"
    first = heard[0] * size
    assert first >= RECORD["phrase_at_sample"], "it woke on the phrase, never on the quiet before it"
    assert len(heard) == 1, f"one phrase, one wake: it woke on frames {heard}"


def test_the_threshold_reaches_the_engine():
    """A threshold far past any score leaves the same clip unheard, so the
    wake above comes from the threshold given and not from one the plugin
    keeps of its own."""
    assert wakes(1e30)[0] == []
