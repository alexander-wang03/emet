"""The wake plugin contract.

Wake is the one category with nothing beneath it. Every other kind of failure
in Emet degrades: a chain that cannot find a head falls through to a light
ring, and a chain that finds nothing at all still speaks. A robot that cannot
hear its own name has no next rung. It just never answers, and looks broken
rather than limited.

So these tests are mostly about honesty at the boundary — an engine reporting
what it can actually hear, rather than what the manifest hoped it would — and
about keeping the choice of engine swappable. Picovoice disabled every free
Porcupine access key on 30 June 2026. The seam tested here is what makes that
a one-line manifest change instead of a dead robot.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

import pytest

from emet_sdk.discovery import GROUP_WAKE, PluginRegistry
from emet_sdk.plugin import WakePlugin
from emet_sdk.types import WakeDescriptor, WakeEvent
from emet_sdk.validate import MissingPluginError


class _FakeWake(WakePlugin):
    """Defined here rather than imported from emet_hal: the SDK's own tests
    must not depend on the layer above it."""

    engine = "fake"

    def describe(self) -> WakeDescriptor:
        declared = self.params.get("phrases")
        return WakeDescriptor(
            engine=self.engine,
            phrases=frozenset(declared) if declared is not None else frozenset({self.phrase}),
            supports_custom_phrases=bool(self.params.get("supports_custom", False)),
            healthy=bool(self.params.get("healthy", True)),
        )

    async def process(self, frame: bytes) -> WakeEvent | None:
        if self.phrase.encode("utf-8") not in frame:
            return None
        return WakeEvent(phrase=self.phrase, confidence=0.8)


def make(phrase: str = "hey emet", **params: Any) -> _FakeWake:
    return _FakeWake({"engine": "fake", "params": params}, phrase)


# --------------------------------------------------------------- discovery


def test_wake_is_a_real_entry_point_group():
    registry = PluginRegistry.discover()
    assert GROUP_WAKE == "emet.wake"
    assert "mock" in registry.wake_names, (
        "emet-hal registers a mock wake engine; if this fails the package "
        "needs reinstalling so its entry points are picked up"
    )
    assert registry.has_wake("mock")


def test_an_uninstalled_engine_is_a_missing_plugin_not_a_schema_error():
    """The same distinction locomotion makes. `engine: openwakeword` is a
    legal value the day before anyone packages it."""
    registry = PluginRegistry.discover()
    with pytest.raises(MissingPluginError) as exc:
        registry.load_wake("openwakeword")
    assert "openwakeword" in str(exc.value.report)


def test_wake_appears_in_the_registry_listing():
    registry = PluginRegistry(wake={})
    assert not registry
    listed = dict.fromkeys(group for group, _ in PluginRegistry.discover())
    assert "wake" in listed


# -------------------------------------------------------------- descriptor


def test_an_engine_reports_only_the_phrases_it_loaded():
    """The case that matters: the engine started fine, and cannot hear the
    name this particular soul answers to."""
    plugin = make("hey barnaby", phrases=["hey jarvis", "alexa"])
    assert not plugin.describe().can_detect("hey barnaby")
    assert plugin.describe().can_detect("alexa")


def test_a_phonetic_engine_can_detect_anything():
    plugin = make("hey barnaby", phrases=[], supports_custom=True)
    assert plugin.describe().can_detect("hey barnaby")


def test_an_unhealthy_engine_detects_nothing_it_claims():
    """A broken engine listing a phrase must not read as able to hear it."""
    plugin = make("hey emet", healthy=False)
    descriptor = plugin.describe()
    assert "hey emet" in descriptor.phrases
    assert not descriptor.can_detect("hey emet")


def test_confidence_is_bounded():
    WakeEvent(phrase="hey emet", confidence=0.0)
    WakeEvent(phrase="hey emet", confidence=1.0)
    with pytest.raises(ValueError):
        WakeEvent(phrase="hey emet", confidence=1.4)


# ----------------------------------------------------------------- contract


def test_the_phrase_comes_from_the_soul_and_the_engine_from_the_body():
    """The one place a soul-declared value reaches a plugin constructor.
    `identity.wake_word` supplies the phrase; `audio.wake` supplies the rest."""
    plugin = make("hey emet", threshold=0.62)
    assert plugin.phrase == "hey emet"
    assert plugin.params["threshold"] == 0.62
    assert plugin.config["engine"] == "fake"


def test_params_are_read_from_the_wake_block_not_driver_params():
    """Wake is not a manifest capability, so there is no `driver` to unwrap."""
    plugin = _FakeWake({"engine": "fake", "driver": {"params": {"wrong": 1}}}, "hey emet")
    assert plugin.params == {}


def test_process_fires_only_on_the_phrase():
    plugin = make("hey emet")

    async def scenario():
        assert await plugin.process(b"unrelated audio") is None
        event = await plugin.process(b"....hey emet....")
        assert event is not None
        assert event.phrase == "hey emet"
        return event

    assert asyncio.run(scenario()).confidence == 0.8


def test_reset_defaults_to_a_no_op():
    """A stateless detector should not have to implement it."""
    asyncio.run(make().reset())
