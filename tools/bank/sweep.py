#!/usr/bin/env python3
"""Write the sine sweep a phone plays for the bank's room responses.

    python tools/bank/sweep.py OUT.wav

For sitting 2, from the repository's root on the laptop, which keeps the
bank beside the repository:

    python tools/bank/sweep.py ../voicebank/sweeps/sweep-100hz-8khz-10s.wav

Bank v1's second sitting takes its room responses inside its block with
the microphone's gain control off (E13 (a), decided 2026-11-25;
`plans/v1-sitting-2.json`): at each of five talker positions the
maintainer holds a phone at mouth height and plays this file twice, and
the robot records it. This script writes that file, and beside it a
JSON file of the same name (`sweep.json` for `sweep.wav`), which names
every parameter and the file's sha256, so the response can be worked out
from the recording later.

The sweep is exponential: its frequency rises from 100 Hz to 8000 Hz at a
constant rate in octaves over 10 s, as Farina's swept-sine method has it
(A. Farina, "Simultaneous measurement of impulse response and distortion
with a swept-sine technique", AES 108th Convention, Paris, February 2000,
paper 5093; the method only, no code; the AES e-library's record read
2026-11-27, the paper itself not read). With f1 and f2 the two ends, T the
length and L = ln(f2 / f1), the sample at time t is

    sin(2 * pi * f1 * T / L * (exp(t * L / T) - 1))

whose frequency is f1 at t = 0 and f2 at t = T. 8000 Hz is the bank's
Nyquist frequency, since the bank records at 16 kHz. The sweep fades in
and out over 50 ms, so it starts and ends without a click, has 0.5 s of
silence on each side for a phone that clips the start of a file, and
peaks at -6 dBFS. The file is mono 16-bit at 48 kHz, which a phone plays
as it is. The same parameters give the same samples, so the sha256 in
the JSON file names the file the sitting played.

**Outside the repository.** The file is for the phone, and nothing in the
repository reads it, so a path inside the repository is refused; the
bank's own folder, `../voicebank/sweeps/` from the repository's root, is
a place for it. A file already at OUT with other bytes is refused too: the
sweep a sitting played is never replaced under the same name.

Exit status 0 when the file is written or is already there with the same
bytes, 2 when the path is refused. The standard library only.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: The repository's root, which the file must stay outside.
REPO = HERE.parent.parent

FORMAT = "emet-sweep/1"
RATE = 48000
F1_HZ = 100.0
F2_HZ = 8000.0
SWEEP_S = 10.0
LEAD_S = 0.5
TAIL_S = 0.5
FADE_S = 0.05
PEAK_DBFS = -6.0
#: The name sitting 2's plan tells the maintainer to have on the phone.
NAME = f"sweep-{F1_HZ:g}hz-{F2_HZ / 1000:g}khz-{SWEEP_S:g}s.wav"


def samples() -> array.array:
    """The whole file's samples, the lead, then the faded sweep, then the tail."""
    n = int(round(SWEEP_S * RATE))
    fade = int(round(FADE_S * RATE))
    span = math.log(F2_HZ / F1_HZ)
    peak = 32767 * 10 ** (PEAK_DBFS / 20.0)
    out = array.array("h", bytes(2 * int(round(LEAD_S * RATE))))
    for i in range(n):
        t = i / RATE
        gain = 1.0
        if i < fade:
            gain = 0.5 - 0.5 * math.cos(math.pi * i / fade)
        elif i >= n - fade:
            gain = 0.5 - 0.5 * math.cos(math.pi * (n - 1 - i) / fade)
        phase = 2 * math.pi * F1_HZ * SWEEP_S / span * (math.exp(t * span / SWEEP_S) - 1)
        out.append(int(round(peak * gain * math.sin(phase))))
    out.extend(array.array("h", bytes(2 * int(round(TAIL_S * RATE)))))
    return out


def wav_bytes(pcm: array.array) -> bytes:
    """The samples as little-endian bytes, whatever this machine's byte order."""
    data = array.array("h", pcm)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def parameters(sha256: str, frames: int) -> dict:
    return {
        "format": FORMAT,
        "method": "exponential sine sweep (Farina, AES 108th Convention, 2000, paper 5093)",
        "made_with": "tools/bank/sweep.py",
        "f1_hz": F1_HZ, "f2_hz": F2_HZ, "sweep_s": SWEEP_S, "lead_s": LEAD_S, "tail_s": TAIL_S,
        "fade_s": FADE_S, "peak_dbfs": PEAK_DBFS,
        "sample_rate": RATE, "channels": 1, "bits": 16, "frames": frames,
        "duration_s": round(frames / RATE, 3), "sha256": sha256,
    }


def inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO.resolve())
    except ValueError:
        return False
    return True


def write(out: Path) -> tuple[Path, dict, bool]:
    """Write the sweep to `out` and its parameters beside it. Returns the
    parameters' path, the parameters, and whether `out` was already there
    with the same bytes. Raises ValueError for a path refused."""
    if out.suffix.lower() != ".wav":
        raise ValueError(f"{out} does not end .wav; the phone plays a WAV file")
    if inside_repository(out):
        raise ValueError(f"{out} is inside the repository; the sweep is a file for the phone and stays outside "
                         "it, for example in ../voicebank/sweeps/")
    pcm = samples()
    tmp = out.with_name(out.name + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(wav_bytes(pcm))
    made = tmp.read_bytes()
    digest = hashlib.sha256(made).hexdigest()
    same = out.exists() and out.read_bytes() == made
    if out.exists() and not same:
        tmp.unlink()
        raise ValueError(f"{out} is there already and holds other bytes; the sweep a sitting played is never "
                         "replaced under its name, so choose another")
    tmp.replace(out)
    doc = parameters(digest, len(pcm))
    side = out.with_suffix(".json")
    side.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    return side, doc, same


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sweep.py",
        description="Write the sine sweep a phone plays for the bank's room responses, and its parameters "
                    "beside it in a JSON file of the same name.")
    parser.add_argument("out", help="the WAV file to write, outside the repository")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser()
    try:
        side, doc, same = write(out)
    except ValueError as exc:
        print(f"sweep: {exc}", file=sys.stderr)
        return 2
    print(f"{'there already, the same bytes' if same else 'wrote'}: {out}")
    print(f"  an exponential sine sweep from {F1_HZ:g} Hz to {F2_HZ:g} Hz over {SWEEP_S:g} s, {LEAD_S:g} s of "
          f"silence each side, {RATE} Hz mono 16-bit, peak {PEAK_DBFS:g} dBFS, {doc['duration_s']:g} s in all")
    print(f"  sha256 {doc['sha256']}")
    print(f"  its parameters: {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
