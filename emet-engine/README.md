# emet-engine

The listen loop and the runtime that drives a body.

Imports `emet_sdk` only. Hardware reaches it by name through entry-point
discovery, never by import. See the layering check in `tools/check_layering.py`.

Two commands. `emet-talk` is the robot: wake, words, answer, voice, speaker,
one exchange after another, everything read from the two documents and
nothing behind a flag.

```sh
emet-talk path/to/manifest.yaml path/to/soul.yaml
emet-talk path/to/manifest.yaml path/to/soul.yaml --replay recording.wav --stats
```

It prints `you:` and the robot's name as the words arrive, says what the
soul's `persona.lines` say when a provider declines or fails, waits one more
window when the words so far look unfinished (`interaction.extend_on_incomplete`),
and lists the intents the model tagged into its reply. A turn whose words
never arrived, because the network went away between the question and the
transcript, gets the soul's `failed` line too; a wake with nothing after it
stays quiet unless `persona.lines.nothing_heard` says otherwise. Every stage
has to be configured; a missing one names the field to set.

`emet-listen` is the same loop with each stage behind a flag, for finding out
which one is wrong:

```sh
emet-listen path/to/manifest.yaml path/to/soul.yaml
emet-listen path/to/manifest.yaml path/to/soul.yaml --replay recording.wav
emet-listen path/to/manifest.yaml path/to/soul.yaml --transcribe
emet-listen path/to/manifest.yaml path/to/soul.yaml --reply
emet-listen path/to/manifest.yaml path/to/soul.yaml --speak
```

Needs `emet-hal[audio,wake]` installed alongside for a microphone and a
detector to exist, and `emet-providers` for `--transcribe` to have a speech
recognition provider to hand the speech to. The soul names the provider under
`models.stt`; a body may take the choice over under its own `models.stt`. The reference
soul names `deepgram`, which needs `emet-providers[deepgram]` and a key
exported as `EMET_DEEPGRAM_KEY`. Without a key, or offline, the body can take
over with `mock`, which reads words out of the bytes it is given, so on a real
microphone give it a line to say:

```yaml
models:
  stt:
    provider: mock
    params: {transcript: "testing the seam"}
```

Partials print as they arrive, one word a frame from the mock, redrawn in
place on a terminal and a line each when the output is a file, and the final
prints as `said`. With `--stats`, an `stt final` line reports the wait from
the endpoint to the final transcript, which is the first latency a person
feels.

Keys are read from a file rather than exported in every shell: put
`NAME=value` lines in `~/.config/emet/keys.env` (or `/etc/emet/keys.env` for
an installed robot, or a file named with `--keys`), `chmod 600` it, and
`emet-listen` loads them before any provider starts. An exported variable
always wins over the file. The header prints which names were loaded and from
where; values are never printed.

`--reply` goes one step further and implies `--transcribe`: what was said
goes to the language model the soul names under `models.chat`, with the
soul's persona as the system prompt and the run's conversation so far, and
the answer prints as it streams after `reply:`. The reference soul names
`openai`, which needs `emet-providers[openai]` and `EMET_OPENAI_KEY`;
`anthropic` needs its extra and `EMET_ANTHROPIC_KEY`; the mock needs
neither and repeats what it heard. `--stats` adds `llm first` and `llm done`,
the wait from the final transcript to the first word and to the whole reply.

`--speak` goes the last step and implies `--reply`: the answer is said
through the voice the soul names under `models.tts`, a sentence at a time as
the model writes it, so the first sentence is heard while the second is still
arriving, and each sentence reaches the speaker a chunk at a time as the
voice produces it. The reference soul names `piper`, the local voice, which needs
`emet-providers[piper]` and a voice model downloaded once:

```sh
pip install -e "emet-providers[piper]"
python -m piper.download_voices en_US-ljspeech-medium --data-dir ~/.local/share/emet/voices
```

A cloud voice is one line of the soul (`{provider: deepgram, model:
"aura-2-thalia-en", key_env: "EMET_DEEPGRAM_KEY"}`); the mock voice spells
the words into the audio and needs nothing. The sink is opened at whatever
rate the voice reports, 22050 Hz for a Piper medium voice, 24000 Hz for
Aura, so `--speak` and `--echo` exclude each other. `--stats` adds `voice
first` and `voice done`: the wait from the final transcript to the first
sound at the speaker, and to the last.

## The body

Since 0.5 both commands boot the body before they open the microphone. Every
part the manifest declares is started through discovery and asked what it can
actually do, except a reserved type, which has no plugin yet; every fallback
chain is resolved once against those answers; and the header prints the
result, grouped by what performs each intent:

```
  body     a Raspberry Pi with a USB microphone and speaker (pi_speakerphone)
  chains   31 resolved at boot against 0 part(s): 0 to hardware, 31 to voice
             voice  utter        speak
             voice  inflect      express.affection express.amusement ...
```

A part that will not start is recorded as unhealthy and bound past, down to
the voice if need be; a driver that is not installed stops the boot and names
the plugin. `--chains FILE` lays a chain file over the SDK's, as `emet
explain --chains` does.

From the same manifest and the same answers the engine compiles the
**self-model**: plain sentences about what the body has and what it lacks,
which go into the system prompt beside the persona. A speakerphone is told
it has no wheels, and that if somebody asks it to come to them, the honest
answer is "I cannot come to you. I have no wheels." `--explain` prints the
self-model, and with a language model running the whole system prompt.

The prompt also asks for **intent tags**, offering only the ones this body can
act on, and each tag the model writes is performed through its binding: on a
bodiless body `[express.curiosity]` is "hm?" in the reply's voice, heard
before the sentence the tag opened; on a body with a head it tilts the head
when the model writes it, until the choreographer (1.0) times gestures to the
words. `emet-listen --reply` lifts and performs tags too, and prints the reply
without them. The engine's own state is performed the same way, as `signal`
intents: a rising pair of tones when it has booted, a soft click on each
wake, a falling pair when it stops, on a body with no status light and no
eyes to show them, whenever a voice is running. Reserved intents, and a
model's attempts at `signal` or `speak`, are logged at debug and dropped.

With a `body.id` in the manifest, the engine keeps a **state file** for the
body. A file already kept for this body wins; otherwise
`/etc/emet/state/<body.id>.json` where that directory exists and is writable;
otherwise `~/.local/state/emet/<body.id>.json`. `--state` names a file, or an
existing directory to keep `<body.id>.json` in. It holds the binding table,
the audio devices that answered, each part's health, joint trims, and
whatever a plugin asks to carry over. The shipped wake engine carries its
adapted cepstral mean, so the next boot on the same body starts as sensitive
as this run ended; the footer says what was kept and where. A `--replay`
carries no plugin's params in or out and offers no mean to paste: the
recording is not this body's room, and two replays of one file have to give
the same answer. It still records the bindings, the devices and each part's
health.

## Checking that it keeps up

The 0.3 acceptance bar is ten minutes without drift, which is a number rather
than a feeling. `--stats` produces it:

```sh
emet-listen manifest.yaml soul.yaml --replay ten-minutes.wav --stats
```

The figure that matters is **realtime**: processing time divided by the audio
processed. Audio arrives at a fixed rate whatever the CPU is doing, so 1.0 is
exactly keeping up with no margin and anything above it is falling behind.
`frames over budget` matters separately: a loop with a fine average that
overruns once a minute is dropping a word once a minute, and the average hides
it.

A run is only evidence if nothing was dropped, nothing went over budget, and
there is real margin. A bare pass on an idle machine is not a pass on a busy
one.

Recorded on both platforms from the same ten-minute recording, made on the
reference body with `tools/soak_prompts.sh` (49 wake phrases, 9 decoys):

```
                Raspberry Pi 5               x86 laptop
  frames        7500  (600.0s of audio)      7500
  per frame     mean 2.00 ms  p95 2.99       mean 1.89 ms  p95 3.18
                max 7.88                     max 9.86
  budget        80 ms/frame, 0 over          0 over
  realtime      0.0250  (40x)                0.0237  (42x)
  dropped       0                            0
  verdict       kept up                      kept up
```

A file replay exercises everything except the sound card. A live run adds a
`clock` line, the wall clock against the audio clock, and a card-overflow
count beside `dropped`. The reference body's live figures are in the 0.3
release notes.
