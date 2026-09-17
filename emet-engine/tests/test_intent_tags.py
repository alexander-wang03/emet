"""Intents leaving the model as tags in the text, lifted out before speech.

The decision the deferred register held for the loop batch. What these
tests protect: a tag is never spoken, a tag split across deltas is still
found, ordinary bracketed prose is left alone, and only names in the closed
vocabulary become intents.
"""

from __future__ import annotations

from emet_sdk.types import Intent, Priority

from emet_engine.intent_tags import MAX_TAG_CHARS, IntentTags, intent_from_tag


def feed_all(*deltas: str) -> tuple[str, list[Intent]]:
    tags = IntentTags()
    text: list[str] = []
    found: list[Intent] = []
    for delta in deltas:
        clean, intents = tags.feed(delta)
        text.append(clean)
        found.extend(intents)
    text.append(tags.flush())
    return "".join(text), found


def test_a_tag_is_lifted_out_and_named():
    text, found = feed_all("That is new. [express.curiosity] Tell me more.")
    assert text == "That is new. Tell me more."
    assert found == [Intent(kind="express", argument="curiosity", priority=Priority.EXPRESSIVE)]


def test_a_tag_split_across_deltas_is_still_found():
    text, found = feed_all("Oh! [expr", "ess.cur", "iosity] Really?")
    assert text == "Oh! Really?"
    assert [i.name for i in found] == ["express.curiosity"]


def test_an_unclosed_bracket_at_the_end_of_the_reply_is_released_by_flush():
    tags = IntentTags()
    clean, found = tags.feed("See the note [1")
    assert clean == "See the note " and found == []
    assert tags.flush() == "[1"


def test_bracketed_prose_with_spaces_is_ordinary_text():
    text, found = feed_all("It was [as the saying goes] fine.")
    assert text == "It was [as the saying goes] fine."
    assert found == []


def test_a_bracket_too_long_to_be_a_tag_is_released_unchanged():
    long = "[" + "x" * (MAX_TAG_CHARS + 1)
    text, found = feed_all(long, " and on")
    assert text == long + " and on" and found == []


def test_stage_directions_are_dropped_and_not_reported():
    """The prompt forbids them and models write them anyway. Reading
    "laughs" aloud would be worse than losing it."""
    tags = IntentTags()
    clean, found = tags.feed("[laughs] That is funny. [sighs]")
    assert clean == "That is funny. "
    assert found == []
    assert tags.removed == ["laughs", "sighs"]


def test_a_tag_that_names_no_intent_is_dropped_not_spoken():
    text, found = feed_all("Sure. [express.smugness] Done.")
    assert text == "Sure. Done." and found == []
    assert intent_from_tag("express.smugness") is None
    assert intent_from_tag("dance") is None


def test_a_tag_at_the_start_leaves_no_leading_space():
    text, _ = feed_all("[express.joy] Hello there.")
    assert text == "Hello there."


def test_case_is_forgiven():
    _, found = feed_all("[Express.Curiosity] hm.")
    assert [i.name for i in found] == ["express.curiosity"]


def test_intents_carry_the_vocabularys_default_priority():
    attend = intent_from_tag("attend.speaker")
    assert attend is not None and attend.priority == Priority.ATTENTION
