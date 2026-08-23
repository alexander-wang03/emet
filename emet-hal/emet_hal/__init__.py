"""Emet HAL — the hardware abstraction layer.

*HAL* is **hardware abstraction layer**, used in Android's sense: the
abstraction itself lives in `emet_sdk.plugin`, and this package is the
collection of per-device implementations that satisfy it.

Nothing here is imported by name. Plugins advertise themselves through entry
points, so installing a package is what makes a driver exist.

Shipped in 0.2:

    emet_hal.mock          an actuator and a sensor that pretend
    differential           two independently driven wheels
    tracked                the same arithmetic, plus tread scrub

No hardware drivers yet. The locomotion plugins are arithmetic and need none;
the mock exists so that the whole stack runs on a laptop.
"""

__version__ = "0.2.0"
