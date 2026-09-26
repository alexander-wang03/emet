"""Acting on an intent through its binding: the six voice actions, a
hardware rung, and the intents that are dropped.

The body is real (started through discovery, on the mock HAL) and the voice
is two recording callables, so every outcome can be read back exactly.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from emet_sdk import intents
from emet_sdk.chains import parse_chain_set
from emet_sdk.discovery import PluginRegistry
from emet_sdk.resolve import load_chains
from emet_sdk.types import Action
from emet_sdk.validate import load_yaml

from emet_engine.acting import Actor, contour, explain_topics, signal, speak
from emet_engine.body import Body
from emet_engine.self_model import compile_self_model

EXAMPLES = Path(__file__).resolve().parents[2] / "emet-sdk" / "examples"


def run(coro):
    return asyncio.run(coro)


class Voice:
    """A stand-in for the session's mouth and sink."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.played: list[bytes] = []

    async def say(self, text: str, *, from_reply: bool = True):
        self.said.append(text)
        return []

    async def play(self, pcm: bytes) -> None:
        self.played.append(pcm)


def actor_for(name: str, *, voiced: bool = True, chains=None, lines=None) -> tuple[Actor, Voice]:
    manifest = load_yaml(EXAMPLES / name)
    body = Body(manifest, PluginRegistry.discover(), chains=chains or load_chains())
    run(body.start())
    model = compile_self_model(manifest, capabilities=body.capabilities, locomotion=body.locomotion, lines=lines)
    voice = Voice()
    actor = Actor(
        body,
        model,
        say=voice.say if voiced else None,
        play=voice.play if voiced else None,
        sample_rate=16000,
    )
    return actor, voice


def perform(actor: Actor, intent):
    return run(actor.perform(intent))


# ------------------------------------------------------------ voice actions


def test_a_sentence_is_a_speak_action_carrying_its_words_and_a_contour():
    actor, voice = actor_for("bodiless.yaml")
    done = perform(actor, speak("Can you hear me?"))
    assert done.outcome == "done" and done.ok and done.voiced
    assert done.binding.action == "utter"
    assert done.action.params["text"] == "Can you hear me?"
    assert done.action.params["contour"] == "rising"
    assert voice.said == ["Can you hear me?"]


def test_curiosity_on_a_bodiless_body_is_a_filler_in_the_voice_and_the_fillers_take_turns():
    actor, voice = actor_for("bodiless.yaml")
    for _ in range(3):
        perform(actor, intents.make("express", "curiosity"))
    assert voice.said == ["hm?", "hmm.", "hm?"]


def test_a_filler_that_is_not_a_word_is_never_read_out():
    """The validator refuses one in a chain file; a chain built in code can
    still carry a null, which `str()` would have made "None"."""
    actor, _ = actor_for("bodiless.yaml")
    assert [actor._filler("express.curiosity", ["hm?", None, 3]) for _ in range(3)] == ["hm?"] * 3
    assert actor._filler("express.joy", [None]) is None


def test_asked_to_come_closer_a_bodiless_body_explains_in_its_self_models_words():
    actor, voice = actor_for("bodiless.yaml")
    done = perform(actor, intents.make("move", "approach"))
    assert done.binding.action == "explain"
    assert voice.said == ["I cannot come to you. I have no wheels."]
    assert done.said == actor.self_model.explanations["cannot_move"]


def test_every_explain_topic_a_body_can_reach_has_words_or_is_the_souls_silence():
    for name in ("bodiless.yaml", "pi-speakerphone.yaml", "mock-scout.yaml"):
        actor, _ = actor_for(name)
        for topic in explain_topics(actor.body.table.bindings):
            assert topic in actor.self_model.explanations, (name, topic)


def test_a_signal_with_a_tone_plays_it_through_the_sink():
    actor, voice = actor_for("bodiless.yaml")
    done = perform(actor, signal("booting"))
    assert done.outcome == "done" and done.audio_ms > 0 and done.voiced
    (pcm,) = voice.played
    assert pcm and len(pcm) % 2 == 0


def test_silence_is_a_satisfied_intent():
    actor, voice = actor_for("bodiless.yaml")
    for intent in (intents.make("attend", "speaker"), intents.make("idle", "doze"), signal("thinking")):
        done = perform(actor, intent)
        assert done.outcome == "silent" and done.ok and not done.voiced
    assert voice.said == [] and voice.played == []


def test_the_backchannel_binds_and_fires_nothing_until_barge_in():
    actor, voice = actor_for("bodiless.yaml")
    done = perform(actor, intents.make("acknowledge"))
    assert done.outcome == "placeholder" and done.ok
    assert voice.said == []


def test_without_a_voice_the_words_are_recorded_as_unvoiced():
    actor, voice = actor_for("bodiless.yaml", voiced=False)
    assert perform(actor, speak("Hello.")).outcome == "unvoiced"
    assert perform(actor, signal("booting")).outcome == "unvoiced"
    assert perform(actor, intents.make("attend", "speaker")).outcome == "silent"


def test_a_soul_that_silenced_its_failure_line_explains_offline_with_nothing():
    actor, voice = actor_for("bodiless.yaml", lines={"failed": None})
    done = perform(actor, signal("offline"))
    assert done.outcome == "silent" and voice.said == []


def test_the_offline_explanation_is_the_souls_own_failure_line():
    actor, voice = actor_for("bodiless.yaml", lines={"failed": "I lost the thread.", "declined": None})
    perform(actor, signal("offline"))
    assert voice.said == ["I lost the thread."]


def test_a_voice_rung_naming_an_action_no_engine_performs_is_unknown_and_quiet():
    chains = load_chains()
    chains.update(parse_chain_set({"express.delight": {"rungs": [{"actuator": {"voice": True}, "action": "sing"}]}}))
    actor, voice = actor_for("bodiless.yaml", chains=chains)
    done = perform(actor, intents.make("express", "delight"))
    assert done.outcome == "unknown" and not done.ok and voice.said == []


def test_a_tone_preset_nobody_rendered_is_unknown():
    chains = load_chains()
    chains.update(parse_chain_set({"signal.booting": {"rungs": [{"actuator": {"voice": True}, "action": "tone", "params": {"preset": "fanfare"}}]}}))
    actor, voice = actor_for("bodiless.yaml", chains=chains)
    done = perform(actor, signal("booting"))
    assert done.outcome == "unknown" and "fanfare" in done.detail and voice.played == []


# -------------------------------------------------------------- hardware


def test_on_the_scout_curiosity_tilts_the_head_and_says_nothing():
    actor, voice = actor_for("mock-scout.yaml")
    done = perform(actor, intents.make("express", "curiosity"))
    assert done.outcome == "done" and not done.binding.is_voice
    (applied,) = actor.body.plugin("head").applied
    assert applied == Action(capability_id="head", name="tilt", params={"angle_deg": 12, "speed": 0.4, "hold_ms": 700}, intensity=0.5)
    assert voice.said == []


def test_a_part_that_raises_loses_the_gesture_and_not_the_turn(monkeypatch):
    from emet_hal import mock

    async def jammed(self, action):
        raise RuntimeError("jammed")

    monkeypatch.setattr(mock.MockActuator, "apply", jammed)
    actor, _ = actor_for("mock-scout.yaml")
    done = perform(actor, intents.make("express", "curiosity"))
    assert done.outcome == "failed" and "jammed" in done.detail


# -------------------------------------------------------- what is dropped


def test_a_reserved_intent_is_logged_at_debug_and_dropped(caplog):
    actor, voice = actor_for("mock-scout.yaml")
    with caplog.at_level(logging.DEBUG, logger="emet_engine.acting"):
        done = perform(actor, intents.make("manipulate", "grasp"))
    assert done.outcome == "dropped" and done.detail == "reserved"
    assert any("reserved" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)
    assert all(not p.applied for p in (actor.body.plugin(c) for c in ("head", "eyes", "ring")))


def test_every_outcome_is_kept_and_reported():
    seen = []
    actor, _ = actor_for("bodiless.yaml")
    actor.on_performed = seen.append
    perform(actor, signal("listening"))
    perform(actor, intents.make("attend", "none"))
    assert [p.intent.name for p in actor.history] == ["signal.listening", "attend.none"]
    assert seen == list(actor.history)


@pytest.mark.parametrize(
    "text,expected",
    [("Is it?", "rising"), ("Oh!", "bright"), ("It is.", "level"), ('"Really?"', "rising"), ("", "level")],
)
def test_the_contour_is_read_off_the_end_mark(text, expected):
    assert contour(text) == expected
