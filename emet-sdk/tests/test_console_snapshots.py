"""Console snapshots of `emet validate` and `emet explain` (RP11, stage 1).

One test a failure state: each way the two commands refuse or fail with a
message a builder reads. The test writes the documents it needs into a
temporary directory, runs `emet` there with relative paths, and compares
what was printed with a file under `tests/snapshots/`. The snapshot is the
whole of what a person sees: the command, the exit code, everything on
stdout and everything on stderr. Only what differs by machine is replaced:
Windows' line ends become line feeds, the temporary directory becomes `<tmp>`, a
Windows backslash between two parts of a path becomes `/`, and the usage
line argparse prints, whose wrapping changes between Python versions,
becomes one line that says so.

The registry is fixed (`registry()` below, the names the four packages
ship, held to their `pyproject.toml` files by a test), so a missing-plugin
message lists the same plugins on every machine.

A mismatch prints a diff and the command that rewrites the snapshot.
`EMET_UPDATE_SNAPSHOTS=1` is read by these tests only, so a changed
message is a deliberate change, reviewed as a changed file. A case it
rewrites is reported as skipped, never as passed, and under CI (`CI` or
`GITHUB_ACTIONS` set) it fails instead. The list of failure states, with
where each is raised and the states no test reaches, is in
`releases/v0.5.6/reports/B7.md` outside the repository.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest

from emet_sdk import cli
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

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE / "snapshots"
EXAMPLES = HERE.parent / "examples"
FILE = "tests/test_console_snapshots.py"
UPDATE = "EMET_UPDATE_SNAPSHOTS"

#: What the four packages register, by group, as their `pyproject.toml`
#: files say; `test_the_shipped_plugins_are_the_ones_the_pyproject_files_register`
#: holds the copy to them. Never loaded here: `emet` only asks whether a
#: name is installed.
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


def registry() -> PluginRegistry:
    """The plugins the four packages ship, and nothing a machine adds."""

    def group(name: str) -> dict[str, EntryPoint]:
        return {n: EntryPoint(n, value, name) for n, value in SHIPPED[name].items()}

    return PluginRegistry(
        actuators=group(GROUP_ACTUATOR), sensors=group(GROUP_SENSOR), locomotion=group(GROUP_LOCOMOTION),
        wake=group(GROUP_WAKE), audio=group(GROUP_AUDIO), audio_out=group(GROUP_AUDIO_OUT),
        stt=group(GROUP_STT), llm=group(GROUP_LLM), tts=group(GROUP_TTS),
    )


# ------------------------------------------------------------- snapshots


def normalise(text: str, tmp: Path) -> str:
    """What differs by machine, replaced: the temporary directory, as
    written and as a repr writes it; Windows' separator inside a path; and
    the usage lines argparse prints before an error."""
    text = text.replace("\r\n", "\n")
    for form in (repr(str(tmp))[1:-1], str(tmp), tmp.as_posix()):
        text = text.replace(form, "<tmp>")
    if os.sep == "\\":
        text = re.sub(r"(?<=[\w.>])\\{1,2}(?=[\w.<])", "/", text)
    return re.sub(r"(?ms)^usage: .*?(?=^[\w .-]+: error: )",
                  "usage: (argparse's usage lines, which differ by Python version)\n", text)


def block(text: str) -> str:
    if not text:
        return "(nothing)\n"
    return text if text.endswith("\n") else text + "\n"


def render(argv: list[str], code: int, out: str, err: str) -> str:
    return f"$ emet {' '.join(argv)}\nexit {code}\n--- stdout\n{block(out)}--- stderr\n{block(err)}"


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
            fromfile=f"snapshots/{name}.txt", tofile="what emet printed"))
        pytest.fail(f"the console changed for {name}:\n{diff}\nIf the change is meant, rewrite the snapshot "
                    f"and review the diff:\n    {command}", pytrace=False)


# ----------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    """One failure state: the files it needs and the command that meets it."""

    name: str
    argv: tuple[str, ...]
    files: dict[str, str] = field(default_factory=dict)
    examples: tuple[str, ...] = ()
    memory: dict[str, tuple[str, ...]] = field(default_factory=dict)


AUDIO = """audio:
  input: {device: "plughw:1,0", sample_rate: 16000}
  output: {device: "plughw:1,0"}
"""


def manifest(body_id: str, rest: str = "", audio: str = AUDIO) -> str:
    return (f'manifest_version: "0.1"\nbody: {{id: {body_id}, scale: desk, power: plugged_in}}\n'
            f"{audio}{rest}")


SOUL = """bundle_version: "0.1"
identity: {name: Emet, wake_word: "hey emet", line: emet}
interaction: {barge_in: true}
"""


def chain(intent: str, voice: str) -> str:
    return f"{intent}:\n  rungs:\n    - actuator: {{role: head}}\n      action: tilt\n    - {voice}\n"


UNWIRED = manifest("unwired", """capabilities:
  - id: ring
    type: light
    role: ambient
    driver: {plugin: acme_ws2812}
""")

#: The plain manifest's audio, with one plugin nothing installs in place of
#: the default microphone or speaker.
AUDIO_FROM_ALSA = """audio:
  input: {device: "plughw:1,0", source: alsa_raw}
  output: {device: "plughw:1,0"}
"""
AUDIO_TO_BLUETOOTH = """audio:
  input: {device: "plughw:1,0"}
  output: {device: "plughw:1,0", sink: bluetooth}
"""

CASES = [
    *(Case(f"validate-{Path(name).stem}", (name,), examples=(name,))
      for name in sorted(p.name for p in (EXAMPLES / "invalid").glob("*.yaml"))),
    Case("validate-invalid-range", ("narrow.yaml",), {"narrow.yaml": manifest("narrow", """capabilities:
  - id: head
    type: joint_group
    role: head
    driver: {plugin: emet_hal.mock}
    joints:
      - {id: pan, axis: yaw, range_deg: [10, 10]}
""")}),
    Case("validate-missing-file", ("absent.yaml",)),
    Case("validate-unknown-document", ("notes.yaml",), {"notes.yaml": "title: shopping\nitems: [eggs, milk]\n"}),
    Case("validate-unreadable", ("broken.yaml",), {"broken.yaml": "manifest_version: \"0.1\"\nbody: [desk\n"}),
    Case("validate-driver-strict", ("--strict", "unwired.yaml"), {"unwired.yaml": UNWIRED}),
    Case("validate-verify-drivers", ("--verify-drivers", "unwired.yaml"), {"unwired.yaml": UNWIRED}),
    Case("validate-unknown-audio-source", ("alsa.yaml",), {"alsa.yaml": manifest("alsa", audio=AUDIO_FROM_ALSA)}),
    Case("validate-unknown-audio-sink", ("bluetooth.yaml",),
         {"bluetooth.yaml": manifest("bluetooth", audio=AUDIO_TO_BLUETOOTH)}),
    Case("validate-soul-schema", ("soul.yaml",),
         {"soul.yaml": 'bundle_version: "0.1"\nidentity: {name: Emet, wake_word: "hey emet"}\n'}),
    Case("validate-pair-barge-in", ("--pair", "body.yaml", "soul.yaml"),
         {"body.yaml": manifest("no_aec", audio='audio:\n  input: {device: default, aec: none}\n'
                                                '  output: {device: default}\n'),
          "soul.yaml": SOUL}),
    Case("validate-bundle-no-soul", ("emet.emet",), {"emet.emet/memory.txt": "nothing here\n"}),
    Case("validate-memory-body-column", ("memory.db",),
         memory={"memory.db": ("CREATE TABLE memories (id INTEGER, text TEXT, body_id TEXT)",)}),
    Case("validate-memory-not-a-database", ("junk.db",), {"junk.db": "this is a text file, not SQLite\n" * 8}),
    Case("validate-motion-pack", ("wave.pack.yaml",), {"wave.pack.yaml": """pack_version: "0.1"
name: waves
clips:
  wave:
    intent: express.glee
    keyframes:
      - {t_ms: 0, joints: {pan: 0}}
      - {t_ms: 400, joints: {pan: 10}}
  nod:
    intent: express.curiosity
    keyframes:
      - {t_ms: 400, joints: {tilt: 10}}
      - {t_ms: 100, joints: {tilt: 0}}
"""}),
    Case("validate-chain-voice-action", ("chains.yaml",),
         {"chains.yaml": chain("express.curiosity", "{actuator: {voice: true}, action: sing}")}),
    Case("validate-chain-unknown-intent", ("chains.yaml",),
         {"chains.yaml": chain("express.glee", "{actuator: {voice: true}, action: say}")}),
    Case("validate-chain-voice-not-last", ("chains.yaml",), {"chains.yaml": """express.curiosity:
  rungs:
    - {actuator: {voice: true}, action: say}
    - {actuator: {role: head}, action: tilt}
    - {actuator: {voice: true}, action: say}
"""}),
    Case("validate-chain-explain-topic", ("chains.yaml",),
         {"chains.yaml": chain("express.curiosity", "{actuator: {voice: true}, action: explain, params: {topic: weather}}")}),
    Case("validate-chain-tone", ("chains.yaml",),
         {"chains.yaml": chain("express.curiosity", "{actuator: {voice: true}, action: tone, params: {preset: chime}}")}),
    Case("validate-chain-inflect", ("chains.yaml",),
         {"chains.yaml": chain("express.curiosity", "{actuator: {voice: true}, action: inflect, params: {filler: []}}")}),
    Case("validate-json", ("--json", "duplicate-id.yaml"), examples=("duplicate-id.yaml",)),
    Case("validate-usage", ()),
    Case("explain-missing-file", ("absent.yaml",)),
    Case("explain-unreadable", ("broken.yaml",), {"broken.yaml": "manifest_version: \"0.1\"\nbody: [desk\n"}),
    Case("explain-not-a-manifest", ("soul.yaml",), {"soul.yaml": SOUL}),
    Case("explain-invalid-manifest", ("two-drives.yaml",), examples=("two-drives.yaml",)),
    Case("explain-bad-chains", ("body.yaml", "--chains", "chains.yaml"),
         {"body.yaml": manifest("plain"), "chains.yaml": chain("express.curiosity", "{actuator: {voice: true}, action: sing}")}),
]


def command(case: Case) -> list[str]:
    verb = "explain" if case.name.startswith("explain-") else "validate"
    return [verb, *case.argv]


@pytest.fixture
def at(tmp_path, monkeypatch):
    """A directory to run `emet` in, with the shipped plugins and nothing
    else installed, and a terminal 80 columns wide."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_Registry", SimpleNamespace(discover=registry))
    monkeypatch.setenv("COLUMNS", "80")
    return tmp_path


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_the_console_shows_this_failure_as_its_snapshot_does(case, at, capsys):
    for name, text in case.files.items():
        (at / name).parent.mkdir(parents=True, exist_ok=True)
        (at / name).write_text(text, encoding="utf-8")
    for name in case.examples:
        shutil.copy(EXAMPLES / "invalid" / name, at / name)
    for name, statements in case.memory.items():
        db = sqlite3.connect(at / name)
        try:
            for statement in statements:
                db.execute(statement)
            db.commit()
        finally:
            db.close()

    argv = command(case)
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    compare(case.name, normalise(render(argv, code, captured.out, captured.err), at))


def test_every_snapshot_file_belongs_to_a_case():
    """A snapshot whose case was removed would sit in the folder unread."""
    names = {c.name for c in CASES}
    assert sorted(p.stem for p in SNAPSHOTS.glob("*.txt") if p.stem not in names) == []


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

    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="under CI"):
        compare("other", "what was printed\n")
    assert not (tmp_path / "other.txt").exists()
