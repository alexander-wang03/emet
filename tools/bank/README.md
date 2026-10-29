# The voice bank's tools

The voice bank is recordings of real speech, each with a manifest that says
what was said where. It is replayed through the engine to check, on every
change, that the robot still hears what it heard before. The audio never
enters this repository: the bank lives at `Project Emet/voicebank/` on the
laptop and `~/voicebank/` on the reference body (decision D4, 2026-10-27).
The repository keeps the tools and what they derive, golden files of numbers
and hashes. `FORMAT.md` is every file's format.

| Tool | Does | Needs |
|---|---|---|
| `manifest.py` | reads, checks and writes the bank's files; the one reader every tool shares | the standard library |
| `check.py` | checks a bank: index, manifests, hashes, slots, consent | the standard library |
| `record.py` | prompts and records a sitting on the reference body | the standard library, `arecord` |
| `label.py` | labels takes: energy, forced alignment, a whisper transcript, flags | a tools venv (`requirements.txt`) |
| `replay.py` | replays the bank through `emet-listen`, against the goldens | the repo's venv |
| `seed_cassettes.py` | turns paid Deepgram replays already made into cassettes | the repo's venv |
| `paid_replay.py` | replays the turns no cassette holds through Deepgram, on the ledger | the repo's venv, a key |
| `canary.py` | each month, checks the cassettes still say what Deepgram says | the repo's venv, a key |

Paths below are from `emet/`, with `PY` the repo's venv:
`PY=emet-sdk/.venv/Scripts/python.exe` on the laptop.

## Check a bank

```sh
"$PY" tools/bank/check.py ../voicebank
python3 tools/bank/check.py ~/voicebank --search ~/voicebank/audio    # on the Pi
python3 tools/bank/check.py ~/voicebank --search ~ --by-hash          # copies kept under other names
```

One line a recording, `ok` or its problems, then a total. Exit 1 on any
problem. A recording is found at its manifest's `path`, or by name in a
`--search` directory; `--by-hash` also takes any `.wav` there whose sha256
matches. The hash is the recording's identity, so a file whose hash differs
is never used.

## Record and label (bank v1)

```sh
# On the reference body, over `ssh -t`, from a copy of this tree:
python3 tools/bank/record.py tools/bank/plans/trial-30s.json --dry-run
python3 tools/bank/record.py tools/bank/plans/v1-sitting-1.json
# On the laptop, in a tools venv made from requirements.txt:
<tools-venv>/python tools/bank/label.py --bank ../voicebank
```

`record.py` starts `arecord` itself with a fixed `-d`, shows each prompt when
the recording reaches it, writes each take's slot into the file's manifest
as it goes, plays no cue, and hashes each file when it closes. A plan is a
sitting: `plans/v1-sitting-1.json` (room A, near and about 3 m) and
`plans/v1-sitting-2.json` (numbers, short answers, pauses and long turns;
a TV, facing away, about 5 m; room B), each about 29 minutes of audio, and
`plans/trial-30s.json`, which tries the prompter on one 30 s file first.
Before each file it says where to stand: Enter starts it, `s` skips it, `q`
ends the sitting. While a file records, `r` asks the take on screen again
at the end of the file and `p` the one before. Ctrl-C stops the prompts,
and the recording runs to its fixed length. Running a plan again resumes
it: a file already recorded is never recorded again. It refuses to start
without a terminal, without `arecord`, without `bank.json` listing the
plan's speaker with a consent file, or while the body runner holds
`~/emet-ci/lock`, and it holds that lock for the sitting.

```sh
python3 tools/bank/record.py PLAN --check          # the plan as a sheet to print, for 3 m
python3 tools/bank/record.py PLAN --dry-run        # the schedule and manifests against a stub; opens no device
python3 tools/bank/record.py PLAN --close FILE_ID  # close a manifest a killed prompter left open
```

`label.py` writes `<bank>/labels/<id>.json` and never a manifest, and prints
the takes a person should read. `--no-whisper` labels by level and
alignment only, which needs pocketsphinx alone. The first run downloads the
model, `Systran/faster-whisper-small.en` at a pinned revision, about 480 MB.
Both say more in their own docstrings.

## Replay against the goldens

```sh
"$PY" tools/bank/replay.py ../voicebank                     # compare; exit 1 on any difference
"$PY" tools/bank/replay.py ../voicebank --only stop-0.5.3   # one recording
"$PY" tools/bank/replay.py ../voicebank --update            # write tools/bank/golden/<platform>/
```

Each recording is replayed in a process of its own through
`emet_engine.cli.main` with `--replay --transcribe --stats`, the null sink,
pocketsphinx from the carried start mean (`--start-mean` names another),
an empty keys file, a scratch state directory and the mock transcriber with
`record_turns`. `sounddevice` cannot be imported there and every connection
or lookup that is not loopback is refused; a run that tries one fails.
Per wake the golden holds the take, the wake's frame end, the lag, the name
cut, the level stop's cut, the audio handed over, whether it was cut short,
the handover's sha256 and the turn's; per file, the footer's counts. Every
handover is rebuilt from the recording and its printed figures and must
equal what was fed. A changed wake prints one line, and that line, with its
reason, goes in the pull request. Goldens are per platform
(`win32-amd64`, `linux-aarch64`, `linux-x86_64`); on 2026-10-27 the
laptop's and the reference body's agreed in every field of all 115 wakes.

A replay is never a hardware result (`RELEASING.md` §6): a number from it
names its platform, commit, bank version and start mean.

## Cassettes and paid replays

```sh
# A replay answered from the bank's cassettes, under the request the
# shipped Deepgram plugin makes (--cassette-request JSON names another):
"$PY" tools/bank/replay.py ../voicebank --cassette ../voicebank/cassettes \
    --misses ../voicebank/misses/2026-10-28/misses.json

# What the turns no cassette held would cost, spending nothing:
"$PY" tools/bank/paid_replay.py ../voicebank/misses/2026-10-28/misses.json --ledger ../voicebank/ledger.json \
    --cassette ../voicebank/cassettes/paid-2026-10-28.json --dry-run

# Spending one entry of the month's allowance:
"$PY" tools/bank/paid_replay.py ../voicebank/misses/2026-10-28/misses.json --ledger ../voicebank/ledger.json \
    --cassette ../voicebank/cassettes/paid-2026-10-28.json --keys ~/.config/emet/keys.env --expect-spent 10
```

A cassette is what Deepgram said for exact bytes under one request
(`emet_providers.cassette`). A replay takes only the turns recorded under
its request, by default the shipped plugin's, so the 0.5.3 replays made
with `smart_format=true` never answer for today's plugin, which sends it
false. A turn whose bytes changed misses, is listed
and never failed, and its bytes go on the miss list: that list is the paid
list. `paid_replay.py` sends those bytes through the real plugin and writes
what came back as a new cassette. It refuses before spending: a request URL
other than `wss://api.deepgram.com/v1/listen` with the expected query
(checked offline, nothing started), a query key the plugin fixes or one
named like a credential, more than 130 s of audio in one entry, a ledger
that does not read, `--expect-spent` that is not the ledger's count, and a
month whose 20 replay entries are spent (D6, decided 2026-10-27; anything
more asks the maintainer first), a keys file with no Deepgram key, and no
`websockets`. The entry is written and read back before
anything connects, and only Deepgram's host is reachable after it.

`seed_cassettes.py` made the bank's first nine cassettes, 75 turns, from
nine of the ten replays the 0.5.3 ledger paid for
(`releases/v0.5.3/harness/runs/dg02_*` to `dg10_*`; dg01 was logged only as
text and kept no hashes of its bytes): it replays the same recording through the mock
at settings that feed the same bytes, twice, the second time with the paid
run's partials so the endpointer ends each turn where it did, and keeps a
turn only when every byte matches.

`canary.py` runs once a month: the four live-vendor tests and one cassette
turn replayed live. A new model version or a changed final marks the
cassettes stale. It spends the month's canary, so without `--spend` it runs
only the checks that need no network and spends nothing.

## Where things live

| What | Where | In git |
|---|---|---|
| tools, formats, plans, goldens | `emet/tools/bank/` | yes |
| recordings | `Project Emet/recordings/`, `voicebank/audio/` on the Pi | never |
| index, manifests, consent | `voicebank/bank.json`, `manifests/`, `consent/` | never |
| labels, cassettes, miss lists, the ledger | `voicebank/labels/`, `cassettes/`, `misses/`, `ledger.json` | never |

## Tests

```sh
"$PY" -m pytest -q -p no:cacheprovider tools/tests
```

A test that needs the real bank skips without it; `EMET_BANK` names the
bank's root and `EMET_BANK_SEARCH` more directories for its audio, separated
as `PATH` is.
