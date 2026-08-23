# emet-hal

The hardware abstraction layer for [Emet](../DESIGN.md) — drivers and
locomotion plugins.

Apache 2.0, community-contributed. This is the package the project most wants
pull requests against: a driver that turns out to be wrong is reverted in a
minute, while a change to the SDK's contracts is with every robot in the field
for years.

## What ships today

| Entry point | Group | What it is |
|---|---|---|
| `emet_hal.mock` | actuator | Logs what it was asked to do and moves nothing |
| `emet_hal.mock_sensor` | sensor | Returns fixed readings |
| `differential` | locomotion | Two independently driven wheels |
| `tracked` | locomotion | The same arithmetic, plus tread scrub |

**No hardware drivers yet.** The locomotion plugins are arithmetic — they turn
a desired velocity into per-wheel speeds and touch no GPIO — so they need no
robot and are fully testable against the mock.

## How a plugin is found

Nothing scans directories, and the engine has no compiled-in list of known
hardware. Plugins advertise themselves through entry points:

```toml
[project.entry-points."emet.actuators"]
"emet_hal.pca9685" = "emet_hal.pca9685:PCA9685Servos"

[project.entry-points."emet.locomotion"]
"legged" = "your_package.legged:StaticGait"
```

For actuators and sensors the entry-point name is what a manifest puts in
`driver.plugin`. For locomotion it is the value of `drive.kinematics` — which
is what makes that enum genuinely open. `kinematics: legged` is legal today
and resolves the moment somebody publishes a package registering `legged`.

## Writing one

Implement `ActuatorPlugin`, `SensorPlugin`, or `LocomotionPlugin` from
`emet_sdk.plugin`, register an entry point, and install the package.

Two things worth getting right:

**`describe()` reports what the instance can *actually* do, after `start()`.**
Chains bind against your descriptor, not against the manifest. Claiming an
axis you cannot drive means a chain binds to you and the robot silently does
nothing — worse than falling through to a light ring.

**`apply()` must return promptly.** Long moves are driven by repeated calls
from the choreographer at 50Hz. Sleeping for the duration of a gesture blocks
the loop that would otherwise let a higher-priority intent preempt it.

## Develop and test

```sh
pip install -e ../emet-sdk -e ".[dev]"
pytest
```
