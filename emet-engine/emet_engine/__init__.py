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

__version__ = "0.2.0"

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
