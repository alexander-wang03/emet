"""Keys, kept out of the shell history and out of every document.

Emet is bring-your-own-key. A soul names the environment variable each
provider reads (`key_env`); the value has to come from somewhere, and
"export it in every new shell" is how keys end up in shell history, in chat
logs and in screenshots. `keys.env` is the answer: one file, `NAME=value` a
line, read once at start-up and put into the environment for the plugins to
find. The environment still wins: a variable that is already exported is
never overwritten, so a one-off key for one run works as it always did.

**Where it lives.** `--keys PATH` on the command line, or the `EMET_KEYS`
variable, names one file and it must exist. Otherwise the two default places
are tried: `~/.config/emet/keys.env` for a person (`XDG_CONFIG_HOME` honoured
when set), and `/etc/emet/keys.env` for a system install (DESIGN.md section 3
puts the config root at `/etc/emet/`). Both are read when both exist. Nothing
ever overwrites a variable that is already set, so the personal file is read
first and its keys win over the machine's, and both lose to the shell.

**Format.** The plain subset of dotenv: `NAME=value`, an optional `export `
in front, `#` comments, blank lines, and a value in single or double quotes
has the quotes removed. No interpolation and no multi-line values: a key is
one token, and a file that needs more than that is not a keys file.

**Permissions.** The file should be readable by its owner alone. On POSIX a
file that group or others can read is loaded anyway, with a warning naming
it and the `chmod 600` that fixes it. Refusing would strand somebody on their
first evening; a warning they see every run teaches the habit.

Values never appear in a log, a header line or an error. Names do.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

__all__ = ["LoadedKeys", "SYSTEM_KEYS", "default_paths", "parse_env", "load_keys", "user_keys_path"]

#: The machine's keys, for an installed robot.
SYSTEM_KEYS = Path("/etc/emet/keys.env")

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def user_keys_path() -> Path:
    """A person's keys: `$XDG_CONFIG_HOME/emet/keys.env`, or `~/.config/emet/keys.env`."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "emet" / "keys.env"


def default_paths() -> list[Path]:
    """Where keys are looked for when nothing names a file. The personal file
    first: a variable once set is never overwritten, so first read wins."""
    return [user_keys_path(), SYSTEM_KEYS]


@dataclass(frozen=True, slots=True)
class LoadedKeys:
    """What one file contributed. Names only, never values."""

    path: Path
    #: Variables this file put into the environment.
    names: tuple[str, ...]
    #: Variables the file held that the environment already had. Left alone.
    kept: tuple[str, ...]
    #: A permissions warning, or None.
    warning: str | None = None


def parse_env(text: str, *, source: str = "keys.env") -> dict[str, str]:
    """Parse the dotenv subset described in the module docstring.

    Raises `ValueError` naming the line for anything else: a bad keys file
    should stop the robot with a line number, not run without a key and fail
    later in a vendor's words.
    """
    out: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _NAME.match(name):
            raise ValueError(
                f"{source}:{number}: expected NAME=value, got {raw.strip()!r}. "
                f"One key a line, no spaces around the name."
            )
        value = value.strip()
        if value[:1] in ("'", '"'):
            # A quoted value ends at its closing quote; a comment may follow.
            end = value.find(value[0], 1)
            if end == -1:
                raise ValueError(f"{source}:{number}: unclosed quote in the value for {name}")
            value = value[1:end]
        elif " #" in value:
            # An unquoted value ends at a trailing comment.
            value = value.split(" #", 1)[0].rstrip()
        out[name] = value
    return out


def _permissions_warning(path: Path) -> str | None:
    if sys.platform.startswith("win"):
        return None
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return (
            f"{path} is readable by other users on this machine. Keys belong to "
            f"you alone: chmod 600 {path}"
        )
    return None


def load_keys(
    path: Path | str | None = None,
    *,
    environ: MutableMapping[str, str] = os.environ,
) -> list[LoadedKeys]:
    """Read the keys file(s) into `environ`, never overwriting what is there.

    With `path`, or `EMET_KEYS` in the environment, that one file is read and
    must exist (`FileNotFoundError`). Otherwise every default file that exists
    is read, in order. Returns one `LoadedKeys` per file read, so a caller can
    say which names came from where without ever saying what they were.
    """
    named = path if path is not None else environ.get("EMET_KEYS")
    if named:
        candidates = [Path(named).expanduser()]
        if not candidates[0].is_file():
            raise FileNotFoundError(
                f"no keys file at {candidates[0]}. Create it with one NAME=value a "
                f"line, or drop --keys to use the default places."
            )
    else:
        candidates = [p for p in default_paths() if p.is_file()]

    loaded: list[LoadedKeys] = []
    for candidate in candidates:
        values = parse_env(candidate.read_text(encoding="utf-8"), source=str(candidate))
        names: list[str] = []
        kept: list[str] = []
        for name, value in values.items():
            if environ.get(name):
                kept.append(name)
                continue
            environ[name] = value
            names.append(name)
        loaded.append(
            LoadedKeys(
                path=candidate,
                names=tuple(names),
                kept=tuple(kept),
                warning=_permissions_warning(candidate),
            )
        )
    return loaded
