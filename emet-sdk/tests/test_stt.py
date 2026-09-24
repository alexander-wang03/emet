"""The speech recognition seam.

Built before any provider exists, on purpose. There are credits enough at one
vendor to write the whole of 0.4 against its client library without
noticing, and the seam is what makes that vendor one line of a soul instead
of the shape of the engine.

**Note what this file imports.** Only `emet_sdk`. The one transcriber that
ships lives in `emet_providers`, and it is never named here: it arrives by
discovery, which is the constraint the engine works under, so these tests
fail the way the engine would.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from emet_sdk.discovery import GROUP_STT, PluginRegistry
from emet_sdk.models import stt_selection
from emet_sdk.plugin import TranscriberPlugin
from emet_sdk.types import AudioFormat, Transcript, TranscriberDescriptor
from emet_sdk.validate import MissingPluginError, load_yaml, validate_manifest, validate_soul

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FRAME_BYTES = 2560


def run(coro):
    return asyncio.run(coro)


def frame(payload: bytes = b"") -> bytes:
    return payload + bytes(FRAME_BYTES - len(payload))


def codes(report) -> set[str]:
    return {f.code for f in report.errors}


def manifest(**stt: Any) -> dict:
    doc = {"audio": {"input": {"device": "x"}, "output": {"device": "y"}}}
    if stt:
        doc["models"] = {"stt": stt}
    return doc


def soul(**stt: Any) -> dict:
    doc = {"identity": {"name": "Emet", "wake_word": "hey emet"}}
    if stt:
        doc["models"] = {"stt": stt}
    return doc


class _FakeTranscriber(TranscriberPlugin):
    """Defined here rather than imported from emet_providers: the SDK's own
    tests must not depend on the layer above it."""

    provider = "fake"

    def __init__(self, config, fmt):
        super().__init__(config, fmt)
        self.heard: list[bytes] = []

    def describe(self) -> TranscriberDescriptor:
        return TranscriberDescriptor(
            provider=self.provider,
            model=self.model,
            streaming=False,
            sample_rate=self.format.sample_rate,
        )

    async def feed(self, frame: bytes) -> Transcript | None:
        self.heard.append(frame)
        return None

    async def finish(self) -> Transcript:
        n = len(self.heard)
        self.heard = []
        return Transcript(text=f"{n} frames", final=True)


# ---------------------------------------------------------------- discovery


def test_stt_is_a_real_entry_point_group():
    registry = PluginRegistry.discover()
    assert GROUP_STT == "emet.stt"
    assert "mock" in registry.stt_names, (
        "emet-providers registers a mock transcriber; if this fails the package "
        "needs installing so its entry points are picked up"
    )
    assert registry.has_stt("mock")


def test_stt_appears_in_the_registry_listing():
    assert not PluginRegistry(stt={})
    listed = {group for group, _ in PluginRegistry.discover()}
    assert "stt" in listed


def test_an_uninstalled_provider_is_a_missing_plugin_not_a_schema_error():
    """`provider: whisper` is a legal value the day before anyone packages
    it, exactly as `provider: deepgram` was the day before it shipped."""
    registry = PluginRegistry.discover()
    with pytest.raises(MissingPluginError) as exc:
        registry.load_stt("whisper")
    assert "whisper" in str(exc.value.report)
    assert "speech recognition provider" in str(exc.value.report)


def test_a_transcriber_arrives_without_being_imported():
    """The crux. This module imports `emet_sdk` only, and still ends up
    holding a class defined in `emet_providers`."""
    cls = PluginRegistry.discover().load_stt("mock")
    assert cls.__module__.startswith("emet_providers"), cls.__module__
    assert issubclass(cls, TranscriberPlugin)


def test_the_engine_can_build_and_drive_a_transcriber_it_never_imported():
    """What the listen loop does, minus the loop: a name from the documents,
    a class from the registry, `cls(config, fmt)`, frames in, text out."""
    selection = stt_selection(manifest(), soul(provider="mock"))
    assert selection is not None
    cls = PluginRegistry.discover().load_stt(selection["provider"])
    stt = cls(selection, AudioFormat())

    async def scenario():
        await stt.start()
        assert stt.describe().healthy
        partials = []
        for f in (frame(b"what"), frame(b"time"), frame(), frame(b"is it")):
            if (t := await stt.feed(f)) is not None:
                partials.append(t)
        final = await stt.finish()
        await stt.shutdown()
        return partials, final

    partials, final = run(scenario())
    assert partials and all(not p.final for p in partials)
    assert partials[-1].text == "what time is it"
    assert final.final and final.text == "what time is it"


# ----------------------------------------------------------------- contract


def test_a_batch_transcriber_satisfies_the_same_contract():
    """Returns nothing from `feed()` and does its work in `finish()`. The
    engine above cannot tell it from a streaming one except by latency."""
    stt = _FakeTranscriber({"provider": "fake", "model": "m"}, AudioFormat())

    async def scenario():
        await stt.start()
        results = [await stt.feed(frame()) for _ in range(3)]
        return results, await stt.finish()

    results, final = run(scenario())
    assert results == [None, None, None]
    assert final == Transcript(text="3 frames", final=True)
    assert not stt.describe().streaming
    assert stt.describe().model == "m"


def test_the_format_is_received_not_stated():
    """One microphone feeds the wake engine and the transcriber, so the wake
    engine fixes the format and the transcriber is handed it."""
    fmt = AudioFormat(sample_rate=8000, frame_samples=640)
    stt = _FakeTranscriber({"provider": "fake"}, fmt)
    assert stt.format == fmt
    assert stt.describe().sample_rate == 8000


def test_the_key_is_never_in_the_config_only_its_name():
    stt = _FakeTranscriber(
        {"provider": "fake", "key_env": "EMET_FAKE_KEY", "params": {"region": "eu"}},
        AudioFormat(),
    )
    assert stt.key_env == "EMET_FAKE_KEY"
    assert "key" not in {k.lower() for k in stt.config}
    assert stt.params == {"region": "eu"}


def test_a_transcript_is_partial_by_default_and_bounded():
    assert not Transcript(text="so far").final
    Transcript(text="x", confidence=0.0)
    Transcript(text="x", confidence=1.0)
    with pytest.raises(ValueError):
        Transcript(text="x", confidence=1.2)


def test_a_transcriber_is_a_plugin_with_the_shared_lifecycle():
    from emet_sdk.plugin import Plugin

    stt = _FakeTranscriber({"provider": "fake"}, AudioFormat())
    assert isinstance(stt, Plugin)
    assert stt.health().ok
    run(stt.start())
    run(stt.shutdown())


# ---------------------------------------------------------------- selection


def test_the_soul_chooses_when_the_body_says_nothing():
    chosen = stt_selection(
        manifest(), soul(provider="deepgram", model="nova-2", key_env="EMET_DEEPGRAM_KEY")
    )
    assert chosen == {
        "provider": "deepgram",
        "model": "nova-2",
        "key_env": "EMET_DEEPGRAM_KEY",
        "params": {},
    }


def test_the_body_tunes_the_souls_choice():
    """`models.stt.params` ride along with whichever provider runs."""
    chosen = stt_selection(
        manifest(params={"endpoint": "eu"}), soul(provider="deepgram", model="nova-2")
    )
    assert chosen is not None
    assert chosen["provider"] == "deepgram"
    assert chosen["params"] == {"endpoint": "eu"}


def test_the_body_takes_over_whole_or_not_at_all():
    """A provider from one document with a model and key from the other is a
    broken reference, so a body override replaces all three together."""
    chosen = stt_selection(
        manifest(provider="mock", params={"transcript": "hi"}),
        soul(provider="deepgram", model="nova-2", key_env="EMET_DEEPGRAM_KEY"),
    )
    assert chosen == {"provider": "mock", "model": None, "key_env": None, "params": {"transcript": "hi"}}


def test_a_body_override_may_carry_its_own_model_and_key():
    chosen = stt_selection(
        manifest(provider="whisper", model="small", key_env="EMET_OPENAI_KEY"),
        soul(provider="deepgram"),
    )
    assert chosen is not None
    assert (chosen["provider"], chosen["model"], chosen["key_env"]) == (
        "whisper",
        "small",
        "EMET_OPENAI_KEY",
    )


def test_no_provider_anywhere_means_no_transcription_and_no_default():
    assert stt_selection(manifest(), soul()) is None
    assert stt_selection(manifest(params={"x": 1}), soul()) is None
    assert stt_selection({}, {}) is None


def test_the_reference_soul_names_a_provider_that_ships():
    """The reference soul is what newcomers copy, so it names a real
    provider, and that provider is installed. Its model is the one the plugin
    would use unasked, so the example and the default cannot drift apart."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    chosen = stt_selection(load_yaml(EXAMPLES / "bodiless.yaml"), doc)
    assert chosen is not None and chosen["provider"] == "deepgram"
    assert chosen["key_env"] == "EMET_DEEPGRAM_KEY"
    registry = PluginRegistry.discover()
    assert registry.has_stt("deepgram")
    cls = registry.load_stt("deepgram")
    assert cls.__module__.startswith("emet_providers")
    assert chosen["model"] == getattr(sys.modules[cls.__module__], "DEFAULT_MODEL")


# --------------------------------------------------------------- validation


def test_an_absent_body_block_is_the_common_case():
    report = validate_manifest(load_yaml(EXAMPLES / "bodiless.yaml"))
    assert report.ok, report.errors
    assert "models" not in load_yaml(EXAMPLES / "bodiless.yaml")


def test_a_body_naming_an_uninstalled_provider_is_an_error():
    """Like `wake.engine`, `input.source` and `output.sink`: a body is one
    machine, and it has named software that has to be installed on it."""
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"stt": {"provider": "carrier_pigeon"}}
    report = validate_manifest(doc)
    assert not report.ok
    assert "missing_plugin" in codes(report)
    assert any(f.path == "/models/stt/provider" for f in report.errors)


def test_the_invalid_fixture_says_the_same():
    report = validate_manifest(load_yaml(EXAMPLES / "invalid" / "unknown-stt-provider.yaml"))
    assert not report.ok
    assert "missing_plugin" in codes(report)


def test_a_body_naming_the_shipped_provider_validates_clean():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"stt": {"provider": "mock", "params": {"transcript": "hello"}}}
    assert validate_manifest(doc).ok


def test_the_mocked_example_body_takes_speech_recognition_over():
    doc = load_yaml(EXAMPLES / "mock-scout.yaml")
    assert doc["models"]["stt"]["provider"] == "mock"
    assert validate_manifest(doc).ok


def test_a_body_may_carry_params_alone():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"stt": {"params": {"endpoint": "eu"}}}
    assert validate_manifest(doc).ok


def test_an_unknown_key_under_the_body_block_is_a_schema_error():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"stt": {"provider": "mock", "api_key": "never-in-a-manifest"}}
    report = validate_manifest(doc)
    assert "schema" in codes(report)


def test_a_soul_naming_an_uninstalled_provider_still_validates():
    """A soul is valid on every machine or on none. Whether its provider is
    installed is a question for boot, like whether its wake phrase can be
    heard."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    doc["models"]["stt"] = {"provider": "nobody_ships_this", "key_env": "X"}
    assert validate_soul(doc).ok


# ------------------------------------------------- a transcript that broke


def test_a_transcript_says_nothing_broke_by_default():
    assert Transcript(text="hello", final=True).error is None


def test_a_final_can_say_why_the_words_are_missing():
    """An empty final and a dead service are the same empty string. The
    engine has to tell them apart to answer one and not the other."""
    broke = Transcript(text="", final=True, error="gaierror: name resolution failed")
    quiet = Transcript(text="", final=True)
    assert broke.text == quiet.text
    assert broke.error and quiet.error is None


def test_the_words_heard_before_a_break_travel_with_the_reason():
    cut = Transcript(text="what time is", final=True, error="no final within 5 s of CloseStream")
    assert cut.text == "what time is" and "CloseStream" in cut.error
