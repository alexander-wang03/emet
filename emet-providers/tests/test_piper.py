"""The Piper voice, against a stand-in for the Piper library.

No model on disk, no ONNX Runtime, no GPL code imported. A fake `piper`
module is put in `sys.modules` with the two names the plugin uses,
`PiperVoice.load` and `SynthesisConfig`, shaped as `piper-tts` 1.8.0 has
them (read from the wheel, 2026-09-15). What these tests prove is the
plugin's half: where it looks for a voice, what it says when there is none,
how the soul's rate becomes Piper's length scale, and that a sentence comes
back as int16 audio at the model's rate. A run against the real library is
by hand, on a machine that has it.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from emet_sdk.plugin import PluginError, VoicePlugin

from emet_providers import piper as plugin
from emet_providers.piper import DEFAULT_MODEL, WARM_TEXT, PiperVoice, download_hint, voice_paths


def run(coro):
    return asyncio.run(coro)


async def collect(chunks) -> list[bytes]:
    return [c async for c in chunks]


# -------------------------------------------------------------- the fake


class _Chunk:
    def __init__(self, text: str, rate: int) -> None:
        self.sample_rate = rate
        self.sample_width = 2
        self.sample_channels = 1
        self.audio_int16_bytes = text.encode("ascii", "replace") + bytes(2)


class _FakeVoice:
    """Shaped like `piper.PiperVoice`: `config.sample_rate`, `synthesize()`
    yielding one chunk per sentence."""

    loaded: list[tuple[str, str | None, bool]] = []
    rate = 22050
    fail_on: str | None = None

    def __init__(self) -> None:
        self.config = types.SimpleNamespace(sample_rate=self.rate)
        self.calls: list[tuple[str, Any]] = []

    @staticmethod
    def load(model_path, config_path=None, use_cuda=False, **_):
        _FakeVoice.loaded.append((str(model_path), str(config_path) if config_path else None, use_cuda))
        return _FakeVoice()

    def synthesize(self, text: str, syn_config=None, include_alignments=False):
        self.calls.append((text, syn_config))
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("phonemizer choked")
        for sentence in [s for s in text.replace("?", ".").replace("!", ".").split(".") if s.strip()]:
            yield _Chunk(sentence.strip(), self.rate)


class _SynthesisConfig:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw


def fake_piper(monkeypatch, *, rate: int = 22050, fail_on: str | None = None) -> types.ModuleType:
    module = types.ModuleType("piper")
    _FakeVoice.loaded = []
    _FakeVoice.rate = rate
    _FakeVoice.fail_on = fail_on
    # A fresh subclass per test, so a test that breaks `load` breaks its own.
    module.PiperVoice = type("PiperVoice", (_FakeVoice,), {})  # type: ignore[attr-defined]
    module.SynthesisConfig = _SynthesisConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "piper", module)
    return module


def voice_files(directory: Path, model: str = DEFAULT_MODEL) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    onnx = directory / f"{model}.onnx"
    onnx.write_bytes(b"not really a model")
    (directory / f"{model}.onnx.json").write_text('{"audio": {"sample_rate": 22050}}', encoding="utf-8")
    return onnx


def make(voices_dir: Path | None = None, *, config: dict | None = None, voice: dict | None = None, **params) -> PiperVoice:
    if voices_dir is not None:
        params["voices_dir"] = str(voices_dir)
    cfg: dict[str, Any] = {"provider": "piper", "params": params}
    cfg.update(config or {})
    return PiperVoice(cfg, voice)


def started(monkeypatch, tmp_path, **kw) -> PiperVoice:
    fake_piper(monkeypatch)
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices", **kw)
    run(tts.start())
    assert tts.describe().healthy, tts.health().detail
    return tts


# ---------------------------------------------------------------- lookup


def test_it_is_a_voice_plugin_registered_as_piper():
    tts = make()
    assert isinstance(tts, VoicePlugin)
    assert tts.provider == "piper"
    assert tts.model_name == DEFAULT_MODEL


def test_the_default_voice_is_public_domain_ljspeech():
    """The reference voice has to be one a shipped default may be. Amy, the
    earlier choice, is fine-tuned from a corpus licensed for research only."""
    assert DEFAULT_MODEL == "en_US-ljspeech-medium"


def test_voices_are_looked_for_in_the_documented_places(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    looked = voice_paths("en_US-ljspeech-medium", {})
    assert looked == [
        tmp_path / "xdg" / "emet" / "voices" / "en_US-ljspeech-medium.onnx",
        Path("/etc/emet/voices/en_US-ljspeech-medium.onnx"),
    ]
    with_dir = voice_paths("v", {"voices_dir": str(tmp_path / "mine")})
    assert with_dir[0] == tmp_path / "mine" / "v.onnx"
    assert voice_paths("v", {"path": str(tmp_path / "x.onnx")}) == [tmp_path / "x.onnx"]


def test_a_missing_model_is_unhealthy_and_names_the_download_command(monkeypatch, tmp_path):
    fake_piper(monkeypatch)
    tts = make(tmp_path / "empty")
    run(tts.start())
    assert not tts.describe().healthy
    detail = tts.health().detail or ""
    assert "no voice model" in detail
    assert DEFAULT_MODEL in detail
    assert download_hint(DEFAULT_MODEL) in detail
    assert "piper.download_voices" in detail
    assert "never fetched at boot" in detail
    assert tts.health().faults == ("no_model",)
    assert _FakeVoice.loaded == [], "nothing was loaded"


def test_a_model_without_its_config_says_so(monkeypatch, tmp_path):
    fake_piper(monkeypatch)
    directory = tmp_path / "voices"
    directory.mkdir()
    (directory / f"{DEFAULT_MODEL}.onnx").write_bytes(b"model")
    tts = make(directory)
    run(tts.start())
    assert not tts.describe().healthy
    assert ".onnx.json" in (tts.health().detail or "")


def test_without_the_library_the_hint_names_the_extra_and_the_licence(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "piper", None)  # import raises ImportError
    tts = make(tmp_path)
    run(tts.start())
    assert not tts.describe().healthy
    detail = tts.health().detail or ""
    assert "emet-providers[piper]" in detail
    assert "GPL" in detail
    assert tts.health().faults == ("no_library",)


def test_the_library_is_checked_before_the_disk(monkeypatch, tmp_path):
    """An owner without the extra gets the install hint, whatever is on disk."""
    monkeypatch.setitem(sys.modules, "piper", None)
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices")
    run(tts.start())
    assert "emet-providers[piper]" in (tts.health().detail or "")


# ----------------------------------------------------------------- loading


def test_the_model_and_its_config_are_loaded_from_where_they_were_found(monkeypatch, tmp_path):
    tts = started(monkeypatch, tmp_path)
    onnx = tmp_path / "voices" / f"{DEFAULT_MODEL}.onnx"
    assert _FakeVoice.loaded == [(str(onnx), str(onnx) + ".json", False)]
    assert tts.path == onnx


def test_an_explicit_path_wins(monkeypatch, tmp_path):
    fake_piper(monkeypatch)
    onnx = voice_files(tmp_path / "elsewhere", "en_GB-alan-low")
    tts = make(path=str(onnx))
    run(tts.start())
    assert tts.describe().healthy
    assert tts.path == onnx
    assert tts.model_name == DEFAULT_MODEL, "the name is for the report; the file is what was loaded"


def test_the_souls_model_is_looked_for_and_reported(monkeypatch, tmp_path):
    fake_piper(monkeypatch)
    voice_files(tmp_path / "voices", "en_US-ryan-high")
    tts = make(tmp_path / "voices", config={"model": "en_US-ryan-high"})
    run(tts.start())
    assert tts.describe().healthy and tts.describe().model == "en_US-ryan-high"


def test_describe_reports_the_models_own_sample_rate(monkeypatch, tmp_path):
    fake_piper(monkeypatch, rate=16000)
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices")
    run(tts.start())
    d = tts.describe()
    assert d.sample_rate == 16000
    assert not d.streaming, "a sentence arrives whole"
    assert d.provider == "piper"


def test_cuda_is_off_unless_asked(monkeypatch, tmp_path):
    fake_piper(monkeypatch)
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices", use_cuda=True)
    run(tts.start())
    assert _FakeVoice.loaded[0][2] is True


def test_a_model_that_will_not_load_is_unhealthy_with_the_reason(monkeypatch, tmp_path):
    module = fake_piper(monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("bad protobuf")

    module.PiperVoice.load = staticmethod(explode)  # type: ignore[attr-defined]
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices")
    run(tts.start())
    assert not tts.describe().healthy
    assert "bad protobuf" in (tts.health().detail or "")


# ----------------------------------------------------------------- speaking


def test_a_sentence_comes_back_as_audio_one_chunk_per_sentence(monkeypatch, tmp_path):
    tts = started(monkeypatch, tmp_path)
    chunks = run(collect(tts.speak("What time is it? It is noon.")))
    assert chunks == [b"What time is it\x00\x00", b"It is noon\x00\x00"]


def test_the_phonemiser_is_warmed_at_boot_and_can_be_left_cold(monkeypatch, tmp_path):
    """The first sentence through a cold Piper cost 3.7 s more than the
    second on the laptop (2026-09-15); that wait belongs at boot."""
    tts = started(monkeypatch, tmp_path)
    assert [text for text, _ in tts._voice.calls] == [WARM_TEXT]
    fake_piper(monkeypatch)
    cold = make(tmp_path / "voices", warm=False)
    run(cold.start())
    assert cold.describe().healthy and cold._voice.calls == []


def test_a_voice_that_cannot_say_the_warm_word_is_unhealthy(monkeypatch, tmp_path):
    fake_piper(monkeypatch, fail_on=WARM_TEXT)
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices")
    run(tts.start())
    assert not tts.describe().healthy
    assert "could not synthesise" in (tts.health().detail or "")


def test_blank_text_is_not_synthesised(monkeypatch, tmp_path):
    tts = started(monkeypatch, tmp_path, warm=False)
    assert run(collect(tts.speak("  \n"))) == []
    assert tts._voice.calls == []


def test_the_souls_rate_becomes_the_inverse_length_scale(monkeypatch, tmp_path):
    """Piper counts phoneme length, so faster is a smaller number."""
    tts = started(monkeypatch, tmp_path, voice={"rate": 1.25}, warm=False)
    run(collect(tts.speak("Hello.")))
    (_, config), = tts._voice.calls
    assert config.kw["length_scale"] == pytest.approx(0.8)


def test_the_bodys_knobs_ride_along(monkeypatch, tmp_path):
    tts = started(monkeypatch, tmp_path, noise_scale=0.5, volume=0.8, warm=False)
    run(collect(tts.speak("Hello.")))
    (_, config), = tts._voice.calls
    assert config.kw == {"length_scale": 1.0, "noise_scale": 0.5, "volume": 0.8}


def test_a_sentence_the_engine_chokes_on_is_one_sentence_lost(monkeypatch, tmp_path):
    fake_piper(monkeypatch, fail_on="Ω")
    voice_files(tmp_path / "voices")
    tts = make(tmp_path / "voices")
    run(tts.start())
    with pytest.raises(PluginError, match="phonemizer choked"):
        run(collect(tts.speak("The symbol Ω.")))
    assert tts.last_error and "phonemizer choked" in tts.last_error
    assert tts.describe().healthy, "a bad sentence is not a broken plugin"
    assert run(collect(tts.speak("Fine again."))) == [b"Fine again\x00\x00"]


def test_speaking_before_start_is_an_error():
    tts = make()

    async def scenario():
        with pytest.raises(PluginError, match="not started"):
            await collect(tts.speak("Hello."))

    run(scenario())


def test_shutdown_forgets_the_model(monkeypatch, tmp_path):
    tts = started(monkeypatch, tmp_path)
    run(tts.shutdown())
    assert not tts.describe().healthy
    assert tts._voice is None


def test_the_module_names_its_licence_and_packaging_facts():
    """The docstring is where the verification lives; a future reader should
    find the date and the licence without leaving the file."""
    doc = plugin.__doc__ or ""
    assert "GPL-3.0-or-later" in doc
    assert "2026-09-15" in doc
    assert "abi3" in doc and "aarch64" in doc


def test_each_sentence_leaves_the_plugin_as_its_own_inference_finishes(monkeypatch, tmp_path):
    """Espeak decides where a sentence ends and does not always agree with
    the engine's splitter, so one call can still produce several chunks.
    Draining the generator into a list first held the first chunk until the
    last was made: 59 to 112 ms on the laptop, measured 2026-09-20."""
    tts = started(monkeypatch, tmp_path)
    order: list[str] = []
    real = tts._voice.synthesize

    def watched(text, syn_config=None, include_alignments=False):
        for chunk in real(text, syn_config):
            order.append("made")
            yield chunk

    tts._voice.synthesize = watched

    async def scenario():
        out = []
        async for chunk in tts.speak("What time is it? It is noon."):
            order.append("heard")
            out.append(chunk)
        return out

    chunks = run(scenario())
    assert len(chunks) == 2
    assert order == ["made", "heard", "made", "heard"], (
        "the first chunk waited for the last inference"
    )


def test_a_failure_after_the_first_sentence_keeps_what_was_already_made(monkeypatch, tmp_path):
    """Stepping the generator changes this, so it is written down: the audio
    already produced has left the plugin when the failure arrives, and the
    engine keeps what was heard rather than losing the whole call."""
    tts = started(monkeypatch, tmp_path)

    def breaks_on_the_second(text, syn_config=None, include_alignments=False):
        yield _Chunk("first", 22050)
        raise RuntimeError("phonemizer choked")

    tts._voice.synthesize = breaks_on_the_second

    async def scenario():
        out = []
        with pytest.raises(PluginError, match="phonemizer choked"):
            async for chunk in tts.speak("First one. Second one."):
                out.append(chunk)
        return out

    assert run(scenario()) == [b"first" + bytes(2)]
