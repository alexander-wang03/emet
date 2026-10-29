#!/usr/bin/env python3
"""Turn the Deepgram replays already paid for into cassettes, for nothing.

    python tools/bank/seed_cassettes.py BANK RUN --out CASSETTE [--regenerated DIR] [--provisional]

RUN is one paid replay's folder in releases/v0.5.3/harness/runs/ (`dg02_*` to
`dg10_*`): its deepgram.json holds, per turn, the sha256 and length of the
bytes fed, each partial with the number of `feed()` calls after which it
came, the final, and every message Deepgram sent; its record.json holds
where each wake fell. A cassette also needs each partial's prefix hash, the
sha256 of the bytes fed up to it, and those bytes were never kept. So they
are made again: the same recording is replayed through the mock
transcriber with `record_turns`, at settings that feed the same bytes, and
a turn is kept only when its bytes hash to the sha256 the paid run
recorded, every byte and no more. dg01 was logged only as text and holds
none of this.

**Two passes.** A live transcriber's partials can change where a turn ends:
the endpointer reads the words so far, and a trailing clause waits once
more. The mock reads none from a real recording, so the first pass can end
a turn where the paid run did not. The second pass replays with a
provisional cassette, the paid run's partials keyed by the first pass's
bytes up to each one, so the mock gives the same words on the same `feed()`
call; the turns are then judged and the cassette written from the second
pass's bytes. `--provisional` writes the first pass's cassette instead.

**Where the bytes come from.** Without `--regenerated`, from this tree:
`replay.py`'s own run at the paid run's start mean (all of dg02 to dg10
started from the carried mean). That feeds the paid run's bytes where the
engine's cut and endpointer are what the paid run used: dg05, dg08 and dg09
fed the streams this tree feeds. The runs made with the 0.5.3 harness's
knobs (fade, margin, gap, a forced handover) patched the cut, so their
bytes come from that harness, run outside the repository with the mock's
`record_turns`, and `--regenerated DIR` names its output: a `turns.jsonl`
and the turns' bytes as the mock writes them, and `wakes.json`,
`{"wake_end_samples": [...]}`, the frame end of each turn's wake in order.

A paid turn is matched to a regenerated one by its wake's frame end in the
recording, and says why when it is not kept.

The standard library and the four packages only. The cassette holds what
the maintainer said, so it goes in the bank, never in the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest as bank_files  # noqa: E402
import replay  # noqa: E402

PROJECT = HERE.parents[2]


def rel(path: Path) -> str:
    """A path relative to Project Emet/, as the ledgers write them."""
    try:
        return Path(path).resolve().relative_to(PROJECT).as_posix()
    except ValueError:
        return Path(path).resolve().as_posix()


def paid_turns(run: Path) -> list[dict[str, Any]]:
    """Each turn of a paid run, with what a cassette needs from it."""
    dg = json.loads((run / "deepgram.json").read_text(encoding="utf-8"))
    record = json.loads((run / "record.json").read_text(encoding="utf-8"))
    entry = (dg.get("ledger") or {}).get("entry_number")
    when = ((dg.get("ledger") or {}).get("entry") or {}).get("time", "")[:10]
    by_wake = {c.get("wake"): c for c in dg.get("connections") or [] if c.get("kind") != "preflight"}
    out = []
    for i, (turn, wake) in enumerate(zip(dg["turns"], record["wakes"])):
        if wake.get("turn_index") != i:
            raise SystemExit(f"seed: {run.name}: record.json's wake {i + 1} is not turn {i + 1}")
        conn = by_wake.get(turn["wake"]) or {}
        query = dict(parse_qsl(urlparse(conn.get("url") or "").query))
        frame = turn["bytes_fed"] // turn["frames_fed"] if turn["frames_fed"] else 0
        out.append({
            "index": i, "wake": turn["wake"], "wake_end_sample": wake["wake_end_sample"],
            "fed_sha256": wake["turn_fed_sha256"], "fed_bytes": turn["bytes_fed"], "frames": turn["frames_fed"],
            "frame_bytes": frame, "handover_sha256": wake.get("fed_handover_sha256"),
            "handover_bytes": 2 * int(wake.get("handover_samples") or 0),
            "partials": [{"text": p["text"], "after_bytes": p["after_frames"] * frame} for p in turn["partials"]],
            "final": {k: turn["final"].get(k) for k in ("text", "confidence", "error")},
            "query": query, "received": conn.get("received") or [],
            "ledger_entry": entry, "recorded": when,
            "source": f"{rel(run / 'deepgram.json')} turns[{i}]",
        })
    return out


def cassette_turn(paid: dict[str, Any], raw: bytes) -> dict[str, Any]:
    """The paid turn as a cassette turn, its prefixes hashed from `raw`."""
    from emet_providers import cassette

    received = [{"text": json.dumps(m["data"])} for m in paid["received"] if isinstance(m.get("data"), dict)]
    version = cassette.model_version(received)
    return cassette.check_turn({
        "request": cassette.request_for("deepgram", paid["query"]),
        "fed_sha256": paid["fed_sha256"], "fed_bytes": paid["fed_bytes"],
        "partials": [{**p, "prefix_sha256": hashlib.sha256(raw[:p["after_bytes"]]).hexdigest()}
                     for p in paid["partials"] if p["after_bytes"] <= len(raw)],
        "final": paid["final"], "model_version": (version or {}).get("version"),
        "recorded": paid["recorded"], "ledger_entry": paid["ledger_entry"], "source": paid["source"],
    })


def judge(paid: dict[str, Any], regenerated: dict[int, bytes]) -> tuple[bytes | None, str]:
    """The regenerated bytes when they are the paid turn's, or why not."""
    raw = regenerated.get(paid["wake_end_sample"])
    if raw is None:
        return None, f"no wake at sample {paid['wake_end_sample']} in the regeneration"
    if len(raw) == paid["fed_bytes"] and hashlib.sha256(raw).hexdigest() == paid["fed_sha256"]:
        return raw, "kept"
    if paid["handover_sha256"] and hashlib.sha256(raw[:paid["handover_bytes"]]).hexdigest() != paid["handover_sha256"]:
        return None, "the handover differs: this tree cuts the name elsewhere than the paid run did"
    frames = len(raw) // paid["frame_bytes"] if paid["frame_bytes"] else 0
    if frames != paid["frames"]:
        return None, f"the turn ran {paid['frames']} frames in the paid run and {frames} here"
    return None, "the same length and handover, other bytes after it"


def regenerate_here(doc: dict, wav: Path, work: Path, mean: str, cassette_path: Path | None,
                    request: dict | None) -> dict[int, bytes]:
    """Replay the recording on this tree; each turn's bytes by its wake's frame end."""
    conditions = {"threshold": replay.THRESHOLD, "start_mean": mean, "cmninit": replay.MEANS[mean],
                  "flags": replay.FLAGS}
    record = replay.replay_one(doc, wav, work, conditions, str(cassette_path) if cassette_path else None, request)
    turns = replay.read_turns(work / "turns")
    return {w["wake_end_sample"]: t["raw"] for w, t in zip(record["wakes"], turns)}


def regenerated_from(path: Path) -> dict[int, bytes]:
    """A regeneration made elsewhere: the mock's turns and wakes.json."""
    ends = json.loads((path / "wakes.json").read_text(encoding="utf-8"))["wake_end_samples"]
    turns = replay.read_turns(path)
    if len(ends) != len(turns):
        raise SystemExit(f"seed: {path}: {len(ends)} wake ends and {len(turns)} turns")
    return {end: t["raw"] for end, t in zip(ends, turns)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path, help="the bank's root, to find the recording by its sha256")
    ap.add_argument("run", type=Path, help="a paid replay's folder, releases/v0.5.3/harness/runs/dgNN_*")
    ap.add_argument("--out", type=Path, required=True, help="the cassette file to write")
    ap.add_argument("--search", type=Path, action="append", default=[], metavar="DIR")
    ap.add_argument("--regenerated", type=Path, metavar="DIR", help="the bytes, from a regeneration made elsewhere")
    ap.add_argument("--provisional", action="store_true", help="write the first pass's cassette and stop")
    ap.add_argument("--work", type=Path, help="where the replays' files go (default: a new temporary folder)")
    args = ap.parse_args(argv)

    dg = json.loads((args.run / "deepgram.json").read_text(encoding="utf-8"))
    if "carried" not in str(dg.get("mean")):
        raise SystemExit(f"seed: {args.run.name} did not start from the carried mean; this tool replays from it")
    paid = paid_turns(args.run)
    root = args.bank.resolve()
    doc = next((d for _, d in bank_files.manifests(root) if d["sha256"] == dg["recording_sha256"]), None)
    if doc is None:
        raise SystemExit(f"seed: no bank recording has sha256 {dg['recording_sha256'][:12]}...")
    wav = bank_files.find_audio(doc, root, args.search)
    if wav is None:
        raise SystemExit(f"seed: the audio of {doc['id']} is not here")
    work = args.work.resolve() if args.work else Path(tempfile.mkdtemp(prefix="emet-seed-"))
    try:
        if args.regenerated:
            first = regenerated_from(args.regenerated)
        else:
            first = regenerate_here(doc, wav, work / "pass1", "carried", None, None)
        from emet_providers import cassette

        provisional = []
        for p in paid:
            if p["wake_end_sample"] not in first:
                continue
            try:
                provisional.append(cassette_turn(p, first[p["wake_end_sample"]]))
            except cassette.CassetteError:
                # A partial at the paid turn's last byte whose first-pass bytes
                # are not the paid turn's: that turn cannot be kept anyway.
                continue

        if args.provisional:
            cassette.save(args.out, provisional)
            print(f"seed: {args.run.name}: a provisional cassette of {len(provisional)} turn(s) in {args.out}")
            return 0
        if args.regenerated:
            second = first
        else:
            prov = work / "provisional.json"
            cassette.save(prov, provisional)
            second = regenerate_here(doc, wav, work / "pass2", "carried", prov, None)
        kept, lines = [], []
        for p in paid:
            raw, why = judge(p, second)
            lines.append(f"  turn {p['index'] + 1} (wake at {p['wake_end_sample'] / doc['sample_rate']:.2f} s): {why}")
            if raw is not None:
                kept.append(cassette_turn(p, raw))
        if kept:
            cassette.save(args.out, kept)
        print(f"seed: {args.run.name}: {len(kept)} of {len(paid)} turn(s) kept"
              + (f", in {args.out}" if kept else ", no cassette written"))
        print("\n".join(lines))
    finally:
        if not args.work:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
