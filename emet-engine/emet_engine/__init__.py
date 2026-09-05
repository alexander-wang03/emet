"""Emet engine — the part that runs, as opposed to the part that describes.

Layering, enforced in CI: this package imports `emet_sdk` and nothing else.
Not for tidiness. The engine is where somebody would reach for a concrete
servo or a concrete microphone, and one `from emet_hal.differential import ...`
would quietly end the claim that the engine holds no hardware knowledge. Every
driver, detector and audio source arrives by name through entry-point
discovery instead.

`emet-hal` is therefore not a dependency of this package. It is a dependency of
a working robot, which is a different thing: install it alongside.
"""

from emet_engine.session import EngineError, ListenSession
from emet_engine.turn import DEFAULT_PATIENCE_MS, EndReason, Endpointer, Utterance
from emet_engine.vad import EnergyVad, VadTuning

from importlib import metadata as _metadata

#: Read from the installed distribution rather than written here, so that
#: `pyproject.toml` is the single place this number appears. Two declarations
#: drift silently: before this change the metadata said one version and the
#: source said another, and nothing noticed because nothing compared them.
try:
    __version__ = _metadata.version("emet-engine")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    # Imported from a tree that was never installed. Say so rather than
    # inventing a number that would later be reported as fact.
    __version__ = "0+unknown"

__all__ = [
    "EngineError",
    "ListenSession",
    "Endpointer",
    "Utterance",
    "EndReason",
    "EnergyVad",
    "VadTuning",
    "DEFAULT_PATIENCE_MS",
    "__version__",
]
