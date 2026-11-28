"""The API diff, `tools/api_diff.py`, against small trees written here.

Each test writes one small repository twice, a base and a head, changes one
thing in the head, and reads the diff of the two listings. Both sides are
read from disk with `DiskTree`, so no test needs git; the one class that
asks git, `GitTree`, is tested with `git_output` replaced, since that
function is the only place the tool runs git.

The last tests read the real tree: every surface the tool promises is in its
listing, it runs against the checkout's own HEAD, and the static reader
names the same public things as `emet-sdk/tests/test_public_api.py`, which
reads them from the imported package.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import api_diff  # noqa: E402  a tool, imported by its bare name from tools/

SCHEMA = {
    "$id": "https://emet.invalid/schemas/demo.schema.json",
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "P0. What the body is called."},
        "battery": {
            "type": "object",
            "description": "RSV. Reserved for battery-aware behaviour.",
            "properties": {"volts": {"type": "number", "minimum": 0}},
        },
        "parts": {"type": "array", "description": "P0. What the body has.", "items": {"$ref": "#/$defs/part"}},
        "clips": {
            "type": "object",
            "description": "P0. Recorded clips by name.",
            "propertyNames": {"pattern": "^[a-z]+$"},
            "additionalProperties": {"$ref": "#/$defs/clip"},
        },
        "delays": {"type": "array", "description": "P0.", "items": {"type": "number", "exclusiveMinimum": 0}},
        "odometry": {"enum": ["none", "encoders", "slam"], "description": "P0: none, encoders. RSV: slam."},
    },
    "$defs": {
        "part": {
            "type": "object",
            "required": ["id", "kind"],
            "properties": {"id": {"type": "string"}, "kind": {"enum": ["drive", "light"]}},
            "allOf": [{"if": {"properties": {"kind": {"const": "drive"}}}, "then": {"$ref": "#/$defs/drive"}}],
        },
        "drive": {"additionalProperties": False, "properties": {"wheels": {"type": "integer", "minimum": 2}}},
        "clip": {"type": "object", "properties": {"frames": {"type": "integer"}}},
    },
}

#: The base: one SDK with two contract modules and a command line, a HAL
#: with one entry point, an engine with two commands that share a helper's
#: flags, and one schema.
BASE: dict[str, str] = {
    "emet-sdk/pyproject.toml": """\
        [project]
        name = "emet-sdk"
        version = "1.0.0"
        requires-python = ">=3.11"
        dependencies = ["pyyaml>=6.0"]

        [project.optional-dependencies]
        dev = ["pytest>=8.0"]

        [project.scripts]
        emet = "emet_sdk.cli:main"
        """,
    "emet-sdk/emet_sdk/__init__.py": '''\
        """The contract."""
        from __future__ import annotations

        from .things import Part, speak

        __all__ = ["Part", "VERSION", "speak"]
        VERSION = "1.0"
        ''',
    "emet-sdk/emet_sdk/things.py": '''\
        """Things a plugin uses."""
        from __future__ import annotations

        from dataclasses import dataclass

        LIMIT = 3


        @dataclass(frozen=True)
        class Part:
            name: str
            size: int = 1
            tags: tuple[str, ...] = ()

            def describe(self, loud: bool = False) -> str:
                return self.name

            @property
            def label(self) -> str:
                return self.name

            def _hidden(self) -> None:
                return None


        class Gone:
            colour = "red"

            def fade(self) -> None:
                return None


        def speak(text: str, *, rate: float = 1.0) -> bytes:
            return text.encode()


        def _private() -> None:
            return None
        ''',
    "emet-sdk/emet_sdk/cli.py": '''\
        """The command line, outside the contract."""
        import argparse


        def run_everything() -> None:
            return None


        def build_parser() -> argparse.ArgumentParser:
            parser = argparse.ArgumentParser(prog="emet")
            sub = parser.add_subparsers(dest="command", required=True)
            validate = sub.add_parser("validate", help="check files")
            validate.add_argument("paths", nargs="+", metavar="PATH")
            validate.add_argument("--json", action="store_true", help="machine output")
            return parser


        def main(argv=None) -> int:
            parser = build_parser()
            parser.parse_args(argv)
            return 0
        ''',
    "emet-sdk/schemas/demo.schema.json": json.dumps(SCHEMA, indent=2) + "\n",
    "emet-hal/pyproject.toml": """\
        [project]
        name = "emet-hal"
        version = "1.0.0"
        dependencies = ["emet-sdk>=1.0.0"]

        [project.entry-points."emet.wake"]
        mock = "emet_hal.mock:MockWake"
        """,
    "emet-engine/pyproject.toml": """\
        [project]
        name = "emet-engine"
        version = "1.0.0"
        dependencies = ["emet-sdk>=1.0.0"]

        [project.scripts]
        emet-listen = "emet_engine.cli:main"
        emet-talk = "emet_engine.talk:main"
        """,
    "emet-engine/emet_engine/__init__.py": "",
    "emet-engine/emet_engine/cli.py": """\
        import argparse


        def add_body_flags(parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--state", metavar="FILE", help="where the state lives")


        def main(argv=None) -> int:
            parser = argparse.ArgumentParser(prog="emet-listen")
            parser.add_argument("manifest", help="body manifest")
            playback = parser.add_mutually_exclusive_group()
            playback.add_argument("--speak", action="store_true", help="say it")
            playback.add_argument("--echo", action="store_true", help="play it back")
            add_body_flags(parser)
            parser.parse_args(argv)
            return 0
        """,
    "emet-engine/emet_engine/talk.py": """\
        import argparse

        from .cli import add_body_flags


        def main(argv=None) -> int:
            parser = argparse.ArgumentParser(prog="emet-talk")
            parser.add_argument("manifest", help="body manifest")
            add_body_flags(parser)
            parser.parse_args(argv)
            return 0
        """,
}


def build(root: Path, files: dict[str, str] = BASE) -> Path:
    for path, text in files.items():
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")
    return root


def edit(root: Path, path: str, old: str, new: str) -> None:
    full = root / path
    text = full.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {path}"
    full.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def listing(root: Path) -> dict[str, str]:
    return api_diff.listing(api_diff.DiskTree(root))


@pytest.fixture
def sides(tmp_path: Path) -> tuple[Path, Path]:
    return build(tmp_path / "base"), build(tmp_path / "head")


def diff_of(sides: tuple[Path, Path]) -> list[str]:
    base, head = sides
    return api_diff.report(listing(base), listing(head), "base")


# --------------------------------------------------------------------------
# What a change prints
# --------------------------------------------------------------------------


def test_no_change_gives_the_one_line(sides):
    assert diff_of(sides) == ["api: no change against base"]


def test_an_added_function_is_shown(sides):
    edit(sides[1], "emet-sdk/emet_sdk/things.py", "\n\ndef _private",
         "\n\ndef listen(seconds: float = 2.0) -> str:\n    return ''\n\n\ndef _private")
    assert diff_of(sides) == [
        "api: 1 added, 0 removed, 0 changed against base",
        "added:",
        "  + sdk emet_sdk.things.listen: def (seconds: float=2.0) -> str",
    ]


def test_a_removed_class_is_shown_with_every_member(sides):
    edit(sides[1], "emet-sdk/emet_sdk/things.py",
         'class Gone:\n    colour = "red"\n\n    def fade(self) -> None:\n        return None\n', "")
    assert diff_of(sides) == [
        "api: 0 added, 3 removed, 0 changed against base",
        "removed:",
        "  - sdk emet_sdk.things.Gone: class",
        "  - sdk emet_sdk.things.Gone.colour: attribute = 'red'",
        "  - sdk emet_sdk.things.Gone.fade: def (self) -> None",
    ]


def test_a_changed_default_shows_both_values(sides):
    edit(sides[1], "emet-sdk/emet_sdk/things.py", "rate: float = 1.0", "rate: float = 1.5")
    edit(sides[1], "emet-sdk/emet_sdk/things.py", "size: int = 1", "size: int = 2")
    assert diff_of(sides) == [
        "api: 0 added, 0 removed, 2 changed against base",
        "changed:",
        "  ~ sdk emet_sdk.things.Part.size",
        "      was: attribute: int = 1",
        "      now: attribute: int = 2",
        "  ~ sdk emet_sdk.things.speak",
        "      was: def (text: str, *, rate: float=1.0) -> bytes",
        "      now: def (text: str, *, rate: float=1.5) -> bytes",
    ]


def test_a_reordered_dataclass_field_is_a_change(sides):
    """The fields' order is the constructor's, so a caller passing them by
    position sees it."""
    edit(sides[1], "emet-sdk/emet_sdk/things.py",
         "    size: int = 1\n    tags: tuple[str, ...] = ()\n",
         "    tags: tuple[str, ...] = ()\n    size: int = 1\n")
    lines = diff_of(sides)
    assert lines[0] == "api: 0 added, 0 removed, 1 changed against base"
    assert lines[2:] == [
        "  ~ sdk emet_sdk.things.Part",
        "      was: @dataclass(frozen=True) class, fields in order: name, size, tags",
        "      now: @dataclass(frozen=True) class, fields in order: name, tags, size",
    ]


def test_a_new_entry_point_is_shown(sides):
    edit(sides[1], "emet-hal/pyproject.toml", 'mock = "emet_hal.mock:MockWake"',
         'mock = "emet_hal.mock:MockWake"\nsherpa = "emet_hal.sherpa:SherpaWake"')
    assert diff_of(sides) == [
        "api: 1 added, 0 removed, 0 changed against base",
        "added:",
        "  + entry-point emet.wake sherpa: emet_hal.sherpa:SherpaWake, from emet-hal",
    ]


def test_a_schema_property_whose_tag_changed_is_shown(sides):
    """The tag of a property, and of what sits under it untagged."""
    edit(sides[1], "emet-sdk/schemas/demo.schema.json",
         "RSV. Reserved for battery-aware behaviour.", "P0. Read at boot.")
    assert diff_of(sides) == [
        "api: 0 added, 0 removed, 2 changed against base",
        "changed:",
        "  ~ schema demo /properties/battery",
        "      was: object; optional; RSV",
        "      now: object; optional; P0",
        "  ~ schema demo /properties/battery/properties/volts",
        "      was: number; optional; untagged, under RSV; minimum=0",
        "      now: number; optional; untagged, under P0; minimum=0",
    ]


def test_a_property_that_stops_being_required_is_shown(sides):
    edit(sides[1], "emet-sdk/schemas/demo.schema.json", '"required": [\n    "name"\n  ]', '"required": []')
    assert diff_of(sides)[2:] == [
        "  ~ schema demo /properties/name",
        "      was: string; required; P0",
        "      now: string; optional; P0",
    ]


def edit_schema(root: Path, change) -> None:
    """Change the demo schema as data, and write it back as `build` does."""
    path = root / "emet-sdk/schemas/demo.schema.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def test_every_property_definition_and_subschema_has_a_line(sides):
    """The walk below the root: definitions, `$ref`, `enum`, `const`, a
    required list below the root, `items`, a map's value schema,
    `propertyNames`, `allOf` and `if`/`then`, each with its limits."""
    found = listing(sides[0])
    assert {key[len("schema demo "):]: value for key, value in found.items()
            if key.startswith("schema demo /")} == {
        "/properties/name": "string; required; P0",
        "/properties/battery": "object; optional; RSV",
        "/properties/battery/properties/volts": "number; optional; untagged, under RSV; minimum=0",
        "/properties/parts": "array of ($ref #/$defs/part); optional; P0",
        "/properties/parts/items": "$ref #/$defs/part; subschema; untagged, under P0",
        "/properties/clips": "object; optional; P0",
        "/properties/clips/additionalProperties": "$ref #/$defs/clip; subschema; untagged, under P0",
        "/properties/clips/propertyNames": 'untyped; subschema; untagged, under P0; pattern="^[a-z]+$"',
        "/properties/delays": "array of (number); optional; P0",
        "/properties/delays/items": "number; subschema; untagged, under P0; exclusiveMinimum=0",
        "/properties/odometry": "enum(none|encoders|slam); optional; mixed (P0: none, encoders. RSV: slam.)",
        "/$defs/part": "object; definition; untagged",
        "/$defs/part/properties/id": "string; required; untagged",
        "/$defs/part/properties/kind": "enum(drive|light); required; untagged",
        "/$defs/part/allOf/0": "untyped; subschema; untagged",
        "/$defs/part/allOf/0/if": "untyped; subschema; untagged",
        "/$defs/part/allOf/0/if/properties/kind": 'const("drive"); optional; untagged',
        "/$defs/part/allOf/0/then": "$ref #/$defs/drive; subschema; untagged",
        "/$defs/drive": "untyped; definition; untagged",
        "/$defs/drive/additionalProperties": "nothing; subschema; untagged",
        "/$defs/drive/properties/wheels": "integer; optional; untagged; minimum=2",
        "/$defs/clip": "object; definition; untagged",
        "/$defs/clip/properties/frames": "integer; optional; untagged",
    }
    assert found["schema demo (root)"] == "closed object", "its additionalProperties is said once, as closed"


def _then(d):
    d["$defs"]["part"]["allOf"][0]["then"]["$ref"] = "#/$defs/clip"


def _opened(d):
    d["$defs"]["drive"]["additionalProperties"] = True


def _map_value(d):
    d["properties"]["clips"]["additionalProperties"] = {"type": "string"}


def _names(d):
    d["properties"]["clips"]["propertyNames"]["pattern"] = "^.*$"


def _items_limit(d):
    d["properties"]["delays"]["items"]["exclusiveMinimum"] = 5


def _enum(d):
    d["$defs"]["part"]["properties"]["kind"]["enum"].append("camera")


def _const(d):
    d["$defs"]["part"]["allOf"][0]["if"]["properties"]["kind"]["const"] = "light"


def _nested_required(d):
    d["$defs"]["part"]["required"] = ["kind"]


def _tag_moved(d):
    d["properties"]["odometry"]["description"] = "P0: none. RSV: encoders, slam."


@pytest.mark.parametrize("change, key, was, now", [
    (_then, "/$defs/part/allOf/0/then",
     "$ref #/$defs/drive; subschema; untagged", "$ref #/$defs/clip; subschema; untagged"),
    (_opened, "/$defs/drive/additionalProperties",
     "nothing; subschema; untagged", "anything; subschema; untagged"),
    (_map_value, "/properties/clips/additionalProperties",
     "$ref #/$defs/clip; subschema; untagged, under P0", "string; subschema; untagged, under P0"),
    (_names, "/properties/clips/propertyNames",
     'untyped; subschema; untagged, under P0; pattern="^[a-z]+$"',
     'untyped; subschema; untagged, under P0; pattern="^.*$"'),
    (_items_limit, "/properties/delays/items",
     "number; subschema; untagged, under P0; exclusiveMinimum=0",
     "number; subschema; untagged, under P0; exclusiveMinimum=5"),
    (_enum, "/$defs/part/properties/kind",
     "enum(drive|light); required; untagged", "enum(drive|light|camera); required; untagged"),
    (_const, "/$defs/part/allOf/0/if/properties/kind",
     'const("drive"); optional; untagged', 'const("light"); optional; untagged'),
    (_nested_required, "/$defs/part/properties/id",
     "string; required; untagged", "string; optional; untagged"),
    (_tag_moved, "/properties/odometry",
     "enum(none|encoders|slam); optional; mixed (P0: none, encoders. RSV: slam.)",
     "enum(none|encoders|slam); optional; mixed (P0: none. RSV: encoders, slam.)"),
], ids=["then-ref", "definition-opened", "map-value", "property-names", "items-limit", "enum",
        "const", "nested-required", "value-moved-between-tags"])
def test_a_change_below_a_property_is_shown(sides, change, key, was, now):
    """Each of these changes what a manifest may hold, and each printed "no
    change" once."""
    edit_schema(sides[1], change)
    assert diff_of(sides) == [
        "api: 0 added, 0 removed, 1 changed against base",
        "changed:",
        f"  ~ schema demo {key}",
        f"      was: {was}",
        f"      now: {now}",
    ]


def test_a_tag_inside_a_phrase_is_prose():
    """A tag counts where a clause starts with it or a colon follows it."""
    assert api_diff._tag_of({"description": "RSV. Ignored by the P0 engine."}) == ("RSV", "RSV")
    assert api_diff._tag_of({"description": "P0. In P0 the robot fails loudly."}) == ("P0", "P0")
    assert api_diff._tag_of({"description": "P0 minimal, V1 full. A loop."}) == ("mixed", "mixed (P0 minimal, V1 full.)")
    assert api_diff._tag_of({"description": "P0, optional. micro is RSV: later."}) == (
        "mixed", "mixed (P0, optional. micro is RSV: later.)")
    assert api_diff._tag_of({"description": "Prose with no tag."}) == ("", "")


def test_a_new_flag_is_shown_for_every_command_that_takes_it(sides):
    """A flag added in a helper reaches both commands that call it, one of
    them through an import from the other's module."""
    edit(sides[1], "emet-engine/emet_engine/cli.py", 'help="where the state lives")',
         'help="where the state lives")\n    parser.add_argument("--keys", metavar="FILE", help="keys")')
    assert diff_of(sides) == [
        "api: 2 added, 0 removed, 0 changed against base",
        "added:",
        "  + flag emet-listen --keys: option, metavar='FILE'",
        "  + flag emet-talk --keys: option, metavar='FILE'",
    ]


def test_a_reworded_help_text_is_no_change(sides):
    edit(sides[1], "emet-engine/emet_engine/cli.py", 'help="say it"', 'help="speak the reply aloud"')
    assert diff_of(sides) == ["api: no change against base"]


def test_a_version_and_a_floor_are_shown(sides):
    edit(sides[1], "emet-sdk/pyproject.toml", 'version = "1.0.0"', 'version = "1.0.1"')
    edit(sides[1], "emet-engine/pyproject.toml", '"emet-sdk>=1.0.0"', '"emet-sdk>=1.0.1"')
    edit(sides[1], "emet-sdk/pyproject.toml", 'dev = ["pytest>=8.0"]', 'dev = ["pytest>=8.0"]\nlive = ["httpx>=0.28"]')
    assert diff_of(sides) == [
        "api: 1 added, 0 removed, 2 changed against base",
        "added:",
        "  + package emet-sdk extra live requires httpx: >=0.28",
        "changed:",
        "  ~ package emet-engine requires emet-sdk",
        "      was: >=1.0.0",
        "      now: >=1.0.1",
        "  ~ package emet-sdk version",
        "      was: 1.0.0",
        "      now: 1.0.1",
    ]


def test_the_marker_file_is_listed(sides):
    (sides[1] / "emet-sdk/emet_sdk/py.typed").write_bytes(b"")
    assert diff_of(sides)[2:] == [
        "  + sdk emet_sdk py.typed: present, so a type checker reads the SDK's annotations (PEP 561)"]


def test_cli_py_is_outside_the_sdk_listing(sides):
    """The command line's surface is its flags (decision E8)."""
    assert api_diff.OUTSIDE_THE_CONTRACT == frozenset({"cli.py", "testing"})
    assert not [key for key in listing(sides[0]) if key.startswith("sdk emet_sdk.cli")]
    edit(sides[1], "emet-sdk/emet_sdk/cli.py", "def run_everything", "def run_more() -> None:\n    return None\n\n\ndef run_everything")
    assert diff_of(sides) == ["api: no change against base"]


def test_a_package_inside_the_sdk_is_read_and_the_testing_folder_is_not(sides):
    """A contract module in a folder of the SDK is in the listing, as it is
    under the line cap; the conformance kit's `testing/` is outside both
    (decision E8), and a folder with no `__init__.py` is no package."""
    head = sides[1]
    build(head, {
        "emet-sdk/emet_sdk/memory/__init__.py": '__all__ = ["Row"]\nfrom .rows import Row\n',
        "emet-sdk/emet_sdk/memory/rows.py": "class Row:\n    def keep(self, level: int = 1) -> bool:\n        return True\n",
        "emet-sdk/emet_sdk/testing/__init__.py": "",
        "emet-sdk/emet_sdk/testing/kit.py": "def check() -> None:\n    return None\n",
        "emet-sdk/emet_sdk/data/loose.py": "def nowhere() -> None:\n    return None\n",
    })
    assert diff_of(sides) == [
        "api: 3 added, 0 removed, 0 changed against base",
        "added:",
        "  + sdk emet_sdk.memory.Row: re-export of emet_sdk.memory.rows.Row",
        "  + sdk emet_sdk.memory.rows.Row: class",
        "  + sdk emet_sdk.memory.rows.Row.keep: def (self, level: int=1) -> bool",
    ]


def test_a_property_made_a_method_is_a_change(sides):
    """A decorator is part of what a plugin author sees."""
    edit(sides[1], "emet-sdk/emet_sdk/things.py", "    @property\n    def label", "    def label")
    assert diff_of(sides) == [
        "api: 0 added, 0 removed, 1 changed against base",
        "changed:",
        "  ~ sdk emet_sdk.things.Part.label",
        "      was: @property def (self) -> str",
        "      now: def (self) -> str",
    ]


def test_a_star_import_is_said(sides):
    """Its names cannot be read without importing, so the listing says what
    it reads, and a name `__all__` lists that only it binds says so."""
    head = sides[1]
    build(head, {"emet-sdk/emet_sdk/facade.py": "from .things import *\n\n\ndef own() -> None:\n    return None\n"})
    found = listing(head)
    assert found["sdk emet_sdk.facade.*"] == (
        "star import from emet_sdk.things, so the names it binds cannot be read without importing")
    assert found["sdk emet_sdk.facade.own"] == "def () -> None"
    edit(head, "emet-sdk/emet_sdk/facade.py", "\n\ndef own", '\n__all__ = ["Part", "own"]\n\n\ndef own')
    found = listing(head)
    assert found["sdk emet_sdk.facade.Part"] == "named in __all__ and bound only by the star import from emet_sdk.things"
    assert "sdk emet_sdk.facade.*" in found


def test_the_reading_rules_the_docstrings_promise(sides):
    """A name bound under a top-level `try`, `__all__ +=`, a `ClassVar` kept
    out of a dataclass's fields, `requires-python`, a flag's default, choices
    and type, a parser handed to a helper by keyword, and a parser of
    another command left out."""
    head = sides[1]
    build(head, {"emet-sdk/emet_sdk/extra.py": '''\
        """More of the contract."""
        from dataclasses import dataclass
        from importlib import metadata as _metadata
        from typing import ClassVar

        __all__ = ["__version__"]
        __all__ += ["Rows"]

        try:
            __version__ = _metadata.version("emet-sdk")
        except _metadata.PackageNotFoundError:
            __version__ = "0+unknown"


        @dataclass
        class Rows:
            count: int
            LIMIT: ClassVar[int] = 3
            size: int = 1
        '''})
    edit(head, "emet-engine/emet_engine/cli.py", 'help="where the state lives")',
         'help="where the state lives")\n'
         '    parser.add_argument("--rate", type=float, default=1.0, choices=[1.0, 1.5], help="how fast")')
    edit(head, "emet-engine/emet_engine/cli.py", "    add_body_flags(parser)\n",
         '    add_body_flags(parser)\n    other = argparse.ArgumentParser(prog="emet-other")\n'
         '    other.add_argument("--nope")\n')
    edit(head, "emet-engine/emet_engine/talk.py", "add_body_flags(parser)", "add_body_flags(parser=parser)")
    found = listing(head)
    assert found["sdk emet_sdk.extra.__version__"] == "attribute = _metadata.version('emet-sdk'), or '0+unknown'"
    assert found["sdk emet_sdk.extra.Rows"] == "@dataclass class, fields in order: count, size"
    assert found["sdk emet_sdk.extra.Rows.LIMIT"] == "attribute: ClassVar[int] = 3"
    assert found["package emet-sdk requires-python"] == ">=3.11"
    assert found["flag emet-listen --rate"] == "option, choices=[1.0, 1.5], default=1.0, type=float"
    assert found["flag emet-talk --rate"] == found["flag emet-listen --rate"], "handed over by keyword"
    assert not [key for key in found if "--nope" in key or "emet-other" in key]


# --------------------------------------------------------------------------
# What the listing holds
# --------------------------------------------------------------------------


def test_all_is_what_a_module_exports(sides):
    found = listing(sides[0])
    sdk = sorted(key for key in found if key.startswith("sdk "))
    assert sdk == [
        "sdk emet_sdk.Part",
        "sdk emet_sdk.VERSION",
        "sdk emet_sdk.speak",
        "sdk emet_sdk.things.Gone",
        "sdk emet_sdk.things.Gone.colour",
        "sdk emet_sdk.things.Gone.fade",
        "sdk emet_sdk.things.LIMIT",
        "sdk emet_sdk.things.Part",
        "sdk emet_sdk.things.Part.describe",
        "sdk emet_sdk.things.Part.label",
        "sdk emet_sdk.things.Part.name",
        "sdk emet_sdk.things.Part.size",
        "sdk emet_sdk.things.Part.tags",
        "sdk emet_sdk.things.speak",
    ], "no private name, no imported helper, nothing from cli.py"
    assert found["sdk emet_sdk.Part"] == "re-export of emet_sdk.things.Part"
    assert found["sdk emet_sdk.VERSION"] == "attribute = '1.0'"


def test_a_computed_all_is_said(sides):
    edit(sides[1], "emet-sdk/emet_sdk/__init__.py", '__all__ = ["Part", "VERSION", "speak"]',
         '__all__ = sorted(["Part", "VERSION", "speak"])')
    found = listing(sides[1])
    assert found["sdk emet_sdk.__all__"].startswith("computed, so it cannot be read without importing")
    assert "sdk emet_sdk.VERSION" in found, "the module's own public names are listed instead"


def test_flags_of_subcommands_and_exclusive_groups(sides):
    found = listing(sides[0])
    flags = {key: value for key, value in found.items() if key.startswith("flag ")}
    assert flags == {
        "flag emet validate": "subcommand",
        "flag emet validate --json": "option, action='store_true'",
        "flag emet validate paths": "positional, metavar='PATH', nargs='+'",
        "flag emet-listen --echo": "option, action='store_true', excludes --speak",
        "flag emet-listen --speak": "option, action='store_true', excludes --echo",
        "flag emet-listen --state": "option, metavar='FILE'",
        "flag emet-listen manifest": "positional",
        "flag emet-talk --state": "option, metavar='FILE'",
        "flag emet-talk manifest": "positional",
    }
    assert found["script emet-talk"] == "emet_engine.talk:main, from emet-engine"


def test_the_listing_is_sorted_and_the_same_twice(sides):
    """Sorted by what each line names, so a subcommand's own line sits
    above its flags."""
    first = api_diff.listing_text(listing(sides[0]))
    keys = [line.partition(": ")[0] for line in first]
    assert keys == sorted(keys)
    assert keys.index("flag emet validate") + 1 == keys.index("flag emet validate --json")
    assert first == api_diff.listing_text(listing(sides[0]))


def test_a_file_that_does_not_parse_is_a_line_and_the_rest_is_read(sides):
    (sides[1] / "emet-sdk/emet_sdk/things.py").write_text("def (:\n", encoding="utf-8")
    found = listing(sides[1])
    assert found["sdk emet_sdk.things"].startswith("could not be parsed")
    assert found["package emet-sdk version"] == "1.0.0"


# --------------------------------------------------------------------------
# The base, read out of git
# --------------------------------------------------------------------------


def test_the_base_is_read_through_git_show_and_ls_tree_alone(sides, monkeypatch):
    """`GitTree` builds the same listing from git's answers as `DiskTree`
    builds from the files, and asks git nothing but `show` and `ls-tree`."""
    base = sides[0]
    asked: list[tuple[str, ...]] = []

    def fake_git(root: Path, *args: str) -> str | None:
        asked.append(args)
        if args[0] == "show":
            ref, _, path = args[1].partition(":")
            full = base / path
            return full.read_text(encoding="utf-8") if ref == "base" and full.is_file() else None
        if args[0] == "ls-tree":
            ref, _, folder = args[1].partition(":")
            full = base / folder
            if ref != "base" or not full.is_dir():
                return None
            return "".join(f"{'040000 tree' if p.is_dir() else '100644 blob'} {'0' * 40}\t{p.name}\n"
                           for p in sorted(full.iterdir()))
        return None

    monkeypatch.setattr(api_diff, "git_output", fake_git)
    from_git = api_diff.listing(api_diff.GitTree(base, "base"))
    assert from_git == listing(base)
    assert {args[0] for args in asked} == {"show", "ls-tree"}
    assert not [args for args in asked if not args[1].startswith("base:")]


def test_the_command(sides, tmp_path, capsys):
    head = sides[1]
    assert api_diff.main([str(head), "--list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == api_diff.listing_text(listing(head))

    # A folder outside any repository has no base to read, and fetching
    # cannot help there. (A ref unknown inside a checkout is tested on the
    # real tree, below.)
    if api_diff.git_output(head, "rev-parse", "--git-dir") is None:
        assert api_diff.main([str(head), "--base", "origin/master"]) == 2
        err = capsys.readouterr().err
        assert f"api: {head.resolve()} is no git checkout, or git is not installed" in err
        assert "Fetch" not in err

    (tmp_path / "empty").mkdir()
    assert api_diff.main([str(tmp_path / "empty"), "--list"]) == 2
    assert "does not look like the Emet repository" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The real tree
# --------------------------------------------------------------------------


def value_tag(text: str) -> str:
    """The tag field of a schema line, the third of `type; required; TAG; limits`."""
    return text.split("; ")[2] if text.count("; ") >= 2 else ""


def test_the_real_tree_lists_every_surface():
    found = listing(REPO)
    values = "\n".join(found.values())
    assert "could not be parsed" not in values and "no ArgumentParser" not in values
    assert {key.split()[1] for key in found if key.startswith("script ")} == {
        "emet", "emet-listen", "emet-talk", "emet-say"}
    groups = {key.split()[1] for key in found if key.startswith("entry-point ")}
    assert groups == {"emet.actuators", "emet.sensors", "emet.locomotion", "emet.wake", "emet.audio",
                      "emet.audio_out", "emet.stt", "emet.llm", "emet.tts"}
    for command in ("emet validate", "emet explain", "emet-listen", "emet-talk", "emet-say"):
        assert [key for key in found if key.startswith(f"flag {command} --")], command
    # emet-talk takes the body's flags from a helper in emet-listen's module.
    assert "flag emet-talk --state" in found
    assert {key.split()[1] for key in found if key.startswith("schema ")} == {
        "body-manifest", "motion-pack", "soul-bundle"}
    assert [key for key in found if key.startswith("schema ") and value_tag(found[key]) == "P0"]
    for package in api_diff.PACKAGES:
        assert f"package {package} version" in found


def test_the_real_tree_against_its_own_head(capsys, monkeypatch):
    """Read-only git: the tool asks `rev-parse`, `show` and `ls-tree` of
    HEAD and nothing else, and a ref git does not know is one to fetch."""
    try:
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=REPO, capture_output=True,
                              check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not installed here")
    if head.returncode != 0:
        pytest.skip("not a git checkout")
    asked: list[str] = []
    real = api_diff.git_output

    def recording(root: Path, *args: str) -> str | None:
        asked.append(args[0])
        return real(root, *args)

    monkeypatch.setattr(api_diff, "git_output", recording)
    assert api_diff.main([str(REPO), "--base", "HEAD"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == "api: no change against HEAD" or re.fullmatch(
        r"api: \d+ added, \d+ removed, \d+ changed against HEAD", first), first
    assert set(asked) == {"rev-parse", "show", "ls-tree"}

    assert api_diff.main([str(REPO), "--base", "refs/heads/no-such-branch"]) == 2
    assert ("git knows no commit named refs/heads/no-such-branch here, so there is nothing to compare "
            "with. Fetch it") in capsys.readouterr().err


def test_the_real_schemas_say_what_sits_below_their_properties():
    """A few of the real listing's lines below a property: a branch's
    `$ref`, a map's value schema, and a description that tags its values at
    two levels."""
    found = listing(REPO)
    thens = {value for key, value in found.items()
             if key.startswith("schema body-manifest /$defs/capability/allOf/") and key.endswith("/then")}
    assert "$ref #/$defs/drive; subschema; untagged" in thens
    assert found["schema motion-pack /properties/clips/additionalProperties"] == "$ref #/$defs/clip; subschema; untagged"
    assert found["schema body-manifest /$defs/drive/properties/odometry"].startswith(
        "enum(none|encoders|imu_fused|slam); optional; mixed (P0: ")
    assert found["schema body-manifest /$defs/capability/properties/type"].startswith("enum(joint_group|drive|")


def _public_api_listing(monkeypatch):
    """`emet-sdk/tests/test_public_api.py`, loaded by path, with the SDK
    importable from this tree when no install provides it."""
    if importlib.util.find_spec("emet_sdk") is None:
        monkeypatch.syspath_prepend(str(REPO / "emet-sdk"))
    spec = importlib.util.spec_from_file_location("emet_sdk_public_api_listing",
                                                  REPO / "emet-sdk/tests/test_public_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The words the golden opens a member's line with, before its name.
MEMBER_WORDS = ("field", "def", "property", "attribute", "member", "classmethod", "staticmethod", "class")


def test_the_static_reader_names_what_the_imported_package_exports(monkeypatch):
    """Two readers of one surface: this tool from the source, the golden's
    test from the imported package. They must name the same modules, the
    same public names in each, and the same members of each class."""
    public = _public_api_listing(monkeypatch)
    static = api_diff.sdk_lines(api_diff.DiskTree(REPO))
    modules = public.contract_modules()
    assert {m.__name__ for m in modules} >= {"emet_sdk", "emet_sdk.types", "emet_sdk.validate"}
    accounted = {"sdk emet_sdk py.typed"}
    for module in modules:
        prefix = f"sdk {module.__name__}."
        static_names = sorted(key[len(prefix):] for key in static
                              if key.startswith(prefix) and "." not in key[len(prefix):])
        assert static_names == public.exported(module), module.__name__
        accounted |= {prefix + name for name in static_names}
        for name in static_names:
            value = getattr(module, name)
            if not (isinstance(value, type) and value.__module__ == module.__name__):
                continue
            imported = set()
            for line in public._class_lines(value)[1:]:
                words = line.split()
                at = next(i for i, word in enumerate(words) if word in MEMBER_WORDS)
                imported.add(words[at + 1].split("(")[0].rstrip(":"))
            members = f"{prefix}{name}."
            read = {key[len(members):] for key in static
                    if key.startswith(members) and "." not in key[len(members):]}
            assert read == imported, f"{module.__name__}.{name}"
            accounted |= {members + member for member in read}
    # Nothing the static reader lists is outside those modules and classes:
    # a module the import does not see, such as the command line, would be.
    assert set(static) - accounted == set()
