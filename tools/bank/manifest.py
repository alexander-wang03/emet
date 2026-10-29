"""Read, check and write the voice bank's files (`tools/bank/FORMAT.md`).

The bank tools share one reader so a file is judged the same way by each of
them. Every format names itself in a `format` field, and `read()` refuses a
file whose field names another format, then lists every problem it finds
rather than the first, so one pass over a bank says everything wrong with it.

The API is small on purpose, since `record.py` and `label.py` import it too:

    read(path, kind)          the document, checked; BankError lists problems
    problems(doc, kind)       the same checks on a document in memory
    write(path, doc, kind)    checked first, then written as `dump()` lays it out
    dump(doc)                 the text a bank file is written as
    manifests(root)           (path, manifest) for each file bank.json lists
    find_audio(manifest, root, search, by_hash=False)
                              the recording, by the manifest's path or by name
    sha256_file(path)         the hash a recording is known by
    wav_facts(path)           rate, channels, bits, frames and duration
    take_at(manifest, sample) the take whose slot holds a sample

`kind` is one of `bank`, `manifest`, `cassette` and `golden`. The standard
library only, so the tools run in the repo's venv and on the reference body
with nothing installed.
"""

from __future__ import annotations

import hashlib
import json
import re
import wave
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "BankError",
    "FORMATS",
    "read",
    "problems",
    "write",
    "dump",
    "manifests",
    "find_audio",
    "sha256_file",
    "wav_facts",
    "take_at",
]

#: The `format` field each kind of file carries.
FORMATS = {
    "bank": "emet-bank/1",
    "manifest": "emet-bank/1",
    "cassette": "emet-cassette/1",
    "golden": "emet-golden/1",
}

RECORDING_KINDS = ("takes", "talk", "room_tone", "soak")
TAKE_KINDS = (
    "wake_pause", "one_breath", "number", "short", "decoy",
    "pause", "long", "talk", "room_tone", "room_response",
)
CASSETTE_HITS = ("hit", "miss", None)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
#: A speaker is an id and never a name: "s" and digits, so a name cannot pass.
_SPEAKER = re.compile(r"^s\d{2,}$")
_PLATFORM = re.compile(r"^(win32|linux|darwin)-[a-z0-9_]+$")
_NAME_CUT = re.compile(r"^\d+ of \d+$")


class BankError(ValueError):
    """A bank file that cannot be used, with every problem found in it."""

    def __init__(self, path: Path | str, found: list[str]) -> None:
        self.path = str(path)
        self.problems = list(found)
        super().__init__(f"{self.path}: " + "; ".join(self.problems))


# ------------------------------------------------------------------ hashes


def sha256_file(path: Path | str) -> str:
    """The sha256 of a file's bytes, read in blocks, in lowercase hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def wav_facts(path: Path | str) -> dict[str, Any]:
    """What the wav header says, in the manifest's own field names."""
    with wave.open(str(path), "rb") as w:
        rate, frames = w.getframerate(), w.getnframes()
        return {
            "sample_rate": rate,
            "channels": w.getnchannels(),
            "bits": 8 * w.getsampwidth(),
            "frames": frames,
            "duration_s": round(frames / rate, 6) if rate else 0.0,
        }


# ------------------------------------------------------------- the checks


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return (isinstance(v, (int, float))) and not isinstance(v, bool)


def _opt_str(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _keys(doc: dict, required: Iterable[str], where: str, out: list[str]) -> None:
    """Every required key present and no other: a misspelt field is a
    problem, never a field quietly ignored."""
    required = tuple(required)
    for key in required:
        if key not in doc:
            out.append(f"{where}: no {key!r}")
    for key in doc:
        if key not in required:
            out.append(f"{where}: unknown field {key!r}")


def _relative(path: Any) -> bool:
    """A path inside a file is relative, in forward slashes."""
    return isinstance(path, str) and bool(path) and "\\" not in path and not path.startswith("/") and ":" not in path


def _check_bank(doc: dict, out: list[str]) -> None:
    _keys(doc, ("format", "version", "files", "rooms", "speakers"), "bank", out)
    if not isinstance(doc.get("version"), str) or not _DATE.match(doc.get("version") or ""):
        out.append("bank: version is the date of the last change, YYYY-MM-DD")
    files = doc.get("files")
    if not isinstance(files, list) or not all(_relative(f) for f in files):
        out.append("bank: files is a list of paths relative to the bank's root")
    elif len(set(files)) != len(files):
        out.append("bank: a manifest is listed twice")
    rooms = doc.get("rooms")
    if not isinstance(rooms, dict) or not rooms:
        out.append("bank: rooms names at least one room")
    else:
        for rid, room in rooms.items():
            if not _ID.match(str(rid)) or not isinstance(room, dict) or not isinstance(room.get("description"), str):
                out.append(f"bank: room {rid!r} is an id with a description")
    speakers = doc.get("speakers")
    if not isinstance(speakers, dict) or not speakers:
        out.append("bank: speakers names at least one speaker")
    else:
        for sid, who in speakers.items():
            if not _SPEAKER.match(str(sid)):
                out.append(f"bank: speaker {sid!r} is not an id like s01; a speaker is never a name")
            if not isinstance(who, dict) or set(who) != {"consent"} or not _relative(who.get("consent")):
                out.append(f"bank: speaker {sid!r} names its consent file, and nothing else")


MANIFEST_FIELDS = (
    "format", "id", "path", "sha256", "sample_rate", "channels", "bits", "frames", "duration_s",
    "recorded", "start_time", "device", "capture_level", "room", "position", "distance",
    "distance_m", "facing", "speaker", "noise", "kind", "lead_s", "prompt_every_s", "made_with",
    "takes",
)
TAKE_FIELDS = (
    "id", "kind", "text", "question", "first_sound", "slot", "expect_wake", "retake_of", "exclude",
)


def _check_manifest(doc: dict, out: list[str]) -> None:
    _keys(doc, MANIFEST_FIELDS, "manifest", out)
    mid = doc.get("id")
    if not isinstance(mid, str) or not _ID.match(mid):
        out.append("manifest: id is lowercase letters, digits, dots, dashes and underscores")
        mid = None
    if not _relative(doc.get("path")):
        out.append("manifest: path is relative to the bank's root, in forward slashes")
    if not isinstance(doc.get("sha256"), str) or not _HEX64.match(doc.get("sha256") or ""):
        out.append("manifest: sha256 is 64 lowercase hex digits")
    for key in ("sample_rate", "channels", "bits"):
        if not _is_int(doc.get(key)) or doc.get(key) <= 0:
            out.append(f"manifest: {key} is a whole number above 0")
    frames = doc.get("frames")
    if not _is_int(frames) or frames < 0:
        out.append("manifest: frames is a whole number, 0 or more")
        frames = None
    rate = doc.get("sample_rate") if _is_int(doc.get("sample_rate")) and doc.get("sample_rate") > 0 else None
    duration = doc.get("duration_s")
    if not _is_num(duration):
        out.append("manifest: duration_s is a number")
    elif frames is not None and rate and abs(duration - frames / rate) > 0.0005:
        out.append(f"manifest: duration_s {duration} is not frames / sample_rate ({frames / rate:g})")
    if not isinstance(doc.get("recorded"), str) or not _DATE.match(doc.get("recorded") or ""):
        out.append("manifest: recorded is a date, YYYY-MM-DD")
    for key in ("start_time", "device", "position", "distance", "facing", "noise", "made_with"):
        if not _opt_str(doc.get(key)):
            out.append(f"manifest: {key} is text or null")
    if not (doc.get("capture_level") is None or isinstance(doc.get("capture_level"), (str, int, float))):
        out.append("manifest: capture_level is text, a number or null")
    if not isinstance(doc.get("room"), str) or not doc.get("room"):
        out.append("manifest: room names a room in bank.json")
    if not isinstance(doc.get("speaker"), str) or not _SPEAKER.match(doc.get("speaker") or ""):
        out.append("manifest: speaker is an id like s01, never a name")
    dm = doc.get("distance_m")
    if not (dm is None or (_is_num(dm) and dm >= 0)):
        out.append("manifest: distance_m is a measured number of metres, or null")
    if doc.get("kind") not in RECORDING_KINDS:
        out.append(f"manifest: kind is one of {', '.join(RECORDING_KINDS)}")
    lead = doc.get("lead_s")
    if not (lead is None or (_is_num(lead) and lead >= 0)):
        out.append("manifest: lead_s is 0 or more, or null")
    every = doc.get("prompt_every_s")
    if not (every is None or (_is_num(every) and every > 0)):
        out.append("manifest: prompt_every_s is above 0, or null")
    takes = doc.get("takes")
    if not isinstance(takes, list):
        out.append("manifest: takes is a list")
        return
    seen: set[str] = set()
    last_end = 0
    for i, take in enumerate(takes):
        where = f"take {i + 1}"
        if not isinstance(take, dict):
            out.append(f"{where}: not an object")
            continue
        _keys(take, TAKE_FIELDS, where, out)
        tid = take.get("id")
        if isinstance(tid, str):
            where = f"take {tid}"
        if not isinstance(tid, str) or (mid and not tid.startswith(mid + "/")):
            out.append(f"{where}: id is the recording's id, a slash, and the take's own")
        elif tid in seen:
            out.append(f"{where}: the id is used twice")
        else:
            seen.add(tid)
        if take.get("kind") not in TAKE_KINDS:
            out.append(f"{where}: kind is one of {', '.join(TAKE_KINDS)}")
        for key in ("text", "question", "first_sound"):
            if not _opt_str(take.get(key)):
                out.append(f"{where}: {key} is text or null")
        if not isinstance(take.get("expect_wake"), bool):
            out.append(f"{where}: expect_wake is true or false")
        slot = take.get("slot")
        if not isinstance(slot, dict) or set(slot) != {"start", "end"} or not all(_is_int(slot.get(k)) for k in ("start", "end")):
            out.append(f"{where}: slot is {{\"start\": N, \"end\": N}} in samples")
        else:
            start, end = slot["start"], slot["end"]
            if not 0 <= start < end:
                out.append(f"{where}: slot starts at 0 or later and ends after it starts")
            elif frames is not None and end > frames:
                out.append(f"{where}: slot ends at {end}, past the recording's {frames} samples")
            if start < last_end:
                out.append(f"{where}: slot starts at {start}, inside the slot before it (ends {last_end})")
            last_end = max(last_end, end)
        retake = take.get("retake_of")
        if retake is not None and (not isinstance(retake, str) or retake not in seen or retake == tid):
            out.append(f"{where}: retake_of names an earlier take of this recording, or is null")
        excl = take.get("exclude")
        if excl is not None:
            if (not isinstance(excl, dict) or set(excl) != {"reason", "date"}
                    or not isinstance(excl.get("reason"), str) or not excl.get("reason").strip()
                    or not isinstance(excl.get("date"), str) or not _DATE.match(excl.get("date"))):
                out.append(f"{where}: exclude is null or {{\"reason\": \"...\", \"date\": \"YYYY-MM-DD\"}}")


def _check_cassette(doc: dict, out: list[str]) -> None:
    _keys(doc, ("format", "turns"), "cassette", out)
    turns = doc.get("turns")
    if not isinstance(turns, list):
        out.append("cassette: turns is a list")
        return
    for i, turn in enumerate(turns):
        where = f"cassette turn {i + 1}"
        if not isinstance(turn, dict):
            out.append(f"{where}: not an object")
            continue
        _keys(turn, ("request", "fed_sha256", "fed_bytes", "partials", "final", "model_version",
                     "recorded", "ledger_entry", "source"), where, out)
        req = turn.get("request")
        if not isinstance(req, dict) or set(req) != {"provider", "model", "sample_rate", "query"}:
            out.append(f"{where}: request is provider, model, sample_rate and query")
        else:
            query = req.get("query")
            if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in query.items()):
                out.append(f"{where}: request.query maps text to text")
            elif any(re.search(r"key|token|auth|secret|pass", k, re.I) for k in query):
                out.append(f"{where}: request.query holds a field named like a credential")
            if not isinstance(req.get("provider"), str) or not _opt_str(req.get("model")) or not _is_int(req.get("sample_rate")):
                out.append(f"{where}: request's provider is text, model text or null, sample_rate a number")
        if not isinstance(turn.get("fed_sha256"), str) or not _HEX64.match(turn.get("fed_sha256") or ""):
            out.append(f"{where}: fed_sha256 is 64 lowercase hex digits")
        fed = turn.get("fed_bytes")
        if not _is_int(fed) or fed < 0:
            out.append(f"{where}: fed_bytes is a whole number")
            fed = None
        partials = turn.get("partials")
        if not isinstance(partials, list):
            out.append(f"{where}: partials is a list")
        else:
            last = 0
            for p in partials:
                if (not isinstance(p, dict) or set(p) != {"text", "after_bytes", "prefix_sha256"}
                        or not isinstance(p.get("text"), str) or not _is_int(p.get("after_bytes"))
                        or not isinstance(p.get("prefix_sha256"), str) or not _HEX64.match(p.get("prefix_sha256") or "")):
                    out.append(f"{where}: a partial is text, after_bytes and prefix_sha256")
                    continue
                if p["after_bytes"] < last or (fed is not None and p["after_bytes"] > fed):
                    out.append(f"{where}: partials come in order, inside the bytes fed")
                last = p["after_bytes"]
        final = turn.get("final")
        if (not isinstance(final, dict) or set(final) != {"text", "confidence", "error"}
                or not isinstance(final.get("text"), str) or not _opt_str(final.get("error"))
                or not (final.get("confidence") is None or _is_num(final.get("confidence")))):
            out.append(f"{where}: final is text, confidence and error")
        if not _opt_str(turn.get("model_version")):
            out.append(f"{where}: model_version is text or null")
        if not isinstance(turn.get("recorded"), str) or not _DATE.match(turn.get("recorded") or ""):
            out.append(f"{where}: recorded is a date, YYYY-MM-DD")
        if not (turn.get("ledger_entry") is None or _is_int(turn.get("ledger_entry"))):
            out.append(f"{where}: ledger_entry is the ledger's entry number, or null")
        if not isinstance(turn.get("source"), str) or not turn.get("source"):
            out.append(f"{where}: source says where the answer was recorded")


GOLDEN_WAKE_FIELDS = (
    "n", "take", "wake_end_sample", "lag_ms", "name_ms", "level_ms", "heard_ms", "cut_short",
    "handover_sha256", "fed_sha256", "final", "cassette",
)


def _check_golden(doc: dict, out: list[str]) -> None:
    _keys(doc, ("format", "recording", "sha256", "platform", "conditions", "footer", "wakes"), "golden", out)
    if not isinstance(doc.get("recording"), str) or not _ID.match(doc.get("recording") or ""):
        out.append("golden: recording is a manifest id")
    if not isinstance(doc.get("sha256"), str) or not _HEX64.match(doc.get("sha256") or ""):
        out.append("golden: sha256 is 64 lowercase hex digits")
    if not isinstance(doc.get("platform"), str) or not _PLATFORM.match(doc.get("platform") or ""):
        out.append("golden: platform is like win32-amd64 or linux-aarch64")
    cond = doc.get("conditions")
    if not isinstance(cond, dict):
        out.append("golden: conditions is an object")
    else:
        _keys(cond, ("threshold", "start_mean", "cmninit", "flags"), "golden conditions", out)
        if not isinstance(cond.get("flags"), list) or not all(isinstance(f, str) for f in cond.get("flags") or [None]):
            out.append("golden conditions: flags is a list of the replay's flags")
        for key in ("threshold", "start_mean"):
            if not isinstance(cond.get(key), str):
                out.append(f"golden conditions: {key} is text")
        if not _opt_str(cond.get("cmninit")):
            out.append("golden conditions: cmninit is the start mean's vector, or null")
    footer = doc.get("footer")
    if not isinstance(footer, dict):
        out.append("golden: footer is an object")
    else:
        _keys(footer, ("frames", "wakes", "turns", "name_cut"), "golden footer", out)
        for key in ("frames", "wakes", "turns"):
            if not _is_int(footer.get(key)) or footer.get(key) < 0:
                out.append(f"golden footer: {key} is a count")
        nc = footer.get("name_cut")
        if not (nc is None or (isinstance(nc, str) and _NAME_CUT.match(nc))):
            out.append('golden footer: name_cut is "N of M", or null when nothing was handed over')
    wakes = doc.get("wakes")
    if not isinstance(wakes, list):
        out.append("golden: wakes is a list")
        return
    for i, w in enumerate(wakes):
        where = f"golden wake {i + 1}"
        if not isinstance(w, dict):
            out.append(f"{where}: not an object")
            continue
        _keys(w, GOLDEN_WAKE_FIELDS, where, out)
        if w.get("n") != i + 1:
            out.append(f"{where}: n counts the wakes from 1, in order")
        if not _opt_str(w.get("take")):
            out.append(f"{where}: take is a take id, or null outside every slot")
        if not _is_int(w.get("wake_end_sample")) or w.get("wake_end_sample") < 0:
            out.append(f"{where}: wake_end_sample is a sample count")
        for key in ("lag_ms", "name_ms", "level_ms", "heard_ms"):
            if not (w.get(key) is None or _is_num(w.get(key))):
                out.append(f"{where}: {key} is a number, or null when nothing was handed over")
        if not isinstance(w.get("cut_short"), bool):
            out.append(f"{where}: cut_short is true or false")
        hs = w.get("handover_sha256")
        if not (hs is None or (isinstance(hs, str) and _HEX64.match(hs))):
            out.append(f"{where}: handover_sha256 is 64 hex digits, or null")
        fs = w.get("fed_sha256")
        if not (fs is None or (isinstance(fs, str) and _HEX64.match(fs))):
            out.append(f"{where}: fed_sha256 is 64 hex digits, or null when no turn was recorded")
        if not _opt_str(w.get("final")):
            out.append(f"{where}: final is text or null")
        if w.get("cassette") not in CASSETTE_HITS:
            out.append(f"{where}: cassette is hit, miss or null")


_CHECKERS = {
    "bank": _check_bank,
    "manifest": _check_manifest,
    "cassette": _check_cassette,
    "golden": _check_golden,
}


def problems(doc: Any, kind: str) -> list[str]:
    """Every problem with `doc` as a file of this kind; empty when it is sound."""
    if kind not in FORMATS:
        raise ValueError(f"no bank format is called {kind!r}")
    if not isinstance(doc, dict):
        return [f"{kind}: not a JSON object"]
    if doc.get("format") != FORMATS[kind]:
        return [f"{kind}: format is {doc.get('format')!r}, and this reads {FORMATS[kind]!r}"]
    out: list[str] = []
    _CHECKERS[kind](doc, out)
    return out


# ------------------------------------------------------------ files on disk


def read(path: Path | str, kind: str) -> dict[str, Any]:
    """The document at `path`, refused with every problem when it is unsound."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BankError(path, ["no such file"]) from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BankError(path, [f"not readable JSON: {exc}"]) from None
    found = problems(doc, kind)
    if found:
        raise BankError(path, found)
    return doc


def dump(doc: dict[str, Any]) -> str:
    """The text a bank file is written as: two-space indent, fields in the
    order given, UTF-8 kept as itself, one newline at the end, so a file
    written twice is byte for byte the same and a diff shows only change."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def write(path: Path | str, doc: dict[str, Any], kind: str) -> Path:
    """Write `doc` after checking it: an unsound document is never written."""
    found = problems(doc, kind)
    if found:
        raise BankError(path, found)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump(doc), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def manifests(root: Path | str) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Each manifest bank.json lists, in its order, as (path, document).
    Raises BankError on the first file that is missing or unsound; check.py
    reads them one at a time instead, to report every one."""
    root = Path(root)
    bank = read(root / "bank.json", "bank")
    for rel in bank["files"]:
        path = root / rel
        yield path, read(path, "manifest")


def find_audio(
    manifest: dict[str, Any],
    root: Path | str,
    search: Iterable[Path | str] = (),
    *,
    by_hash: bool = False,
) -> Path | None:
    """Where the recording is, or None.

    First the manifest's `path`, relative to the bank's root, when its
    sha256 matches. Then the file's name in each directory of `search`, in
    order, taking the first whose sha256 matches (FORMAT.md). With
    `by_hash`, last, every `.wav` directly inside those directories, so a
    copy kept under another name is found by its hash. A file whose hash
    differs is never returned: the hash is the recording's identity.
    """
    want = manifest["sha256"]
    first = Path(root) / manifest["path"]
    if first.is_file() and sha256_file(first) == want:
        return first
    name = Path(manifest["path"]).name
    dirs = [Path(d) for d in search]
    for d in dirs:
        candidate = d / name
        if candidate.is_file() and sha256_file(candidate) == want:
            return candidate
    if by_hash:
        for d in dirs:
            if not d.is_dir():
                continue
            for candidate in sorted(d.glob("*.wav")):
                if candidate.is_file() and sha256_file(candidate) == want:
                    return candidate
    return None


def take_at(manifest: dict[str, Any], sample: int) -> dict[str, Any] | None:
    """The take whose slot holds `sample` (start inclusive, end exclusive),
    or None outside every slot."""
    for take in manifest.get("takes") or []:
        slot = take.get("slot") or {}
        if slot.get("start", 0) <= sample < slot.get("end", -1):
            return take
    return None
