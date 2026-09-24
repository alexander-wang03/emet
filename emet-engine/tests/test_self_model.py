"""The self-model: what the robot is told about its body, and what it says.

The 0.5 row is met when, asked "can you come here?", the robot declines
because it has no wheels. These tests hold the compiler to that on every
example body, and to the rule `DESIGN.md` section 6.1 states: the prompt and
the `explain` rung must never disagree, so a body topic has a fact exactly
when it has an explanation, and the fact quotes it word for word.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from emet_sdk.chains import EXPLAIN_TOPICS
from emet_sdk.resolve import descriptors_from_manifest, load_chains, resolve
from emet_sdk.types import BodyFact, LocomotionDescriptor, SelfModel
from emet_sdk.validate import load_yaml

from emet_engine.prompting import DEFAULT_LINES, persona_lines
from emet_engine.self_model import BODY_TOPICS, compile_self_model

EXAMPLES = Path(__file__).resolve().parents[2] / "emet-sdk" / "examples"

#: What the tracked plugin reports for mock-scout: 2.0 rad/s in the manifest,
#: less the scrub.
TRACKED = LocomotionDescriptor(
    kinematics="tracked",
    max_linear_mps=0.35,
    max_angular_rps=1.7,
    can_turn_in_place=True,
    can_translate_and_rotate=True,
)


def example(name: str) -> dict[str, Any]:
    return load_yaml(EXAMPLES / name)


def manifests(folder: Path) -> list[Path]:
    """Every manifest in a folder; chain files and souls are skipped."""
    found = []
    for path in sorted(folder.glob("*.yaml")):
        doc = load_yaml(path)
        if isinstance(doc, dict) and "manifest_version" in doc:
            found.append(path)
    return found


def everything_said(model: SelfModel) -> str:
    return "\n".join([model.text, *(w for w in model.explanations.values() if w)])


def fact(model: SelfModel, key: str) -> BodyFact:
    (found,) = [f for f in model.facts if f.key == key]
    return found


def keys(model: SelfModel) -> list[str]:
    return [f.key for f in model.facts]


def assert_the_prompt_and_the_chain_agree(model: SelfModel) -> None:
    assert set(model.explanations) == set(EXPLAIN_TOPICS)
    assert len(keys(model)) == len(set(keys(model))), "one fact a key"
    for topic in BODY_TOPICS:
        words = model.explanations[topic]
        if words is None:
            assert topic not in keys(model), f"{topic}: a fact with no explanation behind it"
        else:
            said = fact(model, topic)
            assert words in said.text, f"{topic}: the prompt and the chain disagree"
            assert not said.present


# ------------------------------------------------------------ synthesised


MOCK = {"plugin": "emet_hal.mock", "params": {}}


def body(*blocks: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "manifest_version": "0.1",
        "body": {"id": "test_body", "scale": "desk", "power": "plugged_in", **fields},
        "audio": {"input": {"device": "x", "channels": 1}, "output": {"device": "x"}},
        "capabilities": list(blocks),
    }


def joints(*axes: str) -> list[dict[str, Any]]:
    return [{"id": f"j{i}", "axis": a, "range_deg": [-30, 30], "home_deg": 0} for i, a in enumerate(axes)]


def head(*axes: str, cap_id: str = "head") -> dict[str, Any]:
    return {"id": cap_id, "type": "joint_group", "role": "head", "driver": MOCK, "joints": joints(*axes)}


def arm(cap_id: str = "arm") -> dict[str, Any]:
    return {"id": cap_id, "type": "joint_group", "role": "arm", "driver": MOCK, "joints": joints("pitch")}


def drive(kinematics: str = "differential", cap_id: str = "base", **limits: float) -> dict[str, Any]:
    block: dict[str, Any] = {"id": cap_id, "type": "drive", "kinematics": kinematics, "driver": MOCK}
    if limits:
        block["limits"] = dict(limits)
    return block


def display(role: str, form: str | None = None, cap_id: str = "screen") -> dict[str, Any]:
    block = {"id": cap_id, "type": "display", "role": role, "driver": MOCK}
    return {**block, "form": form} if form else block


def light(role: str, form: str | None = None, cap_id: str = "lamp") -> dict[str, Any]:
    block = {"id": cap_id, "type": "light", "role": role, "driver": MOCK}
    return {**block, "form": form} if form else block


def camera(mounted_on: str | None = None, cap_id: str = "cam") -> dict[str, Any]:
    block = {"id": cap_id, "type": "camera", "role": "front", "driver": MOCK}
    return {**block, "mounted_on": mounted_on} if mounted_on else block


def gripper(cap_id: str = "grip") -> dict[str, Any]:
    return {"id": cap_id, "type": "manipulator"}


def reported(manifest: dict[str, Any], *, broken: tuple[str, ...] = ()) -> list:
    """What the plugins would report: every part healthy but the broken ones."""
    return [
        replace(d, healthy=d.capability_id not in broken) for d in descriptors_from_manifest(manifest)
    ]


def wheels(
    *,
    mps: float = 0.35,
    rps: float = 2.0,
    in_place: bool = True,
    holonomic: bool = False,
    kinematics: str = "differential",
) -> LocomotionDescriptor:
    return LocomotionDescriptor(
        kinematics=kinematics,
        max_linear_mps=mps,
        max_angular_rps=rps,
        can_turn_in_place=in_place,
        can_translate_and_rotate=True,
        holonomic=holonomic,
    )


SYNTHESISED: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "head only": (body(head("yaw", "pitch")), ()),
    "drive only": (body(drive()), ()),
    "head without yaw": (body(head("pitch", "roll")), ()),
    "unhealthy drive": (body(drive("tracked")), ("base",)),
    "unhealthy head": (body(head("yaw", "pitch")), ("head",)),
    "unhealthy drive and head": (body(drive(), head("yaw")), ("base", "head")),
    "unhealthy drive and a head that turns": (body(drive(), head("yaw")), ("base",)),
    "unhealthy drive, no head": (body(drive("legged")), ("base",)),
    "unhealthy drive and a head without yaw": (body(drive(), head("pitch")), ("base",)),
    "manipulator": (body(gripper(), arm()), ()),
    "everything broken": (body(drive(), head("yaw"), display("eyes"), light("ambient"), camera()),
                          ("base", "head", "screen", "lamp", "cam")),
    "two heads, the second turns": (body(head("pitch"), head("yaw", cap_id="neck")), ()),
    "two heads, the one that turns is broken": (body(head("pitch"), head("yaw", cap_id="neck")), ("neck",)),
}

#: The shipped chains whose voice rung explains a body topic, by topic.
CHAINS = load_chains()
EXPLAINING = {
    name: str(chain.voice_rung.params.get("topic"))
    for name, chain in CHAINS.items()
    if chain.voice_rung.action == "explain" and chain.voice_rung.params.get("topic") in BODY_TOPICS
}


def assert_the_chain_explains_exactly_what_the_prompt_denies(manifest, descriptors) -> None:
    """Resolve the shipped chains against the same reports: a chain falls
    through to `explain` exactly when the self-model has words for its topic."""
    model = compile_self_model(manifest, capabilities=descriptors)
    table = resolve(CHAINS, descriptors)
    for name, topic in EXPLAINING.items():
        binding = table[name]
        explains = binding.is_voice and binding.action == "explain"
        assert explains == (model.explanations[topic] is not None), (
            f"{name} binds {binding.target} {binding.action}, and {topic} is {model.explanations[topic]!r}"
        )


# ------------------------------------------------------------------ the gate


@pytest.mark.parametrize("name", ["bodiless.yaml", "pi-speakerphone.yaml"])
def test_a_body_with_no_wheels_is_told_it_cannot_come_to_you(name):
    model = compile_self_model(example(name))
    assert "cannot come to you" in model.text
    assert "I have no wheels" in model.text
    assert model.explanations["cannot_move"] == "I cannot come to you. I have no wheels."


def test_the_treaded_scout_is_told_it_can_come_and_how_its_treads_turn():
    model = compile_self_model(example("mock-scout.yaml"), locomotion=TRACKED)
    assert "cannot come to you" not in everything_said(model)
    assert model.explanations["cannot_move"] is None
    assert model.explanations["cannot_turn"] is None
    moving = fact(model, "locomotion")
    assert moving.present
    assert "treads" in moving.text and "scrub" in moving.text


# -------------------------------------------------------------- the invariant


@pytest.mark.parametrize("path", manifests(EXAMPLES), ids=lambda p: p.name)
def test_every_example_body_says_the_same_thing_in_the_prompt_and_the_chain(path):
    manifest = load_yaml(path)
    assert_the_prompt_and_the_chain_agree(compile_self_model(manifest))
    assert_the_prompt_and_the_chain_agree(compile_self_model(manifest, capabilities=reported(manifest)))
    everything_broken = reported(manifest, broken=tuple(str(b["id"]) for b in manifest["capabilities"]))
    assert_the_prompt_and_the_chain_agree(compile_self_model(manifest, capabilities=everything_broken))


def test_the_examples_include_bodies_with_and_without_wheels():
    """The invariant above is only worth something if both branches run."""
    said = {p.name: compile_self_model(load_yaml(p)).explanations["cannot_move"] for p in manifests(EXAMPLES)}
    assert any(w is None for w in said.values()) and any(w is not None for w in said.values())


@pytest.mark.parametrize("name", sorted(SYNTHESISED))
def test_every_synthesised_body_says_the_same_thing_in_the_prompt_and_the_chain(name):
    manifest, broken = SYNTHESISED[name]
    assert_the_prompt_and_the_chain_agree(compile_self_model(manifest, capabilities=reported(manifest, broken=broken)))
    assert_the_prompt_and_the_chain_agree(compile_self_model(manifest))


def test_the_shipped_chains_explain_the_move_and_turn_topics():
    """The check below is only worth something if it finds the chains."""
    assert set(EXPLAINING.values()) == set(BODY_TOPICS)
    assert {"move.approach", "move.turn_to"} <= set(EXPLAINING)


@pytest.mark.parametrize("path", manifests(EXAMPLES), ids=lambda p: p.name)
def test_an_example_body_explains_at_the_motor_level_exactly_what_its_prompt_denies(path):
    manifest = load_yaml(path)
    every_part = tuple(str(b["id"]) for b in manifest["capabilities"])
    assert_the_chain_explains_exactly_what_the_prompt_denies(manifest, reported(manifest))
    assert_the_chain_explains_exactly_what_the_prompt_denies(manifest, reported(manifest, broken=every_part))


@pytest.mark.parametrize("name", sorted(SYNTHESISED))
def test_a_synthesised_body_explains_at_the_motor_level_exactly_what_its_prompt_denies(name):
    manifest, broken = SYNTHESISED[name]
    assert_the_chain_explains_exactly_what_the_prompt_denies(manifest, reported(manifest, broken=broken))
    assert_the_chain_explains_exactly_what_the_prompt_denies(manifest, reported(manifest))


def test_a_second_head_that_turns_is_the_one_the_robot_is_told_about():
    manifest = body(head("pitch"), head("yaw", cap_id="neck"))
    model = compile_self_model(manifest)
    assert model.explanations["cannot_turn"] is None
    assert fact(model, "head").text == "You can turn your head left and right."
    broken = compile_self_model(manifest, capabilities=reported(manifest, broken=("neck",)))
    assert broken.explanations["cannot_turn"] == (
        "I cannot turn toward you. I have no wheels, and my head does not turn."
    )


# ------------------------------------------------------------------ absences


def test_the_bodiless_robot_is_told_everything_it_lacks():
    model = compile_self_model(example("bodiless.yaml"))
    absent = {f.key for f in model.absences}
    assert {"camera", "arms", "eyes", "locomotion", "head", "direction"} <= absent
    assert {"cannot_move", "cannot_turn"} <= absent
    assert fact(model, "camera").text == "You cannot see: you have no camera."
    assert fact(model, "voice").present and fact(model, "hearing").present


def test_the_facts_come_in_a_fixed_order():
    assert keys(compile_self_model(example("bodiless.yaml"))) == [
        "description", "scale", "power", "hearing", "direction", "voice",
        "locomotion", "cannot_move", "head", "cannot_turn", "arms", "eyes", "camera",
    ]
    assert keys(compile_self_model(example("scout-01.yaml"))) == [
        "description", "scale", "power", "hearing", "direction", "voice",
        "locomotion", "head", "arms", "eyes", "lights", "camera",
    ]


@pytest.mark.parametrize("path", manifests(EXAMPLES), ids=lambda p: p.name)
def test_facts_are_second_person_and_explanations_first(path):
    model = compile_self_model(load_yaml(path))
    for said in model.facts:
        assert said.text.startswith(("You ", "Your ", "If someone asks you")), said.text
    for topic in BODY_TOPICS:
        words = model.explanations[topic]
        assert words is None or words.startswith("I ")


# ------------------------------------------------------------ plain language


def renamed(manifest: dict[str, Any], prefix: str = "zq") -> dict[str, Any]:
    """The same body with every capability id changed to something no
    sentence could contain by accident."""
    doc = copy.deepcopy(manifest)
    for block in doc["capabilities"]:
        block["id"] = prefix + block["id"]
        if block.get("mounted_on") not in (None, "body"):
            block["mounted_on"] = prefix + block["mounted_on"]
    return doc


@pytest.mark.parametrize("name", ["mock-scout.yaml", "scout-01.yaml"])
def test_no_capability_id_or_plugin_name_ever_reaches_the_robot(name):
    manifest = example(name)
    doc = renamed(manifest)
    every_part = tuple(str(b["id"]) for b in doc["capabilities"])
    compiled = [
        compile_self_model(doc),
        compile_self_model(doc, locomotion=TRACKED),
        compile_self_model(doc, capabilities=reported(doc, broken=every_part)),
    ]
    plugins = {str(b["driver"]["plugin"]) for b in manifest["capabilities"] if "driver" in b}
    plugins |= {str(b["kinematics"]) for b in manifest["capabilities"] if "kinematics" in b}
    names = plugins | {p.rsplit(".", 1)[-1] for p in plugins}
    for model in compiled:
        said = everything_said(model).lower()
        assert "zq" not in said, said
        for plugin in names:
            assert plugin.lower() not in said, plugin

    # And with the builder's own ids: the ones that are not also the name of
    # a role, form or type (a part is spoken of by those) never appear.
    vocabulary = {str(b.get(k)) for b in manifest["capabilities"] for k in ("role", "form", "type")}
    said = everything_said(compile_self_model(manifest, locomotion=TRACKED)).lower()
    for cap_id in {str(b["id"]) for b in manifest["capabilities"]} - vocabulary:
        assert not re.search(rf"\b{re.escape(cap_id)}\b", said), cap_id


def test_the_compilation_is_a_template_and_gives_the_same_words_twice():
    for path in manifests(EXAMPLES):
        manifest = load_yaml(path)
        first = compile_self_model(manifest, locomotion=TRACKED, capabilities=reported(manifest))
        again = compile_self_model(copy.deepcopy(manifest), locomotion=TRACKED, capabilities=reported(manifest))
        assert first == again
        assert first.text == again.text


# ------------------------------------------------------------- bare bodies


def test_a_manifest_with_nothing_but_a_microphone_compiles():
    model = compile_self_model({"audio": {"input": {"device": "x"}}})
    assert fact(model, "hearing").text == "You hear through one microphone."
    assert not fact(model, "direction").present
    assert model.explanations["cannot_move"] == "I cannot come to you. I have no wheels."
    assert not {"description", "scale", "power", "lights"} & set(keys(model))


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"body": "a box", "audio": None, "capabilities": None},
        {"capabilities": [None, "head", 3, {"type": "drive"}]},
        {"capabilities": {"id": "head"}},
        {"audio": {"input": {"channels": True, "doa": "yes"}}},
        {"capabilities": [{"id": "head", "type": "joint_group", "role": "head", "joints": 3}]},
        {"capabilities": [{"id": "head", "type": "joint_group", "role": "head", "joints": True}]},
        {"capabilities": [{"id": "head", "type": "joint_group", "role": "head",
                           "joints": [{"id": "pan", "axis": ["yaw"]}, "tilt"]}]},
        {"capabilities": [{"id": "base", "type": "drive", "limits": {"max_linear_mps": float("inf")}}]},
    ],
)
def test_a_malformed_manifest_compiles_without_raising(manifest):
    assert_the_prompt_and_the_chain_agree(compile_self_model(manifest))


def test_a_head_whose_joints_are_malformed_is_a_head_that_cannot_turn():
    manifest = {"capabilities": [{"id": "head", "type": "joint_group", "role": "head", "joints": 3}]}
    model = compile_self_model(manifest)
    assert fact(model, "head").text == "You have a head, but you cannot turn it to look around."
    assert model.explanations["cannot_turn"] == "I cannot turn toward you. I have no wheels, and my head does not turn."


@pytest.mark.parametrize("path", manifests(EXAMPLES / "invalid"), ids=lambda p: p.name)
def test_a_manifest_the_validator_refuses_still_compiles(path):
    assert_the_prompt_and_the_chain_agree(compile_self_model(load_yaml(path)))


# -------------------------------------------------------------- the soul's lines


def test_the_default_lines_answer_offline_and_error():
    model = compile_self_model(example("bodiless.yaml"))
    assert model.explanations["offline"] == DEFAULT_LINES["failed"]
    assert model.explanations["error"] == DEFAULT_LINES["failed"]


def test_the_souls_lines_answer_offline_and_error_in_its_own_words():
    soul = example("emet-soul.yaml")
    model = compile_self_model(example("bodiless.yaml"), lines=persona_lines(soul))
    assert model.explanations["offline"] == "I lost the thread there. Say it again?"
    assert model.explanations["error"] == "I lost the thread there. Say it again?"


def test_a_soul_that_silences_failed_gets_silence():
    model = compile_self_model(example("bodiless.yaml"), lines={"failed": None})
    assert model.explanations["offline"] is None
    assert model.explanations["error"] is None
    assert compile_self_model(example("bodiless.yaml"), lines={}).explanations["offline"] is None


def test_a_broken_part_is_named_in_error_whatever_the_soul_says():
    manifest = body(head("yaw"))
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=("head",)), lines={"failed": None})
    assert model.explanations["error"] == "Part of me is not working: my head."
    assert model.explanations["offline"] is None


# ------------------------------------------------------------ what is broken


def test_an_unhealthy_head_is_not_working_in_the_prompt_and_in_error():
    manifest = body(head("yaw", "pitch"), display("eyes", "dual_round"))
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=("head", "screen")))
    assert fact(model, "head") == BodyFact("head", "Your head is not working right now, so you cannot move it.", False)
    assert fact(model, "eyes") == BodyFact(
        "eyes", "Your eyes are not working right now, so nobody can see your expression.", False
    )
    assert model.explanations["error"] == "Part of me is not working: my head and my eyes."
    assert model.explanations["cannot_turn"] == (
        "I cannot turn toward you. I have no wheels, and my head is not working."
    )


def test_an_unhealthy_drive_cannot_move_and_says_its_treads_are_not_working():
    manifest = body(drive("tracked"))
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=("base",)))
    assert fact(model, "locomotion") == BodyFact(
        "locomotion", "Your treads are not working right now, so you cannot move.", False
    )
    assert model.explanations["cannot_move"] == "I cannot come to you. My treads are not working."
    assert model.explanations["cannot_turn"] == (
        "I cannot turn toward you. My treads are not working, and I have no head."
    )
    assert model.explanations["error"] == "Part of me is not working: my treads."
    assert fact(model, "power").text == "You are plugged into the wall."


def test_a_broken_drive_with_a_head_that_turns_can_still_turn_toward_you():
    manifest = body(drive(), head("yaw"))
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=("base",)))
    assert model.explanations["cannot_move"] == "I cannot come to you. My wheels are not working."
    assert model.explanations["cannot_turn"] is None


def test_error_names_every_broken_part_once_in_manifest_order():
    manifest = body(
        drive(), head("yaw"), arm(), display("face", cap_id="face"), light("ambient"),
        light("status", cap_id="lamp2"), camera(),
        {"id": "imu", "type": "sensor", "sensor_kind": "imu", "driver": MOCK},
        {"id": "spine", "type": "joint_group", "role": "torso", "driver": MOCK, "joints": joints("yaw")},
        {"id": "tail", "type": "joint_group", "role": "custom", "driver": MOCK, "joints": joints("yaw")},
    )
    every_part = tuple(str(b["id"]) for b in manifest["capabilities"])
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=every_part))
    assert model.explanations["error"] == (
        "Part of me is not working: my wheels, my head, my arm, my face, my lights, my camera, "
        "one of my senses, my body and one of my joints."
    )


def test_a_part_the_manifest_declares_and_no_plugin_reported_is_not_working():
    manifest = example("mock-scout.yaml")
    model = compile_self_model(manifest, capabilities=[])
    assert model.explanations["cannot_move"] == "I cannot come to you. My treads are not working."
    assert model.explanations["error"] == (
        "Part of me is not working: my head, my treads, my eyes, my lights and one of my senses."
    )


def test_a_healthy_report_describes_the_body_as_the_manifest_does():
    manifest = example("scout-01.yaml")
    assert compile_self_model(manifest, capabilities=reported(manifest)) == compile_self_model(manifest)


# ------------------------------------------------------------------ the parts


@pytest.mark.parametrize(
    ("channels", "sentence"),
    [
        (None, "You hear through one microphone."),
        (1, "You hear through one microphone."),
        (2, "You hear through a two-microphone array."),
        (4, "You hear through a four-microphone array."),
        (8, "You hear through an eight-microphone array."),
        (10, "You hear through a ten-microphone array."),
        (11, "You hear through an 11-microphone array."),
        (16, "You hear through a 16-microphone array."),
        (18, "You hear through an 18-microphone array."),
    ],
)
def test_hearing_counts_the_microphones(channels, sentence):
    manifest = body()
    manifest["audio"]["input"]["channels"] = channels
    assert fact(compile_self_model(manifest), "hearing").text == sentence


def test_direction_finding_is_a_presence_only_when_the_manifest_says_so():
    manifest = body()
    manifest["audio"]["input"]["doa"] = True
    said = fact(compile_self_model(manifest), "direction")
    assert said == BodyFact("direction", "You can tell roughly which direction a voice comes from.")


@pytest.mark.parametrize(
    ("power", "has_drive", "sentence"),
    [
        ("plugged_in", True, "You are plugged into the wall, so you cannot go far: only as far as your cable reaches."),
        ("plugged_in", False, "You are plugged into the wall."),
        ("battery", True, "You run on a battery, so you can go wherever you can drive until it runs down."),
        ("battery", False, "You run on a battery."),
    ],
)
def test_power_says_how_far_the_body_can_go(power, has_drive, sentence):
    manifest = body(*([drive()] if has_drive else []), power=power)
    assert fact(compile_self_model(manifest), "power").text == sentence


@pytest.mark.parametrize(
    ("scale", "sentence"),
    [
        ("desk", "You are small: you sit on a desk or a table."),
        ("floor", "You live on the floor."),
        ("large", "You are large, about the size of a person."),
        ("enormous", None),
        (None, None),
    ],
)
def test_scale_is_said_plainly_and_an_unknown_one_is_left_out(scale, sentence):
    model = compile_self_model(body(scale=scale))
    if sentence is None:
        assert "scale" not in keys(model)
    else:
        assert fact(model, "scale").text == sentence


def test_the_description_is_quoted_with_its_whitespace_collapsed():
    model = compile_self_model(body(description="  A box\n  with   a speaker.\n"))
    assert fact(model, "description").text == 'Your builder describes you like this: "A box with a speaker."'
    assert "description" not in keys(compile_self_model(body(description="   ")))


def test_a_drive_with_no_report_claims_no_turning():
    model = compile_self_model(example("scout-01.yaml"))
    assert fact(model, "locomotion").text == (
        "You move on wheels. At most you go 0.35 metres a second, slower than a person walks."
    )
    assert fact(compile_self_model(body(drive())), "locomotion").text == "You move on wheels."


def test_the_turning_rate_is_the_plugins_never_the_manifests():
    """The manifest says 2.0 rad/s, a full turn in about three seconds. The
    tracked plugin reports 1.7 after scrub, and that is what the robot is told."""
    said = fact(compile_self_model(example("mock-scout.yaml"), locomotion=TRACKED), "locomotion").text
    assert round(2 * math.pi / 2.0) == 3 and round(2 * math.pi / 1.7) == 4
    assert "A full turn takes you about four seconds." in said
    assert "three" not in said


def test_the_tracked_scout_is_described_whole():
    said = fact(compile_self_model(example("mock-scout.yaml"), locomotion=TRACKED), "locomotion").text
    assert said == (
        "You move on treads. You can drive forward and back and turn on the spot. At most you go "
        "0.35 metres a second, slower than a person walks. A full turn takes you about four seconds. "
        "Your treads scrub when you turn, so your turns are slow and never quite exact."
    )


@pytest.mark.parametrize(
    ("mps", "said"),
    [
        (0.05, "0.05 metres a second, barely faster than a crawl."),
        (0.1, "0.1 metres a second, slower than a person walks."),
        (1.0, "1 metre a second, about as fast as a person walks."),
        (1.25, "1.25 metres a second, about as fast as a person walks."),
        (2.0, "2 metres a second, faster than a person walks."),
        (0.001, "0.001 metres a second, barely faster than a crawl."),
        (0.00001, "0.00001 metres a second, barely faster than a crawl."),
        (0.0049, "0.0049 metres a second, barely faster than a crawl."),
        (10.0, "10 metres a second, faster than a person walks."),
    ],
)
def test_a_top_speed_is_read_out_and_compared_to_walking(mps, said):
    text = fact(compile_self_model(body(drive()), locomotion=wheels(mps=mps)), "locomotion").text
    assert f"At most you go {said}" in text


def test_a_body_with_no_speed_gets_no_speed_sentence():
    text = fact(compile_self_model(body(drive()), locomotion=wheels(mps=0.0)), "locomotion").text
    assert "At most" not in text


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -0.35, None, True])
def test_a_speed_or_turning_rate_nobody_can_say_is_left_unsaid(value):
    report = replace(wheels(), max_linear_mps=value, max_angular_rps=value)
    text = fact(compile_self_model(body(drive()), locomotion=report), "locomotion").text
    assert text == "You move on wheels. You can drive forward and back and turn on the spot."
    unreported = fact(compile_self_model(body(drive(max_linear_mps=value))), "locomotion").text
    assert unreported == "You move on wheels."


@pytest.mark.parametrize(
    ("rps", "said"),
    [
        (10.0, "A full turn takes you less than a second."),
        (2 * math.pi, "A full turn takes you about one second."),
        (0.5, "A full turn takes you about 13 seconds."),
    ],
)
def test_a_full_turn_is_said_in_seconds(rps, said):
    assert said in fact(compile_self_model(body(drive()), locomotion=wheels(rps=rps)), "locomotion").text


def test_a_body_that_cannot_turn_on_the_spot_says_so_and_claims_no_turn_time():
    text = fact(compile_self_model(body(drive()), locomotion=wheels(in_place=False)), "locomotion").text
    assert "You cannot turn on the spot: to face another way you have to drive forward in an arc." in text
    assert "full turn" not in text


def test_a_holonomic_body_can_slide_sideways():
    text = fact(compile_self_model(body(drive()), locomotion=wheels(holonomic=True)), "locomotion").text
    assert "You can also slide sideways without turning." in text


def test_legs_walk():
    model = compile_self_model(body(drive("legged")), locomotion=wheels(kinematics="legged"))
    assert fact(model, "locomotion").text.startswith(
        "You move on legs. You can walk forward and back and turn on the spot."
    )


@pytest.mark.parametrize(
    ("kinematics", "noun"),
    [("differential", "wheels"), ("tracked", "treads"), ("legged", "legs"), ("omni", "wheels")],
)
def test_the_drive_is_named_for_what_it_moves_on(kinematics, noun):
    assert fact(compile_self_model(body(drive(kinematics))), "locomotion").text == f"You move on {noun}."


@pytest.mark.parametrize(
    ("axes", "sentence"),
    [
        (("yaw", "pitch", "roll"), "You can turn your head left and right, tilt it up and down, and tip it to one side."),
        (("pitch", "yaw"), "You can turn your head left and right and tilt it up and down."),
        (("yaw",), "You can turn your head left and right."),
        (("pitch",), "You can tilt your head up and down, but you cannot turn it to look around."),
        (("pitch", "roll"), "You can tilt your head up and down and tip it to one side, but you cannot turn it to look around."),
        (("linear",), "You have a head, but you cannot turn it to look around."),
    ],
)
def test_a_head_is_described_by_its_axes(axes, sentence):
    assert fact(compile_self_model(body(head(*axes))), "head") == BodyFact("head", sentence)


def test_a_head_that_does_not_turn_cannot_turn_toward_you():
    model = compile_self_model(body(head("pitch")))
    assert model.explanations["cannot_turn"] == "I cannot turn toward you. I have no wheels, and my head does not turn."
    assert model.explanations["cannot_move"] == "I cannot come to you. I have no wheels."


def test_a_head_that_turns_can_turn_toward_you_and_still_cannot_come():
    model = compile_self_model(body(head("yaw")))
    assert model.explanations["cannot_turn"] is None
    assert model.explanations["cannot_move"] == "I cannot come to you. I have no wheels."


def test_a_drive_alone_can_come_and_turn():
    model = compile_self_model(body(drive()), locomotion=wheels())
    assert model.explanations["cannot_move"] is None
    assert model.explanations["cannot_turn"] is None
    assert fact(model, "head").text.startswith("You have no head")


def test_a_gripper_is_declared_and_cannot_be_used_yet():
    model = compile_self_model(body(gripper(), arm()))
    assert fact(model, "arms") == BodyFact(
        "arms", "You have a gripper, but you cannot use it yet, so you cannot pick anything up.", False
    )


def test_an_arm_without_a_hand_cannot_pick_anything_up():
    model = compile_self_model(body(arm()))
    assert fact(model, "arms") == BodyFact(
        "arms", "You have an arm you can move, but no hand, so you cannot pick anything up."
    )
    manifest = body(arm())
    broken = compile_self_model(manifest, capabilities=reported(manifest, broken=("arm",)))
    assert fact(broken, "arms").text == "Your arm is not working right now, so you cannot move it or pick anything up."
    assert not fact(broken, "arms").present


@pytest.mark.parametrize(
    ("parts", "sentence"),
    [
        ([display("eyes", "dual_round")], "You have two round eyes on small screens, and they show your expression."),
        ([display("eyes", "single_round")], "You have one round eye on a small screen, and it shows your expression."),
        ([display("eyes", "rect")], "You have eyes on a screen, and they show your expression."),
        ([display("face", "rect")], "You have a face on a screen, and it shows your expression."),
        ([light("eyes", "single")], "You have lights for eyes that glow and change colour."),
        ([light("eyes"), display("eyes", "dual_round", cap_id="eyes")],
         "You have two round eyes on small screens, and they show your expression."),
    ],
)
def test_eyes_are_described_by_what_shows_them(parts, sentence):
    assert fact(compile_self_model(body(*parts)), "eyes") == BodyFact("eyes", sentence)


def test_a_status_screen_is_not_a_pair_of_eyes():
    assert not fact(compile_self_model(body(display("status", "rect"))), "eyes").present


@pytest.mark.parametrize(
    ("parts", "sentence"),
    [
        ([light("ambient", "ring")], "You have a ring of lights that can glow and pulse."),
        ([light("ambient", "strip")], "You have a strip of lights that can glow and pulse."),
        ([light("ambient", "single")], "You have a light that can glow and pulse."),
        ([light("ambient")], "You have a light that can glow and pulse."),
        ([light("status", "ring")], "You have a status light that shows what you are doing."),
        ([light("status", "ring"), light("ambient", "strip", cap_id="strip")],
         "You have a strip of lights that can glow and pulse."),
    ],
)
def test_one_fact_describes_the_lights_the_first_ambient_one_first(parts, sentence):
    assert fact(compile_self_model(body(*parts)), "lights") == BodyFact("lights", sentence)


def test_lights_for_eyes_are_not_lights_and_no_lights_is_no_fact():
    assert "lights" not in keys(compile_self_model(body(light("eyes"))))
    assert "lights" not in keys(compile_self_model(body()))


def test_lights_that_are_not_working_say_so():
    manifest = body(light("ambient", "ring"))
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=("lamp",)))
    assert fact(model, "lights") == BodyFact("lights", "Your lights are not working right now.", False)


@pytest.mark.parametrize(
    ("parts", "place"),
    [
        ([head("yaw", cap_id="neck"), camera("neck")], "head"),
        ([arm(cap_id="limb"), camera("limb")], "arm"),
        ([display("face", cap_id="panel"), camera("panel")], "face"),
        ([display("status", cap_id="panel"), camera("panel")], "screen"),
        ([camera("body")], "body"),
        ([camera()], "body"),
        ([camera("nowhere")], "body"),
        ([{"id": "body", "type": "joint_group", "role": "head", "driver": MOCK, "joints": joints("yaw")},
          camera("body")], "body"),
    ],
)
def test_a_camera_is_an_absence_that_says_where_it_sits(parts, place):
    said = fact(compile_self_model(body(*parts)), "camera")
    assert said == BodyFact(
        "camera", f"You have a camera on your {place}, but nothing it sees reaches you yet, so you cannot see.", False
    )


def test_a_camera_that_is_not_working_says_so():
    manifest = body(camera())
    model = compile_self_model(manifest, capabilities=reported(manifest, broken=("cam",)))
    assert fact(model, "camera") == BodyFact("camera", "Your camera is not working, so you cannot see.", False)
    assert model.explanations["error"] == "Part of me is not working: my camera."
