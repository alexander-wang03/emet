"""Finding installed plugins.

Plugins are ordinary Python packages that advertise themselves through entry
points. Nothing scans directories, nothing imports by convention, and the
engine has no list of known drivers compiled into it; installing a package is
what makes a driver exist.

Nine groups:

    emet.actuators    name = the string used in `driver.plugin`
    emet.sensors      name = the string used in `driver.plugin`
    emet.locomotion   name = the string used in `drive.kinematics`
    emet.wake         name = the string used in `audio.wake.engine`
    emet.audio        name = the string used in `audio.input.source`
    emet.audio_out    name = the string used in `audio.output.sink`
    emet.stt          name = the string used in `models.stt.provider`, on the
                      soul or on the body
    emet.llm          name = the string used in `models.chat.provider`, on the
                      soul or on the body
    emet.tts          name = the string used in `models.tts.provider`, on the
                      soul or on the body

`emet.audio` exists for a reason the others do not share. The engine may import
`emet_sdk` and nothing else, so it cannot reach into `emet_hal` for a
microphone even though that is exactly where microphones live. Discovery is
what carries one across the boundary: the engine asks for `microphone` and
receives a class it never imported. Without this group the layering rule and a
working engine are mutually exclusive.

It earns its place a second way. Pointing a body at `wav` instead of
`microphone` replays a recording through the identical path, which is how you
reproduce a wake failure somebody reports without owning their room.

For locomotion, wake and audio the entry-point name *is* the manifest value,
which is what makes those enums genuinely open: `kinematics: legged` is legal today and
resolves the moment somebody publishes a package registering `legged`.

`emet.stt`, `emet.llm` and `emet.tts` are the groups whose names come from
the *soul* rather than the body: `models.stt.provider`, `models.chat.provider`
and `models.tts.provider`. Each is an account or a voice the owner brings,
and that travels with the soul, so the soul names it. The body may override
any of them, and a test rig does. Either way the name resolves here, against
what is installed, and a provider nobody has packaged yet is a
`MissingPluginError` on the day it is named, exactly like `legged`.

That openness is not theoretical for wake. Picovoice disabled every free
Porcupine access key on 30 June 2026, and every project that had wired one
detector in directly stopped waking. Here it would have been one line of a
manifest.

**Errors from this module are never schema errors.** A name that resolves to
no installed package is a `MissingPluginError`: the document is well-formed
and the value is legal, the software simply is not present. Conflating that
with a typo costs support hours, and closing the enum to avoid the ambiguity
would make walking robots inexpressible without migrating every manifest in
the field.

**Where each plugin came from.** Discovery keeps the distribution that
declared each entry point, and its version, as a `PluginOrigin`, so the boot
lines can say `from emet-providers 0.5.7` beside each provider. Two
different distributions that offer one name in one group are refused with
a `DuplicatePluginError` naming both, when that name is loaded: which one
Python finds first depends on where each is installed and on the order the
file system lists a folder in, which nobody chose, and a package that
takes a provider's name would be handed that provider's key. One
distribution found twice on the path is one claimant. The same name in two
groups, such as `mock` for a voice and for a language model, is no clash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from itertools import chain
from typing import Any, Iterable, Iterator, Mapping

__all__ = [
    "GROUP_ACTUATOR",
    "GROUP_SENSOR",
    "GROUP_LOCOMOTION",
    "GROUP_WAKE",
    "GROUP_AUDIO",
    "GROUP_AUDIO_OUT",
    "GROUP_STT",
    "GROUP_LLM",
    "GROUP_TTS",
    "PluginOrigin",
    "PluginRegistry",
    "discover",
]

GROUP_ACTUATOR = "emet.actuators"
GROUP_SENSOR = "emet.sensors"
GROUP_LOCOMOTION = "emet.locomotion"
GROUP_WAKE = "emet.wake"
GROUP_AUDIO = "emet.audio"
GROUP_AUDIO_OUT = "emet.audio_out"
GROUP_STT = "emet.stt"
GROUP_LLM = "emet.llm"
GROUP_TTS = "emet.tts"

#: Every group, and what its plugins are called in a refusal.
_GROUPS: Mapping[str, str] = {
    GROUP_ACTUATOR: "driver plugin",
    GROUP_SENSOR: "driver plugin",
    GROUP_LOCOMOTION: "locomotion plugin",
    GROUP_WAKE: "wake word plugin",
    GROUP_AUDIO: "audio source",
    GROUP_AUDIO_OUT: "audio sink",
    GROUP_STT: "speech recognition provider",
    GROUP_LLM: "language model provider",
    GROUP_TTS: "speech synthesis provider",
}


@dataclass(frozen=True, slots=True)
class PluginOrigin:
    """Where one installed plugin comes from.

    `distribution` and `version` are those of the installed package whose
    metadata declared the entry point, read at discovery. Both are None for
    an entry point that no distribution declared, which only a test builds.
    """

    group: str
    name: str
    #: The entry point's target, `module:attribute`.
    value: str
    distribution: str | None = None
    version: str | None = None

    @property
    def label(self) -> str | None:
        """The distribution and its version as a boot line prints them, or None."""
        if self.distribution is None:
            return None
        return f"{self.distribution} {self.version}" if self.version else self.distribution


def _distribution_of(dist: Any) -> tuple[str | None, str | None]:
    """The name and version a distribution's metadata gives, in one parse.

    `dist.name` and `dist.version` each parse the whole METADATA file, the
    package's README included, so the file is read once here for both. A
    dist-info folder left with its entry points and no METADATA, as an
    interrupted install can leave one, has no name to read, and its entry
    points count as ones no distribution declared.
    """
    if dist is None:
        return None, None
    try:
        meta = dist.metadata
        name, version = meta.get("Name"), meta.get("Version")
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None, None
    if not name:
        return None, None
    return name, version or None


def _origin(ep: EntryPoint, read: tuple[str | None, str | None] | None = None) -> PluginOrigin:
    name, version = read if read is not None else _distribution_of(getattr(ep, "dist", None))
    return PluginOrigin(group=ep.group, name=ep.name, value=ep.value, distribution=name, version=version)


def _claimant(origin: PluginOrigin) -> str:
    """Who offers an entry point: its distribution's normalised name (PEP 503),
    or, for one no distribution declared, its target."""
    if origin.distribution is None:
        return f"={origin.value}"
    return re.sub(r"[-_.]+", "-", origin.distribution).lower()


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
        wake: Mapping[str, EntryPoint] | None = None,
        audio: Mapping[str, EntryPoint] | None = None,
        audio_out: Mapping[str, EntryPoint] | None = None,
        stt: Mapping[str, EntryPoint] | None = None,
        llm: Mapping[str, EntryPoint] | None = None,
        tts: Mapping[str, EntryPoint] | None = None,
        *,
        verify_drivers: bool = False,
    ) -> None:
        self._actuators = dict(actuators or {})
        self._sensors = dict(sensors or {})
        self._locomotion = dict(locomotion or {})
        self._wake = dict(wake or {})
        self._audio = dict(audio or {})
        self._audio_out = dict(audio_out or {})
        self._stt = dict(stt or {})
        self._llm = dict(llm or {})
        self._tts = dict(tts or {})
        #: When False, an unrecognised *driver* name is reported as a warning
        #: rather than an error. See `validate` for why the two callers differ:
        #: linting a manifest for hardware you have not wired yet is a normal
        #: thing to do; booting an engine against it is not.
        self.verify_drivers = verify_drivers
        #: (group, name) to the entry points of two or more distributions
        #: that offer that name, which `from_entry_points` fills.
        self._claims: dict[tuple[str, str], tuple[EntryPoint, ...]] = {}
        #: (group, name) to where the entry point that name loads comes from,
        #: read once by `from_entry_points`; `origin` reads it again for a
        #: registry built from mappings.
        self._origins: dict[tuple[str, str], PluginOrigin] = {}

    @classmethod
    @lru_cache(maxsize=1)
    def discover(cls) -> "PluginRegistry":
        """Read the entry points of every installed distribution."""
        return cls.from_entry_points(chain.from_iterable(entry_points(group=group) for group in _GROUPS))

    @classmethod
    def from_entry_points(cls, eps: Iterable[EntryPoint], *, verify_drivers: bool = False) -> "PluginRegistry":
        """A registry of these entry points, each kept with the distribution that declared it.

        Entry points outside the nine groups are ignored. For each name in a
        group the first entry point is the one loaded, and a name that two
        distributions offer is kept as a clash, which `load_*` and `origin`
        refuse. `discover()` passes what is installed; a test passes entry
        points read from distributions it wrote.
        """
        found: dict[str, dict[str, EntryPoint]] = {group: {} for group in _GROUPS}
        offered: dict[tuple[str, str], dict[str, EntryPoint]] = {}
        origins: dict[tuple[str, str], PluginOrigin] = {}
        # One read of each distribution's metadata, however many entry
        # points it declares. The distribution is held beside what was read,
        # so its id cannot be reused by another while this loop runs.
        read: dict[int, tuple[Any, tuple[str | None, str | None]]] = {}
        for ep in eps:
            if ep.group not in found:
                continue
            dist = getattr(ep, "dist", None)
            if id(dist) not in read:
                read[id(dist)] = (dist, _distribution_of(dist))
            origin = _origin(ep, read[id(dist)][1])
            found[ep.group].setdefault(ep.name, ep)
            origins.setdefault((ep.group, ep.name), origin)
            offered.setdefault((ep.group, ep.name), {}).setdefault(_claimant(origin), ep)
        registry = cls(
            actuators=found[GROUP_ACTUATOR],
            sensors=found[GROUP_SENSOR],
            locomotion=found[GROUP_LOCOMOTION],
            wake=found[GROUP_WAKE],
            audio=found[GROUP_AUDIO],
            audio_out=found[GROUP_AUDIO_OUT],
            stt=found[GROUP_STT],
            llm=found[GROUP_LLM],
            tts=found[GROUP_TTS],
            verify_drivers=verify_drivers,
        )
        registry._claims = {key: tuple(by.values()) for key, by in offered.items() if len(by) > 1}
        registry._origins = origins
        return registry

    def with_verification(self, verify_drivers: bool) -> "PluginRegistry":
        """A view of the same discovered plugins at a different strictness.

        `discover()` is cached and shared, so callers get a copy rather than
        flipping a flag other callers can see.
        """
        view = PluginRegistry(
            actuators=self._actuators,
            sensors=self._sensors,
            locomotion=self._locomotion,
            wake=self._wake,
            audio=self._audio,
            audio_out=self._audio_out,
            stt=self._stt,
            llm=self._llm,
            tts=self._tts,
            verify_drivers=verify_drivers,
        )
        view._claims = dict(self._claims)
        view._origins = dict(self._origins)
        return view

    def origin(self, group: str, name: str) -> PluginOrigin | None:
        """Where the plugin a name loads comes from, or None when none is installed.

        `group` is one of the `GROUP_*` names. A name two distributions offer
        raises `DuplicatePluginError`, as loading it does.
        """
        ep = self._group(group).get(name)
        if ep is None:
            return None
        self._refuse_a_clash(group, name)
        return self._origins.get((group, name)) or _origin(ep)

    def _group(self, group: str) -> dict[str, EntryPoint]:
        by_group = {
            GROUP_ACTUATOR: self._actuators,
            GROUP_SENSOR: self._sensors,
            GROUP_LOCOMOTION: self._locomotion,
            GROUP_WAKE: self._wake,
            GROUP_AUDIO: self._audio,
            GROUP_AUDIO_OUT: self._audio_out,
            GROUP_STT: self._stt,
            GROUP_LLM: self._llm,
            GROUP_TTS: self._tts,
        }
        if group not in by_group:
            raise ValueError(f"no entry-point group {group!r}. The groups are {', '.join(sorted(by_group))}.")
        return by_group[group]

    def _refuse_a_clash(self, group: str, name: str) -> None:
        claims = self._claims.get((group, name))
        if claims:
            raise _duplicate(group, name, claims)

    def _load(self, group: str, name: str, available: Iterable[str]) -> type:
        ep = self._group(group).get(name)
        if ep is None:
            raise _missing(name, available, _GROUPS[group])
        self._refuse_a_clash(group, name)
        return ep.load()

    # ---------------------------------------------------------------- query

    def has_driver(self, plugin: str) -> bool:
        return plugin in self._actuators or plugin in self._sensors

    def has_locomotion(self, kinematics: str) -> bool:
        return kinematics in self._locomotion

    def has_wake(self, engine: str) -> bool:
        return engine in self._wake

    def has_audio(self, source: str) -> bool:
        return source in self._audio

    def has_audio_out(self, sink: str) -> bool:
        return sink in self._audio_out

    def has_stt(self, provider: str) -> bool:
        return provider in self._stt

    def has_llm(self, provider: str) -> bool:
        return provider in self._llm

    def has_tts(self, provider: str) -> bool:
        return provider in self._tts

    @property
    def driver_names(self) -> list[str]:
        return sorted({*self._actuators, *self._sensors})

    @property
    def locomotion_names(self) -> list[str]:
        return sorted(self._locomotion)

    @property
    def wake_names(self) -> list[str]:
        return sorted(self._wake)

    @property
    def audio_names(self) -> list[str]:
        return sorted(self._audio)

    @property
    def audio_out_names(self) -> list[str]:
        return sorted(self._audio_out)

    @property
    def stt_names(self) -> list[str]:
        return sorted(self._stt)

    @property
    def llm_names(self) -> list[str]:
        return sorted(self._llm)

    @property
    def tts_names(self) -> list[str]:
        return sorted(self._tts)

    def __bool__(self) -> bool:
        return bool(
            self._actuators
            or self._sensors
            or self._locomotion
            or self._wake
            or self._audio
            or self._audio_out
            or self._stt
            or self._llm
            or self._tts
        )

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """(group, name) for everything installed. Used by `emet explain`."""
        for name in sorted(self._actuators):
            yield ("actuator", name)
        for name in sorted(self._sensors):
            yield ("sensor", name)
        for name in sorted(self._locomotion):
            yield ("locomotion", name)
        for name in sorted(self._wake):
            yield ("wake", name)
        for name in sorted(self._audio):
            yield ("audio", name)
        for name in sorted(self._audio_out):
            yield ("audio_out", name)
        for name in sorted(self._stt):
            yield ("stt", name)
        for name in sorted(self._llm):
            yield ("llm", name)
        for name in sorted(self._tts):
            yield ("tts", name)

    # ----------------------------------------------------------------- load

    def load_driver(self, plugin: str) -> type:
        """Import and return the plugin class for a `driver.plugin` name."""
        group = GROUP_ACTUATOR if plugin in self._actuators else GROUP_SENSOR
        return self._load(group, plugin, self.driver_names)

    def load_locomotion(self, kinematics: str) -> type:
        """Import and return the plugin class for a `kinematics` value."""
        return self._load(GROUP_LOCOMOTION, kinematics, self.locomotion_names)

    def load_wake(self, engine: str) -> type:
        """Import and return the plugin class for an `audio.wake.engine` name."""
        return self._load(GROUP_WAKE, engine, self.wake_names)

    def load_audio(self, source: str) -> type:
        """Import and return the class for an `audio.input.source` name.

        The returned class is constructed as `cls(config, fmt)`, where `config`
        is the manifest's `audio.input` block and `fmt` the `AudioFormat` the
        consumer needs. That convention is the audio equivalent of a plugin
        receiving its capability block, and it is what lets the engine build a
        microphone it cannot import.
        """
        return self._load(GROUP_AUDIO, source, self.audio_names)

    def load_audio_out(self, sink: str) -> type:
        """Import and return the class for an `audio.output.sink` name.

        Constructed as `cls(config, fmt)` with the manifest's `audio.output`
        block, the same convention sources follow.
        """
        return self._load(GROUP_AUDIO_OUT, sink, self.audio_out_names)

    def load_stt(self, provider: str) -> type:
        """Import and return the plugin class for a speech recognition provider.

        Constructed as `cls(config, fmt)`, where `config` is the merged
        provider reference from `emet_sdk.models.stt_selection` and `fmt` the
        `AudioFormat` the wake engine fixed. Same shape as a source or a sink,
        for the same reason: the engine holds a name and a mapping.
        """
        return self._load(GROUP_STT, provider, self.stt_names)

    def load_llm(self, provider: str) -> type:
        """Import and return the plugin class for a language model provider.

        Constructed as `cls(config)`, where `config` is the merged provider
        reference from `emet_sdk.models.chat_selection`. No audio format: a
        language model never hears the microphone.
        """
        return self._load(GROUP_LLM, provider, self.llm_names)

    def load_tts(self, provider: str) -> type:
        """Import and return the plugin class for a speech synthesis provider.

        Constructed as `cls(config, voice)`, where `config` is the merged
        provider reference from `emet_sdk.models.tts_selection` and `voice`
        the soul's `voice` block. The voice states its own sample rate after
        `start()`, and the sink is opened to match.
        """
        return self._load(GROUP_TTS, provider, self.tts_names)


_COUNTS = {2: "two", 3: "three", 4: "four"}


def _duplicate(group: str, name: str, claims: Iterable[EntryPoint]) -> Exception:
    """The refusal of a name more than one distribution offers in one group,
    naming each of them and how to remove one."""
    from emet_sdk.validate import DuplicatePluginError, ValidationReport

    origins = sorted((_origin(ep) for ep in claims), key=lambda o: ((o.distribution or "").lower(), o.value))
    named = [f"{o.label} ({o.value})" if o.label else f"an entry point no distribution declared ({o.value})"
             for o in origins]
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    commands = [f"pip uninstall {o.distribution}" for o in origins if o.distribution]
    remove = f" Uninstall the one you did not mean to install, then start again: {', or '.join(commands)}." if commands else ""
    report = ValidationReport()
    report.error(
        "duplicate_plugin",
        f"{_COUNTS.get(len(origins), str(len(origins)))} installed distributions offer the "
        f"{_GROUPS[group]} {name!r} in the group {group}: {listed}. Emet will not choose "
        f"one for you, since which one Python finds first depends on where each is "
        f"installed and on the order the file system lists a folder in, which nobody "
        f"chose.{remove}",
    )
    return DuplicatePluginError(report)


def _missing(name: str, available: Iterable[str], what: str) -> Exception:
    # Imported lazily: validate imports discovery, so discovery must not
    # import validate at module scope.
    from emet_sdk.validate import MissingPluginError, ValidationReport

    report = ValidationReport()
    installed = ", ".join(available) or "(none)"
    report.error(
        "missing_plugin",
        f"no {what} provides {name!r}. Installed: {installed}. "
        f"The value is legal. Install the package that provides it, or "
        f"correct the name.",
    )
    return MissingPluginError(report)


def discover() -> PluginRegistry:
    """Shorthand for `PluginRegistry.discover()`."""
    return PluginRegistry.discover()
