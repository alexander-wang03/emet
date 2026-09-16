"""The keys file: bring your own key, once, and keep it out of the shell."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from emet_engine import keys
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


def test_values_never_appear_in_what_is_reported(tmp_path):
    file = tmp_path / "keys.env"
    file.write_text("EMET_SECRET=hunter2\n", encoding="utf-8")
    (loaded,) = load_keys(file, environ={})
    assert "hunter2" not in repr(loaded)
