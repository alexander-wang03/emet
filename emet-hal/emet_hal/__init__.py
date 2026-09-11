"""Emet HAL: the hardware abstraction layer.

*HAL* is **hardware abstraction layer**, used in Android's sense: the
abstraction itself lives in `emet_sdk.plugin`, and this package is the
collection of per-device implementations that satisfy it.

Nothing here is imported by name. Plugins advertise themselves through entry
points, so installing a package is what makes a driver exist.

Shipped:

    emet_hal.mock          an actuator, a sensor, and a wake engine that pretend
    differential           two independently driven wheels
    tracked                the same arithmetic, plus tread scrub
    pocketsphinx           phonetic wake detection            (extra: wake)
    microphone / wav       audio in, live or from a recording (extra: audio)
    speaker / wav / null   audio out                          (extra: audio)

Still no hardware *drivers*: nothing here drives a servo or a motor controller.
The locomotion plugins are arithmetic and need none, audio goes through
PortAudio, and the mock exists so the whole stack runs on a laptop.
"""

from importlib import metadata as _metadata

#: Read from the installed distribution rather than written here, so that
#: `pyproject.toml` is the single place this number appears. Two declarations
#: drift silently: before this change the metadata said one version and the
#: source said another, and nothing noticed because nothing compared them.
try:
    __version__ = _metadata.version("emet-hal")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    # Imported from a tree that was never installed. Say so rather than
    # inventing a number that would later be reported as fact.
    __version__ = "0+unknown"
