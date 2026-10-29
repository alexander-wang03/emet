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
- A speaker id is `s` and digits. `tools/bank/manifest.py` is strict: a
  field it does not know is a problem, so a typo cannot pass as a new
  field.
- `slot` is in samples from the recording's first sample: where the prompt
  for the take began and where the next one began. The words lie inside it.
- `distance` is the words the record gives ("near", "about 3 m");
  `distance_m` is a number only when one was measured.
- `exclude` is null, or `{"reason": "...", "date": "..."}`.

Labels are written by `label.py` to `<bank>/labels/<id>.json`, keyed by
take id, and never back into a manifest. Each label names the tool and
version that made it.

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
                            "interim_results": "true", "punctuate": "true",
                            "smart_format": "false"}},
      "fed_sha256": "3f2809...",
      "fed_bytes": 64000,
      "partials": [
        {"text": "Where", "after_bytes": 10560, "prefix_sha256": "9a1c..."}
      ],
      "final": {"text": "Where are my keys?", "confidence": 0.999, "error": null},
      "model_version": "2025-04-17.21547",
      "recorded": "2026-10-05",
      "ledger_entry": 9,
      "source": "releases/v0.5.3/harness/runs/dg09_.../deepgram.json wakes[0]"
    }
  ]
}
```

- `request` is what the plugin asked for: the whole query it sends, with
  `model` and `sample_rate` lifted out beside it, and never a key.
  `emet_providers.cassette.request_for()` builds it from a plugin's query.
  The mock takes as candidates only the turns whose `request` equals its
  `cassette_request` param, or every turn when that param is absent.
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

A change to a golden is made with `replay.py --update` and carried in the
pull request, one line per changed wake with its reason.

## The other files the tools write

| Format | Written by | What it holds |
|---|---|---|
| `emet-ledger/1` | `paid_replay.py`, `canary.py` | Every paid call, with its recording, query, tree and result; the monthly allowance (20 replay entries and one canary, D6, decided 2026-10-27) counted from `standing.since`. At `<bank>/ledger.json` |
| `emet-misses/1` | `replay.py --misses` | The turns no cassette held, with their bytes' hashes and lengths: the paid list |
| `emet-live-record/1` | the live-vendor tests, with `EMET_LIVE_RECORD=<dir>` | What a vendor sent, as it came, with the model it reported and the date; never a key or a header that carries one |
| `emet-labels/1` | `label.py` | Per take of one recording: the forced alignment, the onsets by level, the faster-whisper words with their times, and why a take was flagged; each label names the tool and model that made it. At `<bank>/labels/<id>.json` |
| `emet-canary/1` | `canary.py` | One canary's result: the live tests, and one cassette turn replayed live against its recording |
