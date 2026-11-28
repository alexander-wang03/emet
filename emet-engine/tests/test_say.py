"""`emet-say`, which says a line in the soul's voice with nothing listening.

Its console is held by snapshots of its own, in `snapshots/say/`, written by
this file and normalised by this file: the other programs' normaliser
rewrites only their own identity lines, and their check that every snapshot
belongs to a case reads `snapshots/*.txt` and does not look into `say/`.
Only what differs by machine is replaced: Windows' line ends, the temporary
directory, a Windows backslash inside a path, the four versions and the
revision on the identity line, the version after `from <distribution>`, the
time to the first sound, and argparse's usage lines.

Every case runs on the mock path: the mock voice, the null sink or a WAV
file in the test's own directory, and the plugins the four packages ship,
read from their installed metadata so that the voice line names the
distribution that offered the voice. Nothing opens a sound card or reaches
a vendor, and every connection the event loop is asked to open is refused.
`EMET_UPDATE_SNAPSHOTS=1` rewrites a case's snapshot and reports the case as
skipped; under CI it fails instead.
"""

from __future__ import annotations

import asyncio
import asyncio.base_events
import difflib
import logging
import os
import re
import subprocess
import sys
import tomllib
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

import pytest
import yaml

from emet_sdk.discovery import GROUP_AUDIO_OUT, GROUP_TTS, PluginOrigin, PluginRegistry
from emet_sdk.plugin import PluginError, VoicePlugin
from emet_sdk.types import Health, VoiceDescriptor

from emet_engine import cli, say, tones
from emet_engine.speech import SpokenSentence

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE / "snapshots" / "say"
FILE = "tests/test_say.py"
UPDATE = "EMET_UPDATE_SNAPSHOTS"
PHRASE = "hey emet"


# ----------------------------------------------------------- fake plugins


class CloudVoice(VoicePlugin):
    """A voice that sends its words to a server and reads a key, as a cloud
    voice does, and has a notice about itself; no network is touched."""

    provider = "cloud"

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(provider="cloud", model="aura-test", sample_rate=24000,
                               endpoint="https://speech.example.invalid/v1/speak", key_env="EMET_SNAPSHOT_KEY")

    def health(self) -> Health:
        return Health(detail="the voice 'aura-test' retires on 2027-01-01 at Example. "
                             "Choose another under models.tts.model before then.")

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        payload = text.encode("ascii", "replace")
        yield payload + bytes(len(payload) % 2)


class BrokenSink:
    """A sink whose card is gone, which says so the supported way."""

    def __init__(self, config: Any = None, fmt: Any = None) -> None:
        self.stopped = 0

    async def start(self) -> None:
        raise PluginError("the card is gone")

    async def play(self, pcm: bytes) -> None:
        raise AssertionError("a sink that did not start was played to")

    async def cancel(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped += 1


def registry() -> PluginRegistry:
    """The plugins `emet-hal` and `emet-providers` ship, from their installed
    metadata so each keeps the distribution that declared it, and the two
    fakes above, which no distribution declares."""
    shipped = [ep for name in ("emet-hal", "emet-providers") for ep in metadata.distribution(name).entry_points]
    fakes = [EntryPoint("cloud", f"{__name__}:CloudVoice", GROUP_TTS),
             EntryPoint("broken", f"{__name__}:BrokenSink", GROUP_AUDIO_OUT)]
    return PluginRegistry.from_entry_points([*shipped, *fakes])


# ------------------------------------------------------------- snapshots


def normalise(text: str, tmp: Path) -> str:
    """What differs by machine, replaced, as the docstring at the top lists it."""
    text = text.replace("\r\n", "\n")
    for form in (repr(str(tmp))[1:-1], str(tmp), tmp.as_posix()):
        text = text.replace(form, "<tmp>")
    if os.sep == "\\":
        text = re.sub(r"(?<=[\w.>])\\{1,2}(?=[\w.<])", "/", text)
    text = re.sub(r"(?m)^emet-say: .*; revision .*$",
                  lambda m: re.sub(r"(emet-(?:sdk|hal|providers|engine)) [^,;\s]+", r"\1 <version>",
                                   m.group(0).split("; revision ")[0]) + "; revision <revision>", text)
    text = re.sub(r"(\bfrom [A-Za-z0-9][A-Za-z0-9_.-]*) \d+\.\d+[^\s,;)]*", r"\1 <version>", text)
    text = re.sub(r"voice first \d+ ms", "voice first <ms> ms", text)
    return re.sub(r"(?ms)^usage: .*?(?=^[\w .-]+: error: )",
                  "usage: (argparse's usage lines, which differ by Python version)\n", text)


def block(text: str) -> str:
    if not text:
        return "(nothing)\n"
    return text if text.endswith("\n") else text + "\n"


def render(argv: list[str], code: Any, out: str, err: str) -> str:
    shown = " ".join(a if a and " " not in a and "[" not in a else repr(a) for a in argv)
    return f"$ emet-say {shown}\nexit {code}\n--- stdout\n{block(out)}--- stderr\n{block(err)}"


def compare(name: str, actual: str) -> None:
    """Hold `actual` to `snapshots/say/<name>.txt`, or write it under
    `EMET_UPDATE_SNAPSHOTS=1`."""
    path = SNAPSHOTS / f"{name}.txt"
    command = f"{UPDATE}=1 python -m pytest {FILE} -k '{name}]'"
    if os.environ.get(UPDATE) == "1":
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail(f"{UPDATE}=1 under CI: a snapshot is rewritten on purpose by a person, and CI only "
                        f"compares. Unset it.", pytrace=False)
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        pytest.skip(f"rewrote snapshots/say/{name}.txt; read the diff before committing it")
    if not path.exists():
        pytest.fail(f"no snapshot at say/{path.name}. Write it, read it, and commit it:\n    {command}", pytrace=False)
    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if actual != expected:
        diff = "".join(difflib.unified_diff(
            expected.splitlines(keepends=True), actual.splitlines(keepends=True),
            fromfile=f"snapshots/say/{name}.txt", tofile="what the command printed"))
        pytest.fail(f"the console changed for {name}:\n{diff}\nIf the change is meant, rewrite the snapshot "
                    f"and review the diff:\n    {command}", pytrace=False)


@contextmanager
def terminal_logging():
    """Logging as a terminal has it: no handler, so a warning reaches stderr
    through `logging.lastResort`, which capsys captures."""
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
    `asyncio/base_events.py`, as `test_console_snapshots.py` reads it). How
    long a callback takes belongs to the machine, so that line stays out of
    a snapshot, and every other asyncio record stays in."""
    return not str(record.msg).startswith("Executing %s took")


# ------------------------------------------------------------- documents


def body(*, output: dict | None = None, audio_input: dict | None = None, models: dict | None = None) -> str:
    doc: dict[str, Any] = {
        "manifest_version": "0.1",
        "body": {"id": "probe", "name": "a speakerphone on a desk", "scale": "desk", "power": "plugged_in"},
        "audio": {
            # Never opened: the file is not even there.
            "input": audio_input or {"source": "wav", "device": "file", "params": {"path": "take.wav"}},
            "output": output or {"sink": "null", "device": "none"},
            "wake": {"engine": "mock"},
        },
        "capabilities": [],
    }
    if models:
        doc["models"] = models
    return yaml.safe_dump(doc, sort_keys=False)


def soul(tts: dict | None = None) -> str:
    doc: dict[str, Any] = {"bundle_version": "0.1", "identity": {"name": "Emet", "wake_word": PHRASE, "line": "emet"}}
    if tts is not None:
        doc["models"] = {"tts": tts}
    return yaml.safe_dump(doc, sort_keys=False)


MOCK = {"provider": "mock"}
KETTLE = "[express.curiosity] The kettle has boiled."


@dataclass(frozen=True)
class Case:
    name: str
    argv: tuple[str, ...]
    files: dict[str, str] = field(default_factory=dict)
    plugins: bool = True
    setup: Callable[[pytest.MonkeyPatch, Path], None] | None = None


def documents(manifest: str | None = None, soul_text: str | None = None) -> dict[str, str]:
    return {"body.yaml": manifest if manifest is not None else body(), "soul.yaml": soul_text or soul(MOCK)}


def private_keys(monkeypatch: pytest.MonkeyPatch, at: Path) -> None:
    """A keys file only its owner reads, so no machine warns about it."""
    os.chmod(at / "keys.env", 0o600)


CASES = [
    Case("say-to-file", ("body.yaml", "soul.yaml", KETTLE, "--to", "kettle.wav"), documents()),
    Case("say-manifest-sink", ("body.yaml", "soul.yaml", "Hello there."), documents()),
    Case("say-move-tag-explains", ("body.yaml", "soul.yaml", "[move.approach] I will stay here."), documents()),
    Case("say-keys", ("body.yaml", "soul.yaml", "Hello.", "--keys", "keys.env"),
         {**documents(), "keys.env": "EMET_SNAPSHOT_KEY=not-a-real-secret\n"}, setup=private_keys),
    Case("say-cloud-voice", ("body.yaml", "soul.yaml", "Hello.", "--to", "out.wav"),
         documents(soul_text=soul({"provider": "cloud", "model": "aura-test", "key_env": "EMET_SNAPSHOT_KEY"}))),
    Case("say-lost-sentence", ("body.yaml", "soul.yaml", "Fine. The POISON word.", "--to", "out.wav"),
         documents(body(models={"tts": {"provider": "mock", "params": {"fail_on": "POISON"}}}))),
    Case("say-voice-cannot-start", ("body.yaml", "soul.yaml", "Hello."),
         documents(body(models={"tts": {"provider": "mock", "params": {"fail_on_start": True}}}))),
    Case("say-no-voice", ("body.yaml", "soul.yaml", "Hello."), documents(soul_text=soul())),
    Case("say-voice-not-installed", ("body.yaml", "soul.yaml", "Hello."), documents(soul_text=soul({"provider": "nonesuch"}))),
    Case("say-sink-cannot-start", ("body.yaml", "soul.yaml", "Hello."),
         documents(body(output={"sink": "broken", "device": "none"}))),
    Case("say-to-a-missing-folder", ("body.yaml", "soul.yaml", "Hello.", "--to", "nowhere/out.wav"), documents()),
    Case("say-bad-chains", ("body.yaml", "soul.yaml", "Hello.", "--chains", "chains.yaml"), {**documents(), "chains.yaml": (
        "express.curiosity:\n  rungs:\n    - {actuator: {role: head}, action: tilt}\n"
        "    - {actuator: {voice: true}, action: sing}\n")}),
    Case("say-empty-text", ("body.yaml", "soul.yaml", "   "), documents()),
    Case("say-no-text", ("body.yaml", "soul.yaml"), documents()),
    Case("say-no-plugins", ("body.yaml", "soul.yaml", "Hello."), documents(), plugins=False),
]


async def _no_connection(*args: Any, **kwargs: Any) -> Any:
    raise OSError("an emet-say test opens no connection")


@pytest.fixture
def at(tmp_path, monkeypatch):
    """A directory to run in: no key in the environment, git never asked,
    no connection opened, and a terminal 80 columns wide."""
    monkeypatch.chdir(tmp_path)
    # Set, then removed: the undo removes whatever a run puts there.
    monkeypatch.setenv("EMET_SNAPSHOT_KEY", "")
    monkeypatch.delenv("EMET_SNAPSHOT_KEY")
    status = "# branch.oid eb55748541483e9769610526bbaa21bfbe439f0b\n# branch.head master\n"
    monkeypatch.setattr(cli, "_run_git", lambda args, directory: subprocess.CompletedProcess(["git", *args], 0, status, ""))
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", _no_connection)
    monkeypatch.setenv("COLUMNS", "80")
    return tmp_path


def prepare(at: Path, monkeypatch: pytest.MonkeyPatch, case: Case, chosen: PluginRegistry | None = None) -> None:
    """Write the case's files and point `emet-say` at the registry it is to discover."""
    for name, content in case.files.items():
        (at / name).write_text(content, encoding="utf-8")
    if chosen is None:
        chosen = registry() if case.plugins else PluginRegistry()
    monkeypatch.setattr(say, "PluginRegistry", SimpleNamespace(discover=lambda: chosen))
    if case.setup is not None:
        case.setup(monkeypatch, at)


@pytest.mark.parametrize("case", [pytest.param(c, id=c.name) for c in CASES])
def test_the_console_shows_this_run_as_its_snapshot_does(case, at, monkeypatch, capsys):
    prepare(at, monkeypatch, case)
    with terminal_logging():
        try:
            code = say.main(list(case.argv))
        except SystemExit as exc:
            code = exc.code
    captured = capsys.readouterr()
    compare(case.name, normalise(render(list(case.argv), code, captured.out, captured.err), at))


def test_every_snapshot_file_belongs_to_a_case():
    """A snapshot whose case was removed would sit in the folder unread."""
    names = {c.name for c in CASES}
    assert sorted(p.name for p in SNAPSHOTS.iterdir() if p.stem not in names or p.suffix != ".txt") == []


def test_a_rewrite_is_reported_as_a_skip_and_is_refused_under_ci(tmp_path, monkeypatch):
    """`EMET_UPDATE_SNAPSHOTS=1` left exported would otherwise turn every
    case green whatever it printed, and a CI run would rewrite what it is
    there to compare."""
    monkeypatch.setattr(sys.modules[__name__], "SNAPSHOTS", tmp_path)
    monkeypatch.setenv(UPDATE, "1")
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(pytest.skip.Exception, match="rewrote snapshots/say/case.txt"):
        compare("case", "what was printed\n")
    assert (tmp_path / "case.txt").read_text(encoding="utf-8") == "what was printed\n"

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(pytest.fail.Exception, match="under CI"):
        compare("other", "what was printed\n")
    assert not (tmp_path / "other.txt").exists()


def test_a_slow_callback_line_stays_out_and_other_asyncio_lines_stay_in(capsys):
    log = logging.getLogger("asyncio")
    with terminal_logging():
        log.warning("Executing %s took %.3f seconds", "<Task pending name='Task-1'>", 0.25)
        log.warning("an asyncio warning a person should see")
    assert capsys.readouterr().err.splitlines() == ["an asyncio warning a person should see"]
    assert _no_slow_callbacks not in log.filters


def test_the_normaliser_rewrites_emet_say_and_what_differs_by_machine(tmp_path):
    printed = (
        "emet-say: emet-sdk 0.5.7, emet-hal 0.5.7, emet-providers 0.5.7, emet-engine 0.5.7; revision 8de8152abcde, "
        "working tree differs\n"
        "  voice    mock (16000 Hz), on this body, from emet-providers 0.5.7\n"
        f"  sink     wav, {tmp_path / 'out.wav'}; no microphone opened\n"
        "  said     1 sentence(s); voice first 312 ms\n"
    )
    assert normalise(printed, tmp_path) == (
        "emet-say: emet-sdk <version>, emet-hal <version>, emet-providers <version>, emet-engine <version>; "
        "revision <revision>\n"
        "  voice    mock (16000 Hz), on this body, from emet-providers <version>\n"
        "  sink     wav, <tmp>/out.wav; no microphone opened\n"
        "  said     1 sentence(s); voice first <ms> ms\n"
    )


# ----------------------------------------------------------- behaviour


def _forbid(name: str) -> Callable[..., Any]:
    def load(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"emet-say called {name}{args}; it builds the voice and the sink only")

    return load


#: Two parts a manifest can declare: a light on the mock driver, and a drive
#: on the mock driver with differential kinematics, whose locomotion plugin
#: would start its motors.
PARTS = [
    {"id": "ring", "type": "light", "role": "ambient", "driver": {"plugin": "emet_hal.mock"}},
    {"id": "base", "type": "drive", "kinematics": "differential", "driver": {"plugin": "emet_hal.mock"}},
]


def test_it_builds_the_voice_and_the_sink_and_nothing_else(at, monkeypatch, capsys):
    """No wake engine, no microphone, no transcriber, no model and no part
    is built, here for a body whose source is its microphone and which
    declares a light and a drive; the run still says its words into the
    file."""
    manifest = yaml.safe_load(body(audio_input={"source": "microphone", "device": "default"}))
    manifest["capabilities"] = PARTS
    case = Case("only-voice-and-sink", ("body.yaml", "soul.yaml", KETTLE, "--to", "out.wav"),
                documents(yaml.safe_dump(manifest, sort_keys=False)))
    chosen = registry()
    for name in ("load_wake", "load_audio", "load_stt", "load_llm", "load_driver", "load_locomotion"):
        monkeypatch.setattr(chosen, name, _forbid(name))
    prepare(at, monkeypatch, case, chosen)
    assert say.main(list(case.argv)) == 0
    assert "no microphone opened" in capsys.readouterr().out
    assert (at / "out.wav").stat().st_size > 44


def test_to_writes_a_wav_at_the_voices_rate_whatever_the_manifest_names(at, monkeypatch, capsys):
    """The manifest names its speaker and `--to` wins: only the wav sink is
    built, and the file is mono 16-bit at the voice's rate, Piper's 22050 Hz
    here and so not the 16 kHz a sink opens at by default, with the words in
    the order they were said, the tag's filler first."""
    case = Case("to-wins", ("body.yaml", "soul.yaml", KETTLE, "--to", "kettle.wav"), documents(body(
        output={"sink": "speaker", "device": "default"},
        models={"tts": {"provider": "mock", "params": {"sample_rate": 22050}}})))
    chosen = registry()
    built: list[str] = []
    real = chosen.load_audio_out
    monkeypatch.setattr(chosen, "load_audio_out", lambda name: built.append(name) or real(name))
    prepare(at, monkeypatch, case, chosen)
    assert say.main(list(case.argv)) == 0
    assert built == ["wav"]
    with wave.open(str(at / "kettle.wav"), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
        pcm = w.readframes(w.getnframes())
    found = [pcm.find(word) for word in (b"hm?", b"The", b"kettle", b"has", b"boiled.")]
    assert -1 not in found and found == sorted(found)
    out = capsys.readouterr().out
    assert "  said     1 sentence(s) and 1 filler(s); voice first " in out


def test_voice_first_is_counted_from_the_handover_on_the_clock_it_is_given():
    """`voice first` is the wait from handing the text over to the first
    sound at the sink, which the snapshots normalise away. A clock that
    reads 100.0 s at the handover and 100.25 s at the first sound gives
    250 ms."""
    from emet_sdk.resolve import load_chains

    from emet_engine.body import Body
    from emet_engine.prompting import persona_lines
    from emet_engine.self_model import compile_self_model

    readings = [100.0, 100.25]

    def clock() -> float:
        return readings.pop(0) if len(readings) > 1 else readings[0]

    played: list[bytes] = []

    class Sink:
        async def play(self, pcm: bytes) -> None:
            played.append(pcm)

    bodiless = {**yaml.safe_load(body()), "capabilities": []}
    saying = say.Saying(
        CloudVoice({"provider": "cloud"}),
        Sink(),  # type: ignore[arg-type]
        body=Body(bodiless, registry(), chains=load_chains()),
        self_model=compile_self_model(bodiless, capabilities=(), locomotion=None,
                                      lines=persona_lines(yaml.safe_load(soul(MOCK)))),
        sample_rate=24000,
        clock=clock,
    )
    said = asyncio.run(saying.say("Hello."))
    assert played and said.first_ms == pytest.approx(250.0)


class RaisingVoice(VoicePlugin):
    """A voice from another package whose `start()` raises the supported
    error, which no shipped voice does: Piper and the Deepgram voice report
    through `health()` instead."""

    provider = "raising"

    async def start(self) -> None:
        raise PluginError("no such device")

    async def shutdown(self) -> None:
        pass

    def describe(self) -> VoiceDescriptor:
        return VoiceDescriptor(provider="raising", sample_rate=16000)

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        raise AssertionError("a voice that did not start was asked to speak")
        yield b""  # pragma: no cover


def test_a_voice_whose_start_raises_is_refused_in_one_line(at, monkeypatch, capsys):
    case = Case("raising", ("body.yaml", "soul.yaml", "Hello."), documents(soul_text=soul({"provider": "raising"})))
    chosen = PluginRegistry.from_entry_points([
        *[ep for name in ("emet-hal", "emet-providers") for ep in metadata.distribution(name).entry_points],
        EntryPoint("raising", f"{__name__}:RaisingVoice", GROUP_TTS),
    ])
    prepare(at, monkeypatch, case, chosen)
    with terminal_logging():
        assert say.main(list(case.argv)) == 1
    err = capsys.readouterr().err
    assert err.strip() == (
        "the voice 'raising' cannot start, so there is nothing to say the words with. It will not run. no such device"
    )


@pytest.mark.parametrize("text", ["**", "\U0001f60a \U0001f389", "[laughs]"])
def test_a_text_the_cleaning_pass_empties_is_refused_and_writes_no_file(text, at, monkeypatch, capsys):
    """Emoji or marks alone, or a stage direction the tag lifter drops,
    would boot the voice and write a WAV of no frames that `exit 0` calls
    a success. Refused as an empty text is, before anything boots."""
    case = Case("emptied", ("body.yaml", "soul.yaml", text, "--to", "out.wav"), documents())
    prepare(at, monkeypatch, case)
    assert say.main(list(case.argv)) == 2
    assert capsys.readouterr().err == "emet-say: the text to say is empty once its marks are removed\n"
    assert not (at / "out.wav").exists()


def test_a_tag_alone_is_still_said():
    """A text that is one tag has words to say, its chain's filler."""
    assert not say._nothing_to_say("[express.curiosity]")
    assert not say._nothing_to_say("**Hello**")


def test_a_tone_keeps_its_place_among_the_words(at, monkeypatch):
    """A chain whose voice rung is a tone plays it where its tag stood: the
    sentence before it is said first and the one after it follows."""
    chains = "express.curiosity:\n  rungs:\n    - {actuator: {voice: true}, action: tone, params: {preset: soft_click}}\n"
    case = Case("tone", ("body.yaml", "soul.yaml", "First words. [express.curiosity] Last words.",
                         "--to", "out.wav", "--chains", "chains.yaml"), {**documents(), "chains.yaml": chains})
    prepare(at, monkeypatch, case)
    assert say.main(list(case.argv)) == 0
    with wave.open(str(at / "out.wav"), "rb") as w:
        pcm = w.readframes(w.getnframes())
    click = tones.render("soft_click", 16000)
    first, tone, last = pcm.find(b"First"), pcm.find(click), pcm.find(b"Last")
    assert -1 not in (first, tone, last) and first < tone < last


def test_a_sentence_that_runs_into_a_tag_is_said_before_it(at, monkeypatch, capsys):
    """`boiled.[express.curiosity]` has no space after its full stop, and is
    still a sentence the tag follows, as in a streamed reply."""
    case = Case("glued", ("body.yaml", "soul.yaml", "It boiled.[express.curiosity] Shall I pour?", "--to", "out.wav"),
                documents())
    prepare(at, monkeypatch, case)
    assert say.main(list(case.argv)) == 0
    with wave.open(str(at / "out.wav"), "rb") as w:
        pcm = w.readframes(w.getnframes())
    found = [pcm.find(word) for word in (b"boiled.", b"hm?", b"Shall")]
    assert -1 not in found and found == sorted(found)
    assert "  said     2 sentence(s) and 1 filler(s)" in capsys.readouterr().out


def test_the_console_script_is_declared_in_the_pyproject():
    project = tomllib.loads((HERE.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"]["emet-say"] == "emet_engine.say:main"
    assert say.PROGRAM == "emet-say"


# ---------------------------------------------------------------- lines


def _origin(name: str, distribution: str, version: str) -> PluginOrigin:
    return PluginOrigin(group=GROUP_TTS, name=name, value=f"{name}_voice:Voice", distribution=distribution,
                        version=version)


@pytest.mark.parametrize(
    ("descriptor", "origin", "line"),
    [
        (VoiceDescriptor(provider="piper", model="en_US-ljspeech-medium", sample_rate=22050),
         _origin("piper", "emet-providers", "0.5.7"),
         "  voice    piper, en_US-ljspeech-medium (22050 Hz), on this body, from emet-providers 0.5.7"),
        (VoiceDescriptor(provider="deepgram", model="aura-2-thalia-en", sample_rate=24000,
                         endpoint="https://api.deepgram.com/v1/speak", key_env="EMET_DEEPGRAM_KEY"), None,
         "  voice    deepgram, aura-2-thalia-en (24000 Hz), at api.deepgram.com, key EMET_DEEPGRAM_KEY"),
        # A voice another package offers, written before `endpoint` existed:
        # its None says nothing of where its words go.
        (VoiceDescriptor(provider="acme", sample_rate=16000), _origin("acme", "acme-voice", "1.0"),
         "  voice    acme (16000 Hz), where it sends is not stated, from acme-voice 1.0"),
        (VoiceDescriptor(provider="mock", sample_rate=16000), None,
         "  voice    mock (16000 Hz), where it sends is not stated"),
    ],
    ids=["local", "cloud", "outside", "bare"],
)
def test_the_voice_line_reads_as_emet_talks(descriptor, origin, line):
    assert say.voice_line(descriptor.provider, descriptor, origin) == line


def test_the_sink_line_names_a_wav_sinks_file_and_nothing_else():
    assert say.sink_line("wav", {"params": {"path": "kettle.wav"}}) == "  sink     wav, kettle.wav; no microphone opened"
    assert say.sink_line("speaker", {"params": {"path": "ignored"}}) == "  sink     speaker; no microphone opened"


def test_the_said_line_counts_what_was_heard():
    spoken = (
        SpokenSentence("hm?", 64, from_reply=False),
        SpokenSentence("Fine.", 64),
        SpokenSentence("Lost.", 0, error="simulated"),
        SpokenSentence("Silent.", 0),
    )
    assert say.said_lines(say.Said(spoken, 12.4, added=(1,))) == [
        "  said     1 sentence(s) and 1 filler(s); voice first 12 ms",
        "  lost     the voice could not say 'Lost.': simulated",
    ]
    assert say.said_lines(say.Said((), None)) == ["  said     0 sentence(s); no sound reached the sink"]


def test_an_explanation_of_two_sentences_is_one_filler():
    """`[move.approach]` on a body with no wheels is explained in two
    sentences, and is one tag heard; a tag whose words were all lost is
    none."""
    spoken = (
        SpokenSentence("I cannot come to you.", 64, from_reply=False),
        SpokenSentence("I have no wheels.", 64, from_reply=False),
        SpokenSentence("hm?", 0, error="simulated", from_reply=False),
        SpokenSentence("Fine.", 64),
    )
    said = say.Said(spoken, 3.0, added=(2, 1))
    assert (said.sentences, said.fillers, len(said.lost)) == (1, 1, 1)
