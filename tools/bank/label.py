#!/usr/bin/env python3
"""Label the voice bank's takes, and list the ones a person should read.

For each recording bank.json lists, and each take with a slot:

    energy      where speech starts and ends in the slot by level, the
                slot's floor, its peak, and samples at full scale
    alignment   pocketsphinx forced alignment of the take's text, by word
                and by phone: the name, the end of the "-met" vowel, the
                first word after the name and its first phone
    whisper     a faster-whisper transcript of the whole recording with word
                times, each word given to the slot its end falls in; where
                it disagrees with the prompt, a second reading of the slot
                alone, and the take is flagged only when both disagree
    flags       a take to read: whisper's words differ from the prompt, the
                alignment failed or lacked a word, a sample clipped, no
                speech was found, the speech ran to the slot's end, or the
                signs of another voice or a TV under the take

It writes `<bank>/labels/<id>.json`, keyed by take id, and never writes a
manifest: labels are derived, and the bank is append-only (FORMAT.md). Each
label names the tool and version that made it. Every position is in samples
from the recording's first sample, as a slot is.

What it takes from `releases/v0.5.3/harness/align_words.py` (2026-10-03,
outside the repository): the bundled en-us model, a fresh decoder a take,
"emet" added with every pronunciation of the shipped lexicon, a word pass
with `Decoder.seg()` and then a phone pass with `set_alignment()`, a failed
pass returned and never raised, the fallbacks when a pass fails (the same
start with the end 0.3 s after the word pass's last word, then from the
slot's start), the phones of interest (emet's last vowel and its T, the
first word after emet and its phones), the speech onset rule
(`speech_onsets.py`'s: the first 20 ms window at least 20 dB over the
recording's floor, the 10th percentile of 10 ms RMS, that four more such
windows follow), and its two start means. It leaves out the wake boundary
B and the 12-segment spread, which only a replay of the wake engine has.

**Another voice.** No speaker model is run. A take is flagged for words
whisper heard beyond the prompt and a leading name, for words in a take of
room tone or a room's response, and for a slot whose floor sits 10 dB or
more over the recording's. The 10 dB is a choice: the floors of bank v0's
eleven take recordings lay between -57.5 and -53.6 dBFS
(`releases/v0.5.4/recordings-2026-10-06/wake/result.txt` section 5,
laptop, 2026-10-06), and this pass finds one-breath-0.5.2's at the same
-56.2.

Runs in a tools venv only (`tools/bank/requirements.txt`; D9, decided
2026-10-27): faster-whisper and pocketsphinx, imported by none of the four
packages. The model is pinned by its Hugging Face revision.

Usage:
    python tools/bank/label.py [--bank DIR] [--only ID,...] [--model small.en|DIR] [--no-whisper]
Exit:   0 labelled (flags are findings, printed), 1 a recording could not be read.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import math
import operator
import re
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import manifest as bank_files  # noqa: E402  the bank's one reader and writer, beside this file

LABEL_FORMAT = "emet-labels/1"
#: Raised when a rule below changes, so a label says which rules made it.
LABEL_VERSION = "1"
TOOL = "tools/bank/label.py"

RATE = 16000
TEN = 160   # 10 ms, for the floor
WIN = 320   # 20 ms, for the onset
ONSET_DB = 20.0
RUN = 5     # a loud window and four more
#: Quiet 20 ms windows that make a pause between the name and the first
#: word: 160 ms, longer than a stop's closure.
PAUSE_WINDOWS = 8
FLOOR_PERCENTILE = 10
NOISE_DB = 10.0
#: Words whisper may hear before the question for the name ("Hey Emmett,").
NAME_RUN = 3
FULL_SCALE = (32767, -32768)

#: The alignment decoder's start means, from align_words.py: the Pi
#: manifest's 0.4-era value and the carried mean of 2026-09-30.
MEANS = {
    "pi": "33.908,-4.91861,-0.0106799,6.36848,0.139045,3.90264,2.22221,2.65637,4.60943,2.24969,1.8505,1.37519,1.47413",
    "carried": "37.7991,-4.37088,0.731673,6.52026,-8.2342,-2.66036,-4.19786,-1.12147,3.13388,-2.69826,0.0248197,-2.71238,-0.93077",
    "none": None,
}

#: Kinds whose text is said and aligned; the others are labelled by level
#: and whisper only.
ALIGN_KINDS = frozenset({"wake_pause", "one_breath", "number", "short", "decoy", "pause", "long"})
#: Kinds in which nobody speaks: any word whisper hears there is flagged.
SILENT_KINDS = frozenset({"room_tone", "room_response"})
#: The vowels emet's pronunciations can end on before the T (SHIPPED_LEXICON).
EMET_VOWELS = ("EH", "EY", "IH", "AH", "IY")
#: Prompt words the bundled dictionary lacks, spelled from its own entries:
#: "octopus" (AA K T AH P UH S) with the two plural endings it gives
#: "campuses" (2026-10-27). The soak and one-breath-0.5.2 prompts use it.
EXTRA_LEXICON = {"octopuses": ("AA K T AH P UH S IH Z", "AA K T AH P UH S AH Z")}

#: The faster-whisper models label.py knows by name, each pinned to the
#: Hugging Face revision it was checked at. Systran's conversion of
#: Whisper small.en, licence MIT on its card (verified 2026-10-27).
MODELS = {
    "small.en": ("Systran/faster-whisper-small.en", "d1d751a5f8271d482d14ca55d9e2deeebbae577f"),
}
#: How whisper is asked. Each transcript is of the whole file, so a word is
#: heard with its neighbours; conditioning on the previous text is off, as
#: it carries a repeated prompt over into the next one. The temperature is 0
#: alone, so a label comes out the same each run: with faster-whisper's
#: default fallback to sampling, stop-0.5.3's first four slots were empty in
#: one pass and heard in the next (laptop, 2026-10-27 and 28).
WHISPER_OPTIONS = {"language": "en", "beam_size": 5, "word_timestamps": True,
                   "condition_on_previous_text": False, "vad_filter": False, "temperature": 0.0}


# ---------------------------------------------------------------- words

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
         "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINAL = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
            "nine": "ninth", "twelve": "twelfth"}
#: Spellings whisper uses for a word the prompts spell another way.
SAME = {"ok": "okay"}
#: For comparing only, on both sides: a contraction and its long form read
#: alike, and so do whisper's spellings of the name. Whisper wrote "I'm" for
#: a prompted "I am", "can't" for "cannot", "o clock" and "nevermind" on the
#: soak, and "Emmett" or "emit" for every "emet" (bank v0, 2026-10-27).
#: The aligner keeps the words as prompted.
CANON = {
    "i'm": ["i", "am"], "can't": ["can", "not"], "cannot": ["can", "not"], "won't": ["will", "not"],
    "don't": ["do", "not"], "isn't": ["is", "not"], "it's": ["it", "is"], "what's": ["what", "is"],
    "that's": ["that", "is"], "there's": ["there", "is"], "you're": ["you", "are"], "i'll": ["i", "will"],
    "let's": ["let", "us"], "o'clock": ["o", "clock"], "nevermind": ["never", "mind"],
}
NAME_FORMS = frozenset({"emet", "emmett", "emmet", "emit", "emmit", "emmitt", "amit"})


def say_number(n: int) -> list[str]:
    """A whole number from 0 to 9999 as words, the way the prompts write them."""
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_ONES[n // 100], "hundred"] + (say_number(n % 100) if n % 100 else [])
    return say_number(n // 1000) + ["thousand"] + (say_number(n % 1000) if n % 1000 else [])


def _ordinal(words: list[str]) -> list[str]:
    last = words[-1]
    if last in _ORDINAL:
        return words[:-1] + [_ORDINAL[last]]
    if last.endswith("y"):
        return words[:-1] + [last[:-1] + "ieth"]
    return words[:-1] + [last + "th"]


def _token_words(tok: str) -> list[str]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", tok)
    if m:
        hours, mins = int(m.group(1)), int(m.group(2))
        tail = [] if mins == 0 else (["oh"] + say_number(mins) if mins < 10 else say_number(mins))
        return say_number(hours) + tail
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", tok)
    if m and int(m.group(1)) < 10000:
        return _ordinal(say_number(int(m.group(1))))
    if tok.isdigit() and int(tok) < 10000:
        return say_number(int(tok))
    return [SAME.get(tok, tok)]


def words_of(text: str | None) -> list[str]:
    """The words of a prompt or a transcript, compared as words: lowercase,
    punctuation gone, digits said, a `[hold 1.5 s]` note dropped."""
    if not text:
        return []
    text = re.sub(r"\[[^\]]*\]", " ", text).lower().replace("’", "'").replace("...", " ")
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9']+(?::[0-9]{2})?", text):
        tok = tok.strip("'")
        if tok:
            out.extend(_token_words(tok))
    return out


def canon(words: list[str]) -> list[str]:
    out: list[str] = []
    for w in words:
        out.extend(["emet"] if w in NAME_FORMS else CANON.get(w, [w]))
    return out


def compare(expected: list[str], heard: list[str], named: bool) -> str | None:
    """None when whisper heard the expected words in order and together,
    with nothing after them and, when the take starts with the name, at most
    NAME_RUN words before them. Otherwise the reason, for the flag. Both
    sides are read through `canon` first; the reason quotes the words as
    whisper and the prompt have them."""
    said, prompted = " ".join(heard), " ".join(expected)
    expected, heard = canon(expected), canon(heard)
    n = len(expected)
    for i in range(len(heard) - n + 1):
        if heard[i:i + n] == expected:
            before, after = heard[:i], heard[i + n:]
            if (before and not named) or len(before) > NAME_RUN or after:
                return f"whisper heard words beyond the prompt: heard {said!r}, prompted {prompted!r}"
            return None
    return f"whisper's words differ from the prompt: heard {said!r}, prompted {prompted!r}"


def expected_words(take: dict) -> tuple[list[str], bool]:
    """What whisper should hear, and whether a name may come before it."""
    named = words_of(take.get("text"))[:2] == ["hey", "emet"]
    if named and take.get("question"):
        return words_of(take["question"]), True
    return words_of(take.get("text")), False


# ---------------------------------------------------------------- level

def load_samples(path: Path) -> array:
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path.name}: the bank holds 16 kHz mono 16-bit")
        x = array("h")
        x.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        x.byteswap()
    return x


def db(rms: float) -> float:
    return 20 * math.log10(max(rms, 1e-3) / 32768.0)


class Level:
    """Window levels of one recording, with its mean taken out as
    align_words.py takes it out."""

    def __init__(self, x: array) -> None:
        self.x = x
        self.n = len(x)
        self.dc = sum(x) / self.n if self.n else 0.0
        tens = sorted(self.window_db(i, TEN) for i in range(0, self.n - TEN, TEN))
        self.floor = tens[len(tens) * FLOOR_PERCENTILE // 100] if tens else db(0)

    def window_db(self, start: int, size: int) -> float:
        seg = self.x[start:start + size]
        if not seg:
            return db(0)
        n = len(seg)
        sq = sum(map(operator.mul, seg, seg))
        s = sum(seg)
        var = (sq - 2 * self.dc * s + n * self.dc * self.dc) / n
        return db(math.sqrt(max(var, 0.0)))

    def loud(self, a: int, b: int) -> list[bool]:
        """One flag a 20 ms window from a, the onset rule's test."""
        return [self.window_db(s, WIN) >= self.floor + ONSET_DB for s in range(a, b - WIN + 1, WIN)]

    def speech(self, a: int, b: int) -> tuple[int | None, int | None, bool]:
        """(onset, end, runs to the end) of speech in [a, b): the first and
        last runs of RUN loud windows; `runs to the end` when the slot's last
        window is in such a run."""
        loud = self.loud(a, b)
        runs: list[tuple[int, int]] = []
        k = 0
        while k < len(loud):
            if loud[k]:
                j = k
                while j < len(loud) and loud[j]:
                    j += 1
                if j - k >= RUN:
                    runs.append((k, j))
                k = j
            else:
                k += 1
        if not runs:
            return None, None, False
        return a + runs[0][0] * WIN, a + runs[-1][1] * WIN, runs[-1][1] == len(loud)

    def onset_after_quiet(self, a: int, b: int) -> int | None:
        """The first run of RUN loud windows in [a, b) that follows at least
        PAUSE_WINDOWS quiet ones: the first word's start after a pause. One
        quiet window is too few, since the closure of a stop inside "what's
        two" is one (one-breath-0.5.2 take 01, 2026-10-27)."""
        loud = self.loud(a, b)
        quiet = 0
        for k in range(len(loud) - RUN + 1):
            if not loud[k]:
                quiet += 1
            elif quiet >= PAUSE_WINDOWS and all(loud[k:k + RUN]):
                return a + k * WIN
        return None

    def slot_floor(self, a: int, b: int) -> float:
        tens = sorted(self.window_db(i, TEN) for i in range(a, b - TEN, TEN))
        return tens[len(tens) * FLOOR_PERCENTILE // 100] if tens else self.floor

    def peak(self, a: int, b: int) -> tuple[float, int]:
        seg = self.x[a:b]
        if not seg:
            return db(0), 0
        p = max(max(seg), -min(seg))
        return (20 * math.log10(p / 32768) if p else -120.0), sum(seg.count(v) for v in FULL_SCALE)


# ---------------------------------------------------------------- alignment

def shipped_lexicon(repo: Path | None = None) -> dict[str, tuple[str, ...]]:
    """SHIPPED_LEXICON read from emet_hal's source, so the tools venv needs
    no Emet package installed and the aligner says "emet" as the wake engine
    does."""
    repo = repo or HERE.parents[1]
    src = (repo / "emet-hal" / "emet_hal" / "pocketsphinx_wake.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "SHIPPED_LEXICON":
            return ast.literal_eval(node.value)
    raise LookupError("SHIPPED_LEXICON is not in emet_hal/pocketsphinx_wake.py")


def _decoder(mean: str | None, lexicon: dict[str, tuple[str, ...]]):
    sys.modules.setdefault("sounddevice", None)  # type: ignore[arg-type]  # no audio device in this process
    from pocketsphinx import Config, Decoder
    cfg = {"loglevel": "FATAL"}
    if mean:
        cfg["cmninit"] = mean
    d = Decoder(Config(**cfg))
    for i, pron in enumerate(lexicon["emet"]):
        d.add_word("emet" if i == 0 else f"emet({i + 1})", pron, True)
    for word, prons in EXTRA_LEXICON.items():
        if d.lookup_word(word) is None:
            for i, pron in enumerate(prons):
                d.add_word(word if i == 0 else f"{word}({i + 1})", pron, True)
    return d


def align(pcm: bytes, words: list[str], mean: str | None, lexicon: dict[str, tuple[str, ...]]) -> dict:
    """align_words.align(): pass 1 words (Decoder.seg), pass 2 phones
    (set_alignment). A pass can fail with "Failed to stop utterance
    processing", which says only that no path reached the end of the text;
    the failure is returned, never raised. Frames are 10 ms."""
    d = _decoder(mean, lexicon)
    unknown = [w for w in words if d.lookup_word(w) is None]
    known = [w for w in words if w not in unknown]
    out: dict = {"unknown": unknown, "seg": None, "words": [], "phones": [], "failed": None}
    if not known:
        out["failed"] = "no word of the text is in the dictionary"
        return out
    d.set_align_text(" ".join(known))
    d.start_utt()
    d.process_raw(pcm, False, True)
    try:
        d.end_utt()
    except RuntimeError as exc:
        out["failed"] = f"word pass: {exc}"
        return out
    # Audio too short for the text ends the utterance without raising and
    # with no segmentation at all (50 ms for six words, 2026-10-27), which
    # align_words.py did not meet; it is a failure like the other.
    seg = d.seg()
    if seg is None:
        out["failed"] = "word pass: no segmentation"
        return out
    out["seg"] = [(s.word, s.start_frame, s.end_frame + 1) for s in seg]
    d.set_alignment()
    d.start_utt()
    d.process_raw(pcm, False, True)
    try:
        d.end_utt()
    except RuntimeError as exc:
        out["failed"] = f"phone pass: {exc}"
        return out
    al = d.get_alignment()
    if al is None:
        out["failed"] = "phone pass: no alignment"
        return out
    out["words"] = [(e.name, e.start, e.start + e.duration) for e in al.words()]
    out["phones"] = [(e.name, e.start, e.start + e.duration) for e in al.phones()]
    return out


def _last_word_end(res: dict) -> int | None:
    ends = [e for nm, s, e in (res["seg"] or []) if nm not in ("<sil>", "<s>", "</s>")]
    return max(ends) if ends else None


def align_somewhere(raw: bytes, starts: list[int], end: int, words: list[str], mean: str | None,
                    lexicon: dict[str, tuple[str, ...]], aligner: Callable = align) -> tuple[dict, int, int, list[str]]:
    """align_words.align_somewhere(), with the slot's start where it had
    the wake boundary: each start in turn, first to the slot's end and then
    to 0.3 s after the word pass's last word, until both passes align."""
    tried: list[str] = []
    res: dict = {}
    a = b = starts[0]
    for a in starts:
        for trim in (False, True):
            b = end
            if trim:
                last = _last_word_end(res) if res else None
                if last is None:
                    continue
                b = min(end, a + last * TEN + 3 * RATE // 10)
            res = aligner(raw[2 * a:2 * b], words, mean, lexicon)
            tried.append(f"{a / RATE:.2f}-{b / RATE:.2f} s: {res['failed'] or 'aligned'}")
            if not res["failed"]:
                return res, a, b, tried
    return res, a, b, tried


def place(res: dict) -> dict:
    """align_words.place(): the phones of interest, in frames from the
    segment's start. Without emet (a decoy, a take that lacks the name) the
    first word is the first word said."""
    words, phones = res["words"], res["phones"]
    real = [w for w in words if w[0] not in ("<sil>", "<s>", "</s>")]
    out: dict = {}
    emet = next((w for w in real if w[0].startswith("emet")), None)
    if emet is not None:
        inside = [p for p in phones if emet[1] <= p[1] < emet[2]]
        vowels = [p for p in inside if p[0] in EMET_VOWELS]
        out["emet"] = emet
        out["emet_last_vowel"] = vowels[-1] if vowels else None
        ts = [p for p in inside if p[0] == "T"]
        out["emet_t"] = ts[-1] if ts else None
        hey = [w for w in real if w[0] == "hey" and w[2] <= emet[1]]
        out["name"] = ((hey[-1][1] if hey else emet[1]), emet[2])
        after = [w for w in real if w[1] >= emet[2]]
    else:
        after = real
    first = after[0] if after else None
    out["first_word"] = first
    if first is not None:
        qph = [p for p in phones if first[1] <= p[1] < first[2]]
        out["first_phone"] = qph[0] if qph else None
    return out


# ---------------------------------------------------------------- whisper

class Whisper:
    """A faster-whisper model, loaded once, transcribing a whole recording
    into (word, start sample, end sample, probability)."""

    def __init__(self, model: str, compute_type: str = "int8", threads: int = 0) -> None:
        import faster_whisper
        import ctranslate2
        path = Path(model)
        if path.is_dir():
            self.name, self.revision, local = path.name, None, str(path)
        elif model in MODELS:
            from huggingface_hub import snapshot_download
            repo, self.revision = MODELS[model]
            self.name = repo
            local = snapshot_download(repo_id=repo, revision=self.revision)
        else:
            raise SystemExit(f"label: no model {model!r}: name one of {sorted(MODELS)} or a directory")
        # The weights' hash names the model exactly, from a pinned download
        # or from a directory given by hand.
        weights = Path(local) / "model.bin"
        self.model_sha = bank_files.sha256_file(weights) if weights.is_file() else None
        self.versions = {"faster_whisper": faster_whisper.__version__, "ctranslate2": ctranslate2.__version__}
        self.compute_type = compute_type
        self.model = faster_whisper.WhisperModel(local, device="cpu", compute_type=compute_type, cpu_threads=threads)

    @property
    def by(self) -> str:
        rev = f" at {self.revision[:8]}" if self.revision else ""
        sha = f", model.bin sha256 {self.model_sha[:12]}" if self.model_sha else ""
        return (f"{TOOL} {LABEL_VERSION}, faster-whisper {self.versions['faster_whisper']}, "
                f"{self.name}{rev}{sha}, {self.compute_type}")

    def __call__(self, x: array) -> list[tuple[str, int, int, float]]:
        import numpy as np
        audio = np.frombuffer(x.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(audio, **WHISPER_OPTIONS)
        out = []
        for seg in segments:
            for w in seg.words or []:
                out.append((w.word.strip(), int(round(w.start * RATE)), int(round(w.end * RATE)),
                            round(float(w.probability), 3)))
        return out

    def slot(self, x: array, a: int, b: int) -> list[tuple[str, int, int, float]]:
        """The slot [a, b) transcribed alone, in samples from the recording's start."""
        return [(w, s + a, e + a, p) for w, s, e, p in self(x[a:b])]


# ---------------------------------------------------------------- one recording

def _pocketsphinx_version() -> str:
    try:
        from importlib.metadata import version
        return version("pocketsphinx")
    except Exception:  # noqa: BLE001  any failure means unknown
        return "unknown"


def _revision() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def label_recording(doc: dict, x: array, *, transcribe: Callable | None, whisper_by: str | None,
                    mean_name: str = "carried", lexicon: dict | None = None,
                    aligner: Callable = align, made: str | None = None,
                    transcribe_slot: Callable | None = None) -> dict:
    """The labels of one recording, from its manifest and its samples."""
    lexicon = lexicon or shipped_lexicon()
    mean = MEANS[mean_name]
    lv = Level(x)
    raw = x.tobytes() if sys.byteorder == "little" else _little(x)
    heard = transcribe(x) if transcribe else None
    takes_out: dict[str, dict] = {}
    flagged: dict[str, list[str]] = {}
    by_take: dict[str, list] = {t["id"]: [] for t in doc["takes"]}
    outside: list = []
    # Each word goes to the slot its end falls in. Whisper starts a word that
    # follows a silence at the end of the word before: one-breath-0.5.2's
    # take 19 "Hey" ran from 107.78 s, inside take 18's slot, to 111.90 s,
    # where the level put take 19's speech (laptop, 2026-10-27).
    for w in heard or []:
        take = bank_files.take_at(doc, max(w[1], w[2] - 1))
        (by_take[take["id"]] if take else outside).append(list(w))
    energy_by = f"{TOOL} {LABEL_VERSION}, energy: {ONSET_DB:g} dB over the floor for {RUN} windows of 20 ms"
    align_by = f"{TOOL} {LABEL_VERSION}, pocketsphinx {_pocketsphinx_version()}, en-us, start mean {mean_name}"
    for take in doc["takes"]:
        a, b = take["slot"]["start"], min(take["slot"]["end"], lv.n)
        kind = take["kind"]
        flags: list[str] = []
        onset, end, to_end = lv.speech(a, b)
        peak, clipped = lv.peak(a, b)
        sfloor = lv.slot_floor(a, b)
        energy = {"by": energy_by, "onset": onset, "end": end, "runs_to_slot_end": to_end,
                  "slot_floor_dbfs": round(sfloor, 1), "peak_dbfs": round(peak, 1), "clipped": clipped}
        label: dict = {"energy": energy}
        if clipped:
            flags.append(f"clipped: {clipped} sample(s) at full scale")
        if sfloor - lv.floor >= NOISE_DB:
            flags.append(f"noise under the take: its floor is {sfloor - lv.floor:.1f} dB over the recording's")
        if kind in ALIGN_KINDS:
            if onset is None:
                flags.append("no speech found by level")
            elif to_end:
                flags.append("speech runs to the slot's end")
            words = words_of(take.get("text"))
            # From 0.5 s before the onset first, as align_words.py began;
            # then from the slot's start.
            first = max(0, onset - RATE // 2) if onset is not None else a
            starts = [first] + ([a] if a != first else [])
            res, sa, sb, tried = align_somewhere(raw, starts, b, words, mean, lexicon, aligner)
            at = lambda fr: sa + fr * TEN  # noqa: E731  a 10 ms frame to a sample
            span = lambda v: None if v is None else [v[0], at(v[1]), at(v[2])]  # noqa: E731
            pl = place(res) if not res["failed"] else {}
            label["alignment"] = {
                "by": align_by, "text": " ".join(words), "failed": res["failed"], "unknown": res["unknown"],
                "segment": [sa, sb], "tried": tried,
                "words": [span(w) for w in res["words"] if w[0] not in ("<s>", "</s>")],
                "phones": [span(p) for p in res["phones"]],
                "name": None if not pl.get("name") else [at(pl["name"][0]), at(pl["name"][1])],
                "emet_vowel_end": None if not pl.get("emet_last_vowel") else at(pl["emet_last_vowel"][2]),
                "emet_t": span(pl.get("emet_t")),
                "first_word": span(pl.get("first_word")),
                "first_phone": span(pl.get("first_phone")),
            }
            if res["failed"]:
                flags.append(f"the alignment failed: {res['failed']}")
            if res["unknown"]:
                flags.append(f"words the aligner's dictionary lacks, left out of the alignment: {res['unknown']}")
            # The first word's start by level: after a pause past the name,
            # and before the aligner's end of that word, so a pause later in
            # the question is never taken for it. None in one breath.
            name, fw = label["alignment"]["name"], label["alignment"]["first_word"]
            energy["first_word_onset"] = lv.onset_after_quiet(name[1], fw[2]) if name and fw else None
        if heard is not None:
            mine = by_take[take["id"]]
            text = " ".join(w[0] for w in mine)
            wl = {"by": whisper_by, "text": text, "words": mine}
            label["whisper"] = wl
            why = None
            expected, named = expected_words(take)
            if kind in SILENT_KINDS and mine:
                why = f"words in a take where nobody speaks ({text!r}): another voice or a TV"
            elif kind in ALIGN_KINDS:
                why = compare(expected, words_of(text), named)
            if why and transcribe_slot is not None:
                # A second reading, of the slot alone. The pass over the
                # whole file ended the soak's take 19 at "call my sister
                # to", which read alone ends "tomorrow morning" (laptop,
                # 2026-10-28), so a take is flagged only when this reading
                # disagrees too.
                again = [list(w) for w in transcribe_slot(x, a, b)]
                stext = " ".join(w[0] for w in again)
                wl["slot_text"], wl["slot_words"] = stext, again
                if kind in SILENT_KINDS:
                    why = None if not again else f"{why}; read alone, the slot gives {stext!r}"
                elif compare(expected, words_of(stext), named) is None:
                    why = None
                else:
                    why = f"{why}; read alone, the slot gives {stext!r}"
            if why:
                flags.append(why)
        label["flags"] = flags
        takes_out[take["id"]] = label
        if flags and not take.get("exclude"):
            flagged[take["id"]] = flags
    return {
        "format": LABEL_FORMAT,
        "recording": doc["id"],
        "sha256": doc["sha256"],
        "made": made or datetime.date.today().isoformat(),
        "made_with": {"tool": TOOL, "version": LABEL_VERSION, "revision": _revision(),
                      "pocketsphinx": _pocketsphinx_version(), "whisper": whisper_by, "start_mean": mean_name},
        "floor_dbfs": round(lv.floor, 1),
        "whisper_outside_slots": outside if heard is not None else None,
        "takes": takes_out,
        "flagged": flagged,
    }


def _little(x: array) -> bytes:
    y = array("h", x)
    y.byteswap()
    return y.tobytes()


def write_labels(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(bank_files.dump(doc), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def default_bank() -> Path:
    """Where the bank lives (D4, decided 2026-10-27): beside the repository on
    the laptop, `~/voicebank` on the reference body."""
    beside = HERE.parents[2] / "voicebank"
    return beside if (beside / "bank.json").is_file() else Path("~/voicebank").expanduser()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, help="the bank's root (default: Project Emet/voicebank, else ~/voicebank)")
    ap.add_argument("--search", type=Path, action="append", default=[], metavar="DIR",
                    help="a directory to look in for a recording not at its manifest's path")
    ap.add_argument("--only", help="label only these recording ids, comma-separated")
    ap.add_argument("--labels", type=Path, help="where the labels go (default <bank>/labels)")
    ap.add_argument("--model", default="small.en", help=f"a model name ({', '.join(MODELS)}) or a model directory")
    ap.add_argument("--compute-type", default="int8")
    ap.add_argument("--threads", type=int, default=0, help="CPU threads for whisper (0: its default)")
    ap.add_argument("--no-whisper", action="store_true", help="level and alignment only")
    ap.add_argument("--mean", choices=sorted(MEANS), default="carried", help="the alignment decoder's start mean")
    args = ap.parse_args(argv)
    bank = (args.bank or default_bank()).resolve()
    out_dir = args.labels or bank / "labels"
    only = set(args.only.split(",")) if args.only else None
    whisper = None if args.no_whisper else Whisper(args.model, args.compute_type, args.threads)
    lexicon = shipped_lexicon()
    t_all = time.monotonic()
    bad = 0
    total_takes = total_flagged = 0
    audio_s = 0.0
    try:
        listed = list(bank_files.manifests(bank))
    except bank_files.BankError as exc:
        print(f"label: {exc}", file=sys.stderr)
        return 1
    for mpath, doc in listed:
        if only and doc["id"] not in only:
            continue
        wav = bank_files.find_audio(doc, bank, args.search)
        if wav is None:
            print(f"{doc['id']}: no file with sha256 {doc['sha256'][:12]}... found", file=sys.stderr)
            bad += 1
            continue
        t0 = time.monotonic()
        try:
            x = load_samples(wav)
        except (OSError, EOFError, wave.Error, ValueError) as exc:
            print(f"{doc['id']}: {exc}", file=sys.stderr)
            bad += 1
            continue
        labels = label_recording(doc, x, transcribe=whisper, whisper_by=whisper.by if whisper else None,
                                 mean_name=args.mean, lexicon=lexicon,
                                 transcribe_slot=whisper.slot if whisper else None)
        write_labels(out_dir / f"{doc['id']}.json", labels)
        took = time.monotonic() - t0
        audio_s += len(x) / RATE
        total_takes += len(labels["takes"])
        total_flagged += len(labels["flagged"])
        print(f"{doc['id']}: {len(labels['takes'])} take(s), {len(labels['flagged'])} flagged, "
              f"{len(x) / RATE:.0f} s of audio in {took:.1f} s")
        for tid, why in labels["flagged"].items():
            for reason in why:
                print(f"    {tid}: {reason}")
    took = time.monotonic() - t_all
    print(f"total: {total_takes} take(s), {total_flagged} flagged, {audio_s / 60:.1f} min of audio "
          f"in {took / 60:.1f} min; labels in {out_dir}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
