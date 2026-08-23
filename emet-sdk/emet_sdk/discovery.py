"""Finding installed plugins.

Plugins are ordinary Python packages that advertise themselves through entry
points. Nothing scans directories, nothing imports by convention, and the
engine has no list of known drivers compiled into it — installing a package is
what makes a driver exist.

Three groups:

    emet.actuators    name = the string used in `driver.plugin`
    emet.sensors      name = the string used in `driver.plugin`
    emet.locomotion   name = the string used in `drive.kinematics`

For locomotion the entry-point name *is* the kinematics value, which is what
makes that enum genuinely open: `kinematics: legged` is legal today and
resolves the moment somebody publishes a package registering `legged`.

**Errors from this module are never schema errors.** A name that resolves to
no installed package is a `MissingPluginError` — the document is well-formed
and the value is legal, the software simply is not present. Conflating that
with a typo costs support hours, and closing the enum to avoid the ambiguity
would make walking robots inexpressible without migrating every manifest in
the field.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from typing import Iterable, Iterator, Mapping

__all__ = [
    "GROUP_ACTUATOR",
    "GROUP_SENSOR",
    "GROUP_LOCOMOTION",
    "PluginRegistry",
    "discover",
]

GROUP_ACTUATOR = "emet.actuators"
GROUP_SENSOR = "emet.sensors"
GROUP_LOCOMOTION = "emet.locomotion"


def _entry_points(group: str) -> dict[str, EntryPoint]:
    return {ep.name: ep for ep in entry_points(group=group)}


class PluginRegistry:
    """What is installed right now.

    Construct with `PluginRegistry.discover()` for the real thing, or pass
    explicit mappings in tests so that a test does not depend on what happens
    to be installed in the environment running it.
    """

    def __init__(
        self,
        actuators: Mapping[str, EntryPoint] | None = None,
        sensors: Mapping[str, EntryPoint] | None = None,
        locomotion: Mapping[str, EntryPoint] | None = None,
        *,
        verify_drivers: bool = False,
    ) -> None:
        self._actuators = dict(actuators or {})
        self._sensors = dict(sensors or {})
        self._locomotion = dict(locomotion or {})
        #: When False, an unrecognised *driver* name is reported as a warning
        #: rather than an error. See `validate` for why the two callers differ:
        #: linting a manifest for hardware you have not wired yet is a normal
        #: thing to do; booting an engine against it is not.
        self.verify_drivers = verify_drivers

    @classmethod
    @lru_cache(maxsize=1)
    def discover(cls) -> "PluginRegistry":
        """Read the entry points of every installed distribution."""
        return cls(
            actuators=_entry_points(GROUP_ACTUATOR),
            sensors=_entry_points(GROUP_SENSOR),
            locomotion=_entry_points(GROUP_LOCOMOTION),
        )

    def with_verification(self, verify_drivers: bool) -> "PluginRegistry":
        """A view of the same discovered plugins at a different strictness.

        `discover()` is cached and shared, so callers get a copy rather than
        flipping a flag other callers can see.
        """
        return PluginRegistry(
            actuators=self._actuators,
            sensors=self._sensors,
            locomotion=self._locomotion,
            verify_drivers=verify_drivers,
        )

    # ---------------------------------------------------------------- query

    def has_driver(self, plugin: str) -> bool:
        return plugin in self._actuators or plugin in self._sensors

    def has_locomotion(self, kinematics: str) -> bool:
        return kinematics in self._locomotion

    @property
    def driver_names(self) -> list[str]:
        return sorted({*self._actuators, *self._sensors})

    @property
    def locomotion_names(self) -> list[str]:
        return sorted(self._locomotion)

    def __bool__(self) -> bool:
        return bool(self._actuators or self._sensors or self._locomotion)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """(group, name) for everything installed. Used by `emet explain`."""
        for name in sorted(self._actuators):
            yield ("actuator", name)
        for name in sorted(self._sensors):
            yield ("sensor", name)
        for name in sorted(self._locomotion):
            yield ("locomotion", name)

    # ----------------------------------------------------------------- load

    def load_driver(self, plugin: str) -> type:
        """Import and return the plugin class for a `driver.plugin` name."""
        ep = self._actuators.get(plugin) or self._sensors.get(plugin)
        if ep is None:
            raise _missing(plugin, self.driver_names, "driver plugin")
        return ep.load()

    def load_locomotion(self, kinematics: str) -> type:
        """Import and return the plugin class for a `kinematics` value."""
        ep = self._locomotion.get(kinematics)
        if ep is None:
            raise _missing(kinematics, self.locomotion_names, "locomotion plugin")
        return ep.load()


def _missing(name: str, available: Iterable[str], what: str) -> Exception:
    # Imported lazily: validate imports discovery, so discovery must not
    # import validate at module scope.
    from emet_sdk.validate import MissingPluginError, ValidationReport

    report = ValidationReport()
    installed = ", ".join(available) or "(none)"
    report.error(
        "missing_plugin",
        f"no {what} provides {name!r}. Installed: {installed}. "
        f"The value is legal — install the package that provides it, or "
        f"correct the name.",
    )
    return MissingPluginError(report)


def discover() -> PluginRegistry:
    """Shorthand for `PluginRegistry.discover()`."""
    return PluginRegistry.discover()
