"""Body-local state: what this body has learned about itself, kept beside it.

A soul moves between bodies and a calibration must stay behind. `DESIGN.md`
section 4.1 draws the line: experiences travel, conditions do not. A servo
trim fits one horn spline, a USB microphone enumerates differently on the
next Pi, and an adapted cepstral mean describes one room heard through one
microphone. Each is true of this hardware and wrong on any other, so none of
it goes in the soul bundle and all of it goes in one file keyed by
`body.id`. Keying this file is the one job `body.id` has.

**Where it lives.** `/etc/emet/state/<body.id>.json`, beside `body.yaml`, on
a system install. The reference Pi runs as an ordinary user and has no such
directory, so the person's place is the fallback: `$XDG_STATE_HOME/emet/`,
or `~/.local/state/emet/` when that is unset. This mirrors `keys.py`, with
one difference: keys are merged from both places, and state is one file. The
first place that already holds this body's file is the file. With no file
yet, the first place that can take a write gets it. A place this user
cannot look into counts as empty, so a machine directory kept private to a
service's own user sends a person running by hand to their own place. The
engine never creates the machine directory: one made by whichever user ran
first would belong to that user, so a system install makes it with the
right owner. The person's directory is created on the first save. A path
the caller names wins over both.

**What is in it.** Every save writes every key, so the format is stable from
its first version the way the manifest's reserved fields are:

    state_version, body_id, updated, engine   stamped on every save
    carry       what each plugin's `carry_over()` returned, by plugin
    trims       joint trims by capability and joint; these win over the
                manifest's `trim_deg`
    devices     the audio devices PortAudio named at the last boot
    health      each capability's health at the last boot
    bindings    the binding table the last boot resolved
    doa_reference_deg, camera_intrinsics, odometry
                reserved: present, empty, read by nothing yet

**A bad file is copied aside before anything replaces it.** Loading never
raises: a robot that cannot read its notes still runs. A missing file is an
empty state. A file that is not a JSON object, or holds a section of the
wrong shape, is read as empty where it is wrong, and the next save first
copies it to `<path>.bad`. NaN and Infinity count as bad: JSON has neither,
and a file holding one could never be saved again. A later bad file goes to
`<path>.bad.2`, then `.bad.3`, since an older copy may be the only record of
an older calibration. A file from a newer engine is read as empty and never
written: rewriting it would drop whatever the newer engine knows. A file
whose `body_id` names another body is treated the same way, since using it
would put one body's calibration on another. Keys this version does not
know are written back unchanged.

**Writes are atomic.** A save writes `<name>.tmp` beside the file, flushes it
to disk, and moves it over the old one with `os.replace`. A power cut during
a save leaves either the old file or the new one. A save that fails records
a warning and returns None: a robot that cannot write its notes still runs.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from emet_sdk.resolve import BindingTable
from emet_sdk.types import Health

__all__ = [
    "BodyState",
    "MACHINE_STATE_DIR",
    "STATE_VERSION",
    "default_dirs",
    "state_path",
    "user_state_dir",
]

#: The file's format. Compared as integers, part by part: a file with a
#: higher number was written by a newer engine and is left alone.
STATE_VERSION = "0.1"

#: A system install's place, beside `body.yaml` (`DESIGN.md` section 3 puts
#: the config root at `/etc/emet/`).
MACHINE_STATE_DIR = Path("/etc/emet/state")

#: The sections every save writes, in order, each with its empty value.
_SECTIONS: dict[str, Any] = {
    "carry": {},
    "trims": {},
    "devices": {"input": None, "output": None},
    "health": {},
    "bindings": {},
    "doa_reference_deg": None,
    "camera_intrinsics": {},
    "odometry": {},
}

#: A body id that is safe as a file name. The manifest schema is stricter
#: (`^[a-z][a-z0-9_]*$`); this only has to keep the file inside its directory.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*$")

#: How many set-aside copies may sit beside one file. Past that the engine
#: stops writing the file, so no earlier copy is ever written over.
_MAX_BAD = 20


def user_state_dir() -> Path:
    """A person's place: `$XDG_STATE_HOME/emet`, or `~/.local/state/emet`."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "emet"


def default_dirs() -> list[Path]:
    """Where a body's file is looked for when nothing names one. The machine
    first: on a system install that is where the file belongs. The last place
    is the person's, and it is the only one the engine creates."""
    return [MACHINE_STATE_DIR, user_state_dir()]


def state_path(body_id: str, explicit: str | Path | None = None) -> Path:
    """Where this body's state file is, or will be.

    `explicit` wins: a file, or a directory that exists, in which case the
    file inside it is `<body_id>.json`. Otherwise, over `default_dirs()` in
    order: the first place whose file already exists; else the first
    machine place that exists and can be written; else the person's place,
    which `BodyState.save` creates when it has to. A machine place is used
    only when it is already there. Nothing is created here.

    Raises `ValueError` for a body id that would not stay a plain file name.
    """
    if not isinstance(body_id, str) or not _SAFE_ID.match(body_id):
        raise ValueError(
            f"body.id {body_id!r} cannot name a state file. Use lowercase letters, "
            f"digits and underscores, as the manifest schema asks."
        )
    name = f"{body_id}.json"
    if explicit:
        named = Path(explicit).expanduser()
        return named / name if _is_dir(named) else named

    # Through the module, so the test guard in conftest.py takes effect.
    dirs = default_dirs()
    if not dirs:
        raise ValueError("there is no place to keep body-local state")
    for directory in dirs:
        if _is_there(directory / name):
            return directory / name
    *machine, person = dirs
    for directory in machine:
        if _is_dir(directory) and _writable(directory):
            return directory / name
    return person / name


def _is_there(path: Path) -> bool:
    """Whether anything is at `path`. A system install may make its place
    readable by its own user alone, and `Path.exists` raises for a path
    inside it. A place this user cannot look into holds nothing this user
    can use, so the answer is no and the search moves on to the next place."""
    try:
        os.stat(path)
    except (OSError, ValueError):
        return False
    return True


def _is_dir(path: Path) -> bool:
    """Whether `path` is a directory this user can see, on the same terms."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _writable(directory: Path) -> bool:
    """Whether a file can be made in this directory."""
    return os.access(directory, os.W_OK | os.X_OK)


def _engine_version() -> str:
    """The installed engine's version, read when a save needs it."""
    try:
        return metadata.version("emet-engine")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_version(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not _VERSION.match(value):
        return None
    return tuple(int(part) for part in value.split("."))


def _is_newer(theirs: tuple[int, ...], ours: tuple[int, ...]) -> bool:
    """Integer comparison, padded, so "0.10" is newer than "0.9" and "0.1.0"
    is the same as "0.1"."""
    width = max(len(theirs), len(ours))
    return theirs + (0,) * (width - len(theirs)) > ours + (0,) * (width - len(ours))


def _shape_ok(name: str, value: Any) -> bool:
    if name == "doa_reference_deg":
        return value is None or _is_number(value)
    return isinstance(value, dict)


def _is_number(value: Any) -> bool:
    """A finite int or float. A hand-edited file can hold anything, so an
    integer too large for a float is simply not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _thaw(value: Any) -> Any:
    """A deep copy with every mapping made a plain dict, so the copy can be
    written to whatever the manifest loader handed over."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_thaw(item) for item in value)
    return copy.deepcopy(value)


def _no_constant(name: str) -> Any:
    """Python's reader accepts NaN and Infinity, and JSON has neither. A file
    holding one was not written by this module, and every save of it would
    fail, so it is read as a bad file and set aside."""
    raise ValueError(f"{name} is not a JSON value")


def _set_aside_path(path: Path, raw: bytes) -> Path | None:
    """Where a bad file's bytes go: `<path>.bad`, then `<path>.bad.2` and on,
    the first name that is free or already holds these same bytes. An older
    copy may hold the only record of an older calibration, so none is ever
    written over. None when every name is taken."""
    for number in range(1, _MAX_BAD + 1):
        suffix = ".bad" if number == 1 else f".bad.{number}"
        candidate = path.with_name(path.name + suffix)
        try:
            if candidate.read_bytes() == raw:
                return candidate
        except FileNotFoundError:
            return candidate
        except OSError:
            continue
    return None


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write beside the target, flush to disk, then replace it in one step."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@dataclass(eq=False)
class BodyState:
    """One body's state file, read at boot and written back on the way down.

    Build one with `load`. Everything here works in memory whether or not
    the state is enabled, so the engine never has to ask; only `save` cares,
    and without a `body.id` it writes nothing.
    """

    body_id: str | None = None
    #: None when there is no `body.id`: nothing about this body is kept.
    path: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)
    #: Human-readable, one sentence or two each. The CLI prints them.
    warnings: list[str] = field(default_factory=list)
    last_saved: Path | None = None
    #: Set when the file on disk must never be written by this engine: it is
    #: newer, belongs to another body, could not be read, or is bad with no
    #: free name left to set it aside under.
    _refused: bool = field(default=False, init=False, repr=False)
    #: The bytes of a file that could not be used, and where they are copied
    #: before the next save replaces them.
    _bad: bytes | None = field(default=None, init=False, repr=False)
    _bad_path: Path | None = field(default=None, init=False, repr=False)

    @classmethod
    def load(cls, body_id: str | None, explicit: str | Path | None = None) -> "BodyState":
        """This body's state, from its file. Never raises."""
        if not body_id:
            state = cls()
            if explicit:
                state.warnings.append(
                    f"{explicit} was named for body-local state, but the manifest has "
                    f"no body.id, so nothing about this body is kept. Give it an id."
                )
            return state
        try:
            path = state_path(body_id, explicit)
        except (ValueError, OSError, RuntimeError) as exc:
            # RuntimeError: `Path.home()` on a machine whose user has no home.
            state = cls(body_id=body_id)
            state.warnings.append(f"body-local state is off: {exc}")
            return state
        state = cls(body_id=body_id, path=path)
        state._read()
        return state

    @property
    def enabled(self) -> bool:
        return bool(self.body_id) and self.path is not None

    # ---------------------------------------------------------- reading

    def _read(self) -> None:
        path = self.path
        assert path is not None
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._refuse(f"{path} could not be read ({exc.strerror or exc}).")
            return
        try:
            doc = json.loads(raw, parse_constant=_no_constant)
        except (ValueError, RecursionError) as exc:
            self._set_aside(raw, f"is not a JSON object ({exc})")
            return
        if not isinstance(doc, dict):
            self._set_aside(raw, "is not a JSON object")
            return

        version = doc.get("state_version", STATE_VERSION)
        parsed = _parse_version(version)
        if parsed is None:
            self._refuse(f"{path} has state_version {version!r}, which this engine cannot read.")
            return
        if _is_newer(parsed, _parse_version(STATE_VERSION) or ()):
            self._refuse(
                f"{path} was written by a newer engine (state_version {version}, "
                f"this engine writes {STATE_VERSION})."
            )
            return
        owner = doc.get("body_id")
        if owner is not None and owner != self.body_id:
            self._refuse(
                f"{path} belongs to body {owner!r}, and this body is {self.body_id!r}. "
                f"If they are the same body, change body_id in the file."
            )
            return

        wrong = [name for name in _SECTIONS if name in doc and not _shape_ok(name, doc[name])]
        for name in wrong:
            del doc[name]
        self.data = doc
        if wrong:
            self._set_aside(raw, f"has {', '.join(wrong)} in a shape this engine cannot use", keep=True)

    def _refuse(self, why: str) -> None:
        self._refused = True
        self.data = {}
        self.warnings.append(f"{why} Running without it, and leaving the file as it is.")

    def _set_aside(self, raw: bytes, why: str, *, keep: bool = False) -> None:
        if not keep:
            self.data = {}
        assert self.path is not None
        what = "Those parts start empty" if keep else "Running without it"
        bad = _set_aside_path(self.path, raw)
        if bad is None:
            self._refused = True
            self.warnings.append(
                f"{self.path} {why}. {what}, and the file is left as it is: {_MAX_BAD} earlier "
                f"bad copies already sit beside it. Move them somewhere else."
            )
            return
        self._bad, self._bad_path = raw, bad
        self.warnings.append(f"{self.path} {why}. {what}; the file is copied to {bad} before the next save.")

    def _section(self, name: str) -> dict[str, Any]:
        """A section to read. Empty when missing or the wrong shape."""
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def _writable_section(self, name: str) -> dict[str, Any]:
        """A section to write into, made when missing."""
        value = self.data.get(name)
        if not isinstance(value, dict):
            value = self.data[name] = copy.deepcopy(_SECTIONS.get(name) or {})
        return value

    # ------------------------------------------------------- carry-over

    def carried(self, key: str) -> dict[str, Any]:
        """What the plugin under `key` asked to keep last time. A copy."""
        entry = self._section("carry").get(key)
        return copy.deepcopy(entry) if isinstance(entry, dict) else {}

    def carry(self, key: str, params: Mapping[str, Any]) -> bool:
        """Keep a plugin's `carry_over()` for the next boot, and say whether
        it was kept. An empty mapping forgets the key. A value that cannot be
        written as JSON records a warning and keeps what was there: one
        plugin's mistake must not cost every other plugin its notes. What is
        kept comes back as JSON gives it: string keys, lists for tuples."""
        if not isinstance(params, Mapping):
            self.warnings.append(
                f"{key} gave {type(params).__name__} to carry over; it should give "
                f"a mapping. Kept what was there."
            )
            return False
        if not params:
            section = self.data.get("carry")
            if isinstance(section, dict):
                section.pop(key, None)
            return False
        try:
            clean = json.loads(json.dumps(dict(params), allow_nan=False))
        except (TypeError, ValueError) as exc:
            self.warnings.append(f"{key} gave a value that cannot be written down ({exc}). Kept what was there.")
            return False
        self._writable_section("carry")[key] = clean
        return True

    # ------------------------------------------------------------ trims

    def trims(self, capability_id: str) -> dict[str, float]:
        """The trims this file holds for one joint group, by joint id."""
        joints = self._section("trims").get(capability_id)
        if not isinstance(joints, dict):
            return {}
        return {str(joint): float(value) for joint, value in joints.items() if _is_number(value)}

    def set_trim(self, capability_id: str, joint_id: str, degrees: float) -> None:
        """Record one joint's trim. Raises `ValueError` for a value that is not
        a finite number: a trim of NaN would send a servo anywhere."""
        value = float(degrees)
        if not math.isfinite(value):
            raise ValueError(f"a trim is a finite number of degrees, got {degrees!r}")
        section = self._writable_section("trims")
        joints = section.get(capability_id)
        if not isinstance(joints, dict):
            joints = section[capability_id] = {}
        joints[joint_id] = value

    def overlay_trims(self, capability: Mapping[str, Any]) -> dict[str, Any]:
        """A copy of one capability block with this file's trims applied.

        For a joint group, each joint this file has a trim for gets that trim
        as its `trim_deg`, replacing the manifest's. Every other block, and a
        joint group this file says nothing about, comes back as a plain copy.
        The argument is never changed.
        """
        block = _thaw(capability)
        capability_id = block.get("id")
        if block.get("type") != "joint_group" or not isinstance(capability_id, str):
            return block
        trims = self.trims(capability_id)
        joints = block.get("joints")
        if not trims or not isinstance(joints, (list, tuple)):
            return block
        for joint in joints:
            if isinstance(joint, dict) and joint.get("id") in trims:
                joint["trim_deg"] = trims[joint["id"]]
        return block

    # ------------------------------------------------ what a boot records

    def record_bindings(self, table: BindingTable) -> None:
        """The binding table this boot resolved, replacing the last one."""
        bindings: dict[str, Any] = {}
        for intent in table:
            binding = table[intent]
            bindings[intent] = {"target": binding.target, "action": binding.action, "rung": binding.rung_index}
        self.data["bindings"] = bindings

    def record_devices(self, input: str | None, output: str | None) -> None:
        """The audio devices PortAudio named at this boot. None is kept, as
        null: a device that could not be named is worth knowing about."""
        self._writable_section("devices").update(input=input, output=output)

    def record_health(self, health: Mapping[str, Health]) -> None:
        """Each capability's health at this boot, replacing the last record."""
        self.data["health"] = {
            capability_id: {
                "ok": bool(report.ok),
                "detail": None if report.detail is None else str(report.detail),
                "faults": [str(fault) for fault in report.faults],
            }
            for capability_id, report in sorted(health.items())
        }

    # ----------------------------------------------------------- saving

    def _document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "state_version": STATE_VERSION,
            "body_id": self.body_id,
            "updated": _utc_now(),
            "engine": _engine_version(),
        }
        for name, empty in _SECTIONS.items():
            value = self.data.get(name)
            doc[name] = value if name in self.data and _shape_ok(name, value) else copy.deepcopy(empty)
        for name, value in self.data.items():
            if name not in doc:
                doc[name] = value
        return doc

    def save(self) -> Path | None:
        """Write the file. Returns its path, or None when there is nothing to
        write to, the file must be left alone, or the write failed. Never
        raises: a robot that cannot write its notes still runs."""
        if not self.enabled or self._refused:
            return None
        path = self.path
        assert path is not None
        try:
            if not path.parent.is_dir():
                if path.parent in default_dirs()[:-1]:
                    raise FileNotFoundError(f"{path.parent} is gone, and the engine never creates it")
                path.parent.mkdir(parents=True, exist_ok=True)
            if self._bad is not None and self._bad_path is not None:
                _write_atomic(self._bad_path, self._bad)
                self._bad = self._bad_path = None
            doc = self._document()
            payload = json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            _write_atomic(path, payload.encode("utf-8"))
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            self.warnings.append(f"body-local state was not saved to {path}: {exc}")
            return None
        self.data = doc
        self.last_saved = path
        return path
