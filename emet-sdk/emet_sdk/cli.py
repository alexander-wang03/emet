"""The `emet` command line tool.

    emet validate    check a document against the schemas and the semantic rules
    emet explain     show what each intent means on a particular body

`explain` is the one to reach for when a robot is not doing what you expected.
It prints the binding table: for every intent, which part of *this* body
performs it, and — for anything that fell short of its best option — which
rungs were skipped and why.

Exit codes: 0 clean, 1 validation errors, 2 usage or IO failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from emet_sdk import __version__
from emet_sdk.discovery import PluginRegistry as _Registry
from emet_sdk.resolve import descriptors_from_manifest, resolve, unused_reasons
from emet_sdk.chains import parse_chain_set
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
    registry = _Registry.discover().with_verification(args.verify_drivers)
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



# --------------------------------------------------------------------------
# emet explain
# --------------------------------------------------------------------------


def _shipped_chain_files() -> list[Path]:
    return sorted((Path(__file__).resolve().parent / "chains").glob("*.yaml"))


def _load_chains(extra: list[str] | None) -> dict:
    """SDK defaults first, then any override file — later wins.

    That ordering is how per-soul chain overrides are meant to work: a bundle
    ships only the ladders it wants to change.
    """
    chains: dict = {}
    for path in _shipped_chain_files():
        chains.update(parse_chain_set(load_yaml(path)))
    for path in extra or []:
        chains.update(parse_chain_set(load_yaml(Path(path))))
    return chains


def _fmt_params(params: dict) -> str:
    if not params:
        return ""
    return " ".join(f"{k}={v}" for k, v in sorted(params.items()))


def _cmd_explain(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"no such file: {manifest_path}", file=sys.stderr)
        return 2

    # `validate` already reports unreadable input as a finding rather than a
    # traceback; `explain` must behave the same way. Someone pointing this at
    # the wrong file should be told which file and why, not shown a stack.
    try:
        manifest = load_yaml(manifest_path)
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        print(f"FAIL {manifest_path}  (unreadable)")
        print(f"     x read_error")
        for line in f"{type(exc).__name__}: {exc}".splitlines():
            print(f"       {line}")
        return 1

    if not isinstance(manifest, Mapping) or "manifest_version" not in manifest:
        print(f"FAIL {manifest_path}  (not a manifest)")
        print("     x unknown_document")
        print("       `emet explain` needs a body manifest — a document with a")
        print("       top-level `manifest_version` key. For soul bundles and")
        print("       motion packs, use `emet validate`.")
        return 1

    registry = _Registry.discover()

    report = validate_manifest(manifest, registry=registry)
    if not report.ok:
        print(f"FAIL {manifest_path}  (manifest)")
        for finding in report.errors:
            where = f" {finding.path}" if finding.path else ""
            print(f"     x {finding.code}{where}")
            for line in finding.message.splitlines():
                print(f"       {line}")
        print()
        print("Cannot resolve chains against a manifest that does not validate.")
        return 1

    try:
        chains = _load_chains(args.chains)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  (chains)")
        print(f"     x chain_error")
        for line in f"{type(exc).__name__}: {exc}".splitlines():
            print(f"       {line}")
        return 1
    caps = descriptors_from_manifest(manifest)
    table = resolve(chains, caps)

    body = (manifest.get("body") or {}).get("id", "?")
    print(f"BINDING TABLE  —  {manifest_path}   (body: {body})")
    print(f"{len(caps)} capabilities, {len(table)} intents, "
          f"{len(table.hardware_bound)} bound to hardware, "
          f"{len(table.voice_bound)} to voice")
    print()
    print("  These bindings are what the manifest CLAIMS. The engine resolves")
    print("  against what plugins report after starting, so a part that fails")
    print("  to initialise will fall through where this shows it binding.")
    print()

    width = max((len(i) for i in table), default=20)
    for intent in table:
        b = table[intent]
        mark = " " if not b.degraded else "~"
        params = _fmt_params(dict(b.params))
        print(f"  {mark} {intent:<{width}}  {b.target:<12} {b.action:<12} {params}")
        if args.why and b.skipped:
            for skip in b.skipped:
                print(f"      skipped {skip}")

    degraded = [i for i in table if table[i].degraded]

    unused = table.unused_capabilities()
    if unused:
        reasons = unused_reasons(chains, caps, table)
        print()
        print("  Actuators no intent binds to:")
        for cap_id in unused:
            print(f"    {cap_id} — {reasons.get(cap_id, 'bound by nothing')}")

    if args.why and not degraded:
        # Silence here would read as "the flag did nothing" rather than as the
        # good news it is.
        print()
        print("  Nothing degraded: every intent bound to the first rung it asked for.")
        print("  This body can express everything the chains describe.")
    elif not args.why and degraded:
        print()
        print(f"  ~ marks the {len(degraded)} intent(s) that fell short of their best "
              f"rung. Re-run with --why for the reason.")

    return 0


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
        help="require every driver plugin to be installed — what the engine does at "
             "boot. Off by default, because describing hardware you have not wired "
             "yet is a normal thing to do.",
    )
    validate.add_argument(
        "--pair",
        action="store_true",
        help="with exactly two paths, also check cross-document rules between a "
             "manifest and a soul",
    )
    validate.add_argument("--json", action="store_true", help="machine-readable output")
    validate.set_defaults(func=_cmd_validate)

    explain = sub.add_parser(
        "explain",
        help="show what each intent means on a particular body",
    )
    explain.add_argument("manifest", metavar="MANIFEST")
    explain.add_argument(
        "--chains",
        action="append",
        metavar="FILE",
        help="override chain file; may be repeated, later files win",
    )
    explain.add_argument(
        "--why",
        action="store_true",
        help="for every degraded binding, list the rungs that were skipped and why",
    )
    explain.set_defaults(func=_cmd_explain)

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
