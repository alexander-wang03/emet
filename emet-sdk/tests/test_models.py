"""The one selection rule, tested once for every stage it serves.

`emet_sdk.models` chooses the provider for speech recognition, the language
model and the voice by the same rule, and each stage's own test file checks
it for that stage in its own words. Two claims were left without a test named
for them, and the roadmap asked for both by name in 0.5:

* a body's `models.<stage>.params` ride along with whichever provider runs,
  whether the soul chose it or the body took the stage over;
* a soul bundle written before `models.tts` existed, carrying `voice.engine`
  and `voice.model`, still validates, and those two fields choose nothing.

The code already did both. A behaviour with no test named for it is one
refactor away from quietly stopping.

**Note what this file imports.** Only `emet_sdk`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import pytest

from emet_sdk.models import chat_selection, model_selection, stt_selection, tts_selection
from emet_sdk.plugin import VoicePlugin
from emet_sdk.types import VoiceDescriptor
from emet_sdk.validate import load_yaml, validate_soul

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

#: Per stage: the function the engine calls, what a soul chooses, how a body
#: tunes it, and what a body that takes the stage over names instead. The
#: tuning params are ones the shipped provider for that stage really reads.
CASES: dict[str, dict[str, Any]] = {
    "stt": {
        "select": stt_selection,
        "soul": {"provider": "deepgram", "model": "nova-3", "key_env": "EMET_DEEPGRAM_KEY"},
        "tuning": {"open_timeout_s": 20.0},
        "takeover": {"provider": "mock", "params": {"transcript": "what time is it"}},
    },
    "chat": {
        "select": chat_selection,
        "soul": {"provider": "openai", "model": "gpt-5.6-terra", "key_env": "EMET_OPENAI_KEY"},
        "tuning": {"timeout_s": 90.0},
        "takeover": {"provider": "mock", "params": {"reply": "It is noon."}},
    },
    "tts": {
        "select": tts_selection,
        "soul": {"provider": "deepgram", "model": "aura-2-thalia-en", "key_env": "EMET_DEEPGRAM_KEY"},
        "tuning": {"sample_rate": 16000},
        "takeover": {"provider": "mock", "params": {"sample_rate": 8000}},
    },
}


def manifest(stage: str | None = None, block: Mapping[str, Any] | None = None) -> dict:
    doc: dict = {"audio": {"input": {"device": "x"}, "output": {"device": "y"}}}
    if stage is not None and block is not None:
        doc["models"] = {stage: dict(block)}
    return doc


def soul(stage: str | None = None, block: Mapping[str, Any] | None = None) -> dict:
    doc: dict = {"identity": {"name": "Emet", "wake_word": "hey emet"}}
    if stage is not None and block is not None:
        doc["models"] = {stage: dict(block)}
    return doc


class _Voice(VoicePlugin):
    """Defined here rather than imported from emet_providers: the SDK's own
    tests must not depend on the layer above it."""

    provider = "fake"

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(provider=self.provider, model=self.model)

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        yield bytes(2)


# ------------------------------------------------------------ the params rule


@pytest.mark.parametrize("stage", sorted(CASES))
def test_a_bodys_params_ride_along_with_whichever_provider_runs(stage: str):
    case = CASES[stage]
    select = case["select"]
    chosen_by_soul = soul(stage, case["soul"])

    # The soul chooses and the body tunes: the soul's provider, model and key
    # name, with the body's params beside them.
    tuned = manifest(stage, {"params": case["tuning"]})
    got = select(tuned, chosen_by_soul)
    assert got == {**case["soul"], "params": case["tuning"]}
    assert got == model_selection(tuned, chosen_by_soul, stage)

    # The body takes the stage over: its provider and its params, and none of
    # the soul's model or key name, since those belong to a different provider.
    taken = manifest(stage, case["takeover"])
    got = select(taken, chosen_by_soul)
    assert got == {
        "provider": case["takeover"]["provider"],
        "model": None,
        "key_env": None,
        "params": case["takeover"]["params"],
    }

    # The body names no params: an empty mapping, never None, so a plugin
    # reads `params` without checking for it first. That holds whether the
    # soul chose the provider or the body took the stage over.
    got = select(manifest(), chosen_by_soul)
    assert got is not None and got["params"] == {}
    got = select(manifest(stage, {"provider": case["takeover"]["provider"]}), chosen_by_soul)
    assert got is not None and got["provider"] == case["takeover"]["provider"]
    assert got["params"] == {}

    # Neither names a provider: nothing runs, and the params ride along with
    # nothing. There is no default provider to hand them to.
    assert select(manifest(stage, {"params": case["tuning"]}), soul()) is None


@pytest.mark.parametrize("stage", sorted(CASES))
def test_the_params_a_plugin_receives_are_its_own_copy(stage: str):
    """The params ride along untouched, in both directions: the selection
    does not change the manifest, and a plugin that edits its params does not
    change the next boot's selection."""
    case = CASES[stage]
    body = manifest(stage, {"params": dict(case["tuning"])})
    got = case["select"](body, soul(stage, case["soul"]))
    assert got is not None
    got["params"]["added_by_a_plugin"] = True
    assert body["models"][stage]["params"] == case["tuning"]


# ------------------------------------------------- the superseded voice fields


def test_a_soul_with_the_superseded_voice_fields_validates_and_they_are_ignored():
    """`voice.engine` and `voice.model` named the voice before `models.tts`
    existed. A bundle written then is still a valid bundle, and the voice it
    gets is the one `models.tts` names: here LJ Speech, never the Amy model
    its old `voice` block still mentions."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    doc["voice"] = {"engine": "piper", "model": "en_US-amy-medium", "rate": 1.0}

    report = validate_soul(doc)
    assert report.ok, report.errors

    body = load_yaml(EXAMPLES / "bodiless.yaml")
    chosen = tts_selection(body, doc)
    assert chosen is not None
    assert chosen["provider"] == "piper"
    assert chosen["model"] == "en_US-ljspeech-medium"
    assert chosen == tts_selection(body, {**doc, "voice": {"rate": 1.0}})

    # And through to the plugin: the model comes from the selection, and the
    # voice block contributes only its rate.
    voice = _Voice(chosen, doc["voice"])
    assert voice.model == "en_US-ljspeech-medium"
    assert voice.describe().model == "en_US-ljspeech-medium"
    assert voice.rate == 1.0


def test_the_superseded_voice_fields_choose_nothing_on_their_own():
    """A soul with the old fields and no `models.tts` has no voice. Reading
    `voice.engine` as a fallback would bring back the default provider the
    selection rule refuses to have."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    doc["voice"] = {"engine": "piper", "model": "en_US-amy-medium", "rate": 1.0}
    del doc["models"]["tts"]
    assert validate_soul(doc).ok
    assert tts_selection(load_yaml(EXAMPLES / "bodiless.yaml"), doc) is None


def test_both_superseded_engine_values_still_validate():
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    for engine in ("piper", "cloud"):
        doc["voice"] = {"engine": engine, "rate": 1.0}
        assert validate_soul(doc).ok, engine
