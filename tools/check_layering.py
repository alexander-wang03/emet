#!/usr/bin/env python3
"""Assert the package layering that the architecture depends on.

    emet_sdk     imports nothing internal
    emet_hal     imports emet_sdk only
    emet_engine  imports emet_sdk only

Under the earlier closed-engine plan this was structural: an outside
contributor had no engine source to couple to. In a monorepo with everything
open, the accidental-coupling path is open to everyone, so an invariant that
used to be a property of the distribution is now a test. If this does not run
on every push, the layering rots — and the layering is the product.

Deliberately dependency-free and short enough to read in one sitting. It
parses each file's AST and inspects static import statements only. It will not
catch `importlib.import_module("emet_engine.x")` or an `__import__` with a
computed name; nothing in this codebase should be doing that, and if it ever
does, that is worth noticing by hand.

Packages that do not exist yet are skipped and reported, so this stays green
through 0.1 and starts guarding `emet_hal` the moment it appears.

Usage:  python tools/check_layering.py [repo_root]
Exit:   0 clean, 1 violations found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# package -> the internal packages it may import
ALLOWED: dict[str, frozenset[str]] = {
    "emet_sdk": frozenset(),
    "emet_hal": frozenset({"emet_sdk"}),
    "emet_engine": frozenset({"emet_sdk"}),
}

INTERNAL = frozenset(ALLOWED)


def find_package(root: Path, name: str) -> Path | None:
    """Locate a package, whether it sits at the root or under its dist dir."""
    for candidate in (root / name, root / name.replace("_", "-") / name):
        if (candidate / "__init__.py").is_file():
            return candidate
    return None


def imported_roots(tree: ast.AST) -> set[str]:
    """Top-level package names imported by this module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: within the same package by
            # definition, so it cannot cross a layer boundary.
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def check(root: Path) -> int:
    violations: list[str] = []
    checked: list[str] = []
    missing: list[str] = []

    for package, allowed in ALLOWED.items():
        directory = find_package(root, package)
        if directory is None:
            missing.append(package)
            continue
        checked.append(package)

        for path in sorted(directory.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                violations.append(f"{path}: could not parse: {exc}")
                continue

            for imported in sorted(imported_roots(tree)):
                if imported == package or imported not in INTERNAL:
                    continue
                if imported not in allowed:
                    rel = path.relative_to(root)
                    permitted = ", ".join(sorted(allowed)) or "nothing internal"
                    violations.append(
                        f"{rel}: {package} imports {imported}, but may import {permitted}"
                    )

    print(f"layering: checked {', '.join(checked) or '(nothing)'}")
    if missing:
        print(f"layering: not present yet, skipped: {', '.join(missing)}")

    if violations:
        print()
        for line in violations:
            print(f"  x {line}")
        print(f"\n{len(violations)} layering violation(s).")
        return 1

    print("layering: ok")
    return 0


if __name__ == "__main__":
    sys.exit(check(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
