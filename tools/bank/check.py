#!/usr/bin/env python3
"""Check a voice bank: its index, every manifest, every hash, every slot.

    python tools/bank/check.py BANK [--search DIR ...] [--by-hash]

For each recording bank.json lists it prints one line, `ok` or the problems,
and then a total. It checks:

    the index        bank.json is sound, and every room and speaker a
                     manifest names is in it
    the consent      every speaker's consent file exists and is not empty
    each manifest    its fields (`manifest.problems`), and that its id is
                     its file name and unique in the bank
    the audio        found by the manifest's path, or by name in a --search
                     directory, or with --by-hash by any .wav there whose
                     sha256 matches; its sha256 and its header (rate,
                     channels, bits, frames) equal the manifest's
    each slot        inside the recording, in order, not overlapping

It exits 1 on any problem, 0 when every recording is sound, so a script can
gate on it. Nothing is written. The standard library only.

**Why the hash.** A recording is never edited (FORMAT.md): a bank is
append-only, and every number replayed from it names the file by its
sha256. A file that changed under the same name would quietly move every
golden built on it. The bank lives off GitHub on purpose, so this is the
only check that the copy on the laptop and the copy on the reference body
are the same bank.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifest as bank_files  # noqa: E402

#: The header fields compared with the manifest's.
WAV_FIELDS = ("sample_rate", "channels", "bits", "frames")


def check_recording(path: Path, rel: str, bank: dict, root: Path, search: list[Path], by_hash: bool,
                    seen_ids: dict[str, str]) -> tuple[str, list[str], Path | None, dict | None]:
    """(id, problems, audio, manifest) for one manifest the index lists."""
    try:
        doc = bank_files.read(path, "manifest")
    except bank_files.BankError as exc:
        return rel, exc.problems, None, None
    found: list[str] = []
    mid = doc["id"]
    if path.stem != mid:
        found.append(f"the id {mid!r} is not the file's name {path.stem!r}")
    if mid in seen_ids:
        found.append(f"the id is also {seen_ids[mid]}'s")
    seen_ids.setdefault(mid, rel)
    if doc["room"] not in bank["rooms"]:
        found.append(f"room {doc['room']!r} is not in bank.json")
    if doc["speaker"] not in bank["speakers"]:
        found.append(f"speaker {doc['speaker']!r} is not in bank.json")
    audio = bank_files.find_audio(doc, root, search, by_hash=by_hash)
    if audio is None:
        where = [str(root / doc["path"])] + [str(d) for d in search]
        found.append(f"no file with sha256 {doc['sha256'][:12]}... at {', '.join(where)}"
                     + ("" if by_hash else " (by name; --by-hash looks at every .wav there)"))
        return mid, found, None, doc
    try:
        facts = bank_files.wav_facts(audio)
    except Exception as exc:  # noqa: BLE001  the wave module raises several kinds
        found.append(f"{audio} is not a readable wav: {exc}")
        return mid, found, audio, doc
    for key in WAV_FIELDS:
        if facts[key] != doc[key]:
            found.append(f"{key} is {facts[key]} in the file and {doc[key]} in the manifest")
    for take in doc["takes"]:
        if take["slot"]["end"] > facts["frames"]:
            found.append(f"take {take['id']}: slot ends at {take['slot']['end']}, past the file's {facts['frames']} samples")
    return mid, found, audio, doc


def check(root: Path, search: list[Path], by_hash: bool) -> tuple[list[str], int, int]:
    """(lines to print, recordings sound, recordings with problems)."""
    lines: list[str] = []
    try:
        bank = bank_files.read(root / "bank.json", "bank")
    except bank_files.BankError as exc:
        return [f"bank.json: {p}" for p in exc.problems], 0, 1
    bad_index = 0
    for sid, who in bank["speakers"].items():
        consent = root / who["consent"]
        if not consent.is_file():
            lines.append(f"speaker {sid}: no consent file at {who['consent']}")
            bad_index += 1
        elif not consent.read_text(encoding="utf-8").strip():
            lines.append(f"speaker {sid}: the consent file {who['consent']} is empty")
            bad_index += 1
    good = bad = 0
    seen_ids: dict[str, str] = {}
    takes = slots_seconds = 0
    for rel in bank["files"]:
        mid, found, audio, doc = check_recording(root / rel, rel, bank, root, search, by_hash, seen_ids)
        if found or doc is None:
            bad += 1
            lines.append(f"{mid}: {len(found)} problem(s)")
            lines.extend(f"    {p}" for p in found)
            continue
        good += 1
        takes += len(doc["takes"])
        slots_seconds += doc["duration_s"]
        where = "" if audio == root / doc["path"] else f", found at {audio}"
        lines.append(f"{mid}: ok, {len(doc['takes'])} take(s), {doc['duration_s']:g} s, "
                     f"sha256 {doc['sha256'][:12]}...{where}")
    lines.append(f"total: {good} of {good + bad} recording(s) sound, {takes} take(s), "
                 f"{slots_seconds / 60:.1f} min of audio, bank version {bank['version']}"
                 + (f"; {bad_index} index problem(s)" if bad_index else ""))
    return lines, good, bad + bad_index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path, help="the bank's root, the folder holding bank.json")
    ap.add_argument("--search", type=Path, action="append", default=[], metavar="DIR",
                    help="a directory to look in for a recording not at its manifest's path; may be repeated")
    ap.add_argument("--by-hash", action="store_true",
                    help="also take any .wav directly inside a --search directory whose sha256 matches")
    args = ap.parse_args(argv)
    lines, _good, bad = check(args.bank.resolve(), [d.resolve() for d in args.search], args.by_hash)
    print("\n".join(lines))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
