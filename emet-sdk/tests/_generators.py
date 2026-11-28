"""Seeded generators of body manifests, chain sets and mutations of both.

The property tests draw their inputs from here. Standard library only, on
`random.Random(seed)`: Hypothesis waits for 0.6 (decision E7 (b),
2026-11-25), so these generators are Emet's own. Each property is a test of
its seed that builds its input in its own body. CI11's stage 2
(`research/sota-2026-11-25/ci-cd.md`, outside the repository) swaps in
Hypothesis strategies, and needs each property split first into a function
of its input. A seed names one case exactly, on every machine: a failure
that prints its seed is a failure anyone can rerun.

Two variables choose the seeds, and only the property tests read them:

    EMET_PROPERTY_SEED=1234    run that one seed, in or out of the default range
    EMET_PROPERTY_SEEDS=2000   run seeds 0 to 1999 in place of the default

The default range is 0 to 199 for every property. Each file has to finish
in under ten seconds: on the maintainer's laptop on 2026-11-26, six runs
each, the resolver file took 4.9 to 6.4 s (8.9 s once, with other test runs
sharing the machine) and the validator file 5.9 to 7.1 s, 1.8 s of it the
two interpreters of its hash-seed property; a warm `validate_manifest` took
about 1.6 ms a manifest.

"Valid by construction" means: every part's type is one the schema allows,
every field is one its block declares, every plugin name is one `registry()`
installs (except the provider of `models.micro`, which the schema reserves
and nothing checks), at most one drive, unique ids, every home angle inside
its range, and every chain ends in a voice rung whose action and parameters
the validator accepts. A mutation breaks exactly one rule and names the one
error code the validator must report for it.
"""

from __future__ import annotations

import copy
import datetime
import os
import random
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from typing import Any, Callable

from emet_sdk import intents
from emet_sdk.chains import EXPLAIN_TOPICS, TONE_PRESETS, VOICE_ACTIONS
from emet_sdk.discovery import (
    GROUP_ACTUATOR,
    GROUP_AUDIO,
    GROUP_AUDIO_OUT,
    GROUP_LLM,
    GROUP_LOCOMOTION,
    GROUP_SENSOR,
    GROUP_STT,
    GROUP_TTS,
    GROUP_WAKE,
    PluginRegistry,
)

DEFAULT_SEEDS = 200


def seeds(default: int = DEFAULT_SEEDS) -> list[int]:
    """The seeds a property runs over, one named seed or 0 to N - 1."""
    one = os.environ.get("EMET_PROPERTY_SEED")
    if one:
        return [int(one)]
    return list(range(int(os.environ.get("EMET_PROPERTY_SEEDS") or default)))


def seed_id(seed: int) -> str:
    """The test id of a seed. `-k "seed_17]"` picks seed 17 alone: the
    closing bracket of the id keeps out seed_170 to seed_179."""
    return f"seed_{seed}"


def rerun(seed: int, test_file: str, test: str) -> str:
    """The one line that reruns a failing seed, for the failure message. The
    command sits between backticks, so the detail a caller puts after it
    cannot join its `-k` argument."""
    return f"seed {seed}; rerun with `EMET_PROPERTY_SEED={seed} python -m pytest {test_file} -k {test}`"


def rng_for(seed: int, test: str) -> random.Random:
    """A generator of a test's own, so that no property's draws change the
    input another property sees for the same seed."""
    return random.Random(f"{seed}:{test}")


def seed_params(test: str, known: dict[str, dict[str, tuple[int, ...]]], reasons: dict[str, str],
                default: int = DEFAULT_SEEDS) -> list[Any]:
    """The seeds one test runs over. A seed in `known[finding][test]` is a
    case where the code does not do what the property's sentence says: it
    is marked to fail, strictly, with the finding as the reason, so the
    suite stays green while the finding is open and goes red the day it is
    fixed, which is when the entry is deleted."""
    import pytest

    marked = {seed: finding for finding, by_test in known.items() for seed in by_test.get(test, ())}
    out = []
    for seed in seeds(default):
        marks: tuple[Any, ...] = ()
        if seed in marked:
            marks = (pytest.mark.xfail(strict=True, raises=AssertionError, reason=reasons[marked[seed]]),)
        out.append(pytest.param(seed, id=seed_id(seed), marks=marks))
    return out


# ------------------------------------------------------------------ plugins

#: What the generated manifests may name. Fixed here, so that no property
#: depends on what happens to be installed on the machine running it.
INSTALLED: dict[str, tuple[str, ...]] = {
    GROUP_ACTUATOR: ("emet_hal.mock", "acme_servo", "acme_leds"),
    GROUP_SENSOR: ("emet_hal.mock_sensor", "acme_imu"),
    GROUP_LOCOMOTION: ("differential", "tracked"),
    GROUP_WAKE: ("mock", "pocketsphinx"),
    GROUP_AUDIO: ("microphone", "wav"),
    GROUP_AUDIO_OUT: ("speaker", "wav", "null"),
    GROUP_STT: ("mock", "deepgram"),
    GROUP_LLM: ("mock", "anthropic", "openai"),
    GROUP_TTS: ("mock", "piper", "deepgram"),
}

#: Names no group above installs, for the missing-plugin mutations.
ABSENT = ("legged", "porcupine", "whisper", "alsa_raw", "bluetooth", "ollama", "coqui", "acme_missing")


def registry(*, verify_drivers: bool = False) -> PluginRegistry:
    """A registry holding exactly `INSTALLED`, at the strictness asked."""

    def group(name: str) -> dict[str, EntryPoint]:
        return {n: EntryPoint(n, f"emet_generated:{n.replace('.', '_')}", name) for n in INSTALLED[name]}

    return PluginRegistry(
        actuators=group(GROUP_ACTUATOR),
        sensors=group(GROUP_SENSOR),
        locomotion=group(GROUP_LOCOMOTION),
        wake=group(GROUP_WAKE),
        audio=group(GROUP_AUDIO),
        audio_out=group(GROUP_AUDIO_OUT),
        stt=group(GROUP_STT),
        llm=group(GROUP_LLM),
        tts=group(GROUP_TTS),
        verify_drivers=verify_drivers,
    )


# ---------------------------------------------------------------- manifests

#: The part types the schema allows, P0 first; the last four are reserved
#: (V1): accepted by the validator and ignored by the P0 engine.
P0_TYPES = ("joint_group", "drive", "display", "light", "camera", "sensor")
V1_TYPES = ("manipulator", "audio_extra", "projector", "thermal")

_WORDS = ("head", "neck", "eyes", "face", "ring", "strip", "base", "arm", "cam", "imu", "tail", "lamp", "panel")


def _ident(rng: random.Random, taken: set[str]) -> str:
    while True:
        name = rng.choice(_WORDS) + ("" if rng.random() < 0.5 else f"_{rng.randint(0, 99)}")
        if name not in taken:
            taken.add(name)
            return name


def _driver(rng: random.Random, group: str = GROUP_ACTUATOR) -> dict[str, Any]:
    driver: dict[str, Any] = {"plugin": rng.choice(INSTALLED[group])}
    if rng.random() < 0.4:
        driver["params"] = {"address": rng.randint(0, 127), "bus": rng.choice(["i2c1", "spi0"])}
    return driver


def _maybe(rng: random.Random, block: dict[str, Any], key: str, make: Callable[[], Any], p: float = 0.5) -> None:
    if rng.random() < p:
        block[key] = make()


def _joint(rng: random.Random, taken: set[str]) -> dict[str, Any]:
    low = rng.randint(-180, 0)
    high = rng.randint(low + 1, 180)
    joint: dict[str, Any] = {
        "id": _ident(rng, taken),
        "axis": rng.choice(["yaw", "pitch", "roll", "linear"]),
        "range_deg": [low, high],
    }
    _maybe(rng, joint, "home_deg", lambda: rng.choice([rng.randint(low, high), rng.uniform(low, high)]))
    _maybe(rng, joint, "channel", lambda: rng.randint(0, 15), 0.3)
    _maybe(rng, joint, "max_speed_dps", lambda: rng.uniform(1, 400), 0.3)
    _maybe(rng, joint, "invert", lambda: rng.random() < 0.5, 0.3)
    _maybe(rng, joint, "trim_deg", lambda: rng.uniform(-5, 5), 0.3)
    return joint


def _part(rng: random.Random, kind: str, ids: set[str], joint_ids: set[str], mounts: list[str]) -> dict[str, Any]:
    part: dict[str, Any] = {"id": _ident(rng, ids), "type": kind}
    if kind == "joint_group":
        _maybe(rng, part, "role", lambda: rng.choice(["head", "head", "arm", "torso", "custom"]), 0.9)
        part["driver"] = _driver(rng)
        part["joints"] = [_joint(rng, joint_ids) for _ in range(rng.randint(1, 3))]
        _maybe(rng, part, "parent", lambda: rng.choice([None, "base"]), 0.2)
    elif kind == "drive":
        part["kinematics"] = rng.choice(INSTALLED[GROUP_LOCOMOTION])
        part["driver"] = _driver(rng)
        _maybe(rng, part, "geometry", lambda: {"wheel_radius_m": rng.uniform(0.01, 0.1), "track_width_m": rng.uniform(0.05, 0.4)})
        _maybe(rng, part, "limits", lambda: {"max_linear_mps": rng.uniform(0.05, 1), "max_angular_rps": rng.uniform(0.1, 3)})
        _maybe(rng, part, "odometry", lambda: rng.choice(["none", "encoders", "imu_fused", "slam"]))
        _maybe(rng, part, "legs", lambda: {"count": rng.randint(1, 6), "gait": rng.choice(["static", "dynamic"])}, 0.2)
    elif kind == "display":
        _maybe(rng, part, "role", lambda: rng.choice(["eyes", "face", "status", "screen"]), 0.9)
        _maybe(rng, part, "form", lambda: rng.choice(["dual_round", "single_round", "rect", "matrix"]))
        part["driver"] = _driver(rng)
        _maybe(rng, part, "resolution", lambda: [rng.randint(8, 480), rng.randint(8, 480)])
        _maybe(rng, part, "refresh_hz", lambda: rng.uniform(1, 120), 0.3)
    elif kind == "light":
        _maybe(rng, part, "role", lambda: rng.choice(["ambient", "status", "eyes"]), 0.9)
        _maybe(rng, part, "form", lambda: rng.choice(["ring", "strip", "single"]))
        _maybe(rng, part, "count", lambda: rng.randint(1, 64))
        part["driver"] = _driver(rng)
    elif kind == "camera":
        # Free text in the schema, so a builder may well call a camera in
        # the head its eyes.
        _maybe(rng, part, "role", lambda: rng.choice(["gaze", "scene", "eyes", "head"]), 0.3)
        part["driver"] = _driver(rng, GROUP_SENSOR)
        _maybe(rng, part, "resolution", lambda: [640, 480])
        _maybe(rng, part, "fov_deg", lambda: rng.uniform(30, 360), 0.3)
        _maybe(rng, part, "mounted_on", lambda: rng.choice(["body", *mounts]))
        _maybe(rng, part, "local_pipeline", lambda: rng.sample(["face_detect", "motion", "pose"], rng.randint(0, 2)), 0.3)
    elif kind == "sensor":
        part["sensor_kind"] = rng.choice(["imu", "range", "touch", "light", "temp"])
        part["driver"] = _driver(rng, GROUP_SENSOR)
        _maybe(rng, part, "role", lambda: rng.choice(["balance", "bumper", "head", "status"]), 0.3)
    else:
        # Reserved: the schema says nothing about its fields, so these are
        # the plausible ones a builder writing ahead would give it.
        part["driver"] = _driver(rng)
        _maybe(rng, part, "role", lambda: rng.choice(["gripper", "speaker", "wall"]))
        _maybe(rng, part, "mounted_on", lambda: rng.choice(["body", *mounts]), 0.3)
    mounts.append(part["id"])
    return part


def _parts(rng: random.Random) -> list[dict[str, Any]]:
    ids: set[str] = set()
    joint_ids: set[str] = set()
    mounts: list[str] = []
    kinds = [rng.choice(P0_TYPES + V1_TYPES) for _ in range(rng.choice([0, 1, 2, 3, 4, 5, 7]))]
    if kinds.count("drive") > 1:
        first = kinds.index("drive")
        kinds = [k for i, k in enumerate(kinds) if k != "drive" or i == first]
    return [_part(rng, kind, ids, joint_ids, mounts) for kind in kinds]


def _models(rng: random.Random) -> dict[str, Any]:
    groups = {"stt": GROUP_STT, "chat": GROUP_LLM, "tts": GROUP_TTS}
    models: dict[str, Any] = {}
    for stage in rng.sample(["stt", "chat", "tts", "micro"], rng.randint(1, 4)):
        block: dict[str, Any] = {}
        if rng.random() < 0.6:
            block["provider"] = rng.choice(INSTALLED[groups[stage]]) if stage in groups else "tiny_model"
            _maybe(rng, block, "model", lambda: rng.choice(["nova-3", "small", "en_US-ljspeech-medium"]), 0.4)
            _maybe(rng, block, "key_env", lambda: "EMET_GENERATED_KEY", 0.4)
        if not block or rng.random() < 0.4:
            block["params"] = {"timeout_s": rng.randint(1, 60)}
        models[stage] = block
    return models


def manifest(rng: random.Random) -> dict[str, Any]:
    """A valid body manifest, by construction."""
    body: dict[str, Any] = {
        "id": _ident(rng, set()),
        "scale": rng.choice(["desk", "floor", "large"]),
        "power": rng.choice(["plugged_in", "battery"]),
    }
    _maybe(rng, body, "name", lambda: rng.choice(["a speakerphone", "scout-01, simulated", "Robo"]))
    _maybe(rng, body, "description", lambda: "A small robot on a desk.", 0.3)
    _maybe(rng, body, "battery_capacity_wh", lambda: rng.choice([None, rng.uniform(1, 100)]), 0.2)

    audio_in: dict[str, Any] = {"device": rng.choice(["plughw:1,0", "default", "file"])}
    if rng.random() < 0.4:
        audio_in["source"] = rng.choice(INSTALLED[GROUP_AUDIO])
        audio_in["params"] = {"path": "take.wav"}
    _maybe(rng, audio_in, "sample_rate", lambda: rng.choice([16000, 48000]))
    _maybe(rng, audio_in, "channels", lambda: rng.randint(1, 4))
    _maybe(rng, audio_in, "aec", lambda: rng.choice(["hardware", "software", "none"]))
    _maybe(rng, audio_in, "doa", lambda: rng.random() < 0.5)
    audio_out: dict[str, Any] = {"device": rng.choice(["plughw:1,0", "default", "none"])}
    _maybe(rng, audio_out, "sink", lambda: rng.choice(INSTALLED[GROUP_AUDIO_OUT]), 0.4)
    _maybe(rng, audio_out, "sample_rate", lambda: rng.choice([22050, 24000]), 0.3)
    _maybe(rng, audio_out, "gain_db", lambda: rng.uniform(-12, 0))
    audio: dict[str, Any] = {"input": audio_in, "output": audio_out}
    if rng.random() < 0.5:
        audio["wake"] = {"engine": rng.choice(INSTALLED[GROUP_WAKE])}
        _maybe(rng, audio["wake"], "params", lambda: {"threshold": 1e-15})

    doc: dict[str, Any] = {"manifest_version": f"{rng.randint(0, 2)}.{rng.randint(0, 9)}", "body": body, "audio": audio}
    _maybe(rng, doc, "compute", lambda: {"platform": "raspberrypi5", "ram_gb": rng.choice([2, 4, 8]), "accelerator": "none"}, 0.4)
    _maybe(rng, doc, "models", lambda: _models(rng), 0.4)
    parts = _parts(rng)
    if parts or rng.random() < 0.5:
        doc["capabilities"] = parts
    _maybe(rng, doc, "safety", lambda: {"estop_gpio": rng.choice([None, 17]), "max_continuous_motion_s": 30}, 0.3)
    return doc


# --------------------------------------------------------------- mutations


@dataclass(frozen=True)
class Mutation:
    """One broken rule: what was done, the code it must be reported as, and
    the document after it."""

    name: str
    code: str
    doc: Any


#: Where a value sits in a document: the keys and list indexes that lead to it.
KeyPath = tuple[Any, ...]

#: The type each typed field of a closed block takes, in JSON Schema's words.
_FIELDS: dict[str, dict[str, set[str]]] = {
    "top": {"manifest_version": {"string"}, "capabilities": {"array"}},
    "body": {"id": {"string"}, "name": {"string"}, "scale": {"string"}, "power": {"string"},
             "description": {"string"}, "battery_capacity_wh": {"number", "null"}},
    "compute": {"platform": {"string"}, "ram_gb": {"number"}, "accelerator": {"string"}},
    "input": {"source": {"string"}, "params": {"object"}, "device": {"string"}, "sample_rate": {"integer"},
              "channels": {"integer"}, "aec": {"string"}, "doa": {"boolean"}},
    "output": {"sink": {"string"}, "params": {"object"}, "device": {"string"}, "sample_rate": {"integer"},
               "gain_db": {"number"}},
    "wake": {"engine": {"string"}, "params": {"object"}},
    "model": {"provider": {"string"}, "model": {"string"}, "key_env": {"string"}, "params": {"object"}},
    "safety": {"estop_gpio": {"integer", "null"}, "max_continuous_motion_s": {"number"}, "thermal_throttle_c": {"number"}},
    "driver": {"plugin": {"string"}, "params": {"object"}},
    "joint": {"id": {"string"}, "axis": {"string"}, "channel": {"integer"}, "range_deg": {"array"},
              "home_deg": {"number"}, "max_speed_dps": {"number"}, "invert": {"boolean"}, "trim_deg": {"number"}},
    "joint_group": {"id": {"string"}, "type": {"string"}, "role": {"string"}, "driver": {"object"},
                    "joints": {"array"}, "parent": {"string", "null"}},
    "drive": {"id": {"string"}, "type": {"string"}, "kinematics": {"string"}, "driver": {"object"},
              "geometry": {"object"}, "limits": {"object"}, "odometry": {"string"}, "legs": {"object"}},
    "display": {"id": {"string"}, "type": {"string"}, "role": {"string"}, "form": {"string"},
                "driver": {"object"}, "resolution": {"array"}, "refresh_hz": {"number"}},
    "light": {"id": {"string"}, "type": {"string"}, "role": {"string"}, "form": {"string"},
              "count": {"integer"}, "driver": {"object"}},
    "camera": {"id": {"string"}, "type": {"string"}, "role": {"string"}, "driver": {"object"},
               "resolution": {"array"}, "fov_deg": {"number"}, "mounted_on": {"string"}, "local_pipeline": {"array"}},
    "sensor": {"id": {"string"}, "type": {"string"}, "sensor_kind": {"string"}, "driver": {"object"}, "role": {"string"}},
}

#: Values of every JSON type, to put where another type belongs.
_WRONG = (7, 2.5, True, "seven", None, ["x"], {"x": 1})


def _json_types(value: Any) -> set[str]:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    return {"object"}


def _get(doc: Any, path: KeyPath) -> Any:
    for key in path:
        doc = doc[key]
    return doc


def _blocks(doc: dict[str, Any]) -> list[tuple[KeyPath, str]]:
    """Every closed block present in a valid manifest, with its kind."""
    out: list[tuple[KeyPath, str]] = [((), "top"), (("body",), "body"), (("audio", "input"), "input"),
                                   (("audio", "output"), "output")]
    if "wake" in doc["audio"]:
        out.append((("audio", "wake"), "wake"))
    if "compute" in doc:
        out.append((("compute",), "compute"))
    if "safety" in doc:
        out.append((("safety",), "safety"))
    for stage in doc.get("models", {}):
        out.append((("models", stage), "model"))
    for i, part in enumerate(doc.get("capabilities", [])):
        if part["type"] in P0_TYPES:
            out.append((("capabilities", i), part["type"]))
            out.append((("capabilities", i, "driver"), "driver"))
        for j, _ in enumerate(part.get("joints", []) if part["type"] == "joint_group" else []):
            out.append((("capabilities", i, "joints", j), "joint"))
    return out


def _required(doc: dict[str, Any]) -> list[KeyPath]:
    """Every field present that its block requires."""
    out: list[KeyPath] = [("manifest_version",), ("body",), ("audio",), ("body", "id"), ("body", "scale"),
                       ("body", "power"), ("audio", "input"), ("audio", "output"),
                       ("audio", "input", "device"), ("audio", "output", "device")]
    if "wake" in doc["audio"]:
        out.append(("audio", "wake", "engine"))
    needs = {"joint_group": ("driver", "joints"), "drive": ("kinematics", "driver"), "display": ("driver",),
             "light": ("driver",), "camera": ("driver",), "sensor": ("sensor_kind", "driver")}
    for i, part in enumerate(doc.get("capabilities", [])):
        out += [("capabilities", i, "id"), ("capabilities", i, "type")]
        out += [("capabilities", i, key) for key in needs.get(part["type"], ())]
        if part["type"] in needs:
            out.append(("capabilities", i, "driver", "plugin"))
        for j, _ in enumerate(part.get("joints", []) if part["type"] == "joint_group" else []):
            out += [("capabilities", i, "joints", j, key) for key in ("id", "axis", "range_deg")]
    return out


def _parts_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return doc.setdefault("capabilities", [])


def _fresh_id(doc: dict[str, Any], rng: random.Random) -> str:
    return _ident(rng, {p["id"] for p in _parts_of(doc)})


def _a_joint(doc: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """A joint of the manifest, after adding a head when it has none."""
    groups = [p for p in _parts_of(doc) if p["type"] == "joint_group"]
    if not groups:
        joint_ids = {j["id"] for p in _parts_of(doc) for j in p.get("joints", []) if isinstance(j, dict)}
        group = _part(rng, "joint_group", {p["id"] for p in _parts_of(doc)}, joint_ids, [])
        _parts_of(doc).append(group)
        groups = [group]
    return rng.choice(rng.choice(groups)["joints"])


def _remove_required(doc: dict[str, Any], rng: random.Random) -> str:
    path = rng.choice(_required(doc))
    del _get(doc, path[:-1])[path[-1]]
    return "a required field removed: /" + "/".join(map(str, path))


def _wrong_type(doc: dict[str, Any], rng: random.Random) -> str:
    choices = [(path + (key,), allowed) for path, kind in _blocks(doc)
               for key, allowed in _FIELDS[kind].items() if key in _get(doc, path)]
    path, allowed = rng.choice(choices)
    value = rng.choice([v for v in _WRONG if not (_json_types(v) & allowed)])
    _get(doc, path[:-1])[path[-1]] = copy.deepcopy(value)
    return f"a field of the wrong type: /{'/'.join(map(str, path))} = {value!r}"


def _unknown_key(doc: dict[str, Any], rng: random.Random) -> str:
    path, _ = rng.choice(_blocks(doc))
    _get(doc, path)["colour"] = "red"
    return f"an unknown key in a closed block: /{'/'.join(map(str, path))}/colour"


def _duplicate_id(doc: dict[str, Any], rng: random.Random) -> str:
    parts = _parts_of(doc)
    while len(parts) < 2:
        parts.append({"id": _fresh_id(doc, rng), "type": "light", "role": "ambient", "driver": _driver(rng)})
    mounted = {p.get("mounted_on") for p in parts}
    i = rng.randrange(len(parts))
    others = [j for j in range(len(parts)) if j != i and parts[j]["id"] not in mounted]
    if not others:
        parts.append({"id": parts[i]["id"], "type": "light", "role": "status", "driver": _driver(rng)})
    else:
        parts[rng.choice(others)]["id"] = parts[i]["id"]
    return f"a duplicate id: {parts[i]['id']!r}"


def _unknown_mount(doc: dict[str, Any], rng: random.Random) -> str:
    parts = _parts_of(doc)
    held = [p for p in parts if p.get("mounted_on") not in (None, "body")]
    if held and rng.random() < 0.5:
        holder = rng.choice(held)
        target = holder["mounted_on"]
        doc["capabilities"] = [p for p in parts if p["id"] != target]
        return f"{target!r} removed, which {holder['id']!r} is mounted on"
    parts.append({"id": _fresh_id(doc, rng), "type": "camera", "driver": _driver(rng, GROUP_SENSOR),
                  "mounted_on": "ghost_head"})
    return "a camera mounted on 'ghost_head', which is not there"


def _home_out_of_range(doc: dict[str, Any], rng: random.Random) -> str:
    joint = _a_joint(doc, rng)
    low, high = joint["range_deg"]
    joint["home_deg"] = rng.choice([high + rng.randint(1, 30), low - rng.randint(1, 30)])
    return f"joint {joint['id']!r} homed at {joint['home_deg']} outside {joint['range_deg']}"


def _range_reversed(doc: dict[str, Any], rng: random.Random) -> str:
    joint = _a_joint(doc, rng)
    low, high = joint["range_deg"]
    joint["range_deg"] = rng.choice([[high, low], [low, low]])
    return f"joint {joint['id']!r} given range {joint['range_deg']}"


def _two_drives(doc: dict[str, Any], rng: random.Random) -> str:
    parts = _parts_of(doc)
    while sum(p["type"] == "drive" for p in parts) < 2:
        parts.append({"id": _fresh_id(doc, rng), "type": "drive",
                      "kinematics": rng.choice(INSTALLED[GROUP_LOCOMOTION]), "driver": _driver(rng)})
    return "a second drive"


#: Every place a manifest names a plugin that has to be installed, in the
#: order `mutate(..., pick=)` takes them.
PLUGIN_SITES = ("wake", "source", "sink", "stt", "chat", "tts", "kinematics")


def _missing_plugin(doc: dict[str, Any], rng: random.Random, where: str | None = None) -> str:
    """A plugin nothing installs, named at `where`, or at a site drawn from
    those the manifest has. A drive is added for `kinematics` when there is
    none."""
    name = rng.choice(ABSENT)
    drives = [p for p in _parts_of(doc) if p["type"] == "drive"]
    if where is None:
        where = rng.choice(list(PLUGIN_SITES[:-1]) + (["kinematics"] if drives else []))
    elif where == "kinematics" and not drives:
        drives = [{"id": _fresh_id(doc, rng), "type": "drive", "kinematics": "differential", "driver": _driver(rng)}]
        _parts_of(doc).append(drives[0])
    audio = doc["audio"]
    if where == "wake":
        audio.setdefault("wake", {})["engine"] = name
    elif where == "source":
        audio["input"]["source"] = name
    elif where == "sink":
        audio["output"]["sink"] = name
    elif where == "kinematics":
        drives[0]["kinematics"] = name
    else:
        doc.setdefault("models", {}).setdefault(where, {})["provider"] = name
    return f"{where} names {name!r}, which nothing installs"


#: Each single mutation, and the one code the validator must report for it.
MUTATIONS: tuple[tuple[str, Callable[[dict[str, Any], random.Random], str]], ...] = (
    ("schema", _remove_required),
    ("schema", _wrong_type),
    ("schema", _unknown_key),
    ("duplicate_id", _duplicate_id),
    ("unknown_reference", _unknown_mount),
    ("home_out_of_range", _home_out_of_range),
    ("invalid_range", _range_reversed),
    ("multiple_drives", _two_drives),
    ("missing_plugin", _missing_plugin),
)


def mutate(rng: random.Random, doc: dict[str, Any], pick: int | None = None) -> Mutation:
    """One rule broken in a copy of a valid manifest. The mutation is drawn,
    or with `pick` taken in turn: `pick % 9` names the mutation, and for a
    missing plugin `pick // 9 % 7` names its site, so a run over 0 to N - 1
    reaches every mutation and every site once N is 63 or more."""
    doc = copy.deepcopy(doc)
    if pick is None:
        code, apply = rng.choice(MUTATIONS)
        return Mutation(apply(doc, rng), code, doc)
    code, apply = MUTATIONS[pick % len(MUTATIONS)]
    if apply is _missing_plugin:
        where = PLUGIN_SITES[pick // len(MUTATIONS) % len(PLUGIN_SITES)]
        return Mutation(_missing_plugin(doc, rng, where), code, doc)
    return Mutation(apply(doc, rng), code, doc)


def unknown_driver(rng: random.Random, doc: dict[str, Any], count: int = 1) -> dict[str, Any]:
    """A copy in which `count` parts with a driver name absent plugins, each
    a different one; parts are added when there are too few to change."""
    doc = copy.deepcopy(doc)
    parts = _parts_of(doc)
    with_driver = [p for p in parts if isinstance(p.get("driver"), dict)]
    while len(with_driver) < count:
        parts.append({"id": _fresh_id(doc, rng), "type": "light", "driver": _driver(rng)})
        with_driver.append(parts[-1])
    if count == 1:
        rng.choice(with_driver)["driver"]["plugin"] = rng.choice(ABSENT)
        return doc
    for part, name in zip(rng.sample(with_driver, count), rng.sample(ABSENT, count)):
        part["driver"]["plugin"] = name
    return doc


# ------------------------------------------------- inputs beyond the schema

#: Values YAML can produce: a date for `2026-11-26`, a float for `.nan` or
#: `.inf`, an integer key for `1:`, as well as every JSON type.
ODD = (None, 0, -1, 2.5, float("nan"), float("inf"), True, "", "x", [], [1, "a"], {}, {"k": [None]},
       datetime.date(2026, 11, 26), {1: "one"})


def _all_paths(node: Any, path: KeyPath = ()) -> list[KeyPath]:
    out = [path] if path else []
    if isinstance(node, dict):
        for key, value in node.items():
            out += _all_paths(value, path + (key,))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _all_paths(value, path + (i,))
    return out


def _anything_anywhere(doc: Any, rng: random.Random) -> None:
    paths = _all_paths(doc)
    if not paths:
        return
    path = rng.choice(paths)
    _get(doc, path[:-1])[path[-1]] = copy.deepcopy(rng.choice(ODD))


def _reserved_part_anything(doc: Any, rng: random.Random) -> None:
    """A reserved part, whose fields the schema leaves open, given any value
    in a field a P0 part of another type would hold."""
    parts = doc.get("capabilities") if isinstance(doc, dict) else None
    if not isinstance(parts, list):
        return
    reserved = [p for p in parts if isinstance(p, dict) and p.get("type") in V1_TYPES]
    if not reserved:
        parts.append({"id": f"spare_{len(parts)}", "type": rng.choice(V1_TYPES)})
        reserved = parts[-1:]
    key = rng.choice(["driver", "mounted_on", "role", "joints", "form", "kinematics"])
    rng.choice(reserved)[key] = copy.deepcopy(rng.choice(ODD))


def stacked(rng: random.Random, doc: dict[str, Any]) -> Any:
    """A copy with two to six mutations stacked, single ones and values no
    schema allows, applied until one of them no longer finds its way in."""
    doc = copy.deepcopy(doc)
    for _ in range(rng.randint(2, 6)):
        pick = rng.random()
        try:
            if pick < 0.4:
                rng.choice(MUTATIONS)[1](doc, rng)
            elif pick < 0.7:
                _anything_anywhere(doc, rng)
            else:
                _reserved_part_anything(doc, rng)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            # An earlier mutation took away what this one edits. The
            # document is still an input; stop stacking on it.
            break
    if rng.random() < 0.05:
        doc = rng.choice(ODD + ([doc],))
    return doc


def schema_valid_variant(rng: random.Random, doc: dict[str, Any]) -> dict[str, Any]:
    """A copy the schema still accepts, changed only where the schema leaves
    the shape open: the fields of reserved parts and the contents of
    `params` blocks."""
    doc = copy.deepcopy(doc)
    for _ in range(rng.randint(1, 4)):
        if rng.random() < 0.7:
            _reserved_part_anything(doc, rng)
        else:
            params = [p for p in _all_paths(doc) if p and p[-1] == "params" and isinstance(_get(doc, p), dict)]
            if params:
                _get(doc, rng.choice(params))[rng.choice(["k", 1, "path"])] = copy.deepcopy(rng.choice(ODD))
    return doc


# -------------------------------------------------------------- chain sets


def vocabulary() -> list[str]:
    """Every intent name the closed vocabulary accepts, one per argument;
    a free argument gets one example."""
    names: list[str] = []
    for kind, allowed in sorted(intents.ALL_INTENTS.items()):
        if allowed is intents.FREE_ARGUMENT:
            names.append(f"{kind}.example")
        elif not allowed:
            names.append(kind)
        else:
            names += [f"{kind}.{arg}" for arg in sorted(allowed)]
    return names


ROLES = ("head", "arm", "torso", "custom", "eyes", "face", "status", "screen", "ambient")
BINDABLE = ("joint_group", "drive", "display", "light")
AXES = ("yaw", "pitch", "roll", "linear")


def _selector(rng: random.Random) -> dict[str, Any]:
    selector: dict[str, Any] = {}
    while not selector:
        if rng.random() < 0.7:
            selector["role"] = rng.choice(ROLES)
        if rng.random() < 0.4:
            selector["type"] = rng.choice(BINDABLE + ("camera", "manipulator"))
        if rng.random() < 0.4:
            selector["axis"] = rng.choice(AXES)
    return selector


def _voice_rung(rng: random.Random) -> dict[str, Any]:
    action = rng.choice(sorted(VOICE_ACTIONS))
    params: dict[str, Any] = {}
    if action == "inflect":
        params = {"preset": "rising", "filler": rng.choice(["hm?", ["hm?", "hmm."], ["oh!"]])}
    elif action == "tone":
        params = {"preset": rng.choice(sorted(TONE_PRESETS))}
    elif action == "explain":
        params = {"topic": rng.choice(sorted(EXPLAIN_TOPICS))}
    return {"actuator": {"voice": True}, "action": action, "params": params}


def _chain(rng: random.Random) -> dict[str, Any]:
    rungs = [
        {"actuator": _selector(rng), "action": rng.choice(["tilt", "pulse", "expression", "drive_to", "nod"]),
         "params": {"hold_ms": rng.randint(100, 1500)}}
        for _ in range(rng.randint(0, 4))
    ]
    chain: dict[str, Any] = {"rungs": rungs + [_voice_rung(rng)]}
    if rng.random() < 0.5:
        chain["mode"] = "first"
    return chain


def chain_set(rng: random.Random) -> dict[str, Any]:
    """A valid chain document, as a chain file would load."""
    names = rng.sample(vocabulary(), rng.randint(1, 12))
    return {name: _chain(rng) for name in names}


def _a_chain(doc: dict[str, Any], rng: random.Random, *, hardware: bool = False) -> dict[str, Any]:
    chain = doc[rng.choice(sorted(doc))]
    if hardware and len(chain["rungs"]) < 2:
        chain["rungs"].insert(0, {"actuator": {"role": "head"}, "action": "tilt"})
    return chain


def _cut_last_rung(doc, rng):
    _a_chain(doc, rng, hardware=True)["rungs"].pop()
    return "a chain's last rung cut"


def _voice_not_last(doc, rng):
    chain = _a_chain(doc, rng, hardware=True)
    chain["rungs"].insert(0, _voice_rung(rng))
    return "a voice rung that is not last"


def _unknown_intent(doc, rng):
    # An unknown kind, an unknown argument, an argument to a kind that takes
    # none, and a kind that takes one given none.
    old = rng.choice(sorted(doc))
    new = rng.choice(["express.glee", "dance", "move.fly", "signal.panic", "acknowledge.loudly", "express", "move"])
    doc[new] = doc.pop(old)
    return f"an intent outside the vocabulary: {new!r}"


def _voice_action(doc, rng, action: str, params: dict[str, Any]) -> None:
    voice = _a_chain(doc, rng)["rungs"][-1]
    voice["action"] = action
    voice["params"] = params


def _unknown_voice_action(doc, rng):
    _voice_action(doc, rng, rng.choice(["sing", "shout", "whistle"]), {})
    return "a voice action no engine performs"


def _unknown_topic(doc, rng):
    _voice_action(doc, rng, "explain", {"topic": rng.choice(["weather", "cannot_fly", None, 3])})
    return "an explain topic with no words"


def _unknown_tone(doc, rng):
    _voice_action(doc, rng, "tone", {"preset": rng.choice(["chime", "", None])})
    return "a tone the engine cannot render"


def _empty_inflect(doc, rng):
    _voice_action(doc, rng, "inflect", {"filler": rng.choice([[], "", "  ", [3], ["hm?", None], None])})
    return "an inflect with nothing to say"


def _malformed(doc, rng):
    chain = _a_chain(doc, rng, hardware=True)
    how = rng.choice(["selector key", "empty selector", "voice with role", "mode", "rungs", "action"])
    if how == "selector key":
        chain["rungs"][0]["actuator"] = {"role": "head", "pin": 4}
    elif how == "empty selector":
        chain["rungs"][0]["actuator"] = {}
    elif how == "voice with role":
        chain["rungs"][-1]["actuator"] = {"voice": True, "role": "head"}
    elif how == "mode":
        chain["mode"] = "sometimes"
    elif how == "rungs":
        chain["rungs"] = "tilt, then hm"
    else:
        del chain["rungs"][0]["action"]
    return f"a malformed chain: {how}"


CHAIN_MUTATIONS: tuple[tuple[str, Callable[[dict[str, Any], random.Random], str]], ...] = (
    ("unterminated_chain", _cut_last_rung),
    ("malformed_chain", _voice_not_last),
    ("unknown_intent", _unknown_intent),
    ("unknown_voice_action", _unknown_voice_action),
    ("unknown_explain_topic", _unknown_topic),
    ("unknown_tone_preset", _unknown_tone),
    ("empty_inflect", _empty_inflect),
    ("malformed_chain", _malformed),
)


def mutate_chains(rng: random.Random, doc: dict[str, Any]) -> Mutation:
    """One rule broken in a copy of a valid chain document."""
    doc = copy.deepcopy(doc)
    code, apply = rng.choice(CHAIN_MUTATIONS)
    return Mutation(apply(doc, rng), code, doc)


def stacked_chains(rng: random.Random, doc: dict[str, Any]) -> Any:
    """A copy with two to six chain mutations and odd values stacked."""
    doc = copy.deepcopy(doc)
    for _ in range(rng.randint(2, 6)):
        try:
            if rng.random() < 0.5:
                rng.choice(CHAIN_MUTATIONS)[1](doc, rng)
            elif rng.random() < 0.8:
                _anything_anywhere(doc, rng)
            else:
                doc[rng.choice([1, None, True, "speak"])] = copy.deepcopy(rng.choice(ODD + (_chain(rng),)))
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            break
    return doc
