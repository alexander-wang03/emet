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

## [0.5.4] - 2026-10-06

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

Verified on the laptop, 2026-10-05 to 2026-10-07, with recordings made on
the reference body, a Raspberry Pi 5 with a USB microphone and speaker. The
body has not run this code yet. Measurements in #14.

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
