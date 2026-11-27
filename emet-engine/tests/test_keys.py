"""The keys file: bring your own key, once, and keep it out of the shell."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import wave
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest

from emet_sdk.discovery import GROUP_AUDIO, GROUP_AUDIO_OUT, GROUP_LLM, GROUP_STT, GROUP_TTS, GROUP_WAKE, PluginRegistry

from emet_engine import cli, keys, talk
from emet_engine.keys import LoadedKeys, load_keys, parse_env


# ------------------------------------------------------------------ parsing


def test_the_plain_dotenv_subset_parses():
    text = """
    # Emet keys. One a line.
    EMET_DEEPGRAM_KEY=dg_abc
    export EMET_OPENAI_KEY="sk-quoted"
    EMET_ANTHROPIC_KEY='sk-single'   # trailing comment after quotes is ignored
    EMET_LOCAL_KEY=plain # a comment
    """
    assert parse_env(text) == {
        "EMET_DEEPGRAM_KEY": "dg_abc",
        "EMET_OPENAI_KEY": "sk-quoted",
        "EMET_ANTHROPIC_KEY": "sk-single",
        "EMET_LOCAL_KEY": "plain",
    }


def test_a_bad_line_names_its_number_and_stops():
    with pytest.raises(ValueError, match=r"keys.env:2"):
        parse_env("GOOD=1\nthis is not a key\n")
    with pytest.raises(ValueError, match="NAME=value"):
        parse_env("9BAD=1")


def test_values_are_one_token_and_never_interpolated():
    assert parse_env("A=$HOME/x")["A"] == "$HOME/x"
    assert parse_env("B==with=equals")["B"] == "=with=equals"
    assert parse_env('C="has # inside" # comment')["C"] == "has # inside"


def test_an_unclosed_quote_is_a_named_error():
    with pytest.raises(ValueError, match=r"keys.env:1.*unclosed quote"):
        parse_env("A='oops")


# ------------------------------------------------------------------ loading


def test_a_named_file_is_loaded_and_the_environment_wins(tmp_path):
    file = tmp_path / "keys.env"
    file.write_text("EMET_ONE=from_file\nEMET_TWO=from_file\n", encoding="utf-8")
    environ = {"EMET_TWO": "already exported"}

    (loaded,) = load_keys(file, environ=environ)

    assert environ == {"EMET_ONE": "from_file", "EMET_TWO": "already exported"}
    assert loaded == LoadedKeys(path=file, names=("EMET_ONE",), kept=("EMET_TWO",), warning=loaded.warning)


def test_a_named_file_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="no keys file"):
        load_keys(tmp_path / "nope.env", environ={})


def test_a_missing_file_says_to_undo_whatever_named_it(tmp_path):
    """`--keys` names the file, or `EMET_KEYS` does when no flag is given;
    the way back to the default places is to drop the one that was used."""
    missing = tmp_path / "nope.env"
    with pytest.raises(FileNotFoundError) as flag:
        load_keys(missing, environ={})
    assert str(flag.value) == (
        f"no keys file at {missing}. Create it with one NAME=value a line, or drop --keys to use the "
        f"default places."
    )
    with pytest.raises(FileNotFoundError) as variable:
        load_keys(environ={"EMET_KEYS": str(missing)})
    assert str(variable.value) == (
        f"no keys file at {missing}. Create it with one NAME=value a line, or unset EMET_KEYS to use the "
        f"default places."
    )


def test_emet_keys_in_the_environment_names_the_file(tmp_path):
    file = tmp_path / "elsewhere.env"
    file.write_text("EMET_X=1\n", encoding="utf-8")
    environ = {"EMET_KEYS": str(file)}
    (loaded,) = load_keys(environ=environ)
    assert loaded.path == file and environ["EMET_X"] == "1"


def test_the_persons_file_is_read_first_so_it_wins_over_the_machines(tmp_path, monkeypatch):
    """Nothing overwrites a variable once set, so the order of reading is the
    order of precedence: the shell, then the person, then the machine."""
    user = tmp_path / "home" / "keys.env"
    system = tmp_path / "etc" / "keys.env"
    user.parent.mkdir(parents=True)
    system.parent.mkdir(parents=True)
    user.write_text("EMET_SHARED=person\n", encoding="utf-8")
    system.write_text("EMET_SHARED=machine\nEMET_ONLY_SYSTEM=yes\n", encoding="utf-8")
    monkeypatch.setattr(keys, "default_paths", lambda: [user, system])

    environ: dict[str, str] = {"EMET_ONLY_SYSTEM": "shell"}
    loaded = load_keys(environ=environ)

    assert [l.path for l in loaded] == [user, system]
    assert environ["EMET_SHARED"] == "person"
    assert loaded[1].kept == ("EMET_SHARED", "EMET_ONLY_SYSTEM")
    assert environ["EMET_ONLY_SYSTEM"] == "shell"


def test_the_real_default_order_is_person_then_machine(monkeypatch):
    monkeypatch.undo()  # conftest empties default_paths for every test; look at the real one
    paths = keys.default_paths()
    assert paths == [keys.user_keys_path(), keys.SYSTEM_KEYS]


def test_no_file_anywhere_loads_nothing_and_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(keys, "default_paths", lambda: [tmp_path / "absent.env"])
    assert load_keys(environ={}) == []


def test_the_user_path_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert keys.user_keys_path() == tmp_path / "emet" / "keys.env"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert keys.user_keys_path() == Path.home() / ".config" / "emet" / "keys.env"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes")
def test_a_file_others_can_read_is_loaded_with_a_warning(tmp_path):
    file = tmp_path / "keys.env"
    file.write_text("EMET_K=1\n", encoding="utf-8")
    os.chmod(file, 0o644)
    (loaded,) = load_keys(file, environ={})
    assert loaded.warning and "chmod 600" in loaded.warning
    os.chmod(file, 0o600)
    (loaded,) = load_keys(file, environ={})
    assert loaded.warning is None


class _FileOfMode:
    """A path whose `stat()` reports a mode, so the check is held on every
    machine: Windows reports every plain file as 0o666 or 0o444."""

    def __init__(self, mode: int) -> None:
        self.mode = mode

    def stat(self) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFREG | self.mode)

    def __str__(self) -> str:
        return "keys.env"


@pytest.mark.parametrize(
    "mode, warns",
    [(0o600, False), (0o400, False), (0o640, True), (0o604, True), (0o644, True)],
    ids=["owner-only", "owner-read-only", "group-can-read", "others-can-read", "both-can-read"],
)
def test_a_file_the_group_or_others_can_read_warns_and_one_only_its_owner_reads_does_not(mode, warns, monkeypatch):
    """keys.py: "a file that group or others can read is loaded anyway, with
    a warning naming it and the `chmod 600` that fixes it"."""
    monkeypatch.setattr(keys, "sys", SimpleNamespace(platform="linux"))
    warning = keys._permissions_warning(_FileOfMode(mode))
    if warns:
        assert warning == "keys.env is readable by other users on this machine. Keys belong to you alone: chmod 600 keys.env"
    else:
        assert warning is None


def test_values_never_appear_in_what_is_reported(tmp_path):
    file = tmp_path / "keys.env"
    file.write_text("EMET_SECRET=hunter2\n", encoding="utf-8")
    (loaded,) = load_keys(file, environ={})
    assert "hunter2" not in repr(loaded)


# ------------------------------------------------------ a line that does not parse

#: A value shaped like a vendor's key: a prefix, a dash, and a tail of mixed
#: case and digits that no message holds by chance.
SECRET = "dg-Zq7XvW4mRt2Kd8Lp3Nf6Hy9Bc"

#: A key of letters and digits alone, as a base64 key is before its padding.
#: Unlike `SECRET` it is a legal variable name, so a pasted copy reaches the
#: `=` that follows it.
BARE = SECRET[3:]

#: Lines that do not parse, each with `SECRET` or `BARE` where a key would
#: sit. The first five are a key pasted on its own, with no name in front.
MALFORMED = {
    "pasted-alone": SECRET,
    "pasted-alone-ending-in-equals": f"{SECRET}==",
    "pasted-alone-name-characters-and-padding": f"{BARE}==",
    "pasted-alone-padding-and-a-comment": f"{BARE}== # pasted here",
    "pasted-alone-after-export": f"export {BARE}===",
    "dash-in-the-name": f"EMET-DEEPGRAM-KEY={SECRET}",
    "space-in-the-name": f"EMET DEEPGRAM KEY={SECRET}",
    "digit-first": f"9EMET_DEEPGRAM_KEY={SECRET}",
    "no-name": f"={SECRET}",
    "export-and-a-dash": f"export EMET-DEEPGRAM-KEY={SECRET}",
    "space-for-the-equals": f"EMET_DEEPGRAM_KEY {SECRET}",
    "colon-for-the-equals": f"EMET_DEEPGRAM_KEY: {SECRET}",
    "unclosed-quote": f'EMET_DEEPGRAM_KEY="{SECRET}',
}


def shown(text: str) -> list[str]:
    """Every run of six characters of `SECRET` that `text` holds. A message
    may hold none of them."""
    return [SECRET[i : i + 6] for i in range(len(SECRET) - 5) if SECRET[i : i + 6] in text]


@pytest.mark.parametrize("line", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_line_that_does_not_parse_names_its_number_and_never_its_value(line):
    with pytest.raises(ValueError) as caught:
        parse_env(f"EMET_GOOD=1\n{line}\n")
    error = caught.value
    assert str(error).startswith("keys.env:2: ")
    assert str(error).endswith(f" {keys._UNSHOWN}")
    assert shown(f"{error} {error!r}") == []
    # Nothing chained either, so a traceback has no value to print.
    assert error.__cause__ is None and error.__context__ is None


def test_the_variable_is_named_only_for_an_unclosed_quote():
    with pytest.raises(ValueError) as caught:
        parse_env(MALFORMED["unclosed-quote"])
    assert str(caught.value) == (
        "keys.env:1: unclosed quote in the value for EMET_DEEPGRAM_KEY. Close the quote or remove it. "
        "The line is not shown, since it may hold a key."
    )


def test_a_value_of_padding_alone_is_refused_without_naming_the_text_before_it():
    """keys.py: "An unquoted value made only of `=` signs is refused", since
    the text before it is a base64 key pasted on its own. A quoted one was
    written on purpose and stays a value."""
    with pytest.raises(ValueError) as caught:
        parse_env(f"{BARE}==")
    assert str(caught.value) == (
        "keys.env:1: expected NAME=value, and the value is only '=' signs, the way a key pasted on its "
        "own ends. The line is not shown, since it may hold a key."
    )
    assert parse_env('EMET_ODD="=="') == {"EMET_ODD": "=="}


def test_an_empty_value_sets_nothing_and_names_nothing(tmp_path):
    """A key with one `=` of padding pasted alone reads as a name with an
    empty value, as a placeholder line does. Neither is a key, and the first
    is a key's text, so neither reaches the environment or `names`."""
    file = tmp_path / "keys.env"
    file.write_text(f"EMET_GOOD=1\n{BARE}=\nEMET_LATER=\nEMET_QUOTED=''\n", encoding="utf-8")
    environ: dict[str, str] = {}
    (loaded,) = load_keys(file, environ=environ)
    assert environ == {"EMET_GOOD": "1"}
    assert loaded == LoadedKeys(path=file, names=("EMET_GOOD",), kept=(), warning=loaded.warning)
    assert shown(repr(loaded)) == []


def test_a_byte_order_mark_is_read_past(tmp_path):
    """Notepad's "UTF-8 with BOM" and Windows PowerShell 5.1's
    `Set-Content -Encoding UTF8` write EF BB BF first. Read as plain UTF-8 the
    mark sits inside the first name, which then fails as no variable name."""
    file = tmp_path / "keys.env"
    file.write_bytes(b"\xef\xbb\xbfEMET_DEEPGRAM_KEY=abc\n")
    environ: dict[str, str] = {}
    (loaded,) = load_keys(file, environ=environ)
    assert environ == {"EMET_DEEPGRAM_KEY": "abc"} and loaded.names == ("EMET_DEEPGRAM_KEY",)

    file.write_bytes(b"\xef\xbb\xbf# my keys\nEMET_DEEPGRAM_KEY=abc\n")
    environ = {}
    load_keys(file, environ=environ)
    assert environ == {"EMET_DEEPGRAM_KEY": "abc"}

    file.write_bytes(b"\xef\xbb\xbfEMET_GOOD=1\nEMET_DEEPGRAM_KEY=" + SECRET.encode() + bytes([0xE9]) + b"\n")
    with pytest.raises(ValueError, match=r":2: not UTF-8 text\."):
        load_keys(file, environ={})


def test_a_byte_that_is_not_utf8_is_named_by_its_line_and_never_shown(tmp_path):
    """The decoder's own message names the byte, and here the byte is part
    of a key."""
    file = tmp_path / "keys.env"
    file.write_bytes(b"EMET_GOOD=1\nEMET_DEEPGRAM_KEY=" + SECRET.encode() + bytes([0xE9]) + b"\n")
    with pytest.raises(ValueError) as caught:
        load_keys(file, environ={})
    error = caught.value
    assert str(error) == (
        f"{file}:2: not UTF-8 text. Save the file as UTF-8. The line is not shown, since it may hold a key."
    )
    assert shown(repr(error)) == []
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize("program", ["emet-listen", "emet-talk"])
def test_a_planted_value_reaches_no_output_of_either_command(program, tmp_path, monkeypatch, capsys, caplog):
    """Each malformed line, and a byte that is not UTF-8, through the command
    a person runs: the run stops, naming the file and the line, and the value
    is in neither stream nor in any log record."""
    registry = PluginRegistry(wake={"mock": EntryPoint("mock", "emet_hal.mock:MockWake", GROUP_WAKE)})
    for module in (cli, talk):
        monkeypatch.setattr(module, "PluginRegistry", SimpleNamespace(discover=lambda: registry))
    monkeypatch.setattr(cli, "_run_git", lambda args, directory: subprocess.CompletedProcess(args, 128, "", ""))
    caplog.set_level(logging.DEBUG)
    main = talk.main if program == "emet-talk" else cli.main
    file = tmp_path / "keys.env"
    contents = {name: f"EMET_GOOD=1\n{line}\n".encode() for name, line in MALFORMED.items()}
    contents["not-utf8"] = b"EMET_GOOD=1\nEMET_DEEPGRAM_KEY=" + SECRET.encode() + bytes([0xE9]) + b"\n"
    for name, content in contents.items():
        file.write_bytes(content)
        code = main(["body.yaml", "soul.yaml", "--keys", str(file)])
        out, err = capsys.readouterr()
        assert code == 1, name
        assert f"{file}:2: " in err, name
        assert shown(out + err + caplog.text) == [], name
        assert "0xe9" not in err, name


# ------------------------------------------------------ a file that loads

#: A body on the mock path: a wav file for the microphone, the null sink and
#: the mock wake engine.
BODY = """manifest_version: "0.1"
body: {id: probe, scale: desk, power: plugged_in}
audio:
  input: {source: wav, device: file, params: {path: take.wav}}
  output: {sink: "null", device: none}
  wake: {engine: mock}
"""

#: A soul whose three stages are the mock providers, so `emet-talk` boots.
SOUL = """bundle_version: "0.1"
identity: {name: Emet, wake_word: hey emet, line: emet}
models:
  stt: {provider: mock}
  chat: {provider: mock}
  tts: {provider: mock}
"""


def _quiet_take() -> bytes:
    """Three frames of silence at 16 kHz: a run that boots, hears nothing, ends."""
    import io

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(2 * 1280 * 3))
    return out.getvalue()


@pytest.mark.parametrize("program", ["emet-listen", "emet-talk"])
def test_a_loaded_key_is_named_on_the_banner_and_its_value_is_never_shown(program, tmp_path, monkeypatch, capsys, caplog):
    """keys.py: "Values never appear in a log, a header line or an error.
    Names do." Both commands boot to the banner on the mock path, from a
    keys file holding one good line and a key pasted alone with one `=` of
    padding. The banner names the good line's variable, and neither the key
    nor the pasted text is in either stream or any log record."""

    def group(name: str, plugins: dict[str, str]) -> dict[str, EntryPoint]:
        return {n: EntryPoint(n, value, name) for n, value in plugins.items()}

    registry = PluginRegistry(
        wake=group(GROUP_WAKE, {"mock": "emet_hal.mock:MockWake"}),
        audio=group(GROUP_AUDIO, {"wav": "emet_hal.audio:WavSource"}),
        audio_out=group(GROUP_AUDIO_OUT, {"null": "emet_hal.audio:NullSink"}),
        stt=group(GROUP_STT, {"mock": "emet_providers.mock:MockTranscriber"}),
        llm=group(GROUP_LLM, {"mock": "emet_providers.mock:MockLanguageModel"}),
        tts=group(GROUP_TTS, {"mock": "emet_providers.mock:MockVoice"}),
    )
    for module in (cli, talk):
        monkeypatch.setattr(module, "PluginRegistry", SimpleNamespace(discover=lambda: registry))
    monkeypatch.setattr(cli, "_run_git", lambda args, directory: subprocess.CompletedProcess(args, 128, "", ""))
    for name in ("EMET_BANNER_KEY", BARE):
        # Set, then removed: the undo removes whatever the run puts there.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "body.yaml").write_text(BODY, encoding="utf-8")
    (tmp_path / "soul.yaml").write_text(SOUL, encoding="utf-8")
    (tmp_path / "take.wav").write_bytes(_quiet_take())
    file = tmp_path / "keys.env"
    file.write_text(f"EMET_BANNER_KEY={SECRET}\n{BARE}=\n", encoding="utf-8")
    caplog.set_level(logging.DEBUG)

    main = talk.main if program == "emet-talk" else cli.main
    code = main(["body.yaml", "soul.yaml", "--keys", str(file)])
    out, err = capsys.readouterr()

    assert code == 0, err
    assert f"  keys     EMET_BANNER_KEY from {file}" in out.splitlines()
    assert shown(out + err + caplog.text) == []
