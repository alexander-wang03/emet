"""Intents leaving the model as tags in the text, lifted out before speech.

The decision the deferred register held for the loop batch. What these
tests protect: a tag is never spoken, a tag split across deltas is still
found, ordinary bracketed prose is left alone, and only names in the closed
vocabulary become intents. From 0.5: an intent keeps its place among the
words, so the engine can act on it where the model put it, and the names a
model may not raise (reserved kinds, `signal`, `speak`) are dropped.
"""

from __future__ import annotations

import logging

import pytest

from emet_sdk import intents as vocabulary
from emet_sdk.types import Intent, Priority

from emet_engine.intent_tags import (
    ENGINE_ONLY_KINDS,
    MAX_TAG_CHARS,
    IntentTags,
    intent_from_tag,
)

CURIOSITY = Intent(kind="express", argument="curiosity", priority=Priority.EXPRESSIVE)
DELIGHT = Intent(kind="express", argument="delight", priority=Priority.EXPRESSIVE)
ATTEND = Intent(kind="attend", argument="speaker", priority=Priority.ATTENTION)

#: One valid tag per reserved kind. A test below fails when the SDK reserves
#: a fifth kind and this list does not name it.
RESERVED_TAGS = ("manipulate.grasp", "navigate.kitchen", "gesture.wave", "attend_joint")

#: Valid names of the kinds only the engine raises.
ENGINE_ONLY_TAGS = ("signal.offline", "signal.muted", "speak", "speak.hello")


def pieces_all(*deltas: str) -> list[str | Intent]:
    """Every piece of a streamed reply, in order, with the flush at the end."""
    tags = IntentTags()
    out: list[str | Intent] = []
    for delta in deltas:
        out.extend(tags.pieces(delta))
    held = tags.flush()
    if held:
        out.append(held)
    return out


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


# ------------------------------------------------------------------- pieces


def test_a_tag_in_the_middle_stands_between_the_words_around_it():
    assert pieces_all("That is new. [express.curiosity] Tell me more.") == [
        "That is new. ",
        CURIOSITY,
        "Tell me more.",
    ]


def test_a_tag_at_the_start_comes_before_every_word():
    """A filler for the tag lands before the sentence the tag opened."""
    assert pieces_all("[express.delight] Hello there.") == [DELIGHT, "Hello there."]


def test_a_tag_at_the_end_comes_after_every_word():
    assert pieces_all("That is new. [express.curiosity]") == ["That is new. ", CURIOSITY]


def test_order_is_kept_across_deltas_when_a_tag_is_split():
    got = pieces_all("Oh! [expr", "ess.cur", "iosity] Really? [attend", ".speaker] Yes.")
    assert got == ["Oh! ", CURIOSITY, "Really? ", ATTEND, "Yes."]


def test_a_delta_that_is_only_part_of_a_tag_has_no_pieces():
    tags = IntentTags()
    assert tags.pieces("Oh! [expr") == ["Oh! "]
    assert tags.pieces("ess.cur") == []
    assert tags.pieces("iosity]") == [CURIOSITY]


def test_two_tags_side_by_side_keep_their_order():
    assert pieces_all("[express.curiosity][attend.speaker] Hm.") == [CURIOSITY, ATTEND, "Hm."]


def test_no_text_piece_is_empty():
    got = pieces_all("[express.curiosity]", " ", "[attend.speaker]", " Hm. [express.delight]")
    assert "" not in got
    assert got == [CURIOSITY, ATTEND, "Hm. ", DELIGHT]


def test_text_split_only_by_a_dropped_token_comes_back_as_one_piece():
    assert pieces_all("Sure. [express.smugness] Done. [laughs] Really.") == ["Sure. Done. Really."]


@pytest.mark.parametrize(
    "deltas",
    [
        ("See [1 and [express.curiosity] more.",),
        ("See [1 and [expr", "ess.curiosity] more."),
        ("See [1", " and [", "express.curiosity] more."),
    ],
)
def test_a_stray_bracket_earlier_in_the_reply_cannot_hide_a_tag(deltas):
    """A tag opens at the last `[` before its `]`. Read from the first one,
    the tag here would be spoken, and the intent lost."""
    got = pieces_all(*deltas)
    at = got.index(CURIOSITY)
    assert [p for p in got if not isinstance(p, str)] == [CURIOSITY]
    assert "".join(got[:at]) == "See [1 and " and "".join(got[at + 1 :]) == "more."


def test_a_doubled_bracket_still_yields_the_tag():
    assert pieces_all("[[express.curiosity]] Hm.") == ["[", CURIOSITY, "] Hm."]


@pytest.mark.parametrize(
    "deltas",
    [
        ("That is new. [express.curiosity] Tell me more.",),
        ("Oh! [expr", "ess.cur", "iosity] Really?"),
        ("See the note [1",),
        ("It was [as the saying goes] fine.",),
        ("[" + "x" * (MAX_TAG_CHARS + 1), " and on"),
        ("[laughs] That is funny. [sighs]",),
        ("Sure. [express.smugness] Done.",),
        ("[express.joy] Hello there.",),
        ("Hi. ", "[attend.speaker]", " ", "[signal.muted] Yes. [navigate.kitchen]"),
        ("See [1 and [expr", "ess.curiosity] more. [", "[attend.speaker]]"),
    ],
)
def test_feed_is_pieces_with_the_words_joined_and_the_intents_listed(deltas):
    """`feed()` is built on `pieces()`, so both must say the same thing about
    the same stream, whitespace included."""
    by_feed, by_pieces = IntentTags(), IntentTags()
    for delta in deltas:
        pieces = by_pieces.pieces(delta)
        text = "".join(p for p in pieces if isinstance(p, str))
        intents = [p for p in pieces if not isinstance(p, str)]
        assert by_feed.feed(delta) == (text, intents)
    assert by_feed.flush() == by_pieces.flush()
    assert by_feed.removed == by_pieces.removed
    assert by_feed.ignored == by_pieces.ignored


# ------------------------------------------------ names the model may not use


def test_the_reserved_tags_here_cover_every_reserved_kind():
    kinds = {vocabulary.parse(tag)[0] for tag in RESERVED_TAGS}
    assert kinds == set(vocabulary.RESERVED_INTENTS)
    assert all(intent_from_tag(tag) is not None for tag in RESERVED_TAGS)


@pytest.mark.parametrize("tag", RESERVED_TAGS)
def test_a_reserved_intent_is_dropped_and_logged_at_debug(tag, caplog):
    """DESIGN 5.2: legal to emit, logged at debug level and dropped."""
    tags = IntentTags()
    with caplog.at_level(logging.DEBUG, logger="emet_engine.intent_tags"):
        clean, found = tags.feed(f"Here you are. [{tag}] Take it.")
        rest = tags.flush()
    assert clean + rest == "Here you are. Take it."
    assert found == []
    assert tags.removed == [tag] and tags.ignored == [tag]
    records = [r for r in caplog.records if r.name == "emet_engine.intent_tags"]
    assert records and all(r.levelno == logging.DEBUG for r in records)
    assert any(repr(tag) in r.getMessage() and "reserved" in r.getMessage() for r in records)


def test_a_reserved_intent_never_appears_among_the_pieces():
    got = pieces_all("[gesture.wave] Hello. [manipulate.offer] Here.")
    assert got == ["Hello. Here."]


def test_the_engine_only_kinds_are_in_the_vocabulary_and_not_reserved():
    assert ENGINE_ONLY_KINDS == {"signal", "speak"}
    for kind in ENGINE_ONLY_KINDS:
        assert kind in vocabulary.P0_INTENTS and not vocabulary.is_reserved(kind)
    assert all(intent_from_tag(tag) is not None for tag in ENGINE_ONLY_TAGS)


@pytest.mark.parametrize("tag", ENGINE_ONLY_TAGS)
def test_a_model_cannot_raise_signal_or_speak(tag, caplog):
    """A model that could write `[signal.offline]` could make the robot show
    a state it is not in; `speak` is the text itself."""
    tags = IntentTags()
    with caplog.at_level(logging.DEBUG, logger="emet_engine.intent_tags"):
        got = tags.pieces(f"All is well. [{tag}] Carry on.")
    assert got == ["All is well. Carry on."]
    assert tags.removed == [tag] and tags.ignored == [tag]
    records = [r for r in caplog.records if r.name == "emet_engine.intent_tags"]
    assert records and all(r.levelno == logging.DEBUG for r in records)
    assert any(repr(tag) in r.getMessage() and "engine" in r.getMessage() for r in records)


def test_a_stray_bracket_cannot_carry_a_signal_tag_into_the_speech():
    tags = IntentTags()
    assert tags.pieces("A stray [ then [signal.offline] fine.") == ["A stray [ then fine."]
    assert tags.ignored == ["signal.offline"]


def test_a_dropped_name_is_found_whatever_its_case():
    tags = IntentTags()
    assert tags.feed("[Signal.Offline] [Navigate.Kitchen] Fine.") == ("Fine.", [])
    assert tags.ignored == ["Signal.Offline", "Navigate.Kitchen"]


def test_a_stage_direction_or_a_typo_is_removed_but_not_ignored():
    """`ignored` holds the rule's drops only, so a log can tell a rule from a
    name the model got wrong."""
    tags = IntentTags()
    tags.feed("[laughs] Sure. [express.smugness] [signal.nonsense] Done.")
    assert tags.removed == ["laughs", "express.smugness", "signal.nonsense"]
    assert tags.ignored == []


def test_a_reported_intent_is_not_logged_as_dropped(caplog):
    tags = IntentTags()
    with caplog.at_level(logging.DEBUG, logger="emet_engine.intent_tags"):
        tags.feed("[express.curiosity] Hm.")
    assert not [r for r in caplog.records if r.name == "emet_engine.intent_tags"]
    assert tags.ignored == []


@pytest.mark.parametrize("held", ["[express.del", "[express.", "[express", "[e"])
def test_a_tag_the_reply_ended_inside_is_dropped_at_flush(held):
    """A reply cut off at its token cap in the middle of a tag. Released as
    text, the voice would read out "express dot del"."""
    tags = IntentTags()
    clean, found = tags.feed("Of course. " + held)
    assert clean == "Of course. " and found == []
    assert tags.flush() == ""


@pytest.mark.parametrize("held", ["[4b", "[1", "[a-b", "[see:"])
def test_an_open_bracket_that_cannot_be_a_tag_is_released_at_flush(held):
    tags = IntentTags()
    tags.feed("It is in section " + held)
    assert tags.flush() == held
