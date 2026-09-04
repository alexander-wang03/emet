"""How the engine gets a microphone.

`emet_engine` may import `emet_sdk` and nothing else. Microphones live in
`emet_hal`. Those two facts are only compatible because audio crosses the
boundary the same way plugins do: the engine holds a *name* from the manifest,
asks the registry for it, and receives a class it never imported.

**Note what this file imports.** Only `emet_sdk`. Everything it exercises is
implemented in `emet_hal`, and none of it is named here — which is exactly the
constraint the engine works under, so these tests fail the way the engine
would.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from emet_sdk.discovery import GROUP_AUDIO, GROUP_AUDIO_OUT, PluginRegistry
from emet_sdk.types import AudioFormat, AudioSink, AudioSource
from emet_sdk.validate import MissingPluginError, load_yaml, validate_manifest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run(coro):
    return asyncio.run(coro)


def write_wav(path: Path, frames: int) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * frames)
    return path


def codes(report) -> set[str]:
    return {f.code for f in report.errors}


# ---------------------------------------------------------------- discovery


def test_audio_is_a_real_entry_point_group():
    registry = PluginRegistry.discover()
    assert GROUP_AUDIO == "emet.audio"
    assert "microphone" in registry.audio_names
    assert "wav" in registry.audio_names
    assert registry.has_audio("microphone")


def test_audio_appears_in_the_registry_listing():
    listed = {group for group, _ in PluginRegistry.discover()}
    assert "audio" in listed


def test_an_uninstalled_source_is_a_missing_plugin_not_a_schema_error():
    registry = PluginRegistry.discover()
    with pytest.raises(MissingPluginError) as exc:
        registry.load_audio("some_network_stream")
    assert "some_network_stream" in str(exc.value.report)


# ------------------------------------------------- the constraint, exercised


def test_a_source_arrives_without_being_imported():
    """The crux. This module imports `emet_sdk` only, and still ends up
    holding a class defined in `emet_hal`."""
    cls = PluginRegistry.discover().load_audio("microphone")
    assert cls.__module__.startswith("emet_hal"), cls.__module__


def test_the_engine_can_build_and_drain_a_source_it_never_imported(tmp_path):
    """What the listen loop will do, minus the loop.

    Take a name from a manifest, ask the registry for the class, construct it
    with the `audio.input` block, and read frames. No import of `emet_hal`
    anywhere above.
    """
    manifest_audio_input = {
        "source": "wav",
        "params": {"path": str(write_wav(tmp_path / "quiet.wav", 1280 * 3))},
    }

    registry = PluginRegistry.discover()
    cls = registry.load_audio(manifest_audio_input["source"])
    source = cls(manifest_audio_input, AudioFormat())

    assert isinstance(source, AudioSource)

    async def scenario():
        await source.start()
        frames = []
        while (frame := await source.read()) is not None:
            frames.append(frame)
        await source.stop()
        return frames

    frames = run(scenario())
    assert len(frames) == 3
    assert all(len(f) == AudioFormat().frame_bytes for f in frames)


def test_every_shipped_source_takes_the_documented_constructor():
    """`cls(config, fmt)` is the contract. A source that takes anything else
    cannot be built by a caller that only has a name and a mapping."""
    registry = PluginRegistry.discover()
    for name in registry.audio_names:
        cls = registry.load_audio(name)
        instance = cls({"source": name}, AudioFormat())
        assert isinstance(instance, AudioSource), name
        assert instance.format.sample_rate == 16000, name


# ---------------------------------------------------------------- validation


def test_an_absent_source_is_the_common_case():
    """Most manifests say nothing and get a live microphone."""
    report = validate_manifest(load_yaml(EXAMPLES / "bodiless.yaml"))
    assert report.ok, report.errors
    assert "source" not in (load_yaml(EXAMPLES / "bodiless.yaml")["audio"]["input"])


def test_an_unresolvable_source_is_an_error():
    """Like `kinematics` and `wake.engine`, unlike `driver.plugin`. A body
    whose audio source does not resolve produces no frames at all, which takes
    the wake engine and every voice rung down with it."""
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["audio"]["input"]["source"] = "nobody_ships_this"
    report = validate_manifest(doc)
    assert not report.ok
    assert "missing_plugin" in codes(report)


def test_a_shipped_source_validates_clean():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["audio"]["input"]["source"] = "microphone"
    assert validate_manifest(doc).ok


# ------------------------------------------------------- the other direction


def test_audio_out_is_its_own_entry_point_group():
    """Separate from `emet.audio` because audio is pushed into a sink and
    pulled from a source, and because the two are chosen independently: read a
    recording, play to a real speaker."""
    registry = PluginRegistry.discover()
    assert GROUP_AUDIO_OUT == "emet.audio_out"
    assert set(registry.audio_out_names) >= {"speaker", "wav", "null"}
    assert registry.has_audio_out("null")


def test_a_name_can_mean_different_things_in_each_direction():
    """`wav` reads in one group and writes in the other. Sharing a namespace
    would have made that impossible to express."""
    registry = PluginRegistry.discover()
    assert registry.load_audio("wav") is not registry.load_audio_out("wav")


def test_both_directions_appear_in_the_registry_listing():
    listed = {group for group, _ in PluginRegistry.discover()}
    assert {"audio", "audio_out"} <= listed


def test_an_uninstalled_sink_is_a_missing_plugin():
    with pytest.raises(MissingPluginError):
        PluginRegistry.discover().load_audio_out("a_tin_can_and_string")


def test_the_engine_can_build_a_sink_it_never_imported():
    registry = PluginRegistry.discover()
    cls = registry.load_audio_out("null")
    sink = cls({"sink": "null"}, AudioFormat(sample_rate=22050))
    assert isinstance(sink, AudioSink)
    assert cls.__module__.startswith("emet_hal")


def test_every_shipped_sink_takes_the_documented_constructor():
    registry = PluginRegistry.discover()
    for name in registry.audio_out_names:
        cls = registry.load_audio_out(name)
        assert isinstance(cls({"sink": name}, AudioFormat()), AudioSink), name


def test_an_unresolvable_sink_is_an_error():
    """A body that cannot play audio has no voice rung, and every chain in
    Emet terminates in one."""
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["audio"]["output"]["sink"] = "nobody_ships_this"
    report = validate_manifest(doc)
    assert not report.ok
    assert "missing_plugin" in codes(report)


def test_an_absent_sink_is_the_common_case():
    assert validate_manifest(load_yaml(EXAMPLES / "bodiless.yaml")).ok
