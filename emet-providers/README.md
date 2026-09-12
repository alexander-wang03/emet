# emet-providers

The plugins that reach a service for [Emet](../DESIGN.md): speech recognition
today, language models and speech synthesis as releases arrive.

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

**No real provider yet.** The seam shipped first, so that the first call to a
vendor is made through it. `DESIGN.md` section 12.3 has the contract and the
reasoning.

## How a provider is chosen

The soul names it, under `models.stt`:

```yaml
models:
  stt: {provider: deepgram, model: "nova-2", key_env: "EMET_DEEPGRAM_KEY"}
```

The key is the owner's and travels with the soul, so the choice is a soul
field: a cloud account is not hardware. The body may take the choice over
with `audio.stt.provider`, carrying its own `model` and `key_env`, which is
what a test rig with no key does (`examples/mock-scout.yaml` names `mock`),
and it tunes whichever provider runs through `audio.stt.params`. The rule in
code is `emet_sdk.models.stt_selection`.

A soul's provider is never checked against what is installed, because a soul
must be valid on every machine or on none. A body's is, and an unresolvable
name is a `missing_plugin` error like `audio.wake.engine`. There is no default
provider: nobody can be assumed to hold an account, and choosing one quietly
would spend somebody's credits.

## Writing one

Implement `TranscriberPlugin` from `emet_sdk.plugin`, register an entry
point, and install the package:

```toml
[project.entry-points."emet.stt"]
"deepgram" = "your_package.deepgram:DeepgramTranscriber"
```

Three methods: `describe()` reports the model that answered, whether partials
will arrive, and the sample rate the instance will actually run at;
`feed(frame)` takes one frame and returns the newest partial if the text so
far changed; `finish()` closes the utterance and returns the final. Every
partial carries the whole text heard so far in the utterance, so a caption
replaces its line rather than splicing fragments.

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
