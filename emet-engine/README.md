# emet-engine

The listen loop and the runtime that drives a body.

Imports `emet_sdk` only. Hardware reaches it by name through entry-point
discovery, never by import. See the layering check in `tools/check_layering.py`.

Three commands. `emet-talk` is the robot: wake, words, answer, voice, speaker,
one exchange after another, everything read from the two documents and
nothing behind a flag.

```sh
emet-talk path/to/manifest.yaml path/to/soul.yaml
emet-talk path/to/manifest.yaml path/to/soul.yaml --replay recording.wav --stats
```

It prints `you:` and the robot's name as the words arrive, says the soul's
`persona.lines` when a provider declines or fails and the engine's own
words for a model that cannot answer or an unpaid chat account, waits one more
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

A wake fires a little after the phrase ends, so the transcriber is first
handed the audio in between, which holds the start of a question said in
the same breath as the name. When the end of the name is still sounding at
the start of that audio and fades before the question begins, it is written
as silence. With `--stats`, a `name cut` line counts those handovers and how
many had the end of the name cut from their start, with the mean and longest
cut in milliseconds, and a line under each wake that handed audio over says
which: `lag 190 ms, name cut 130 ms, level stop alone 130 ms`. The lag is
where the wake engine put the end of the phrase; the name cut is how much of
the start was written as silence; the level stop alone is what the rule
would have written without its stop on a rise with the high frequencies
weighted up, and is never applied. The two differ only where that stop kept
the wake engine's cut. When the lag passes what the loop keeps, the line
ends `; the last 640 ms handed over as heard`, and neither cut is made.
`emet-talk --stats` prints the same line in parentheses. Only the footer's
line starts with `name cut`.

Keys are read from a file rather than exported in every shell: put
`NAME=value` lines in `~/.config/emet/keys.env` (or `/etc/emet/keys.env` for
an installed robot, or a file named with `--keys`), `chmod 600` it, and
`emet-listen` loads them before any provider starts. A variable exported
with a value always wins over the file. The header prints which names were
loaded and from where; values are never printed.

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
pip install -e "../emet-providers[piper]"
python -m piper.download_voices en_US-ljspeech-medium --data-dir ~/.local/share/emet/voices
```

A cloud voice is one line of the soul (`{provider: deepgram, model:
"aura-2-thalia-en", key_env: "EMET_DEEPGRAM_KEY"}`); the mock voice spells
the words into the audio and needs nothing. The sink is opened at whatever
rate the voice reports, 22050 Hz for a Piper medium voice, 24000 Hz for
Aura, so `--speak` and `--echo` exclude each other. `--stats` adds `voice
first` and `voice done`: the wait from the final transcript to the first
sound at the speaker, and to the last.

Each provider's boot line says where its stage runs, which key it read and
which installed package offered it:

```
  stt      deepgram (streaming), model nova-3, at api.deepgram.com, key EMET_DEEPGRAM_KEY, from emet-providers 0.5.7
  llm      openai, model gpt-5.6-terra, at api.openai.com, key EMET_OPENAI_KEY, from emet-providers 0.5.7
  voice    piper, model en_US-ljspeech-medium (22050 Hz), on this body, from emet-providers 0.5.7
```

`at` names the host and port the provider sends to, never a user name or a
password written into its URL; `on this body` is one of Emet's own plugins
that sends nothing anywhere. A plugin from another package that names no
address reads `where it sends is not stated`, since the engine cannot tell
whether it sends anything. `key` names the variable, and a key's value is never
printed. `emet-talk` prints the same as `words`, `answers` and `voice`. The
`stt` line puts the mode straight after the provider's name, so the name is
always the line's second word and is never followed by a comma, which is
how `tools/bank/replay.py` reads it. Two installed packages that offer one
plugin name stop the boot before the robot makes a sound, naming both and
the `pip uninstall` for each.

`emet-say` says a line in the soul's voice with nothing listening. It boots
the voice and the sink only, performs the text's tags through the voice rungs
of their chains, and prints how long the first sound took. `--to` writes a
WAV at the voice's rate in place of the manifest's sink:

```sh
emet-say path/to/manifest.yaml path/to/soul.yaml "[express.curiosity] The kettle has boiled." --to kettle.wav
```

## When something fails

Each failure of a turn, and a provider, audio source or sink that cannot
start, prints one line from one table, `emet_engine/failures.py`: a
provider that cannot start, a chat model its
vendor has retired or does not know, an account out of credit, a reply that
spent its whole token cap thinking, words that never arrived, and an audio
source or sink that cannot open. The session picks the line from what
failed, and never from the wording of an error, so the detail a person reads
is the plugin's own sentence and the line around it stays the same. A
refusal at boot is one line on stderr and exit 1, before the robot makes a
sound; only a microphone that will not open is refused after the boot tone,
which plays before the microphone opens on purpose:

```
the audio source 'wav' cannot start, so the robot would listen to nothing. It will not run. could not open 'nothere.wav': [Errno 2] No such file or directory: 'nothere.wav'
```

A failed turn prints its line as a note, and the robot says the line the row
names: the soul's `failed` line, or one of the engine's own for a model that
cannot answer and for an unpaid language-model account. `emet-talk` prints it after the
speaker's tag; `emet-listen --speak` says it too and prints `spoke the line:`
and the words. A chat model that is retired or unknown does not stop the
boot: it prints one `!` notice, and each wake then gets the unavailable line
at the end of its turn, with nothing sent to the transcriber or the model.
A model with a retirement date still to come boots as it always did, with
one notice naming the date.

A plugin logs what the engine prints at INFO, so a refusal prints once, and
the reply's tag (`reply:`, or the robot's name in `emet-talk`) is printed
with the reply's first word, so a reply that failed before any text leaves
no empty tag above its line.

## The body

Before anything boots, both commands print which tree is running, as one
line in a fixed shape for a script to read:

```
emet-listen: emet-sdk 0.5.6, emet-hal 0.5.6, emet-providers 0.5.6, emet-engine 0.5.6; revision 2cc6acb984d6, working tree differs
```

Each version is what that package's installed metadata says, so a package
installed editable and not reinstalled since its version changed shows the
old number, and a package that is missing shows `not-installed`. The
revision is the commit of the checkout the engine's source is in, from one
`git status` given two seconds, and whether the working tree differs from
it, untracked files included. A copy installed from a wheel, a directory
that is not a checkout, or a machine without git prints `revision unknown: `
and the reason. The machine's name is never printed.

Since 0.5 both commands boot the body before they open the microphone. Every
part the manifest declares is started through discovery and asked what it can
actually do, except a reserved type, which has no plugin yet; every fallback
chain is resolved once against those answers; and the header prints the
result, grouped by what performs each intent:

```
  body     a Raspberry Pi with a USB microphone and speaker (pi_speakerphone)
  chains   32 resolved at boot against 0 part(s): 0 to hardware, 32 to voice
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
`clock` line, the wall clock against the audio clock, and an `overflows`
line, the callbacks PortAudio flagged as an input overflow. The reference body's live figures are in the 0.3
release notes.

`--stats` also reads the board as the run starts and again at the footer,
and prints both above the verdict. On a Raspberry Pi:

```
  board         at start: temp 49.9'C, throttled 0x0, load 0.07 0.06 0.05
  board         at end:   temp 51.2'C, throttled 0x0, load 0.35 0.12 0.08
  board         during the run: no throttle flag set
```

The temperature and the flags are `vcgencmd measure_temp` and `vcgencmd
get_throttled`, and the load is the 1, 5 and 15 minute load average. A flag
that is set is named, from the Raspberry Pi documentation's table:
under-voltage, frequency capped, throttled and the soft temperature limit,
each `now` or `has occurred`. A `has occurred` flag stays set after its
condition clears, so the last line compares the two readings and names
what the run itself saw. Without `vcgencmd` only the load is printed, and without a load
average either (Windows) there is no board line; a reader that exists and
fails prints `unknown`. Nothing is read per frame. The method is the Reachy
LiteRT sample's, cited in `CITATIONS.md`.

Under `--stats` each turn ends with its own line, the wait a person had and
what the turn sent off the body:

```
    turn gap 2260 ms = quiet 960 + stt final 410 + llm first 620 + voice 270; 967 tokens in, 18 out; sent 3.3 s of audio to api.deepgram.com, 2071 characters to api.openai.com
```

The gap runs from the last loud frame of the question to the first sound of
the answer, and its first term is the quiet the endpointer waited for, in
whole frames: the soul's patience, or twice it on an extended turn whose
extension ran out in silence. `emet-talk` prints the same line in parentheses. The footer
adds `turn gap`, `tokens`, `sent` and `cleaned` after `voice done`, summed
over the run; none of them starts with `name cut`.

At a terminal, `--stats` also keeps one line under the header up to date,
what the ears hear now:

```
    ears  -51.8 dBFS   waiting for the name   last wake 1.00, 14 s ago
```

It is redrawn at most twice a second and cleared before anything else is
written, so it never lands inside another line. Written to a file, as a
replay's output is, it is never drawn; the body runner's typescript keeps
each redraw as a piece after a carriage return, which its reader skips.

`--events PATH`, with `--stats`, writes what `--stats` prints to PATH as
JSON, one record a line: a header naming the four packages and the
platform, a record beside each line under a wake and each turn's line, and
the footer's numbers. It holds numbers and verdicts only, never a word
anybody said, and it changes no line on the console. The format is
`emet-events` 1.0, described by `emet_engine/schemas/events-1.schema.json`;
a reader refuses another major version and ignores keys it does not know.
Without `--stats` the flag is refused with exit 2.
