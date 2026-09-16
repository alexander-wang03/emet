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
