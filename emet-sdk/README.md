# emet-sdk

The contract layer for [Emet](../DESIGN.md) — types, schemas, and the intent
vocabulary that the engine and every plugin agree on.

Apache 2.0. Under ~2,000 lines on purpose: this is contracts and almost no
logic, because it is the one thing both sides of the boundary must share.

**HAL** below and throughout means *hardware abstraction layer*. Emet uses the
term in Android's sense: the abstraction itself lives here in
`emet_sdk.plugin`, and `emet-hal` is the collection of per-device
implementations that satisfy it.

## What is in 0.1

Prima Materia's first minor release is schema and validation only. There is no
runtime, no plugin discovery, no audio, and no engine.

| | |
|---|---|
| `schemas/` | Body manifest, soul bundle, and motion pack, as JSON Schema. The **full** surface — every P0 field, every RSV field reserved for later releases, and the reserved V1 capability types. |
| `emet_sdk/types.py` | `Intent`, `Action`, `Pose`, `Twist`, `CapabilityDescriptor`, `LocomotionDescriptor`, `Health`, `Priority`, `Sensitivity`. |
| `emet_sdk/intents.py` | The closed intent vocabulary, plus the four names reserved from P0. |
| `emet_sdk/chains.py` | Fallback chain format, and the rule that every chain terminates in a voice rung. |
| `emet_sdk/validate.py` | Semantic rules and the error taxonomy. |
| `emet_sdk/cli.py` | `emet validate`. |

## Install

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
```

## Use

```sh
emet validate examples/bodiless.yaml
emet validate examples/scout-01.yaml examples/emet-soul.yaml --pair
emet validate examples/invalid/legged-no-plugin.yaml
```

Exit codes: `0` clean, `1` validation errors, `2` usage or IO failure.
Add `--strict` to fail on warnings, `--json` for machine-readable output.

## The two rules worth knowing

**Every fallback chain must end in a voice rung.** The hardware floor is a
microphone and a speaker, so a chain ending in voice can never fail to bind.
This is how "every intent is always satisfiable" becomes mechanical rather
than aspirational — no engine code checks it, because an unterminated chain
cannot get past the validator.

**A missing plugin is never a schema error.** `drive.kinematics` is an *open*
enum: any string naming an installed locomotion plugin is legal.
`kinematics: legged` is therefore a `MissingPluginError` — the value is
correct, the software just is not written yet. Conflating a typo with absent
hardware costs support hours, and closing that enum would make bipeds
unexpressible without migrating every robot in the field.

## Tests

```sh
.venv/bin/pytest
```

`tests/test_acceptance.py` is the 0.1 definition of done, executable.
