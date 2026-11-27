# The voice bank's files

The formats the bank tools read and write. The audio itself never enters
this repository: the bank lives on the maintainer's machines, at
`Project Emet/voicebank/` on the laptop and `~/voicebank/` on the reference
body (decided 2026-10-27). What the repository keeps is derived from it:
golden files of numbers and hashes, and the tools.

Every file is JSON in UTF-8. Every format names itself in a `format` field,
so a reader refuses a file it was not written for.

## The bank: `bank.json` and one manifest a recording

`<bank>/bank.json` is the index:

```json
{
  "format": "emet-bank/1",
  "version": "2026-10-27",
  "files": ["manifests/one-breath-0.5.2.json"],
  "rooms": {"unrecorded": {"description": "no record names the room"}},
  "speakers": {"s01": {"consent": "consent/s01.txt"}}
}
```

- `version` is the date of the last change. The bank is append-only: a
  recording is never edited, a correction is a new entry, and an exclusion
  carries its reason.
- A speaker is an id and never a name.

`<bank>/manifests/<id>.json` describes one recording and its takes:

```json
{
  "format": "emet-bank/1",
  "id": "one-breath-0.5.2",
  "path": "../recordings/one-breath-0.5.2.wav",
  "sha256": "6fb45bcd...",
  "sample_rate": 16000, "channels": 1, "bits": 16,
  "frames": 2080000, "duration_s": 130.0,
  "recorded": "2026-09-29", "start_time": null,
  "device": null, "capture_level": null,
  "room": "unrecorded", "position": null,
  "distance": "near", "distance_m": null, "facing": null,
  "speaker": "s01", "noise": null,
  "kind": "takes",
  "lead_s": 3.0, "prompt_every_s": 6.0,
  "made_with": "tools/one_breath_prompts.sh",
  "takes": [
    {
      "id": "one-breath-0.5.2/01",
      "kind": "number",
      "text": "hey emet, what's two plus two",
      "question": "what's two plus two",
      "first_sound": "w",
      "slot": {"start": 48000, "end": 144000},
      "expect_wake": true,
      "retake_of": null,
      "exclude": null
    }
  ]
}
```

- `path` is relative to the bank's root. A reader that does not find the
  file there looks for its name in the directories it was given and takes
  the first whose sha256 matches. The hash is the recording's identity.
- `kind` of a recording: `takes`, `talk` (speech with no wake phrase, for
  false wakes), `room_tone`, or `soak`.
- `kind` of a take: `wake_pause`, `one_breath`, `number`, `short`, `decoy`,
  `pause`, `long`, `talk`, `room_tone`, `room_response`.
- `first_sound` is the question's first phone in lower-case ARPAbet
  (`w`, `hh`, `th`); pocketsphinx's dictionary writes the same phones in
  upper case.
- A speaker id is `s` and digits for a person, `g` and digits for a
  generated voice (below). `tools/bank/manifest.py` is strict: a field it
  does not know is a problem, so a typo cannot pass as a new field.
- `slot` is in samples from the recording's first sample: where the prompt
  for the take began and where the next one began. The words lie inside it.
- `distance` is the words the record gives ("near", "about 3 m");
  `distance_m` is a number only when one was measured.
- `exclude` is null, or `{"reason": "...", "date": "..."}`.
- `made_with` says what prompted the recording. `record.py` writes
  `tools/bank/record.py, plan ID`, then `; says again ...` and
  `; again for ID` where they apply. `manifest.plan_of` reads the plan
  back, and `replay.py` takes a plan's recordings as one sitting.

Labels are written by `label.py` to `<bank>/labels/<id>.json`, keyed by
take id, and never back into a manifest. Each label names the tool and
version that made it.

## A generated bank

`tools/bank/generate.py` writes a second kind of bank in the same format:
speech made by Piper from public voices, played through simulated rooms,
that anybody may hold, rebuild and replay. It lives outside the
repository too (`Project Emet/genbank/`), and is to be published as a
release asset of a repository of its own, `emet-testaudio` (decision D8,
named 2026-11-26); until that store exists no pull request replays it.
Its goldens are public and live in
`tools/bank/golden/generated/<platform>/`.

**What it may decide.** Generated speech in a simulated room screens a
change: a golden that moved says look here. It never sets or moves a
default, never gives a recall or false-wake rate for a real room, never
fires the roadmap's wake-threshold trigger, and never counts as a run on a
body. A synthetic voice is no second voice, and a simulated room is no
second room.

**What marks a bank as generated.** Its speakers are `g` ids, and each
names a voice record where a person names a consent file:

```json
"speakers": {"g01": {"voice": "voices/g01.json"}}
```

A bank is recorded or generated, never both: `check.py` refuses an `s`
speaker in a generated bank and a `generated` field in a recorded one, so a
real voice never enters a store that is published. A sitting plan that
names a `g` speaker is refused by `record.py`: a generated voice is never
recorded. Each recording's id is `gen-<voice id>-<condition>-<part>`, as in
`gen-g01-near-1`.

**The voice record**, `voices/<voice id>.json`, format `emet-voice/1`,
holds the voice list's entry: the voice's name and what it speaks as; its
model, config and MODEL_CARD, each with its URL at the pinned revision, its
sha256 and its size; the dataset, licence and Training section as the card
states them, and `terms`, any sentence the dataset's own licence file adds
(null when none); its base and the base's own record when it was
fine-tuned; the line of piper1-gpl's `COMMERCIAL_USE.md` it is listed
under; and the credit its dataset asks for. `check.py` holds these word for
word and the files by name, sha256 and size. The record also says where
the list stood when the bank was built: the engine that spoke, the
repository and revision, the `COMMERCIAL_USE.md` commit and the day the
cards were read. Those are held to their shape only, so a list that gains
a voice or moves to a later revision fails no bank built before. The card
itself lies beside the record, `voices/<name>.MODEL_CARD`, since a voice
travels with its card.

**A manifest's `generated` field**, format `emet-generated/1`, says what
made the recording. A manifest of a `g` speaker has it and no other
manifest may:

| Field | What it holds |
|---|---|
| `seed`, `part` | The bank's seed and the part of the phrase list the recording says |
| `voice` | Its name and the sha256 of its model, config and card, held to the voice list |
| `engine` | What spoke: `piper-tts` and its version, its licence, onnxruntime's version, the arguments, both noise scales (0) and the silence between sentences |
| `room` | The room's id and condition, its size, absorption and length in seconds, where the talker and the microphone stood and the distance between, the speed of sound, the gain at 1 m, and the response made from them: its taps, its decay read back by Schroeder's integral, and its sha256 |
| `noise` | null, or pink noise's rms in samples, its level in dBFS, and the seed it came from |
| `resampler` | The windowed-sinc resampler's taps and pass band |
| `clipped` | Samples that hit full scale |
| `inputs`, `builder_sha256` | The sha256 of each list under `tools/bank/generated/`, with CRLF read as LF, and of `generate.py` itself |
| `tools` | The versions of Python and numpy that ran, and the platform |
| `takes` | Per take id: its phrase, its speaking rate, what was said piece by piece (each piece's text, the sha256 of the samples the voice gave back, their rate and frames, or a pause in seconds), and where the take lies, `at_sample` for `samples` |

A take with no record, a record with no take, and a take whose audio
leaves its slot are refused, and so is a manifest without the field: no
take without provenance. The manifest's `room` and `distance_m` are the
record's, bank.json's entry for that room has the record's size and
absorption, and the manifest's `noise` is the record's in words
(`pink noise at -55 dBFS`), or null in both.

**The lists**, under `tools/bank/generated/`, are the builder's only
inputs:

| File | Format | What it holds |
|---|---|---|
| `voices.json` | `emet-generated-voices/1` | The pinned voices: the Hugging Face revision, the `COMMERCIAL_USE.md` commit they were taken from, the date the cards were read, the engine, the bases, and each voice's files and card facts |
| `rooms.json` | `emet-generated-rooms/1` | The rooms (size, absorption, response length), the conditions (room, microphone and talker positions, distance in words, noise), the speed of sound and the gain at 1 m |
| `phrases.json` | `emet-generated-phrases/1` | The phrases in groups, each group with its take kind, its slot length, and where in this repository its texts already are |
| `sets.json` | `emet-generated-sets/1` | The bank's version and seed, the parts, the lead, the speaking rates, and each set's rule |

**Two sets from one rule.** `full` (`every-cell`) is every voice in every
condition saying every part. `small` (`one-a-condition`) is one recording a
condition, its part and its voice stepping with the condition, so it says
every phrase once and each of its recordings is one of `full`'s, byte for
byte.

**The store's files**, beside `bank.json`: `audio/`, `manifests/`,
`voices/`, `DATASHEET.md` (what the bank is, is for and is not for, how it
was made and how to rebuild and replay it), `ATTRIBUTION.md` (every voice
and base, with the credit its dataset asks for and any term it adds), the
store's cassettes under `cassettes/` once the lead's paid run has made
them, and `SHA256SUMS`, every other file with its sha256 as `sha256sum -c`
reads it. A file added to a built bank, a cassette among them, is taken
into the sums by `generate.py --sums BANK`, which reads each file under
`cassettes/` as a cassette first and checks the bank after.
`generate.py --pack` makes the release asset,
`genbank-<folder>-<version>.tar.gz` with a `.sha256` line beside it, the
same bytes from the same files: the folder is the set's name for a set
built as `genbank/<set>/`, which is the name CI's store takes. `--fetch URL
--sha256 HEX --to DIR` takes it back, keeping it only when the hash
matches and the bank passes `check.py`.

CI takes each set back from the `url` and `sha256` in
`tools/bank/generated/store.json` (`emet-genbank-store/1`), empty until
the store is published. When a set's bank carries a `cassettes/` folder,
listed in `SHA256SUMS` like every file, the bank jobs replay with it.

**The clip** under `emet-hal/tests/clips/` is one take made the same way,
with its record beside it, format `emet-clip/1`: the file's sha256 and
size, the text, where the phrase starts, the voice and its hashes, the
engine, the room, the noise and its seed, the builder's and the lists'
hashes, and the command that made it.

## What the mock transcriber records: `record_turns`

With `models.stt.params.record_turns: <dir>` the mock transcriber writes
each utterance's bytes, exactly as fed, to `<dir>/turn-0001.raw` and on,
and appends one line to `<dir>/turns.jsonl` when the utterance finishes:

```json
{"turn": 1, "bytes": 64000, "feed_sizes": [5440, 2560, 2560],
 "fed_sha256": "3f2809...", "cassette": "hit", "final": "Where are my keys?",
 "partials": 3}
```

`cassette` is `"hit"`, `"miss"`, or null without a cassette. The replay
runner derives everything else from these bytes, the handover's hash
included (below, under the golden files).

## Cassettes: a transcriber's recorded answers

A cassette is what a real transcriber said for exact bytes, so a replay can
give the same answer again for nothing:

```json
{
  "format": "emet-cassette/1",
  "turns": [
    {
      "request": {"provider": "deepgram", "model": "nova-3",
                  "sample_rate": 16000,
                  "query": {"channels": "1", "encoding": "linear16",
                            "interim_results": "true", "mip_opt_out": "true",
                            "punctuate": "true", "smart_format": "false"}},
      "fed_sha256": "3f2809...",
      "fed_bytes": 64000,
      "partials": [
        {"text": "Where", "after_bytes": 10560, "prefix_sha256": "9a1c..."}
      ],
      "final": {"text": "Where are my keys?", "confidence": 0.999, "error": null},
      "model_version": "2025-04-17.21547",
      "recorded": "2026-11-26",
      "ledger_entry": 11,
      "source": "misses.json turn two-plus-two-0.5.2/1"
    }
  ]
}
```

- `request` is what the plugin asked for: the whole query it sends, with
  `model` and `sample_rate` lifted out beside it, and never a key.
  `emet_providers.cassette.request_for()` builds it from a plugin's query.
  The mock takes as candidates only the turns whose `request` equals its
  `cassette_request` param, or every turn when that param is absent.
- `query` holds `"mip_opt_out": "true"` in every turn recorded from 0.5.6
  on, since the plugin sends it with every Deepgram request. A turn without
  it predates the opt-out key: it still loads, answers no request the
  plugin makes now, and a miss on its bytes says so with the day it was
  recorded (`Played.reason`).
- `keyterm` is present only for a body with `params.keyterms`. It is the
  one query value that is a list, the terms in the order sent, each
  non-empty text, and the one name holding `key` a cassette keeps. A turn
  recorded with one list never answers a request with another, or with
  none.
- Every field shown is required and no other is allowed; `after_bytes`
  rises strictly from one partial to the next.
- A partial is given on the `feed()` call after which the bytes fed reach
  `after_bytes`, when the sha256 of every byte fed so far equals
  `prefix_sha256`. `after_bytes` is always the total at the end of a
  `feed()` call, so it is checked once.
- On `finish()`, a turn whose `fed_sha256` and `fed_bytes` match gives its
  final. Anything else is a miss: an empty final whose `error` reads
  `cassette miss`, and the runner puts those bytes on the paid list.
- Cassettes hold what the maintainer said, so they live in the bank
  (`<bank>/cassettes/`), never in the repository.

## Golden files: `tools/bank/golden/<platform>/<id>.json`

What a replay of one bank recording must reproduce, exactly, on one
platform (`win32-amd64`, `linux-x86_64`, `linux-aarch64`):

```json
{
  "format": "emet-golden/1",
  "recording": "one-breath-0.5.2", "sha256": "6fb45bcd...",
  "platform": "win32-amd64",
  "conditions": {"threshold": "1e-15", "start_mean": "carried", "cmninit": "37.7991,...",
                 "flags": ["--replay", "--transcribe", "--stats"]},
  "footer": {"frames": 1625, "wakes": 10, "turns": 10, "name_cut": "4 of 10"},
  "wakes": [
    {"n": 1, "take": "one-breath-0.5.2/02", "lag_ms": 170, "name_ms": 0,
     "level_ms": 0, "heard_ms": 170, "cut_short": false,
     "handover_sha256": "...", "fed_sha256": "..."}
  ]
}
```

- `conditions.start_mean` is `carried` (the mean the reference body's
  state file carried on 2026-09-30, the default), `pi-manifest` (the
  0.4-era mean in the Pi's manifests) or `default` (pocketsphinx's own);
  `cmninit` gives the first two in full. Each wake also records `wake_end_sample`, `final` and
  `cassette`; a wake that handed nothing over has null figures, and a file
  with no handover has a null `footer.name_cut`.
- `handover_sha256` hashes the frames handed over with the wake, the
  silence written at their front included: the bytes the transcriber was
  given first.
- Goldens are written without a cassette, so `final` is empty and no word
  anyone said reaches the repository. A run with a cassette is held to the
  golden's audio figures and reports its finals beside them.
- A recording with no golden for the platform fails a compare, as
  `NO GOLDEN`. One case is named and held to nothing: a recording whose
  `made_with` reads `tools/bank/record.py, plan ID`, while no recording of
  that plan has a golden on any platform. It is replayed, printed as
  `UNRELEASED`, and listed in the total line and in the report. The first
  golden written for any recording of the plan, on any platform, holds
  every recording of the plan from then on.
- `--report FILE` lists `held`, the ids compared with their golden, and
  `unreleased`, the ids named and held to nothing. A compare that held no
  recording to a golden exits 1.

A change to a golden is made with `replay.py --update` and carried in the
pull request, one line per changed wake with its reason.

The generated bank's goldens are the same format in a folder of their own,
`tools/bank/golden/generated/<platform>/`, named `gen-...` so no name meets
one of bank v0's; `replay.py BANK --golden tools/bank/golden/generated`
holds a generated bank to them, and refuses a generated bank given bank
v0's folder. They are public: the bank holds no recording of a person.
win32-amd64's were made on 2026-11-26; another platform's are written by
its first replay with `--update --platform NAME` and come back in a pull
request, and until then a replay there holds the bank to nothing.

## The other files the tools write

| Format | Written by | What it holds |
|---|---|---|
| `emet-ledger/1` | `paid_replay.py`, `canary.py` | Every paid call, with its recording, query, keyterms, tree and result; the allowance in its own `"monthly": {"replay_minutes": N, "canaries": N}` (1,000 minutes of replay audio a month counted from each replay entry's `audio_s`, and one canary, the maintainer, 2026-11-24, replacing D6's 20 entries of at most 130 s), counted from `standing.since`, with `standing.vendor_turns_a_release` (200). At `<bank>/ledger.json` |
| `emet-misses/1` | `replay.py --misses` | The turns no cassette held, with their bytes' hashes and lengths: the paid list |
| `emet-live-record/1` | the live-vendor tests, with `EMET_LIVE_RECORD=<dir>` | What a vendor sent, as it came, with the model it reported and the date; never a key or a header that carries one |
| `emet-labels/1` | `label.py` | Per take of one recording: the forced alignment, the onsets by level, the faster-whisper words with their times, and why a take was flagged; each label names the tool and model that made it. At `<bank>/labels/<id>.json` |
| `emet-canary/1` | `canary.py` | One canary's result: the live tests, and one cassette turn replayed live against its recording |
