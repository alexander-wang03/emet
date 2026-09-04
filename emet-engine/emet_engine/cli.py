"""`emet-listen` — bring a body up and print what it hears.

The 0.3 milestone in one command. It does not understand anything yet: it
brings up a microphone and a wake detector, and says so each time the robot
hears its name. Speech recognition arrives in 0.4.

Useful before that, though. `--replay` points the same path at a recording
instead of a microphone, so a wake failure somebody reports can be reproduced
exactly, on a machine that was never in their room.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import sys
from pathlib import Path
from typing import Any

from emet_sdk.discovery import PluginRegistry
from emet_sdk.validate import (
    MissingPluginError,
    ValidationError,
    load_yaml,
    validate_manifest,
    validate_soul,
)

from emet_engine.session import EngineError, ListenSession
from emet_engine.turn import EndReason

__all__ = ["main"]


def _load(path: Path, kind: str, registry: PluginRegistry) -> dict[str, Any]:
    try:
        doc = load_yaml(path)
    except Exception as exc:
        raise EngineError(f"could not read {path}: {exc}") from exc
    report = (
        validate_manifest(doc, registry=registry) if kind == "manifest" else validate_soul(doc)
    )
    for finding in report.warnings:
        print(f"  ! {finding.code} {finding.path}", file=sys.stderr)
    if not report.ok:
        for finding in report.errors:
            print(f"  x {finding.code} {finding.path}\n    {finding.message}", file=sys.stderr)
        raise EngineError(f"{path.name} is not a valid {kind}")
    return doc


def _replay(manifest: dict[str, Any], wav: str) -> dict[str, Any]:
    """Point the body at a recording instead of its microphone."""
    manifest = copy.deepcopy(manifest)
    manifest.setdefault("audio", {})["input"] = {
        "source": "wav",
        "params": {"path": wav},
        # `device` is required by the schema and meaningless for a file. The
        # session never reads it; it is here so the document still validates.
        "device": "file",
    }
    return manifest


async def _run(args: argparse.Namespace) -> int:
    registry = PluginRegistry.discover()
    if not registry:
        print(
            "no plugins are installed, so there is no microphone and no wake\n"
            "engine to use. Install the hardware layer:\n"
            "    pip install 'emet-hal[audio,wake]'",
            file=sys.stderr,
        )
        return 2

    manifest = _load(Path(args.manifest), "manifest", registry)
    soul = _load(Path(args.soul), "soul", registry)
    if args.replay:
        manifest = _replay(manifest, args.replay)

    session = ListenSession(manifest, soul, registry=registry)
    async with session:
        assert session.descriptor is not None and session.format is not None
        print(
            f"listening for {session.phrase!r}\n"
            f"  engine   {session.engine_name}\n"
            f"  source   {session.source_name}\n"
            f"  audio    {session.format.sample_rate} Hz, "
            f"{session.format.frame_ms:.0f} ms frames\n"
            f"  patience {session.patience_ms} ms"
        )
        print("  (ctrl-c to stop)\n" if not args.replay else "")

        heard = 0
        async for event, utterance in session.turns():
            heard += 1
            print(f"  heard {event.phrase!r}  (confidence {event.confidence:.2f})")
            if utterance.had_speech:
                # 0.4 hands this audio to speech recognition. Until then the
                # useful thing to show is that the turn was bounded correctly.
                print(
                    f"    then {utterance.duration_ms / 1000:.1f}s of speech, "
                    f"ended on {utterance.reason.value}"
                )
            elif utterance.reason is EndReason.SOURCE_ENDED:
                print("    the recording ended before anything followed.")
            else:
                print("    then nothing. probably a false wake.")

        # Only reached when the source ends, which means a replay finished.
        print(f"\nsource ended. heard it {heard} time(s).")
        _finish(session, args)
    return 0


def _finish(session: ListenSession, args: argparse.Namespace) -> None:
    """Report what the run cost, if asked, and always report what it lost."""
    if args.stats:
        # `live` from the source that actually ran, not from the flag: only a
        # real sound card can drift, and claiming otherwise for a file would
        # be inventing a measurement.
        print("\n" + session.stats.report(live=session.source_name != "wav"))
    if session.dropped:
        print(
            f"\nwarning: {session.dropped} frame(s) were dropped, so wake words "
            f"may have been missed. The loop is not keeping up with the audio."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emet-listen",
        description="Bring up a body and print each time it hears its name.",
    )
    parser.add_argument("manifest", help="body manifest (yaml)")
    parser.add_argument("soul", help="soul bundle (yaml)")
    parser.add_argument(
        "--replay",
        metavar="WAV",
        help="read this 16-bit mono wav instead of the microphone, so a wake "
        "failure can be reproduced away from the room it happened in",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="report whether the loop kept up: per-frame timings, frames over "
        "budget, and the real-time factor. This is how the ten-minute soak in "
        "the 0.3 acceptance criteria is actually checked",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0
    except (EngineError, MissingPluginError, ValidationError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
