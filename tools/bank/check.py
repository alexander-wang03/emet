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

A generated bank (FORMAT.md, "A generated bank": its speakers are `g` ids)
has no consent to hold. It is held to its own rules in its place:

    the voices       every speaker's voice record is the pinned one: its
                     own fields word for word and its files hash for hash
                     (`generated/voices.json`), and its MODEL_CARD lies
                     beside it with the pinned hash. Where the list stood
                     when the bank was built (the revision, the list's
                     commit, the day the cards were read, what spoke) is
                     held to its shape only, so a list that grows fails no
                     bank already built. A voice the list does not have is
                     refused
    the provenance   every recording says what made it, and every take has
                     its record: what was said, the sha256 of what the
                     voice gave back, the rate, and where it was placed. A
                     take with no record fails, and so does a record with
                     no take or a take whose audio leaves its slot
    the room         the manifest's room and distance in metres are its
                     record's; bank.json gives that room the record's size
                     and absorption; and the manifest's noise is the
                     record's in words and level, or none in both
    the store        DATASHEET.md and ATTRIBUTION.md are there, the second
                     naming every voice with its credit, its base's credit
                     and any term its dataset adds, and SHA256SUMS lists
                     every file with the hash it has

A bank is recorded or generated, never both: a real voice never enters a
store that is published.

It exits 1 on any problem, 0 when every recording is sound, so a script can
gate on it. Nothing is written. The standard library only.

**Why the hash.** A recording is never edited (FORMAT.md): a bank is
append-only, and every number replayed from it names the file by its
sha256. A file that changed under the same name would quietly move every
golden built on it. The bank lives off GitHub on purpose, so this is the
only check that the copy on the laptop and the copy on the reference body
are the same bank.

**Why the card.** piper1-gpl's `COMMERCIAL_USE.md` lists the voices whose
every dataset allows commercial use, and says a copy downloaded before a
voice was retrained is the old voice under the same name. So a voice is
pinned by the sha256 of its model, and its card's Training section must
name the base the list records. `card_problems` refuses a card whose
Training section says it is fine-tuned from lessac or ryan, the two bases
`COMMERCIAL_USE.md` excludes ("Everything fine-tuned from lessac or ryan",
read 2026-11-25); lessac is the base that kept Amy out of the reference
soul (CITATIONS.md).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifest as bank_files  # noqa: E402

__all__ = [
    "check", "check_recording", "voice_list", "voice_list_problems", "card_facts", "card_problems",
    "training_problems", "licence_allowed", "voice_record", "voice_record_problems", "generated_problems",
    "noise_problems", "noise_words", "attribution_problems", "terms_sound", "sums_problems", "is_generated",
]

#: The header fields compared with the manifest's.
WAV_FIELDS = ("sample_rate", "channels", "bits", "frames")

#: The pinned voices a generated bank may use.
VOICES = Path(__file__).resolve().parent / "generated" / "voices.json"
VOICE_LIST_FORMAT = "emet-generated-voices/1"
VOICE_FORMAT = "emet-voice/1"
GENERATED_FORMAT = "emet-generated/1"
SUMS = "SHA256SUMS"
STORE_FILES = ("DATASHEET.md", "ATTRIBUTION.md")

ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GENERATED_SPEAKER = re.compile(r"^g\d{2,}$")

#: What a card's License line may say, in lower case: the line is one of
#: these, or one of these and then a space and more. This is the bank's own
#: rule, narrower than E10 (a) (decided 2026-11-25), which admits every
#: voice piper1-gpl's COMMERCIAL_USE.md lists, CC BY-SA 4.0 ones among
#: them: each licence here allows commercial use and asks for no more than
#: credit. A dataset may add a term of its own beyond its licence, as the
#: Alba corpus does; the voice list records it under `terms`, and
#: ATTRIBUTION.md quotes it.
LICENCES = (
    "public domain", "cc0", "cc0 1.0 universal", "https://creativecommons.org/publicdomain/zero/1.0/",
    "cc by 4.0", "https://creativecommons.org/licenses/by/4.0/",
)
_NEVER = re.compile(r"\bnc\b|non-?commercial|research only|\bnd\b")
#: "Finetuned from ... lessac" within one clause. A card may still mention
#: lessac another way: the hifi_tts_92 base's says it replaces it.
_BAD_BASE = re.compile(r"fine-?tuned\s+from.{0,60}?\b(lessac|ryan)\b", re.S)


# ------------------------------------------------------------ voices, cards


def licence_allowed(licence: str) -> bool:
    """Whether a card's License line is one a generated bank may rest on."""
    low = " ".join(str(licence).lower().split())
    if _NEVER.search(low):
        return False
    return any(low == form or low.startswith(form + " ") for form in LICENCES)


def training_problems(training: str, base: str | None) -> list[str]:
    """Why a Training section does not say what the list records of a
    voice's base: None is trained from scratch, a name is fine-tuned from
    that base. A card that says fine-tuned from lessac or ryan is refused
    whatever the list records."""
    low = " ".join(str(training).lower().split())
    out = []
    if _BAD_BASE.search(low):
        out.append("the Training section says the voice is fine-tuned from lessac or ryan")
    if base is None:
        if "from scratch" not in low:
            out.append("the list records no base, and the Training section does not say trained from scratch")
    elif not re.search(r"fine-?tuned\s+from.{0,80}?\b" + re.escape(base.lower()) + r"\b", low, re.S):
        out.append(f"the Training section does not say the voice is fine-tuned from {base}")
    return out


def card_facts(card: str) -> dict[str, str | None]:
    """A MODEL_CARD's dataset, licence and Training section, as it words
    them, with runs of white space as one space."""
    dataset = re.search(r"^\* URL:\s*(.+)$", card, re.M)
    licence = re.search(r"^\* License:\s*(.+)$", card, re.M)
    training = card.split("## Training", 1)[1] if "## Training" in card else None
    return {
        "dataset": dataset.group(1).strip() if dataset else None,
        "licence": licence.group(1).strip() if licence else None,
        "training": " ".join(training.split()) if training is not None else None,
    }


def card_problems(card: str, voice: dict[str, Any]) -> list[str]:
    """Why a MODEL_CARD is not the card the list was written from: its
    dataset, its licence or its Training section reads otherwise, the
    licence is not one a bank may rest on, or the Training section does not
    name the base the list records."""
    facts = card_facts(card)
    out = []
    for key, name in (("dataset", "dataset URL"), ("licence", "License line"), ("training", "Training section")):
        if facts[key] is None:
            out.append(f"the card has no {name}")
        elif facts[key] != voice.get(key):
            out.append(f"the card's {name} reads {facts[key][:80]!r}, and the list records "
                       f"{str(voice.get(key))[:80]!r}")
    if facts["licence"] is not None and not licence_allowed(facts["licence"]):
        out.append(f"the card's licence, {facts['licence']!r}, is not one a generated bank may use")
    if facts["training"] is not None:
        out.extend(training_problems(facts["training"], voice.get("base")))
    return out


def _file_entry(entry: Any, where: str, prefix: str | None, out: list[str]) -> None:
    if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]
            or "/" in entry["name"] or "\\" in entry["name"]
            or not isinstance(entry.get("url"), str) or not isinstance(entry.get("sha256"), str)
            or not HEX64.match(entry["sha256"]) or not isinstance(entry.get("bytes"), int)):
        out.append(f"{where} is a name, a url, a sha256 and a size in bytes")
    elif prefix and not entry["url"].startswith(prefix):
        out.append(f"{where}'s url is not at the pinned revision, {prefix}")


def voice_list_problems(doc: Any) -> list[str]:
    """Every problem with a voice list (`generated/voices.json`)."""
    if not isinstance(doc, dict) or doc.get("format") != VOICE_LIST_FORMAT:
        return [f"voices: format is not {VOICE_LIST_FORMAT!r}"]
    out: list[str] = []
    if not isinstance(doc.get("read"), str) or not DATE.match(doc.get("read") or ""):
        out.append("voices: read is the date the cards were read, YYYY-MM-DD")
    engine = doc.get("engine")
    if not isinstance(engine, dict) or not all(isinstance(engine.get(k), str) and engine[k]
                                               for k in ("name", "version", "licence")):
        out.append("voices: engine names the tool that speaks, its version and its licence")
    if not isinstance(doc.get("list"), dict) or not isinstance(doc["list"].get("url"), str):
        out.append("voices: list gives the url of the list the voices were taken from")
    repository, revision = doc.get("repository"), doc.get("revision")
    prefix = None
    if not isinstance(repository, str) or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        out.append("voices: repository and revision pin where the files are, the revision a 40-digit commit")
    else:
        prefix = f"{repository}/resolve/{revision}/"
    bases = doc.get("bases")
    if not isinstance(bases, dict):
        out.append("voices: bases is an object, empty when no voice has a base")
        bases = {}
    for bid, base in bases.items():
        where = f"voices: base {bid}"
        if not isinstance(base, dict) or not all(isinstance(base.get(k), str) and base[k]
                                                 for k in ("name", "dataset", "licence", "training", "credit")):
            out.append(f"{where} gives its name, dataset, licence, training and credit")
            continue
        if not licence_allowed(base["licence"]):
            out.append(f"{where}: its licence, {base['licence']!r}, is not one a generated bank may use")
        out.extend(f"{where}: {p}" for p in training_problems(base["training"], None))
    voices = doc.get("voices")
    if not isinstance(voices, list) or not voices:
        return out + ["voices: voices lists at least one voice"]
    ids: set[str] = set()
    names: set[str] = set()
    for i, voice in enumerate(voices):
        where = f"voices: voice {i + 1}"
        if not isinstance(voice, dict):
            out.append(f"{where}: not an object")
            continue
        vid, name = voice.get("id"), voice.get("name")
        if isinstance(name, str) and name:
            where = f"voices: {name}"
        if not isinstance(vid, str) or not GENERATED_SPEAKER.match(vid) or vid in ids:
            out.append(f"{where}: id is g and digits, like g01, used once")
        if not isinstance(name, str) or not name or name in names:
            out.append(f"{where}: name is the voice's name, listed once")
        ids.add(str(vid))
        names.add(str(name))
        for key in ("describes", "dataset", "licence", "training", "listed", "credit"):
            if not isinstance(voice.get(key), str) or not voice[key].strip():
                out.append(f"{where}: {key} is text")
        if not terms_sound(voice.get("terms")):
            out.append(f"{where}: terms, when given, is the text a dataset adds to its licence, with its url and "
                       "the date it was read")
        files = voice.get("files")
        if not isinstance(files, dict) or set(files) != {"model", "config", "card"}:
            out.append(f"{where}: files are its model, its config and its card")
        else:
            for key in ("model", "config", "card"):
                _file_entry(files[key], f"{where}: the {key}", prefix, out)
        base = voice.get("base")
        if base is not None and base not in bases:
            out.append(f"{where}: its base, {base!r}, is not among the list's bases")
        if isinstance(voice.get("licence"), str) and not licence_allowed(voice["licence"]):
            out.append(f"{where}: its licence, {voice['licence']!r}, is not one a generated bank may use")
        if isinstance(voice.get("training"), str):
            out.extend(f"{where}: {p}" for p in training_problems(voice["training"], base))
    return out


def voice_list(path: Path | None = None) -> dict[str, Any]:
    """The pinned voice list, refused with every problem when unsound."""
    path = Path(path) if path else VOICES
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise bank_files.BankError(path, ["no such file"]) from None
    except (OSError, ValueError) as exc:
        raise bank_files.BankError(path, [f"not readable JSON: {exc}"]) from None
    found = voice_list_problems(doc)
    if found:
        raise bank_files.BankError(path, found)
    return doc


def terms_sound(terms: Any) -> bool:
    """Whether a voice's `terms` is absent, or the text its dataset adds to
    its licence with the url it was read at and the date."""
    return terms is None or (
        isinstance(terms, dict) and set(terms) == {"text", "url", "read"}
        and all(isinstance(terms[k], str) and terms[k].strip() for k in terms) and bool(DATE.match(terms["read"])))


#: The fields of a voice record that say where the list stood when the bank
#: was built: what spoke, where the files were pinned, the list they were
#: taken from and the day the cards were read. A list that gains a voice, or
#: moves to a later revision, moves these and changes no voice already used.
BUILT_FIELDS = ("engine", "repository", "revision", "list", "read")


def voice_record(voice: dict[str, Any], voices: dict[str, Any], engine: dict[str, Any] | None = None,
                 ) -> dict[str, Any]:
    """A bank's record of one voice (`voices/<id>.json`): the list's entry,
    with what spoke (`engine`, the engine that ran; None takes the list's),
    where the files are pinned, and its base's own entry. It stands where a
    person's consent file stands."""
    base = voice.get("base")
    return {
        "format": VOICE_FORMAT, "id": voice["id"], "name": voice["name"], "describes": voice["describes"],
        "engine": engine if engine is not None else voices["engine"], "repository": voices["repository"],
        "revision": voices["revision"], "list": voices["list"], "read": voices["read"], "files": voice["files"],
        "dataset": voice["dataset"], "licence": voice["licence"], "terms": voice.get("terms"),
        "training": voice["training"], "base": base,
        "base_record": voices["bases"][base] if base is not None else None,
        "listed": voice["listed"], "credit": voice["credit"],
    }


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    """What a voice record says of the voice itself: its own fields, its
    files by name, sha256 and size, and its base's facts with the card's
    hash. URLs carry the revision, so they are left to `BUILT_FIELDS`."""
    out = {k: v for k, v in record.items() if k not in BUILT_FIELDS and k not in ("files", "base_record")}
    files = record.get("files")
    out["files"] = ({k: {f: e.get(f) for f in ("name", "sha256", "bytes")} if isinstance(e, dict) else e
                     for k, e in files.items()} if isinstance(files, dict) else files)
    base = record.get("base_record")
    if isinstance(base, dict):
        base = {**base, "card": (base.get("card") or {}).get("sha256") if isinstance(base.get("card"), dict)
                else base.get("card")}
    out["base_record"] = base
    return out


def voice_record_problems(record: Any, entry: dict[str, Any], voices: dict[str, Any]) -> list[str]:
    """Why a bank's voice record is not the pinned one for `entry`: a field
    of the voice itself that differs from the list, or a field of
    `BUILT_FIELDS` that is not the shape the builder writes. The second
    kind is never held to today's list, which may have grown since."""
    want = voice_record(entry, voices)
    if not isinstance(record, dict):
        return ["the voice record is not an object"]
    have, pinned = _identity(record), _identity(want)
    keys = [k for k in want if k not in BUILT_FIELDS and have.get(k) != pinned.get(k)]
    extra = [f"unknown {k}" for k in record if k not in want]
    out = []
    if keys or extra:
        out.append(f"the voice record is not the pinned one for {entry['name']}: "
                   + ", ".join(keys + extra) + " differ(s) from the voice list")
    engine, listed = record.get("engine"), record.get("list")
    if not (isinstance(engine, dict) and all(isinstance(engine.get(k), str) and engine[k] for k in ("name", "version"))
            and isinstance(record.get("repository"), str) and record["repository"]
            and isinstance(record.get("revision"), str) and re.fullmatch(r"[0-9a-f]{40}", record["revision"])
            and isinstance(listed, dict) and isinstance(listed.get("url"), str)
            and isinstance(record.get("read"), str) and DATE.match(record["read"])):
        out.append("the voice record says what spoke, the repository and revision its files were pinned at, the "
                   "list they were taken from and the day the cards were read")
    return out


def noise_words(noise: dict[str, Any] | None) -> str | None:
    """A noise record as a manifest's `noise` says it, or None for none."""
    if not noise or not noise.get("rms"):
        return None
    return f"{noise['kind']} noise at {noise['level_dbfs']:g} dBFS"


def attribution_problems(text: str, entries: list[dict[str, Any]], voices: dict[str, Any]) -> list[str]:
    """Why ATTRIBUTION.md does not credit the voices a bank speaks with:
    each voice's name, its credit and any term its dataset adds, and the
    credit of the base it was fine-tuned from, word for word."""
    out = []
    for entry in entries:
        if entry["name"] not in text:
            out.append(f"ATTRIBUTION.md does not name {entry['name']}")
        if entry["credit"] not in text:
            out.append(f"ATTRIBUTION.md does not give {entry['name']}'s credit, {entry['credit'][:60]!r}")
        if entry.get("terms") and entry["terms"]["text"] not in text:
            out.append(f"ATTRIBUTION.md does not quote the term {entry['name']}'s dataset adds")
        base = entry.get("base")
        if base is not None and voices["bases"][base]["credit"] not in text:
            out.append(f"ATTRIBUTION.md does not give the credit of {entry['name']}'s base, {base}")
    return out


def noise_problems(noise: Any) -> list[str]:
    """Why a noise record is unsound. None is no noise. The rms, in samples,
    is what the builder uses; level_dbfs describes it, and must."""
    if noise is None:
        return []
    if (not isinstance(noise, dict) or noise.get("kind") != "pink" or not isinstance(noise.get("rms"), int)
            or isinstance(noise.get("rms"), bool) or noise["rms"] <= 0
            or not isinstance(noise.get("level_dbfs"), (int, float))):
        return ["noise is null, or pink noise with a whole rms above 0 and a level_dbfs"]
    if abs(20.0 * math.log10(noise["rms"] / 32768.0) - noise["level_dbfs"]) > 0.05:
        return [f"noise: an rms of {noise['rms']} is {20.0 * math.log10(noise['rms'] / 32768.0):.2f} dBFS, "
                f"and level_dbfs says {noise['level_dbfs']:g}"]
    return []


def is_generated(bank: dict[str, Any]) -> bool:
    """Whether a bank's speakers are generated voices."""
    return any(GENERATED_SPEAKER.match(str(sid)) for sid in bank.get("speakers") or {})


# ------------------------------------------------- a generated bank's rules


def _hex(value: Any) -> bool:
    return isinstance(value, str) and bool(HEX64.match(value))


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def generated_problems(doc: dict[str, Any], pinned: dict[str, Any] | None,
                       bank_room: dict[str, Any] | None = None) -> list[str]:
    """Why a generated recording's manifest does not say what made it.

    `pinned` is the list's entry for the manifest's speaker, or None when
    the list has no such voice, which the index reports. `bank_room` is
    bank.json's entry for the manifest's room, or None when it has none,
    which `check_recording` reports."""
    made = doc.get("generated")
    if not isinstance(made, dict):
        return ["no record of what made it: generated is missing"]
    if made.get("format") != GENERATED_FORMAT:
        return [f"generated: format is {made.get('format')!r}, and this reads {GENERATED_FORMAT!r}"]
    out: list[str] = []
    for key in ("seed", "part", "voice", "engine", "room", "noise", "resampler", "clipped", "inputs",
                "builder_sha256", "tools", "takes"):
        if key not in made:
            out.append(f"generated: no {key!r}")
    if not _whole(made.get("seed")):
        out.append("generated: seed is the whole number everything random came from")
    voice = made.get("voice")
    if not isinstance(voice, dict) or not isinstance(voice.get("name"), str) or not all(
            _hex(voice.get(f"{k}_sha256")) for k in ("model", "config", "card")):
        out.append("generated: voice is its name and the sha256 of its model, config and card")
    elif pinned is not None:
        if voice["name"] != pinned["name"]:
            out.append(f"generated: the voice is {voice['name']}, and speaker {doc.get('speaker')} is "
                       f"{pinned['name']} in the list")
        for key in ("model", "config", "card"):
            if voice[f"{key}_sha256"] != pinned["files"][key]["sha256"]:
                out.append(f"generated: the voice's {key} has sha256 {voice[f'{key}_sha256'][:12]}..., and the "
                           f"list pins {pinned['files'][key]['sha256'][:12]}...")
    engine = made.get("engine")
    if not isinstance(engine, dict) or not all(isinstance(engine.get(k), str) and engine[k]
                                               for k in ("name", "version")):
        out.append("generated: engine names what spoke and its version")
    room = made.get("room")
    if not isinstance(room, dict) or not (
            isinstance(room.get("id"), str)
            and all(isinstance(room.get(k), list) and len(room[k]) == 3 and all(_num(v) for v in room[k])
                    for k in ("size_m", "source_m", "microphone_m"))
            and all(_num(room.get(k)) for k in ("absorption", "length_s", "distance_m"))
            and _whole(room.get("taps")) and _hex(room.get("taps_sha256"))):
        out.append("generated: room gives its id, its size, where the talker and the microphone stood, its "
                   "absorption and the response made from them")
    else:
        if room["id"] != doc.get("room"):
            out.append(f"generated: the room is {room['id']}, and the manifest names {doc.get('room')}")
        if room["distance_m"] != doc.get("distance_m"):
            out.append(f"generated: the room puts the talker {room['distance_m']} m away, and the manifest says "
                       f"{doc.get('distance_m')}")
        if isinstance(bank_room, dict):
            for key in ("size_m", "absorption"):
                if bank_room.get(key) != room[key]:
                    out.append(f"generated: the room's {key} is {room[key]}, and bank.json gives {room['id']} "
                               f"{bank_room.get(key)}")
    noise = made.get("noise")
    found = noise_problems(noise)
    out.extend(f"generated: {p}" for p in found)
    if (noise is None) != (doc.get("noise") is None):
        out.append("generated: the manifest's noise and the record's do not agree on whether there is any")
    elif noise is not None and not found and doc.get("noise") != noise_words(noise):
        out.append(f"generated: the manifest says the noise is {doc.get('noise')!r}, and its record is "
                   f"{noise_words(noise)!r}")
    if not _whole(made.get("clipped")) or made.get("clipped", 0) < 0:
        out.append("generated: clipped counts the samples that hit full scale")
    if not isinstance(made.get("inputs"), dict) or not all(_hex(v) for v in (made.get("inputs") or {"": 0}).values()):
        out.append("generated: inputs gives the sha256 of each list the builder read")
    if not _hex(made.get("builder_sha256")):
        out.append("generated: builder_sha256 is the sha256 of the builder that ran")
    if not isinstance(made.get("tools"), dict) or not isinstance((made.get("tools") or {}).get("python"), str):
        out.append("generated: tools gives the versions that ran, python among them")
    records = made.get("takes")
    if not isinstance(records, dict):
        return out + ["generated: takes maps each take's id to what made it"]
    takes = {t.get("id"): t for t in doc.get("takes") or [] if isinstance(t, dict)}
    for tid in takes:
        if tid not in records:
            out.append(f"take {tid}: no record of what made it")
    for tid in records:
        if tid not in takes:
            out.append(f"generated: a record for {tid}, which is no take of this recording")
    for tid, take in takes.items():
        record = records.get(tid)
        if record is None:
            continue
        where = f"take {tid}"
        said = record.get("said") if isinstance(record, dict) else None
        if not isinstance(record, dict) or not isinstance(said, list) or not said:
            out.append(f"{where}: its record says what was said, piece by piece")
            continue
        spoke = 0
        for piece in said:
            if isinstance(piece, dict) and set(piece) == {"pause_s"} and _num(piece["pause_s"]) and piece["pause_s"] >= 0:
                continue
            if (isinstance(piece, dict) and set(piece) == {"say", "sha256", "sample_rate", "frames"}
                    and isinstance(piece["say"], str) and piece["say"].strip() and _hex(piece["sha256"])
                    and _whole(piece["sample_rate"]) and piece["sample_rate"] > 0
                    and _whole(piece["frames"]) and piece["frames"] > 0):
                spoke += 1
                continue
            out.append(f"{where}: a piece is a pause, or what was said with the sha256, rate and frames of what "
                       "the voice gave back")
        if not spoke:
            out.append(f"{where}: nothing was said in it")
        if not isinstance(record.get("phrase"), str) or not _num(record.get("length_scale")):
            out.append(f"{where}: its record names its phrase and its speaking rate")
        slot = take.get("slot") or {}
        at, n = record.get("at_sample"), record.get("samples")
        if not (_whole(at) and _whole(n) and n > 0 and _whole(slot.get("start")) and _whole(slot.get("end"))
                and slot["start"] <= at and at + n <= slot["end"]):
            out.append(f"{where}: its audio lies inside its slot, from at_sample for samples")
    return out


def sums_problems(root: Path) -> list[str]:
    """Why `SHA256SUMS` does not describe the bank's files: a line that is
    not a hash and a path, a file gone or changed, or a file never listed."""
    path = root / SUMS
    if not path.is_file():
        return [f"no {SUMS}: a generated bank lists every file with its sha256"]
    listed: dict[str, str] = {}
    out: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        digest, sep, rel = line.partition("  ")
        if not sep or not HEX64.match(digest) or not rel or rel.startswith("/") or ".." in rel.split("/"):
            out.append(f"{SUMS} line {n}: not a sha256, two spaces and a path inside the bank")
            continue
        listed[rel] = digest
    for rel, digest in listed.items():
        target = root / rel
        if not target.is_file():
            out.append(f"{SUMS}: {rel} is listed and is not there")
        elif bank_files.sha256_file(target) != digest:
            out.append(f"{SUMS}: {rel} is not the file that was listed")
    for target in sorted(root.rglob("*")):
        rel = target.relative_to(root).as_posix()
        if target.is_file() and rel != SUMS and rel not in listed:
            out.append(f"{SUMS}: {rel} is in the bank and is not listed")
    return out


def _generated_index(root: Path, bank: dict[str, Any], voices: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The index problems of a generated bank, and each speaker's pinned entry."""
    lines: list[str] = []
    pinned = {v["id"]: v for v in voices["voices"]}
    found: dict[str, Any] = {}
    for sid, who in bank["speakers"].items():
        if not GENERATED_SPEAKER.match(str(sid)):
            lines.append(f"speaker {sid}: a recorded voice in a generated bank; a bank is recorded or generated, "
                         "never both")
            continue
        entry = pinned.get(sid)
        if entry is None:
            lines.append(f"speaker {sid}: not in the voice list, so nothing says the voice may be used")
            continue
        found[sid] = entry
        path = root / who["voice"]
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            lines.append(f"speaker {sid}: no voice record at {who['voice']}")
            continue
        except (OSError, ValueError) as exc:
            lines.append(f"speaker {sid}: the voice record {who['voice']} is not readable JSON: {exc}")
            continue
        lines.extend(f"speaker {sid}: {p}" for p in voice_record_problems(record, entry, voices))
        card = path.parent / entry["files"]["card"]["name"]
        if not card.is_file():
            lines.append(f"speaker {sid}: no {card.name} beside the voice record; a voice travels with its card")
        elif bank_files.sha256_file(card) != entry["files"]["card"]["sha256"]:
            lines.append(f"speaker {sid}: {card.name} is not the card the list was written from")
        else:
            lines.extend(f"speaker {sid}: {p}" for p in card_problems(card.read_text(encoding="utf-8"), entry))
    for name in STORE_FILES:
        target = root / name
        if not target.is_file() or not target.read_text(encoding="utf-8").strip():
            lines.append(f"no {name}: a generated bank says what it is and whose voices it holds")
    attribution = root / "ATTRIBUTION.md"
    if attribution.is_file():
        lines.extend(attribution_problems(attribution.read_text(encoding="utf-8"), list(found.values()), voices))
    lines.extend(sums_problems(root))
    return lines, found


# ---------------------------------------------------------------- the check


def check_recording(path: Path, rel: str, bank: dict, root: Path, search: list[Path], by_hash: bool,
                    seen_ids: dict[str, str], pinned: dict[str, Any] | None = None,
                    ) -> tuple[str, list[str], Path | None, dict | None]:
    """(id, problems, audio, manifest) for one manifest the index lists.
    `pinned` is each generated speaker's entry in the voice list, when the
    bank is a generated one."""
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
    if pinned is not None:
        found.extend(generated_problems(doc, pinned.get(doc["speaker"]), bank["rooms"].get(doc["room"])))
    elif "generated" in doc:
        found.append("a generated recording in a recorded bank; a bank is recorded or generated, never both")
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


def check(root: Path, search: list[Path], by_hash: bool, voices: dict[str, Any] | None = None,
          ) -> tuple[list[str], int, int]:
    """(lines to print, recordings sound, recordings with problems).

    `voices` is the voice list a generated bank is held to; None reads the
    pinned one, `generated/voices.json`."""
    lines: list[str] = []
    try:
        bank = bank_files.read(root / "bank.json", "bank")
    except bank_files.BankError as exc:
        return [f"bank.json: {p}" for p in exc.problems], 0, 1
    bad_index = 0
    pinned: dict[str, Any] | None = None
    if is_generated(bank):
        try:
            voices = voices if voices is not None else voice_list()
        except bank_files.BankError as exc:
            return [f"the voice list: {p}" for p in exc.problems], 0, 1
        found = voice_list_problems(voices)
        if found:
            return [f"the voice list: {p}" for p in found], 0, 1
        index_lines, pinned = _generated_index(root, bank, voices)
        lines.extend(index_lines)
        bad_index += len(index_lines)
    else:
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
        mid, found, audio, doc = check_recording(root / rel, rel, bank, root, search, by_hash, seen_ids, pinned)
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
                 + (", generated" if pinned is not None else "")
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
