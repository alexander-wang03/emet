#!/usr/bin/env python3
"""Replay the voice bank through emet-listen and compare with the goldens.

    python tools/bank/replay.py BANK [--search DIR ...] [--only ID ...]
                                [--update] [--start-mean carried|pi-manifest|default]
                                [--cassette PATH [--cassette-request JSON]]
                                [--misses FILE] [--report FILE] [--work DIR --keep]

Each recording the bank lists is replayed through `emet_engine.cli.main`
with `--replay --transcribe --stats`, in a process of its own, with a body
manifest and a soul this script writes: the wav source, the null sink,
pocketsphinx at the start mean named, and the mock transcriber with
`record_turns`, so every byte the transcriber was fed is kept. `--keys`
names an empty file and `--state` a scratch directory, so no key and no
carried mean reaches the run.

**What a replay records**, per wake: the take whose slot holds the wake's
frame end; that frame end, found by locating the bytes fed after the
handover in the recording; the figures `--stats` prints under the wake
(`lag N ms, name cut N ms, level stop alone N ms`, and `; the last N ms
handed over as heard` when the tail was cut short); the sha256 of the
handover, the frames fed before the first frame read after the wake, front
padding included; the sha256 of every byte fed in the turn; and the final
with its cassette hit or miss. Per file, the footer's frames, wakes, turns
and `name cut N of M`. Each handover is rebuilt from the recording and the
printed figures and must equal what was fed, so a figure misread here, or
a cut that no longer matches its line, stops the run.

**Golden files.** With `--update` the records are written to
`tools/bank/golden/<platform>/<id>.json` (`win32-amd64`, `linux-aarch64`,
`linux-x86_64`); without it they are compared with those files exactly,
one line printed for each wake that changed, and the exit code is 1 on any
difference or problem. A golden is compared only under the conditions it
was made under (threshold, start mean, cassette); other conditions are
refused, never compared. Timings are never golden: they depend on the
machine and its load.

**Guards**, from releases/v0.5.3/harness/cut_harness.py: `sounddevice`
cannot be imported in the replay's process, so no microphone or speaker
can open; every socket connection or name lookup that is not loopback is
refused and counted, and a run with one is a failed run; before the run,
the manifest and soul it was given must select the null sink (and that
name must load `emet_hal.audio.NullSink`) and the mock transcriber, through
the same `emet_sdk.models` function the engine uses; after it, the header
must say `source wav` and `stt mock`. No engine function is patched: a
patched function the session stopped calling once made every 0.5.3 knob
run read as hidden while it exited 0 (`RELEASE_PROCESS.md` §10). The
figures come from the console, and the bytes from the mock's own record.

**Start means.** A replay starts from the manifest's mean by design
(ROADMAP, decided 2026-09-28). `carried` (the default) is the mean the
reference body's state file carried on 2026-09-30, c0 37.8, which the
2026-10-06 wake study and the Deepgram replays 2 to 10 started from
(`releases/v0.5.3/harness/means/carried.yaml`); `pi-manifest` is the 0.4-era
value in `~/pi-speakerphone.yaml`, c0 33.9; `default` names none, so
pocketsphinx starts from its own (it reads back as 40,3,-1).

**Cassettes and misses.** `--cassette` hands the mock a cassette file or a
directory of them (FORMAT.md), and the request the run stands for: the
one `--cassette-request` names, or else the one the shipped Deepgram
plugin makes. Only turns recorded under that request answer, so a turn
recorded with another query (`smart_format=true` in 0.5.3's replays) never
passes for today's. A turn that misses is listed, never failed;
`--misses FILE` writes the list as JSON with each missed turn's bytes
beside it, for `paid_replay.py`. The bytes are the speaker's voice, so the
file belongs in the bank, never in the repository.

The standard library and the four packages only. Nothing is written into
the bank. The work directory holds the turns' bytes and is deleted at the
end unless `--keep`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden"
sys.path.insert(0, str(HERE))

import manifest as bank_files  # noqa: E402  the standard library only, so safe before the guards

#: The flags every replay runs with, as the golden records them.
FLAGS = ["--replay", "--transcribe", "--stats"]

#: Start means, by name. See the module docstring for where each comes from.
MEANS: dict[str, str | None] = {
    "carried": "37.7991,-4.37088,0.731673,6.52026,-8.2342,-2.66036,-4.19786,"
               "-1.12147,3.13388,-2.69826,0.0248197,-2.71238,-0.93077",
    "pi-manifest": "33.908,-4.91861,-0.0106799,6.36848,0.139045,3.90264,2.22221,"
                   "2.65637,4.60943,2.24969,1.8505,1.37519,1.47413",
    "default": None,
}

#: pocketsphinx_wake.DEFAULT_THRESHOLD, written out as the harness's means did.
THRESHOLD = "1e-15"

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# ======================================================== the replay's process


def _host(address: Any) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    if isinstance(address, (bytes, bytearray)):
        return bytes(address).decode("ascii", "replace")
    return str(address)


def install_guards(refused: list[str]) -> None:
    """Before anything from emet is imported: no PortAudio, no network.

    Python's socket calls are refused for any host but loopback, and an
    audit hook refuses the same underneath them, so a library that calls
    the C socket module directly is stopped too. On Windows the event
    loop's proactor connects through ConnectEx, which no socket audit event
    sees (cut_deepgram.py, checked on the laptop, 2026-10-01), so it is
    guarded too. Loopback stays open for the event loop's self-pipe."""
    import socket

    sys.modules["sounddevice"] = None  # type: ignore[assignment]  # no microphone, no speaker, ever
    for name in [n for n in os.environ if n.startswith("EMET_")]:
        os.environ.pop(name)  # no key or keys-file override reaches the run; values never read

    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def refuse(what: str, host: str) -> OSError:
        refused.append(f"{what} {host}")
        return PermissionError(f"replay.py: {what} {host} refused, a replay uses no network")

    def guarded_connect(self, address):
        if _host(address) not in LOOPBACK:
            raise refuse("connect", _host(address))
        return connect(self, address)

    def guarded_connect_ex(self, address):
        if _host(address) not in LOOPBACK:
            raise refuse("connect_ex", _host(address))
        return connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and _host(host) not in LOOPBACK:
            raise refuse("getaddrinfo", _host(host))
        return getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    if sys.platform == "win32":
        import asyncio.windows_events as windows_events

        iocp_connect = windows_events.IocpProactor.connect

        def guarded_iocp_connect(self, conn, address):
            if _host(address) not in LOOPBACK:
                raise refuse("ConnectEx", _host(address))
            return iocp_connect(self, conn, address)

        windows_events.IocpProactor.connect = guarded_iocp_connect  # type: ignore[method-assign]

    def audit(event: str, args: tuple) -> None:
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            address = args[1] if len(args) > 1 else None
            if address is not None and not isinstance(address, (int,)) and _host(address) not in LOOPBACK:
                raise refuse(f"audit {event}", _host(address))
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            if args and args[0] is not None and _host(args[0]) not in LOOPBACK:
                raise refuse(f"audit {event}", _host(args[0]))

    sys.addaudithook(audit)


def preflight(manifest_path: Path, soul_path: Path) -> list[str]:
    """The sink and the transcriber the run will use, judged before it
    starts by the functions the engine itself calls. Empty when both are
    the harmless ones."""
    from emet_sdk.discovery import PluginRegistry
    from emet_sdk.models import stt_selection
    from emet_sdk.validate import load_yaml

    body, soul = load_yaml(manifest_path), load_yaml(soul_path)
    found = []
    sink = str(((body.get("audio") or {}).get("output") or {}).get("sink") or "")
    if sink != "null":
        found.append(f"the sink is {sink!r}, not null")
    else:
        cls = PluginRegistry.discover().load_audio_out("null")
        if f"{cls.__module__}.{cls.__qualname__}" != "emet_hal.audio.NullSink":
            found.append(f"the sink named null loads {cls.__module__}.{cls.__qualname__}, not emet_hal.audio.NullSink")
    stt = stt_selection(body, soul) or {}
    if stt.get("provider") != "mock":
        found.append(f"the transcriber is {stt.get('provider')!r}, not mock")
    if not (stt.get("params") or {}).get("record_turns"):
        found.append("the mock transcriber records no turns")
    return found


def run_child(config_path: Path) -> int:
    """The replay itself, in its own process: guards, preflight, cli.main."""
    refused: list[str] = []
    install_guards(refused)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"exit": None, "refused": refused, "preflight": []}
    found = preflight(Path(cfg["manifest"]), Path(cfg["soul"]))
    out["preflight"] = found
    if not found:
        import emet_engine.cli as cli

        argv = [cfg["manifest"], cfg["soul"], "--replay", cfg["wav"], "--transcribe", "--stats",
                "--keys", cfg["keys"], "--state", cfg["state"]]
        with open(cfg["console"], "w", encoding="utf-8", newline="\n") as console, contextlib.redirect_stdout(console):
            out["exit"] = cli.main(argv)
    Path(cfg["result"]).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


# ========================================================= files for the run


def body_yaml(mean: str | None, threshold: str, turns_dir: Path, cassette: str | None,
              request: dict | None) -> str:
    """The reference body as the harness replayed it (means/carried.yaml),
    with the mock transcriber taking the stage over. JSON strings are YAML
    double-quoted strings, so paths with backslashes survive."""
    q = json.dumps
    wake = [f"      threshold: {float(threshold):.1e}"]
    if mean is not None:
        wake.append(f"      cmninit: {q(mean)}")
    stt = [f"      record_turns: {q(str(turns_dir))}"]
    if cassette is not None:
        stt.append(f"      cassette: {q(cassette)}")
    if request is not None:
        stt.append(f"      cassette_request: {q(request)}")
    return "\n".join([
        "# Written by tools/bank/replay.py for one replay. The reference body with the",
        "# null sink and the mock transcriber; nothing here can open a device.",
        'manifest_version: "0.1"',
        "body:",
        "  id: pi_speakerphone",
        '  name: "a Raspberry Pi with a USB microphone and speaker"',
        "  scale: desk",
        "  power: plugged_in",
        '  description: "A bare single-board computer on a desk with a microphone and a speaker plugged into it."',
        "compute: {platform: raspberrypi5, ram_gb: 8, accelerator: none}",
        "audio:",
        '  input: {device: "USB", sample_rate: 16000, channels: 1, aec: none, doa: false}',
        '  output: {sink: "null", device: "USB", gain_db: -6.0, sample_rate: 48000}',
        "  wake:",
        "    engine: pocketsphinx",
        "    params:",
        *wake,
        "capabilities: []",
        "safety: {estop_gpio: null, max_continuous_motion_s: 30, thermal_throttle_c: 75}",
        "models:",
        "  stt:",
        "    provider: mock",
        "    params:",
        *stt,
        "",
    ])


#: The reference soul as the harness replayed it (emet-soul-mockstt.yaml),
#: with every stage the mock: a replay calls no vendor.
SOUL_YAML = """\
# Written by tools/bank/replay.py: the reference soul, every model the mock.
bundle_version: "0.1"
identity: {name: "Emet", wake_word: "hey emet", line: emet, pronouns: "it/its", author: "The Emet Authors", created: "2026-08-08", license: null}
voice: {rate: 1.0, pitch_shift_semitones: 0}
persona:
  summary: "Earnest and plain-spoken."
  system_prompt: "You are Emet."
  traits: {warmth: 0.7, humor: 0.4, verbosity: 0.4, curiosity: 0.9, formality: 0.3}
  drift: {enabled: false, rate: 0.0, locked_traits: []}
  lines: {declined: "I'd rather not answer that one.", failed: "I lost the thread there.", nothing_heard: null}
interaction:
  patience_ms: 900
  extend_on_incomplete: true
  barge_in: true
  backchannel: true
  greeting: {on_recognized: true, on_unknown: true}
idle: {enabled: true, interval_s: [20, 90], weights: {settle: 0.4, look_around: 0.4, fidget: 0.2}, proactivity: 0.0}
memory: {personalization: true, cross_person_disclosure: true, consolidation: nightly, retention_days: 0}
models:
  chat: {provider: mock}
  stt: {provider: mock}
  tts: {provider: mock}
"""


# ============================================================ reading a run

HEARD = re.compile(r"^  heard '.*'  \(confidence [0-9.]+\)$")
LINE = re.compile(r"^    lag (\d+) ms, name cut (\d+) ms, level stop alone (\d+) ms"
                  r"(?:; the last (\d+) ms handed over as heard)?$")
AUDIO = re.compile(r"^  audio    (\d+) Hz, (\d+) ms frames$")
FRAMES = re.compile(r"^  frames +(\d+) ")
WAKES = re.compile(r"^  wakes +(\d+) +turns (\d+)")
NAME_CUT = re.compile(r"^  name cut +(\d+ of \d+) handover")


class RunProblem(RuntimeError):
    """A replay whose record cannot be trusted. It is never compared."""


def read_console(text: str) -> dict[str, Any]:
    """The header's audio format, the figures under each wake, the footer.

    The line under a wake must be the line straight after it: a line found
    anywhere else is a problem, as `body_log_check.py` holds a body's log."""
    lines = text.splitlines()
    wakes: list[dict[str, Any]] = []
    header: dict[str, Any] = {"source": None, "stt": None, "identity": lines[0] if lines else None}
    footer: dict[str, Any] = {"frames": None, "wakes": None, "turns": None, "name_cut": None}
    after_heard = False
    for line in lines:
        if (m := AUDIO.match(line)):
            header["rate"], header["frame_ms"] = int(m.group(1)), int(m.group(2))
        elif line.startswith("  source   "):
            header["source"] = line.split()[1]
        elif line.startswith("  stt      "):
            header["stt"] = line.split()[1]
        elif HEARD.match(line):
            wakes.append({"line": None})
            after_heard = True
            continue
        elif (m := LINE.match(line)):
            if not after_heard:
                raise RunProblem(f"a line under no wake: {line.strip()!r}")
            lag, name, level, short = m.groups()
            wakes[-1]["line"] = {
                "lag_ms": float(lag), "name_ms": float(name), "level_ms": float(level),
                "heard_ms": float(short if short is not None else lag), "cut_short": short is not None,
            }
        elif (m := FRAMES.match(line)):
            footer["frames"] = int(m.group(1))
        elif (m := WAKES.match(line)):
            footer["wakes"], footer["turns"] = int(m.group(1)), int(m.group(2))
        elif (m := NAME_CUT.match(line)):
            footer["name_cut"] = m.group(1)
        after_heard = False
    return {"header": header, "wakes": wakes, "footer": footer}


def read_turns(turns_dir: Path) -> list[dict[str, Any]]:
    """The mock's turns.jsonl, each line checked against its raw file."""
    index = turns_dir / "turns.jsonl"
    if not index.is_file():
        return []
    turns = []
    for n, line in enumerate(index.read_text(encoding="utf-8").splitlines(), 1):
        turn = json.loads(line)
        if turn.get("turn") != n:
            raise RunProblem(f"turns.jsonl line {n} is turn {turn.get('turn')}")
        raw = (turns_dir / f"turn-{n:04d}.raw").read_bytes()
        if len(raw) != turn["bytes"] or sum(turn["feed_sizes"]) != len(raw):
            raise RunProblem(f"turn {n}: {len(raw)} bytes on disk, {turn['bytes']} in turns.jsonl")
        if hashlib.sha256(raw).hexdigest() != turn["fed_sha256"]:
            raise RunProblem(f"turn {n}: its bytes on disk do not hash to turns.jsonl's fed_sha256")
        turn["raw"] = raw
        turns.append(turn)
    return turns


def frame_index(pcm: bytes, frame_bytes: int) -> dict[bytes, list[int]]:
    """Each whole frame of the recording by its digest: where it starts, in frames."""
    index: dict[bytes, list[int]] = {}
    for j in range(len(pcm) // frame_bytes):
        index.setdefault(hashlib.sha256(pcm[j * frame_bytes:(j + 1) * frame_bytes]).digest(), []).append(j)
    return index


def locate_wake(turn: dict, k: int, pcm: bytes, frame_bytes: int, index: dict[bytes, list[int]],
                after_frame: int) -> int:
    """The frame the wake engine fired on ends where the bytes fed after the
    handover begin in the recording: its end, in frames from the start.

    The engine feeds the frames it reads after the wake straight on, so
    those bytes are the recording's from the wake's frame end, frame after
    frame; they are checked all the way. They are looked for from
    `after_frame` on, the end of the audio the turn before consumed. A
    recording that repeats itself byte for byte could put them in two
    places, and a wake placed at the wrong one would carry another take's
    name, so two places are refused. A turn with nothing after the handover
    is a recording that ended on its wake's frame."""
    raw, sizes = turn["raw"], turn["feed_sizes"]
    start = sum(sizes[:k])
    rest = raw[start:]
    whole = len(pcm) // frame_bytes
    if not rest:
        return whole
    if any(s != frame_bytes for s in sizes[k:]):
        raise RunProblem(f"turn {turn['turn']}: a frame after the handover is not {frame_bytes} bytes")
    first = hashlib.sha256(rest[:frame_bytes]).digest()
    found = [j for j in index.get(first, [])
             if j >= after_frame and pcm[j * frame_bytes:j * frame_bytes + len(rest)] == rest]
    if not found:
        raise RunProblem(f"turn {turn['turn']}: the bytes fed after the handover are nowhere in the recording")
    if len(found) > 1:
        raise RunProblem(f"turn {turn['turn']}: the bytes fed after the handover are in the recording at "
                         f"frames {found[:4]}; which one the wake was cannot be told")
    return found[0]


def expected_handover(pcm: bytes, wake_end: int, figures: dict, rate: int, frame_bytes: int) -> bytes:
    """The handover rebuilt from the recording and the printed figures: the
    last `heard_ms` before the wake's frame end, its first `name_ms` written
    as silence, padded at the front to whole frames."""
    heard = 2 * int(round(figures["heard_ms"] * rate / 1000.0))
    name = 2 * int(round(figures["name_ms"] * rate / 1000.0))
    end = 2 * wake_end
    tail = bytes(name) + pcm[end - heard + name:end]
    return bytes((-len(tail)) % frame_bytes) + tail


def read_run(doc: dict, pcm: bytes, console: str, turns: list[dict]) -> dict[str, Any]:
    """One replay as the golden records it, every figure cross-checked."""
    seen = read_console(console)
    header, footer = seen["header"], seen["footer"]
    if header["source"] != "wav":
        raise RunProblem(f"the header's source is {header['source']!r}, not wav")
    if header["stt"] != "mock":
        raise RunProblem(f"the header's transcriber is {header['stt']!r}, not mock")
    rate = header.get("rate")
    if rate != doc["sample_rate"] or not header.get("frame_ms"):
        raise RunProblem(f"the header's audio is {rate} Hz; the recording is {doc['sample_rate']} Hz")
    frame_samples = rate * header["frame_ms"] // 1000
    frame_bytes = 2 * frame_samples
    if None in (footer["frames"], footer["wakes"], footer["turns"]):
        raise RunProblem("the run printed no --stats footer")
    if not len(seen["wakes"]) == footer["wakes"] == footer["turns"] == len(turns):
        raise RunProblem(f"{len(seen['wakes'])} wakes printed, the footer says {footer['wakes']} wakes and "
                         f"{footer['turns']} turns, and the mock recorded {len(turns)} turns")
    index = frame_index(pcm, frame_bytes)
    wakes, after = [], 0
    for n, (printed, turn) in enumerate(zip(seen["wakes"], turns), 1):
        fig = printed["line"]
        k = 0 if fig is None else math.ceil(int(round(fig["heard_ms"] * rate / 1000.0)) / frame_samples)
        end_frame = locate_wake(turn, k, pcm, frame_bytes, index, after)
        handover = turn["raw"][:sum(turn["feed_sizes"][:k])]
        # The next wake comes after every frame this turn read on from its wake.
        after = end_frame + (len(turn["raw"]) - len(handover)) // frame_bytes
        end = end_frame * frame_samples
        if fig is not None and handover != expected_handover(pcm, end, fig, rate, frame_bytes):
            raise RunProblem(f"wake {n}: the handover fed is not the recording's, cut as its line says")
        take = bank_files.take_at(doc, end)
        turn["handover_feeds"] = k
        wakes.append({
            "n": n, "take": take["id"] if take else None, "wake_end_sample": end,
            "lag_ms": None if fig is None else fig["lag_ms"],
            "name_ms": None if fig is None else fig["name_ms"],
            "level_ms": None if fig is None else fig["level_ms"],
            "heard_ms": None if fig is None else fig["heard_ms"],
            "cut_short": False if fig is None else fig["cut_short"],
            "handover_sha256": None if fig is None else hashlib.sha256(handover).hexdigest(),
            "fed_sha256": turn["fed_sha256"], "final": turn["final"], "cassette": turn["cassette"],
        })
    lines = sum(1 for w in seen["wakes"] if w["line"] is not None)
    if footer["name_cut"] is not None and int(footer["name_cut"].split(" of ")[1]) != lines:
        raise RunProblem(f"the footer counts {footer['name_cut']} handovers and {lines} wakes printed a line")
    all_fed = b"".join(t["raw"] for t in turns)
    return {
        "footer": {k: footer[k] for k in ("frames", "wakes", "turns", "name_cut")},
        "wakes": wakes,
        "identity": header["identity"],
        "all_fed_sha256": hashlib.sha256(all_fed).hexdigest(),
        "all_fed_bytes": len(all_fed),
        "misses": [t for t in turns if t["cassette"] == "miss"],
    }


# ============================================================== comparing


def short(v: Any) -> str:
    if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v):
        return v[:8]
    if isinstance(v, float) and v.is_integer():
        return f"{v:.0f}"
    return json.dumps(v) if isinstance(v, str) else str(v)


def compare(old: dict, new: dict) -> list[str]:
    """One line for each wake that changed, one for the footer, and one for
    the conditions, each naming every field that changed."""
    rid = new["recording"]
    lines = []
    if old["sha256"] != new["sha256"]:
        return [f"{rid}: the golden was made from another recording ({old['sha256'][:8]}, this is {new['sha256'][:8]})"]
    if old["conditions"] != new["conditions"]:
        keys = list(dict.fromkeys([*old["conditions"], *new["conditions"]]))
        lines.append(f"{rid} conditions: " + ", ".join(
            f"{k} {short(old['conditions'].get(k))} -> {short(new['conditions'].get(k))}"
            for k in keys if old["conditions"].get(k) != new["conditions"].get(k)))
    if old["footer"] != new["footer"]:
        changed = [f"{k} {short(old['footer'][k])} -> {short(new['footer'][k])}"
                   for k in new["footer"] if old["footer"].get(k) != new["footer"][k]]
        lines.append(f"{rid} footer: " + ", ".join(changed))
    for i in range(max(len(old["wakes"]), len(new["wakes"]))):
        a = old["wakes"][i] if i < len(old["wakes"]) else None
        b = new["wakes"][i] if i < len(new["wakes"]) else None
        if a == b:
            continue
        if a is None:
            lines.append(f"{rid} wake {b['n']}: new, take {b['take']}, at sample {b['wake_end_sample']}")
        elif b is None:
            lines.append(f"{rid} wake {a['n']}: gone, was take {a['take']} at sample {a['wake_end_sample']}")
        else:
            changed = [f"{k} {short(a.get(k))} -> {short(b[k])}" for k in b if a.get(k) != b[k]]
            lines.append(f"{rid} wake {b['n']} ({b['take']}): " + ", ".join(changed))
    return lines


def platform_name() -> str:
    """win32-amd64, linux-aarch64, linux-x86_64: the golden's folder."""
    return f"{sys.platform}-{_platform.machine().lower()}"


# ================================================================== the run


def replay_one(doc: dict, wav: Path, work: Path, conditions: dict, cassette: str | None,
               request: dict | None) -> dict[str, Any]:
    """Replay one recording in a child process and read what it did."""
    if work.exists():
        shutil.rmtree(work)
    (work / "state").mkdir(parents=True)
    turns_dir = work / "turns"
    keys = work / "empty-keys.env"
    keys.write_text("", encoding="utf-8")
    (work / "body.yaml").write_text(body_yaml(conditions["cmninit"], conditions["threshold"], turns_dir,
                                              cassette, request), encoding="utf-8")
    (work / "soul.yaml").write_text(SOUL_YAML, encoding="utf-8")
    cfg = {
        "manifest": str(work / "body.yaml"), "soul": str(work / "soul.yaml"), "wav": str(wav),
        "keys": str(keys), "state": str(work / "state"), "console": str(work / "console.txt"),
        "result": str(work / "result.json"),
    }
    (work / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EMET_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(work / "config.json")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    (work / "stderr.txt").write_text(done.stderr or "", encoding="utf-8")
    if done.returncode != 0 or not (work / "result.json").is_file():
        raise RunProblem(f"the replay's process exited {done.returncode}: {(done.stderr or '').strip()[-400:]}")
    result = json.loads((work / "result.json").read_text(encoding="utf-8"))
    if result["preflight"]:
        raise RunProblem("refused before the run: " + "; ".join(result["preflight"]))
    if result["refused"]:
        raise RunProblem(f"{len(result['refused'])} network attempt(s) refused: {result['refused'][:3]}")
    if result["exit"] != 0:
        raise RunProblem(f"emet-listen exited {result['exit']}: {(done.stderr or '').strip()[-400:]}")
    with wave.open(str(wav), "rb") as w:
        pcm = w.readframes(w.getnframes())
    record = read_run(doc, pcm, (work / "console.txt").read_text(encoding="utf-8"), read_turns(turns_dir))
    record["network_refused"] = 0
    return record


def golden_doc(doc: dict, record: dict, conditions: dict, plat: str) -> dict:
    return {
        "format": bank_files.FORMATS["golden"],
        "recording": doc["id"], "sha256": doc["sha256"], "platform": plat,
        "conditions": conditions, "footer": record["footer"], "wakes": record["wakes"],
    }


def shipped_request(rate: int) -> dict[str, Any]:
    """The request the shipped Deepgram plugin makes at this rate, as a
    cassette records it: every parameter of its query, defaults included.
    A cassette turn recorded under another query, such as the replays of
    0.5.3 with `smart_format=true` that wrote "22" for "two plus two", is
    then never a candidate. Asked of a plugin that is never started."""
    from emet_providers import cassette
    from emet_providers.deepgram import DeepgramTranscriber
    from emet_sdk.types import AudioFormat

    plugin = DeepgramTranscriber({"provider": "deepgram"}, AudioFormat(sample_rate=rate))
    return cassette.request_for("deepgram", plugin.query())


def write_misses(path: Path, misses: list[dict]) -> None:
    """The paid list: each missed turn's facts, and its bytes beside the list."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for m in misses:
        raw_name = f"{m['recording']}-turn-{m['turn']:04d}.raw"
        (path.parent / raw_name).write_bytes(m.pop("raw"))
        out.append({**m, "raw": raw_name})
    path.write_text(json.dumps({"format": "emet-misses/1", "turns": out}, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--child"]:
        return run_child(Path(argv[1]))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path, help="the bank's root, the folder holding bank.json")
    ap.add_argument("--search", type=Path, action="append", default=[], metavar="DIR",
                    help="a directory to look in for a recording not at its manifest's path")
    ap.add_argument("--by-hash", action="store_true", help="also take any .wav there whose sha256 matches")
    ap.add_argument("--only", action="append", default=[], metavar="ID", help="replay only this recording")
    ap.add_argument("--update", action="store_true", help="write the goldens instead of comparing")
    ap.add_argument("--golden", type=Path, default=GOLDEN, help="the golden folder (default %(default)s)")
    ap.add_argument("--platform", default=platform_name(), help="the golden's platform (default %(default)s)")
    ap.add_argument("--start-mean", choices=sorted(MEANS), default="carried")
    ap.add_argument("--cassette", help="a cassette file or a directory of them, for the mock transcriber")
    ap.add_argument("--cassette-request", metavar="JSON",
                    help="the request the run stands for (provider, model, sample_rate, query); with --cassette "
                         "and none given, the request the shipped Deepgram plugin makes, so a turn recorded "
                         "under another query never answers")
    ap.add_argument("--misses", type=Path, metavar="FILE", help="write the turns the cassette missed, with their bytes")
    ap.add_argument("--report", type=Path, metavar="FILE", help="write every record, footers and streams, as JSON")
    ap.add_argument("--work", type=Path, help="where the runs' files go (default: a new temporary folder)")
    ap.add_argument("--keep", action="store_true", help="keep the work folder")
    args = ap.parse_args(argv)
    if args.update and args.cassette:
        ap.error("goldens are written without a cassette, so a cassette run only compares or reports")
    request = json.loads(args.cassette_request) if args.cassette_request else None
    conditions = {"threshold": THRESHOLD, "start_mean": args.start_mean, "cmninit": MEANS[args.start_mean],
                  "flags": FLAGS}

    root = args.bank.resolve()
    try:
        bank = bank_files.read(root / "bank.json", "bank")
        ids = [bank_files.read(root / rel, "manifest")["id"] for rel in bank["files"]]
    except bank_files.BankError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1
    unknown = [i for i in args.only if i not in ids]
    if unknown:
        print(f"replay: no recording in the bank is called {', '.join(unknown)}; it holds {', '.join(ids)}",
              file=sys.stderr)
        return 2
    work =(args.work.resolve() if args.work else Path(tempfile.mkdtemp(prefix="emet-replay-")))
    plat_dir = args.golden / args.platform
    problems = changed = 0
    report: dict[str, Any] = {"platform": args.platform, "conditions": conditions, "cassette": args.cassette,
                              "recordings": {}}
    misses: list[dict] = []
    try:
        for rel in bank["files"]:
            doc = bank_files.read(root / rel, "manifest")
            if args.only and doc["id"] not in args.only:
                continue
            wav = bank_files.find_audio(doc, root, args.search, by_hash=args.by_hash)
            if wav is None:
                print(f"{doc['id']}: PROBLEM no audio with sha256 {doc['sha256'][:12]}...")
                problems += 1
                continue
            wanted = request
            if args.cassette and wanted is None:
                wanted = shipped_request(doc["sample_rate"])
            report.setdefault("cassette_request", wanted)
            try:
                record = replay_one(doc, wav, work / doc["id"], conditions, args.cassette, wanted)
            except RunProblem as exc:
                print(f"{doc['id']}: PROBLEM {exc}")
                problems += 1
                continue
            for m in record.pop("misses"):
                wake = record["wakes"][m["turn"] - 1]
                misses.append({"recording": doc["id"], "recording_sha256": doc["sha256"], "turn": m["turn"],
                               "take": wake["take"], "fed_sha256": m["fed_sha256"], "fed_bytes": m["bytes"],
                               "feed_sizes": m["feed_sizes"], "handover_feeds": m["handover_feeds"],
                               "raw": m["raw"]})
            report["recordings"][doc["id"]] = record
            new = golden_doc(doc, record, conditions, args.platform)
            path = plat_dir / f"{doc['id']}.json"
            f = record["footer"]
            summary = (f"{f['wakes']} wakes, {f['turns']} turns, name cut {f['name_cut'] or 'none'}, "
                       f"fed {record['all_fed_sha256'][:8]}")
            hits = sum(1 for w in record["wakes"] if w["cassette"] == "hit")
            if args.cassette:
                summary += f", cassette {hits} hit and {sum(1 for w in record['wakes'] if w['cassette'] == 'miss')} miss"
            old = None
            if path.is_file():
                try:
                    old = bank_files.read(path, "golden")
                except bank_files.BankError as exc:
                    print(f"{doc['id']}: PROBLEM the golden is unsound: {exc}")
                    problems += 1
                    continue
            if args.update:
                diff = compare(old, new) if old is not None and old["conditions"] == conditions else []
                bank_files.write(path, new, "golden")
                print(f"{doc['id']}: wrote {path.name}, {summary}")
                for line in diff:
                    print(f"  changed: {line}")
                continue
            if old is None:
                print(f"{doc['id']}: NO GOLDEN for {args.platform}; {summary}")
                changed += 1
                continue
            want = {**old["conditions"]}
            if want != conditions:
                print(f"{doc['id']}: PROBLEM the golden was made under {want}, this run is {conditions}")
                problems += 1
                continue
            if args.cassette:
                # A cassette run is held to the golden's audio; its finals are reported.
                new = {**new, "wakes": [{**w, "final": o.get("final"), "cassette": o.get("cassette")}
                                        for w, o in zip(new["wakes"], old["wakes"])] + new["wakes"][len(old["wakes"]):]}
            diff = compare(old, new)
            if diff:
                changed += 1
                print(f"{doc['id']}: CHANGED, {summary}")
                for line in diff:
                    print(f"  {line}")
            else:
                print(f"{doc['id']}: same as the golden, {summary}")
    finally:
        if args.keep:
            print(f"replay: the runs' files are in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    if args.misses:
        write_misses(args.misses, misses)
        print(f"replay: {len(misses)} missed turn(s) written to {args.misses}")
    else:
        for m in misses:
            m.pop("raw", None)
    if args.report:
        report["misses"] = misses if not args.misses else len(misses)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    replayed = len(report["recordings"])
    print(f"total: {replayed} replayed on {args.platform} from the {args.start_mean} mean, "
          f"{changed} changed or without a golden, {problems} problem(s)")
    return 1 if problems or (changed and not args.update) else 0


if __name__ == "__main__":
    raise SystemExit(main())
