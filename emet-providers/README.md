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
| `mock` | `emet.stt` | Reads words out of the bytes it is given, one partial a frame, then a final; or plays a cassette of a real transcriber's answers back for the same bytes |
| `deepgram` | `emet.stt` | Streaming recognition through Deepgram, `nova-3` unless the soul says otherwise. Extra: `deepgram` |
| `mock` | `emet.llm` | Repeats what it heard, or says a scripted line, one word a delta; can ask for a tool, and can spend its token cap thinking (`params.reasoning_tokens`) |
| `anthropic` | `emet.llm` | Claude through the Messages API, streamed, `claude-opus-5` unless the soul says otherwise. Extra: `anthropic` |
| `openai` | `emet.llm` | GPT through Chat Completions, streamed, `gpt-5.6-terra` unless the soul says otherwise; `params.url` points it at any compatible server. Extra: `openai` |
| `mock` | `emet.tts` | Spells the words into the audio it produces, a chunk a word; `read_back()` turns the audio into the words again |
| `piper` | `emet.tts` | The local voice, through Piper on ONNX Runtime, `en_US-ljspeech-medium` unless the soul says otherwise; the model is a file downloaded once. Extra: `piper` (GPL-3.0, imported, never bundled) |
| `deepgram` | `emet.tts` | The cloud voice: Aura over Deepgram's speak API, raw linear16 streamed back, `aura-2-thalia-en` unless the soul says otherwise. Extra: `deepgram` |

In each group the mock shipped first, so that the real providers were written
against the seam rather than the seam around them, and the real providers
came as a pair: a seam with one implementation is untested as a seam.
`DESIGN.md` sections 12.3 to 12.5 have the contracts and the reasoning.

Both Deepgram plugins send `mip_opt_out=true` with every request, their
boot checks included, so each is excluded from Deepgram's Model Improvement
Program, and no body can turn it off. A body may give the transcriber
`models.stt.params.keyterms`, a list of words to listen for, such as the
robot's name. Each goes out as one `keyterm`, for a Nova-3 model only, and
each minute with keyterms costs more on Deepgram's pricing (read
2026-11-26). The plugin refuses `params.keyterm`, singular, and a `keyterm`
or `keyterms` under `params.query`, so every term is checked.

## The local voice

Synthesis is local by default. The reference soul names `piper`, and a
voice is two files the owner downloads once into a place the plugin looks
(`~/.local/share/emet/voices`, or `/etc/emet/voices` for an installed robot,
or wherever `models.tts.params.voices_dir` says):

```sh
pip install -e ".[piper]"
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
pip install -e ".[deepgram,openai,anthropic,piper]"
```

An installed robot reads `/etc/emet/keys.env` too, and `--keys FILE` names
any other. A variable already exported with a value in the shell always
wins over the file, so a one-off key for one run still works. `keys.env`
is in the repository's `.gitignore`, whatever directory it lands in.

At boot each plugin makes one cheap request (a connection for the Deepgram
transcriber, the soul's model by id for the language models, one
six-character word for the Deepgram voice), so a missing key, a rejected
key (HTTP 401) or an unreachable network is reported in those words before
the first question, and the robot refuses to run rather than hear questions
it can never answer. A language model at a server other than OpenAI's or
Anthropic's may take a second, the list of models, below; `warm_up` adds
one more. Emet never sees a key except to send it in a header.

One failure boots all the same: a chat model OpenAI or Anthropic has
retired, or an id it does not know, read at the vendor's own address. The
boot prints why, and the robot answers each wake
with one line saying it cannot think, because a robot with no screen that
refused to boot would tell the person in the room nothing. A date the
vendor has set for retiring the model is printed once at boot, while the
model still answers. An account out of credit is named as that, with the
vendor's code, in place of a bare HTTP status.

## A local language model

Point the `openai` provider at any server on the robot or on the home
network that speaks Chat Completions. The plugin needs a key in the
variable `key_env` names, so give it the key the server checks, or any
value for a server that checks none.

The block goes in the body manifest, where `models.chat` takes the stage
over and tunes it through `params`. A soul's `models` carry no `params`, so
the same block in a soul fails validation.

```yaml
models:
  chat:
    provider: openai
    model: "llama3.2"
    key_env: "EMET_LOCAL_KEY"          # in keys.env: a long random value
    params: {url: "http://localhost:11434/v1", max_tokens_field: "max_tokens", warm_up: true}
```

When nothing answers at boot, the refusal says where the address is: "That
address is this machine: start the model's server, or correct
models.chat.params.url", or the same for the local network. A cloud address
keeps "The language model needs a network". The robot refuses to run either
way.

Three params matter more for a local server than for a cloud one:

- **`warm_up: true`** sends one request of one output token at boot, after
  the model check, so the server loads the model before the first question.
  On litert-lm the first question after the server started waited 15.5 s
  for its first word, most of it the engine's load and its weight cache
  being written, against 3.8 to 4.4 s once warm (laptop, 2026-10-27 and
  28). A later start with that cache already on disk was not measured, so
  what `warm_up` saves there is unknown. It warms the
  load only, because the robot's system prompt is assembled after the
  plugin starts. A warm-up that fails leaves the robot
  running and the first question slow. Off by default, because against a
  cloud vendor it spends a request at every boot.
- **`max_tokens_field: "max_tokens"`** for a server that reads the older
  name of the cap. Ollama documents `max_tokens` and not
  `max_completion_tokens`; llama.cpp's server README names neither (both
  read 2026-11-27), so the older name is the safe one there.
- **`reasoning_effort`** is sent only when set, so a server that refuses the
  field never sees it. llama.cpp passes it to the model's template and turns
  thinking off at `none`, and Ollama reads it to control thinking (both read
  2026-11-27).

Each recipe starts a server bound to 127.0.0.1, so nothing else on the
network reaches it. A web page open in a browser on the same machine still
can, and the reference body's image is a desktop with a browser. A server
that lets any page's script read its answers (CORS) serves that page, and a
page can reach a local port through a name it controls (DNS rebinding),
which a CORS limit does not stop and a key does. So:

- **llama.cpp** checks a key when given `--api-key`, and its CORS allows
  every origin unless `--cors-origins` limits it; `localhost` allows its own
  pages only (its server README, read 2026-11-27). The recipe below sets
  both, with the value of `EMET_LOCAL_KEY`.
- **litert-lm** 0.18.1's server checks no key at all (its serve module,
  read 2026-10-27), so it cannot be told to; what it answers a page's
  script (CORS) was not read.
- **Ollama** allows browser requests from the origins `127.0.0.1` and
  `0.0.0.0` only unless `OLLAMA_ORIGINS` adds more, and its FAQ names no
  key check (read 2026-11-27).

**litert-lm** (Google, Apache-2.0), as run on a laptop with 0.18.1 on
2026-10-27. It reads `max_completion_tokens`, so the default field works:

```sh
pip install litert-lm==0.18.1
litert-lm import --from-huggingface-repo litert-community/gemma-4-E2B-it-litert-lm gemma-4-E2B-it.litertlm e2b
litert-lm serve --host 127.0.0.1 --port 9379
```

```yaml
    model: "e2b"
    params: {url: "http://127.0.0.1:9379/v1", timeout_s: 300, warm_up: true}
```

**Ollama** (MIT). It listens on 127.0.0.1:11434 and unloads a model after 5
idle minutes; a negative `OLLAMA_KEEP_ALIVE` keeps it loaded. Its context is
4096 tokens unless `OLLAMA_CONTEXT_LENGTH` says otherwise (Ollama's FAQ and
its OpenAI compatibility page, read 2026-11-27):

```sh
ollama pull llama3.2
OLLAMA_KEEP_ALIVE=-1 ollama serve
```

```yaml
    model: "llama3.2"
    params: {url: "http://localhost:11434/v1", max_tokens_field: "max_tokens", warm_up: true}
```

**llama.cpp** (MIT). `llama-server` serves a GGUF file, and `cache_prompt`,
on by default, reuses the cache of the start of a prompt it has already
seen, and Emet's system prompt stays the same for a run. Its README gave
the default port as 9931 on 2026-11-27, so the command names one. Run it
from a shell where `EMET_LOCAL_KEY` holds the value in keys.env:

```sh
llama-server -m model.gguf --host 127.0.0.1 --port 8080 --api-key "$EMET_LOCAL_KEY" --cors-origins localhost
```

```yaml
    model: "model.gguf"                # the id llama-server's /v1/models lists
    params: {url: "http://127.0.0.1:8080/v1", max_tokens_field: "max_tokens", warm_up: true}
```

At a server other than OpenAI's or Anthropic's the model check asks for the
model by id, and any answer but the model or a rejected key lists the
models instead, as the check did before 0.5.7. So a server without the
per-model path still boots, and so does an id that server does not know.
The robot finds out at the first question. An answer carrying OpenAI's
code `model_not_found` makes the model unavailable for the rest of the
run, as at OpenAI; any other fails that turn as a server fault does, with
the soul's `failed` line and `signal.offline`.

## Replaying a transcriber for nothing

A cassette is what a real transcriber said for exact bytes. The mock
transcriber plays one back, so a replay of a bank recording gets Deepgram's
words again without a key, a network or a cent. A body sets it under
`models.stt.params`:

```yaml
models:
  stt:
    provider: mock
    params:
      cassette: ~/voicebank/cassettes          # a file, or a directory of them
      cassette_request:                        # the request the run stands for
        provider: deepgram
        model: nova-3
        sample_rate: 16000
        query: {encoding: linear16, channels: "1", interim_results: "true",
                punctuate: "true", smart_format: "false", mip_opt_out: "true"}
      record_turns: runs/2026-10-27/turns      # each utterance's bytes, as fed
```

The request is the one the real plugin would make:
`cassette.request_for("deepgram", DeepgramTranscriber(...).query())` moves
`model` and `sample_rate` out of the query and keeps every other parameter
it sends. A body with `params.keyterms` adds them to the request as
`keyterm`, a list in the body's order. A turn recorded before 0.5.6 has no
`mip_opt_out` in its request, so it answers no request the plugin makes
now: the player sets it aside, and the mock logs a warning with each miss
on its bytes, naming the day it was recorded. A `cassette_request` without
`mip_opt_out`, 0.5.5's request, still reaches those turns.

- **A hit** needs every byte of the utterance to hash to a recorded turn's
  `fed_sha256`, among the turns recorded for `cassette_request` (every turn
  when it is absent). A recorded partial is given on the `feed()` call whose
  bytes so far match it.
- **A miss** is an empty final whose `error` reads `cassette miss`. It never
  raises. The runner puts those bytes on the paid list.
- **A cassette that cannot be read** leaves `start()` unhealthy with the
  reason, as `fail_on_start` does. `cassette` excludes `transcript`.
- **`record_turns`** writes `turn-0001.raw` and on, exactly as fed, and one
  line a turn to `turns.jsonl`: bytes, feed sizes, sha256, hit or miss, the
  final and the number of partials. A directory that already holds a
  `turns.jsonl` is refused.
- Without these params the mock does what it always did.

`emet_providers.cassette` reads and writes the files (`load`, `save`,
`TurnRecorder` for a writer, `Player` for playing back), and refuses any
request field named like a credential. The format is
[`tools/bank/FORMAT.md`](../tools/bank/FORMAT.md). Cassettes hold what
somebody said, so they live in the voice bank and never in this repository.
The running hash costs a median 1.90 us a 2560-byte frame on a Windows
laptop and 1.83 us on the reference body, and the mock's whole `feed()` with
a 100-turn cassette 2.60 us and 2.56 us (Python 3.13, 2026-10-28; the
script and its output are in `releases/v0.5.5/timings/`, outside this
repository).

## The live tests, recorded

`tests/test_deepgram_live.py`, `test_llm_live.py` and `test_tts_live.py`
call the vendors for real, and skip unless `EMET_LIVE_TESTS=1` and the
vendor's key are set. With `EMET_LIVE_RECORD=<dir>` as well, each saves what
it received, as it came (the WebSocket messages, or the response's bytes
chunk by chunk), with the model the vendor reported and the date, to one
JSON file a test (`"format": "emet-live-record/1"`). Request headers are
never kept, response headers only from a short list that carries no
credential, and a file holding the value of any `*_KEY` variable in the
environment is refused before it is written. The files can serve the
stand-in servers as fixtures, so parsing could be tested against real bytes
as well as the documented shapes; no test loads one yet, and none is in the
repository.

`tools/bank/canary.py` runs those four tests with recording on, then replays
one cassette turn through the real Deepgram plugin and prints `stale` when
the model version or the final differs from the cassette. It spends money
only with `--spend`: without it, it runs only the checks that need no
network, says what it would spend, and spends nothing. With it, it records
itself in the ledger it is given, once a month.

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
"whisper" = "your_package.whisper:WhisperTranscriber"

[project.entry-points."emet.llm"]
"gemini" = "your_package.gemini:GeminiLanguageModel"

[project.entry-points."emet.tts"]
"kokoro" = "your_package.kokoro:KokoroVoice"
```

Choose a name no installed package already offers in its group. Two
distributions that offer one name, such as a second `deepgram` in
`emet.stt` beside this package's, are refused at boot with
`DuplicatePluginError`, which names both and the `pip uninstall` for each,
since which one Python found first would depend on the order they were
installed.

Every `describe()` also says where the plugin sends what it is given:
`endpoint`, the URL as configured with no credentials or query, and
`key_env`, the variable the key was read from. Leave both None only for a
provider that runs on the body. The boot line prints `, on this body` for
an `endpoint` of None, and `--stats` counts nothing sent from it, so a
cloud plugin that leaves it out is reported as sending nothing.

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
