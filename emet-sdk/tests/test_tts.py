"""The speech synthesis seam.

The third provider seam, built like the other two: a contract in the SDK, a
mock behind it, then the local voice and one cloud voice. The design keeps
synthesis local by default, so the reference soul names the local one.

**Note what this file imports.** Only `emet_sdk`. Every plugin it exercises
lives in `emet_providers` and arrives by discovery, which is the constraint
the engine works under.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from emet_sdk.discovery import GROUP_TTS, PluginRegistry
from emet_sdk.models import STAGES, model_selection, tts_selection
from emet_sdk.plugin import PluginError, VoicePlugin
from emet_sdk.types import VoiceDescriptor
from emet_sdk.validate import (
    BUILTIN_TTS,
    MissingPluginError,
    load_yaml,
    validate_manifest,
    validate_soul,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run(coro):
    return asyncio.run(coro)


def codes(report) -> set[str]:
    return {f.code for f in report.errors}


def manifest(**models: Any) -> dict:
    doc = {"audio": {"input": {"device": "x"}, "output": {"device": "y"}}}
    if models:
        doc["models"] = models
    return doc


def soul(voice: dict | None = None, **models: Any) -> dict:
    doc: dict = {"identity": {"name": "Emet", "wake_word": "hey emet"}}
    if voice is not None:
        doc["voice"] = voice
    if models:
        doc["models"] = models
    return doc


async def collect(chunks: AsyncIterator[bytes]) -> list[bytes]:
    return [c async for c in chunks]


class _FakeBatchVoice(VoicePlugin):
    """Defined here rather than imported from emet_providers: the SDK's own
    tests must not depend on the layer above it. Yields a sentence as one
    chunk, the way a voice that does not stream would."""

    provider = "fake"

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(provider=self.provider, model=self.model, sample_rate=16000, streaming=False)

    async def speak(self, text: str):
        if not text.strip():
            return
        if text == "fail":
            yield b"\x01\x00"
            raise PluginError("the fake voice lost its breath")
        yield bytes(2 * len(text))


# ---------------------------------------------------------------- discovery


def test_tts_is_a_real_entry_point_group():
    registry = PluginRegistry.discover()
    assert GROUP_TTS == "emet.tts"
    assert BUILTIN_TTS <= set(registry.tts_names), (
        "emet-providers registers three voices; if this fails the package "
        "needs installing so its entry points are picked up"
    )
    assert registry.has_tts("mock")


def test_tts_appears_in_the_registry_listing():
    assert not PluginRegistry(tts={})
    listed = {group for group, _ in PluginRegistry.discover()}
    assert "tts" in listed


def test_an_uninstalled_voice_is_a_missing_plugin_not_a_schema_error():
    registry = PluginRegistry.discover()
    with pytest.raises(MissingPluginError) as exc:
        registry.load_tts("gramophone")
    assert "gramophone" in str(exc.value.report)
    assert "speech synthesis provider" in str(exc.value.report)


def test_every_shipped_voice_arrives_without_being_imported():
    registry = PluginRegistry.discover()
    for name in sorted(BUILTIN_TTS):
        cls = registry.load_tts(name)
        assert cls.__module__.startswith("emet_providers"), cls.__module__
        assert issubclass(cls, VoicePlugin)
        assert cls.provider == name


def test_the_engine_can_build_and_drive_a_voice_it_never_imported():
    """A name from the documents, a class from the registry,
    `cls(config, voice)`, a sentence in, audio out."""
    selection = tts_selection(manifest(), soul(tts={"provider": "mock"}))
    assert selection is not None
    voice = PluginRegistry.discover().load_tts(selection["provider"])(selection, {"rate": 1.0})

    async def scenario():
        await voice.start()
        described = voice.describe()
        assert described.healthy and described.sample_rate > 0
        chunks = await collect(voice.speak("what time is it"))
        await voice.shutdown()
        return chunks

    chunks = run(scenario())
    assert chunks and all(isinstance(c, bytes) for c in chunks)
    assert all(len(c) % 2 == 0 for c in chunks), "int16 audio has an even number of bytes"


# ----------------------------------------------------------------- contract


def test_a_batch_voice_satisfies_the_same_contract():
    voice = _FakeBatchVoice({"provider": "fake", "model": "fake-1"})
    chunks = run(collect(voice.speak("hello")))
    assert chunks == [bytes(10)]
    assert not voice.describe().streaming
    assert voice.model == "fake-1"


def test_blank_text_yields_nothing():
    voice = _FakeBatchVoice({"provider": "fake"})
    assert run(collect(voice.speak("   "))) == []


def test_a_failure_mid_sentence_raises_after_the_audio_so_far():
    voice = _FakeBatchVoice({"provider": "fake"})

    async def scenario():
        got = []
        with pytest.raises(PluginError, match="breath"):
            async for chunk in voice.speak("fail"):
                got.append(chunk)
        return got

    assert run(scenario()) == [b"\x01\x00"]


def test_the_config_and_the_souls_voice_block_are_unpacked_as_the_engine_hands_them_over():
    voice = _FakeBatchVoice(
        {"provider": "fake", "model": "m", "key_env": "K", "params": {"x": 1}},
        {"rate": 1.25, "pitch_shift_semitones": 2},
    )
    assert (voice.model, voice.key_env, voice.params) == ("m", "K", {"x": 1})
    assert voice.rate == 1.25
    assert voice.voice["pitch_shift_semitones"] == 2
    assert "key" not in {k.lower() for k in voice.config}


def test_the_rate_defaults_to_the_voices_own_pace():
    assert _FakeBatchVoice({"provider": "fake"}).rate == 1.0
    assert _FakeBatchVoice({"provider": "fake"}, {}).rate == 1.0
    assert _FakeBatchVoice({"provider": "fake"}, {"rate": 0}).rate == 1.0
    assert _FakeBatchVoice({"provider": "fake"}, {"rate": "fast"}).rate == 1.0


def test_a_voice_descriptor_states_the_rate_the_sink_must_open_at():
    d = VoiceDescriptor(provider="fake")
    assert d.sample_rate == 22050 and not d.streaming and d.healthy
    assert VoiceDescriptor(provider="fake", sample_rate=24000, streaming=True).sample_rate == 24000


def test_a_voice_is_a_plugin_with_the_shared_lifecycle():
    from emet_sdk.plugin import Plugin

    voice = _FakeBatchVoice({"provider": "fake"})
    assert isinstance(voice, Plugin)
    assert voice.health().ok
    run(voice.start())
    run(voice.shutdown())


# ---------------------------------------------------------------- selection


def test_the_voice_is_chosen_by_the_one_rule():
    assert "tts" in STAGES
    soul_doc = soul(tts={"provider": "piper", "model": "en_US-ljspeech-medium"})
    assert tts_selection(manifest(), soul_doc) == {
        "provider": "piper",
        "model": "en_US-ljspeech-medium",
        "key_env": None,
        "params": {},
    }
    assert model_selection(manifest(), soul_doc, "tts") == tts_selection(manifest(), soul_doc)


def test_the_body_takes_the_voice_over_whole_or_not_at_all():
    chosen = tts_selection(
        manifest(tts={"provider": "mock", "params": {"sample_rate": 8000}}),
        soul(tts={"provider": "deepgram", "model": "aura-2-thalia-en", "key_env": "EMET_DEEPGRAM_KEY"}),
    )
    assert chosen == {"provider": "mock", "model": None, "key_env": None, "params": {"sample_rate": 8000}}


def test_the_body_tunes_the_souls_voice():
    chosen = tts_selection(
        manifest(tts={"params": {"voices_dir": "/etc/emet/voices"}}),
        soul(tts={"provider": "piper", "model": "en_US-ljspeech-medium"}),
    )
    assert chosen is not None
    assert chosen["provider"] == "piper" and chosen["params"] == {"voices_dir": "/etc/emet/voices"}


def test_there_is_no_default_voice():
    assert tts_selection(manifest(), soul()) is None
    assert tts_selection(manifest(stt={"provider": "mock"}), soul(chat={"provider": "mock"})) is None


def test_the_reference_soul_names_a_local_voice_that_ships():
    """The design keeps synthesis local by default, and the reference soul
    is what newcomers copy, so it names the local voice, with the model that
    plugin would use unasked."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    chosen = tts_selection(load_yaml(EXAMPLES / "bodiless.yaml"), doc)
    assert chosen is not None and chosen["provider"] == "piper"
    assert chosen["key_env"] is None, "a local voice needs no key"
    cls = PluginRegistry.discover().load_tts("piper")
    assert chosen["model"] == getattr(sys.modules[cls.__module__], "DEFAULT_MODEL")


def test_the_reference_souls_voice_block_carries_the_rate_and_no_engine():
    """`voice.engine` and `voice.model` were the pre-seam way to name a
    voice. `models.tts` is the way now, so the reference soul does not set
    them; the schema still accepts them for older bundles."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    assert doc["voice"]["rate"] == 1.0
    assert "engine" not in doc["voice"] and "model" not in doc["voice"]
    assert validate_soul(doc).ok
    doc["voice"]["engine"] = "piper"
    doc["voice"]["model"] = "en_US-amy-medium"
    assert validate_soul(doc).ok, "older bundles still validate"


# --------------------------------------------------------------- validation


def test_a_body_naming_an_uninstalled_voice_is_an_error():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"tts": {"provider": "gramophone"}}
    report = validate_manifest(doc)
    assert not report.ok
    assert "missing_plugin" in codes(report)
    assert any(f.path == "/models/tts/provider" for f in report.errors)


def test_the_invalid_fixture_says_the_same():
    report = validate_manifest(load_yaml(EXAMPLES / "invalid" / "unknown-tts-provider.yaml"))
    assert not report.ok and "missing_plugin" in codes(report)


def test_a_body_naming_shipped_voices_validates_clean():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    for name in sorted(BUILTIN_TTS):
        doc["models"] = {"tts": {"provider": name, "params": {"x": 1}}}
        assert validate_manifest(doc).ok, name


def test_a_soul_naming_an_uninstalled_voice_still_validates():
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    doc["models"]["tts"] = {"provider": "gramophone"}
    assert validate_soul(doc).ok


def test_the_mocked_example_body_takes_all_three_stages_over():
    doc = load_yaml(EXAMPLES / "mock-scout.yaml")
    assert doc["models"]["tts"]["provider"] == "mock"
    assert validate_manifest(doc).ok
