# emet-providers

The plugins that reach a service for [Emet](../DESIGN.md), or a model on the
robot's own disk: speech recognition, language models and voices.

Apache 2.0. The counterpart to `emet-hal`, which is named for hardware and
holds drivers, locomotion, wake and audio. A provider is a plugin in the same
sense: it satisfies a contract in `emet_sdk.plugin`, registers an entry point,
and reaches the engine by name. It differs in what it talks to, and in what it
drags in: client libraries are heavy and networked, and a body that only ever
wakes should not install them. So they live here.

## What ships today

| Entry point | Group | What it is |
|---|---|---|
| `mock` | `emet.stt` | Reads words out of the bytes it is given, one partial a frame, then a final |
| `deepgram` | `emet.stt` | Streaming recognition through Deepgram, `nova-3` unless the soul says otherwise. Extra: `deepgram` |
| `mock` | `emet.llm` | Repeats what it heard, or says a scripted line, one word a delta; can ask for a tool |
| `anthropic` | `emet.llm` | Claude through the Messages API, streamed, `claude-opus-5` unless the soul says otherwise. Extra: `anthropic` |
| `openai` | `emet.llm` | GPT through Chat Completions, streamed, `gpt-5.6-terra` unless the soul says otherwise; `params.url` points it at any compatible server. Extra: `openai` |
| `mock` | `emet.tts` | Spells the words into the audio it produces, a chunk a word; `read_back()` turns the audio into the words again |
| `piper` | `emet.tts` | The local voice, through Piper on ONNX Runtime, `en_US-ljspeech-medium` unless the soul says otherwise; the model is a file downloaded once. Extra: `piper` (GPL-3.0, imported, never bundled) |
| `deepgram` | `emet.tts` | The cloud voice: Aura over Deepgram's speak API, raw linear16 streamed back, `aura-2-thalia-en` unless the soul says otherwise. Extra: `deepgram` |

In each group the mock shipped first, so that the real providers were written
against the seam rather than the seam around them, and the real providers
came as a pair: a seam with one implementation is untested as a seam.
`DESIGN.md` sections 12.3 to 12.5 have the contracts and the reasoning.

## The local voice

Synthesis is local by default. The reference soul names `piper`, and a
voice is two files the owner downloads once into a place the plugin looks
(`~/.local/share/emet/voices`, or `/etc/emet/voices` for an installed robot,
or wherever `models.tts.params.voices_dir` says):

```sh
pip install -e "emet-providers[piper]"
python -m piper.download_voices en_US-ljspeech-medium --data-dir ~/.local/share/emet/voices
```

The plugin never downloads a voice at boot; when the file is missing it
names that command and refuses to run. Each voice on Hugging Face has its
own licence in its model card. LJ Speech is public domain, which is why it
is the reference voice; several others in the catalogue are fine-tuned from
a corpus licensed for research only, and a shipped default cannot rest on
those. `piper-tts` itself is GPL-3.0-or-later (it compiles espeak-ng in), so
it is an optional extra that Emet imports and does not bundle; a body
without the extra carries none of it.

A cloud voice is one line of the soul, and a Deepgram key serves both the
transcriber and the voice:

```yaml
models:
  tts: {provider: deepgram, model: "aura-2-thalia-en", key_env: "EMET_DEEPGRAM_KEY"}
```

## Keys

Bring your own. The soul names the environment variable per stage (`key_env`;
the reference soul says `EMET_DEEPGRAM_KEY` and `EMET_OPENAI_KEY`, and the
Anthropic plugin reads `EMET_ANTHROPIC_KEY` when a soul names nothing); the
key itself is never written into a soul or a manifest. Put them in a keys
file, once, and `emet-listen` reads it before any provider starts:

```sh
mkdir -p ~/.config/emet
cat > ~/.config/emet/keys.env <<'EOF'
EMET_DEEPGRAM_KEY=...        # from console.deepgram.com
EMET_OPENAI_KEY=...          # from platform.openai.com
EMET_ANTHROPIC_KEY=...       # from console.anthropic.com
EOF
chmod 600 ~/.config/emet/keys.env
pip install -e "emet-providers[deepgram,openai,anthropic,piper]"
```

An installed robot reads `/etc/emet/keys.env` too, and `--keys FILE` names
any other. A variable already exported in the shell always wins over the
file, so a one-off key for one run still works. `keys.env` is in the
repository's `.gitignore`, whatever directory it lands in.

At boot each plugin makes one cheap request (a connection for the Deepgram
transcriber, a model listing for the language models, one six-character
word for the Deepgram voice), so a missing key, a rejected key (HTTP 401)
or an unreachable network is reported in those words before the first
question, and the robot refuses to run rather than hear questions it can
never answer. Emet never sees a key except to send it in a header.

A local model works the same way: point the `openai` provider at any server
that speaks Chat Completions and give it whatever key that server wants.

```yaml
models:
  chat:
    provider: openai
    model: "llama3.2"
    key_env: "EMET_LOCAL_KEY"
    params: {url: "http://localhost:11434/v1", max_tokens_field: "max_tokens"}
```

## How a provider is chosen

The soul names one per stage, under `models`:

```yaml
models:
  stt:  {provider: deepgram, model: "nova-3", key_env: "EMET_DEEPGRAM_KEY"}
  chat: {provider: openai, model: "gpt-5.6-terra", key_env: "EMET_OPENAI_KEY"}
  tts:  {provider: piper, model: "en_US-ljspeech-medium"}
```

The key is the owner's and travels with the soul, so the choice is a soul
field: a cloud account is not hardware. The body may take a stage over with
its own `models.<stage>.provider`, carrying its own `model` and `key_env`,
which is what a test rig with no key does (`examples/mock-scout.yaml` names
`mock` for all three), and it tunes whichever provider runs through
`models.<stage>.params`. The soul's `voice.rate`, how fast it speaks, rides
along to whichever voice runs: it is a persona trait, like `patience_ms`. The rule in code is `emet_sdk.models.model_selection`,
one rule for every stage.

A soul's provider is never checked against what is installed, because a soul
must be valid on every machine or on none. A body's is, and an unresolvable
name is a `missing_plugin` error like `audio.wake.engine`. There is no default
provider: nobody can be assumed to hold an account, and choosing one quietly
would spend somebody's credits.

## Writing one

Implement `TranscriberPlugin`, `LanguageModelPlugin` or `VoicePlugin` from
`emet_sdk.plugin`, register an entry point, and install the package:

```toml
[project.entry-points."emet.stt"]
"deepgram" = "your_package.deepgram:DeepgramTranscriber"

[project.entry-points."emet.llm"]
"gemini" = "your_package.gemini:GeminiLanguageModel"

[project.entry-points."emet.tts"]
"kokoro" = "your_package.kokoro:KokoroVoice"
```

A transcriber has three methods: `describe()` reports the model that
answered, whether partials will arrive, and the sample rate the instance will
actually run at; `feed(frame)` takes one frame and returns the newest partial
if the text so far changed; `finish()` closes the utterance and returns the
final. Every partial carries the whole text heard so far in the utterance, so
a caption replaces its line rather than splicing fragments. A final whose
words are missing because something broke says why in `Transcript.error` and
does not raise: the engine answers that in the soul's own words, where an
empty final with no error is a cough and stays silent.

A language model has two: `describe()`, and `reply(prompt)`, an async
iterator that yields `TextDelta`s as text arrives, a `ToolCall` per completed
call, and one `ReplyDone` last, on success and on failure alike. A failure
the vendor reported or the network caused ends the stream with
`stop_reason="error"` and the text so far; it never raises through the
engine's turn.

A voice has two as well: `describe()`, which states the sample rate its
audio arrives at (the engine opens the speaker to match, so say the true
one), and `speak(text)`, an async iterator of mono int16 chunks for one
sentence. Raise `PluginError` for a failure the provider reported; the
engine loses that sentence and goes on with the next.

Two things worth getting right:

**Read the key in `start()`, from the environment variable `key_env` names,
and report `healthy=False` with the variable's name when it is absent.** The
engine prints that detail and refuses to run. Claiming health and returning
empty transcripts forever is the failure this contract exists to prevent.

**`feed()` must return promptly.** It runs inside the capture loop. Hand the
frame to the service and report whatever results have already come back;
a provider that waits for the network here drops audio.

## Develop and test

```sh
pip install -e ../emet-sdk -e ".[dev]"
pytest
```
