"""What the robot knows about its own body, as the SDK types it.

Two contracts, both new in 0.5 and both about the body rather than the soul:

* `BodyFact` and `SelfModel`, the shapes the engine's self-model compiler
  fills in (`DESIGN.md` section 7). The prompt and the `explain` voice rung
  both read one `SelfModel`, which is what keeps them from disagreeing
  (section 6.1). The compiler lives in the engine; these tests pin only what
  the SDK promises about the containers.
* `Plugin.carry_over()`, the params a plugin wants back at the next boot on
  the same body (section 4.1). The default has to be an empty mapping on
  every category, or the engine would write something into the body's state
  file for a plugin that never asked it to.

**Note what this file imports.** Only `emet_sdk`. The plugins below are
minimal subclasses written here, because the SDK's own tests must not depend
on the layers above it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import MutableMapping
from typing import Any, AsyncIterator, Callable

import pytest

import emet_sdk
from emet_sdk.plugin import (
    ActuatorPlugin,
    LanguageModelPlugin,
    LocomotionPlugin,
    Plugin,
    SensorPlugin,
    TranscriberPlugin,
    VoicePlugin,
    WakePlugin,
)
from emet_sdk.types import (
    Action,
    AudioFormat,
    BodyFact,
    CapabilityDescriptor,
    LanguageModelDescriptor,
    LocomotionDescriptor,
    Prompt,
    Reading,
    ReplyDone,
    ReplyEvent,
    SelfModel,
    Transcript,
    TranscriberDescriptor,
    Twist,
    VoiceDescriptor,
    WakeDescriptor,
    WakeEvent,
)


def bodiless_facts() -> tuple[BodyFact, ...]:
    """A short self-model in the order the compiler writes it: two things
    the body has, two it lacks, interleaved so that order is observable."""
    return (
        BodyFact("hearing", "You hear through one microphone."),
        BodyFact("locomotion", "You have no wheels and no legs.", present=False),
        BodyFact("voice", "You speak through a speaker."),
        BodyFact("camera", "You cannot see: you have no camera.", present=False),
    )


# ------------------------------------------------------------------ BodyFact


def test_a_body_fact_is_a_presence_unless_it_says_otherwise():
    fact = BodyFact("voice", "You speak through a speaker.")
    assert fact.present
    assert (fact.key, fact.text) == ("voice", "You speak through a speaker.")
    assert not BodyFact("camera", "You cannot see.", present=False).present


def test_a_body_fact_cannot_be_changed_after_it_is_compiled():
    """The self-model is compiled once at boot. A fact that could be edited
    in place could drift from the explanation compiled beside it."""
    fact = BodyFact("voice", "You speak through a speaker.")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.text = "You sing."  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.present = False  # type: ignore[misc]


def test_equal_facts_compare_equal():
    """The compiler's determinism test compares two compilations with `==`."""
    assert BodyFact("voice", "x") == BodyFact("voice", "x", present=True)
    assert BodyFact("voice", "x") != BodyFact("voice", "x", present=False)


# ----------------------------------------------------------------- SelfModel


def test_an_empty_self_model_says_nothing():
    model = SelfModel()
    assert model.facts == ()
    assert model.absences == ()
    assert model.text == ""
    assert dict(model.explanations) == {}


def test_the_text_is_the_facts_one_a_line_in_order():
    facts = bodiless_facts()
    model = SelfModel(facts=facts)
    assert model.text.splitlines() == [f.text for f in facts]
    assert model.text == (
        "You hear through one microphone.\n"
        "You have no wheels and no legs.\n"
        "You speak through a speaker.\n"
        "You cannot see: you have no camera."
    )


def test_the_absences_are_the_facts_the_body_lacks_in_their_order():
    """Absences do more work than presences: they stop the robot offering
    what it cannot do. They keep the compiler's order, so a prompt built from
    them reads the same way every boot."""
    model = SelfModel(facts=bodiless_facts())
    assert [f.key for f in model.absences] == ["locomotion", "camera"]
    assert all(not f.present for f in model.absences)
    assert isinstance(model.absences, tuple)


def test_a_body_with_nothing_missing_has_no_absences():
    model = SelfModel(facts=(BodyFact("voice", "You speak through a speaker."),))
    assert model.absences == ()


def test_the_explanations_travel_beside_the_facts_and_may_be_silent():
    """One compilation produces both. None is nothing to say: the body can
    do it, or the soul chose silence. It is an answer and never a gap."""
    model = SelfModel(
        facts=bodiless_facts(),
        explanations={"cannot_move": "I cannot come to you. I have no wheels.", "offline": None},
    )
    assert model.explanations["cannot_move"] == "I cannot come to you. I have no wheels."
    assert model.explanations["offline"] is None


def test_a_self_model_cannot_be_swapped_out_after_it_is_compiled():
    model = SelfModel(facts=bodiless_facts())
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.facts = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.explanations = {}  # type: ignore[misc]


def test_two_compilations_of_the_same_body_compare_equal():
    explanations = {"cannot_move": "I cannot come to you. I have no wheels.", "error": None}
    assert SelfModel(bodiless_facts(), dict(explanations)) == SelfModel(
        bodiless_facts(), dict(explanations)
    )
    assert SelfModel(bodiless_facts(), explanations) != SelfModel(
        bodiless_facts()[:-1], explanations
    )


def test_the_self_model_types_are_part_of_the_public_surface():
    """A plugin author and the engine import them from the package root, the
    way they import `Intent` and `CapabilityDescriptor`."""
    assert emet_sdk.BodyFact is BodyFact
    assert emet_sdk.SelfModel is SelfModel
    assert {"BodyFact", "SelfModel"} <= set(emet_sdk.__all__)


# ---------------------------------------------------------------- carry-over


class _Head(ActuatorPlugin):
    capability_type = "joint_group"

    def describe(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(capability_id=self.capability_id, capability_type="joint_group")

    async def apply(self, action: Action) -> None:
        return None


class _Imu(SensorPlugin):
    capability_type = "sensor"

    def describe(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(capability_id=self.capability_id, capability_type="sensor")

    async def poll(self) -> Reading:
        return Reading(capability_id=self.capability_id, kind="imu")


class _Wheels(LocomotionPlugin):
    kinematics = "differential"

    def describe(self) -> LocomotionDescriptor:
        return LocomotionDescriptor(
            kinematics=self.kinematics,
            max_linear_mps=0.3,
            max_angular_rps=1.5,
            can_turn_in_place=True,
            can_translate_and_rotate=True,
        )

    async def command(self, twist: Twist) -> None:
        return None


class _Wake(WakePlugin):
    engine = "fake"

    def describe(self) -> WakeDescriptor:
        return WakeDescriptor(engine=self.engine, phrases=frozenset({self.phrase}))

    async def process(self, frame: bytes) -> WakeEvent | None:
        return None


class _Transcriber(TranscriberPlugin):
    provider = "fake"

    def describe(self) -> TranscriberDescriptor:
        return TranscriberDescriptor(provider=self.provider)

    async def feed(self, frame: bytes) -> Transcript | None:
        return None

    async def finish(self) -> Transcript:
        return Transcript(text="", final=True)


class _Model(LanguageModelPlugin):
    provider = "fake"

    def describe(self) -> LanguageModelDescriptor:
        return LanguageModelDescriptor(provider=self.provider)

    async def reply(self, prompt: Prompt) -> AsyncIterator[ReplyEvent]:
        yield ReplyDone(text="", stop_reason="end")


class _Voice(VoicePlugin):
    provider = "fake"

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(provider=self.provider)

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        yield bytes(2)


#: One plugin of every category, each built the way the engine builds it,
#: with params in the place its category reads them from. A default that
#: echoed `params` back would show up here as a non-empty carry-over.
_EVERY_CATEGORY: dict[str, Callable[[], Plugin]] = {
    "actuator": lambda: _Head({"id": "head", "type": "joint_group", "driver": {"params": {"bus": 1}}}),
    "sensor": lambda: _Imu({"id": "imu", "type": "sensor", "driver": {"params": {"bus": 1}}}),
    "locomotion": lambda: _Wheels({"id": "base", "type": "drive", "driver": {"params": {"bus": 1}}}),
    "wake": lambda: _Wake({"engine": "fake", "params": {"cmninit": "40,3,-1"}}, "hey emet"),
    "transcriber": lambda: _Transcriber({"provider": "fake", "params": {"x": 1}}, AudioFormat()),
    "language model": lambda: _Model({"provider": "fake", "params": {"x": 1}}),
    "voice": lambda: _Voice({"provider": "fake", "params": {"x": 1}}, {"rate": 1.0}),
}


@pytest.mark.parametrize("category", sorted(_EVERY_CATEGORY))
def test_a_plugin_carries_nothing_over_unless_it_says_so(category: str):
    """Body-local state is opt-in. A plugin that learned nothing about this
    body leaves nothing in its state file."""
    plugin = _EVERY_CATEGORY[category]()
    assert isinstance(plugin, Plugin)
    carried = plugin.carry_over()
    assert dict(carried) == {}


def test_one_plugins_carry_over_never_reaches_another():
    """The contract returns a `Mapping`. If the default is a mutable one, each
    call has to make a new one: a single shared dict that one caller wrote
    into would carry that plugin's values into every other plugin."""
    first = _Wake({"engine": "fake"}, "hey emet").carry_over()
    second = _Wake({"engine": "fake"}, "hey emet").carry_over()
    if isinstance(first, MutableMapping):
        assert first is not second
        first["cmninit"] = "40,3,-1"
    assert dict(second) == {}
    assert dict(_Wake({"engine": "fake"}, "hey emet").carry_over()) == {}


def test_a_plugin_that_learned_something_says_what_it_learned():
    """The override the contract is for: an adapted mean, a calibration."""

    class _AdaptingWake(_Wake):
        def carry_over(self) -> dict[str, Any]:
            return {"cmninit": "43.0,6.0,-1.2"}

    plugin = _AdaptingWake({"engine": "fake"}, "hey emet")
    assert plugin.carry_over() == {"cmninit": "43.0,6.0,-1.2"}
    assert plugin.params == {}, "carrying over does not change what it was built from"
