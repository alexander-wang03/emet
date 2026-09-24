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

## [0.5.0] - 2026-09-23

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

## [0.4.1] - 2026-09-22

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
