"""Emet SDK — the contract layer.

Types and contracts and almost no logic. This is the only thing the engine and
every plugin must agree on, which is why it is small on purpose and why
changes here are versioned carefully.

    from emet_sdk import Intent, Action, CapabilityDescriptor

Layering, enforced in CI: `emet_sdk` imports nothing internal. `emet_hal`
imports `emet_sdk` only. `emet_engine` imports `emet_sdk` only.
"""

from emet_sdk.plugin import (
    ActuatorPlugin,
    CapabilityPlugin,
    LocomotionPlugin,
    PluginError,
    SensorPlugin,
    WakePlugin,
)
from emet_sdk.types import (
    Action,
    AudioFormat,
    AudioSource,
    CapabilityDescriptor,
    Health,
    Intent,
    LocomotionDescriptor,
    MemoryKind,
    Pose,
    Priority,
    Reading,
    Sensitivity,
    Target,
    TargetKind,
    Twist,
    WakeDescriptor,
    WakeEvent,
)

__version__ = "0.2.0"

#: Bumped when a released schema changes shape. Manifests and bundles record
#: the version they were written against; a document from a newer SDK is a
#: hard error, an older one is migrated forward with a backup left beside it.
SCHEMA_VERSION = "0.1"

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "Action",
    "ActuatorPlugin",
    "AudioFormat",
    "AudioSource",
    "CapabilityPlugin",
    "LocomotionPlugin",
    "PluginError",
    "Reading",
    "SensorPlugin",
    "WakePlugin",
    "CapabilityDescriptor",
    "Health",
    "Intent",
    "LocomotionDescriptor",
    "MemoryKind",
    "Pose",
    "Priority",
    "Sensitivity",
    "Target",
    "TargetKind",
    "Twist",
    "WakeDescriptor",
    "WakeEvent",
]
