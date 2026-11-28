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
| `check.py` | checks a bank: index, manifests, hashes, slots, consent; a generated bank's voices, provenance and sums | the standard library |
| `generate.py` | builds the generated bank from public voices and simulated rooms; packs and fetches it | the standard library, numpy to be fast; Piper in a venv of its own, run and never imported |
| `record.py` | prompts and records a sitting on the reference body | the standard library, `arecord` |
| `sweep.py` | writes the sine sweep a phone plays for sitting 2's room responses, outside the repository | the standard library |
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
# On the laptop, from emet/, with earbuds or headphones in: the tones once, to set the volume, then the sitting.
python tools/bank/sit.py --test-tone
python tools/bank/sit.py tools/bank/plans/v1-sitting-1.json
# On the laptop, in a tools venv made from requirements.txt:
<tools-venv>/python tools/bank/label.py --bank ../voicebank
```

`sit.py` runs on the laptop. It starts `record.py` on the reference body
over `ssh -t`, with the terminal passed straight through, and follows the
prompter's cue file over a second SSH connection, playing a soft tick at
each 3, 2, 1 and a higher tone at each NOW on the laptop. Wear earbuds or
headphones: through the laptop's speakers the tones are in the recording.
A cue that arrives late is dropped, so a stall never sounds as one burst; when the
cue connection ends, two low tones sound, the terminal says so and it is
started again; a tone that fails to play is said at once. `sit.py` refuses
a dry run, whose cues would all come at once: `--test-tone` tries the
tones. The terminal bell of `record.py --bell` still works for a terminal
that rings, and went unheard in the first sitting, 2026-11-25. That
sitting recorded `v1s1-02-a-near-wake-1` at 6 s a take; resuming the plan
skips it, so record it again first with `--again v1s1-02-a-near-wake-1`.

`record.py` starts `arecord` itself with a fixed `-d`, shows each prompt when
the recording reaches it, writes each take's slot into the file's manifest
as it goes, plays nothing on the Pi, and hashes each file when it closes.
Before each prompt it counts in 3, 2, 1, a second apart, on a line under
the take on screen, which stays until the next NOW, and shows the take after
it as "next:". A take said in one breath gets 6 s; a take with a pause,
wake_pause among them, 8 s. `--check` prints the plan as a sheet with each
take numbered through the sitting, and each screen shows its take's number.
A plan is a sitting: `plans/v1-sitting-1.json` (room A, near and about 3 m;
31 minutes of audio) and `plans/v1-sitting-2.json` (numbers, short answers,
pauses and long turns; a TV, facing away, about 5 m; room B; a block with
the gain control off that holds the room responses; 33 min 40 s),
and `plans/trial-30s.json`, which tried the prompter on one file first, 38 s
since its wake_pause take has 8 s. Before each file it says where to stand:
Enter starts it, `s` skips it, `q` ends the sitting. While a file records,
`r` asks the take on screen again at the end of the file and `p` the one
before. Ctrl-C stops the prompts, and the recording runs to its fixed
length. Running a plan again resumes it: a file already recorded is never
recorded again; `--again FILE_ID` records it once more as `FILE_ID-2` (then
`-3`), and once that file closes it excludes the takes of the one it
replaces, whose audio and manifest stay. It refuses to start without a
terminal, without `arecord`, without `bank.json` listing the plan's speaker
with a consent file, or while the body runner holds `~/emet-ci/lock`, and it
holds that lock for the sitting.

```sh
python3 tools/bank/record.py PLAN --check          # the plan as a sheet to print, for 3 m
python3 tools/bank/record.py PLAN --dry-run        # the schedule and manifests against a stub; opens no device
python3 tools/bank/record.py PLAN --close FILE_ID  # close a manifest a killed prompter left open
python3 tools/bank/record.py PLAN --again FILE_ID --dry-run       # what --again would do; changes nothing
python3 tools/bank/record.py PLAN --cues FILE      # a line at each count and NOW, which sit.py sounds
python tools/bank/sit.py PLAN --again FILE_ID      # on the laptop: record a file again, with the tones
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

A recording with no golden fails the compare. A sitting still being
recorded is the one case held to nothing: while no recording of its plan
has a golden on any platform, each is replayed, printed as `UNRELEASED`
and named in the total line. `--update` on such a recording writes its
golden and says that the plan is held from then on, on every platform, so
a sitting's goldens are written on the laptop and on the body in one
change. A compare that held no recording to a golden exits 1.

A replay is never a hardware result (`RELEASING.md` §6): a number from it
names its platform, commit, bank version and start mean.

## The generated bank

A second bank that anybody may hold: speech made by Piper from public
voices (`generated/voices.json`: LJ Speech's voice and three others whose
every dataset allows it, each pinned by sha256), played through simulated
rooms (`generated/rooms.json`), saying phrases whose text is already in
this repository (`generated/phrases.json`). Once its store is published
(decision D8, `emet-testaudio`), it screens a change on every pull request
and every week without the maintainer's voice; until then no pull request
replays it, and `python tools/ci.py bank` says it was not run. **It never
sets or moves a default**: a synthetic voice is no second voice and a
simulated room is no second room, so its numbers never stand for a body or
a real room. `FORMAT.md`, "A generated bank", has its files.

```sh
python tools/bank/generate.py --plan                    # both sets' recordings and takes; needs no voice
"$PY" tools/bank/check.py ../genbank/small              # its voices, provenance and sums, as for any bank
"$PY" tools/bank/replay.py ../genbank/small --golden tools/bank/golden/generated
```

To build it, Piper goes in a venv of its own (GPL-3.0, CLAUDE.md), and the
builder runs it as a child process. numpy makes the room and the resampler
fast and gives the same integers as the plain Python the tests use:

```sh
python -m venv ../genbank-piper && ../genbank-piper/bin/python -m pip install piper-tts==1.8.0 onnxruntime==1.31.1
python -m venv ../genbank-tools && ../genbank-tools/bin/python -m pip install numpy==2.5.5
../genbank-tools/bin/python tools/bank/generate.py --fetch-voices ../genbank-voices
../genbank-tools/bin/python tools/bank/generate.py ../genbank/small --set small --voices ../genbank-voices --piper ../genbank-piper/bin/python
python tools/bank/generate.py --compare ../genbank/small OTHER_BUILD
```

`small` is one recording a condition, 75 takes, 9.1 minutes: every phrase
once, for every pull request. `full` is every voice in every condition,
100 recordings and 181 minutes, for the weekly run. Both come from
`generated/sets.json` by `generate.py`'s `plan`, and each small recording
is one of full's, byte for byte. Everything after the voice is whole-number
arithmetic, so the same utterances give the same bytes on any machine;
Piper runs with both noise scales at 0, and gave the same samples twice on
the laptop (2026-11-26).

`--pack BANK --to DIR` writes the release asset and its `.sha256`;
`--fetch URL --sha256 HEX --to DIR` takes it back, keeps it only when the
hash matches, and unpacks it only when every member lies inside it and the
bank passes `check.py`. `--sums BANK` writes a built bank's `SHA256SUMS`
again after a file is added, such as the store's cassettes under
`cassettes/`, and checks the bank. `--clip FILE` writes the wake clip of
`emet-hal/tests/clips/`. The goldens exist for win32-amd64 (2026-11-26);
on another platform every recording prints `NO GOLDEN` until that
platform's first replay writes them with `--update --platform NAME` for a
pull request.

The pull request after a bank version is published fills in that set's
`url` and `sha256` in `generated/store.json`; until then
`python tools/ci.py bank` and `bank-full` say they were not run.

## Cassettes and paid replays

```sh
# A replay answered from the bank's cassettes, under the request the
# shipped Deepgram plugin makes (--cassette-request JSON names another):
"$PY" tools/bank/replay.py ../voicebank --cassette ../voicebank/cassettes \
    --misses ../voicebank/misses/2026-10-28/misses.json

# What the turns no cassette held would cost, spending nothing:
"$PY" tools/bank/paid_replay.py ../voicebank/misses/2026-10-28/misses.json --ledger ../voicebank/ledger.json \
    --cassette ../voicebank/cassettes/paid-2026-10-28.json --dry-run

# Spending the miss list's minutes of the month's allowance:
"$PY" tools/bank/paid_replay.py ../voicebank/misses/2026-10-28/misses.json --ledger ../voicebank/ledger.json \
    --cassette ../voicebank/cassettes/paid-2026-10-28.json --keys ~/.config/emet/keys.env --expect-spent 10
```

A cassette is what Deepgram said for exact bytes under one request
(`emet_providers.cassette`). A replay takes only the turns recorded under
its request, by default the shipped plugin's, so the 0.5.3 replays made
with `smart_format=true` never answer for today's plugin, which sends it
false. From 0.5.6 the plugin sends `mip_opt_out=true` with every request,
so no turn of bank v0's nine cassettes answers it, and every turn misses
until the lead's reseed of 0.5.6 writes the reseeded turns to a new
cassette (`--cassette-request` with 0.5.5's request still answers from the
old ones). A turn whose bytes changed misses, is listed
and never failed, and its bytes go on the miss list: that list is the paid
list. `paid_replay.py` sends those bytes through the real plugin and writes
what came back as a new cassette. It refuses before spending: a request URL
other than `wss://api.deepgram.com/v1/listen` with the expected query
(checked offline, nothing started), a query key the plugin fixes or one
named like a credential, a ledger that does not read, `--expect-spent`
that is not the ledger's count, more audio than the month's replay minutes
have left, a keys file with no Deepgram key, and no `websockets`. The entry
is written and read back before anything connects, and only Deepgram's
host is reachable after it.

**The allowance** (the maintainer, 2026-11-24, replacing D6's 20 replay
entries of at most 130 s, decided 2026-10-27): 1,000 minutes of replay
audio a month, counted from each replay entry's `audio_s`, with no cap on
the number of entries and an entry as long as its recording; one canary a
month; 200 live vendor turns a release. 1,000 minutes is about $4.80 at
Deepgram's promotional $0.0048 a minute for nova-3 streaming, $7.70 at
the $0.0077 it replaces (deepgram.com/pricing, verified 2026-11-24). The
ledger's own `"monthly": {"replay_minutes": N, "canaries": N}` wins, so
the numbers change in one line of `ledger.json`. The refusal past it says
how many minutes are left; anything more asks the maintainer first.

`seed_cassettes.py` made the bank's first nine cassettes, 75 turns, from
nine of the ten replays the 0.5.3 ledger paid for
(`releases/v0.5.3/harness/runs/dg02_*` to `dg10_*`; dg01 was logged only as
text and kept no hashes of its bytes): it replays the same recording through the mock
at settings that feed the same bytes, twice, the second time with the paid
run's partials so the endpointer ends each turn where it did, and keeps a
turn only when every byte matches.

`canary.py` runs once a month: the four live-vendor tests and one cassette
turn replayed live. A new model version or a final with other words marks
the cassettes stale; a final that differs in case or punctuation alone is a
note (ledger L45). It spends the month's canary, so without `--spend` it runs
only the checks that need no network and spends nothing.

## Where things live

| What | Where | In git |
|---|---|---|
| tools, formats, plans, goldens | `emet/tools/bank/` | yes |
| recordings | `Project Emet/recordings/`, `voicebank/audio/` on the Pi | never |
| index, manifests, consent | `voicebank/bank.json`, `manifests/`, `consent/` | never |
| labels, cassettes, miss lists, the ledger | `voicebank/labels/`, `cassettes/`, `misses/`, `ledger.json` | never |
| the generated bank's lists and goldens | `emet/tools/bank/generated/`, `golden/generated/` | yes |
| the generated bank | `Project Emet/genbank/`, to be published as a release asset of its own repository (D8) | never |

## Tests

```sh
"$PY" -m pytest -q -p no:cacheprovider tools/tests
```

A test that needs the real bank skips without it; `EMET_BANK` names the
bank's root and `EMET_BANK_SEARCH` more directories for its audio, separated
as `PATH` is. `EMET_GENBANK` names the generated bank's small set, which is
otherwise looked for at `Project Emet/genbank/small/`. The builder's tests
need neither Piper nor numpy nor the network: a fake voice gives a tone for
each text.
