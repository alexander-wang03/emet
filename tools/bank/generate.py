#!/usr/bin/env python3
"""Build the generated bank from pinned public voices played through simulated rooms.

    python tools/bank/generate.py OUT --set small --voices DIR --piper PYTHON_OR_EXE [--synth-cache DIR]
    python tools/bank/generate.py --plan [--set full]
    python tools/bank/generate.py --fetch-voices DIR
    python tools/bank/generate.py --sums BANK
    python tools/bank/generate.py --pack BANK --to DIR
    python tools/bank/generate.py --fetch URL --sha256 HEX --to DIR
    python tools/bank/generate.py --compare BANK OTHER
    python tools/bank/generate.py --clip FILE --voices DIR --piper PYTHON_OR_EXE

`--plan` prints every set, or the one `--set` names. `--sums BANK` writes
a built bank's `SHA256SUMS` again after a file is added to it, such as a
cassette under `cassettes/`, and checks the bank.

The real voice bank is the maintainer's voice and never leaves their
machines. This builds a second bank that anybody may hold: speech made by
Piper from voices whose every dataset allows it (`generated/voices.json`),
played through simulated rooms (`generated/rooms.json`), saying phrases whose
text is already in this repository (`generated/phrases.json`). It is written
in the bank's own format (`FORMAT.md`), so `check.py` holds it and
`replay.py` replays it against goldens in `golden/generated/`.

**What it may decide.** Generated speech in a simulated room screens a
change: a golden that moved says look here. It never sets or moves a
default, never gives a recall or false-wake rate for a real room, and never
stands for a run on a body. A synthetic voice is no second voice, and a
simulated room is no second room.

**Voices.** `--voices DIR` holds each voice's `<name>.onnx`,
`<name>.onnx.json` and `<name>.MODEL_CARD`, as `--fetch-voices DIR` downloads
them from the pinned revision. A file whose sha256 is not the pinned one is
refused, and so is a card whose Training section does not name the base the
list records (`check.card_problems`).

**Piper is a tool here.** It is GPL-3.0-or-later, so it lives in a scratch
venv outside the repository and this script imports nothing from it: it
runs `PYTHON -m piper` (or the `piper` program) as a child process, one a
voice and speaking rate, with both of Piper's noise scales at 0. With its
own noise, two runs of one sentence gave different samples; with none, the
same (measured 2026-11-26, piper-tts 1.8.0, four voices).

**Everything after synthesis is whole-number arithmetic**, so the same
utterances give the same bytes on any machine: the resampler's taps, the
room's response and the noise are integers, and every sum is exact. numpy
makes it fast and is optional; without it the same integers are computed in
plain Python, slowly, which is how the tests run. No sine, logarithm or
power from a maths library reaches a sample: the resampler's window is
computed by `_sin_pi`, a series in the four operations.

**The rooms** are shoebox rooms by the image method of Allen and Berkley
(J. Acoust. Soc. Am. 65(4), 1979, an idea, cited in `CITATIONS.md`): every
mirror image of the talker that arrives within the room's `length_s` adds
one tap, at the nearest sample, of size 1 over its distance times the
walls' reflection once a bounce, the reflection being sqrt(1 - absorption)
from the absorption the room list states. The decay is never stated: it is
read back from the response by Schroeder's integral and recorded.
pyroomacoustics (0.10.1, MIT, needing numpy, scipy and Cython; PyPI,
verified 2026-11-26) would do the same with more packages in the tools venv
and a response that follows its version; this is about seventy lines of
square roots and the four operations, the same integers everywhere.

**The noise** is pink, by the Voss algorithm as Gardner described it
(Scientific American 238(4), 1978): rows of white noise, row k held for
2**k samples, summed. Each value is SplitMix64's mixing function (Steele,
Lea and Flood, OOPSLA 2014; Vigna's public-domain code) of a seed and a
counter, so any stretch can be made without the stretch before it.

**Two sets from one rule** (`plan`): the lists make voices x conditions x
parts recordings, part k holding every phrase whose number is k modulo the
parts. `full` is all of them. `small` is one recording a condition, the
part and the voice both stepping with the condition, so it says every
phrase once and is a subset of `full`, file for file.

numpy, when present, is the only package outside the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import wave
from array import array
from pathlib import Path
from typing import Any, Callable, Sequence

HERE = Path(__file__).resolve().parent
DATA = HERE / "generated"
sys.path.insert(0, str(HERE))

import check as bank_check  # noqa: E402  the standard library only
import manifest as bank_files  # noqa: E402

__all__ = [
    "GenerateError", "load_inputs", "plan", "build", "verify_voices", "PiperTool", "fetch", "pack",
    "fetch_voices", "make_clip", "resample", "room_response", "backend", "write_sums", "sums_again",
]

#: The bank's rate: what the wake engine states and the wav source gives it.
RATE = 16000
#: Fixed point: a tap, a gain or a coefficient is an integer over 2**15.
Q = 15
ONE = 1 << Q
HALF = 1 << (Q - 1)

#: The resampler: input samples under its window, and where it cuts, as a
#: fraction of the lower of the two Nyquist rates. With 64 taps under a
#: Blackman window, 22050 Hz becoming 16000 Hz is flat to 6 kHz (0.0 dB),
#: 0.4 dB down at 6.5 kHz and 75.6 dB down at 8.1 kHz: measured 2026-11-26
#: with tones through the numpy path (releases/v0.5.6/reports/C3-logs/
#: c3_resampler.py).
RESAMPLE_TAPS = 64
RESAMPLE_PASS = (22, 25)

#: Rows of the pink noise: row k changes every 2**k samples, so twelve rows
#: reach from 8 kHz down to about 4 Hz.
PINK_ROWS = 12
#: The standard deviation of twelve such rows summed, to the whole number.
PINK_SPREAD = math.isqrt(PINK_ROWS * ((1 << 32) - 1) // 12)

_MASK = (1 << 64) - 1
_GAMMA = 0x9E3779B97F4A7C15
_MIX_1 = 0xBF58476D1CE4E5B9
_MIX_2 = 0x94D049BB133111EB

#: Where a take starts inside its slot, and how far the seed may move it: a
#: take always at a multiple of the wake engine's 80 ms frame would test one
#: frame phase only.
TAKE_OFFSET_S = 0.5
TAKE_JITTER_S = 0.5


class GenerateError(RuntimeError):
    """A bank that cannot be built, or a file that is not the pinned one."""


# ====================================================== exact arithmetic


def _sin_pi(num: int, den: int) -> float:
    """sin(pi * num / den) from additions, multiplications and divisions.

    IEEE 754 rounds those four the same way on every machine; a maths
    library's sine may differ in its last digit from one platform to the
    next, and one digit can move a rounded tap. The angle is folded into the
    first quarter turn with whole numbers first, so the series is short."""
    num %= 2 * den
    sign = 1.0
    if num >= den:
        num -= den
        sign = -1.0
    if 2 * num > den:
        num = den - num
    x = math.pi * num / den
    x2 = x * x
    term = total = x
    for k in range(1, 15):
        term = -term * x2 / ((2 * k) * (2 * k + 1))
        total += term
    return sign * total


def _cos_pi(num: int, den: int) -> float:
    return _sin_pi(2 * num + den, 2 * den)


_KERNELS: dict[tuple[int, int], list[list[int]]] = {}


def resample_kernel(up: int, down: int) -> list[list[int]]:
    """The resampler's taps, one row an output phase, each row summing to ONE.

    A sinc cut at RESAMPLE_PASS of the lower Nyquist rate under a Blackman
    window, sampled at the `up` phases an output sample can fall on between
    two input samples. Each row is scaled to sum to exactly ONE, so a
    constant goes through unchanged at every phase."""
    if (up, down) in _KERNELS:
        return _KERNELS[(up, down)]
    wide, half = max(up, down), RESAMPLE_TAPS // 2
    span = half * up
    num, den = RESAMPLE_PASS

    def tap(m: int) -> float:
        if abs(m) >= span:
            return 0.0
        core = 1.0 if m == 0 else _sin_pi(num * m, den * wide) / (math.pi * num * m / (den * wide))
        return core * (0.42 + 0.5 * _cos_pi(m, span) + 0.08 * _cos_pi(2 * m, span))

    kernel = []
    for phase in range(up):
        row = [tap(phase - j * up) for j in range(-half + 1, half + 1)]
        total = 0.0
        for value in row:
            total += value
        ints = [int(round(value / total * ONE)) for value in row]
        ints[ints.index(max(ints))] += ONE - sum(ints)
        kernel.append(ints)
    _KERNELS[(up, down)] = kernel
    return kernel


def _mix(z: int) -> int:
    """SplitMix64's output function: 64 bits in, 64 well-stirred bits out."""
    z &= _MASK
    z = ((z ^ (z >> 30)) * _MIX_1) & _MASK
    z = ((z ^ (z >> 27)) * _MIX_2) & _MASK
    return z ^ (z >> 31)


def derive_seed(seed: int, *names: Any) -> int:
    """A 64-bit seed for one named use of the bank's seed: a recording's
    noise, a take's place in its slot. sha256, so it reads the same on any
    Python."""
    text = json.dumps([int(seed), *[str(n) for n in names]])
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


class _Plain:
    """The arithmetic in plain Python: lists of whole numbers."""

    name = "python"

    def zeros(self, n: int) -> list[int]:
        return [0] * n

    def from_pcm(self, pcm: bytes) -> list[int]:
        return array("h", pcm).tolist() if sys.byteorder == "little" else _swapped(pcm).tolist()

    def length(self, x: Any) -> int:
        return len(x)

    def join(self, parts: list[Any]) -> list[int]:
        out: list[int] = []
        for part in parts:
            out.extend(part)
        return out

    def add_at(self, into: list[int], at: int, x: Sequence[int]) -> None:
        end = min(len(into), at + len(x))
        into[at:end] = [a + b for a, b in zip(into[at:end], x)]

    def resample(self, x: Sequence[int], up: int, down: int) -> list[int]:
        kernel, half = resample_kernel(up, down), RESAMPLE_TAPS // 2
        padded = [0] * half + list(x) + [0] * half
        out = []
        for n in range((len(x) * up + down - 1) // down):
            q, phase = divmod(n * down, up)
            row = kernel[phase]
            acc = 0
            for k in range(RESAMPLE_TAPS):
                acc += row[k] * padded[q + 1 + k]
            out.append((acc + HALF) >> Q)
        return out

    def convolve(self, x: Sequence[int], taps: Sequence[int]) -> list[int]:
        out = [0] * (len(x) + len(taps) - 1)
        n = len(x)
        for k, tap in enumerate(taps):
            if tap:
                out[k:k + n] = [a + tap * b for a, b in zip(out[k:k + n], x)]
        return [(v + HALF) >> Q for v in out]

    def pink(self, n: int, seed: int, rms: int) -> list[int]:
        total = [0] * n
        for row in range(PINK_ROWS):
            row_seed, held = _mix(seed + row), 1 << row
            values = [(_mix(row_seed + i * _GAMMA) >> 48) - 32768 for i in range((n + held - 1) // held)]
            if held == 1:
                total = [a + b for a, b in zip(total, values)]
            else:
                total = [a + values[i >> row] for i, a in enumerate(total)]
        return [(v * rms + PINK_SPREAD // 2) // PINK_SPREAD for v in total]

    def to_pcm(self, x: Sequence[int]) -> tuple[bytes, int]:
        clipped = sum(1 for v in x if v > 32767 or v < -32768)
        out = array("h", [32767 if v > 32767 else -32768 if v < -32768 else v for v in x])
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes(), clipped


def _swapped(pcm: bytes) -> array:
    out = array("h", pcm)
    out.byteswap()
    return out


class _Fast:
    """The same whole numbers through numpy."""

    name = "numpy"

    def __init__(self, np: Any) -> None:
        self.np = np

    def zeros(self, n: int) -> Any:
        return self.np.zeros(n, dtype=self.np.int64)

    def from_pcm(self, pcm: bytes) -> Any:
        return self.np.frombuffer(pcm, dtype="<i2").astype(self.np.int64)

    def length(self, x: Any) -> int:
        return int(len(x))

    def join(self, parts: list[Any]) -> Any:
        np = self.np
        return np.concatenate([np.asarray(p, dtype=np.int64) for p in parts]) if parts else self.zeros(0)

    def add_at(self, into: Any, at: int, x: Any) -> None:
        end = min(len(into), at + len(x))
        into[at:end] += self.np.asarray(x, dtype=self.np.int64)[:end - at]

    def resample(self, x: Any, up: int, down: int) -> Any:
        np = self.np
        kernel, half = np.array(resample_kernel(up, down), dtype=np.int64), RESAMPLE_TAPS // 2
        x = np.asarray(x, dtype=np.int64)
        padded = np.zeros(len(x) + 2 * half, dtype=np.int64)
        padded[half:half + len(x)] = x
        n_out = (len(x) * up + down - 1) // down
        out = np.empty(n_out, dtype=np.int64)
        taps = np.arange(RESAMPLE_TAPS, dtype=np.int64)
        for start in range(0, n_out, 1 << 15):
            n = np.arange(start, min(start + (1 << 15), n_out), dtype=np.int64)
            q, phase = np.divmod(n * down, up)
            block = (padded[(q + 1)[:, None] + taps[None, :]] * kernel[phase]).sum(axis=1)
            out[start:start + len(n)] = (block + HALF) >> Q
        return out

    def convolve(self, x: Any, taps: Any) -> Any:
        np = self.np
        x, taps = np.asarray(x, dtype=np.int64), np.asarray(taps, dtype=np.int64)
        return (self._exact_convolve(x, taps) + HALF) >> Q

    def _exact_convolve(self, x: Any, taps: Any) -> Any:
        """The whole-number convolution, by a floating transform when it
        passes two checks and by the plain sum otherwise.

        The transform's error here is about a ten-thousandth of a sample, so
        rounding gives the exact integers. It is held to two checks: every
        value must land within 1/64 of a whole number, and the result's
        plain and alternating sums must equal the products of the inputs'
        own, which whole-number convolution satisfies exactly. They catch
        any single value one off. Errors that cancel in both sums, such as
        one value one up and another of the same parity one down, would
        pass them, so the checks make an error unlikely and prove nothing
        more. A result that fails either is thrown away for numpy's integer
        convolution, which is exact and slower."""
        np = self.np
        n = len(x) + len(taps) - 1
        if min(len(x), len(taps)) < 64:
            return np.convolve(x, taps)
        size = 1 << (n - 1).bit_length()
        real = np.fft.irfft(np.fft.rfft(x.astype(np.float64), size) * np.fft.rfft(taps.astype(np.float64), size),
                            size)[:n]
        out = np.rint(real).astype(np.int64)
        if float(np.abs(real - out).max()) < 1 / 64 and _sums(np, out) == _product_sums(np, x, taps):
            return out
        return np.convolve(x, taps)

    def pink(self, n: int, seed: int, rms: int) -> Any:
        np = self.np
        total = np.zeros(n, dtype=np.int64)
        u = np.uint64
        for row in range(PINK_ROWS):
            held = 1 << row
            count = (n + held - 1) // held
            z = u(_mix(seed + row)) + np.arange(count, dtype=np.uint64) * u(_GAMMA)
            z = (z ^ (z >> u(30))) * u(_MIX_1)
            z = (z ^ (z >> u(27))) * u(_MIX_2)
            z = z ^ (z >> u(31))
            values = (z >> u(48)).astype(np.int64) - 32768
            total += np.repeat(values, held)[:n]
        return (total * rms + PINK_SPREAD // 2) // PINK_SPREAD

    def to_pcm(self, x: Any) -> tuple[bytes, int]:
        np = self.np
        x = np.asarray(x, dtype=np.int64)
        clipped = int(((x > 32767) | (x < -32768)).sum())
        return np.clip(x, -32768, 32767).astype("<i2").tobytes(), clipped


def _sums(np: Any, y: Any) -> tuple[int, int]:
    signs = 1 - 2 * (np.arange(len(y), dtype=np.int64) & 1)
    return int(y.sum()), int((y * signs).sum())


def _product_sums(np: Any, x: Any, taps: Any) -> tuple[int, int]:
    xs, ts = _sums(np, x), _sums(np, taps)
    return xs[0] * ts[0], xs[1] * ts[1]


def backend(plain: bool = False) -> Any:
    """The arithmetic to use: numpy's when numpy is here, plain Python's
    otherwise or when asked. Both give the same integers."""
    if not plain:
        try:
            import numpy
        except ImportError:
            pass
        else:
            return _Fast(numpy)
    return _Plain()


def resample(x: Any, source_rate: int, rate: int = RATE, *, be: Any = None) -> Any:
    """`x` at `rate`: a windowed-sinc resampler in whole numbers."""
    be = be or backend()
    if source_rate == rate:
        return x
    g = math.gcd(source_rate, rate)
    return be.resample(x, rate // g, source_rate // g)


# ================================================================== rooms


def _axis_images(length: float, source: float, microphone: float, reach: float) -> list[tuple[float, int]]:
    """Along one axis: each image's squared offset from the microphone and
    how many walls it bounced off, for every image within reach."""
    out = []
    most = int(reach / (2.0 * length)) + 2
    for n in range(-most, most + 1):
        for position, bounces in ((2 * n * length + source, abs(2 * n)), (2 * n * length - source, abs(2 * n - 1))):
            offset = position - microphone
            if abs(offset) <= reach:
                out.append((offset * offset, bounces))
    return out


def distance_m(source: Sequence[float], microphone: Sequence[float]) -> float:
    """From the talker to the microphone, in metres."""
    total = 0.0
    for s, m in zip(source, microphone):
        total += (float(s) - float(m)) * (float(s) - float(m))
    return math.sqrt(total)


def room_response(size: Sequence[float], source: Sequence[float], microphone: Sequence[float],
                  absorption: float, length_s: float, gain_at_1m_q15: int, speed: float = 343.0,
                  rate: int = RATE) -> list[int]:
    """A shoebox room's response from `source` to `microphone`, as Q15 taps.

    The image method (Allen and Berkley, 1979): the walls are mirrors, each
    image of the talker that arrives within `length_s` is one tap, at the
    nearest sample, of size gain / distance * reflection ** bounces, where
    the reflection is sqrt(1 - absorption) for every wall. The arithmetic is
    square roots and the four operations in one fixed order, each image is
    rounded to a whole number at 2**30 before it is added, and the sum is
    rounded to Q15, so the taps are the same on every machine."""
    for axis, (length, s, m) in enumerate(zip(size, source, microphone)):
        if not (0.0 < float(s) < float(length) and 0.0 < float(m) < float(length)):
            raise GenerateError(f"axis {axis}: the talker ({s} m) and the microphone ({m} m) must stand inside "
                                f"the room ({length} m)")
    if not 0.0 < float(absorption) < 1.0:
        raise GenerateError(f"a wall's absorption is between 0 and 1, and this is {absorption}")
    if distance_m(source, microphone) < 0.1:
        raise GenerateError("the talker is closer than 0.1 m to the microphone")
    beta = math.sqrt(1.0 - float(absorption))
    count = int(float(length_s) * rate)
    reach = count * speed / rate
    reach2 = reach * reach
    axes = [_axis_images(float(length), float(s), float(m), reach)
            for length, s, m in zip(size, source, microphone)]
    powers = [1.0]
    for _ in range(sum(max((b for _d, b in axis), default=0) for axis in axes)):
        powers.append(powers[-1] * beta)
    scale = float(gain_at_1m_q15) * ONE
    fine = [0] * (count + 1)
    for dx2, bx in axes[0]:
        for dy2, by in axes[1]:
            dxy2, bxy = dx2 + dy2, bx + by
            if dxy2 > reach2:
                continue
            for dz2, bz in axes[2]:
                d2 = dxy2 + dz2
                if d2 > reach2:
                    continue
                distance = math.sqrt(d2)
                fine[int(distance * rate / speed + 0.5)] += int(scale * powers[bxy + bz] / distance + 0.5)
    taps = [(v + HALF) >> Q for v in fine[:count]]
    while taps and taps[-1] == 0:
        taps.pop()
    return taps


def decay_ms(taps: Sequence[int], rate: int = RATE) -> int | None:
    """How long a response takes to fall 60 dB, in ms, read from its own
    taps: Schroeder's backward integral (J. Acoust. Soc. Am. 37, 1965) from
    5 dB down to 25 dB down, times three. Whole numbers throughout, the two
    levels as 10**-0.5 and 10**-2.5 to seven places. None when the response
    is too short to fall 25 dB."""
    energy = [v * v for v in taps]
    total = tail = sum(energy)
    if total == 0:
        return None
    first = last = None
    for i, e in enumerate(energy):
        if first is None and tail * 10_000_000 <= total * 3_162_278:
            first = i
        if tail * 10_000_000 <= total * 31_623:
            last = i
            break
        tail -= e
    if first is None or last is None:
        return None
    return (3 * (last - first) * 1000 + rate // 2) // rate


# ============================================================= the inputs

INPUTS = {
    "voices": "emet-generated-voices/1",
    "rooms": "emet-generated-rooms/1",
    "phrases": "emet-generated-phrases/1",
    "sets": "emet-generated-sets/1",
}

#: A question that starts with one of these is given a question mark.
QUESTION_WORDS = ("what", "what's", "where", "is", "are", "can", "how", "why", "should", "do", "did", "which", "who")

RULES = ("every-cell", "one-a-condition")


def text_sha256(path: Path) -> str:
    """A text file's sha256 with its line ends as LF, so a checkout that
    writes CRLF names the same inputs."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GenerateError(f"{path}: not readable JSON: {exc}") from None


def input_problems(inputs: dict[str, Any]) -> list[str]:
    """Every problem with the room, phrase and set lists; the voice list is
    `check.voice_list_problems`'s."""
    out: list[str] = []
    rooms, phrases, sets = inputs["rooms"], inputs["phrases"], inputs["sets"]
    for rid, room in (rooms.get("rooms") or {}).items():
        size = room.get("size_m")
        if not (isinstance(room.get("description"), str) and isinstance(size, list) and len(size) == 3
                and all(isinstance(v, (int, float)) and v > 0 for v in size)
                and isinstance(room.get("absorption"), (int, float)) and 0 < room["absorption"] < 1
                and isinstance(room.get("length_s"), (int, float)) and room["length_s"] > 0):
            out.append(f"rooms: {rid} needs a description, size_m of three lengths, an absorption between 0 and 1 "
                       "and a length_s")
    seen: set[str] = set()
    for cond in rooms.get("conditions") or []:
        cid = str(cond.get("id"))
        if cid in seen or not bank_check.ID.match(cid):
            out.append(f"rooms: condition {cid!r} is an id, used once")
        seen.add(cid)
        if cond.get("room") not in (rooms.get("rooms") or {}):
            out.append(f"rooms: condition {cid} names room {cond.get('room')!r}, which the list does not have")
        for key in ("microphone_m", "source_m"):
            if not (isinstance(cond.get(key), list) and len(cond[key]) == 3):
                out.append(f"rooms: condition {cid} gives {key} as three metres")
        if not isinstance(cond.get("distance"), str):
            out.append(f"rooms: condition {cid} says its distance in words")
        out.extend(f"rooms: condition {cid}: {p}" for p in bank_check.noise_problems(cond.get("noise")))
    if not seen:
        out.append("rooms: no condition is listed")
    if not isinstance(rooms.get("gain_at_1m_q15"), int) or not 0 < rooms["gain_at_1m_q15"] <= ONE:
        out.append("rooms: gain_at_1m_q15 is a whole number from 1 to 32768")
    ids: set[str] = set()
    for group in phrases.get("groups") or []:
        gid = group.get("id")
        if group.get("kind") not in bank_files.TAKE_KINDS:
            out.append(f"phrases: group {gid}'s kind is one of the bank's take kinds")
        if not isinstance(group.get("slot_s"), (int, float)) or group["slot_s"] <= 0:
            out.append(f"phrases: group {gid} gives each take a slot_s above 0")
        if not isinstance(group.get("source"), str) or not group["source"]:
            out.append(f"phrases: group {gid} says where in the repository its texts are")
        for phrase in group.get("phrases") or []:
            pid = phrase.get("id")
            if pid in ids or not isinstance(pid, str):
                out.append(f"phrases: the id {pid!r} is used twice or is no text")
            ids.add(pid)
            if not isinstance(phrase.get("text"), str) or not phrase["text"].strip():
                out.append(f"phrases: {pid} has no text")
            if phrase.get("ends") not in (None, ".", "?", "!"):
                out.append(f"phrases: {pid}'s ends is one of . ? !")
    if not ids:
        out.append("phrases: no phrase is listed")
    if not isinstance(sets.get("seed"), int):
        out.append("sets: seed is a whole number")
    if not isinstance(sets.get("parts"), int) or sets.get("parts", 0) < 1:
        out.append("sets: parts is a whole number, 1 or more")
    if not isinstance(sets.get("version"), str) or not bank_check.DATE.match(sets.get("version") or ""):
        out.append("sets: version is the bank's date, YYYY-MM-DD")
    scales = sets.get("length_scales")
    if not isinstance(scales, list) or not scales or not all(isinstance(v, (int, float)) and v > 0 for v in scales):
        out.append("sets: length_scales lists at least one speaking rate above 0")
    if not isinstance(sets.get("lead_s"), (int, float)) or sets.get("lead_s", 0) < 5:
        out.append("sets: lead_s is at least 5 s: the wake decoder's mean settles about 3 s in")
    for name, rule in (sets.get("sets") or {}).items():
        if not isinstance(rule, dict) or rule.get("rule") not in RULES:
            out.append(f"sets: {name}'s rule is one of {', '.join(RULES)}")
    if not sets.get("sets"):
        out.append("sets: no set is listed")
    return out


def load_inputs(data: Path = DATA) -> dict[str, Any]:
    """The four lists, each checked. A list with a problem builds nothing."""
    inputs: dict[str, Any] = {}
    for name, fmt in INPUTS.items():
        doc = _read_json(Path(data) / f"{name}.json")
        if not isinstance(doc, dict) or doc.get("format") != fmt:
            raise GenerateError(f"{Path(data) / (name + '.json')}: its format is not {fmt!r}")
        inputs[name] = doc
    inputs["sha256"] = {name: text_sha256(Path(data) / f"{name}.json") for name in INPUTS}
    found = bank_check.voice_list_problems(inputs["voices"]) + input_problems(inputs)
    if found:
        raise GenerateError("the lists under generated/ are unsound: " + "; ".join(found))
    return inputs


def spoken(piece: str, end: str, say_as: dict[str, str]) -> str:
    """A piece of a phrase as the voice is given it: a capital first letter,
    an end mark unless it has one, and any word of `say_as` respelt."""
    words = []
    for word in piece.split():
        bare = word.strip(",.?!")
        swap = say_as.get(bare.lower())
        words.append(word if swap is None else word.replace(bare, swap))
    piece = " ".join(words)
    piece = piece[0].upper() + piece[1:]
    return piece if piece[-1] in ".?!" else piece + end


def segments(group: dict[str, Any], phrase: dict[str, Any], say_as: dict[str, str]) -> list[dict[str, Any]]:
    """What a take is made of, in order: pieces to say and pauses between."""
    pieces = [p for p in phrase["text"].split(" ... ") if p.strip()]
    asked = (phrase.get("question") or phrase["text"]).split()
    last = phrase.get("ends") or ("?" if asked and asked[0].lower() in QUESTION_WORDS else ".")
    between = "," if group["kind"] == "pause" else "."
    pause = float(phrase.get("pause_s", group.get("pause_s", 0.7)))
    out: list[dict[str, Any]] = []
    for n, piece in enumerate(pieces):
        if n:
            out.append({"pause_s": pause})
        out.append({"say": spoken(piece, last if n == len(pieces) - 1 else between, say_as)})
    return out


def numbered_phrases(inputs: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every phrase with its group, in the list's order: a phrase's number
    is its place here, from 0."""
    return [(group, phrase) for group in inputs["phrases"]["groups"] for phrase in group["phrases"]]


def cells(inputs: dict[str, Any], set_name: str) -> list[tuple[int, int, int]]:
    """The recordings of a set, each as (voice, condition, part) numbers.

    `every-cell` is all of them. `one-a-condition` is one a condition, the
    part and the voice stepping with it, so it is a subset of the other."""
    sets = inputs["sets"]
    if set_name not in sets["sets"]:
        raise GenerateError(f"no set is called {set_name!r}; the list has {', '.join(sets['sets'])}")
    voices, conditions, parts = len(inputs["voices"]["voices"]), len(inputs["rooms"]["conditions"]), sets["parts"]
    if sets["sets"][set_name]["rule"] == "one-a-condition":
        return [(j % voices, j, j % parts) for j in range(conditions)]
    return [(i, j, k) for i in range(voices) for j in range(conditions) for k in range(parts)]


def plan(inputs: dict[str, Any], set_name: str) -> list[dict[str, Any]]:
    """Each recording of a set and its takes, from the lists and nothing else."""
    sets, say_as = inputs["sets"], inputs["phrases"].get("say_as") or {}
    voices, conditions = inputs["voices"]["voices"], inputs["rooms"]["conditions"]
    scales, parts = sets["length_scales"], sets["parts"]
    phrases = numbered_phrases(inputs)
    out = []
    for i, j, k in cells(inputs, set_name):
        takes = []
        for number, (group, phrase) in enumerate(phrases):
            if number % parts != k:
                continue
            takes.append({
                "phrase": phrase["id"], "kind": group["kind"], "text": phrase["text"],
                "question": phrase.get("question"), "first_sound": phrase.get("first_sound"),
                "expect_wake": group["kind"] not in ("decoy", "talk"),
                "slot_s": float(group["slot_s"]),
                "length_scale": scales[(number + i + j) % len(scales)],
                "segments": segments(group, phrase, say_as),
            })
        out.append({"id": f"gen-{voices[i]['id']}-{conditions[j]['id']}-{k + 1}", "voice": voices[i],
                    "condition": conditions[j], "part": k + 1, "takes": takes})
    return out


def plan_seconds(inputs: dict[str, Any], recording: dict[str, Any]) -> float:
    return float(inputs["sets"]["lead_s"]) + sum(t["slot_s"] for t in recording["takes"])


# ============================================================== the voices


def verify_voices(inputs: dict[str, Any], voices_dir: Path, names: Sequence[str] | None = None) -> dict[str, Any]:
    """Each voice's files, held to the list: {name: {"model", "config", "card"}}.

    A model or a config whose sha256 is not the pinned one is refused, and
    so is a card that is not the pinned card or whose Training section no
    longer names the base the list records. `names` limits the check to the
    voices a set uses."""
    found: dict[str, Any] = {}
    refused: list[str] = []
    fetchable = False
    for voice in inputs["voices"]["voices"]:
        if names is not None and voice["name"] not in names:
            continue
        files = voice["files"]
        paths = {key: Path(voices_dir) / files[key]["name"] for key in ("model", "config", "card")}
        for key, path in paths.items():
            if not path.is_file():
                refused.append(f"{voice['name']}: no {path.name} in {voices_dir}")
                fetchable = True
                continue
            got = bank_files.sha256_file(path)
            if got != files[key]["sha256"]:
                refused.append(f"{voice['name']}: {path.name} has sha256 {got[:12]}..., and the list pins "
                               f"{files[key]['sha256'][:12]}...: it is not the file that was read and checked")
                fetchable = True
        if paths["card"].is_file():
            card = paths["card"].read_text(encoding="utf-8")
            refused.extend(f"{voice['name']}: {p}" for p in bank_check.card_problems(card, voice))
            found[voice["name"]] = {"model": paths["model"], "config": paths["config"], "card": card}
    if refused:
        fix = (f"\n  fetch the pinned files with: python tools/bank/generate.py --fetch-voices "
               f"{Path(voices_dir).as_posix()}" if fetchable else "")
        raise GenerateError("the voices are not the pinned ones:\n  " + "\n  ".join(refused) + fix)
    return found


def fetch_voices(inputs: dict[str, Any], voices_dir: Path, say: Callable[[str], None] = print) -> None:
    """Download each listed voice's three files from the pinned revision
    into `voices_dir`, skipping a file already there with the pinned hash."""
    voices_dir = Path(voices_dir)
    voices_dir.mkdir(parents=True, exist_ok=True)
    for voice in inputs["voices"]["voices"]:
        for key in ("model", "config", "card"):
            entry = voice["files"][key]
            path = voices_dir / entry["name"]
            if path.is_file() and bank_files.sha256_file(path) == entry["sha256"]:
                say(f"{entry['name']}: here already")
                continue
            say(f"{entry['name']}: fetching {entry['bytes']} bytes")
            _download(entry["url"], path, entry["sha256"])
    verify_voices(inputs, voices_dir)
    say(f"{len(inputs['voices']['voices'])} voice(s) in {voices_dir}, each the pinned file")


def _download(url: str, path: Path, sha256: str) -> None:
    """`url` to `path`, kept only when its sha256 is the one given."""
    part = path.with_name(path.name + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "emet-generate"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, open(part, "wb") as fh:
            for block in iter(lambda: response.read(1 << 20), b""):
                digest.update(block)
                fh.write(block)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise GenerateError(f"{url}: {exc}") from None
    if digest.hexdigest() != sha256.lower():
        part.unlink(missing_ok=True)
        raise GenerateError(f"{url}: its sha256 is {digest.hexdigest()}, and {sha256.lower()} was asked for; "
                            "nothing was kept")
    part.replace(path)


class PiperTool:
    """Piper as a child process: `PYTHON -m piper`, or the `piper` program.

    Nothing of Piper's is imported. One process says a batch of lines for
    one voice at one rate, both noise scales at 0, and writes one wav a
    line into a new folder, named by a rising clock; the lines come back in
    the order given.

    Every child has a bound, so a Piper that stalls (a model load waiting
    on a lock, a damaged venv) stops the build with the voice and the rate
    named: `VERSIONS_S` to say its versions, and for a batch
    `BATCH_S + LINE_S` a line, times the speaking rate when it is slower
    than 1."""

    VERSIONS_S = 60.0
    BATCH_S = 30.0
    LINE_S = 5.0

    def __init__(self, program: str, voices: dict[str, Any], sentence_silence_s: float = 0.2) -> None:
        self.program = str(program)
        stem = Path(self.program).stem.lower()
        self.command = [self.program, "-m", "piper"] if stem.startswith("py") else [self.program]
        self.voices = voices
        self.sentence_silence_s = float(sentence_silence_s)

    def bound_s(self, length_scale: float, lines: int) -> float:
        """How long one batch may take before it is stopped."""
        return (self.BATCH_S + self.LINE_S * lines) * max(1.0, float(length_scale))

    def interpreter(self) -> str | None:
        """The Python that has Piper: the program itself, or the one beside
        a `piper` program in its venv."""
        if len(self.command) == 3:
            return self.program
        for name in ("python.exe", "python", "python3"):
            beside = Path(self.program).with_name(name)
            if beside.is_file():
                return str(beside)
        return None

    def versions(self) -> dict[str, str]:
        """The versions of piper-tts and onnxruntime, asked of that Python."""
        python = self.interpreter()
        if python is None:
            raise GenerateError(f"{self.program}: no Python beside it to ask Piper's version of; give --piper "
                                "the interpreter of the venv that holds piper-tts")
        code = ("import json; from importlib.metadata import version; "
                "print(json.dumps({n: version(n) for n in ('piper-tts', 'onnxruntime')}))")
        try:
            done = subprocess.run([python, "-c", code], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.VERSIONS_S)
        except subprocess.TimeoutExpired:
            raise GenerateError(f"{python} did not say Piper's version within {self.VERSIONS_S:g} s; "
                                "check that venv") from None
        if done.returncode != 0:
            raise GenerateError(f"{python}: piper-tts is not installed there: {done.stderr.strip()[-300:]}")
        return json.loads(done.stdout)

    def arguments(self, voice: dict[str, Any], length_scale: float) -> list[str]:
        files = self.voices[voice["name"]]
        return ["-m", str(files["model"]), "-c", str(files["config"]), "--length-scale", repr(float(length_scale)),
                "--noise-scale", "0", "--noise-w-scale", "0", "--sentence-silence", repr(self.sentence_silence_s),
                "--output-dir-naming", "timestamp"]

    def __call__(self, voice: dict[str, Any], length_scale: float, texts: Sequence[str]) -> list[tuple[int, bytes]]:
        with tempfile.TemporaryDirectory(prefix="emet-piper-") as scratch:
            lines, out = Path(scratch) / "lines.txt", Path(scratch) / "out"
            lines.write_text("".join(text + "\n" for text in texts), encoding="utf-8", newline="\n")
            bound = self.bound_s(length_scale, len(texts))
            try:
                done = subprocess.run([*self.command, *self.arguments(voice, length_scale), "-i", str(lines),
                                       "-d", str(out)], capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", stdin=subprocess.DEVNULL, timeout=bound)
            except subprocess.TimeoutExpired:
                raise GenerateError(f"piper did not finish {len(texts)} line(s) of {voice['name']} at length scale "
                                    f"{float(length_scale):g} within {bound:g} s, so it was stopped") from None
            if done.returncode != 0:
                raise GenerateError(f"piper exited {done.returncode} on {voice['name']}: "
                                    f"{(done.stderr or '').strip()[-600:]}")
            wavs = sorted(out.glob("*.wav"), key=lambda p: int(p.stem))
            if len(wavs) != len(texts):
                raise GenerateError(f"piper wrote {len(wavs)} file(s) for {len(texts)} line(s) of {voice['name']}")
            return [read_wav(path) for path in wavs]


def read_wav(path: Path) -> tuple[int, bytes]:
    """(rate, samples) of a 16-bit mono wav."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise GenerateError(f"{path}: {w.getnchannels()} channel(s) of {8 * w.getsampwidth()} bits; "
                                "a voice gives 16-bit mono")
        return w.getframerate(), w.readframes(w.getnframes())


def write_wav(path: Path, pcm: bytes, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class SynthCache:
    """What the voice said, kept by what it was asked: so the full set does
    not say again what the small set said, and a rebuild can start from the
    utterances a build kept."""

    def __init__(self, folder: Path | None, engine: dict[str, Any]) -> None:
        self.folder = Path(folder) if folder else None
        self.engine = engine
        self.memory: dict[str, dict[str, Any]] = {}
        self.index: dict[str, Any] = {}
        if self.folder and (self.folder / "index.json").is_file():
            self.index = _read_json(self.folder / "index.json")

    def key(self, voice: dict[str, Any], length_scale: float, text: str) -> str:
        asked = [voice["files"]["model"]["sha256"], voice["files"]["config"]["sha256"], self.engine.get("name"),
                 self.engine.get("version"), self.engine.get("runtime"), repr(float(length_scale)),
                 self.engine.get("sentence_silence_s"), text]
        return hashlib.sha256(json.dumps(asked, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    def get(self, voice: dict[str, Any], length_scale: float, text: str) -> dict[str, Any] | None:
        key = self.key(voice, length_scale, text)
        if key in self.memory:
            return self.memory[key]
        entry = self.index.get(key)
        if entry and self.folder and (self.folder / f"{key}.wav").is_file():
            rate, pcm = read_wav(self.folder / f"{key}.wav")
            if hashlib.sha256(pcm).hexdigest() == entry["sha256"] and rate == entry["sample_rate"]:
                self.memory[key] = {"rate": rate, "pcm": pcm, "sha256": entry["sha256"]}
                return self.memory[key]
        return None

    def put(self, voice: dict[str, Any], length_scale: float, text: str, rate: int, pcm: bytes) -> None:
        key = self.key(voice, length_scale, text)
        entry = {"rate": rate, "pcm": pcm, "sha256": hashlib.sha256(pcm).hexdigest()}
        self.memory[key] = entry
        if self.folder:
            write_wav(self.folder / f"{key}.wav", pcm, rate)
            self.index[key] = {"voice": voice["name"], "length_scale": float(length_scale), "text": text,
                               "sample_rate": rate, "frames": len(pcm) // 2, "sha256": entry["sha256"]}

    def save(self) -> None:
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / "index.json").write_text(json.dumps(self.index, indent=1, ensure_ascii=False,
                                                               sort_keys=True) + "\n", encoding="utf-8", newline="\n")


# ================================================================ the build


def _tools(be: Any) -> dict[str, Any]:
    import platform

    return {"python": platform.python_version(), "numpy": getattr(getattr(be, "np", None), "__version__", None),
            "platform": f"{sys.platform}-{platform.machine().lower()}"}


#: The wake engine's frame, in samples at the bank's rate: 80 ms.
FRAME_SAMPLES = 1280


def build(out: Path, set_name: str, inputs: dict[str, Any],
          synthesise: Callable[[dict[str, Any], float, Sequence[str]], list[tuple[int, bytes]]], *,
          engine: dict[str, Any], cards: dict[str, str], synth_dir: Path | None = None, be: Any = None,
          say: Callable[[str], None] = print) -> dict[str, Any]:
    """Build one set as a bank in `out`, a new folder, and check it.

    `synthesise(voice, length_scale, texts)` gives (rate, 16-bit samples)
    for each text, in order; `cards` holds each voice's MODEL_CARD by name;
    `engine` says what spoke, for the record. Everything else comes from
    `inputs`. Returns the counts."""
    be = be or backend()
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise GenerateError(f"{out} is not empty: a bank is built into a new folder and never over one")
    recordings = plan(inputs, set_name)
    sets, rooms = inputs["sets"], inputs["rooms"]
    seed, lead = sets["seed"], int(round(float(sets["lead_s"]) * RATE))
    engine = {**engine, "noise_scale": 0, "noise_w_scale": 0,
              "sentence_silence_s": float(sets.get("sentence_silence_s", 0.2))}
    cache = SynthCache(synth_dir, engine)

    # What each voice must say at each rate, once, in the order first met.
    wanted: dict[tuple[str, float], dict[str, None]] = {}
    by_name = {rec["voice"]["name"]: rec["voice"] for rec in recordings}
    for rec in recordings:
        for take in rec["takes"]:
            for seg in take["segments"]:
                if "say" in seg:
                    wanted.setdefault((rec["voice"]["name"], take["length_scale"]), {})[seg["say"]] = None
    for (name, scale), texts in wanted.items():
        missing = [t for t in texts if cache.get(by_name[name], scale, t) is None]
        if missing:
            say(f"{name} at {scale:g}: saying {len(missing)} of {len(texts)} line(s)")
            said = synthesise(by_name[name], scale, missing)
            if len(said) != len(missing):
                raise GenerateError(f"{name}: {len(said)} utterance(s) came back for {len(missing)} line(s)")
            for text, (rate, pcm) in zip(missing, said):
                if not pcm:
                    raise GenerateError(f"{name} said nothing for {text!r}")
                cache.put(by_name[name], scale, text, rate, pcm)
            cache.save()

    responses: dict[str, dict[str, Any]] = {}
    at_rate: dict[tuple[str, float, str], Any] = {}
    files, total_takes, total_frames = [], 0, 0
    phases: set[int] = set()  # where the takes fall in the wake engine's frame
    for rec in recordings:
        voice, cond = rec["voice"], rec["condition"]
        if cond["id"] not in responses:
            room = rooms["rooms"][cond["room"]]
            taps = room_response(room["size_m"], cond["source_m"], cond["microphone_m"], room["absorption"],
                                 room["length_s"], rooms["gain_at_1m_q15"], float(rooms["speed_of_sound_m_s"]))
            responses[cond["id"]] = {
                "taps": taps,
                "record": {
                    "id": cond["room"], "condition": cond["id"], "size_m": room["size_m"],
                    "absorption": room["absorption"], "length_s": room["length_s"],
                    "source_m": cond["source_m"], "microphone_m": cond["microphone_m"],
                    "distance_m": round(distance_m(cond["source_m"], cond["microphone_m"]), 3),
                    "speed_of_sound_m_s": rooms["speed_of_sound_m_s"], "gain_at_1m_q15": rooms["gain_at_1m_q15"],
                    "taps": len(taps), "decay_ms": decay_ms(taps),
                    "taps_sha256": hashlib.sha256(json.dumps(taps).encode("ascii")).hexdigest(),
                },
            }
        taps = responses[cond["id"]]["taps"]
        frames = lead + sum(int(round(t["slot_s"] * RATE)) for t in rec["takes"])
        mix = be.zeros(frames)
        cursor, takes, made = lead, [], {}
        for n, take in enumerate(rec["takes"], 1):
            take_id = f"{rec['id']}/{n:02d}"
            slot = int(round(take["slot_s"] * RATE))
            parts, record = [], []
            for seg in take["segments"]:
                if "pause_s" in seg:
                    parts.append(be.zeros(int(round(seg["pause_s"] * RATE))))
                    record.append({"pause_s": seg["pause_s"]})
                    continue
                key = (voice["name"], take["length_scale"], seg["say"])
                dry = cache.get(voice, take["length_scale"], seg["say"])
                if key not in at_rate:
                    at_rate[key] = resample(be.from_pcm(dry["pcm"]), dry["rate"], RATE, be=be)
                parts.append(at_rate[key])
                record.append({"say": seg["say"], "sha256": dry["sha256"], "sample_rate": dry["rate"],
                               "frames": len(dry["pcm"]) // 2})
            wet = be.convolve(be.join(parts), taps)
            place = cursor + int(TAKE_OFFSET_S * RATE) + derive_seed(seed, take_id, "place") % int(TAKE_JITTER_S * RATE)
            if place + be.length(wet) > cursor + slot:
                raise GenerateError(f"{take_id} ({take['phrase']}) runs {be.length(wet) / RATE:.1f} s from "
                                    f"{(place - cursor) / RATE:.2f} s into a slot of {take['slot_s']:g} s: give its "
                                    "group a longer slot_s")
            be.add_at(mix, place, wet)
            takes.append({"id": take_id, "kind": take["kind"], "text": take["text"], "question": take["question"],
                          "first_sound": take["first_sound"], "slot": {"start": cursor, "end": cursor + slot},
                          "expect_wake": take["expect_wake"], "retake_of": None, "exclude": None})
            made[take_id] = {"phrase": take["phrase"], "length_scale": take["length_scale"], "said": record,
                             "at_sample": place, "samples": be.length(wet)}
            cursor += slot
        noise = cond.get("noise") or None
        noise_seed = derive_seed(seed, rec["id"], "noise")
        if noise and noise.get("rms"):
            be.add_at(mix, 0, be.pink(frames, noise_seed, int(noise["rms"])))
        pcm, clipped = be.to_pcm(mix)
        wav = out / "audio" / f"{rec['id']}.wav"
        write_wav(wav, pcm)
        slots = {t["slot_s"] for t in rec["takes"]}
        doc = {
            "format": bank_files.FORMATS["manifest"], "id": rec["id"], "path": f"audio/{rec['id']}.wav",
            "sha256": bank_files.sha256_file(wav), "sample_rate": RATE, "channels": 1, "bits": 16,
            "frames": frames, "duration_s": round(frames / RATE, 6), "recorded": sets["version"],
            "start_time": None, "device": None, "capture_level": None, "room": cond["room"],
            "position": f"simulated, condition {cond['id']}", "distance": cond["distance"],
            "distance_m": responses[cond["id"]]["record"]["distance_m"], "facing": None,
            "speaker": voice["id"], "noise": bank_check.noise_words(noise), "kind": "takes",
            "lead_s": float(sets["lead_s"]), "prompt_every_s": slots.pop() if len(slots) == 1 else None,
            "made_with": "tools/bank/generate.py", "takes": takes,
            "generated": {
                "format": bank_check.GENERATED_FORMAT, "seed": seed, "part": rec["part"],
                "voice": {"name": voice["name"], **{f"{k}_sha256": voice["files"][k]["sha256"]
                                                     for k in ("model", "config", "card")}},
                "engine": engine, "room": responses[cond["id"]]["record"],
                "noise": {**noise, "seed": noise_seed} if noise and noise.get("rms") else None,
                "resampler": {"taps": RESAMPLE_TAPS, "pass": f"{RESAMPLE_PASS[0]}/{RESAMPLE_PASS[1]}"},
                "clipped": clipped, "inputs": inputs["sha256"], "builder_sha256": text_sha256(Path(__file__)),
                "tools": _tools(be), "takes": made,
            },
        }
        bank_files.write(out / "manifests" / f"{rec['id']}.json", doc, "manifest")
        files.append(f"manifests/{rec['id']}.json")
        total_takes += len(takes)
        total_frames += frames
        phases.update(record["at_sample"] % FRAME_SAMPLES for record in made.values())
        say(f"{rec['id']}: {len(takes)} take(s), {frames / RATE:g} s"
            + (f", {clipped} sample(s) clipped" if clipped else ""))
        if len(at_rate) > 4000:
            at_rate.clear()

    used_rooms = {rec["condition"]["room"] for rec in recordings}
    used_voices = {rec["voice"]["id"]: rec["voice"] for rec in recordings}
    bank = {
        "format": bank_files.FORMATS["bank"], "version": sets["version"], "files": files,
        "rooms": {rid: {"description": room["description"], "size_m": room["size_m"],
                        "absorption": room["absorption"]}
                  for rid, room in rooms["rooms"].items() if rid in used_rooms},
        "speakers": {vid: {"voice": f"voices/{vid}.json"} for vid in sorted(used_voices)},
    }
    bank_files.write(out / "bank.json", bank, "bank")
    for vid, voice in sorted(used_voices.items()):
        record = bank_check.voice_record(voice, inputs["voices"], engine)
        (out / "voices").mkdir(parents=True, exist_ok=True)
        (out / "voices" / f"{vid}.json").write_text(bank_files.dump(record), encoding="utf-8", newline="\n")
        (out / "voices" / voice["files"]["card"]["name"]).write_bytes(cards[voice["name"]].encode("utf-8"))
    summary = {"set": set_name, "recordings": len(files), "takes": total_takes,
               "seconds": round(total_frames / RATE, 3), "arithmetic": be.name, "phases": len(phases)}
    facts = {"summary": summary, "engine": engine, "tools": _tools(be),
             "rooms": {cid: r["record"] for cid, r in responses.items()}}
    (out / "DATASHEET.md").write_text(datasheet(inputs, recordings, facts), encoding="utf-8", newline="\n")
    (out / "ATTRIBUTION.md").write_text(attribution(inputs, used_voices, engine), encoding="utf-8", newline="\n")
    write_sums(out)
    lines, _good, bad = bank_check.check(out, [], False, voices=inputs["voices"])
    if bad:
        raise GenerateError("the bank just built does not pass check.py:\n  " + "\n  ".join(lines))
    say(lines[-1])
    return summary


def write_sums(root: Path) -> Path:
    """`SHA256SUMS`: every file of the bank but itself, as `sha256sum -c`
    reads it."""
    root = Path(root)
    rows = [f"{bank_files.sha256_file(p)}  {p.relative_to(root).as_posix()}"
            for p in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix())
            if p.is_file() and p.name != bank_check.SUMS]
    path = root / bank_check.SUMS
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


def sums_again(root: Path, say: Callable[[str], None] = print, voices: dict[str, Any] | None = None) -> Path:
    """`--sums BANK`: a built bank's `SHA256SUMS` written again after a file
    was added to it, such as the store's cassettes under `cassettes/`.

    Each file under `cassettes/` must read as a cassette (FORMAT.md) before
    the sums are written, and the bank must pass `check.py` against
    `voices` (None: the pinned list) after, or it is refused."""
    root = Path(root).resolve()
    if not (root / "bank.json").is_file():
        raise GenerateError(f"{root} holds no bank.json, so it is no bank")
    refused = []
    for path in sorted((root / "cassettes").rglob("*")) if (root / "cassettes").is_dir() else []:
        if path.is_file():
            try:
                bank_files.read(path, "cassette")
            except bank_files.BankError as exc:
                refused.append(f"{path.relative_to(root).as_posix()}: {'; '.join(exc.problems)}")
    if refused:
        raise GenerateError("a file under cassettes/ is not a cassette, so the sums were not written:\n  "
                            + "\n  ".join(refused))
    path = write_sums(root)
    lines, _good, bad = bank_check.check(root, [], False, voices=voices)
    if bad:
        raise GenerateError(f"{path} written, and the bank does not pass check.py:\n  " + "\n  ".join(lines))
    say(f"{path}: written; {lines[-1]}")
    return path


# ============================================================ the documents

#: What generated speech in a simulated room may and may not decide
#: (research/references-2026-10-26/verification-design.md 5.2), as the
#: datasheet and FORMAT.md say it.
MAY = (
    "flag a regression: a golden that moved says look here, and the pull request says why",
    "find a counterexample to a rule, which then goes to the real bank",
    "exercise code paths no person recorded: every first sound, numbers, long turns, near misses",
    "screen a candidate threshold or rule before the real bank is spent on it",
)
MAY_NOT = (
    "set or move a default",
    "give a recall or false-wake rate for a real room: a simulated room's numbers lean easy",
    "fire the roadmap's wake-threshold trigger: a synthetic voice is no second voice, and a simulated "
    "room is no second room",
    "count as a run on a body (RELEASING.md section 6)",
    "stand in for the enrolled voices of 0.7",
)

#: Where a rebuild's commands are written, so the datasheet names no
#: machine's own paths.
#: What BANK stands for in the two blocks below.
BANK_IS = ("# BANK is this set's folder, the one that holds this datasheet "
           "(genbank-{set}-{version} once --fetch unpacks it).")

REBUILD = """\
```sh
# From the Emet repository's emet/ folder, at a commit whose
# tools/bank/generate.py hashes to the builder_sha256 above. On Windows the
# venvs' programs are under Scripts\\ in place of bin/.
{bank_is}
python -m venv ../genbank-piper && ../genbank-piper/bin/python -m pip install piper-tts=={piper} onnxruntime=={ort}
python -m venv ../genbank-tools && ../genbank-tools/bin/python -m pip install numpy=={numpy}
../genbank-tools/bin/python tools/bank/generate.py --fetch-voices ../genbank-voices
../genbank-tools/bin/python tools/bank/generate.py ../genbank-rebuilt/{set} --set {set} --voices ../genbank-voices --piper ../genbank-piper/bin/python
python tools/bank/generate.py --compare BANK ../genbank-rebuilt/{set}
```"""

REPLAY = """\
```sh
{bank_is}
python tools/bank/check.py BANK
python tools/bank/replay.py BANK --golden tools/bank/golden/generated
# On a platform with no goldens yet: write them, and return them in a pull request.
python tools/bank/replay.py BANK --golden tools/bank/golden/generated --update --platform PLATFORM
```"""


def _kinds(recordings: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    """(kind, takes, takes that should wake), in the order kinds first come."""
    out: dict[str, list[int]] = {}
    for rec in recordings:
        for take in rec["takes"]:
            row = out.setdefault(take["kind"], [0, 0])
            row[0] += 1
            row[1] += 1 if take["expect_wake"] else 0
    return [(kind, n, wake) for kind, (n, wake) in out.items()]


def datasheet(inputs: dict[str, Any], recordings: list[dict[str, Any]], facts: dict[str, Any]) -> str:
    """DATASHEET.md for one built set, after the questions of Gebru et al.,
    "Datasheets for Datasets" (2018, arXiv 1803.09010; an idea, cited in
    CITATIONS.md): what it is for, what it holds, how it was made, how to
    rebuild it. It names no path of the machine that built it."""
    summary, engine, tools = facts["summary"], facts["engine"], facts["tools"]
    sets, voices = inputs["sets"], inputs["voices"]
    used = {rec["voice"]["id"]: rec["voice"] for rec in recordings}
    rule = sets["sets"][summary["set"]]
    bank_is = BANK_IS.format(set=summary["set"], version=sets["version"])
    out = [
        f"# The generated bank's {summary['set']} set, version {sets['version']}",
        "",
        "Speech made by Piper from public voices, played through simulated rooms, for screening a change to "
        "Emet's listening path. Anyone may rebuild it and replay it, and hold it to the goldens in the Emet "
        "repository's `tools/bank/golden/generated/` on a platform that has them (below). It holds no recording "
        "of a person: every speaker is a generated voice, an id starting `g`, whose record in `voices/` stands "
        "where a person's consent file stands. Each voice is a model trained on recordings of real readers, "
        "and `ATTRIBUTION.md` credits each voice's dataset.",
        "",
        "## What it may decide",
        "",
        *[f"- It may {line}." for line in MAY],
        "",
        "## What it may not decide",
        "",
        *[f"- It may not {line}." for line in MAY_NOT],
        "",
        "## What it holds",
        "",
        f"- {summary['recordings']} recordings, {summary['takes']} takes, {summary['seconds'] / 60:.1f} minutes "
        f"of 16 kHz mono 16-bit audio.",
        f"- The `{summary['set']}` set, by the rule `{rule['rule']}`, for {rule.get('for', 'its use')}. "
        "Every set comes from the same lists by `tools/bank/generate.py`'s `plan`, never by hand; the small "
        "set's recordings are each one of the full set's, byte for byte.",
        f"- Each recording opens with {float(sets['lead_s']):g} s of the room's noise alone, so the wake "
        f"decoder's mean settles before the first take, then one take a slot. A take starts "
        f"{TAKE_OFFSET_S:g} s into its slot, moved on by up to {TAKE_JITTER_S:g} s more by the seed, so the "
        f"takes fall on many phases of the wake engine's 80 ms frame: {summary['phases']} of its "
        f"{FRAME_SAMPLES:,} sample phases in this set.",
        "",
        "| Take kind | Takes | Should wake |",
        "|---|---|---|",
        *[f"| {kind} | {n} | {wake} |" for kind, n, wake in _kinds(recordings)],
        "",
        "Every text is already in the Emet repository, at the source its group in `tools/bank/generated/"
        "phrases.json` names; none comes from a private recording. Decoys and talk should not wake it; the "
        "decoys include near misses of the name.",
        "",
        "### Voices",
        "",
        "| Id | Voice | Speaks as | Licence, as its card states it | Dataset, as its card states it |",
        "|---|---|---|---|---|",
        *[f"| {vid} | {v['name']} | {v['describes']} | {v['licence']} | {v['dataset']} |"
          for vid, v in sorted(used.items())],
        "",
        f"Each voice is pinned by the sha256 of its model, its config and its MODEL_CARD, at revision "
        f"`{voices['revision']}` of {voices['repository']}, and was taken only because piper1-gpl's "
        f"COMMERCIAL_USE.md (commit `{voices['list']['commit']}`) lists it and its card's Training section names "
        f"the base that list gives. The cards were read on {voices['read']}. `ATTRIBUTION.md` credits each one.",
        "",
        "### Rooms",
        "",
        "| Condition | Room | Size (m) | Absorption | Talker to microphone | Decay, read from the response | Noise |",
        "|---|---|---|---|---|---|---|",
    ]
    for cid, room in sorted(facts["rooms"].items()):
        cond = next(c for c in inputs["rooms"]["conditions"] if c["id"] == cid)
        noise = bank_check.noise_words(cond.get("noise")) or "none"
        decay = f"{room['decay_ms']} ms" if room["decay_ms"] is not None else "under 25 dB of fall"
        out.append(f"| {cid} | {room['id']} | {' x '.join(f'{v:g}' for v in room['size_m'])} | "
                   f"{room['absorption']:g} | {room['distance_m']:g} m ({cond['distance']}) | {decay} | {noise} |")
    out += [
        "",
        "A room is a box with one absorption on every wall, simulated by the image method of Allen and Berkley "
        "(1979). The decay is read from the response the builder made, by Schroeder's backward integral "
        "(1965). The noise is pink, by the Voss algorithm as Gardner described it (1978), from SplitMix64 "
        "(Steele, Lea and Flood, 2014) and the seed. No outside noise corpus is used.",
        "",
        "## How it was made",
        "",
        f"- Speech: {engine['name']} {engine['version']} ({voices['engine']['licence']}, run as a tool in a "
        f"venv of its own, never imported), {engine.get('runtime', 'its runtime')}, with "
        f"`{engine.get('arguments', '')}`. Both noise scales at 0: with Piper's own noise two runs of one "
        "sentence gave different samples, with none the same.",
        f"- Everything after the voice, in whole numbers: a 64-tap windowed-sinc resampler to 16 kHz, the "
        f"room, the noise, the mix. Arithmetic: {summary['arithmetic']}. Python {tools['python']}"
        + (f", numpy {tools['numpy']}" if tools.get("numpy") else "") + f", on {tools['platform']}.",
        f"- Seed {sets['seed']}. Each recording's noise and each take's place come from it by sha256, so one "
        "recording can be made without the others.",
        f"- The builder: `tools/bank/generate.py`, builder_sha256 `{text_sha256(Path(__file__))}`, its line ends "
        "read as LF. Each manifest gives the same hash, and the sha256 of each list it read.",
        "- Each manifest's `generated` field holds what made the recording: the voice and its hashes, the "
        "engine, the room and its response's sha256, the noise and its seed, the sha256 of the lists and of "
        "the builder, and per take what was said and the sha256 of what the voice gave back. `check.py` "
        "refuses a recording without it.",
        "",
        "## Rebuild it",
        "",
        REBUILD.format(piper=engine["version"], ort=(str(engine.get("runtime") or "?").split() or ["?"])[-1],
                       numpy=tools.get("numpy") or "2.5.4", set=summary["set"], bank_is=bank_is),
        "",
        "`--compare` reads both banks' manifests and says, recording by recording, whether the audio has the "
        "same sha256. Where it does not, because another machine's onnxruntime gave the voice other samples, "
        "no tool yet holds the rebuild to this bank: the published files with their hashes are the bank, and "
        "a rebuild with other bytes is unverified.",
        "",
        "## Replay it",
        "",
        REPLAY.format(bank_is=bank_is),
        "",
        "The goldens are per platform, in `tools/bank/golden/generated/<platform>/` (`win32-amd64`, "
        "`linux-x86_64`, `linux-aarch64`), and a platform has them only once a replay there has written them. "
        "On a platform with none, every recording prints `NO GOLDEN` and the replay exits 1: the bank is held "
        "to nothing there until the goldens the last command writes come back to the repository in a pull "
        "request.",
        "",
        "## Licence",
        "",
        "The project takes the audio to carry each voice's dataset licence, as its card states it. Whether a "
        "trained model's output is a derivative work of its recordings is unsettled, and piper1-gpl's "
        "COMMERCIAL_USE.md takes no position on it. A CC BY dataset asks for credit, and `ATTRIBUTION.md` gives "
        "it, with any term a dataset adds to its licence. Piper's GPL-3.0-or-later covers Piper; nothing of "
        "Piper is in this bank.",
        "",
        *[f"- {v['name']}'s dataset adds to its licence: \"{v['terms']['text']}\" ({v['terms']['url']}, read "
          f"{v['terms']['read']})." for _vid, v in sorted(used.items()) if v.get("terms")],
        *([""] if any(v.get("terms") for v in used.values()) else []),
        "Questions to the Emet project, wake.up.emet@gmail.com.",
        "",
    ]
    return "\n".join(out)


def attribution(inputs: dict[str, Any], used_voices: dict[str, dict[str, Any]], engine: dict[str, Any]) -> str:
    """ATTRIBUTION.md: every voice the bank speaks with, its dataset's credit
    and any term it adds, and the base it was fine-tuned from, in the cards'
    own words. `engine` is what spoke, as the manifests record it."""
    voices = inputs["voices"]
    pinned = voices["engine"]
    source = f", {pinned['source']}" if engine.get("name") == pinned["name"] and pinned.get("source") else ""
    out = [
        "# Attribution",
        "",
        f"The speech in this bank was made by {engine['name']} {engine['version']} "
        f"({engine.get('licence') or pinned['licence']}{source}) from the voices below, each at "
        f"revision `{voices['revision']}` of {voices['repository']}. Each voice's MODEL_CARD is in `voices/` "
        "beside its record: piper1-gpl's COMMERCIAL_USE.md asks that a voice travel with its card.",
        "",
    ]
    bases: dict[str, dict[str, Any]] = {}
    for vid, voice in sorted(used_voices.items()):
        terms = voice.get("terms")
        out += [
            f"## {vid}: {voice['name']}",
            "",
            f"- Credit: {voice['credit']}",
            f"- Dataset: {voice['dataset']}",
            f"- Licence: {voice['licence']}",
            *([f"- Its dataset adds: \"{terms['text']}\" ({terms['url']}, read {terms['read']})"] if terms else []),
            f"- Training: {voice['training']}",
            f"- Listed in COMMERCIAL_USE.md as: {voice['listed']}",
            f"- Model: {voice['files']['model']['url']}, sha256 {voice['files']['model']['sha256']}",
            "",
        ]
        if voice.get("base"):
            bases[voice["base"]] = voices["bases"][voice["base"]]
    for bid, base in sorted(bases.items()):
        out += [
            f"## The base {bid}: {base['name']}",
            "",
            f"- Credit: {base['credit']}",
            f"- Dataset: {base['dataset']}",
            f"- Licence: {base['licence']}",
            f"- Card: {base['card']['url']}, sha256 {base['card']['sha256']}",
            "",
        ]
    out += ["Built by the Emet project's `tools/bank/generate.py`. Questions to wake.up.emet@gmail.com.", ""]
    return "\n".join(out)


# ====================================================== the public store


def _epoch(day: str) -> int:
    """A YYYY-MM-DD date as seconds since 1970 at midnight UTC."""
    import datetime

    return (datetime.date.fromisoformat(day) - datetime.date(1970, 1, 1)).days * 86400


def pack(bank: Path, to: Path, say: Callable[[str], None] = print, voices: dict[str, Any] | None = None) -> Path:
    """The release asset of a built bank: `genbank-<folder>-<version>.tar.gz`
    in `to`, with a `.sha256` line beside it.

    The bank is checked first, against `voices` or, when None, the pinned
    list. The archive is the same bytes from the same files: members in
    sorted order, every time the bank's date, owner 0, mode 644, and a gzip
    header with no name and no time."""
    import gzip

    bank = Path(bank).resolve()
    lines, _good, bad = bank_check.check(bank, [], False, voices=voices)
    if bad:
        raise GenerateError(f"{bank} does not pass check.py, so it is not packed:\n  " + "\n  ".join(lines))
    version = bank_files.read(bank / "bank.json", "bank")["version"]
    top = f"genbank-{bank.name}-{version}"
    to = Path(to)
    to.mkdir(parents=True, exist_ok=True)
    out = to / f"{top}.tar.gz"
    part = out.with_name(out.name + ".part")
    when = _epoch(version)
    files = sorted((p for p in bank.rglob("*") if p.is_file()), key=lambda p: p.relative_to(bank).as_posix())
    with open(part, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in files:
            info = tarfile.TarInfo(f"{top}/{path.relative_to(bank).as_posix()}")
            info.size, info.mtime, info.mode = path.stat().st_size, when, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
    part.replace(out)
    digest = bank_files.sha256_file(out)
    (to / f"{top}.tar.gz.sha256").write_text(f"{digest}  {out.name}\n", encoding="utf-8", newline="\n")
    say(f"{out.name}: {len(files)} files, {out.stat().st_size} bytes, sha256 {digest}")
    return out


def _unpack(archive: Path, to: Path, top: str) -> None:
    """Every member a plain file or folder under `top`, no link, nothing
    outside: anything else refuses the archive before a byte is written."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            parts = m.name.split("/")
            if (not (m.isfile() or m.isdir()) or m.name.startswith("/") or "\\" in m.name or ".." in parts
                    or parts[0] != top or ":" in m.name):
                raise GenerateError(f"{archive.name}: the member {m.name!r} is not a file inside {top}/")
        try:
            tar.extractall(to, members=members, filter="data")
        except TypeError:  # Python before 3.11.4 has no filter; the members were checked above
            tar.extractall(to, members=members)


def fetch(url: str, sha256: str, to: Path, say: Callable[[str], None] = print,
          voices: dict[str, Any] | None = None) -> Path:
    """A packed bank from `url`, kept only when its sha256 is `sha256`,
    unpacked into `to` and checked against `voices` (None: the pinned
    list). Returns the bank's root.

    A file URL works the same way, which is how the tests run it. A folder
    already there is used again only when the line beside it names the same
    archive hash and it still passes `check.py`; any other is refused."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise GenerateError("--sha256 is the archive's 64 hex digits")
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if not name.endswith(".tar.gz") or not bank_check.ID.match(name):
        raise GenerateError(f"{url}: a packed bank's name ends .tar.gz, as `--pack` writes it")
    top = name[: -len(".tar.gz")]
    to = Path(to)
    to.mkdir(parents=True, exist_ok=True)
    target, marker = to / top, to / f"{top}.fetched"
    if target.exists():
        same = marker.is_file() and marker.read_text(encoding="utf-8").split()[:1] == [sha256.lower()]
        if not same:
            raise GenerateError(f"{target} is there already, from another archive or none: move it away first")
        lines, _good, bad = bank_check.check(target, [], False, voices=voices)
        if bad:
            raise GenerateError(f"{target} came from this archive and no longer passes check.py: move it away "
                                "first\n  " + "\n  ".join(lines))
        say(f"{target}: here already, from {sha256.lower()[:12]}..., and it passes check.py")
        return target
    archive = to / name
    _download(url, archive, sha256)
    try:
        _unpack(archive, to, top)
    finally:
        archive.unlink(missing_ok=True)
    lines, _good, bad = bank_check.check(target, [], False, voices=voices)
    if bad:
        shutil.rmtree(target, ignore_errors=True)
        raise GenerateError(f"{name} unpacked, and does not pass check.py, so it was removed:\n  " + "\n  ".join(lines))
    marker.write_text(f"{sha256.lower()}  {name}\n", encoding="utf-8", newline="\n")
    say(f"{target}: {lines[-1]}")
    return target


def compare(first: Path, second: Path) -> tuple[list[str], int]:
    """(lines, recordings that differ): whether two builds of a set hold
    the same audio, recording by recording, by the sha256 each manifest
    gives. The manifests themselves may differ in the tools they name."""
    a = {doc["id"]: doc for _p, doc in bank_files.manifests(first)}
    b = {doc["id"]: doc for _p, doc in bank_files.manifests(second)}
    lines, differ = [], 0
    for rid in sorted(set(a) | set(b)):
        if rid not in a or rid not in b:
            differ += 1
            lines.append(f"{rid}: only in {first if rid in a else second}")
        elif a[rid]["sha256"] != b[rid]["sha256"]:
            differ += 1
            lines.append(f"{rid}: DIFFERENT audio, {a[rid]['sha256'][:12]}... and {b[rid]['sha256'][:12]}...")
        else:
            lines.append(f"{rid}: same audio, sha256 {a[rid]['sha256'][:12]}...")
    lines.append(f"compare: {len(set(a) | set(b)) - differ} of {len(set(a) | set(b))} recording(s) the same")
    return lines, differ


# ================================================================ the clip

#: The clip under emet-hal/tests/ (decision D8): the reference voice saying
#: the wake phrase, in the near room, after more quiet than the decoder's
#: mean needs to settle (about 3 s, RELEASE_PROCESS section 10's 0.5 row).
CLIP_VOICE = "en_US-ljspeech-medium"
CLIP_TEXT = "hey emet"
CLIP_CONDITION = "near"
CLIP_LEAD_S = 5.5
CLIP_TAIL_S = 1.0


def make_clip(out: Path, inputs: dict[str, Any],
              synthesise: Callable[[dict[str, Any], float, Sequence[str]], list[tuple[int, bytes]]], *,
              engine: dict[str, Any], be: Any = None, command: str | None = None) -> dict[str, Any]:
    """Write the clip and its record beside it (`<clip>.json`). Returns the record."""
    be = be or backend()
    out = Path(out)
    voice = next((v for v in inputs["voices"]["voices"] if v["name"] == CLIP_VOICE), None)
    if voice is None:
        raise GenerateError(f"the clip's voice, {CLIP_VOICE}, is not in the voice list")
    rooms = inputs["rooms"]
    cond = next(c for c in rooms["conditions"] if c["id"] == CLIP_CONDITION)
    room = rooms["rooms"][cond["room"]]
    said = spoken(CLIP_TEXT, ".", inputs["phrases"].get("say_as") or {})
    (rate, pcm), = synthesise(voice, 1.0, [said])
    taps = room_response(room["size_m"], cond["source_m"], cond["microphone_m"], room["absorption"],
                         room["length_s"], rooms["gain_at_1m_q15"], float(rooms["speed_of_sound_m_s"]))
    wet = be.convolve(resample(be.from_pcm(pcm), rate, RATE, be=be), taps)
    lead = int(CLIP_LEAD_S * RATE)
    frames = lead + be.length(wet) + int(CLIP_TAIL_S * RATE)
    mix = be.zeros(frames)
    be.add_at(mix, lead, wet)
    seed = derive_seed(inputs["sets"]["seed"], "clip", "noise")
    be.add_at(mix, 0, be.pink(frames, seed, int(cond["noise"]["rms"])))
    samples, clipped = be.to_pcm(mix)
    write_wav(out, samples)
    record = {
        "format": "emet-clip/1", "file": out.name, "sha256": bank_files.sha256_file(out),
        "bytes": out.stat().st_size, "sample_rate": RATE, "channels": 1, "bits": 16, "frames": frames,
        "duration_s": round(frames / RATE, 6), "text": CLIP_TEXT, "said": said, "phrase_at_sample": lead,
        "voice": {"name": voice["name"], "licence": voice["licence"], "dataset": voice["dataset"],
                  **{f"{k}_sha256": voice["files"][k]["sha256"] for k in ("model", "config", "card")}},
        "engine": {**engine, "length_scale": 1.0, "noise_scale": 0, "noise_w_scale": 0},
        "condition": {"id": cond["id"], "room": cond["room"], "size_m": room["size_m"],
                      "absorption": room["absorption"], "source_m": cond["source_m"],
                      "microphone_m": cond["microphone_m"],
                      "distance_m": round(distance_m(cond["source_m"], cond["microphone_m"]), 3)},
        "noise": {**cond["noise"], "seed": seed}, "clipped": clipped,
        "builder_sha256": text_sha256(Path(__file__)), "inputs": inputs["sha256"],
        "command": command or "python tools/bank/generate.py --clip FILE --voices VOICES --piper PIPER",
        "note": "Generated speech in a simulated room shows the wake engine hears the reference voice. The clip "
                "sets no default.",
    }
    out.with_suffix(".json").write_text(bank_files.dump(record), encoding="utf-8", newline="\n")
    return record


# ============================================================ the command


def _summary(inputs: dict[str, Any], set_name: str) -> list[str]:
    recordings = plan(inputs, set_name)
    seconds = sum(plan_seconds(inputs, rec) for rec in recordings)
    takes = sum(len(rec["takes"]) for rec in recordings)
    rule = inputs["sets"]["sets"][set_name]
    out = [f"{set_name}: {len(recordings)} recording(s), {takes} take(s), {seconds / 60:.1f} min, "
           f"rule {rule['rule']}, for {rule.get('for', 'its use')}"]
    out += [f"  {rec['id']}: {rec['voice']['name']}, {rec['condition']['id']}, part {rec['part']}, "
            f"{len(rec['takes'])} take(s), {plan_seconds(inputs, rec):g} s" for rec in recordings]
    out += [f"  {kind}: {n} take(s), {wake} should wake" for kind, n, wake in _kinds(recordings)]
    return out


def _engine(tool: PiperTool, inputs: dict[str, Any], say: Callable[[str], None]) -> dict[str, Any]:
    """What speaks, as the manifests record it: the versions asked of the
    Piper venv, and the arguments every line is said with."""
    found = tool.versions()
    pinned = inputs["voices"]["engine"]
    engine = {"name": "piper-tts", "version": found["piper-tts"], "licence": pinned["licence"],
              "runtime": f"onnxruntime {found['onnxruntime']}",
              "arguments": "--length-scale L --noise-scale 0 --noise-w-scale 0 --sentence-silence "
                           f"{tool.sentence_silence_s:g}"}
    if found["piper-tts"] != pinned["version"] or engine["runtime"] != pinned.get("runtime", "").split(",")[0]:
        say(f"note: this Piper is piper-tts {found['piper-tts']} with onnxruntime {found['onnxruntime']}; the "
            f"list was read with {pinned['version']} and {pinned.get('runtime')}. The audio may differ from the "
            "published bank's: --compare shows where, and no tool holds a rebuild with other bytes to it.")
    return engine


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", type=Path, help="the new bank's folder, which must not exist or be empty")
    ap.add_argument("--set", help="the set to build (default small) or plan (default every set), as "
                                  "generated/sets.json names it")
    ap.add_argument("--voices", type=Path, metavar="DIR", help="the voices' files, as --fetch-voices writes them")
    ap.add_argument("--piper", metavar="PYTHON_OR_EXE",
                    help="the Python of a venv that holds piper-tts, or its piper program; run, never imported")
    ap.add_argument("--synth-cache", type=Path, metavar="DIR",
                    help="keep what the voice said here, and say again only what is not there")
    ap.add_argument("--data", type=Path, default=DATA, help="the lists' folder (default %(default)s)")
    ap.add_argument("--plain", action="store_true", help="the arithmetic in plain Python even with numpy here")
    ap.add_argument("--plan", action="store_true", help="print each set's recordings and takes, or the --set "
                                                         "named; needs no voice")
    ap.add_argument("--fetch-voices", type=Path, metavar="DIR", help="download the pinned voices into DIR")
    ap.add_argument("--sums", type=Path, metavar="BANK",
                    help="write a built bank's SHA256SUMS again after adding a file, such as a cassette, and check it")
    ap.add_argument("--pack", type=Path, metavar="BANK", help="pack a built bank as a release asset")
    ap.add_argument("--to", type=Path, metavar="DIR", help="where --pack writes and --fetch unpacks")
    ap.add_argument("--fetch", metavar="URL", help="download a packed bank, held to --sha256, and unpack it")
    ap.add_argument("--sha256", metavar="HEX", help="the packed bank's sha256, for --fetch")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BANK", "OTHER"),
                    help="whether two builds hold the same audio, recording by recording")
    ap.add_argument("--clip", type=Path, metavar="FILE", help="write the wake clip for emet-hal's tests")
    args = ap.parse_args(argv)
    try:
        if args.compare:
            lines, differ = compare(*args.compare)
            print("\n".join(lines))
            return 1 if differ else 0
        if args.fetch:
            if not args.sha256 or not args.to:
                ap.error("--fetch needs --sha256 and --to")
            fetch(args.fetch, args.sha256, args.to)
            return 0
        if args.sums:
            sums_again(args.sums)
            return 0
        if args.pack:
            if not args.to:
                ap.error("--pack needs --to")
            pack(args.pack, args.to)
            return 0
        inputs = load_inputs(args.data)
        if args.plan:
            names = list(inputs["sets"]["sets"]) if args.set is None else [args.set]
            for name in names:
                print("\n".join(_summary(inputs, name)))
            return 0
        set_name = args.set or "small"
        if args.fetch_voices:
            fetch_voices(inputs, args.fetch_voices)
            return 0
        if not args.voices or not args.piper:
            ap.error("building a set or the clip needs --voices and --piper")
        be = backend(plain=args.plain)
        sets = inputs["sets"]
        if args.clip:
            found = verify_voices(inputs, args.voices, [CLIP_VOICE])
            tool = PiperTool(args.piper, found, float(sets.get("sentence_silence_s", 0.2)))
            record = make_clip(args.clip, inputs, tool, engine=_engine(tool, inputs, print), be=be,
                               command=f"python tools/bank/generate.py --clip {args.clip.as_posix()} "
                                       "--voices VOICES --piper PIPER")
            print(f"{args.clip}: {record['duration_s']:g} s, {record['bytes']} bytes, sha256 {record['sha256']}")
            return 0
        if args.out is None:
            ap.error("name the new bank's folder, or one of --plan, --fetch-voices, --sums, --pack, --fetch, "
                     "--compare")
        names = sorted({rec["voice"]["name"] for rec in plan(inputs, set_name)})
        found = verify_voices(inputs, args.voices, names)
        tool = PiperTool(args.piper, found, float(sets.get("sentence_silence_s", 0.2)))
        engine = _engine(tool, inputs, print)
        summary = build(args.out, set_name, inputs, tool, engine=engine,
                        cards={name: entry["card"] for name, entry in found.items()},
                        synth_dir=args.synth_cache, be=be)
        print(f"built {set_name}: {summary['recordings']} recording(s), {summary['takes']} take(s), "
              f"{summary['seconds'] / 60:.1f} min, arithmetic {summary['arithmetic']}")
        return 0
    except (GenerateError, bank_files.BankError) as exc:
        print(f"generate: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
