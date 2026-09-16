# emet-engine

The listen loop and the runtime that drives a body.

Imports `emet_sdk` only. Hardware reaches it by name through entry-point
discovery, never by import. See the layering check in `tools/check_layering.py`.

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
`models.stt`; a body may take the choice over under `audio.stt`. The reference
soul names `deepgram`, which needs `emet-providers[deepgram]` and a key
exported as `EMET_DEEPGRAM_KEY`. Without a key, or offline, the body can take
over with `mock`, which reads words out of the bytes it is given, so on a real
microphone give it a line to say:

```yaml
audio:
  stt:
    provider: mock
    params: {transcript: "testing the seam"}
```

Partials print as they arrive, one word a frame from the mock, and the final
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
arriving. The reference soul names `piper`, the local voice, which needs
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
