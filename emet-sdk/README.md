# emet-sdk

The contract layer for [Emet](../DESIGN.md) — types, schemas, and the intent
vocabulary that the engine and every plugin agree on.

Apache 2.0. Under ~2,000 lines on purpose: this is contracts and almost no
logic, because it is the one thing both sides of the boundary must share.

**HAL** below and throughout means *hardware abstraction layer*. Emet uses the
term in Android's sense: the abstraction itself lives here in
`emet_sdk.plugin`, and `emet-hal` is the collection of per-device
implementations that satisfy it.

## What is in 0.3

Schema, validation, the plugin contracts, and chain resolution. This package
still runs nothing: it is the layer the engine and every plugin agree on, and
it deliberately contains almost no logic.

| | |
|---|---|
| `schemas/` | Body manifest, soul bundle, and motion pack, as JSON Schema. The **full** surface — every P0 field, every RSV field reserved for later releases, and the reserved V1 capability types. |
| `emet_sdk/types.py` | `Intent`, `Action`, `Pose`, `Twist`, `CapabilityDescriptor`, `LocomotionDescriptor`, `WakeDescriptor`, `AudioFormat`, `AudioSource`, `AudioSink`, `Health`, `Priority`, `Sensitivity`. |
| `emet_sdk/intents.py` | The closed intent vocabulary, plus the four names reserved from P0. |
| `emet_sdk/chains.py` | Fallback chain format, and the rule that every chain terminates in a voice rung. |
| `emet_sdk/plugin.py` | `ActuatorPlugin`, `SensorPlugin`, `LocomotionPlugin`, `WakePlugin` — the public contract. |
| `emet_sdk/discovery.py` | Entry-point discovery across six groups. Installing a package is what makes a driver exist. |
| `emet_sdk/resolve.py` | Chain resolution: `(chains, descriptors) → binding table`. |
| `emet_sdk/validate.py` | Semantic rules and the error taxonomy. |
| `emet_sdk/cli.py` | `emet validate`, `emet explain`. |

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

emet explain examples/bodiless.yaml          # everything falls to voice
emet explain examples/mock-scout.yaml --why  # and why anything degraded
```

`explain` is the one to reach for when a robot is not doing what you expected.
It prints, for every intent, which part of *this* body performs it — and for
anything that fell short of its best option, which rungs were skipped and why:

```
  ~ express.affection  eyes   expression   hold_ms=1500 preset=soft
      skipped rung 0 {role: head, axis: roll}: 'head' has no 'roll' axis; it has pitch, yaw
```

**Validating and booting are deliberately different strictnesses.** Naming a
driver you have not installed is a warning, because describing hardware you
have not wired yet is a normal thing to do. `--verify-drivers` applies the rule
the engine will apply at boot, where it is an error.

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
