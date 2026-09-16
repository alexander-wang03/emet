"""The local voice, through Piper.

The design keeps synthesis local by default (`DESIGN.md` section 14): it is
the stage that costs the most per turn in the cloud, and the one a robot in a
home should manage with the network down. Piper is a small neural
text-to-speech engine that runs a voice model through ONNX Runtime on a CPU,
fast enough on a Raspberry Pi 5 to answer before a person notices the wait.

**Packaging, verified against PyPI on 2026-09-15.** `piper-tts` 1.8.0
(2026-09-04) ships `cp39-abi3` wheels for manylinux aarch64 and x86_64, macOS
and Windows, so one wheel serves Python 3.9 through 3.13 on the Pi with no
compiler. It depends on `onnxruntime` (MIT, 1.30.0 ships cp313 aarch64
wheels) and `pathvalidate`; espeak-ng, the phonemiser, is compiled into the
wheel. That is also why the licence is **GPL-3.0-or-later**: the project
moved from the MIT-licensed `rhasspy/piper` to `OHF-Voice/piper1-gpl` when it
took espeak-ng in. Emet does not copy or bundle Piper; this file imports it
through the optional extra `emet-providers[piper]`, and a body without the
extra never loads it. `CITATIONS.md` records the licence.

**Voices are files the owner downloads once.** A voice is a `.onnx` model
and a `.onnx.json` config beside it, from Hugging Face, and the plugin will
not fetch one at boot: a robot that downloaded sixty megabytes because a
soul named a voice would be doing something nobody asked for. It looks in
`params.path`, then `params.voices_dir`, then `$XDG_DATA_HOME/emet/voices`
(`~/.local/share/emet/voices`), then `/etc/emet/voices`, and when it finds
nothing it names the command that fixes it. Each voice carries its own
licence in a `MODEL_CARD` beside the model on Hugging Face; the reference
soul names `en_US-ljspeech-medium` because LJ Speech is public domain.

**Streaming.** Piper synthesises a sentence at a time and the engine hands it
one sentence at a time, so `speak()` yields one chunk per sentence Piper
finds in the text, after each is complete. `describe().streaming` is
therefore False, honestly: the first byte arrives when the sentence does.
Synthesis is CPU work and runs in a thread, so the event loop keeps reading
the microphone meanwhile.

**Warm at boot.** Piper loads espeak-ng on the first sentence it is given,
which on a laptop cost the first reply 3.7 s more than the second (measured
2026-09-15: 3844 ms for the first sentence, 155 ms for the next). So
`start()` says one word into the void after loading the model, and the cost
lands at boot, where a person expects to wait, rather than on the first
answer, where they do not. `params.warm: false` skips it.

Params, all optional, from the body's `models.tts.params`:

    path          the `.onnx` file itself, config beside it as `.onnx.json`.
    voices_dir    a directory holding `<model>.onnx` and `<model>.onnx.json`.
    noise_scale, noise_w_scale, volume
                  Piper's own synthesis knobs, passed through. The soul's
                  `voice.rate` becomes `length_scale` (its inverse: Piper
                  counts phoneme length, a soul counts speed).
    use_cuda      bool, default false. A GPU, where there is one.
    warm          bool, default true. Synthesise one word at boot so the
                  phonemiser is loaded before the first reply.

Dependencies: `piper-tts` (GPL-3.0-or-later) and through it `onnxruntime`
(MIT), both through the `piper` extra.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from emet_sdk.plugin import PluginError, VoicePlugin
from emet_sdk.types import Health, VoiceDescriptor

__all__ = ["PiperVoice", "DEFAULT_MODEL", "SYSTEM_VOICES", "WARM_TEXT", "user_voices_dir", "voice_paths", "download_hint"]

log = logging.getLogger("emet_providers.piper")

#: The voice a soul gets when it says only `provider: piper`. LJ Speech is
#: public domain (the dataset's own terms, read 2026-09-15), which is what a
#: shipped default has to be; several other voices in the same catalogue are
#: fine-tuned from a corpus licensed for research only.
DEFAULT_MODEL = "en_US-ljspeech-medium"

#: The machine's voices, beside the machine's keys.
SYSTEM_VOICES = Path("/etc/emet/voices")

#: Said at boot to load the phonemiser. Never played.
WARM_TEXT = "Ready."


def user_voices_dir() -> Path:
    """A person's voices: `$XDG_DATA_HOME/emet/voices`, or `~/.local/share/emet/voices`."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "emet" / "voices"


def voice_paths(model: str, params: Mapping[str, Any]) -> list[Path]:
    """Where `<model>.onnx` is looked for, in order. `params.path` names the
    file itself and wins outright."""
    explicit = params.get("path")
    if explicit:
        return [Path(str(explicit)).expanduser()]
    dirs: list[Path] = []
    if params.get("voices_dir"):
        dirs.append(Path(str(params["voices_dir"])).expanduser())
    dirs.extend([user_voices_dir(), SYSTEM_VOICES])
    return [d / f"{model}.onnx" for d in dirs]


def download_hint(model: str) -> str:
    """The one command that fetches a voice into the place this plugin looks."""
    return (
        f"python -m piper.download_voices {model} --data-dir {user_voices_dir()}"
    )


class PiperVoice(VoicePlugin):
    """Local synthesis through Piper, one sentence at a time."""

    provider = "piper"

    def __init__(self, config: Mapping[str, Any], voice: Mapping[str, Any] | None = None) -> None:
        super().__init__(config, voice)
        self.model_name: str = self.model or DEFAULT_MODEL
        self._voice: Any = None
        self._synthesis_config: Any = None
        self._sample_rate: int = 22050
        self._started = False
        self._fault: str | None = None
        self._fault_kind: str = "start_failed"
        #: Where the model was found, once it was.
        self.path: Path | None = None
        #: The last failure inside a sentence, for diagnostics. Not a fault.
        self.last_error: str | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            from piper import PiperVoice as _Loader, SynthesisConfig
        except ImportError:
            self._fail(
                "the piper-tts library is not installed. It is an optional "
                "dependency: install `emet-providers[piper]`. Piper is GPL-3.0; "
                "Emet imports it and does not bundle it.",
                kind="no_library",
            )
            return

        candidates = voice_paths(self.model_name, self.params)
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            looked = ", ".join(str(p) for p in candidates)
            self._fail(
                f"no voice model for {self.model_name!r}. Looked for {looked}. "
                f"Download it once with:\n    {download_hint(self.model_name)}\n"
                f"and the plugin finds it there on the next boot. A voice model is "
                f"never fetched at boot, so that a soul naming one cannot make the "
                f"robot download anything unasked.",
                kind="no_model",
            )
            return
        config_path = found.with_name(found.name + ".json")
        if not config_path.is_file():
            self._fail(
                f"{found} has no config beside it: expected {config_path}. Piper "
                f"voices are two files, and the download command fetches both:\n"
                f"    {download_hint(self.model_name)}",
                kind="no_model",
            )
            return

        try:
            # Loading the ONNX session takes a second on a Pi; keep the loop
            # free rather than stall the microphone during boot.
            self._voice = await asyncio.to_thread(
                _Loader.load, str(found), str(config_path), bool(self.params.get("use_cuda", False))
            )
        except Exception as exc:  # noqa: BLE001 - reported through health, not raised
            self._fail(f"piper could not load {found}: {type(exc).__name__}: {exc}")
            return

        knobs: dict[str, Any] = {"length_scale": 1.0 / self.rate}
        for name in ("noise_scale", "noise_w_scale", "volume"):
            if self.params.get(name) is not None:
                knobs[name] = float(self.params[name])
        self._synthesis_config = SynthesisConfig(**knobs)
        self._sample_rate = int(self._voice.config.sample_rate)
        self.path = found

        if self.params.get("warm", True):
            voice, config = self._voice, self._synthesis_config
            try:
                await asyncio.to_thread(lambda: list(voice.synthesize(WARM_TEXT, config)))
            except Exception as exc:  # noqa: BLE001 - a voice that cannot say one word cannot say any
                self._fail(f"piper loaded {found} and could not synthesise: {type(exc).__name__}: {exc}")
                return

        self._started = True
        log.info("piper: ready, voice %s at %d Hz from %s", self.model_name, self._sample_rate, found)

    def _fail(self, reason: str, *, kind: str = "start_failed") -> None:
        self._fault = reason
        self._fault_kind = kind
        log.error("piper: %s", reason)

    async def shutdown(self) -> None:
        self._started = False
        self._voice = None

    # ------------------------------------------------------------ reporting

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(
            provider=self.provider,
            model=self.model_name,
            sample_rate=self._sample_rate,
            streaming=False,
            healthy=self._started,
        )

    def health(self) -> Health:
        if self._fault:
            return Health(ok=False, detail=self._fault, faults=(self._fault_kind,))
        return Health()

    # ---------------------------------------------------------------- speak

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        if not self._started or self._voice is None:
            raise PluginError("the piper voice was not started")

        def synthesise() -> list[bytes]:
            return [
                chunk.audio_int16_bytes
                for chunk in self._voice.synthesize(text, self._synthesis_config)
            ]

        try:
            chunks = await asyncio.to_thread(synthesise)
        except Exception as exc:  # noqa: BLE001 - one sentence lost, reported
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("piper: %s", self.last_error)
            raise PluginError(f"piper could not say {text!r}: {self.last_error}") from exc
        for chunk in chunks:
            if chunk:
                yield chunk
