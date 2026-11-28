# Changelog

Every change to Emet that a person running it would notice, newest first.
The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The
numbers are [Semantic Versioning](https://semver.org/spec/v2.0.0.html): a
major release overhauls a framework, a minor release adds a capability on
the framework it has, a patch fixes or improves what is already there.
Before 1.0 a minor is a roadmap row met.

Every merge to master is one version, one tag and one entry here. An entry
is one line per change. The evidence behind it, what was run and on which
machine and what the numbers were, lives in the pull request the entry
names, and GitHub keeps that beside the commit.

## [0.5.7] - 2026-11-27

Say plainly what failed, what it spent, and how long you waited.

### Added
- A robot whose chat model its vendor has retired or does not know boots
  with a notice, and at the end of each wake's turn in which a person
  spoke says "The part of me that thinks has been switched off. My owner
  needs to choose another.", sending nothing to the transcriber or the
  model; a false wake stays silent (#21)
- A reply the vendor refuses because the account is out of credit is
  answered aloud with "I can't answer right now. The account that pays for
  my thinking has run out." (#21)
- A reply that spends its whole token cap before any words, or writes only
  a tag or marks the voice would not say, gets the soul's `failed` line,
  where the robot said nothing (#21)
- The voice says only what a person would say aloud: markdown markers and
  emoji are removed before it speaks, a URL is read as its host, and a
  sentence left with nothing to say is never sent; what is printed and what
  the conversation keeps are the model's own words. A lone `*` or `#` is
  dropped only where a bullet or a heading stands, so "6 * 7" and "the *
  key" reach the voice as written, and a tag the model wrote with words
  inside its brackets is lifted whole (#21)
- A body that declares `aec: none`, or no `aec`, tells its model it cannot
  hear anyone while it speaks, so a person has to wait until it finishes
  (#21)
- `signal.unavailable` and the `unavailable` explain topic, for a language
  model the vendor has retired or does not know; its voice rung says the
  engine's line whatever the soul's `failed` line is (#21)
- `emet-say MANIFEST SOUL TEXT`, a third console script of `emet-engine`,
  says a line in the soul's voice with no microphone, wake engine,
  transcriber or language model, and performs the text's tags through the
  voice rungs of their chains; a text with nothing left to say once its
  marks are removed is refused with exit 2 (#21)
- `emet-say --to FILE` writes what is said to a mono 16-bit WAV at the
  voice's rate, whatever sink the manifest names (#21)
- `emet-say --keys FILE` reads provider keys from a file as `emet-talk`
  does, and `--chains FILE`, which may be repeated, lays a chain file over
  the SDK's as `emet explain --chains` does (#21)
- Each provider's boot line in `emet-listen` and `emet-talk` says where its
  stage runs (`at` the host; `on this body` for Emet's own plugins that
  send nothing; `where it sends is not stated` for another package's plugin
  that names no address), the variable its key was read from, and the
  installed package that offered the plugin (#21)
- `--stats` ends each turn of `emet-listen` and `emet-talk` with a line: the
  turn gap from the person's last loud frame to the robot's first sound with
  the terms it adds up to, the tokens the replies counted, and what each
  stage sent to which host off this machine; a model server on the body
  sends nothing off it (#21)
- The `--stats` footer prints the turn gap's median, mean and max, the
  tokens, what the turns sent off the body, and how many sentences cleaning
  changed, after `voice done` and before `clock` (#21)
- Under `--stats` at a terminal, `emet-listen` and `emet-talk` draw a live
  line, redrawn at most twice a second: the input level in dBFS, the
  endpointer's state (`waiting for the name`, or the speech and the quiet
  against the patience, twice it in an extended turn) and the last wake's
  score and age, which
  pocketsphinx gives as 1.00 for every wake. Every other line, warning and
  traceback clears it first, and a run whose stdout is no terminal prints
  nothing new (#21)
- `--events PATH` on `emet-listen` and `emet-talk`, with `--stats`, writes a
  header naming the format, its version, the four packages and the platform,
  then one JSON record for each line `--stats` prints, numbers and verdicts
  only and no word anybody said; without `--stats` it is refused with exit 2
  (#21)
- The events file's JSON Schema ships in the engine's wheel as
  `emet_engine/schemas/events-1.schema.json`, and the wheel job fails an
  engine wheel without it (#21)
- `emet_engine.failures`, one table of the failure lines both programs
  print, with the line the robot says and the signal it shows for each (#21)
- `ListenSession.finish_turn()` ends every turn of `emet-listen` and
  `emet-talk`. It names the turn's outcome, takes its line and its signal
  from the failure table, and fills the turn's record (`last_turn`) (#21)
- `emet_engine.metrics.TurnRecord`, one per turn on
  `SessionStats.turn_records`, and `emet_engine.events`, which writes the
  `--events` file (#21)
- `ListenSession.on_frame` reports each frame's level and the turn's speech
  and quiet so far, outside the per-frame timing (#21)
- `Utterance.tail_ms` holds the quiet from the last loud frame to the
  endpoint, the first part of the turn gap (#21)
- `SpokenSentence.cleaned` says whether the voice was handed other text than
  the model wrote (#21)
- `FAULT_KINDS` and `FAULT_PREFIX` name the kinds `Health.faults` may hold,
  ten with `model_unavailable` and `out_of_credit` new (#21)
- The speech recognition, language model and voice descriptors carry
  `endpoint` and `key_env`, and `host` gives the host and port with no
  credentials. The Deepgram, OpenAI and Anthropic plugins report where they
  send and the variable their key was read from, and Piper and the mocks
  report that they run on the body. A plugin that sends anything off the
  body sets both; one left None reads `where it sends is not stated` when
  it comes from another package, and `--stats` counts nothing sent for it
  (#21)
- `PluginRegistry` keeps each plugin's distribution and version
  (`PluginOrigin`, `origin()`, `from_entry_points()`) (#21)
- The OpenAI plugin sends `models.chat.params.reasoning_effort` as
  `reasoning_effort` when a body sets it, and the reference body's manifest
  sets `low` for OpenAI and Anthropic alike (#21)
- `models.chat.params.warm_up` makes the OpenAI plugin send one request of
  one output token at boot, so a local server loads the model before the
  first question; it is off by default (#21)
- `emet-providers`' README has a recipe for litert-lm, Ollama and llama.cpp
  behind the OpenAI plugin (#21)
- `emet_providers/lifecycle.py` lists every default model with the dates its
  vendor has given for retiring it, and `release_check.py` holds each
  default model to its row with 180 days of runway, refusing a default
  model or a soul stage it cannot read (#21)
- The Deepgram transcriber and voice name an unpaid account (HTTP 402) as
  `out_of_credit` and a model the project cannot use (HTTP 403
  `INSUFFICIENT_PERMISSIONS`) as `model_unavailable`, at boot and mid-run,
  each in one sentence that says what to do; after that 403 the voice
  sends nothing more and loses each later sentence (#21)
- The mock language model takes `params.reasoning_tokens`, spent against the
  prompt's `max_tokens` before its first word, so a test gets the empty
  reply at the token cap, or the reply cut at it, without a vendor (#21)
- The mocks take `params.fault`, the kind a `fail_on_start` mock reports,
  and `params.endpoint`, a host for a mock to report (#21)
- A test guard in every suite: a test that opens a sound device stream,
  connects off the machine or looks a name up fails, save
  `test_audio_virtual.py` for streams and the live-vendor files for the
  network; a reloaded `sounddevice` is guarded again (#21)
- `check_style.py` refuses a one-line "X: Y" docstring, a closed list of
  litotes, negative parallelism on one line and the banned words' other
  forms, and reads a file outside the tree with `--file`, the release's pull
  request body included (#21)
- flake8 in the `layering` job, holding the four codes the code's markers
  name (#21)
- `release_check.py` refuses a comment that names a shipped release as still
  to come (#21)
- The dco job refuses a branch commit that carries a co-author or a tool's
  line (#21)
- The `test` job on 3.13 installs the `piper` and `audio` extras in a venv
  of their own and holds their licences to the gate (#21)
- The monthly canary asks each vendor's models endpoint about each default
  model, following no redirect, so a vendor's key reaches no other address
  (#21)
- `tools/reply_audio.py` says fixed replies through `emet-say --to` into WAV
  files and reports each file's largest gap, largest step out of near
  silence and loudness step between sentences against 700 ms, 0.10 and 6 dB,
  exiting 1 when one is crossed; `--measure` measures files it is given
  (#21)
- `tools/events_file.py` reads an `--events` file, refuses another format or
  a major version other than 1, skips kinds it does not know, and prints a
  run's turns, turn gap, tokens and what was sent (#21)
- `tools/bank/sweep.py` writes the exponential sine sweep a phone plays for
  the bank's room responses, outside the repository, with its parameters and
  sha256 beside it (#21)
- The body runner's budget table measures the turn gap's median on the local
  path, last loud frame to first sound on the one-breath replay, and holds
  it to the last release's as every row; DESIGN.md section 13 gives it a
  budget of 2.0 s at the median and 1.0 s as the stretch (#21)
- `tools/body/run.py --collect-only --sheet PATH` writes `sitting.json`,
  each rating of the release sitting's sheet beside its turn's record from
  the sitting's events file (#21)

### Changed
- The bank tools' pins move to the newer releases of 11 packages,
  ctranslate2 4.9.0, onnxruntime 1.32.0 and numpy 2.5.5 among them,
  Dependabot's change from #20 carried into this version, with the
  pins' licence table read again (#21)
- `signal.offline` is performed when a turn's words or reply are lost to a
  failure the plugin names no kind for, such as a network that is down, a
  server error, a 400 or a revoked key's 401. It is never performed for a
  retired model, an unpaid account or an empty reply at the cap (#21)
- The session reads every plugin's `health()` through `read_health()`. A
  report that raises refuses the boot in one line with its own words and is
  never a traceback; mid-run it fails its turn (#21)
- A soul's `persona.lines` sets `declined`, `failed` and `nothing_heard`.
  The engine's `unavailable` and `out_of_credit` lines are fixed until 0.6
  (#21)
- `emet-listen --speak` says a failed turn's line, and the soul's `declined`
  line after a refusal, aloud as `emet-talk` does, and prints
  `spoke the line:`; a turn heard with no transcriber says nothing (#21)
- `emet-listen`'s `stt` line puts the mode straight after the provider's
  name (`stt      mock (streaming), model ...`) (#21)
- `emet_hal.audio.AudioError` is an `emet_sdk.plugin.PluginError`, and still
  a `RuntimeError` (#21)
- `emet validate --pair` names the two files on its pairing line, and
  `emet explain` names the chain files when they fail to load (#21)
- `Health` refuses a fault kind outside `FAULT_KINDS`; a plugin outside Emet
  that named its own kind writes it `x_`, then a lower-case letter or a
  digit, then lower-case letters, digits or `_` (`"overcurrent"` becomes
  `"x_overcurrent"`), and a kind that misspells one of the set, such as
  `"model-unavailable"`, is offered that kind (#21)
- A name two installed distributions offer in one entry-point group is
  refused with `DuplicatePluginError` before any part of the robot starts,
  naming both and the `pip uninstall` for each; before, one of the two was
  loaded without a word (#21)
- `emet-hal`, `emet-providers` and `emet-engine` require `emet-sdk` 0.5.7
  (#21)
- The OpenAI and Anthropic plugins' boot check asks for the soul's model by
  id: a model the vendor has retired or does not know reports
  `model_unavailable`, a retirement date still to come prints once at boot,
  and another server behind either plugin that answers anything there but
  the model or a rejected key is asked for the model list as before (#21)
- After a reply refused with HTTP 404, the Anthropic plugin asks whether the
  model is gone, and sends a retired model nothing more for the rest of the
  run (#21)
- An OpenAI or Anthropic account out of credit or at a spend limit reports
  `out_of_credit`, with the vendor's code and where to add credit, until a
  request succeeds or fails for a reason that names no kind, as the
  Deepgram transcriber and voice do (#21)
- A language model server on this machine or on the local network that does
  not answer at boot is named as there, with the fix, in place of "The
  language model needs a network", behind the OpenAI and Anthropic plugins
  alike (#21)
- A vendor's error line names its code after its type when the two differ
  (#21)
- The Anthropic plugin sends `anthropic-version: 2026-10-21`, and sends none
  of the fields that version refuses (#21)
- The Anthropic plugin counts input tokens read from and written to the
  prompt cache, and takes the final counts from the last `message_delta`
  (#21)
- A Deepgram handshake refused for a reason with no kind quotes Deepgram's
  own error message, such as "No such model/language/tier combination
  found", in place of the library's "server rejected WebSocket connection"
  (#21)
- The Deepgram plugins report a missing `websockets` or `httpx` as
  `no_library`, in place of `start_failed` (#21)
- The mock language model honours the prompt's `max_tokens`, a word counted
  as a token, and stops at `length` past it (#21)
- Every one-line docstring shaped "X: Y" is one plain sentence, and a
  message that named the character `_` writes it (#21)
- The release sitting's sheet rates each wait from 1 to 5 ("1, I would stop
  talking to it; 3, acceptable; 5, as quick as a person") in place of the
  "Soon" tick, counts tries in a column of their own, and runs turns 3 to 10
  with `--events` (#21)
- The body runner's `speaker-talk` and `live-stats` steps write an events
  file, which comes home and gives each step its turns and its turn gap's
  median, and `--fetch` also takes `~/sitting-*.events.jsonl` (#21)
- The body runner installs pip-audit from the hashed pins of
  `tools/ci/requirements.txt` with `--require-hashes`, and makes its audit
  venv again when those pins change (#21)
- Bank v1's sitting 2 holds one block with the microphone's gain control
  off: two sweeps at each of five talker positions, and the same slots near
  and at 3 m as files recorded with it on; the sitting is 30 files and 33
  min 40 s (#21)
- `tools/bank/replay.py` reads a boot line's provider up to a space, a comma
  or a bracket, so the `stt` line's place and origin leave it as it was
  (#21)
- DESIGN.md says a robot whose chat model is retired or unknown boots and
  says the unavailable line, that `signal.offline` is for network and server
  errors, and that the part declared first wins a rung two parts fit (#21)

### Fixed
- An audio source or sink that cannot start, such as a missing replay file
  or a sink file that cannot be opened, is refused in one line naming the
  plugin and the program exits 1, where it printed a traceback of about 40
  lines (#21)
- A refusal at boot, a failed reply, a lost turn, a lost sentence and a part
  that will not start each print once, since the plugins, the engine's voice
  and the body log them at INFO (#21)
- No empty `emet:` or `reply:` line stands before a reply that failed or was
  declined before any text (#21)
- A Deepgram voice whose account is out of credit no longer tells the owner
  to check the voice name (#21)
- `emet explain` and the engine read a reserved part whose `role` or `form`
  is no string, whose `joints` are no list, or whose joint's `axis` is no
  string, as a part without them, where resolving its chains raised
  `TypeError` (#21)
- `emet validate` and the engine's boot report a reserved part's
  `mounted_on` that is a list or a mapping as an unknown reference, and pass
  over a reserved part's `driver` that is no mapping, where both raised
  `TypeError` or `AttributeError` (#21)
- A chain file whose intent name YAML reads as a number, a null or a boolean
  (`1:`, `null:`, `yes:`) is refused as an unknown intent that says so,
  where `emet validate` printed `AttributeError` and `--chains` raised it
  (#21)
- The body runner upgrades pip in its venv to 26.2 before the install, which
  fixes six advisories in the venv's pip 25.1.1 (#21)
- The canary makes `--out` absolute, so a relative one writes nothing inside
  the repository and `canary.json` reads the live tests' results (#21)
- Four unused imports are gone, and a test's closure no longer reads a name
  the test deletes (#21)
- The bank replay's golden-folder test and the SDK's test of the shipped
  plugins find their files from where they sit, so both pass in mutmut's
  copy of the tree (#21)
- Two `tools/dev.py` tests pass on Python 3.12 (#21)
- A test holds the plan template's flag-gap table to the columns
  `tools/plan_file.py` reads (#21)
- An audio plugin name nobody installed is refused before the body starts
  or the boot tone plays, and a replay file that will not open is refused
  before the boot tone, with no sound made (#21)
- An audio source or sink that refuses its block as it is built, such as a
  `gain_db` of `.inf`, is refused in one line (#21)
- A password written into `params.url` reaches no refusal: a URL with no
  scheme loses it as well, the Deepgram plugins' "could not reach" lines
  name the URL without it, and a URL that cannot be read is refused at
  boot in one line that names the field (#21)
- With no key set at a server other than the vendor's, the OpenAI and
  Anthropic plugins no longer send the owner to the vendor's console (#21)
- The canary calls the cassettes stale when a final's words change; a
  final that differs in case or punctuation alone stays fresh, with a note
  (#21)
- The body runner's pip reads no configuration file, so it installs from
  PyPI alone (#21)

## [0.5.6] - 2026-11-27

Run every release by one playbook.

### Added
- `tools/release_audit.py --pr N` reads the release's plan and the body's
  runs, from the folder `--evidence DIR` names when it is not the default,
  and refuses a merge with an unfinished stage, an S1 open or carried, an
  S2 open or carried on a minor or a major or unnamed in a patch's or a
  hotfix's pull request, no passing body run of the head, or a body flag
  not run in this release (#19)
- `tools/body/run.py` writes `run.json` format 2, with the body's manifest
  and state hashes and the flags the run itself exercised, and its live step
  runs `emet-talk` for 60 s (#19)
- The body runner soaks the bank's recordings with the null sink, holds a
  budget table to the last release's on the same body, audits its venv with
  pip-audit, runs nightly from master, and cuts the network with SSH up (#19)
- `tools/body/run.py --soak-collect` brings home a soak that outlived the
  laptop's run (#19)
- `tools/ci.py` holds each CI job's steps, GitHub and the laptop run them,
  the body runner runs its `layering` job, and `--all` names each job this
  machine could not run (#19)
- `tools/ci/check_workflows.py` holds the workflows to the house rules, and
  the CI-only tools install from `tools/ci/requirements.txt`, pinned by
  hash (#19)
- The `layering` job also runs zizmor, and checks that the tools a bare
  Python starts need only the standard library (#19)
- The test job on the newest Python prints each package's line coverage,
  with no floor (#19)
- The wheel job builds each wheel twice and compares them, and fails when an
  install would take an Emet package from the package index (#19)
- A weekly CI run on master, and a weekly workflow with OpenSSF Scorecard
  and pip-audit (#19)
- Advisory jobs run the documents' commands and links, print the public API
  diff, run mutation testing on the gate functions and replay the generated
  bank; the two bank jobs say they were not run until the bank's store
  publishes a set (#19)
- `tools/release_check.py` refuses an Emet requirement other than the
  release's own version, and a Security line with no advisory id (#19)
- `tools/check_style.py` refuses an email address outside four allowed
  forms (#19)
- `tools/check_licences.py` holds every installed dependency's licence to
  an allowed list, `tools/check_docs.py` checks the documents, and
  `tools/dev.py` sets up and checks a contributor's venv (#19)
- `tools/api_diff.py`, a test holding the SDK's public API to a golden
  file, `py.typed`, and `tools/check_health.py`, which holds the SDK's
  contract modules to 4,000 lines (#19)
- Seeded property tests of chain resolution and validation, and snapshot
  tests of the failure messages of `emet validate`, `emet explain`,
  `emet-listen` and `emet-talk` (#19)
- `tools/bank/generate.py` builds a generated bank from four public voices
  in simulated rooms, and a generated wake clip joins the HAL's tests (#19)
- `tools/bank/check.py` holds a generated bank to its pinned voices, its
  provenance and its `SHA256SUMS` (#19)
- The `virtual sound card` job listens live with `emet-listen` for 30 s
  while the generated wake clip plays into its null sink, and must hear the
  name once (#19)
- `params.keyterms` on the Deepgram transcriber, sent as one `keyterm` each
  for a Nova-3 model (#19)
- `tools/bank/paid_replay.py --keyterm TERM` sends a term in the replay's
  request (#19)

### Changed
- Every Deepgram request, the voice's and the preflights included, carries
  `mip_opt_out=true`, and the cassette key holds it and the keyterms (#19)
- The Deepgram transcriber refuses `params.keyterm`, and a `keyterm` or
  `keyterms` under `params.query` (#19)
- `tools/bank/canary.py` refuses a cassette turn recorded before the
  opt-out key (#19)
- `emet-hal`, `emet-providers` and `emet-engine` require `emet-sdk` 0.5.6,
  and the suites and the tools' tests run with warnings as errors (#19)
- `emet-sdk` requires jsonschema 4.26.0 and PyYAML 6.0.4 or later, and every
  `dev` extra pytest 9.2.0 (#19)
- Every CI job but the weekly Scorecard runs
  `python tools/ci.py <job> --install`; actions are pinned by commit, each
  workflow grants `contents: read` at the top and more per job, and no
  checkout keeps its token (#19)
- Dependabot waits seven days before proposing a release, and watches
  `emet-engine`, `tools/ci` and `tools/bank` as well (#19)
- The body runner installs the provider extras and piper in its venv (#19)
- `tools/bank/record.py` refuses a sitting plan that names a generated
  voice (#19)
- `tools/bank/requirements.txt` carries the licence gate's table of its
  pins (#19)
- `tools/bank/replay.py` names the recordings of a sitting with no goldens as
  unreleased, and the body's bank step passes only when a recording was held
  to a golden (#19)
- `SECURITY.md` says what follows a report: a first answer within a week,
  an embargo of at most 90 days, a CVE through GitHub and the advisory with
  the fix (#19)
- `SECURITY.md` says memory is planned for 0.6, and that no code enforces a
  body's motion limits while it moves yet (#19)
- The README and DESIGN open with "An open engine that gives any small
  companion robot a portable soul." (#19)
- `RELEASING.md` names the playbook's stages, `tools/ci.py --all`, the
  merge's new refusals, the severity ledger, the console snapshots and the
  API diff (#19)

### Fixed
- `validate_memory_db` closes its connection, and reads a database at a
  path holding `#`, `?` or `%`, or at a POSIX path that begins with two
  slashes (#19)
- A `keys.env` line that does not parse, or holds only the `=` padding of a
  key pasted on its own, is named by its line and never quoted, and a name
  with an empty value sets nothing and is never named, so a key pasted on
  its own stays out of the terminal (#19)
- A `keys.env` saved with a UTF-8 byte-order mark loads, and a missing keys
  file named by `EMET_KEYS` says to unset it (#19)
- A wav sink that cannot open its file raises that error alone, one whose
  format `wave` refuses closes the file and raises its own error, and a sink
  never stopped closes its file (#19)
- A wav sink started a second time closes its first writer before it opens
  the file again, so no tail of the first take follows the second (#19)
- The speaker's `cancel()` drops the audio of a `play()` still opening the
  device (#19)

### Security
- The `deepgram`, `anthropic` and `openai` extras require `certifi`
  2026.10.18 or later (PYSEC-2026-4320, GHSA-6q48-6c7w-9gw2) (#19)

Verified on the laptop and, over SSH, on the reference body, a Raspberry
Pi 5: the suites, the checks and the bank on c1ec3f2 on 2026-11-27; the
two-hour soak and the budget table on 8e618b3 on 2026-11-26, and every
body flag on 8e618b3 on 2026-11-27, a head that differs from c1ec3f2
only in the licence gate's list. No release sitting: the release changes
nothing a person judges in conversation. Measurements in #19.

## [0.5.5] - 2026-11-26

Replay a voice bank and run the body's checks over SSH.

### Added
- `emet-listen` and `emet-talk` print an identity line first: the four
  packages' versions and the checkout's commit, with whether the working
  tree differs from it, or why the revision is unknown (#16)
- `--stats` prints the board's temperature, throttle flags and load average
  at the start and the end of the run, and any flag set during it, where
  the machine reports them (#16)
- The mock transcriber plays a cassette back: `params.cassette` gives the
  recorded partials and final for the same bytes, and any other bytes an
  empty final reading `cassette miss`; `params.record_turns` writes each
  utterance's bytes as fed (#16)
- The live-vendor tests keep what each vendor sent with
  `EMET_LIVE_RECORD=<dir>`, and never a key (#16)
- `tools/bank/`, the voice bank's tools: a checker for its manifests, a
  prompter that records a sitting on the reference body over SSH, a
  labelling pass, a replay runner held exactly to golden files per
  platform, a paid replay within a monthly allowance of audio minutes,
  cassettes made from the Deepgram replays already paid for, and a monthly
  canary (#16)
- The prompter keeps each take on screen until the next one, counts in
  each prompt 3, 2, 1, gives a take with a pause 8 s, and records a file
  again with `--again`, keeping the old one with its takes excluded;
  `tools/bank/sit.py` runs it from the laptop and plays quiet tones in the
  laptop's earbuds at each count, warning when the cues stop (#16)
- `tools/body/run.py`, the body runner: from the laptop it puts a tree on
  the reference body over SSH, runs the suites, the checks and the bank's
  replays, runs the microphone and speaker steps only inside a named
  window, and lists when each body flag last ran; `tools/body/sitting.md`
  is the release sitting (#16)
- CI runs the tools' tests, the four suites on GitHub's arm64 runner as an
  advisory job, and the real PortAudio path against a virtual sound
  card (#16)

### Changed
- CI runs `actions/setup-python` 7.1.0, Dependabot's change from #15
  carried into this version (#16)

### Fixed
- `emet-providers` needs `emet-sdk` 0.5.1 or later, as `emet-hal` and
  `emet-engine` do. Its floor of 0.1 was met by an unrelated package that
  took the name `emet-sdk` on PyPI on 2026-10-30, so installing
  `emet-providers` alone could fetch that package in place of Emet's
  SDK (#16)

Verified on the laptop and, over SSH, on the reference body, a Raspberry
Pi 5, 2026-10-27 to 2026-11-25. No release sitting: the release changes
nothing a person judges in conversation. Measurements in #16.

## [0.5.4] - 2026-10-27

Keep spoken numbers as words in the transcript.

### Added
- `--stats` prints a line under each wake that handed audio to the
  transcriber, in `emet-listen` and `emet-talk`: the lag the wake engine
  reported, how much of the start was cut as the end of the name, and what
  the cut would have been with only its stop on a rise in level (#14)

### Changed
- The Deepgram transcriber sends `smart_format=false`, so numbers, dates,
  times, amounts, emails and URLs reach the model as the words said, and no
  final waits up to 3 s to be formatted. With it on, "what's two plus two"
  came back "What's 22" when the only final was the flush after the stream
  closed. A body can turn it back on in `models.stt.params.query` (#14)

### Fixed
- When a handover started inside the "m" of "emet" and the vowel rose
  less than 6 dB in level, the stop on a rise with the high frequencies
  weighted up fired where the "m" opens into its vowel, and the whole
  "-met" went to the transcriber in front of the question; that stop is
  now read only once the sound has fallen 6 dB below where the handover
  started. Where the level rises more, the "-met" still goes over (#14)

Verified on the laptop, 2026-10-05 to 2026-10-07, and on the reference
body, a Raspberry Pi 5 with a USB microphone and speaker, 2026-10-07 and
2026-10-26. Measurements in #14.

## [0.5.3] - 2026-10-04

Cut the end of the name from a one-breath question.

### Added
- `--stats` prints a `name cut` line: of the wakes that handed the words
  said with the name to the transcriber, how many had the end of the name
  left out, and by how much (#13)
- `tools/one_breath_prompts.sh` repeats one question with `ONE_BREATH_ONLY`
  and `ONE_BREATH_TIMES` (#13)

### Changed
- The audio handed to the transcriber with a wake leaves out the end of
  "emet" when it was still sounding, since the transcriber can drop the
  question's first word behind it: the start is written as silence up to
  where that sound has fallen 20 dB, unless the audio rises 6 dB first, in
  level or with its high frequencies weighted up, where the question or an
  "s" said straight on from the name begins (#13)
- A lone "no" ends the turn after one patience window, as "yes" does (#13)
- The prompt scripts give a replay 5 s of room before the first prompt, and
  the one-breath script no longer promises a wake count (#13)

### Fixed
- A tag made on another day than its merge takes its commit's date, and
  `tag_release.py` reads the day back before the push: dated a day late it
  would have failed the release audit on every later pull request (#13)
- `release_audit.py` read GitHub not answering as a missing tag, pull
  request or page, or as a red check; it now stops with "GitHub did not
  answer" and merges nothing (#13)
- `tag_release.py` printed its `gh release create` line with no `--title`,
  and a page made from it would fail the release audit (#13)

Verified on the reference body, a Raspberry Pi 5 with a USB microphone and
speaker, 2026-10-02 and 2026-10-03. Measurements in #13.

## [0.5.2] - 2026-09-30

Stop it waking on its own voice.

### Added
- The system prompt tells the model the phrase that wakes it: asked how to
  wake it at 0.5.1, the reference soul offered "Emet, wake up" where the
  phrase is "hey emet" (#12)
- `release_audit.py --pr N --merge`, which merges only when every check
  passed, pinned to the commit it checked, and `--pr N --ci`, the part of
  that check a CI run can hold; the check now also refuses a version not
  above master's, an entry that cites another pull request and uncommitted
  changes, and a refused date prints the command that fixes it (#12)
- `tools/one_breath_prompts.sh`, the prompts for recording one-breath
  questions on a body, which the cut before the wake is tuned against (#12)
- The run footer counts the wakes it ignored in the tail of the robot's own
  voice (#12)

### Changed
- On a body with no echo cancellation, whatever waited in the capture queue
  while the robot spoke is thrown away before the wake engine hears again,
  and a phrase that ended in the tail of its voice is ignored, however late
  the wake engine reports it: it heard its name in its own reply and
  answered itself (#12)
- The release audit runs in its own workflow, on every push to a pull
  request and every title edit as well as every merge and tag, and checks
  the pull request itself beside every version since 0.4.1 (#12)
- `RELEASING.md` carries a Dependabot pull request's change into the next
  versioned one, and merges through `--merge` (#12)

### Fixed
- The 0.5.1 entry carries the day it merged and was tagged, 2026-09-28 (#12)

Verified on the reference body, a Raspberry Pi 5 with a USB microphone and
speaker, 2026-09-29. Measurements in #12.

## [0.5.1] - 2026-09-28

Keep the words said in the same breath as the name.

### Added
- `WakeEvent.lag_ms`, how much audio the wake engine had already heard after
  the phrase ended when it fired; pocketsphinx reports it from its own
  segmentation of the phrase (#11)
- `tools/release_audit.py`, which checks every version since 0.4.1 against
  git and GitHub (the commit, the tag, the entry, the release page and the
  pull request agree, the tags are signed, no AI attribution), and with
  `--pr` a pull request before it merges; the `release audit` CI job runs
  it (#11)

### Changed
- The transcriber first hears the audio after the phrase that the wake
  engine had already heard, with the phrase written as silence, so a
  question said in the same breath as the name reaches it from its first
  word (#11)
- A turn whose words the transcriber heard while the energy detector never
  saw speech start ends on silence one patience later, instead of after the
  2.5 s lead-in: "hey emet, yes" said inside the listening click (#11)
- The run footer splits dropped frames by when they were lost: while
  speaking, thinking or waiting for the words is a note, while listening a
  warning, and `emet-listen --transcribe` alone no longer reads as a loop
  that cannot keep up (#11)
- `--stats` counts card overflows as the callbacks PortAudio flagged, on a
  line of their own on a live run (#11)
- scout-01 is a wheeled robot; its description said treaded while its name
  and its kinematics said wheeled (#11)
- The wake threshold's talk column is re-measured through the engine's own
  replay path: 3 false wakes in five minutes of talk at 1e-15, where the
  table said 2 (#11)

### Fixed
- `--stats` counted no turn for a wake whose turn the end of a recording
  closed (#11)
- The 0.4.1 and 0.5.0 entries carry the days they merged and were tagged,
  2026-09-23 and 2026-09-26 (#11)
- `tools/tag_release.py` keeps an entry's Added, Changed and Fixed headings
  in the tag: git strips lines that start with `#` from a tag message by
  default, and the v0.4.1 and v0.5.0 tags lost them (#11)
- `RELEASING.md` covers every version, not only minors and majors, and
  ends with the audit (#11)

Verified on the reference body, a Raspberry Pi 5 with a USB microphone and
speaker, 2026-09-27. Measurements in #11.

## [0.5.0] - 2026-09-26

It knows its body.

### Added
- The self-model: at boot the engine compiles the manifest, and what its
  parts reported, into plain sentences about what the body has and lacks,
  and puts them in the system prompt beside the persona (#10)
- The engine starts every declared part that has a plugin category and
  resolves every chain once against what the parts report; the boot log
  prints the binding table (#10)
- The voice rung's six actions: `utter`, `inflect`, `tone`, `explain` in
  the self-model's words, `backchannel` as a placeholder until 1.0, and
  `silence` (#10)
- Signal intents from the engine's own state: `booting`, `listening`,
  `thinking`, `speaking`, `muted`, `offline` and `error` (#10)
- With a voice running, a body with no status light and no eyes plays a
  rising pair when it boots, a soft click when it hears its name and a
  falling pair when it stops, and says at boot which part is not working (#10)
- A state file per body, `/etc/emet/state/<body.id>.json` or
  `~/.local/state/emet/<body.id>.json`, holding the binding table, the
  audio devices, each part's health, joint trims and carried params (#10)
- `Plugin.carry_over()`; the wake engine keeps its adapted cepstral mean
  for the next live boot on the same body by itself (#10)
- `--chains`, `--state` and `--explain` on `emet-listen` and `emet-talk` (#10)
- `SelfModel`, `BodyFact`, `VOICE_ACTIONS`, `EXPLAIN_TOPICS`,
  `TONE_PRESETS` and `load_chains()`, the one chain loader `emet explain`
  and the engine share, in the SDK (#10)
- The run header prints the speaker's own output latency (#10)

### Changed
- The prompt asks for intent tags, offering only those this body acts on (#10)
- A tag the model writes is performed through its binding: on a bodiless
  body `[express.curiosity]` is "hm?" in its own voice (#10)
- `emet-listen --reply` lifts tags out of the printed reply and performs
  them (#10)
- Each sentence of a reply is a `speak` intent carrying a prosody hint,
  performed through its binding (#10)
- The rule about speaking aloud no longer names a speaker; the self-model
  describes the body (#10)
- Reserved intents, and a model's `signal` and `speak` tags, are logged at
  debug and dropped (#10)
- In `emet-talk`, a turn whose words or reply did not arrive is
  `signal.offline`, and the soul's `failed` line is said once (#10)
- On a body that declares no echo cancellation, the endpointer does not
  take the robot's own listening click for the start of speech (#10)
- A replay carries no plugin's params into or out of the body's state file;
  it still records the bindings, the devices and each part's health (#10)
- `emet validate` and every `--chains` override reject a voice action, an
  explain topic or a tone no engine performs, and an inflect whose filler
  is not a word or a list of words (#10)
- A joint's `trim_deg` in the state file wins over the manifest's, which
  becomes the builder's first guess (#10)
- A manifest naming a part's driver that is not installed stops the boot,
  naming the plugin; reserved types are not started (#10)
- The speaker applies `audio.output.gain_db`, and refuses at boot a gain
  it cannot apply (#10)
- SIGTERM and SIGHUP end a run the way Ctrl-C does, during boot too; a
  run started under `nohup` ignores the hangup (#10)
- `emet-listen` redraws the live caption in place on a terminal (#10)
- `emet explain` and the run header name the body (#10)

### Fixed
- The provider tests failed on a machine with a Piper voice downloaded,
  since they looked in its real voices directories (#10)

Verified on the reference body, a Raspberry Pi 5 with a USB microphone and
speaker, 2026-09-24 and 2026-09-25. Measurements in #10.

## [0.4.1] - 2026-09-23

Play speech through one open output stream.

### Added
- `Transcript.error`, so a transcriber says why the words are missing and
  `emet-talk` speaks the soul's `failed` line instead of staying silent (#9)
- A count in the `--stats` footer of silence played inside a sentence
  because the voice fell behind real time (#9)
- This file, and a version on every merge to master (#9)

### Changed
- The speaker keeps one output stream open for the whole run and the engine
  hands it each chunk as the voice makes it. First sound through Deepgram
  Aura went from 2617 ms to 1895 ms after the transcript on the reference
  body (#9)
- The Piper plugin yields each sentence as its own inference finishes (#9)
- The `--stats` clock line counts the first frame on both clocks and prints
  the skew left after the dropped frames (#9)
- `tools/tag_release.py` takes the tag message from this file (#9)

### Fixed
- A stream that would not start was left open, a stream started after
  `stop()` was orphaned, `stop()` cut the last block of the last word, and
  `play()` could wait forever on a card that had stopped asking (#9)
- The transcriber waited fifteen seconds on a dead socket where its timeout
  is five (#9)

Verified on the reference body, a Raspberry Pi 5 with a USB microphone and
speaker, 2026-09-21 and 2026-09-22. Measurements in #9.

## [0.4.0] - 2026-09-17

It talks.

### Added
- `emet-talk`: wake, words, answer, voice, speaker, one exchange after
  another, everything from the two documents and nothing behind a flag (#8)
- `emet-providers`, the fourth package, for plugins that reach a service or
  a model on disk (#8)
- `emet.stt`, `emet.llm` and `emet.tts` plugin groups, each a contract in
  `emet-sdk` with a mock before any vendor (#8)
- Speech recognition through Deepgram (`nova-3`, one WebSocket per
  utterance, `websockets`), language models through Anthropic (Messages API,
  `claude-opus-5`) and OpenAI (Chat Completions, `gpt-5.6-terra`, or any
  compatible server), voices through Piper (local, the default,
  `en_US-ljspeech-medium`) and Deepgram Aura (`aura-2-thalia-en`), all on
  raw HTTP with no vendor SDK (#8)
- `models` on the soul and the body: the soul chooses each stage's
  provider, the body may take a stage over whole, no stage has a default (#8)
- A keys file, `~/.config/emet/keys.env` or `/etc/emet/keys.env` or
  `--keys`, read before any provider starts (#8)
- `emet-listen --transcribe`, `--reply` and `--speak`, and `stt final`,
  `llm first`, `llm done`, `voice first` and `voice done` in `--stats` (#8)
- The trailing clause: `interaction.extend_on_incomplete` waits one more
  window when the words so far look unfinished (#8)
- `persona.lines`: the soul's own words for a refusal, a failure and a wake
  with nothing after it, spoken in its voice (#8)
- Intents leave the model as inline tags, `[express.curiosity]`, lifted out
  before the voice and reported (#8)
- `tools/check_style.py` in CI, and `tools/tag_release.py` (#7)
- A CI job that runs every suite with the optional extras absent (#8)

### Changed
- The reference soul's chat model is `gpt-5.6-terra` and its voice is LJ
  Speech, which is public domain; Amy is fine-tuned from a research-only
  corpus (#8)
- Frames dropped while the robot speaks, thinks or waits for a transcript
  are counted apart and left out of the `--stats` verdict (#8)
- The sink opens at the rate the voice reports, so `--speak` and `--echo`
  exclude each other (#8)
- A body naming an uninstalled provider is a `missing_plugin` error (#8)
- Every em-dash in the tree removed; the style check keeps them out (#7)

### Deprecated
- `voice.engine` and `voice.model` on the soul, superseded by `models.tts`.
  Still accepted by the schema and ignored (#8)

## [0.3.0] - 2026-09-12

It listens.

### Added
- `emet-engine`, the third package, and `emet-listen` with `--replay`,
  `--echo` and `--stats` (#5)
- `emet.wake`, `emet.audio` and `emet.audio_out` plugin groups, with
  pocketsphinx, `microphone`, `wav`, `speaker` and `null` behind them (#5)
- Energy voice activity detection and endpointing driven by the soul's
  `interaction.patience_ms` (#5)
- `tools/release_check.py`, `RELEASING.md` and `CITATIONS.md` (#5)
- A README, community health files and a hardened supply chain (#2, #3)

### Changed
- `identity.wake_word` is free text: the detector is phonetic, so any
  phrase works given a pronunciation (#5)
- The version is declared once per package, in `pyproject.toml` (#5)
- The pocketsphinx threshold is 1e-15, from two recordings on the reference
  body; higher is stricter (#5)
- An utterance may run 30 s before it is cut, up from 15 (#5)

## [0.2.0] - 2026-08-25

Chains resolve against a body.

### Added
- `ActuatorPlugin`, `SensorPlugin` and `LocomotionPlugin`, found through
  entry points: installing a package is what makes a driver exist
- The chain resolver and `emet explain --why`
- `emet-hal` with a mock actuator and sensor, and `differential` and
  `tracked` locomotion
- A CI job that installs from a built wheel

### Changed
- Naming an uninstalled driver is a warning; `--verify-drivers` makes it the
  error the engine raises at boot
- `power: mains` is `power: plugged_in`; the soul `pneuma` is `neuma`

## [0.1.0] - 2026-08-25

Contracts written down and enforced.

### Added
- JSON Schemas for the body manifest, the soul bundle and motion packs,
  every field including those reserved for later releases
- `types.py`, `intents.py`, `chains.py`, `validate.py` and `emet validate`
- 31 fallback chains, every one ending in a voice rung
- Apache 2.0 throughout, CI with the layering check

[0.5.0]: https://github.com/alexander-wang03/emet/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/alexander-wang03/emet/compare/v0.4...v0.4.1
[0.4.0]: https://github.com/alexander-wang03/emet/compare/v0.3...v0.4
[0.3.0]: https://github.com/alexander-wang03/emet/compare/v0.2...v0.3
[0.2.0]: https://github.com/alexander-wang03/emet/compare/v0.1...v0.2
[0.1.0]: https://github.com/alexander-wang03/emet/releases/tag/v0.1
