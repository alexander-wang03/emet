"""The mutation targets, `tools/ci/mutmut-targets.txt`, held to the tree.

The list names the functions a merge gate or a floor rests on, and
`tools/ci.py mutmut` mutates those a branch changes. A name that no longer
matches a function would drop out of mutation without a word, since the job
only mutates what it finds, so each line is held here to a function that
exists. A decorated function is refused too: mutmut 3.8.1 makes no mutant
of a function with a decorator, a property or one under `lru_cache`, except
a function whose one decorator is `staticmethod` or `classmethod` (its
`mutation/file_mutation.py`, read 2026-11-26), so any other decorated
target would be listed and never tested. And the list is held complete: a
helper a listed function calls in its own file is listed, or named in
`LEFT_OUT` with why, since a branch that changes only that helper would
otherwise mutate nothing.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
TARGETS = TOOLS / "ci" / "mutmut-targets.txt"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

#: The groups the list must hold: the file each one's functions live in.
GROUPS = {
    "the plan-file parser": "tools/plan_file.py",
    "the audit's evidence rules": "tools/release_audit.py",
    "the release check's floors": "tools/release_check.py",
    "the resolver": "emet-sdk/emet_sdk/resolve.py",
    "the validator": "emet-sdk/emet_sdk/validate.py",
}


def read(text: str) -> list[tuple[int, str, str, str]]:
    """Each target: its line number, its path, its function, and the
    comment line above its group."""
    found = []
    comment = ""
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            comment = ""
        elif stripped.startswith("#"):
            comment = stripped.lstrip("# ").strip()
        else:
            path, sep, name = stripped.partition("::")
            assert sep and path and name and " " not in stripped, f"line {number}: {line!r} is no path::function"
            found.append((number, path, name, comment))
    return found


def function(path: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function `name` names in the file, a method as `Class.method`."""
    body = ast.parse((REPO / path).read_text(encoding="utf-8")).body
    node = None
    for part in name.split("."):
        node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            return None
        body = node.body
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


#: The decorators mutmut 3.8.1 still mutates under, when one stands alone.
MUTATED_UNDER = frozenset({"staticmethod", "classmethod"})


def mutated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether mutmut makes mutants of this function: no decorator, or one
    alone that is the bare name `staticmethod` or `classmethod`."""
    if not node.decorator_list:
        return True
    if len(node.decorator_list) != 1:
        return False
    decorator = node.decorator_list[0]
    return isinstance(decorator, ast.Name) and decorator.id in MUTATED_UNDER


@pytest.fixture(scope="module")
def targets() -> list[tuple[int, str, str, str]]:
    return read(TARGETS.read_text(encoding="utf-8"))


def test_every_target_is_a_function_that_exists(targets):
    assert targets
    for number, path, name, _ in targets:
        assert (REPO / path).is_file(), f"line {number}: no file {path}"
        assert function(path, name) is not None, f"line {number}: {path} has no function {name}"


def test_every_target_is_one_mutmut_mutates(targets):
    for number, path, name, _ in targets:
        node = function(path, name)
        assert node is not None and mutated(node), (
            f"line {number}: {path}::{name} is decorated, and mutmut makes no mutant of it")


def test_the_decorators_mutmut_skips():
    def first(*lines: str) -> ast.FunctionDef:
        """The function a few lines of source define, one line a string."""
        node = ast.parse("\n".join(lines)).body[0]
        assert isinstance(node, ast.FunctionDef)
        return node

    assert mutated(first("def plain(): pass"))
    assert mutated(first("@classmethod", "def make(cls): pass"))
    assert mutated(first("@staticmethod", "def helper(): pass"))
    assert not mutated(first("@property", "def done(self): pass"))
    assert not mutated(first("@functools.lru_cache", "def cached(): pass"))
    assert not mutated(first("@classmethod", "@functools.cache", "def both(cls): pass"))


def test_no_target_is_listed_twice(targets):
    seen = [(path, name) for _, path, name, _ in targets]
    assert len(seen) == len(set(seen))


def test_every_group_has_a_comment_above_it_and_the_five_are_there(targets):
    for number, path, name, comment in targets:
        assert comment, f"line {number}: {path}::{name} has no comment line above its group"
    comments = {comment for *_, comment in targets}
    for group, path in GROUPS.items():
        holding = [c for c in comments if group in c.lower()]
        assert holding, f"no group for {group}"
        assert [t for t in targets if t[3] in holding and t[1] == path], f"{group} lists nothing in {path}"


def test_ci_py_reads_the_same_targets_and_finds_each_one(targets):
    """`tools/ci.py mutmut` is the list's one caller: its reader must agree
    with this one, and its own function finder must find every target."""
    if not (TOOLS / "ci.py").is_file():
        pytest.skip("tools/ci.py is not in this tree")
    import ci

    text = TARGETS.read_text(encoding="utf-8")
    assert ci.read_targets(text) == [(path, name) for _, path, name, _ in targets]
    for _, path, name, _ in targets:
        assert ci.function_span((REPO / path).read_text(encoding="utf-8"), name) is not None, f"{path}::{name}"


#: Helpers a listed function calls that are left out of the list, each with
#: why. Every other undecorated helper a listed function reaches in its own
#: file is listed (`test_every_helper_a_target_calls_is_listed_or_left_out`).
LEFT_OUT: dict[str, str] = {
    "tools/plan_file.py::_miscounted": "words a problem's sentence; the plan is refused whatever it says",
    "tools/plan_file.py::_listed": "words a problem's sentence; the plan is refused whatever it says",
    "tools/plan_file.py::_cell_count": "words a problem's sentence; the plan is refused whatever it says",
    "tools/release_audit.py::Audit.run": "the seam the audit's tests answer git and gh through, so no test runs it",
    "tools/release_audit.py::Audit.git": "asks git through Audit.run, which the tests replace",
    "tools/release_audit.py::Audit.gh": "asks gh through Audit.run, which the tests replace",
    "tools/release_audit.py::Audit.fix": "keeps the fix printed after a refusal, which no verdict reads",
    "tools/release_audit.py::Audit.plan_fix": "words the fix printed after a refusal, which no verdict reads",
    "tools/release_audit.py::Audit.run_the_head": "words the fix printed after a refusal, which no verdict reads",
    "tools/release_audit.py::Audit.moved": "words the fix printed after a refusal, which no verdict reads",
    "tools/release_audit.py::Audit.shown": "words a path in a fix, which no verdict reads",
    "tools/release_audit.py::_first_line": "words the first line of an error in a refusal that stands without it",
    "tools/release_audit.py::_some": "words a list of files in a refusal that stands without it",
    "emet-sdk/emet_sdk/validate.py::_schema_dir": ("finds the schemas in an installed or a source layout; a run "
                                                   "has one of the two, so the other's mutants cannot be killed there"),
    "emet-sdk/emet_sdk/validate.py::default_registry": "one call to discovery, whose own tests hold it",
}


def _index(tree: ast.Module) -> tuple[dict[str, ast.FunctionDef | ast.AsyncFunctionDef], set[str],
                                      dict[str, list[str]]]:
    """A file's functions and methods by `name` or `Class.method`, its
    classes, and which classes define each method name."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    classes: set[str] = set()
    methods: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{node.name}.{member.name}"] = member
                    methods.setdefault(member.name, []).append(f"{node.name}.{member.name}")
    return functions, classes, methods


def called(name: str, node: ast.AST, functions: dict, classes: set[str], methods: dict[str, list[str]]) -> set[str]:
    """What a function calls in its own file: a function by name; a class,
    whose `__init__` and `__post_init__` run; `self.m()` in its own class;
    `Class.m()`; and `x.m()` where one class alone defines `m`."""
    owner = name.rpartition(".")[0]
    found: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in functions:
                found.add(func.id)
            if func.id in classes:
                found |= {f"{func.id}.{d}" for d in ("__init__", "__post_init__") if f"{func.id}.{d}" in functions}
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            receiver, method = func.value.id, func.attr
            if receiver in ("self", "cls") and owner and f"{owner}.{method}" in functions:
                found.add(f"{owner}.{method}")
            elif receiver in classes and f"{receiver}.{method}" in functions:
                found.add(f"{receiver}.{method}")
            elif len(methods.get(method, [])) == 1:
                found.add(methods[method][0])
    return found


def unaccounted(targets: list[tuple[int, str, str, str]], left_out: dict[str, str]) -> list[str]:
    """Each undecorated function a listed target reaches in its own file,
    directly or through another it reaches, that is neither listed nor
    left out."""
    missing: list[str] = []
    for path in sorted({t[1] for t in targets}):
        listed = {t[2] for t in targets if t[1] == path}
        functions, classes, methods = _index(ast.parse((REPO / path).read_text(encoding="utf-8")))
        reached: set[str] = set()
        todo = [name for name in listed if name in functions]
        while todo:
            name = todo.pop()
            if name not in reached:
                reached.add(name)
                todo += called(name, functions[name], functions, classes, methods)
        missing += [f"{path}::{name}" for name in sorted(reached - listed)
                    if mutated(functions[name]) and f"{path}::{name}" not in left_out]
    return missing


def test_every_helper_a_target_calls_is_listed_or_left_out(targets):
    """`tools/ci.py mutmut` mutates only listed functions a branch changes,
    so a helper a gate's verdict passes through, left off the list, is never
    mutated when a branch changes it alone."""
    assert unaccounted(targets, LEFT_OUT) == [], "list each, or name it in LEFT_OUT with why"
    listed = {f"{path}::{name}" for _, path, name, _ in targets}
    for entry, why in LEFT_OUT.items():
        path, _, name = entry.partition("::")
        assert function(path, name) is not None, f"LEFT_OUT names {entry}, which is no function"
        assert entry not in listed, f"{entry} is both listed and left out"
        assert why, entry


def test_a_helper_dropped_from_the_list_is_caught(targets):
    """The rule above, on the list with one line taken out, and with nothing
    left out: then exactly what `LEFT_OUT` names is unaccounted, so no entry
    of it names a helper nothing listed reaches."""
    dropped = [t for t in targets if (t[1], t[2]) != ("tools/plan_file.py", "_first_word")]
    assert unaccounted(dropped, LEFT_OUT) == ["tools/plan_file.py::_first_word"]
    assert sorted(unaccounted(targets, {})) == sorted(LEFT_OUT)


def test_a_stale_or_decorated_line_is_caught(tmp_path):
    """The checks above, on a list written here."""
    assert function("tools/plan_file.py", "read_plan") is not None
    assert function("tools/plan_file.py", "no_such_function") is None
    assert function("tools/plan_file.py", "StageRow.done").decorator_list, "a property, so refused"
    assert read("# a group\ntools/plan_file.py::read_plan\n\ntools/plan_file.py::stage\n") == [
        (2, "tools/plan_file.py", "read_plan", "a group"),
        (4, "tools/plan_file.py", "stage", ""),
    ], "a blank line ends a group, so the line after it has no comment"
    with pytest.raises(AssertionError):
        read("tools/plan_file.py read_plan\n")
