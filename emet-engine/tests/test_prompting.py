"""What the language model is told, and what it remembers."""

from __future__ import annotations

from emet_sdk.types import Message, ToolSpec

from emet_engine.prompting import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TURNS,
    SPOKEN_ALOUD,
    Conversation,
    system_prompt,
)


def test_a_written_persona_prompt_is_used_and_the_speaking_rule_follows_it():
    soul = {"identity": {"name": "Emet"}, "persona": {"system_prompt": "You are Emet. Be honest.\n"}}
    text = system_prompt(soul)
    assert text.startswith("You are Emet. Be honest.")
    assert text.endswith(SPOKEN_ALOUD)
    assert "\n\n" in text


def test_without_a_written_prompt_the_name_and_summary_make_one():
    soul = {"identity": {"name": "Barnaby"}, "persona": {"summary": "Gentle,\n  reflective."}}
    text = system_prompt(soul)
    assert text.startswith("You are Barnaby. Gentle, reflective.")
    assert SPOKEN_ALOUD in text


def test_a_bare_soul_still_gets_a_prompt():
    assert system_prompt({}).startswith("You are Emet.")


def test_the_speaking_rule_names_no_hardware():
    """Principle 1 at the prompt: the body enters through the self-model in
    0.5, never through this constant."""
    for word in ("servo", "GPIO", "I2C", "driver", "microphone", "camera"):
        assert word.lower() not in SPOKEN_ALOUD.lower()


def test_a_conversation_alternates_and_packages_its_tail():
    conversation = Conversation()
    conversation.add_user("what time is it")
    conversation.add_assistant("Nearly noon.")
    conversation.add_user("thanks")
    prompt = conversation.prompt("sys", tools=(ToolSpec(name="t", description="d"),), max_tokens=99)
    assert prompt.system == "sys"
    assert [m.role for m in prompt.messages] == ["user", "assistant", "user"]
    assert prompt.messages[1] == Message(role="assistant", content="Nearly noon.")
    assert prompt.max_tokens == 99 and prompt.tools[0].name == "t"


def test_a_conversation_forgets_the_oldest_turns_in_pairs():
    conversation = Conversation(turns=4)
    for i in range(4):
        conversation.add_user(f"q{i}")
        conversation.add_assistant(f"a{i}")
    assert [m.content for m in conversation.messages] == ["q2", "a2", "q3", "a3"]
    assert conversation.messages[0].role == "user"


def test_the_defaults_suit_a_spoken_reply():
    assert DEFAULT_MAX_TOKENS == 300
    assert DEFAULT_TURNS == 24
    assert Conversation().prompt("s").max_tokens == DEFAULT_MAX_TOKENS


# ------------------------------------------------------------------- lines


from emet_engine.prompting import DEFAULT_LINES, persona_lines  # noqa: E402


def test_the_engine_has_words_for_the_moments_the_model_has_none():
    lines = persona_lines({})
    assert lines == dict(DEFAULT_LINES)
    assert lines["declined"] and lines["failed"]
    assert lines["nothing_heard"] is None, "a false wake is met with silence unless the soul says otherwise"


def test_a_soul_overrides_them_in_its_own_voice():
    lines = persona_lines({"persona": {"lines": {"declined": "Not that.", "nothing_heard": "Yes?"}}})
    assert lines["declined"] == "Not that."
    assert lines["nothing_heard"] == "Yes?"
    assert lines["failed"] == DEFAULT_LINES["failed"]


def test_a_soul_can_silence_a_line():
    lines = persona_lines({"persona": {"lines": {"failed": None, "declined": "  "}}})
    assert lines["failed"] is None and lines["declined"] is None


# --------------------------------------------------------- the body in it


from emet_sdk.resolve import descriptors_from_manifest, load_chains, resolve  # noqa: E402
from emet_sdk.types import BodyFact, SelfModel  # noqa: E402

from emet_engine.prompting import (  # noqa: E402
    SELF_MODEL_HEADING,
    SELF_MODEL_PREFACE,
    offered_tags,
    tag_guide,
)


def test_the_body_sits_between_the_persona_and_the_rule_about_speaking():
    model = SelfModel(facts=(BodyFact("voice", "You speak through a speaker."), BodyFact("camera", "You cannot see.", False)))
    text = system_prompt({"persona": {"system_prompt": "You are Emet."}}, self_model=model, tags=["express.curiosity"])
    persona, body, tags, rule = text.split("\n\n")
    assert persona == "You are Emet."
    assert body.splitlines() == [SELF_MODEL_HEADING, SELF_MODEL_PREFACE, "You speak through a speaker.", "You cannot see."]
    assert "[express.curiosity]" in tags
    assert rule == SPOKEN_ALOUD


def test_without_a_body_the_prompt_is_the_persona_and_the_rule():
    assert system_prompt({}, self_model=SelfModel(), tags=()) == system_prompt({})


def body_table(capabilities=()):
    return resolve(load_chains(), descriptors_from_manifest({"capabilities": list(capabilities)}))


def test_a_bodiless_body_is_offered_every_expression_and_nothing_else():
    tags = offered_tags(body_table())
    assert set(tags) == {f"express.{a}" for a in ("curiosity", "delight", "confusion", "concern", "amusement", "boredom", "surprise", "affection", "thinking")}


def test_a_body_with_a_drive_and_a_head_is_offered_the_moves_and_the_glances():
    head = {"id": "h", "type": "joint_group", "role": "head", "joints": [{"id": "p", "axis": "yaw"}, {"id": "t", "axis": "pitch"}]}
    drive = {"id": "b", "type": "drive", "kinematics": "differential"}
    tags = offered_tags(body_table([head, drive]))
    assert tags[:9] == [t for t in tags if t.startswith("express.")]
    assert "move.approach" in tags and "move.turn_to" in tags and "move.stop" in tags
    assert "attend.speaker" in tags
    assert "attend.person" not in tags and "attend.bearing" not in tags, "a tag cannot carry a target"
    assert not any(t.startswith(("signal.", "idle.", "speak", "acknowledge")) for t in tags)


def test_the_guide_glosses_what_a_name_does_not_say():
    guide = tag_guide(["express.curiosity", "move.approach"])
    assert "[express.curiosity], [move.approach] to come closer." in guide
    assert "never read aloud" in guide and "at most one in a reply" in guide
