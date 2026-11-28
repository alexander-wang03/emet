"""The SDK's public API, held equal to a file.

`emet_sdk` is the contract every plugin and the engine agree on, and until
this test nothing saw a signature change: one test pinned two names. Here the
whole surface is written out from the imported package and compared with
`tests/golden/public_api.txt`. A change to a public name, a signature, a
field, a default or a base class fails this test until the golden is updated
on purpose, and the golden's diff is then in the pull request, beside the
contract checklist.

**What is listed.** Every contract module: `emet_sdk` and each module and
package in it, at any depth, except `cli`, whose surface is its flags, and
the `testing` package a conformance kit would be (decision E8). In each,
every name in `__all__`. A function with its signature. A class with its
bases, its fields in order when it is a dataclass, and each method, property
and attribute it defines itself; what it inherits shows under the base. A
constant with its value. A name a module only passes on shows where it
comes from.

**The same text on every Python.** Annotations are printed as the source
wrote them, which is what `from __future__ import annotations` keeps. A set
prints sorted, an enum member as `Type.NAME`, a generic alias by its origin
and arguments (`dict[str, int]`), and a default built by a factory as
`factory <name> -> <what calling it returns>`, so a factory that starts to
return something else is a change. `__version__` prints its type alone,
since its value is the release's. Methods a dataclass, an enum or a Protocol
writes for itself are left out: they differ between Python versions and
nobody typed them.

Run as a script, this file prints the listing, or rewrites the golden:

    python tests/test_public_api.py            # print it
    python tests/test_public_api.py --update   # write tests/golden/public_api.txt

The script needs the standard library alone, so it also runs on a Python
that has no pytest. `tools/api_diff.py` reads the same surface a second way,
from the source, and prints it against master in every pull request.

The idea of a declared and pinned public API is Semantic Versioning 2.0.0's
first rule and REP 2004's "strictly declared public API" (`CITATIONS.md`).
"""

from __future__ import annotations

import dataclasses
import difflib
import enum
import importlib
import inspect
import os
import pkgutil
import sys
import types
import typing
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "public_api.txt"

#: What directly under `emet_sdk` is outside the contract (decision E8): the
#: command line, and the conformance kit's package with everything in it.
#: `tools/check_health.py` and `tools/api_diff.py` leave out the same two.
OUTSIDE_THE_CONTRACT = frozenset({"cli", "testing"})

#: The one command that rewrites the golden, from `emet-sdk/`.
UPDATE = "python tests/test_public_api.py --update"

MESSAGE = """\
The SDK's public API is no longer what tests/golden/public_api.txt says.

{diff}

A line starting with "-" is in the golden and gone from the code. A line
starting with "+" is in the code and new to the golden.

If the change is meant, rewrite the golden with one command, from emet-sdk/:

    {update}

and commit the golden with the change, so the pull request shows this diff.
An added or removed name, or a changed signature, is also a line under Added,
Changed or Removed in CHANGELOG.md. If the change is not meant, the code is
what to fix."""


# --------------------------------------------------------------------------
# Printing values the same way on every Python
# --------------------------------------------------------------------------


def _type_name(cls: type) -> str:
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def _show(value: Any) -> str:
    """A value as text that does not depend on hashing or on the Python."""
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return repr(value)
    if isinstance(value, (set, frozenset)):
        inner = ", ".join(sorted(_show(v) for v in value))
        return f"{type(value).__name__}({{{inner}}})" if value else f"{type(value).__name__}()"
    if isinstance(value, tuple):
        return "(" + ", ".join(_show(v) for v in value) + ("," if len(value) == 1 else "") + ")"
    if isinstance(value, list):
        return "[" + ", ".join(_show(v) for v in value) + "]"
    if isinstance(value, (dict, types.MappingProxyType)):
        pairs = sorted((_show(k), _show(v)) for k, v in value.items())
        return "{" + ", ".join(f"{k}: {v}" for k, v in pairs) + "}"
    if isinstance(value, type):
        return _type_name(value)
    if value is Ellipsis:
        return "..."
    if typing.get_origin(value) is typing.Literal:
        return "Literal[" + ", ".join(_show(v) for v in typing.get_args(value)) + "]"
    if isinstance(value, types.UnionType) or typing.get_origin(value) is typing.Union:
        return " | ".join(_show(v) for v in typing.get_args(value))
    if typing.get_origin(value) is not None and typing.get_args(value):
        # A generic alias, `dict[str, int]`: a change of argument is a change.
        arguments = ", ".join(_show(v) for v in typing.get_args(value))
        return f"{_show(typing.get_origin(value))}[{arguments}]"
    return f"<{type(value).__name__}>"


def _annotation(annotation: Any) -> str:
    """An annotation as the source wrote it."""
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        return _type_name(annotation)
    return _show(annotation)


def _signature(function: Any) -> str:
    """`(a: int, *, b: str = 'x') -> bool`, from the function itself."""
    signature = inspect.signature(function)
    parts: list[str] = []
    parameters = list(signature.parameters.values())
    for index, parameter in enumerate(parameters):
        text = parameter.name
        if parameter.kind is parameter.VAR_POSITIONAL:
            text = "*" + text
        elif parameter.kind is parameter.VAR_KEYWORD:
            text = "**" + text
        elif parameter.kind is parameter.KEYWORD_ONLY:
            before = parameters[index - 1].kind if index else None
            if before not in (parameter.KEYWORD_ONLY, parameter.VAR_POSITIONAL):
                parts.append("*")
        if parameter.annotation is not parameter.empty:
            text += f": {_annotation(parameter.annotation)}"
        if parameter.default is not parameter.empty:
            text += f" = {_show(parameter.default)}"
        parts.append(text)
        after = parameters[index + 1].kind if index + 1 < len(parameters) else None
        if parameter.kind is parameter.POSITIONAL_ONLY and after is not parameter.POSITIONAL_ONLY:
            parts.append("/")
    returns = ""
    if signature.return_annotation is not signature.empty:
        returns = f" -> {_annotation(signature.return_annotation)}"
    return f"({', '.join(parts)}){returns}"


# --------------------------------------------------------------------------
# One name at a time
# --------------------------------------------------------------------------


def _written_here(function: Any, cls: type) -> bool:
    """Whether somebody typed this method in this class's module.

    A dataclass compiles its `__init__` and `__eq__` from a string and wraps
    its `__repr__` in a function of the standard library's, and an enum or a
    Protocol borrows methods from the standard library. Which of those exist
    differs between Python versions, and none is the SDK's. So the test is
    where the code came from: the file of the class's own module.
    """
    code = getattr(inspect.unwrap(function), "__code__", None)
    home = getattr(sys.modules.get(cls.__module__), "__file__", None)
    if code is None or home is None:
        return False
    return os.path.normcase(os.path.abspath(code.co_filename)) == os.path.normcase(os.path.abspath(home))


def _own_annotations(cls: type) -> dict[str, Any]:
    return dict(inspect.get_annotations(cls))


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _function_line(name: str, function: Any, word: str = "def") -> str:
    marks = ""
    if getattr(function, "__isabstractmethod__", False):
        marks += "abstract "
    if inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function):
        marks += "async "
    return f"{marks}{word} {name}{_signature(function)}"


def _field_line(field: dataclasses.Field) -> str:
    text = f"field {field.name}: {_annotation(field.type)}"
    if field.default is not dataclasses.MISSING:
        text += f" = {_show(field.default)}"
    elif field.default_factory is not dataclasses.MISSING:
        factory = field.default_factory
        text += f" = factory {getattr(factory, '__name__', '?')}"
        # The name alone holds nothing of a lambda's body, so the value it
        # makes is printed too. The SDK's factories make plain containers.
        try:
            made = factory()
        except Exception as exc:  # a factory that cannot run alone is said, never fatal
            text += f", which raised {type(exc).__name__} when called alone"
        else:
            text += f" -> {_show(made)}"
    if not field.init:
        text += "  [not in the constructor]"
    if field.kw_only:
        text += "  [keyword only]"
    return text


def _class_lines(cls: type) -> list[str]:
    """A class as lines, its bases and kind first and then every member it defines itself."""
    bases = [_type_name(b) for b in cls.__bases__ if b is not object]
    kinds: list[str] = []
    if dataclasses.is_dataclass(cls):
        kinds.append("dataclass")
        if cls.__dataclass_params__.frozen:
            kinds.append("frozen")
        if "__slots__" in vars(cls):
            kinds.append("slots")
    if getattr(cls, "_is_protocol", False):
        kinds.append("protocol")
        if getattr(cls, "_is_runtime_protocol", False):
            kinds.append("runtime checkable")
    head = f"class {cls.__name__}" + (f"({', '.join(bases)})" if bases else "")
    lines = [head + (f"  [{', '.join(kinds)}]" if kinds else "")]

    fields = dataclasses.fields(cls) if dataclasses.is_dataclass(cls) else ()
    field_names = {f.name for f in fields}
    members: list[str] = [_field_line(f) for f in fields]  # in order: it is the constructor's

    if issubclass(cls, enum.Enum):
        members += [f"member {name} = {_show(member.value)}" for name, member in cls.__members__.items()]

    annotations = _own_annotations(cls)
    rest: list[str] = []
    seen: set[str] = set()
    for name, value in vars(cls).items():
        if name in field_names or (name.startswith("_") and not _is_dunder(name)):
            continue
        if isinstance(value, enum.Enum) and isinstance(value, cls):
            continue
        if isinstance(value, (classmethod, staticmethod)):
            function = inspect.unwrap(value.__func__)
            if _is_dunder(name) and not _written_here(function, cls):
                continue
            rest.append(_function_line(name, function, type(value).__name__))
        elif isinstance(value, property):
            returns = ""
            if value.fget is not None:
                annotation = inspect.signature(value.fget).return_annotation
                if annotation is not inspect.Signature.empty:
                    returns = f" -> {_annotation(annotation)}"
            rest.append(f"property {name}{returns}" + ("  [settable]" if value.fset else ""))
        elif inspect.isfunction(value):
            if _is_dunder(name) and not _written_here(value, cls):
                continue
            rest.append(_function_line(name, value))
        elif _is_dunder(name) or isinstance(value, types.MemberDescriptorType):
            continue
        elif isinstance(value, type):
            rest.append(f"class {name}")
        else:
            typed = f": {_annotation(annotations[name])}" if name in annotations else ""
            rest.append(f"attribute {name}{typed} = {_show(value)}")
        seen.add(name)
    for name, annotation in annotations.items():
        # Declared and given no value: a Protocol's attribute.
        if name not in seen and name not in field_names and not name.startswith("_"):
            rest.append(f"attribute {name}: {_annotation(annotation)}")
    return lines + ["    " + line for line in members + sorted(rest)]


def _describe(module: types.ModuleType, name: str) -> list[str]:
    value = getattr(module, name)
    if name == "__version__":
        # The value is the release's, and a source checkout has another.
        return [f"{name}: {type(value).__name__}"]
    if isinstance(value, type) or inspect.isfunction(value):
        home = f"{value.__module__}.{value.__qualname__}"
        if home != f"{module.__name__}.{name}":
            return [f"{name} = {home}"]
        return _class_lines(value) if isinstance(value, type) else [_function_line(name, value)]
    if typing.get_origin(value) is not None or isinstance(value, types.UnionType):
        return [f"{name} = {_show(value)}"]
    return [f"{name}: {type(value).__name__} = {_show(value)}"]


def modules_of(package: types.ModuleType) -> list[types.ModuleType]:
    """A package, then each module and package in it at any depth that is
    part of the contract, by dotted name. Directly under the package,
    `OUTSIDE_THE_CONTRACT` is left out and never imported."""
    found: list[types.ModuleType] = []

    def visit(inner: types.ModuleType, top: bool) -> None:
        for info in pkgutil.iter_modules(inner.__path__):
            if info.name.startswith("_") or (top and info.name in OUTSIDE_THE_CONTRACT):
                continue
            module = importlib.import_module(f"{inner.__name__}.{info.name}")
            found.append(module)
            if info.ispkg:
                visit(module, False)

    visit(package, True)
    return [package] + sorted(found, key=lambda module: module.__name__)


def contract_modules() -> list[types.ModuleType]:
    """`emet_sdk`, then each of its modules that is part of the contract."""
    import emet_sdk

    return modules_of(emet_sdk)


def exported(module: types.ModuleType) -> list[str]:
    """A module's public names: `__all__`, or without one every name it
    defines itself that does not start with `_`."""
    if hasattr(module, "__all__"):
        return sorted(set(module.__all__))
    return sorted(
        name for name, value in vars(module).items()
        if not name.startswith("_") and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", module.__name__) == module.__name__
    )


def public_api() -> list[str]:
    """The whole listing, one line an entry, in a fixed order."""
    lines: list[str] = []
    for module in contract_modules():
        lines.append(module.__name__)
        for name in exported(module):
            lines += ["    " + line for line in _describe(module, name)]
        lines.append("")
    return lines


# --------------------------------------------------------------------------
# The tests
# --------------------------------------------------------------------------


#: Set by mutmut from the start of its run, before any test runs (mutmut
#: 3.8.1's `mutation/trampoline.py` and `__main__.py`, read 2026-11-26).
UNDER_MUTMUT = "MUTANT_UNDER_TEST"


def test_the_public_api_is_what_the_golden_says():
    import pytest

    if UNDER_MUTMUT in os.environ:
        # mutmut rewrites each function it mutates as a trampoline and adds
        # every mutant beside it, in the module and in the class, so the
        # package imported under it is mutmut's copy. Its first pass runs
        # every test and stops at the first failure, so this test would stop
        # every mutation run of the SDK. The golden asks whether a name or a
        # signature moved, a question a mutation run does not ask.
        pytest.skip("under mutmut, which adds its mutants to the package's names")
    assert GOLDEN.is_file(), f"no golden at {GOLDEN}: write it with `{UPDATE}`, from emet-sdk/"
    wanted = GOLDEN.read_text(encoding="utf-8").splitlines()
    found = public_api()
    if found != wanted:
        diff = "\n".join(difflib.unified_diff(
            wanted, found, "tests/golden/public_api.txt", "emet_sdk, as imported", lineterm="", n=2))
        pytest.fail(MESSAGE.format(diff=diff, update=UPDATE), pytrace=False)


def test_every_contract_module_is_listed_and_the_command_line_is_not():
    names = [module.__name__ for module in contract_modules()]
    assert names[0] == "emet_sdk"
    assert "emet_sdk.cli" not in names
    for expected in ("chains", "discovery", "intents", "models", "plugin", "resolve", "types", "validate"):
        assert f"emet_sdk.{expected}" in names
    golden = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert [line for line in golden if line and not line.startswith(" ")] == names


def test_every_exported_name_is_bound():
    """`__all__` names nothing the module does not have: `import *` would
    raise on the first missing one."""
    for module in contract_modules():
        missing = [name for name in exported(module) if not hasattr(module, name)]
        assert not missing, f"{module.__name__}.__all__ names {missing}, which the module does not bind"


def test_the_marker_file_sits_beside_the_package():
    """PEP 561: `py.typed` says the package's annotations may be read. It
    promises nothing about how complete they are, and it stays empty.

    This reads the package as imported, which in the suites is the editable
    source tree. That the wheel carries the file is the wheel job's to hold,
    through the data files `tools/ci.py` looks for in it."""
    import emet_sdk

    marker = Path(emet_sdk.__file__).resolve().parent / "py.typed"
    assert marker.is_file(), "emet_sdk/py.typed is gone, so a type checker ignores the SDK's annotations"
    assert marker.read_bytes() == b""


def test_values_print_the_same_whatever_the_hash_seed():
    assert _show(frozenset({"b", "a", "c"})) == "frozenset({'a', 'b', 'c'})"
    assert _show(frozenset()) == "frozenset()"
    assert _show(types.MappingProxyType({"b": 1, "a": frozenset({"y", "x"})})) == \
        "{'a': frozenset({'x', 'y'}), 'b': 1}"
    assert _show(("only",)) == "('only',)"
    assert _show(typing.Literal["error", "warning"]) == "Literal['error', 'warning']"
    assert _show(int | None) == "int | NoneType"
    # A generic alias by its origin and arguments, so a changed argument is
    # a changed line.
    assert _show(dict[str, int]) == "dict[str, int]"
    assert _show(dict[str, str]) == "dict[str, str]"
    assert _show(typing.Mapping[str, int]) == "collections.abc.Mapping[str, int]"
    assert _show(tuple[int, ...]) == "tuple[int, ...]"


def test_a_factory_default_shows_what_it_makes():
    """The factory's name alone would hold nothing of a lambda's body."""
    @dataclasses.dataclass(frozen=True)
    class Spec:
        schema: dict[str, Any] = dataclasses.field(default_factory=lambda: {"type": "object", "properties": {}})
        rows: list[int] = dataclasses.field(default_factory=list)

    fields = {f.name: f for f in dataclasses.fields(Spec)}
    assert _field_line(fields["schema"]) == (
        "field schema: dict[str, Any] = factory <lambda> -> {'properties': {}, 'type': 'object'}")
    assert _field_line(fields["rows"]) == "field rows: list[int] = factory list -> []"

    def needs_an_argument(value):
        return value

    broken = dataclasses.field(default_factory=needs_an_argument)
    broken.name, broken.type, broken.kw_only = "odd", "int", False   # what a dataclass would set
    assert _field_line(broken) == "field odd: int = factory needs_an_argument, which raised TypeError when called alone"


def test_a_signature_keeps_what_the_source_wrote():
    class Colour(enum.Enum):
        RED = 1

    # Never evaluated: this module's annotations stay text, as the SDK's do.
    def sample(first: Mapping[str, Any], /, second: int = 2, *rest: str,  # noqa: F821
               flag: bool = False, shade: Colour = Colour.RED, **more: float) -> dict[str, Any] | None:
        return None

    def bare(a, *, b=None):
        return None

    assert _signature(sample) == (
        "(first: Mapping[str, Any], /, second: int = 2, *rest: str, "
        "flag: bool = False, shade: Colour = Colour.RED, **more: float) -> dict[str, Any] | None"
    )
    assert _signature(bare) == "(a, *, b = None)"


def test_a_class_lists_what_it_defines_and_nothing_python_wrote_for_it():
    @dataclasses.dataclass(frozen=True, slots=True)
    class Sample:
        name: str
        tags: tuple[str, ...] = ()
        extra: dict[str, int] = dataclasses.field(default_factory=dict)
        seen: int = dataclasses.field(default=0, init=False)
        rate: float = dataclasses.field(default=1.0, kw_only=True)

        @property
        def loud(self) -> str:
            return self.name.upper()

        def __str__(self) -> str:
            return self.name

    lines = _class_lines(Sample)
    assert lines[0] == "class Sample  [dataclass, frozen, slots]"
    assert lines[1:6] == [
        "    field name: str",
        "    field tags: tuple[str, ...] = ()",
        "    field extra: dict[str, int] = factory dict -> {}",
        "    field seen: int = 0  [not in the constructor]",
        "    field rate: float = 1.0  [keyword only]",
    ]
    assert "    property loud -> str" in lines
    assert "    def __str__(self) -> str" in lines
    # The constructor and the comparison a dataclass compiles are not listed.
    assert not [line for line in lines if "__init__" in line or "__eq__" in line or "__replace__" in line]

    class Dial:
        @property
        def level(self) -> int:
            return 1

        @level.setter
        def level(self, value: int) -> None:
            return None

    assert "    property level -> int  [settable]" in _class_lines(Dial)


def test_a_module_without_all_exports_what_it_defines_itself():
    module = types.ModuleType("emet_sdk_throwaway")

    def own() -> None:
        return None

    own.__module__ = module.__name__
    module.own = own
    module.joined = os.path.join          # imported: its module is another's
    module.sub = types.ModuleType("emet_sdk_throwaway.sub")
    module._private = 1
    assert exported(module) == ["own"]


def test_a_package_inside_the_sdk_is_walked_and_its_testing_package_is_not(tmp_path, monkeypatch):
    """The same rule as the line cap and the API diff: a package inside the
    contract is read at any depth, and `testing` is left out unimported."""
    root = tmp_path / "throwaway_sdk"
    for path, text in {
        "__init__.py": "",
        "types.py": "",
        "cli.py": "raise ImportError('cli.py is outside and never imported')\n",
        "memory/__init__.py": "",
        "memory/rows.py": "",
        "testing/__init__.py": "raise ImportError('testing is outside and never imported')\n",
        "_private.py": "",
    }.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    package = importlib.import_module("throwaway_sdk")
    try:
        assert [m.__name__ for m in modules_of(package)] == [
            "throwaway_sdk", "throwaway_sdk.memory", "throwaway_sdk.memory.rows", "throwaway_sdk.types"]
    finally:
        for name in [n for n in sys.modules if n.split(".")[0] == "throwaway_sdk"]:
            del sys.modules[name]


def main(argv: list[str]) -> int:
    # Read the tree this file sits in, installed or not.
    sys.path.insert(0, str(HERE.parent))
    text = "\n".join(public_api()) + "\n"
    if "--update" in argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {GOLDEN}")
        print("An added or removed name, or a changed signature, is a line in CHANGELOG.md.")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
