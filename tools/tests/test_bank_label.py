"""Tests for tools/bank/label.py, the bank's labelling pass.

The rules that need nothing outside the standard library run everywhere:
words, the comparison with the prompt, the level rules, the label's shape.
The alignment needs pocketsphinx and the transcript faster-whisper, and
those tests skip without them, as the live-vendor tests skip without keys;
both are in the tools venv of tools/bank/requirements.txt. No test reads
speech: the audio is tones and silence written here.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import wave
from array import array
from pathlib import Path

import pytest

BANK_TOOLS = Path(__file__).resolve().parents[1] / "bank"
sys.path.insert(0, str(BANK_TOOLS))

import label  # noqa: E402

RATE = 16000


def samples(*parts: tuple[str, float, float]) -> array:
    """Concatenated ("tone"|"noise"|"quiet", seconds, level) parts. Quiet is
    a faint hiss so the floor is finite, as a room's is."""
    out = array("h")
    k = 0
    for what, seconds, level in parts:
        for i in range(int(round(seconds * RATE))):
            if what == "tone":
                v = level * math.sin(2 * math.pi * 300 * k / RATE)
            else:
                v = level * (((k * 7919) % 200) / 100.0 - 1.0)
            out.append(max(-32768, min(32767, int(round(32767 * v)))))
            k += 1
    return out


QUIET = ("quiet", 0.0, 0.0005)


def q(seconds):
    return ("quiet", seconds, 0.0005)


def tone(seconds, level=0.3):
    return ("tone", seconds, level)


def take(tid, kind, text, start, end, question=None, exclude=None):
    return {"id": tid, "kind": kind, "text": text, "question": question, "first_sound": None,
            "slot": {"start": start, "end": end}, "expect_wake": kind not in ("decoy", "room_tone"),
            "retake_of": None, "exclude": exclude}


# ------------------------------------------------------------------ words

@pytest.mark.parametrize("text, words", [
    ("Hey Emmett, what's 7 times 8?", ["hey", "emmett", "what's", "seven", "times", "eight"]),
    ("Wake me at 6:30.", ["wake", "me", "at", "six", "thirty"]),
    ("until the 21st", ["until", "the", "twenty", "first"]),
    ("is it twenty-two degrees", ["is", "it", "twenty", "two", "degrees"]),
    ("a 100 minus 1", ["a", "one", "hundred", "minus", "one"]),
    ("hey emet, I want to [hold 1.5 s] go to the shops", ["hey", "emet", "i", "want", "to", "go", "to", "the", "shops"]),
    ("hey emet ... what time is it", ["hey", "emet", "what", "time", "is", "it"]),
    ("OK.", ["okay"]),
    ("Don’t they?", ["don't", "they"]),
])
def test_words_are_compared_as_said(text, words):
    assert label.words_of(text) == words


def test_whisper_may_spell_the_name_its_own_way_before_the_question():
    assert label.compare(["what", "time", "is", "it"], label.words_of("Hey Emmett, what time is it?"), True) is None
    assert label.compare(["what", "time", "is", "it"], label.words_of("Emmett, what time is it?"), True) is None


def test_contractions_and_the_name_read_alike():
    assert label.compare(["i", "am", "going", "to", "bed"], label.words_of("Hey Emmett, I'm going to bed."), True) is None
    assert label.compare(label.words_of("I cannot find them"), label.words_of("I can't find them"), False) is None
    assert label.compare(label.words_of("three o'clock"), label.words_of("three o clock"), False) is None
    assert label.compare(label.words_of("hey Emmett, how was school"), label.words_of("Hey emit, how was school?"), False) is None
    assert label.compare(label.words_of("hey Emma"), label.words_of("Hey Emmett."), False) is not None
    assert label.words_of("what's two plus two") == ["what's", "two", "plus", "two"]  # the aligner keeps the prompt


def test_different_words_are_flagged():
    why = label.compare(["what's", "two", "plus", "two"], label.words_of("Hey Emmett, it was two plus two."), True)
    assert why and "differ" in why


def test_words_beyond_the_prompt_are_flagged():
    why = label.compare(["make", "it", "louder"], label.words_of("Hey Emmett, make it louder. Hey"), True)
    assert why and "beyond the prompt" in why
    why = label.compare(["hey", "emma"], label.words_of("Well, hey Emma"), False)
    assert why and "beyond the prompt" in why
    assert label.compare(["what"], label.words_of("a b c d what"), True) is not None  # four words before


def test_a_take_with_the_name_is_compared_on_its_question():
    t = take("r/01", "one_breath", "hey emet, what time is it", 0, 1, question="what time is it")
    assert label.expected_words(t) == (["what", "time", "is", "it"], True)
    d = take("r/02", "decoy", "hey Emmett, how was school", 0, 1)
    assert label.expected_words(d) == (["hey", "emmett", "how", "was", "school"], False)


# ------------------------------------------------------------------ level

def test_the_onset_is_the_first_of_five_loud_windows():
    x = samples(q(1.0), tone(0.5), q(1.0))
    lv = label.Level(x)
    onset, end, to_end = lv.speech(0, len(x))
    assert onset == 16000 and end == 24000 and not to_end
    short = samples(q(1.0), tone(0.08), q(1.0))     # four windows: no onset
    assert label.Level(short).speech(0, len(short))[0] is None


def test_speech_that_reaches_the_slot_end_is_seen():
    x = samples(q(1.0), tone(1.0))
    lv = label.Level(x)
    assert lv.speech(0, len(x))[2] is True


def test_the_first_word_after_a_pause_and_not_after_a_closure():
    x = samples(q(1.0), tone(0.4), q(0.06), tone(0.4), q(0.3), tone(0.4), q(0.5))
    lv = label.Level(x)
    name_end = 16000 + int(0.4 * RATE)
    # the 60 ms gap is a stop's closure; the 300 ms one is a pause
    assert lv.onset_after_quiet(name_end, len(x)) == 16000 + int(1.16 * RATE)
    assert lv.onset_after_quiet(name_end, 16000 + int(1.0 * RATE)) is None


def test_clipping_and_a_raised_floor_are_counted():
    x = samples(q(2.0), tone(0.3, level=1.2), q(0.5), ("noise", 2.0, 0.02), tone(0.3), q(0.2))
    lv = label.Level(x)
    peak, clipped = lv.peak(0, 40000)
    assert peak == 0.0 and clipped > 0
    noisy = (int(2.8 * RATE), int(4.8 * RATE))
    assert lv.slot_floor(*noisy) - lv.floor >= label.NOISE_DB


# ------------------------------------------------------------------ the lexicon

def test_the_lexicon_is_the_shipped_one():
    emet_hal = pytest.importorskip("emet_hal.pocketsphinx_wake")
    assert label.shipped_lexicon() == dict(emet_hal.SHIPPED_LEXICON)


# ------------------------------------------------------------------ one recording, with stand-ins

def fake_aligner(pcm, words, mean, lexicon):
    """Aligns "hey emet <rest>" at 10 ms frames: hey 10-30, emet 30-60, then a
    word every 30 frames; fails on any text without "emet"."""
    if "emet" not in words:
        return {"unknown": [], "seg": None, "words": [], "phones": [], "failed": "word pass: Failed to stop utterance processing"}
    ws, ph = [("<sil>", 0, 10), ("hey", 10, 30), ("emet", 30, 60)], [("HH", 10, 20), ("EY", 20, 30),
                                                                   ("EH", 30, 40), ("M", 40, 45), ("EH", 45, 55), ("T", 55, 60)]
    at = 60
    for w in words[2:]:
        ws.append((w, at, at + 30))
        ph.append((w[0].upper(), at, at + 10))
        at += 30
    return {"unknown": [], "seg": [(w, s, e) for w, s, e in ws], "words": ws, "phones": ph, "failed": None}


def manifest_doc(takes):
    return {"format": "emet-bank/1", "id": "r", "sha256": "0" * 64, "takes": takes}


def test_a_recording_is_labelled_by_take_and_each_label_names_its_maker():
    x = samples(q(1.0), tone(0.5), q(0.5), q(1.0), tone(0.5), q(0.5), q(3.0))
    doc = manifest_doc([
        take("r/01", "one_breath", "hey emet, what time is it", 0, 32000, question="what time is it"),
        take("r/02", "one_breath", "hey emet, open the window", 32000, 64000, question="open the window"),
        take("r/03", "room_tone", "nobody speaks", 64000, 96000),
    ])
    heard = [("Hey", 16000, 18000, 0.9), ("Emmett,", 18000, 20000, 0.9), ("what", 20000, 21000, 0.9),
             ("time", 21000, 22000, 0.9), ("is", 22000, 23000, 0.9), ("it?", 23000, 24000, 0.9),
             # take 02's first word, whose start whisper puts in take 01's slot
             ("Hey", 25000, 48500, 0.2), ("Emmett,", 48500, 50000, 0.9), ("open", 50000, 51000, 0.9),
             ("the", 51000, 52000, 0.9), ("door.", 52000, 54000, 0.9),
             ("Hello?", 70000, 72000, 0.5), ("um", 99000, 99500, 0.5)]
    out = label.label_recording(doc, x, transcribe=lambda _x: heard, whisper_by="whisper stand-in",
                                aligner=fake_aligner, lexicon={"emet": ("EH M EH T",)}, made="2026-10-27")
    assert out["format"] == "emet-labels/1" and out["recording"] == "r"
    assert set(out["takes"]) == {"r/01", "r/02", "r/03"}
    for lab in out["takes"].values():
        for group in ("energy", "whisper"):
            assert lab[group]["by"].startswith("tools/bank/label.py 1") or lab[group]["by"] == "whisper stand-in"
    assert out["takes"]["r/01"]["alignment"]["by"].startswith("tools/bank/label.py 1, pocketsphinx")
    assert out["takes"]["r/01"]["whisper"]["text"] == "Hey Emmett, what time is it?"
    assert out["takes"]["r/02"]["whisper"]["text"].startswith("Hey Emmett, open")
    assert out["flagged"] == {
        "r/02": ["whisper's words differ from the prompt: heard 'hey emmett open the door', prompted 'open the window'"],
        "r/03": ["words in a take where nobody speaks ('Hello?'): another voice or a TV"],
    }
    assert out["whisper_outside_slots"] == [["um", 99000, 99500, 0.5]]
    al = out["takes"]["r/01"]["alignment"]
    assert al["segment"] == [8000, 32000]  # from 0.5 s before the onset, as align_words.py began
    seg = al["segment"][0]
    assert al["name"] == [seg + 10 * 160, seg + 60 * 160] and al["emet_vowel_end"] == seg + 55 * 160
    assert al["first_word"] == ["what", seg + 60 * 160, seg + 90 * 160]


def test_a_take_is_flagged_only_when_the_slot_read_alone_disagrees_too():
    x = samples(q(1.0), tone(0.5), q(0.5), q(1.0), tone(0.5), q(0.5), q(2.0))
    doc = manifest_doc([
        take("r/01", "one_breath", "hey emet, what time is it", 0, 32000, question="what time is it"),
        take("r/02", "one_breath", "hey emet, open the window", 32000, 64000, question="open the window"),
        take("r/03", "room_tone", "nobody speaks", 64000, 96000),
    ])
    heard = [("Hello?", 70000, 72000, 0.5)]  # the file pass: nothing in 01 or 02, a word in the room tone
    alone = {0: [("Hey", 16000, 17000, 0.9), ("Emmett,", 17000, 18000, 0.9), ("what", 18000, 19000, 0.9),
                 ("time", 19000, 20000, 0.9), ("is", 20000, 21000, 0.9), ("it?", 21000, 22000, 0.9)],
             32000: [("Hey", 48000, 49000, 0.9), ("Emmett,", 49000, 50000, 0.9), ("open", 50000, 51000, 0.9)],
             64000: []}
    asked = []

    def slot(xs, a, b):
        asked.append((a, b))
        return alone[a]

    out = label.label_recording(doc, x, transcribe=lambda _x: heard, whisper_by="whisper stand-in",
                                aligner=fake_aligner, lexicon={"emet": ("EH M EH T",)}, transcribe_slot=slot)
    assert asked == [(0, 32000), (32000, 64000), (64000, 96000)]
    assert "r/01" not in out["flagged"] and "r/03" not in out["flagged"]
    assert out["takes"]["r/01"]["whisper"]["slot_text"] == "Hey Emmett, what time is it?"
    assert out["flagged"]["r/02"] == ["whisper's words differ from the prompt: heard '', prompted 'open the window'; "
                                      "read alone, the slot gives 'Hey Emmett, open'"]


def test_a_failed_alignment_is_flagged_and_an_excluded_take_is_not_listed():
    x = samples(q(1.0), tone(0.5), q(0.5), q(1.0), tone(0.5), q(0.5))
    doc = manifest_doc([
        take("r/01", "decoy", "hey Emma", 0, 32000),
        take("r/02", "decoy", "hey Emma", 32000, 64000, exclude={"reason": "marked a retake at the prompter", "date": "2026-10-27"}),
    ])
    out = label.label_recording(doc, x, transcribe=None, whisper_by=None, aligner=fake_aligner,
                                lexicon={"emet": ("EH M EH T",)})
    reasons = out["flagged"]["r/01"]
    assert any(r.startswith("the alignment failed: word pass") for r in reasons)
    assert "tried" in out["takes"]["r/01"]["alignment"] and len(out["takes"]["r/01"]["alignment"]["tried"]) == 2
    assert "r/02" not in out["flagged"] and out["takes"]["r/02"]["flags"]
    assert "whisper" not in out["takes"]["r/01"] and out["whisper_outside_slots"] is None


# ------------------------------------------------------------------ with pocketsphinx

def write_wav(path: Path, x: array) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(x)}h", *x))
    return path


def test_an_alignment_that_cannot_finish_is_returned_never_raised():
    pytest.importorskip("pocketsphinx")
    pcm = samples(q(0.05)).tobytes()
    res = label.align(pcm, label.words_of("hey emet, what time is it"), label.MEANS["carried"], label.shipped_lexicon())
    assert res["failed"] and res["failed"].startswith(("word pass", "phone pass"))


def test_the_extra_word_is_known_to_the_aligner():
    pytest.importorskip("pocketsphinx")
    d = label._decoder(None, label.shipped_lexicon())
    assert d.lookup_word("octopuses") is not None and d.lookup_word("emet") is not None


def test_the_pass_writes_labels_and_never_a_manifest(tmp_path, capsys):
    pytest.importorskip("pocketsphinx")
    bank = tmp_path / "bank"
    (bank / "manifests").mkdir(parents=True)
    (bank / "consent").mkdir()
    (bank / "consent" / "s01.txt").write_text("test\n", encoding="utf-8")
    wav = write_wav(bank / "r.wav", samples(q(1.0), tone(0.6), q(1.4)))
    digest = hashlib.sha256(wav.read_bytes()).hexdigest()
    man = {"format": "emet-bank/1", "id": "r", "path": "r.wav", "sha256": digest, "sample_rate": 16000,
           "channels": 1, "bits": 16, "frames": 48000, "duration_s": 3.0, "recorded": "2026-10-27",
           "start_time": None, "device": None, "capture_level": None, "room": "a", "position": None,
           "distance": "near", "distance_m": None, "facing": None, "speaker": "s01", "noise": None,
           "kind": "takes", "lead_s": 1.0, "prompt_every_s": 2.0, "made_with": "a test",
           "takes": [take("r/01", "one_breath", "hey emet, what time is it", 16000, 48000, question="what time is it")]}
    (bank / "manifests" / "r.json").write_text(json.dumps(man), encoding="utf-8")
    (bank / "bank.json").write_text(json.dumps({"format": "emet-bank/1", "version": "2026-10-27",
                                                "files": ["manifests/r.json"], "rooms": {"a": {"description": "t"}},
                                                "speakers": {"s01": {"consent": "consent/s01.txt"}}}), encoding="utf-8")
    before = {p: p.read_bytes() for p in (bank / "manifests").iterdir()}
    before[bank / "bank.json"] = (bank / "bank.json").read_bytes()
    assert label.main(["--bank", str(bank), "--no-whisper"]) == 0
    after = {p: p.read_bytes() for p in (bank / "manifests").iterdir()}
    after[bank / "bank.json"] = (bank / "bank.json").read_bytes()
    assert after == before
    out = json.loads((bank / "labels" / "r.json").read_text(encoding="utf-8"))
    assert list(out["takes"]) == ["r/01"] and out["sha256"] == digest
    assert out["made_with"]["tool"] == "tools/bank/label.py" and out["made_with"]["version"] == label.LABEL_VERSION


def test_whisper_is_asked_for_one_reading_at_temperature_0():
    """A label comes out the same each run. faster-whisper's default falls
    back to sampling, and with it stop-0.5.3's first four slots were empty
    in one pass and heard in the next (laptop, 2026-10-27 and 28)."""
    assert label.WHISPER_OPTIONS["temperature"] == 0.0
    assert label.WHISPER_OPTIONS["condition_on_previous_text"] is False


# ------------------------------------------------------------------ with faster-whisper

def test_whisper_words_come_back_in_samples():
    pytest.importorskip("faster_whisper")

    class Word:
        def __init__(self, word, start, end, probability):
            self.word, self.start, self.end, self.probability = word, start, end, probability

    class Seg:
        words = [Word(" Hey", 1.0, 1.25, 0.81), Word(" Emmett,", 1.25, 1.5, 0.9)]

    class Model:
        def transcribe(self, audio, **kw):
            assert kw == label.WHISPER_OPTIONS and audio.dtype.name == "float32"
            return iter([Seg()]), None

    w = label.Whisper.__new__(label.Whisper)
    w.model = Model()
    assert w(samples(q(2.0))) == [("Hey", 16000, 20000, 0.81), ("Emmett,", 20000, 24000, 0.9)]
    assert w.slot(samples(q(2.0)), 8000, 32000) == [("Hey", 24000, 28000, 0.81), ("Emmett,", 28000, 32000, 0.9)]
    w.name, w.revision, w.model_sha, w.compute_type = "m", None, "62b2a45b05ee59ac", "int8"
    w.versions = {"faster_whisper": "1.2.1", "ctranslate2": "4.8.3"}
    assert w.by == "tools/bank/label.py 1, faster-whisper 1.2.1, m, model.bin sha256 62b2a45b05ee, int8"
