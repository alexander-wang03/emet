"""Manifest, bundle, pack, and chain validation.

Two layers, deliberately kept apart:

*Schema* validation answers "is this the right shape?" and is expressed in
JSON Schema. *Semantic* validation answers "does this mean anything?" and
lives here, because cross-field and cross-document rules — a home angle
inside its range, a capability id referenced by a camera mount, a chain that
terminates in voice — cannot be said in JSON Schema.

**The error taxonomy is the point of this module.** A typo in a plugin name
and a robot that genuinely lacks a servo are different failures, and
conflating them costs support hours. So a missing plugin is
never reported as a schema error, and an open enum like `drive.kinematics`
stays open precisely because an unrecognised value raises
`MissingPluginError` rather than failing schema validation.

Plugin discovery is real as of 0.2: the registry reads the entry points of
every installed distribution, so what counts as a known plugin is decided by
what is installed rather than by a list compiled into the engine.

**Validating and booting are deliberately not the same strictness.** Linting a
manifest for hardware you have not wired yet is a normal thing to do, so an
unrecognised driver name is a warning here by default. Booting an engine
against that manifest is not, so `verify_drivers=True` — what `emet validate
--verify-drivers` passes, and what the engine will use — makes it an error.
`kinematics` is always strict: a body that cannot move the way it claims is a
different class of problem from one missing an LED driver.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import json

from emet_sdk import chains as _chains
from emet_sdk import intents as _intents
from emet_sdk.discovery import PluginRegistry

__all__ = [
    "Severity",
    "Finding",
    "ValidationReport",
    "ValidationError",
    "MissingPluginError",
    "PluginRegistry",
    "BUILTIN_LOCOMOTION",
    "BUILTIN_WAKE",
    "BUILTIN_AUDIO",
    "DEFAULT_WAKE_ENGINE",
    "DEFAULT_AUDIO_SOURCE",
    "load_yaml",
    "validate_manifest",
    "validate_soul",
    "validate_motion_pack",
    "validate_chain_document",
    "validate_pairing",
    "validate_memory_db",
]


Severity = Literal["error", "warning"]


#: What `emet-hal` ships. Kept for documentation and for tests that must not
#: depend on what happens to be installed — it is NOT what the validator checks
#: against. `drive.kinematics` is an open enum resolved against real entry
#: points, so a third-party `legged` package is as valid as anything here.
BUILTIN_LOCOMOTION: frozenset[str] = frozenset({"differential", "tracked"})


#: What `emet-hal` ships today. Same status as `BUILTIN_LOCOMOTION`: not what
#: the validator checks against, because `audio.wake.engine` is an open enum
#: resolved against real entry points.
#:
#: `identity.wake_word` used to be checked against a fixed set of pretrained
#: names, on the assumption that a wake engine can only hear words somebody
#: trained a model for. That assumption belonged to one class of engine. A
#: phonetic keyword spotter takes any phrase and a pronunciation, so the
#: shipped default is `pocketsphinx` (0.3) and the field is free text: a soul
#: may answer to whatever it likes, and whether a given engine can actually
#: hear it is answered by `WakeDescriptor.can_detect` after `start()`, not by
#: a list in this file.
BUILTIN_WAKE: frozenset[str] = frozenset({"mock"})

#: What `emet-hal` ships as audio sources. Same status again: documentation,
#: not what the validator checks against.
#:
#: `microphone` is the default and needs no manifest entry. `wav` replays a
#: recording through the identical path, which is how a wake failure reported
#: by somebody else gets reproduced without their room.
BUILTIN_AUDIO: frozenset[str] = frozenset({"microphone", "wav"})

#: What an omitted field means. Most manifests will mention neither, so these
#: are the values the engine actually runs with most of the time.
#:
#: They live here rather than in the engine because "what an absent field
#: means" is part of the document's meaning, and a validator and an engine
#: quietly disagreeing about a default is the kind of bug that only ever
#: appears on somebody else's robot.
DEFAULT_WAKE_ENGINE = "pocketsphinx"
DEFAULT_AUDIO_SOURCE = "microphone"


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    code: str
    message: str
    path: str = ""

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        return f"[{self.severity}] {self.code}{where}: {self.message}"


@dataclass(slots=True)
class ValidationReport:
    """Every problem found, rather than only the first.

    A builder fixing a hand-written manifest should learn about all four
    mistakes in one run, not discover them one restart at a time.
    """

    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: Severity, code: str, message: str, path: str = "") -> None:
        self.findings.append(Finding(severity, code, message, path))

    def error(self, code: str, message: str, path: str = "") -> None:
        self.add("error", code, message, path)

    def warn(self, code: str, message: str, path: str = "") -> None:
        self.add("warning", code, message, path)

    def extend(self, other: ValidationReport) -> None:
        self.findings.extend(other.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_status(self) -> None:
        if self.ok:
            return
        if any(f.code == "missing_plugin" for f in self.errors):
            raise MissingPluginError(self)
        raise ValidationError(self)


class ValidationError(Exception):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__("\n".join(str(f) for f in report.errors))


class MissingPluginError(ValidationError):
    """A referenced plugin is not installed.

    Its own type because it is emphatically *not* a schema error: the document
    is well-formed and the value is legal, the software just is not present.
    Never silently degrade because of this — that is a different failure from
    missing hardware.
    """


# --------------------------------------------------------------------------
# Plugin registry
# --------------------------------------------------------------------------


# `PluginRegistry` lives in `discovery` now that it reads real entry points.
# Re-exported here because every caller already imports it from `validate`, and
# a rename would be churn for no gain.


def default_registry() -> PluginRegistry:
    """The plugins actually installed in this environment."""
    return PluginRegistry.discover()


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _schema_dir() -> Path:
    """Locate the schema directory, installed or in a source checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "schemas", here.parent / "schemas"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "cannot locate the emet-sdk schemas directory; expected it beside "
        f"{here} or at {here.parent / 'schemas'}"
    )


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = _schema_dir() / f"{name}.schema.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_yaml(path: str | Path) -> Any:
    """Load a YAML document. Separated so callers can report IO errors well."""
    import yaml

    with Path(path).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _check_schema(doc: Any, schema_name: str, report: ValidationReport) -> bool:
    """Run JSON Schema validation, recording every violation. Returns True if clean."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(load_schema(schema_name))
    clean = True
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        clean = False
        pointer = "/" + "/".join(str(p) for p in err.absolute_path)
        report.error("schema", err.message, pointer)
    return clean


# --------------------------------------------------------------------------
# Body manifest
# --------------------------------------------------------------------------


def validate_manifest(
    doc: Any,
    *,
    registry: PluginRegistry | None = None,
) -> ValidationReport:
    """Validate a body manifest, shape then meaning."""
    report = ValidationReport()
    registry = registry if registry is not None else default_registry()

    if not _check_schema(doc, "body-manifest", report):
        # Semantic rules assume a well-shaped document; running them on a
        # broken one produces noise that buries the real error.
        return report

    capabilities: Sequence[Mapping[str, Any]] = doc.get("capabilities") or []

    _check_unique_ids(capabilities, report)
    _check_joints(capabilities, report)
    _check_single_drive(capabilities, report)
    _check_mount_references(capabilities, doc, report)
    _check_plugins(capabilities, registry, report)
    _check_wake_engine(doc, registry, report)
    _check_audio_source(doc, registry, report)

    return report


def _check_wake_engine(
    doc: Mapping[str, Any],
    registry: PluginRegistry,
    report: ValidationReport,
) -> None:
    """Resolve `audio.wake.engine`, the way `drive.kinematics` resolves.

    Absent is fine — a manifest that says nothing about wake takes the default,
    and most will.

    Unconditional, unlike the `driver.plugin` check a few lines up, and the
    difference is worth stating because both are open enums resolved against
    entry points. `driver.plugin` names a driver for *a physical device*, and
    describing a body you have not finished wiring is an ordinary thing to do,
    so an absent one is a warning. `drive.kinematics` and `audio.wake.engine`
    name an *implementation that has to exist* for the document to mean
    anything: there is no half-built state in which a body moves by a
    kinematics nobody wrote, or wakes to a detector nobody installed.

    Wake has the stronger claim of the two. Every other missing piece degrades
    — a chain that cannot find a head falls through to a light ring. A robot
    that cannot hear its own name has no next rung, so this is the last place
    a soft warning would be a kindness. It is also not hypothetical: Picovoice
    disabled every free Porcupine access key on 30 June 2026, and a manifest
    naming one went from working to unbindable overnight.
    """
    engine = ((doc.get("audio") or {}).get("wake") or {}).get("engine")
    if not isinstance(engine, str) or registry.has_wake(engine):
        return
    installed = ", ".join(registry.wake_names) or "(none)"
    report.error(
        "missing_plugin",
        f"no wake word plugin provides {engine!r}. Installed: {installed}. "
        f"`audio.wake.engine` is an open enum — this value is legal, the "
        f"plugin simply is not installed. Unlike a missing driver this is an "
        f"error rather than a warning, because a robot that cannot hear its "
        f"own name has nothing to fall back to.",
        "/audio/wake/engine",
    )


def _check_audio_source(
    doc: Mapping[str, Any],
    registry: PluginRegistry,
    report: ValidationReport,
) -> None:
    """Resolve `audio.input.source`, on the same terms as the wake engine.

    Absent is the common case and means a live microphone.

    Unconditional, like `kinematics` and `wake.engine` and unlike
    `driver.plugin`: this names an implementation that has to exist, not a
    device you might not have wired. A body whose audio source does not resolve
    produces no frames at all, which takes the wake engine and every voice rung
    with it.
    """
    source = ((doc.get("audio") or {}).get("input") or {}).get("source")
    if not isinstance(source, str) or registry.has_audio(source):
        return
    installed = ", ".join(registry.audio_names) or "(none)"
    report.error(
        "missing_plugin",
        f"no audio source provides {source!r}. Installed: {installed}. "
        f"`audio.input.source` is an open enum — this value is legal, the "
        f"plugin simply is not installed. Omit it entirely for a live "
        f"microphone, which is the default.",
        "/audio/input/source",
    )


def _check_unique_ids(caps: Sequence[Mapping[str, Any]], report: ValidationReport) -> None:
    seen: dict[str, int] = {}
    for i, cap in enumerate(caps):
        cid = cap.get("id")
        if not isinstance(cid, str):
            continue
        if cid in seen:
            report.error(
                "duplicate_id",
                f"capability id {cid!r} is used by both capability {seen[cid]} and {i}; "
                f"ids must be unique across the manifest",
                f"/capabilities/{i}/id",
            )
        else:
            seen[cid] = i


def _check_joints(caps: Sequence[Mapping[str, Any]], report: ValidationReport) -> None:
    for i, cap in enumerate(caps):
        if cap.get("type") != "joint_group":
            continue
        for j, joint in enumerate(cap.get("joints") or []):
            path = f"/capabilities/{i}/joints/{j}"
            rng = joint.get("range_deg")
            if not (isinstance(rng, Sequence) and len(rng) == 2):
                continue
            low, high = rng
            if low >= high:
                report.error(
                    "invalid_range",
                    f"joint {joint.get('id')!r}: range_deg is [{low}, {high}]; "
                    f"the first value must be less than the second",
                    f"{path}/range_deg",
                )
                continue
            home = joint.get("home_deg")
            if home is not None and not (low <= home <= high):
                report.error(
                    "home_out_of_range",
                    f"joint {joint.get('id')!r}: home_deg {home} lies outside "
                    f"range_deg [{low}, {high}]. Homing would drive the joint "
                    f"into its own limit.",
                    f"{path}/home_deg",
                )


def _check_single_drive(caps: Sequence[Mapping[str, Any]], report: ValidationReport) -> None:
    drives = [i for i, cap in enumerate(caps) if cap.get("type") == "drive"]
    if len(drives) > 1:
        report.error(
            "multiple_drives",
            f"found {len(drives)} drive capabilities (at indices {drives}); "
            f"P0 permits at most one. Hybrid locomotion is V1.",
            f"/capabilities/{drives[1]}",
        )


def _check_mount_references(
    caps: Sequence[Mapping[str, Any]],
    doc: Mapping[str, Any],
    report: ValidationReport,
) -> None:
    ids = {cap.get("id") for cap in caps if isinstance(cap.get("id"), str)}
    for i, cap in enumerate(caps):
        mount = cap.get("mounted_on")
        if mount is None or mount == "body":
            continue
        if mount not in ids:
            report.error(
                "unknown_reference",
                f"{cap.get('id')!r} is mounted_on {mount!r}, which is not a "
                f"capability in this manifest. Known ids: {', '.join(sorted(ids)) or '(none)'}",
                f"/capabilities/{i}/mounted_on",
            )


def _check_plugins(
    caps: Sequence[Mapping[str, Any]],
    registry: PluginRegistry,
    report: ValidationReport,
) -> None:
    unverified: list[str] = []

    for i, cap in enumerate(caps):
        driver = cap.get("driver") or {}
        plugin = driver.get("plugin")
        if isinstance(plugin, str):
            if registry.verify_drivers:
                if not registry.has_driver(plugin):
                    report.error(
                        "missing_plugin",
                        f"driver plugin {plugin!r} is not installed. Install the "
                        f"package that provides it, or correct the name — a typo "
                        f"here is a different failure from missing hardware.",
                        f"/capabilities/{i}/driver/plugin",
                    )
            elif not registry.has_driver(plugin) and plugin not in unverified:
                unverified.append(plugin)

        if cap.get("type") == "drive":
            kinematics = cap.get("kinematics")
            if isinstance(kinematics, str) and not registry.has_locomotion(kinematics):
                report.error(
                    "missing_plugin",
                    f"no locomotion plugin provides {kinematics!r}. "
                    f"Installed: {', '.join(registry.locomotion_names) or '(none)'}. "
                    f"`kinematics` is an open enum — this value is legal, the "
                    f"plugin simply is not installed.",
                    f"/capabilities/{i}/kinematics",
                )

    # One line, not one per capability. A manifest naming six drivers should
    # not bury its real errors under six identical notices.
    if unverified:
        report.warn(
            "driver_not_installed",
            f"{len(unverified)} driver plugin(s) named here are not installed "
            f"({', '.join(unverified)}). Legal in a manifest — describing hardware "
            f"you have not wired yet is normal — but the engine will refuse to "
            f"boot against it. Run with --verify-drivers to treat this as an error.",
            "/capabilities",
        )


# --------------------------------------------------------------------------
# Soul bundle
# --------------------------------------------------------------------------


def validate_soul(doc: Any) -> ValidationReport:
    """Validate a soul bundle.

    Note what is *not* checked here. `identity.wake_word` is free text, and
    whether it can actually be heard depends on which engine a given body
    installs — which this document does not know and must not care about.
    That check is a boot-time one against a live `WakeDescriptor`, and putting
    it here would have made a soul valid or invalid depending on the machine
    it was linted on.
    """
    report = ValidationReport()
    if not _check_schema(doc, "soul-bundle", report):
        return report

    weights = ((doc.get("idle") or {}).get("weights")) or {}
    if weights:
        total = sum(v for v in weights.values() if isinstance(v, (int, float)))
        if abs(total - 1.0) > 0.01:
            report.warn(
                "idle_weights_unnormalised",
                f"idle.weights sum to {total:.3f}; they are sampled as relative "
                f"weights, so this works, but 1.0 is easier to reason about",
                "/idle/weights",
            )

    return report


def validate_pairing(manifest: Mapping[str, Any], soul: Mapping[str, Any]) -> ValidationReport:
    """Cross-document rules that need a body and a soul together."""
    report = ValidationReport()

    interaction = soul.get("interaction") or {}
    aec = ((manifest.get("audio") or {}).get("input") or {}).get("aec")
    if interaction.get("barge_in") and aec == "none":
        report.error(
            "barge_in_without_aec",
            "interaction.barge_in is enabled but this body reports "
            "audio.input.aec: none. Without echo cancellation the robot hears "
            "its own speech and interrupts itself. Set barge_in: false, or "
            "declare the AEC this body actually has.",
            "/interaction/barge_in",
        )

    return report


# --------------------------------------------------------------------------
# Motion packs and chains
# --------------------------------------------------------------------------


def validate_motion_pack(doc: Any) -> ValidationReport:
    report = ValidationReport()
    if not _check_schema(doc, "motion-pack", report):
        return report

    for name, clip in (doc.get("clips") or {}).items():
        intent_name = clip.get("intent")
        if not isinstance(intent_name, str):
            continue
        kind, argument = _intents.parse(intent_name)
        try:
            _intents.validate_intent_name(kind, argument)
        except _intents.UnknownIntentError as exc:
            report.error("unknown_intent", str(exc), f"/clips/{name}/intent")
            continue
        if _intents.is_reserved(kind):
            report.warn(
                "reserved_intent",
                f"clip {name!r} targets {intent_name!r}, which is reserved: the "
                f"P0 engine logs and drops it. The clip is valid and will simply "
                f"not play yet.",
                f"/clips/{name}/intent",
            )

        times = [kf.get("t_ms") for kf in clip.get("keyframes") or []]
        if times != sorted(times):
            report.error(
                "keyframes_unordered",
                f"clip {name!r}: keyframe t_ms values are not monotonically "
                f"increasing ({times})",
                f"/clips/{name}/keyframes",
            )

    return report


def validate_chain_document(doc: Any) -> ValidationReport:
    """Validate a chain file, including the terminal-voice-rung rule.

    This is the mechanical enforcement of principle 2 — every intent is always
    satisfiable — and it is the reason the SDK rejects chains at all.
    """
    report = ValidationReport()

    if not isinstance(doc, Mapping):
        report.error("schema", "a chain file must be a mapping of intent name to chain")
        return report

    try:
        parsed = _chains.parse_chain_set(doc)
    except _chains.UnterminatedChainError as exc:
        report.error("unterminated_chain", str(exc))
        return report
    except _chains.ChainError as exc:
        report.error("malformed_chain", str(exc))
        return report

    for name in parsed:
        kind, argument = _intents.parse(name)
        try:
            _intents.validate_intent_name(kind, argument)
        except _intents.UnknownIntentError as exc:
            report.error("unknown_intent", str(exc), f"/{name}")

    for name, chain in parsed.items():
        if chain.mode == _chains.ChainMode.ALL:
            report.warn(
                "reserved_mode",
                f"chain {name!r} uses mode: all, which is RSV. The P0 engine "
                f"binds the first matching rung only.",
                f"/{name}/mode",
            )

    return report


# --------------------------------------------------------------------------
# Memory database
# --------------------------------------------------------------------------

#: Column names that would make a memory body-specific. Memories belong to the
#: soul and travel with the bundle; if retrieval were scoped by body, moving a
#: bundle to a new chassis would silently return zero rows and the robot would
#: greet its owner of six months as a stranger. Experiences travel; conditions
#: do not.
_BODY_COLUMN_HINTS = ("body_id", "body", "chassis", "manifest_id")

_SOUL_TABLES = ("people", "memories", "episodes")


def validate_memory_db(path: str | Path) -> ValidationReport:
    """Assert that a bundle's memory database is not namespaced by body."""
    report = ValidationReport()
    db = Path(path)
    if not db.exists():
        report.error("missing_file", f"no memory database at {db}")
        return report

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        for table in tables:
            if table not in _SOUL_TABLES:
                continue
            for row in conn.execute(f"PRAGMA table_info({table})"):
                column = row[1].lower()
                if any(hint in column for hint in _BODY_COLUMN_HINTS):
                    report.error(
                        "memory_body_column",
                        f"table {table!r} has column {row[1]!r}, which appears to "
                        f"scope memory to a body. Memories belong to the soul and "
                        f"must travel with the bundle; body-local state lives in "
                        f"/etc/emet/state/<body.id>.json.",
                        f"{table}.{row[1]}",
                    )

    return report
