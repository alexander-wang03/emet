"""Emet SDK — the contract layer.

Types and contracts and almost no logic. This is the only thing the engine and
every plugin must agree on, which is why it is small on purpose and why
changes here are versioned carefully.

    from emet_sdk import Intent, Action, CapabilityDescriptor

Layering, enforced in CI: `emet_sdk` imports nothing internal. `emet_hal`
imports `emet_sdk` only. `emet_engine` imports `emet_sdk` only.
"""

from importlib import metadata as _metadata

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

#: Read from the installed distribution rather than written here, so that
#: `pyproject.toml` is the single place this number appears. Two declarations
#: drift silently: before this change the metadata said one version and the
#: source said another, and nothing noticed because nothing compared them.
try:
    __version__ = _metadata.version("emet-sdk")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    # Imported from a tree that was never installed. Say so rather than
    # inventing a number that would later be reported as fact.
    __version__ = "0+unknown"

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
