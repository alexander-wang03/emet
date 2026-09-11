"""The shipped wake engine.

These tests cover configuration, dictionary resolution, and every failure
path, none of which need a microphone. What they deliberately do not cover is
whether the engine fires on a human actually saying the phrase.

That was verified by hand against synthesised speech before this plugin was
written: with the pronunciations in `SHIPPED_LEXICON`, "hey emet", "hey hugr"
and "hey neuma" each fired on their own clip and on none of the other five,
including a conversational control clip. "hey barnaby" fired straight from the
bundled dictionary with no lexicon entry at all, which is the whole argument
for a phonetic engine.

Committing that audio would put binary blobs of uncertain provenance in the
repository to support a handful of assertions, so the gap is recorded here
rather than papered over. It closes when 0.3 has a real capture path and a
recorded fixture worth keeping.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pocketsphinx", reason="emet-hal[wake] is not installed")

from emet_hal.pocketsphinx_wake import (  # noqa: E402
    DEFAULT_THRESHOLD,
    SHIPPED_LEXICON,
    PocketSphinxWake,
)


def run(coro):
    return asyncio.run(coro)


def started(phrase: str, **params) -> PocketSphinxWake:
    plugin = PocketSphinxWake({"engine": "pocketsphinx", "params": params}, phrase)
    run(plugin.start())
    return plugin


# ------------------------------------------------------- resolving a phrase


def test_an_ordinary_english_name_needs_no_lexicon():
    """The argument for a phonetic engine, as a test.

    This exact phrase was rejected by the validator before wake became a
    plugin category, on the grounds that nobody had trained a model for it.
    """
    plugin = started("hey barnaby")
    assert plugin.describe().healthy, plugin.health().detail
    assert plugin.describe().can_detect("hey barnaby")


def test_a_shipped_name_resolves_through_the_lexicon():
    """`emet` is not an English word, so it needs its five phonemes."""
    assert "emet" in SHIPPED_LEXICON
    plugin = started("hey emet")
    assert plugin.describe().healthy, plugin.health().detail


@pytest.mark.parametrize("name", sorted(SHIPPED_LEXICON))
def test_every_shipped_name_can_be_taught(name: str):
    plugin = started(f"hey {name}")
    assert plugin.describe().healthy, plugin.health().detail


def test_a_body_can_supply_its_own_pronunciation():
    plugin = started("hey zorbax", lexicon={"zorbax": "Z AO R B AE K S"})
    assert plugin.describe().healthy, plugin.health().detail


def test_alternate_pronunciations_are_accepted_as_a_list():
    plugin = started("hey zorbax", lexicon={"zorbax": ["Z AO R B AE K S", "Z ER B AE K S"]})
    assert plugin.describe().healthy, plugin.health().detail


# -------------------------------------------------------------- failing well


def test_an_unpronounceable_phrase_is_unhealthy_and_says_why():
    """No fallback exists below wake, so this has to be loud and specific."""
    plugin = started("hey zzqqxvv")
    assert not plugin.describe().healthy
    health = plugin.health()
    assert not health.ok
    assert "unpronounceable_phrase" in health.faults
    assert "zzqqxvv" in health.detail
    assert "lexicon" in health.detail


def test_an_engine_that_did_not_start_hears_nothing():
    plugin = started("hey zzqqxvv")
    assert run(plugin.process(b"\x00\x00" * 1280)) is None
    assert plugin.describe().phrases == frozenset()


def test_a_started_engine_reports_only_the_phrase_it_loaded():
    """It could have been pointed at any phrase. It was pointed at one."""
    plugin = started("hey emet")
    descriptor = plugin.describe()
    assert descriptor.supports_custom_phrases
    assert descriptor.can_detect("hey emet")
    assert not descriptor.can_detect("hey barnaby")


# ------------------------------------------------------------ audio contract


def test_the_descriptor_states_the_rate_and_frame_it_wants():
    """The engine feeds frames to this spec; guessing would degrade detection
    silently rather than failing."""
    descriptor = started("hey emet").describe()
    assert descriptor.sample_rate == 16000
    assert descriptor.frame_samples == 1280
    assert descriptor.engine == "pocketsphinx"


def test_silence_does_not_wake_it():
    plugin = started("hey emet")
    for _ in range(40):
        assert run(plugin.process(b"\x00\x00" * 1280)) is None


def test_the_threshold_is_tunable_and_has_a_measured_default():
    """1e-15 keeps 44 of 49 wakes on a real-room recording and cuts false
    wakes on ordinary talk from three a minute to one every two and a half.
    See the tables in the plugin."""
    assert DEFAULT_THRESHOLD == 1e-15
    plugin = started("hey emet", threshold=1e-30)
    assert plugin.describe().healthy


def test_shutdown_stops_listening():
    plugin = started("hey emet")
    assert plugin.describe().healthy
    run(plugin.shutdown())
    assert not plugin.describe().healthy
    assert run(plugin.process(b"\x00\x00" * 1280)) is None
