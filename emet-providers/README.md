# emet-providers

The plugins that reach a service for [Emet](../DESIGN.md): speech recognition
and language models today, speech synthesis as releases arrive.

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

In each group the mock shipped first, so that the real providers were written
against the seam rather than the seam around them, and the language models
came as a pair: a seam with one implementation is untested as a seam.
`DESIGN.md` sections 12.3 and 12.4 have the contracts and the reasoning.

## Keys

Bring your own. The soul names the environment variable per stage (`key_env`;
the reference soul says `EMET_DEEPGRAM_KEY` and `EMET_OPENAI_KEY`, and the
Anthropic plugin reads `EMET_ANTHROPIC_KEY` when a soul names nothing); the
key itself is never written into a soul or a manifest. Export them in the
shell that runs the robot:

```sh
export EMET_DEEPGRAM_KEY=...        # from console.deepgram.com
export EMET_OPENAI_KEY=...          # from platform.openai.com
export EMET_ANTHROPIC_KEY=...       # from console.anthropic.com
pip install -e "emet-providers[deepgram,openai,anthropic]"
```

At boot each plugin makes one cheap request (a connection for Deepgram, a
model listing for the language models), so a missing key, a rejected key
(HTTP 401) or an unreachable network is reported in those words before the
first question, and the robot refuses to run rather than hear questions it
can never answer. Emet never sees a key except to send it in a header.

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
```

The key is the owner's and travels with the soul, so the choice is a soul
field: a cloud account is not hardware. The body may take a stage over with
its own `models.<stage>.provider`, carrying its own `model` and `key_env`,
which is what a test rig with no key does (`examples/mock-scout.yaml` names
`mock` for both), and it tunes whichever provider runs through
`models.<stage>.params`. The rule in code is `emet_sdk.models.model_selection`,
one rule for every stage.

A soul's provider is never checked against what is installed, because a soul
must be valid on every machine or on none. A body's is, and an unresolvable
name is a `missing_plugin` error like `audio.wake.engine`. There is no default
provider: nobody can be assumed to hold an account, and choosing one quietly
would spend somebody's credits.

## Writing one

Implement `TranscriberPlugin` or `LanguageModelPlugin` from
`emet_sdk.plugin`, register an entry point, and install the package:

```toml
[project.entry-points."emet.stt"]
"deepgram" = "your_package.deepgram:DeepgramTranscriber"

[project.entry-points."emet.llm"]
"gemini" = "your_package.gemini:GeminiLanguageModel"
```

A transcriber has three methods: `describe()` reports the model that
answered, whether partials will arrive, and the sample rate the instance will
actually run at; `feed(frame)` takes one frame and returns the newest partial
if the text so far changed; `finish()` closes the utterance and returns the
final. Every partial carries the whole text heard so far in the utterance, so
a caption replaces its line rather than splicing fragments.

A language model has two: `describe()`, and `reply(prompt)`, an async
iterator that yields `TextDelta`s as text arrives, a `ToolCall` per completed
call, and one `ReplyDone` last, on success and on failure alike. A failure
the vendor reported or the network caused ends the stream with
`stop_reason="error"` and the text so far; it never raises through the
engine's turn.

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
