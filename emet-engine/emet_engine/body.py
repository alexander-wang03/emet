"""The parts a manifest declares: built, started, described, and bound.

Until 0.5 the engine never started a capability plugin. The resolver and
`emet explain` existed, and a body's chains were resolved only on a laptop,
against what the manifest claimed. This module is the boot half of principle
5 (degradation is declared, not improvised): every part the manifest declares
is built by name through discovery, started, and asked what it can actually
do, and every chain is resolved once against those answers. At runtime,
performing an intent is a dictionary lookup.

**What the manifest claims and what the hardware says.** A joint group whose
servo board did not answer raises `PluginError` from `start()`. The part is
then recorded as present and unhealthy, its descriptor says so, and every
chain that wanted it falls through to its next rung, down to the voice if
need be. A robot with a dead servo is still a robot that can talk. A driver
that is not *installed* is a different failure and is not softened here:
discovery raises `MissingPluginError` and boot stops, naming the plugin,
because a typo in a manifest must never look like missing hardware.

**A drive is two plugins.** `driver.plugin` is the motor driver, an actuator
the chains bind to; `kinematics` is the locomotion plugin, which knows how
this body moves and reports it in a `LocomotionDescriptor` for the
self-model. If either fails to start, the drive is unhealthy as a whole: a
motor driver with no arithmetic above it moves nothing, and arithmetic with
no motors below it is the same.

**Body-local state reaches the plugins here.** A joint's `trim_deg` in the
body's state file wins over the manifest's, which is the builder's first
guess until calibration writes a measured one (`DESIGN.md` section 4.1).
Whatever a plugin returned from `carry_over()` at the last shutdown on this
body is merged over its `driver.params`, unless the body was built with
`carry=False`, as it is for a replay: then the trims alone reach it.

Reserved capability types (`manipulator`, `audio_extra`, `projector`,
`thermal`) have no plugins yet. They are listed in `reserved` for the
self-model, which reads them from the manifest, and nothing is built.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from emet_sdk.chains import VOICE_ACTIONS, Chain
from emet_sdk.discovery import PluginRegistry
from emet_sdk.plugin import PluginError
from emet_sdk.resolve import Binding, BindingTable, descriptors_from_manifest, resolve
from emet_sdk.types import Action, CapabilityDescriptor, Health, LocomotionDescriptor

if TYPE_CHECKING:  # pragma: no cover
    from emet_engine.state import BodyState

__all__ = ["Body", "RESERVED_TYPES", "format_bindings"]

log = logging.getLogger("emet_engine.body")

#: Capability types the schema accepts and the P0 engine ignores. No plugin
#: category exists for them yet, so nothing is built.
RESERVED_TYPES: frozenset[str] = frozenset({"manipulator", "audio_extra", "projector", "thermal"})


def carry_key(capability_id: str) -> str:
    """Where a capability's driver keeps its carried params in the state file."""
    return f"capability.{capability_id}"


def locomotion_key(capability_id: str) -> str:
    """Where a drive's locomotion plugin keeps its carried params."""
    return f"locomotion.{capability_id}"


class Body:
    """Every declared part, started, and the binding table resolved against it.

    `start()` builds and starts the plugins in manifest order; `shutdown()`
    stops them in reverse, and raises nothing but a cancellation. After
    `start()`:

    * `capabilities` is what the plugins reported, one descriptor per part;
    * `locomotion` is the drive's `LocomotionDescriptor`, or None;
    * `health` is each part's `Health`, which is also what the state file
      records;
    * `table` is the `BindingTable` every intent is performed through.
    """

    def __init__(
        self,
        manifest: Mapping[str, Any],
        registry: PluginRegistry,
        *,
        chains: Mapping[str, Chain],
        state: "BodyState | None" = None,
        carry: bool = True,
    ) -> None:
        self.manifest = manifest
        self.registry = registry
        self.chains = chains
        self.state = state
        #: Whether the plugins are handed what they carried over last time.
        self.carry = carry
        self.capabilities: tuple[CapabilityDescriptor, ...] = ()
        self.locomotion: LocomotionDescriptor | None = None
        self.health: dict[str, Health] = {}
        self.table: BindingTable = resolve(chains, ())
        #: Capability ids of declared parts whose type is reserved.
        self.reserved: list[str] = []
        self._plugins: dict[str, Any] = {}
        self._locomotion_plugin: Any = None
        self._locomotion_plugins: dict[str, Any] = {}
        self._started: list[Any] = []

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        described: list[CapabilityDescriptor] = []
        for block in self.manifest.get("capabilities") or []:
            if not isinstance(block, Mapping):
                continue
            cap_id = str(block.get("id") or "")
            cap_type = str(block.get("type") or "")
            if cap_type in RESERVED_TYPES:
                self.reserved.append(cap_id)
                continue
            described.append(await self._start_part(block))
        self.capabilities = tuple(described)
        self.table = resolve(self.chains, self.capabilities)
        for line in format_bindings(self.table):
            log.info("binding: %s", line)

    def _prepared(self, block: Mapping[str, Any], key: str) -> dict[str, Any]:
        """A copy of the manifest's block with this body's state laid over it:
        its trims, and what the plugin under `key` carried over, merged over
        `driver.params`. The state file wins over the builder's guess."""
        prepared: dict[str, Any] = (
            self.state.overlay_trims(block) if self.state is not None else copy.deepcopy(dict(block))
        )
        carried = self.state.carried(key) if self.state is not None and self.carry else {}
        if carried:
            driver = dict(prepared.get("driver") or {})
            driver["params"] = {**dict(driver.get("params") or {}), **carried}
            prepared["driver"] = driver
        return prepared

    async def _start_part(self, declared: Mapping[str, Any]) -> CapabilityDescriptor:
        cap_id = str(declared.get("id") or "")
        block = self._prepared(declared, carry_key(cap_id))
        plugin_name = str((block.get("driver") or {}).get("plugin") or "")
        # Raises MissingPluginError for a package that is not installed. Boot
        # stops there, on purpose.
        plugin = self.registry.load_driver(plugin_name)(block)
        self._plugins[cap_id] = plugin
        descriptor = await self._started_descriptor(plugin, block)

        if block.get("type") == "drive":
            # The locomotion plugin reads its own copy of the manifest's
            # block, with what it carried over under a key of its own and
            # none of what the motor driver carried.
            moves = self._prepared(declared, locomotion_key(cap_id))
            locomotion = self.registry.load_locomotion(str(block.get("kinematics") or ""))(moves)
            self._locomotion_plugin = locomotion
            self._locomotion_plugins[cap_id] = locomotion
            fault = await self._start(locomotion, cap_id)
            if fault is None:
                try:
                    self.locomotion = locomotion.describe()
                except Exception as exc:  # noqa: BLE001 - a broken describe() is a fault and the boot goes on
                    fault = f"{type(exc).__name__}: {exc}"
            if fault is not None:
                self.locomotion = None
                self.health[cap_id] = Health(ok=False, detail=fault, faults=("locomotion",))
                descriptor = replace(descriptor, healthy=False)
        return descriptor

    async def _started_descriptor(self, plugin: Any, block: Mapping[str, Any]) -> CapabilityDescriptor:
        cap_id = str(block.get("id") or "")
        fault = await self._start(plugin, cap_id)
        descriptor: CapabilityDescriptor | None = None
        if fault is None:
            try:
                descriptor = plugin.describe()
            except Exception as exc:  # noqa: BLE001 - see above
                fault = f"{type(exc).__name__}: {exc}"
        try:
            health = plugin.health()
        except Exception as exc:  # noqa: BLE001
            health = Health(ok=False, detail=f"{type(exc).__name__}: {exc}", faults=("health",))
        if fault is not None:
            health = Health(ok=False, detail=fault, faults=("start_failed",))
        if descriptor is None:
            # What the manifest claims, marked dead, so chains fall through.
            (static,) = descriptors_from_manifest({"capabilities": [block]})
            descriptor = replace(static, healthy=False)
        if not health.ok and descriptor.healthy:
            # A plugin that started, describes itself as healthy and reports
            # unhealthy is believed on the side of caution.
            descriptor = replace(descriptor, healthy=False)
        if not descriptor.healthy and health.ok:
            health = Health(ok=False, detail="reported unhealthy", faults=("unhealthy",))
        self.health[cap_id] = health
        return descriptor

    async def _start(self, plugin: Any, cap_id: str) -> str | None:
        """Start one plugin. Returns the fault, or None.

        The plugin is recorded for `shutdown()` before `start()` runs, since
        shutdown must not assume start succeeded: a part that raised half
        way up, or a boot cancelled while it started, is still put to rest.
        `PluginError` is the contract's way to say the hardware is not
        there; any other exception from a driver (an `OSError` from a bus
        that did not answer) is a fault the same way, and the part is bound
        past. A cancellation is never swallowed.
        """
        self._started.append(plugin)
        try:
            await plugin.start()
        except Exception as exc:  # noqa: BLE001 - a dead part is bound past, whatever it raised
            detail = str(exc) or type(exc).__name__
            if not isinstance(exc, PluginError):
                detail = f"{type(exc).__name__}: {detail}"
            log.warning("body: %s did not start: %s", cap_id, detail)
            return detail
        return None

    async def shutdown(self) -> None:
        """Stop every plugin, last started first. A plugin that fails is
        logged. A cancellation is raised, once every plugin has been told to
        stop; nothing else is."""
        cancelled = False
        while self._started:
            plugin = self._started.pop()
            try:
                await plugin.shutdown()
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:  # noqa: BLE001 - one broken part must not strand the rest
                log.warning("body: shutdown of %s failed: %s", type(plugin).__name__, exc)
        if cancelled:
            raise asyncio.CancelledError()

    # ------------------------------------------------------------- queries

    @property
    def faults(self) -> dict[str, str]:
        """Declared parts that are not working, with the reason."""
        return {
            cap_id: health.detail or "not working"
            for cap_id, health in self.health.items()
            if not health.ok
        }

    def plugin(self, capability_id: str) -> Any:
        return self._plugins.get(capability_id)

    def carry_over(self) -> dict[str, dict[str, Any]]:
        """What each part's plugins want back at the next boot, by state key:
        every driver under `capability.<id>`, and a drive's locomotion plugin
        under `locomotion.<id>`."""
        asked = [(carry_key(cap_id), plugin) for cap_id, plugin in self._plugins.items()]
        asked += [(locomotion_key(cap_id), plugin) for cap_id, plugin in self._locomotion_plugins.items()]
        out: dict[str, dict[str, Any]] = {}
        for key, plugin in asked:
            try:
                out[key] = dict(plugin.carry_over() or {})
            except Exception as exc:  # noqa: BLE001
                log.warning("body: carry_over for %s failed: %s", key, exc)
        return out

    # ---------------------------------------------------------------- acting

    async def apply(self, binding: Binding, action: Action) -> None:
        """Hand one action to the part a hardware binding names."""
        plugin = self._plugins.get(binding.capability_id or "")
        apply = getattr(plugin, "apply", None)
        if apply is None:
            raise PluginError(f"{binding.capability_id!r} cannot be driven: it has no apply()")
        await apply(action)


def format_bindings(table: BindingTable, *, width: int = 64) -> list[str]:
    """The binding table as the boot log shows it, grouped by what performs it.

    A line per part and action, the intents it performs listed after and
    wrapped at `width`, so a bodiless body's thirty-one bindings fit on one
    screen as six groups and a body with a head shows at a glance which
    intents reached it. Hardware first, by capability id, then the voice
    rung's actions in the order `DESIGN.md` section 6.1 gives them. `emet
    explain` is the per-intent view, and `emet explain --why` adds the
    skipped rungs and their reasons.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for name in table:
        binding = table[name]
        groups.setdefault((binding.target, binding.action), []).append(name)

    voice_order = ["utter", "inflect", "tone", "explain", "backchannel", "silence"]
    voice_order += sorted(VOICE_ACTIONS - set(voice_order))

    def order(key: tuple[str, str]) -> tuple[int, str, int, str]:
        target, action = key
        if target != "voice":
            return (0, target, 0, action)
        rank = voice_order.index(action) if action in voice_order else len(voice_order)
        return (1, "", rank, action)

    head_width = max((len(t) + 2 + len(a) for t, a in groups), default=0)
    lines: list[str] = []
    for key in sorted(groups, key=order):
        target, action = key
        current = f"{target}  {action}".ljust(head_width) + " "
        on_line = 0
        for name in sorted(groups[key]):
            if on_line and len(current) + 1 + len(name) > width:
                lines.append(current)
                current, on_line = " " * (head_width + 1), 0
            current, on_line = f"{current} {name}", on_line + 1
        lines.append(current)
    return lines
