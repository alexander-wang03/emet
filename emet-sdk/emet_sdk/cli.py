"""The `emet` command line tool.

0.1 ships one subcommand. `emet validate` is the whole executable surface of
Prima Materia's first minor release: there is no runtime yet, no plugin
discovery, and no engine — only the contracts and the ability to check a
document against them.

Exit codes: 0 clean, 1 validation errors, 2 usage or IO failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from emet_sdk import __version__
from emet_sdk.validate import (
    PluginRegistry,
    ValidationReport,
    load_yaml,
    validate_chain_document,
    validate_manifest,
    validate_memory_db,
    validate_motion_pack,
    validate_pairing,
    validate_soul,
)

# ASCII on purpose. This runs over SSH on a Pi, under cron, and in CI, where
# the console encoding is not ours to assume — a status line that raises
# UnicodeEncodeError is worse than a plain one.
_MARKS = {"error": "x", "warning": "!"}


def _detect(path: Path, doc: Any) -> str | None:
    """Work out what kind of document this is, by content then by name."""
    if isinstance(doc, Mapping):
        if "manifest_version" in doc:
            return "manifest"
        if "bundle_version" in doc:
            return "soul"
        if "pack_version" in doc:
            return "pack"
        if doc and all(
            isinstance(v, Mapping) and "rungs" in v for v in doc.values()
        ):
            return "chains"
    if path.suffix == ".db":
        return "memory"
    return None


def _validate_path(path: Path, registry: PluginRegistry) -> tuple[str, ValidationReport]:
    if path.suffix == ".db":
        return "memory", validate_memory_db(path)

    if path.is_dir():
        return _validate_bundle(path, registry)

    doc = load_yaml(path)
    kind = _detect(path, doc)

    if kind == "manifest":
        return kind, validate_manifest(doc, registry=registry)
    if kind == "soul":
        return kind, validate_soul(doc)
    if kind == "pack":
        return kind, validate_motion_pack(doc)
    if kind == "chains":
        return kind, validate_chain_document(doc)

    report = ValidationReport()
    report.error(
        "unknown_document",
        "cannot tell what kind of document this is. Expected a top-level "
        "`manifest_version`, `bundle_version`, or `pack_version` key, a chain "
        "file, or a .db memory database.",
    )
    return "unknown", report


def _validate_bundle(path: Path, registry: PluginRegistry) -> tuple[str, ValidationReport]:
    """Validate a *.emet directory: soul.yaml, memory.db, motion packs."""
    report = ValidationReport()

    soul_path = path / "soul.yaml"
    if not soul_path.exists():
        report.error("missing_file", f"bundle has no soul.yaml at {soul_path}")
        return "bundle", report

    report.extend(validate_soul(load_yaml(soul_path)))

    memory_path = path / "memory.db"
    if memory_path.exists():
        report.extend(validate_memory_db(memory_path))

    for pack in sorted((path / "motion").glob("*.pack.yaml")) if (path / "motion").is_dir() else []:
        report.extend(validate_motion_pack(load_yaml(pack)))

    return "bundle", report


def _print_human(path: Path, kind: str, report: ValidationReport, *, strict: bool) -> None:
    label = f"{path}  ({kind})"
    failed = report.errors or (strict and report.warnings)
    status = "FAIL" if failed else "ok"

    print(f"{status:<4} {label}")
    if not report.findings:
        return

    for finding in report.findings:
        mark = _MARKS[finding.severity]
        where = f" {finding.path}" if finding.path else ""
        print(f"     {mark} {finding.code}{where}")
        for line in finding.message.splitlines():
            print(f"       {line}")


def _cmd_validate(args: argparse.Namespace) -> int:
    registry = PluginRegistry(verify_drivers=args.verify_drivers)
    results: list[tuple[Path, str, ValidationReport]] = []

    for raw in args.paths:
        path = Path(raw)
        if not path.exists():
            report = ValidationReport()
            report.error("missing_file", f"no such file or directory: {path}")
            results.append((path, "unknown", report))
            continue
        try:
            kind, report = _validate_path(path, registry)
        except Exception as exc:  # noqa: BLE001 — surfaced to the user, not swallowed
            report = ValidationReport()
            report.error("read_error", f"{type(exc).__name__}: {exc}")
            kind = "unknown"
        results.append((path, kind, report))

    if args.pair and len(args.paths) == 2:
        docs = [load_yaml(Path(p)) for p in args.paths]
        manifest = next((d for d in docs if "manifest_version" in d), None)
        soul = next((d for d in docs if "bundle_version" in d), None)
        if manifest and soul:
            results.append((Path("(pairing)"), "pairing", validate_pairing(manifest, soul)))

    if args.json:
        payload = [
            {
                "path": str(p),
                "kind": k,
                "ok": r.ok,
                "findings": [
                    {
                        "severity": f.severity,
                        "code": f.code,
                        "message": f.message,
                        "path": f.path,
                    }
                    for f in r.findings
                ],
            }
            for p, k, r in results
        ]
        print(json.dumps(payload, indent=2))
    else:
        for path, kind, report in results:
            _print_human(path, kind, report, strict=args.strict)

    failed = any(
        r.errors or (args.strict and r.warnings) for _, _, r in results
    )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emet",
        description="Emet — validate body manifests, soul bundles, motion packs, and chains.",
    )
    parser.add_argument("--version", action="version", version=f"emet-sdk {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser(
        "validate",
        help="check a document against the schemas and the semantic rules",
    )
    validate.add_argument("paths", nargs="+", metavar="PATH")
    validate.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    validate.add_argument(
        "--verify-drivers",
        action="store_true",
        help="require every driver plugin to be installed (plugin discovery lands in 0.2, "
             "so this currently rejects everything)",
    )
    validate.add_argument(
        "--pair",
        action="store_true",
        help="with exactly two paths, also check cross-document rules between a "
             "manifest and a soul",
    )
    validate.add_argument("--json", action="store_true", help="machine-readable output")
    validate.set_defaults(func=_cmd_validate)

    return parser


def _force_utf8_output() -> None:
    """Make non-ASCII in messages survive a legacy console.

    Windows still defaults stdout to cp1252, where an em-dash in a validation
    message either mangles or raises. Harmless everywhere else.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 2


if __name__ == "__main__":
    sys.exit(main())
