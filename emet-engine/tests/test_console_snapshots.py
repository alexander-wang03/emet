"""Console snapshots of `emet-listen` and `emet-talk` (RP11, stage 1).

One test a failure state: each way the two commands refuse or fail with a
message a builder reads, reached through the mock path only: a wav file for
the microphone, the null sink, the mock wake engine and the mock providers,
from documents written to a temporary directory and read back by the real
loaders. Nothing opens a sound card, reads a real `keys.env` (this
package's `conftest.py` empties the default places) or finds a real Piper
voice (the cases that name Piper hide its library, or stand a stub in for
it with the voice places under the test's own directory). Nothing reaches
a vendor: no case holds a real key, every connection the event loop is
asked to open is refused, and the two cases that need an answer from
OpenAI get it from `httpx.MockTransport`.

The snapshot is the whole of what a person sees: the command, the exit
code, everything on stdout and everything on stderr. The plugins' log
records are part of it. The commands configure no logging, so a terminal
shows a warning through Python's last-resort handler on stderr; each run
here clears pytest's handlers for its length so the same lines reach
stderr. Only what differs by machine is replaced: Windows' line ends
become line feeds, the temporary directory becomes `<tmp>`, a Windows backslash
between two parts of a path becomes `/`, the four versions and the
revision on the identity line become `<version>` and `<revision>`, and
argparse's usage lines become one line that says so. One log line is
dropped: asyncio's slow-callback warning, which `python -X dev` turns on
and whose timing is the machine's.

The registry is fixed (`registry()` below, the names the four packages
ship, held to their `pyproject.toml` files by a test), so a missing-plugin
message lists the same plugins on every machine. Each entry point comes from
a distribution the test writes in memory, so a boot line's `from
emet-providers ...` names the package as discovery would, and the
normaliser replaces the version after it.

**The failure table.** Every row of `emet_engine.failures` is reached in
both programs by at least one fake (`FAKES`, keyed by program and row): a
fake transport behind the real vendor plugin where a vendor can raise the
row, the mocks where only the engine can. Each fake is one snapshot,
`snapshots/{listen|talk}-{row}-{vendor}.txt`, and its run is also held to
the row: the row's line is on the stream the row names, and the line the
row has the robot say follows the speaker's tag (`emet-talk`) or `spoke the
line: ` (`emet-listen`, run with `--reply --speak` so both are printed).

A mismatch prints a diff and the command that rewrites the snapshot.
`EMET_UPDATE_SNAPSHOTS=1` is read by these tests only. A case it rewrites
is reported as skipped, never as passed, and under CI (`CI` or
`GITHUB_ACTIONS` set) it fails instead. The list of failure states, with
where each is raised and the states no test reaches, is in
`releases/v0.5.6/reports/B7.md` outside the repository.
"""

from __future__ import annotations

import asyncio
import asyncio.base_events
import difflib
import importlib
import json
import logging
import os
import re
import string
import subprocess
import sys
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import pytest
import yaml

from emet_sdk.discovery import (
    GROUP_ACTUATOR,
    GROUP_AUDIO,
    GROUP_AUDIO_OUT,
    GROUP_LLM,
    GROUP_LOCOMOTION,
    GROUP_SENSOR,
    GROUP_STT,
    GROUP_TTS,
    GROUP_WAKE,
    PluginRegistry,
)
from emet_sdk.types import ReplyDone

from emet_engine import cli, keys, talk
from emet_engine import session as session_module
from emet_engine.failures import FAILURES, PROGRAMS, row
from emet_engine.prompting import DEFAULT_MAX_TOKENS, persona_lines
from emet_engine.session import ListenSession

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE / "snapshots"
FILE = "tests/test_console_snapshots.py"
UPDATE = "EMET_UPDATE_SNAPSHOTS"

PHRASE = "hey emet"
FRAME = 1280  # samples a frame at 16 kHz, the mock wake engine's 80 ms

#: What the four packages register, by group, as their `pyproject.toml`
#: files say; `test_the_shipped_plugins_are_the_ones_the_pyproject_files_register`
#: holds the copy to them.
SHIPPED: dict[str, dict[str, str]] = {
    GROUP_ACTUATOR: {"emet_hal.mock": "emet_hal.mock:MockActuator"},
    GROUP_SENSOR: {"emet_hal.mock_sensor": "emet_hal.mock:MockSensor"},
    GROUP_LOCOMOTION: {"differential": "emet_hal.differential:DifferentialDrive",
                       "tracked": "emet_hal.tracked:TrackedDrive"},
    GROUP_WAKE: {"pocketsphinx": "emet_hal.pocketsphinx_wake:PocketSphinxWake", "mock": "emet_hal.mock:MockWake"},
    GROUP_AUDIO: {"microphone": "emet_hal.audio:MicrophoneSource", "wav": "emet_hal.audio:WavSource"},
    GROUP_AUDIO_OUT: {"speaker": "emet_hal.audio:Speaker", "wav": "emet_hal.audio:WavSink",
                      "null": "emet_hal.audio:NullSink"},
    GROUP_STT: {"mock": "emet_providers.mock:MockTranscriber", "deepgram": "emet_providers.deepgram:DeepgramTranscriber"},
    GROUP_LLM: {"mock": "emet_providers.mock:MockLanguageModel",
                "anthropic": "emet_providers.anthropic:AnthropicLanguageModel",
                "openai": "emet_providers.openai:OpenAILanguageModel"},
    GROUP_TTS: {"mock": "emet_providers.mock:MockVoice", "piper": "emet_providers.piper:PiperVoice",
                "deepgram": "emet_providers.deepgram_voice:DeepgramVoice"},
}


class Written(metadata.Distribution):
    """A distribution held in memory: its name, its version and its entry
    points, read as discovery reads an installed one."""

    def __init__(self, name: str, version: str, entries: dict[str, dict[str, str]]) -> None:
        self._files = {
            "METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            "entry_points.txt": "".join(
                f"[{group}]\n" + "".join(f"{n} = {v}\n" for n, v in names.items()) for group, names in entries.items()
            ),
        }

    def read_text(self, filename: str) -> str | None:
        return self._files.get(filename)

    def locate_file(self, path: Any) -> Path:
        return Path(str(path))


def _shipped_by(distribution: str) -> dict[str, dict[str, str]]:
    module = distribution.replace("-", "_") + "."
    return {g: {n: v for n, v in names.items() if v.startswith(module)} for g, names in SHIPPED.items()}


#: A second distribution that offers `openai` in `emet.llm`, as a fork
#: installed beside `emet-providers` would.
FORK = Written("emet-openai-fork", "0.1.0", {GROUP_LLM: {"openai": "emet_openai_fork:Model"}})


def registry(*extra: metadata.Distribution) -> PluginRegistry:
    """The plugins the four packages ship, and nothing a machine adds, each
    from the package that ships it (and any `extra` distribution after
    them)."""
    shipped = [Written(name, cli._version(name), _shipped_by(name)) for name in ("emet-hal", "emet-providers")]
    eps = [ep for dist in (*shipped, *extra) for ep in dist.entry_points]
    return PluginRegistry.from_entry_points(eps)


# ------------------------------------------------------------- snapshots


def normalise(text: str, tmp: Path) -> str:
    """What differs by machine, replaced: the temporary directory, as
    written and as a repr writes it; Windows' separator inside a path; the
    versions and revision on the identity line, and any other version of
    one of the four packages; and argparse's usage."""
    text = text.replace("\r\n", "\n")
    for form in (repr(str(tmp))[1:-1], str(tmp), tmp.as_posix()):
        text = text.replace(form, "<tmp>")
    if os.sep == "\\":
        text = re.sub(r"(?<=[\w.>])\\{1,2}(?=[\w.<])", "/", text)
    text = re.sub(r"(?m)^(emet-listen|emet-talk): .*; revision .*$",
                  lambda m: re.sub(r"(emet-(?:sdk|hal|providers|engine)) [^,;\s]+", r"\1 <version>",
                                   m.group(0).split("; revision ")[0]) + "; revision <revision>", text)
    # One of the four packages named with its version anywhere else: a
    # boot line names the package a plugin came from, and the duplicate
    # refusal names the two that offer one name.
    text = re.sub(r"\b(emet-(?:sdk|hal|providers|engine)) \d+\.\d+[^\s,;)]*", r"\1 <version>", text)
    return re.sub(r"(?ms)^usage: .*?(?=^[\w .-]+: error: )",
                  "usage: (argparse's usage lines, which differ by Python version)\n", text)


def block(text: str) -> str:
    if not text:
        return "(nothing)\n"
    return text if text.endswith("\n") else text + "\n"


def render(program: str, argv: list[str], code: Any, out: str, err: str) -> str:
    return f"$ {program} {' '.join(argv)}\nexit {code}\n--- stdout\n{block(out)}--- stderr\n{block(err)}"


def compare(name: str, actual: str) -> None:
    """Hold `actual` to `snapshots/<name>.txt`, or write it under
    `EMET_UPDATE_SNAPSHOTS=1`."""
    path = SNAPSHOTS / f"{name}.txt"
    # The closing bracket of the test id, so `-k` picks this case alone and
    # not every case whose name starts with this one.
    command = f"{UPDATE}=1 python -m pytest {FILE} -k '{name}]'"
    if os.environ.get(UPDATE) == "1":
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail(f"{UPDATE}=1 under CI: a snapshot is rewritten on purpose by a person, and CI only "
                        f"compares. Unset it.", pytrace=False)
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        # A skip, so a run with the variable left exported cannot pass for a green one.
        pytest.skip(f"rewrote snapshots/{name}.txt; read the diff before committing it")
    if not path.exists():
        pytest.fail(f"no snapshot at {path.name}. Write it, read it, and commit it:\n    {command}", pytrace=False)
    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if actual != expected:
        diff = "".join(difflib.unified_diff(
            expected.splitlines(keepends=True), actual.splitlines(keepends=True),
            fromfile=f"snapshots/{name}.txt", tofile="what the command printed"))
        pytest.fail(f"the console changed for {name}:\n{diff}\nIf the change is meant, rewrite the snapshot "
                    f"and review the diff:\n    {command}", pytrace=False)


@contextmanager
def terminal_logging():
    """Logging as a terminal has it: no handler, so a warning goes to
    stderr through `logging.lastResort`, which capsys captures."""
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    asyncio_log = logging.getLogger("asyncio")
    asyncio_log.addFilter(_no_slow_callbacks)
    try:
        yield
    finally:
        asyncio_log.removeFilter(_no_slow_callbacks)
        root.handlers[:] = saved


def _no_slow_callbacks(record: logging.LogRecord) -> bool:
    """`python -X dev` runs asyncio in debug mode, which logs a callback over
    0.1 s as `Executing <Task ...> took N seconds` (CPython 3.13.2,
    `asyncio/base_events.py` lines 437 and 2035, read 2026-11-26). How long
    a callback takes belongs to the machine, so that line stays out of a
    snapshot. Every other asyncio record stays in."""
    return not str(record.msg).startswith("Executing %s took")


# ------------------------------------------------------------- documents


def wav(pcm: bytes, *, rate: int = 16000, width: int = 2) -> bytes:
    """A mono wav file's bytes, 16-bit at 16 kHz unless told otherwise."""
    import io

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def frame(payload: bytes = b"") -> bytes:
    return payload + bytes(2 * FRAME - len(payload))


#: Three frames with nothing in them: a run that boots, hears nothing, ends.
QUIET = wav(frame() * 3)
#: The name, two words the mock transcriber reads, and the silence after.
SPOKEN = wav(frame() + frame(PHRASE.encode()) + frame(b"hello") + frame(b"there") + frame() * 20)


def body(*, models: dict | None = None, wake: dict | None = None, capabilities: list | None = None,
         audio: dict | None = None) -> str:
    doc: dict[str, Any] = {
        "manifest_version": "0.1",
        "body": {"id": "probe", "name": "a speakerphone on a desk", "scale": "desk", "power": "plugged_in"},
        "audio": audio or {
            "input": {"source": "wav", "device": "file", "params": {"path": "take.wav"}},
            "output": {"sink": "null", "device": "none"},
            "wake": wake or {"engine": "mock"},
        },
        "capabilities": capabilities or [],
    }
    if models:
        doc["models"] = models
    return yaml.safe_dump(doc, sort_keys=False)


def soul(*, models: dict | None = None, lines: dict | None = None) -> str:
    doc: dict[str, Any] = {"bundle_version": "0.1", "identity": {"name": "Emet", "wake_word": PHRASE, "line": "emet"}}
    if lines:
        doc["persona"] = {"lines": lines}
    if models:
        doc["models"] = models
    return yaml.safe_dump(doc, sort_keys=False)


MOCKS = {"stt": {"provider": "mock"}, "chat": {"provider": "mock"}, "tts": {"provider": "mock"}}
STT = {"stt": {"provider": "mock"}}


# ----------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    """One failure state: the program, its arguments, the files in its
    directory, and anything a test must change for the state to arise."""

    name: str
    argv: tuple[str, ...] = ("body.yaml", "soul.yaml")
    files: dict[str, str | bytes] = field(default_factory=dict)
    setup: Callable[[pytest.MonkeyPatch, Path], None] | None = None
    plugins: bool = True
    #: The failure row the case reaches, for a case made from a `Fake`.
    row: str | None = None
    #: Distributions installed beside the four packages.
    extra: tuple[metadata.Distribution, ...] = ()

    @property
    def program(self) -> str:
        return "emet-talk" if self.name.startswith("talk-") else "emet-listen"


def documents(manifest: str | None = None, soul_text: str | None = None, take: bytes = QUIET) -> dict[str, str | bytes]:
    return {"body.yaml": manifest if manifest is not None else body(), "soul.yaml": soul_text or soul(), "take.wav": take}


Setup = Callable[[pytest.MonkeyPatch, Path], None]


def hide(*modules: str) -> Setup:
    """An optional library absent, as a machine without the extra has it.
    Its submodules go too: `from websockets.asyncio.client import connect`
    finds a submodule an earlier test imported without asking for its
    parent."""

    def setup(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
        for module in modules:
            for name in [m for m in sys.modules if m.startswith(f"{module}.")]:
                monkeypatch.setitem(sys.modules, name, None)
            monkeypatch.setitem(sys.modules, module, None)

    return setup


def both(*setups: Setup) -> Setup:
    def setup(monkeypatch: pytest.MonkeyPatch, at: Path) -> None:
        for one in setups:
            one(monkeypatch, at)

    return setup


def a_key(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
    """A key that is no vendor's, for a state reached after the key check."""
    monkeypatch.setenv("EMET_SNAPSHOT_KEY", "not-a-real-key")


def keys_named_by_the_variable(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
    monkeypatch.setenv("EMET_KEYS", "absent.env")


def a_folder(name: str) -> Setup:
    def setup(monkeypatch: pytest.MonkeyPatch, at: Path) -> None:
        (at / name).mkdir()

    return setup


def piper_stub(*, model: bool) -> Setup:
    """Piper installed, as a module the plugin imports, with the voice
    places under the test's directory: empty, or holding a model with no
    config beside it."""

    def setup(monkeypatch: pytest.MonkeyPatch, at: Path) -> None:
        from emet_providers import piper as piper_plugin

        stub = ModuleType("piper")
        stub.PiperVoice = None  # type: ignore[attr-defined]  # the model is looked for first, and not found
        stub.SynthesisConfig = None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "piper", stub)
        monkeypatch.setattr(piper_plugin, "user_voices_dir", lambda: at / "voices")
        monkeypatch.setattr(piper_plugin, "SYSTEM_VOICES", at / "system-voices")
        if model:
            (at / "voices").mkdir()
            (at / "voices" / "en_US-ljspeech-medium.onnx").write_bytes(b"")

    return setup


def openai_answers(answer: str) -> Setup:
    """OpenAI's preflight answered by `httpx.MockTransport`: no network, or
    the key refused."""

    def setup(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
        httpx = pytest.importorskip("httpx")
        from emet_providers import openai as openai_plugin

        def handler(request: Any) -> Any:
            if answer == "no network":
                raise httpx.ConnectError("no network in a snapshot test", request=request)
            return httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})

        real_init = openai_plugin.OpenAILanguageModel.__init__

        def init(self: Any, config: Any, *, transport: Any = None) -> None:
            real_init(self, config, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(openai_plugin.OpenAILanguageModel, "__init__", init)
        monkeypatch.setenv("EMET_SNAPSHOT_KEY", "not-a-real-key")

    return setup


def provider_declines(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
    """The mock language model refuses every turn, as a vendor's safety
    system does."""
    from emet_providers.mock import MockLanguageModel

    async def reply(self: Any, prompt: Any) -> Any:
        self.prompts.append(prompt)
        yield ReplyDone(text="", stop_reason="refusal", model="mock")

    monkeypatch.setattr(MockLanguageModel, "reply", reply)


def readable_keys(monkeypatch: pytest.MonkeyPatch, at: Path) -> None:
    """A keys file others can read, judged as on Linux on every machine:
    Windows reports a plain file as mode 0o666, so the check sees what a
    0o644 file on the body shows it."""
    monkeypatch.setattr(keys, "sys", SimpleNamespace(platform="linux"))
    os.chmod(at / "keys.env", 0o644)


def model_gone_after_boot(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
    """The vendor goes away after boot: the mock language model answers
    every turn with an error from then on."""
    real_start = ListenSession.start

    async def start(self):
        await real_start(self)
        self._llm._started = False

    monkeypatch.setattr(ListenSession, "start", start)


# ------------------------------------------------------------ the vendors
#
# Each vendor's real plugin, behind a fake transport. The bodies are built
# from the plugin's own `FAULT_CODES`, so a fake sends the status and code
# the plugin's author read from the vendor's error page, and no word of a
# vendor's message is guessed here.

MODEL = "gpt-5.6-terra"
CLAUDE = "claude-opus-5"


def _coded(module: str, kind: str) -> tuple[int, str]:
    """The first status and code in a plugin's `FAULT_CODES` that names `kind`."""
    codes = importlib.import_module(f"emet_providers.{module}").FAULT_CODES
    return next((status, code) for (status, code), named in codes.items() if named == kind and code)


def _error_body(code: str, message: str) -> dict[str, Any]:
    """An error body in the shape both chat vendors use, `error.type` and
    `error.code` both set, so each plugin reads the field it reads."""
    return {"type": "error", "error": {"type": code, "code": code, "message": message}}


def http_vendor(module: str, cls: str, answer: Callable[[Any, Any], Any], *, key: bool = True) -> Setup:
    """A plugin of `emet_providers.<module>` whose every request `answer`
    takes, as `answer(httpx, request)`, through `httpx.MockTransport`."""

    def setup(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
        httpx = pytest.importorskip("httpx")
        plugin = getattr(importlib.import_module(f"emet_providers.{module}"), cls)
        real_init = plugin.__init__

        def init(self: Any, *args: Any, transport: Any = None, **kwargs: Any) -> None:
            real_init(self, *args, transport=httpx.MockTransport(lambda request: answer(httpx, request)), **kwargs)

        monkeypatch.setattr(plugin, "__init__", init)
        if key:
            monkeypatch.setenv("EMET_SNAPSHOT_KEY", "not-a-real-key")

    return setup


def _model(httpx: Any, request: Any, *, status: int = 200, **fields: Any) -> Any:
    """A model object, as `GET /models/{id}` answers at either vendor."""
    model = request.url.path.rsplit("/", 1)[-1]
    if status != 200:
        return httpx.Response(status, json=_error_body("model_not_found", f"The model `{model}` does not exist."))
    return httpx.Response(200, json={"id": model, "object": "model", "type": "model", "display_name": model,
                                     "created": 0, "created_at": "2026-01-01T00:00:00Z", **fields})


def openai(*, model: int = 200, reply: tuple[int, str] | None = None, shutdown: str | None = None) -> Setup:
    """OpenAI: the preflight's model answered with `model`, a reply refused
    with `reply`'s status and code."""

    def answer(httpx: Any, request: Any) -> Any:
        if request.method == "GET":
            return _model(httpx, request, status=model, **({"shutdown_date": shutdown} if shutdown else {}))
        status, code = reply or (500, "server_error")
        return httpx.Response(status, json=_error_body(code, "a refusal written by a snapshot test"))

    return http_vendor("openai", "OpenAILanguageModel", answer)


def anthropic(*, reply: tuple[int, str]) -> Setup:
    """Anthropic, with the preflight's model active and a reply refused with `reply`."""

    def answer(httpx: Any, request: Any) -> Any:
        if request.method == "GET":
            return _model(httpx, request, lifecycle="active", retires_at=None)
        status, code = reply
        return httpx.Response(status, json=_error_body(code, "a refusal written by a snapshot test"))

    return http_vendor("anthropic", "AnthropicLanguageModel", answer)


def nothing_answers(module: str, cls: str) -> Setup:
    """A server that does not answer, at whatever address the plugin was
    given."""

    def answer(httpx: Any, request: Any) -> Any:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    return http_vendor(module, cls, answer)


def deepgram_voice(*, status: int, code: str | None = None) -> Setup:
    """Deepgram's voice, its preflight answered with `status`."""

    def answer(httpx: Any, request: Any) -> Any:
        if status == 200:
            return httpx.Response(200, content=bytes(480), headers={"content-type": "audio/l16"})
        body = {"err_code": code, "err_msg": "a refusal written by a snapshot test"} if code else {}
        return httpx.Response(status, json=body)

    return http_vendor("deepgram_voice", "DeepgramVoice", answer)


class _Closed(Exception):
    """Shaped like websockets' `ConnectionClosed`, which carries `rcvd` and `sent`."""

    def __init__(self) -> None:
        super().__init__("closed 1000")
        self.rcvd = self.sent = SimpleNamespace(code=1000, reason="")


class _Refused(Exception):
    """Shaped like websockets' `InvalidStatus`, which carries the handshake's response."""

    def __init__(self, status: int, code: str | None) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status}")
        body = json.dumps({"err_code": code, "err_msg": "a refusal written by a snapshot test"}) if code else ""
        self.response = SimpleNamespace(status_code=status, body=body.encode())


class _Socket:
    """A connection that takes what it is sent and closes when asked."""

    async def send(self, message: Any) -> None:
        pass

    async def recv(self) -> Any:
        raise _Closed()

    async def close(self) -> None:
        pass


def deepgram(*, boot: Exception | None = None, turn: Exception | None = None) -> Setup:
    """Deepgram's transcriber with a fake `connect`: the preflight's
    connection refused with `boot`, or opened, and each utterance's refused
    with `turn`."""

    def setup(monkeypatch: pytest.MonkeyPatch, _: Path) -> None:
        from emet_providers import deepgram as plugin

        calls = {"n": 0}

        async def connect(url: str, **kwargs: Any) -> Any:
            calls["n"] += 1
            refusal = boot if calls["n"] == 1 else turn
            if refusal is not None:
                raise refusal
            return _Socket()

        real_init = plugin.DeepgramTranscriber.__init__

        def init(self: Any, config: Any, fmt: Any, **kwargs: Any) -> None:
            real_init(self, config, fmt, connect=connect)

        monkeypatch.setattr(plugin.DeepgramTranscriber, "__init__", init)
        monkeypatch.setenv("EMET_SNAPSHOT_KEY", "not-a-real-key")

    return setup


#: The soul's words when a turn failed, so the line is told from others.
FAILED = "I lost the thread there. Say it again?"


def fake_soul(**models: dict) -> str:
    return soul(models={**MOCKS, **models}, lines={"failed": FAILED})


@dataclass(frozen=True)
class Fake:
    """One way to reach a failure row: the vendor behind it and the run
    that makes the vendor fail. `emet-listen` runs it with `--reply
    --speak`, so the failure line and the line said aloud both print."""

    vendor: str
    files: dict[str, str | bytes]
    setup: Setup | None = None
    extra: tuple[metadata.Distribution, ...] = ()

    def case(self, program: str, failure: str) -> Case:
        prefix = "talk" if program == "emet-talk" else "listen"
        argv = ("body.yaml", "soul.yaml") + (("--reply", "--speak") if program == "emet-listen" else ())
        return Case(f"{prefix}-{failure}-{self.vendor}", argv, self.files, self.setup, row=failure, extra=self.extra)


OPENAI = {"chat": {"provider": "openai", "model": MODEL, "key_env": "EMET_SNAPSHOT_KEY"}}
DEEPGRAM_STT = {"stt": {"provider": "deepgram", "model": "nova-3", "key_env": "EMET_SNAPSHOT_KEY"}}


def chat_body(**chat: Any) -> str:
    """A body that takes the chat stage over with `params`, which a soul's
    closed model block cannot hold."""
    return body(models={"chat": chat})


#: Each failure row and the fakes that reach it.
ROW_FAKES: dict[str, tuple[Fake, ...]] = {
    "llm-unavailable-at-boot": (
        Fake("openai", documents(soul_text=fake_soul(**OPENAI), take=SPOKEN), openai(model=404)),
    ),
    "llm-unavailable-before": (
        Fake("mock", documents(chat_body(provider="mock", params={
            "fail_on_start": True, "fault": "model_unavailable",
            "fault_detail": "The mock was told its model is retired."}), fake_soul(), SPOKEN)),
    ),
    "llm-unavailable-on-reply": (
        Fake("openai", documents(soul_text=fake_soul(**OPENAI), take=SPOKEN),
             openai(reply=_coded("openai", "model_unavailable"))),
    ),
    "llm-empty-at-cap": (
        Fake("mock", documents(chat_body(provider="mock", params={"reasoning_tokens": 100000}), fake_soul(), SPOKEN)),
    ),
    "llm-cut-at-cap": (
        Fake("mock", documents(chat_body(provider="mock", params={
            "reply": "Well. The sky is blue because air scatters blue light the most.",
            "reasoning_tokens": DEFAULT_MAX_TOKENS - 3}), fake_soul(), SPOKEN)),
    ),
    "llm-out-of-credit": (
        Fake("openai", documents(soul_text=fake_soul(**OPENAI), take=SPOKEN),
             openai(reply=_coded("openai", "out_of_credit"))),
        Fake("anthropic", documents(soul_text=fake_soul(chat={
            "provider": "anthropic", "model": CLAUDE, "key_env": "EMET_SNAPSHOT_KEY"}), take=SPOKEN),
             anthropic(reply=_coded("anthropic", "out_of_credit"))),
    ),
    "llm-failed": (
        Fake("mock", documents(soul_text=fake_soul(), take=SPOKEN), model_gone_after_boot),
    ),
    "llm-cannot-start": (
        Fake("openai", documents(chat_body(provider="openai", model="qwen3", key_env="EMET_SNAPSHOT_KEY",
                                           params={"url": "http://127.0.0.1:9380/v1"}), fake_soul()),
             nothing_answers("openai", "OpenAILanguageModel")),
        Fake("anthropic", documents(chat_body(provider="anthropic", model="qwen3", key_env="EMET_SNAPSHOT_KEY",
                                              params={"url": "http://192.168.1.20:8080"}), fake_soul()),
             nothing_answers("anthropic", "AnthropicLanguageModel")),
    ),
    "stt-out-of-credit-at-boot": (
        Fake("deepgram", documents(soul_text=fake_soul(**DEEPGRAM_STT)),
             deepgram(boot=_Refused(*_coded("deepgram", "out_of_credit")))),
    ),
    "voice-out-of-credit-at-boot": (
        Fake("deepgram", documents(soul_text=fake_soul(tts={
            "provider": "deepgram", "model": "aura-2-thalia-en", "key_env": "EMET_SNAPSHOT_KEY"})),
             deepgram_voice(status=402)),
    ),
    "stt-out-of-credit": (
        Fake("deepgram", documents(soul_text=fake_soul(**DEEPGRAM_STT), take=SPOKEN),
             deepgram(turn=_Refused(*_coded("deepgram", "out_of_credit")))),
    ),
    "stt-model-rejected": (
        Fake("deepgram", documents(soul_text=fake_soul(**DEEPGRAM_STT), take=SPOKEN),
             deepgram(turn=_Refused(*_coded("deepgram", "model_unavailable")))),
    ),
    "stt-words-lost": (
        Fake("deepgram", documents(soul_text=fake_soul(**DEEPGRAM_STT), take=SPOKEN),
             deepgram(turn=ConnectionRefusedError("[Errno 111] Connection refused"))),
    ),
    "source-cannot-start": (
        Fake("wav", {"body.yaml": body(audio={
            "input": {"source": "wav", "device": "file", "params": {"path": "nothere.wav"}},
            "output": {"sink": "null", "device": "none"}, "wake": {"engine": "mock"}}), "soul.yaml": fake_soul()}),
    ),
    "sink-cannot-start": (
        Fake("wav", documents(body(audio={
            "input": {"source": "wav", "device": "file", "params": {"path": "take.wav"}},
            "output": {"sink": "wav", "device": "file", "params": {"path": "nofolder/said.wav"}},
            "wake": {"engine": "mock"}}), fake_soul())),
    ),
    "duplicate-plugin": (
        Fake("openai", documents(soul_text=fake_soul(**OPENAI)), extra=(FORK,)),
    ),
}

#: Every row in both programs, each with its fakes.
FAKES: dict[tuple[str, str], tuple[Fake, ...]] = {
    (program, failure): fakes for failure, fakes in ROW_FAKES.items() for program in sorted(PROGRAMS)
}


CASES = [
    Case("listen-no-plugins", plugins=False),
    Case("talk-no-plugins", plugins=False),
    Case("listen-keys-missing", ("body.yaml", "soul.yaml", "--keys", "absent.env"), documents()),
    Case("listen-keys-malformed", ("body.yaml", "soul.yaml", "--keys", "keys.env"),
         {**documents(), "keys.env": "EMET-SNAPSHOT-KEY=not-a-real-secret\n"}),
    Case("listen-keys-unclosed-quote", ("body.yaml", "soul.yaml", "--keys", "keys.env"),
         {**documents(), "keys.env": 'EMET_SNAPSHOT_KEY="not-a-real-secret\n'}),
    Case("listen-keys-readable-by-others", ("body.yaml", "soul.yaml", "--keys", "keys.env"),
         {**documents(), "keys.env": "EMET_SNAPSHOT_KEY=not-a-real-secret\n"}, readable_keys),
    Case("listen-keys-no-equals", ("body.yaml", "soul.yaml", "--keys", "keys.env"),
         {**documents(), "keys.env": "EMET_GOOD=1\nnot-a-real-secret-token\n"}),
    Case("listen-keys-only-padding", ("body.yaml", "soul.yaml", "--keys", "keys.env"),
         {**documents(), "keys.env": "NotARealSecretToken0123==\n"}),
    Case("listen-keys-not-utf8", ("body.yaml", "soul.yaml", "--keys", "keys.env"),
         {**documents(), "keys.env": b"EMET_GOOD=1\nEMET_SNAPSHOT_KEY=not-a-real-secret\xe9\n"}),
    Case("listen-keys-named-by-the-variable", files=documents(), setup=keys_named_by_the_variable),
    Case("listen-manifest-missing", files={"soul.yaml": soul()}),
    Case("listen-manifest-unreadable", files=documents('manifest_version: "0.1"\nbody: [desk\n')),
    Case("listen-manifest-invalid", files=documents('manifest_version: "0.1"\n'
                                                    "body: {id: probe, scale: desk, power: plugged_in}\n")),
    Case("listen-manifest-missing-plugin", files=documents(body(wake={"engine": "porcupine"}))),
    Case("listen-soul-invalid", files=documents(soul_text='bundle_version: "0.1"\nidentity: {name: Emet, wake_word: hey emet}\n')),
    Case("listen-driver-not-installed", files=documents(body(capabilities=[
        {"id": "ring", "type": "light", "role": "ambient", "driver": {"plugin": "acme_ws2812"}}]))),
    Case("listen-cannot-hear-the-name", files=documents(body(wake={"engine": "mock", "params": {"phrases": ["ok robot"]}}))),
    Case("listen-wake-not-installed", files=documents(body(wake={"engine": "pocketsphinx"})), setup=hide("pocketsphinx")),
    # L16: the mock wake engine logs its refusal at INFO, so it prints once.
    Case("listen-wake-cannot-start", files=documents(body(wake={"engine": "mock", "params": {"fail_on_start": True}}))),
    Case("listen-transcribe-no-provider", ("body.yaml", "soul.yaml", "--transcribe"), documents()),
    Case("listen-transcribe-not-installed", ("body.yaml", "soul.yaml", "--transcribe"),
         documents(soul_text=soul(models={"stt": {"provider": "whisper"}}))),
    # A soul's model block is closed and takes no `params`; a body takes the
    # stage over with them, as a test rig does.
    Case("listen-transcribe-no-key", ("body.yaml", "soul.yaml", "--transcribe"), documents(body(models={
        "stt": {"provider": "mock", "key_env": "EMET_SNAPSHOT_KEY", "params": {"require_key": True}}}))),
    Case("listen-transcribe-wrong-rate", ("body.yaml", "soul.yaml", "--transcribe"),
         documents(body(models={"stt": {"provider": "mock", "params": {"sample_rate": 8000}}}))),
    Case("listen-transcribe-deepgram-no-key", ("body.yaml", "soul.yaml", "--transcribe"),
         documents(soul_text=soul(models={"stt": {"provider": "deepgram", "key_env": "EMET_SNAPSHOT_KEY"}}))),
    Case("listen-transcribe-no-websockets", ("body.yaml", "soul.yaml", "--transcribe"), documents(body(models={
        "stt": {"provider": "deepgram", "key_env": "EMET_SNAPSHOT_KEY", "params": {"preflight": False}}})),
         both(a_key, hide("websockets"))),
    Case("listen-words-never-arrived", ("body.yaml", "soul.yaml", "--transcribe"), documents(body(models={
        "stt": {"provider": "mock", "params": {"cassette": "cassettes"}}}), take=SPOKEN), a_folder("cassettes")),
    Case("listen-reply-no-model", ("body.yaml", "soul.yaml", "--reply"), documents(soul_text=soul(models=STT))),
    Case("listen-reply-no-key", ("body.yaml", "soul.yaml", "--reply"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "openai", "model": "gpt-5.6-terra", "key_env": "EMET_OPENAI_KEY"}}))),
    Case("listen-reply-anthropic-no-key", ("body.yaml", "soul.yaml", "--reply"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "anthropic", "model": "claude-sonnet-5", "key_env": "EMET_SNAPSHOT_KEY"}}))),
    Case("listen-reply-no-http-library", ("body.yaml", "soul.yaml", "--reply"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "openai", "model": "gpt-5.6-terra", "key_env": "EMET_SNAPSHOT_KEY"}})),
         both(a_key, hide("httpx"))),
    Case("listen-reply-no-network", ("body.yaml", "soul.yaml", "--reply"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "openai", "model": "gpt-5.6-terra", "key_env": "EMET_SNAPSHOT_KEY"}})),
         openai_answers("no network")),
    Case("listen-reply-key-rejected", ("body.yaml", "soul.yaml", "--reply"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "openai", "model": "gpt-5.6-terra", "key_env": "EMET_SNAPSHOT_KEY"}})),
         openai_answers("key rejected")),
    Case("listen-reply-declined", ("body.yaml", "soul.yaml", "--reply"),
         documents(soul_text=soul(models={**STT, "chat": {"provider": "mock"}}), take=SPOKEN), provider_declines),
    # C2: with --speak a refusal is answered aloud with the soul's declined line.
    Case("listen-speak-declined", ("body.yaml", "soul.yaml", "--reply", "--speak"),
         documents(soul_text=soul(models=MOCKS, lines={"declined": "I would rather not answer that."}), take=SPOKEN),
         provider_declines),
    Case("listen-speak-no-voice", ("body.yaml", "soul.yaml", "--speak"),
         documents(soul_text=soul(models={**STT, "chat": {"provider": "mock"}}))),
    Case("listen-speak-voice-not-installed", ("body.yaml", "soul.yaml", "--speak"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "mock"}, "tts": {"provider": "piper", "model": "en_US-ljspeech-medium"}})),
         hide("piper")),
    Case("listen-speak-no-voice-model", ("body.yaml", "soul.yaml", "--speak"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "mock"}, "tts": {"provider": "piper", "model": "en_US-ljspeech-medium"}})),
         piper_stub(model=False)),
    Case("listen-speak-voice-no-config", ("body.yaml", "soul.yaml", "--speak"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "mock"}, "tts": {"provider": "piper", "model": "en_US-ljspeech-medium"}})),
         piper_stub(model=True)),
    Case("listen-speak-deepgram-no-key", ("body.yaml", "soul.yaml", "--speak"), documents(soul_text=soul(models={
        **STT, "chat": {"provider": "mock"}, "tts": {"provider": "deepgram", "key_env": "EMET_SNAPSHOT_KEY"}}))),
    Case("listen-speak-and-echo", ("body.yaml", "soul.yaml", "--echo", "--speak"), documents()),
    # B7-F1, ledger L11 of 0.5.7: a replay file the source cannot read is
    # one line naming the source, and no traceback.
    Case("listen-replay-missing", ("body.yaml", "soul.yaml", "--replay", "nothere.wav"), documents()),
    Case("listen-replay-wrong-rate", ("body.yaml", "soul.yaml", "--replay", "slow.wav"),
         {**documents(), "slow.wav": wav(frame() * 3, rate=8000)}),
    Case("listen-replay-wrong-width", ("body.yaml", "soul.yaml", "--replay", "narrow.wav"),
         {**documents(), "narrow.wav": wav(bytes(FRAME * 3), width=1)}),
    Case("talk-replay-missing", ("body.yaml", "soul.yaml", "--replay", "nothere.wav"),
         documents(soul_text=soul(models=MOCKS))),
    # MC1 of 0.5.7: a model with a retirement date boots, with one notice.
    Case("talk-model-retires", files=documents(soul_text=soul(models={**MOCKS, **OPENAI})),
         setup=openai(shutdown="2099-12-31")),
    # SF2 stage 1: where each stage runs, the key it read, the package it came from.
    Case("talk-providers-named", files=documents(soul_text=soul(models={
        **DEEPGRAM_STT, **OPENAI, "tts": {"provider": "deepgram", "model": "aura-2-thalia-en",
                                          "key_env": "EMET_SNAPSHOT_KEY"}})),
         setup=both(deepgram(), openai(), deepgram_voice(status=200))),
    Case("listen-providers-named", ("body.yaml", "soul.yaml", "--speak"), documents(soul_text=soul(models={
        **DEEPGRAM_STT, **OPENAI, "tts": {"provider": "mock"}})), setup=both(deepgram(), openai())),
    # BVP12: --events writes what --stats prints, so it is refused alone.
    Case("listen-events-without-stats", ("body.yaml", "soul.yaml", "--events", "run.jsonl"), documents()),
    Case("talk-events-without-stats", ("body.yaml", "soul.yaml", "--events", "run.jsonl"), documents()),
    Case("listen-events-unwritable", ("body.yaml", "soul.yaml", "--stats", "--events", "nofolder/run.jsonl"),
         documents()),
    Case("listen-bad-chains", ("body.yaml", "soul.yaml", "--chains", "chains.yaml"), {**documents(), "chains.yaml": (
        "express.curiosity:\n  rungs:\n    - {actuator: {role: head}, action: tilt}\n"
        "    - {actuator: {voice: true}, action: sing}\n")}),
    Case("listen-part-fault", files=documents(body(capabilities=[
        {"id": "ring", "type": "light", "role": "ambient",
         "driver": {"plugin": "emet_hal.mock", "params": {"fail_on_start": True}}}]))),
    Case("listen-state-of-another-body", ("body.yaml", "soul.yaml", "--state", "state.json"),
         {**documents(), "state.json": '{"state_version": "0.1", "body_id": "scout_01"}\n'}),
    Case("listen-state-not-json", ("body.yaml", "soul.yaml", "--state", "state.json"),
         {**documents(), "state.json": "not json {\n"}),
    Case("listen-state-not-an-object", ("body.yaml", "soul.yaml", "--state", "state.json"),
         {**documents(), "state.json": "[1, 2]\n"}),
    Case("listen-state-newer-engine", ("body.yaml", "soul.yaml", "--state", "state.json"),
         {**documents(), "state.json": '{"state_version": "9.0", "body_id": "probe"}\n'}),
    Case("listen-state-unreadable-version", ("body.yaml", "soul.yaml", "--state", "state.json"),
         {**documents(), "state.json": '{"state_version": "banana", "body_id": "probe"}\n'}),
    Case("listen-state-wrong-shape", ("body.yaml", "soul.yaml", "--state", "state.json"),
         {**documents(), "state.json": '{"state_version": "0.1", "body_id": "probe", "trims": [1, 2]}\n'}),
    Case("listen-lost-sentence", ("body.yaml", "soul.yaml", "--speak"), documents(body(models={
        "chat": {"provider": "mock", "params": {"reply": "Fine. The POISON word."}},
        "tts": {"provider": "mock", "params": {"fail_on": "POISON"}}}), soul(models=MOCKS), SPOKEN)),
    Case("talk-no-models", files=documents()),
    Case("talk-failed-line", files=documents(soul_text=soul(models=MOCKS, lines={"failed": "I lost the thread there. Say it again?"}),
                                             take=SPOKEN), setup=model_gone_after_boot),
    Case("talk-declined-line", files=documents(soul_text=soul(models=MOCKS, lines={"declined": "I would rather not answer that."}),
                                               take=SPOKEN), setup=provider_declines),
    Case("talk-words-never-arrived", files=documents(body(models={
        "stt": {"provider": "mock", "params": {"cassette": "cassettes"}}}), soul(models=MOCKS), SPOKEN),
         setup=a_folder("cassettes")),
]


async def _no_connection(*args: Any, **kwargs: Any) -> Any:
    raise OSError("a console snapshot test opens no connection")


@pytest.fixture
def at(tmp_path, monkeypatch):
    """A directory to run in: no key in the environment, git never asked,
    no connection opened, and a terminal 80 columns wide."""
    monkeypatch.chdir(tmp_path)
    for name in ("EMET_SNAPSHOT_KEY", "EMET_OPENAI_KEY"):
        # Set, then removed: the undo removes whatever a run puts there.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    status = "# branch.oid eb55748541483e9769610526bbaa21bfbe439f0b\n# branch.head master\n"
    monkeypatch.setattr(cli, "_run_git", lambda args, directory: subprocess.CompletedProcess(["git", *args], 0, status, ""))
    # httpx and websockets both connect through the event loop.
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", _no_connection)
    monkeypatch.setenv("COLUMNS", "80")
    return tmp_path


#: Every case a fake makes, one for each program, row and vendor.
FAKE_CASES = [fake.case(program, failure) for (program, failure), fakes in FAKES.items() for fake in fakes]


def run_case(
    case: Case, at: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, taken: list[str] | None = None
) -> tuple[Any, str, str]:
    """Run one case's program in `at`, as a terminal would show it.

    `taken`, when given, gets the case of every failure row the session
    took: each turn's, from `finish_turn`, and each boot row it took by
    name. Rows that print the same line are told apart by it.
    """
    if taken is not None:
        real_finish = ListenSession.finish_turn
        real_row = session_module.row

        async def finish_turn(self: ListenSession, utterance: Any, reply: Any) -> Any:
            end = await real_finish(self, utterance, reply)
            if end.failure is not None:
                taken.append(end.failure.case)
            return end

        def by_name(name: str) -> Any:
            taken.append(name)
            return real_row(name)

        monkeypatch.setattr(ListenSession, "finish_turn", finish_turn)
        monkeypatch.setattr(session_module, "row", by_name)
    for name, content in case.files.items():
        if isinstance(content, bytes):
            (at / name).write_bytes(content)
        else:
            (at / name).write_text(content, encoding="utf-8")
    chosen = registry(*case.extra) if case.plugins else PluginRegistry()
    for module in (cli, talk):
        monkeypatch.setattr(module, "PluginRegistry", SimpleNamespace(discover=lambda: chosen))
    if case.setup is not None:
        case.setup(monkeypatch, at)

    main = talk.main if case.program == "emet-talk" else cli.main
    with terminal_logging():
        try:
            code = main(list(case.argv))
        except SystemExit as exc:
            code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("case", [pytest.param(c, id=c.name) for c in CASES + FAKE_CASES])
def test_the_console_shows_this_failure_as_its_snapshot_does(case, at, monkeypatch, capsys):
    taken: list[str] = []
    code, out, err = run_case(case, at, monkeypatch, capsys, taken)
    if case.row is not None:
        problems = row_problems(case, out, err, taken)
        assert problems == [], f"{case.name}: " + "; ".join(problems)
    compare(case.name, normalise(render(case.program, list(case.argv), code, out, err), at))


def pattern(template: str) -> re.Pattern[str]:
    """A failure line's format string as a pattern any filling matches: a
    `{name!r}` is a quoted name, `{cap}` a number, any other field any
    text that is not empty, since every fake's fault has its sentence."""
    parts = []
    for literal, name, _, conversion in string.Formatter().parse(template):
        parts.append(re.escape(literal))
        if name is None:
            continue
        parts.append("'[^']*'" if conversion == "r" else r"\d+" if name == "cap" else r"\S.*")
    return re.compile("".join(parts))


#: How the one row with no words of its own is known: its whole line is the
#: SDK's refusal, `{exc}`, which opens with its code.
WHOLE_LINE_ROWS = {"duplicate-plugin": "[error] duplicate_plugin: "}


def row_problems(case: Case, out: str, err: str, taken: list[str] | None = None) -> list[str]:
    """What a fake's run lacks of its row: the row's line on the row's
    stream, the row's spoken line after the speaker's tag or after
    `spoke the line: `, and, given `taken`, the row among those the session
    took, since three of the transcriber's rows print the same line. Each
    problem names the row and what is missing."""
    failure = row(case.row or "")
    stream = out if failure.stream == "stdout" else err
    template = failure.template(case.program)
    problems = []
    lines = stream.splitlines()
    if not any(pattern(template).fullmatch(line) or pattern(template.rstrip()).fullmatch(line) for line in lines):
        problems.append(f"no line on {failure.stream} matches the row {failure.case!r}: {template!r}")
    opening = WHOLE_LINE_ROWS.get(failure.case)
    if opening is not None and not any(line.startswith(opening) for line in lines):
        problems.append(f"the row {failure.case!r} is the SDK's refusal and no line opens {opening!r}")
    if taken is not None and opening is None and failure.case not in taken:
        problems.append(f"the session took the row(s) {taken} and not {failure.case!r}")
    if failure.spoken is not None:
        words = persona_lines(yaml.safe_load(case.files["soul.yaml"]))[failure.spoken]
        said = f"  emet: {words}" if case.program == "emet-talk" else f"    spoke the line: {words}"
        if said not in out.splitlines():
            problems.append(f"the row {failure.case!r} says {failure.spoken!r} and no line reads {said!r}")
    return problems


def test_every_failure_row_has_a_fake_in_both_programs():
    """`FAKES` names, for each program, every row of the failure table, so
    a row added to `emet_engine.failures` without a way to reach it fails
    here, naming it."""
    assert set(FAKES) == {(program, f.case) for f in FAILURES for program in f.programs}
    assert all(FAKES.values())


def test_a_run_missing_its_rows_line_is_named():
    """The check every fake's snapshot runs, broken: a run that printed
    neither the row's line nor its spoken line gets both named."""
    case = Fake("mock", documents(soul_text=fake_soul())).case("emet-talk", "llm-failed")
    problems = row_problems(case, "  (heard 'hey emet')\n  emet: something else\n", "")
    assert problems == [
        "no line on stdout matches the row 'llm-failed': '  (the language model failed: {detail})'",
        f"the row 'llm-failed' says 'failed' and no line reads '  emet: {FAILED}'",
    ]


def test_the_row_check_tells_apart_rows_that_print_the_same_line():
    """Three of the transcriber's rows print one line, so a run that took
    the wrong one is named by the row it took; an empty detail is no
    filling, and the registry's row is known by the SDK's own refusal."""
    lost = Fake("deepgram", documents(soul_text=fake_soul())).case("emet-talk", "stt-model-rejected")
    printed = f"  (the words never arrived: Deepgram does not serve the model 'nova-3'.)\n  emet: {FAILED}\n"
    assert row_problems(lost, printed, "", ["stt-model-rejected"]) == []
    assert row_problems(lost, printed, "", ["stt-words-lost"]) == [
        "the session took the row(s) ['stt-words-lost'] and not 'stt-model-rejected'"
    ]
    assert row_problems(lost, f"  (the words never arrived: )\n  emet: {FAILED}\n", "", ["stt-model-rejected"]) == [
        "no line on stdout matches the row 'stt-model-rejected': '  (the words never arrived: {detail})'"
    ]
    twice = Fake("openai", documents(soul_text=fake_soul())).case("emet-talk", "duplicate-plugin")
    assert row_problems(twice, "", "\nsome other refusal entirely\n", []) == [
        "the row 'duplicate-plugin' is the SDK's refusal and no line opens '[error] duplicate_plugin: '"
    ]


def test_every_snapshot_file_belongs_to_a_case():
    """A snapshot whose case was removed would sit in the folder unread."""
    names = {c.name for c in CASES + FAKE_CASES}
    assert sorted(p.stem for p in SNAPSHOTS.glob("*.txt") if p.stem not in names) == []


def test_each_plugin_comes_from_the_package_that_ships_it():
    """The registry the cases run with knows each plugin's package, as
    discovery does, and finds a name a second package offers."""
    shipped = registry()
    assert shipped.origin(GROUP_LLM, "openai").label == f"emet-providers {cli._version('emet-providers')}"
    assert shipped.origin(GROUP_WAKE, "mock").distribution == "emet-hal"
    assert sum(len(names) for names in _shipped_by("emet-hal").values()) + sum(
        len(names) for names in _shipped_by("emet-providers").values()) == sum(len(n) for n in SHIPPED.values())
    from emet_sdk.validate import DuplicatePluginError

    with pytest.raises(DuplicatePluginError, match="emet-openai-fork 0.1.0"):
        registry(FORK).load_llm("openai")


def test_the_shipped_plugins_are_the_ones_the_pyproject_files_register():
    """`SHIPPED` is a copy, so that no snapshot depends on what a machine has
    installed. A plugin added to a `pyproject.toml` changes every
    "Installed: ..." line a builder sees, and this test makes the copy, and
    with it those snapshots, change in the same pull request."""
    import tomllib

    registered: dict[str, dict[str, str]] = {}
    for package in ("emet-sdk", "emet-hal", "emet-providers", "emet-engine"):
        project = tomllib.loads((HERE.parents[1] / package / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        for group, entries in project.get("entry-points", {}).items():
            if group.startswith("emet."):
                registered.setdefault(group, {}).update(entries)
    assert registered == SHIPPED


def test_a_rewrite_is_reported_as_a_skip_and_is_refused_under_ci(tmp_path, monkeypatch):
    """`EMET_UPDATE_SNAPSHOTS=1` left exported would otherwise turn every
    case green whatever it printed, and a CI run would rewrite what it is
    there to compare."""
    monkeypatch.setattr(sys.modules[__name__], "SNAPSHOTS", tmp_path)
    monkeypatch.setenv(UPDATE, "1")
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(pytest.skip.Exception, match="rewrote snapshots/case.txt"):
        compare("case", "what was printed\n")
    assert (tmp_path / "case.txt").read_text(encoding="utf-8") == "what was printed\n"

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(pytest.fail.Exception, match="under CI"):
        compare("other", "what was printed\n")
    assert not (tmp_path / "other.txt").exists()


def test_no_case_can_open_a_connection(at):
    """The guard the `at` fixture sets, which keeps a case that names a
    vendor from reaching it if a hidden library or a stub were to fail."""

    async def connect() -> None:
        await asyncio.get_running_loop().create_connection(asyncio.Protocol, "127.0.0.1", 9)

    with pytest.raises(OSError, match="opens no connection"):
        asyncio.run(connect())


def test_a_slow_callback_line_stays_out_and_other_asyncio_lines_stay_in(capsys):
    """The line asyncio's debug mode writes, as `base_events.py` writes it,
    reaches no snapshot under `python -X dev`. Any other asyncio warning
    still does, and the filter is gone once the run ends."""
    log = logging.getLogger("asyncio")
    with terminal_logging():
        log.warning("Executing %s took %.3f seconds", "<Task pending name='Task-1'>", 0.25)
        log.warning("an asyncio warning a person should see")
    assert capsys.readouterr().err == "an asyncio warning a person should see\n"
    assert _no_slow_callbacks not in log.filters
