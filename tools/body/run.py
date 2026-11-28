#!/usr/bin/env python3
"""Run the body's checks on the reference body over SSH, from the laptop.

The agent, or the maintainer by hand, runs this on the laptop. It drives the
reference body, a Raspberry Pi 5 reached as `emet-pi`, and brings the
evidence home:

    preflight   uptime, load, disk, temperature and throttling, recorded;
                it refuses while an emet-listen, emet-talk, arecord, aplay
                or the bank's record.py runs on the body, while a sound
                card's PCM is open, or while ~/emet-ci/lock exists, so the
                runner never takes the robot from the maintainer
    sync        the code under test into ~/emet-ci/emet: the laptop's
                working tree (`--tree`), or a pushed commit fetched
                anonymously from the public repository (`--commit REF`)
    install     pip in ~/emet-ci/.venv upgraded to RUNNER_PIP, then the
                release process's install line with the provider extras and
                piper, as the maintainer's venv on the body has them, again
                whenever a pyproject.toml or the pip pin has changed
    body        the body's fingerprint: the maintainer's manifest hashed in
                canonical form and the carried state file hashed as it is,
                so the run names the body it ran on; never a step, and a
                file that is missing is a note and never a failure
    audit       pip-audit over the runner's venv, from a venv of its own,
                installed from the hashed pins of tools/ci/requirements.txt;
                its findings are recorded and never fail the run
    suites      the four packages' tests, collected, passed and skipped
                counted per package
    checks      tools/ci.py layering where the tree has it, and otherwise
                tools/check_layering.py, release_check.py, check_style.py
    bank        the voice bank's replays, tools/bank/replay.py over
                ~/voicebank/; it passes only when a recording was held to
                a golden, and it names each recording of a sitting that has
                no golden yet, which replay.py holds to nothing
    budgets     the plan's budget table: boot, the first chunk at the null
                sink and the turn gap's median on the local path, the
                per-frame mean and 99th percentile, frames over 80 ms,
                resident memory and idle CPU, each held to the last
                release's on this body, and the board lines beside them,
                recorded and never judged
    speaker     the real speaker with no microphone: --replay with --echo,
                --replay with --speak, and emet-talk --replay, the last
                writing an events file
    live        the microphone: a plain run of 60 s, emet-talk with no
                flags for 60 s, and --stats for ten minutes, each with
                nobody speaking, the last writing an events file
    netcut      with --netcut SECONDS: port 443 cut with SSH up and put back,
                through the root-owned emet-netcut and its one sudo rule
    soak        with --soak MINUTES: the bank's recordings at real time
                through one emet-listen with the null sink, detached on the
                body and sampled each minute, then once under -X dev; flat
                or not by Stage 4's exit. It runs last, after the speaker
                and the microphone, so it never takes their window
    collect     the run's text files to releases/vX.Y.Z/body/<date>/,
                body_log_check.py on each typescript, the date each body
                flag of RELEASE_PROCESS.md §4 last ran, read from the logs,
                the flags this run itself exercised, read from its own
                typescripts, and each events file's turns, read with
                tools/events_file.py; with `--sheet`, the release sitting's
                ratings joined to its turns in sitting.json

**What it writes.** `run.json`, format `emet-body-run/2`, and `report.txt`.
Format 2 keeps every field of format 1 and adds `body`, the fingerprint, and
`flags_run`, one entry for each flag and way (live or replay) that a speaker
or live step of this run exercised and passed. The merge gate,
`tools/release_audit.py`, reads both. `audit`, `soak` and `budgets` are there
when their steps ran; tools/body/README.md has the fields.

**Three modes take no steps:** `--soak-collect` brings home a soak that
outlived the laptop's run, `--nightly-report` reads home the reports
tools/body/nightly.sh writes each night on the body, and `--budget-table`
prints a run's budget table without reaching the body.

**The speaker and the microphone** are used only inside a window the
maintainer allowed in a reply beforehand (his decision of 2026-10-27), named
with `--window START END`. Each such step refuses when it would start before
the window or end after it, and when any minute of it falls between 22:00
and 08:00 unless `--allow-night` is given. The steps keep no audio on disk
and send none off the body: the runner's manifest gives every model stage to
a mock (or the voice to local Piper with `--voice piper`), `--keys` names an
empty file, and after each step the runner looks for an audio file written
since the step began and fails the step if it finds one. `--state` names a
fresh copy of the carried state, so the maintainer's own file is read and
never written; its hash is compared before and after.

**How long a window.** Each step is held to the window by its bound, the
longest it may run: 300 s for each of the three replays, 80 s for the plain
run, 80 s for emet-talk and 620 s for the ten minutes, 1,680 s end to end,
and each keeps 30 s before the window closes. So 28 and a half minutes hold
both steps once the first has started. A replay ends by itself when its
file does, so a run uses less than its bound. In a run of every step the
sync, the install, the audit, the suites, the checks, the bank and the
budgets come first, and the network cut and the soak after;
tools/body/README.md has their bounds.

**What it never does.** It changes no git state on either machine, apart from
its own bare clone at ~/emet-ci/repo.git. It never touches the maintainer's
checkout or venv on the body (~/Documents/emet). It never reads or prints a
keys file, and every command on the body starts by unsetting each EMET_
variable, so a key exported in a shell there cannot reach a run, and neither
can EMET_LIVE_TESTS or the CI-only EMET_VIRTUAL_AUDIO. An unreachable body
is reported as "body run not done", with exit code 2.

Standard library only: Python 3.12 or newer on the laptop, and on the body
bash, Python 3 with venv, git, util-linux's `script` and coreutils'
`timeout`. `ssh` must reach the body without a password
(`ssh -o BatchMode=yes emet-pi true`).

Usage:
    python tools/body/run.py --release 0.5.5 --tree
    python tools/body/run.py --release 0.5.5 --commit 1a2b3c4
    python tools/body/run.py --release 0.5.5 --tree --window 2026-10-28T14:00 2026-10-28T15:00
    python tools/body/run.py --release 0.5.5 --collect-only --fetch '~/sitting-0.5.5.log'
    python tools/body/run.py --release 0.5.7 --collect-only --fetch '~/sitting-0.5.7.log' \\
        --fetch '~/sitting-0.5.7.events.jsonl' --sheet ../releases/v0.5.7/sitting-sheet.md
    python tools/body/run.py --release 0.5.6 --commit 1a2b3c4 --soak 60 --repeat 5
    python tools/body/run.py --release 0.5.6 --soak-collect
    python tools/body/run.py --release 0.5.6 --nightly-report
    python tools/body/run.py --release 0.5.6 --budget-table
Exit:   0 every step that ran passed; 1 a step failed; 2 the body run was
        not done: no answer over SSH, refused at preflight, or the code could
        not be synced or installed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

REPO = Path(__file__).resolve().parents[2]
#: `Project Emet/`, where the maintainer keeps `releases/` beside the repo.
TOP = REPO.parent

#: `run.json`'s format. 2 adds `body` and `flags_run` to every field of 1.
FORMAT = "emet-body-run/2"

HOST = "emet-pi"
#: BatchMode fails at once instead of asking for a password nobody is there
#: to type; ConnectTimeout bounds the wait for a body that is off.
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
UPSTREAM = "https://github.com/alexander-wang03/emet"

PACKAGES = ("emet-sdk", "emet-hal", "emet-providers", "emet-engine")
#: RELEASE_PROCESS.md §3's install line with the provider extras the
#: maintainer's venv on the body holds, so the runner's providers suite skips
#: the 4 live vendor tests the maintainer's skips, where 0.5.5's runs skipped
#: 9. `piper` is
#: GPL-3.0, imported through its extra and never bundled; the budget's first
#: chunk on the local path needs it. tools/body/nightly.sh keeps the same line
#: and the same RUNNER_PIP, so the two share one install key.
INSTALL = ("emet-sdk[dev]", "emet-hal[dev,audio,wake]", "emet-providers[dev,deepgram,anthropic,openai,piper]",
           "emet-engine[dev]")
#: The pip the runner's venv is upgraded to before the install line (ledger
#: L27). The venv's own pip was 25.1.1, with six advisories of its own
#: (PYSEC-2026-1795, -1796, -2875, -2876, -196 and -3721, pip-audit 2.10.1 on
#: the body, 2026-11-26), and pip-audit names 26.2 as the fix for all six.
#: Part of the install's key, so a new pin installs again. nightly.sh holds
#: the same pin, and a test holds the two keys equal.
RUNNER_PIP = "pip==26.2"
CHECKS = ("check_layering.py", "release_check.py", "check_style.py")
#: In the order they run. The speaker and the microphone come before the
#: network cut and the soak, so a window is spent on them alone.
STEPS = ("sync", "install", "audit", "suites", "checks", "bank", "budgets", "speaker", "live", "netcut", "soak")
HARDWARE_STEPS = ("speaker", "live")
#: Steps a run takes only when asked: `--soak MINUTES` and `--netcut SECONDS`.
ASKED_STEPS = ("soak", "netcut")
#: What the two hardware steps run, in order, each a step of its own in
#: `run.json`. Only these add to `flags_run`.
HARDWARE_SUBSTEPS = ("speaker-echo", "speaker-speak", "speaker-talk", "live-plain", "live-talk", "live-stats")
#: The two sub-steps that also write an events file (`--events`, BVP12),
#: each beside its typescript as `<step>.events.jsonl`: emet-talk's replay
#: and the ten minutes of `--stats`. `--events` needs `--stats`, which both
#: have, and it is never a BEHAVIOUR flag.
EVENTS_STEPS = ("speaker-talk", "live-stats")
EVENTS_SUFFIX = ".events.jsonl"
#: tools/events_file.py, the events file's reader, beside this file's folder.
EVENTS_READER = Path(__file__).resolve().parents[1] / "events_file.py"

NIGHT_STARTS = dt.time(22, 0)
NIGHT_ENDS = dt.time(8, 0)
PLAIN_S = 60
STATS_S = 600
#: Seconds for emet-listen to boot before its audio clock starts, so a
#: `--stats` run stopped by `timeout` still reads ten minutes of audio.
BOOT_S = 20
#: A replay ends by itself; this bounds one that does not.
REPLAY_LIMIT_S = 300
#: Added to each hardware step's bound before it is held against the window.
MARGIN_S = 30

MANIFEST = "~/pi-speakerphone.yaml"
SOUL = "emet-sdk/examples/emet-soul.yaml"
SPEAKER_WAV = "~/one-breath.wav"
BANK = "~/voicebank"
LOG_CHECK = TOP / "releases" / "v0.5.4" / "body_log_check.py"

#: What the body runs with on the speaker and the microphone. The body takes
#: every model stage over, so the soul's vendors are never reached; the mock
#: transcriber says a fixed line and records nothing (`record_turns` is
#: absent), and the mock voice plays the words' bytes as clicks.
MODELS: dict[str, dict[str, Any]] = {
    "stt": {"provider": "mock", "params": {"transcript": "testing the speaker"}},
    "chat": {"provider": "mock", "params": {"reply": "This is the body runner, checking the speaker."}},
    "tts": {"provider": "mock"},
}
#: `--voice piper`: the reference voice, local and free, through the `piper`
#: extra in the runner's venv. It reads the voice the maintainer downloaded.
PIPER_TTS = {"provider": "piper", "model": "en_US-ljspeech-medium"}

AUDIO_SUFFIXES = (".wav", ".raw", ".pcm", ".flac", ".ogg", ".mp3", ".opus")

#: The canonical form of a body manifest and its hash, as Python the body
#: runs: the file is hashed where it lies and only the hash comes home. The
#: YAML is parsed, so its comments, its layout and its quoting are gone;
#: every mapping's keys become text and are sorted; a sequence keeps its
#: order, which means something; the result is written as JSON with no
#: spaces, in ASCII, and those bytes are hashed. The idea of a configuration
#: fingerprint is EasyNavigation's roadmap item RD032 (Apache-2.0,
#: CITATIONS.md); the form is this file's.
CANONICAL_PY = '''\
import hashlib, json


def canonical(value):
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(json.dumps(canonical(item), sort_keys=True) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def canonical_sha256(doc):
    text = json.dumps(canonical(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()
'''

#: The body's fingerprint, run on the body with the manifest's path. It
#: prints two hashes, the state file's path and a note for each thing it
#: could not do. Nothing else of the manifest is printed: a YAML parser's
#: message quotes the file, so a file that does not parse is named by the
#: error's kind alone.
FINGERPRINT_PY = CANONICAL_PY + '''
import sys
from pathlib import Path

notes = []
body_id = None
try:
    raw = Path(sys.argv[1]).read_bytes()
except FileNotFoundError:
    raw = None
    notes.append("the manifest is not there")
except OSError as exc:
    raw = None
    notes.append("the manifest could not be read (" + type(exc).__name__ + ")")
if raw is not None:
    try:
        import yaml
        doc = yaml.safe_load(raw.decode("utf-8"))
    except ImportError:
        notes.append("the manifest was not hashed: PyYAML is not installed for the Python that read it")
    except Exception as exc:
        notes.append("the manifest is not readable YAML (" + type(exc).__name__ + ")")
    else:
        print("manifest_hash=" + canonical_sha256(doc))
        body = doc.get("body") if isinstance(doc, dict) else None
        body_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(body_id, str):
            body_id = None
            notes.append("the manifest names no body.id, so no state file is named")
if body_id is not None:
    try:
        from emet_engine.state import state_path
        state = Path(state_path(body_id))
    except ImportError:
        notes.append("the state file was not looked for: emet_engine is not installed for the Python that read the manifest")
    except (ValueError, OSError, RuntimeError) as exc:
        notes.append("the state file's place could not be worked out (" + type(exc).__name__ + ")")
    else:
        print("state=" + str(state))
        try:
            print("state_hash=" + hashlib.sha256(state.read_bytes()).hexdigest())
        except FileNotFoundError:
            notes.append("there is no state file yet")
        except OSError as exc:
            notes.append("the state file could not be read (" + type(exc).__name__ + ")")
for note in notes:
    print("note=" + note)
'''

#: What the bank step reads from replay.py's report, on the body: how many
#: recordings were held to a golden, and each one named and held to nothing.
#: A report without `held` is one a replay.py from before 0.5.6 wrote.
BANK_REPORT_PY = '''\
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
except (OSError, ValueError):
    report = {}
held, unreleased = report.get("held"), report.get("unreleased")
print("held=" + (str(len(held)) if isinstance(held, list) else "unknown"))
for name in unreleased if isinstance(unreleased, list) else []:
    print("unreleased=" + str(name))
'''

HEX64 = re.compile(r"[0-9a-f]{64}")

#: Every command on the body starts here. The EMET_ names that were set are
#: kept for the preflight record, names only, and then unset. Temporary
#: files and pip's cache go inside ~/emet-ci, so the runner writes nowhere
#: else on the body. `PIP_CONFIG_FILE=/dev/null` sets every pip
#: configuration file aside: the Pi's `/etc/pip.conf` adds piwheels as an
#: extra index (read 2026-11-27), whose own builds of a version carry
#: hashes PyPI does not list, so the runner's installs come from PyPI alone
#: (ledger L53). pip skips its files when the variable is `os.devnull`
#: (`pip/_internal/configuration.py`, pip 24.3.1 and 25.1.1, read
#: 2026-11-27).
PRELUDE = r"""set -u
EMET_SET=$(env | sed -n 's/^\(EMET_[A-Za-z0-9_]*\)=.*/\1/p' | tr '\n' ' ')
for name in $EMET_SET; do unset "$name"; done
CI="$HOME/emet-ci"
PY="$CI/.venv/bin/python"
export PYTHONDONTWRITEBYTECODE=1 GIT_TERMINAL_PROMPT=0 TMPDIR="$CI/tmp" PIP_CACHE_DIR="$CI/cache/pip"
export PIP_CONFIG_FILE=/dev/null
mkdir -p "$TMPDIR"
"""

#: A process that means the robot is in use: emet-listen or emet-talk (the
#: console script, the `script -c` wrapping one, or the module run
#: directly), the voice bank's prompter, or arecord and aplay. A grep for
#: this pattern does not match its own command line.
BUSY = re.compile(r"(?:^|[\s/])(?:emet-listen|emet-talk|arecord|aplay)(?:\s|$)"
                  r"|\bemet_engine\.(?:cli|talk)\b|(?:^|[\s/])tools/bank/record\.py\b")

#: Every PCM substream the kernel knows, open or closed. A closed one's
#: status reads `closed`; an open one's starts `state:` (sound/core/pcm.c,
#: snd_pcm_substream_proc_status_read), whoever opened it, so a recording
#: started any other way is seen too. `{root}` is /proc/asound on the body.
PCM_CHECK = r"""for f in {root}/card*/pcm*/sub*/status; do
  [ -r "$f" ] || continue
  first=$(head -n 1 "$f")
  [ "$first" = "closed" ] || echo "pcm=$f: $first, $(grep -m 1 owner_pid "$f" | tr -s ' ')"
done
"""
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
#: What `--fetch` may copy home: the typescripts the maintainer's body runs
#: and the release sitting write in the home folder, and the events files
#: beside them (`--events`, which hold numbers and verdicts and never a
#: word), and nothing else. The name has no directory in it, so nothing
#: under ~/.config or ~/.ssh can match, and it ends in .log or
#: .events.jsonl, so no recording can; a name holding key, env, secret or
#: token is refused besides.
FETCHABLE = re.compile(r"^~/(?:pi|sitting)-[A-Za-z0-9._-]+\.(?:log|events\.jsonl)$")
SECRET = re.compile(r"key|env|secret|token", re.IGNORECASE)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")

#: vcgencmd get_throttled's bits (Raspberry Pi documentation, "get_throttled",
#: verified 2026-10-27), named as emet_engine/board.py names them for the
#: `--stats` footer, so the report and the footer use the same words for one
#: bit. A copy, since this script imports nothing outside the standard
#: library; a test holds the two equal.
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "frequency capped has occurred",
    18: "throttled has occurred",
    19: "soft temperature limit has occurred",
}

# --------------------------------------------------------------------------
# The audit, the budgets, the soak and the network cut: what each runs
# --------------------------------------------------------------------------

#: The vulnerability audit of the runner's venv (BVP2 stage 2). pip-audit
#: 2.10.1, Apache-2.0 (PyPI's JSON for pip-audit, its classifier and its
#: upload of 2026-06-10, verified 2026-11-26). It lives in a venv of its own
#: beside the runner's and reads the runner's site-packages with `--path`:
#: installed into the runner's venv, its dependencies (ten direct ones in
#: 2.10.1's metadata, and theirs) would join the venv the suites run in and
#: be audited as the robot's.
#:
#: It is installed from the CI lock, tools/ci/requirements.txt, which `uv pip
#: compile --generate-hashes` writes and Dependabot keeps (ledger L21): the
#: laptop reads pip-audit's pin there and every pin pip-audit needs, each with
#: its hashes, and the body installs exactly those with `--require-hashes`,
#: so a file PyPI serves that the lock never hashed is refused. The lock is
#: the one beside this file, the tree the runner itself is in.
AUDIT_LOCK = Path(__file__).resolve().parents[1] / "ci" / "requirements.txt"
AUDIT_TOOL = "pip-audit"
AUDIT_VENV = "audit-venv"
#: What a pip-audit that could not reach its service says, in its last lines.
NO_NETWORK = re.compile(r"ConnectionError|NewConnectionError|Name or service not known|name resolution|"
                        r"Network is unreachable|Max retries exceeded|timed out|No route to host", re.IGNORECASE)

#: Reads pip-audit's JSON on the body and prints one line a finding, the same
#: finding listed twice counted once.
AUDIT_REPORT_PY = '''\
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
except (OSError, ValueError):
    report = None
deps = report.get("dependencies") if isinstance(report, dict) else None
if not isinstance(deps, list):
    print("parsed=no")
    raise SystemExit(0)
print("parsed=yes")
print("packages=" + str(sum(1 for dep in deps if isinstance(dep, dict) and "vulns" in dep)))
seen = set()
for dep in deps:
    if not isinstance(dep, dict):
        continue
    if "skip_reason" in dep:
        print("skipped=" + str(dep.get("name")) + "|" + str(dep.get("skip_reason")))
    for vuln in dep.get("vulns") or []:
        key = (str(dep.get("name")), str(dep.get("version")), str(vuln.get("id")))
        if key not in seen:
            seen.add(key)
            print("finding=" + "|".join(key + (",".join(str(v) for v in vuln.get("fix_versions") or []),)))
'''

#: The runner's manifests for the budget and the soak: the maintainer's
#: manifest with every model stage given to the runner's choice and the
#: sink set to "null", one file for each (target, models) pair. It prints
#: what each file says of its sink and models, so the laptop holds every
#: file to them before anything runs.
NULL_MANIFEST_PY = CANONICAL_PY + '''
import copy, sys
from pathlib import Path
try:
    import yaml
    from emet_engine.state import state_path
    doc = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("manifest_hash=" + canonical_sha256(doc))
    for target, models in zip(sys.argv[2::2], sys.argv[3::2]):
        mine = copy.deepcopy(doc)
        mine["models"] = json.loads(models)
        audio = mine.setdefault("audio", {})
        output = audio.get("output")
        audio["output"] = dict(output) if isinstance(output, dict) else {}
        audio["output"]["sink"] = "null"
        Path(target).write_text(yaml.safe_dump(mine, sort_keys=False), encoding="utf-8")
        written = yaml.safe_load(Path(target).read_text(encoding="utf-8"))
        print("written=" + json.dumps({"path": target, "sink": (written.get("audio") or {}).get("output", {}).get("sink"),
                                       "source": ((written.get("audio") or {}).get("input") or {}).get("source"),
                                       "models": written.get("models")}, sort_keys=True))
    print("body=" + doc["body"]["id"])
    print("state=" + str(state_path(doc["body"]["id"])))
except Exception as exc:
    print("problem=" + type(exc).__name__)
    sys.exit(3)
'''

#: The budget's runs of emet-listen: `cli.main` in this process, with two
#: methods of `SessionStats` wrapped to read what the engine measures already
#: and prints only in part (its footer has no 99th percentile and no time of
#: the first frame). `record_frame` notes the first frame's time, resident
#: memory and CPU clock; `report`, which the footer calls, notes the rest,
#: the turn gap of each turn among them (`TurnRecord.gap_ms`, last loud frame
#: to first sound, None for a turn that made no sound). Both call the
#: engine's own method. A wrapped method the engine stopped calling, or an
#: engine with no `turn_records`, leaves its numbers out of the result, and
#: the measure is null: the 0.5.3 row of RELEASE_PROCESS.md §10, a harness
#: that names what it wraps.
BUDGET_PY = '''\
import json, sys, time
from pathlib import Path

from emet_engine import cli, metrics

start_ns, result, argv = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
seen = {"start_ns": start_ns, "wrapped": ["SessionStats.record_frame", "SessionStats.report"]}


def rss_kb():
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


record_frame, report = metrics.SessionStats.record_frame, metrics.SessionStats.report


def first_frame(self, process_ms):
    if "first_ns" not in seen:
        seen.update(first_ns=time.time_ns(), rss_boot_kb=rss_kb(), cpu_start=time.process_time(),
                    wall_start=time.monotonic())
    return record_frame(self, process_ms)


def footer(self, *args, **kwargs):
    seen.update(frames=self.frames, mean_ms=self.mean_ms, p99_ms=self.percentile(99), max_ms=self.process_ms_max,
                over=self.over_budget, budget_ms=self.frame_ms, voice_first_ms=list(self.speech_first_ms),
                cpu_end=time.process_time(), wall_end=time.monotonic(), rss_end_kb=rss_kb())
    records = getattr(self, "turn_records", None)
    if records is not None:
        seen["gap_ms"] = [r.gap_ms for r in records if r.gap_ms is not None]
    return report(self, *args, **kwargs)


metrics.SessionStats.record_frame = first_frame
metrics.SessionStats.report = footer
code = 1
try:
    code = cli.main(argv)
finally:
    seen["exit"] = code
    result.write_text(json.dumps(seen), encoding="utf-8")
raise SystemExit(code)
'''

#: Feeds the soak and the budget's quiet minute through a named pipe, as a
#: wav of 16 kHz mono 16-bit whose header names the largest size a wav can
#: hold, so one emet-listen reads an hour without a file of that length on
#: disk. Python's wave reads a pipe once its data chunk is found: it seeks
#: only before that (CPython's Lib/wave.py, `_data_seek_needed`). Modes:
#: `realtime` loops the recordings at the speed a microphone gives them for
#: SECONDS; `once` sends each recording once as fast as it is read; `quiet`
#: sends SECONDS of low noise at real time, a room with nobody in it.
FEEDER_PY = '''\
import random, struct, sys, time, wave
from pathlib import Path

RATE, WIDTH, FRAME = 16000, 2, 1280
pipe, mode, seconds, folder = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
DATA = 0xFFFFFFFE - 36
header = (b"RIFF" + struct.pack("<I", 36 + DATA) + b"WAVE" + b"fmt "
          + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * WIDTH, WIDTH, 16) + b"data" + struct.pack("<I", DATA))
out = open(pipe, "wb", buffering=0)
start = time.monotonic()
# Counted in samples, a whole number, so SECONDS of audio is exact.
sent, limit = 0, int(round(seconds * RATE))


def send(chunk, paced):
    global sent
    if paced:
        delay = start + sent / RATE - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    out.write(chunk)
    sent += len(chunk) // WIDTH


def usable():
    found = []
    for path in sorted(Path(folder).glob("*.wav")) if mode != "quiet" else []:
        try:
            with wave.open(str(path), "rb") as wav:
                shape = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
        except (OSError, EOFError, wave.Error) as exc:
            print("skipped=" + path.name + ": " + type(exc).__name__)
            continue
        if shape != (RATE, 1, WIDTH):
            print("skipped=" + path.name + ": %d Hz, %d channel(s), %d-bit" % (shape[0], shape[1], shape[2] * 8))
            continue
        found.append(path)
    return found


def frames(path):
    with wave.open(str(path), "rb") as wav:
        while True:
            chunk = wav.readframes(FRAME)
            if len(chunk) < FRAME * WIDTH:
                return
            yield chunk


try:
    out.write(header)
    files = usable()
    print("files=" + str(len(files)))
    if mode == "quiet":
        noise = random.Random(0)
        block = b"".join(struct.pack("<h", noise.randint(-32, 32)) for _ in range(FRAME * 100))
        while sent < limit:
            for at in range(0, len(block), FRAME * WIDTH):
                if sent >= limit:
                    break
                send(block[at:at + FRAME * WIDTH], True)
    elif mode == "once":
        for path in files:
            for chunk in frames(path):
                send(chunk, False)
    else:
        while files and sent < limit:
            for path in files:
                for chunk in frames(path):
                    if sent >= limit:
                        break
                    send(chunk, True)
except BrokenPipeError:
    print("reader_gone=yes")
    sys.exit(3)
print("fed_s=%.1f" % (sent / RATE))
out.close()
'''

#: The soak (RP6 stage 1). Lengths by kind, RELEASE_PROCESS.md §1.1; each a
#: value to tune.
SOAK_MINUTES = {"patch": 60, "minor": 480, "major": 4320}
#: One listener reads one wav stream, and a wav's data holds at most
#: 0xFFFFFFFE bytes: 37.3 hours of 16 kHz mono. So a soak here is at most 36
#: hours; the 72 hours before a major need the listener started again for a
#: second stream, which the soak does not do yet. That is first needed before
#: the 1.0 freeze, in 0.10 (ROADMAP.md, RP6 stage 3).
SOAK_MIN, SOAK_MAX = 10, 2160
#: The recordings the soak loops: every 16 kHz mono wav in the bank's audio.
SOAK_WAVS = "~/voicebank/audio"
#: One sample a minute, on the body, by the soak's own script.
SAMPLE_S = 60
#: Samples before this, counted from the first sample that was listening,
#: are boot and are left out of the judgement.
WARMUP_S = 300
#: Stage 4's exit, each a named value to tune: resident memory growing under
#: 1 MB an hour, open files and threads constant, no frame over 80 ms, no
#: throttle flag. A frame is held to FRAME_BUDGET_MS by the footer's longest
#: frame and, when the engine's own budget line names the same 80 ms, by its
#: count of frames over it.
RSS_GROWTH_MB_PER_H = 1.0
#: The growth is held over the second half of the samples after the warm-up,
#: so a leak that runs the whole soak fails and one step that then holds
#: still does not. 0.5.6's rehearsal soak on the body (2026-11-26,
#: releases/v0.5.6/body/2026-11-26/soak/samples.txt) rose 3.9 MB once, at
#: minute 24, and stayed at 148,528 kB for the 36 minutes after: 6.21 MB an
#: hour over its whole length, none over its second half. The whole length's
#: slope is still printed beside it.
RSS_JUDGED_FROM = 0.5
FRAME_BUDGET_MS = 80.0
MIN_SOAK_SAMPLES = 3
#: How often the laptop asks whether the soak has ended, and how long it
#: waits for a body that stopped answering before it leaves the soak to
#: `--soak-collect`.
POLL_S = 60
LOST_S = 900
#: The -X dev pass replays each recording once as fast as it is read; this
#: bounds it, and the laptop waits this long after the timed minutes.
DEV_PASS_S = 1800
#: How long past its length the timed listener may run before the soak's
#: script stops it as Ctrl-C would; two samples later it is killed. The -X
#: dev pass is held to DEV_PASS_S the same way, so the script ends at most
#: SOAK_OVERRUN_S, DEV_PASS_S and six samples, 39 minutes, past the soak's
#: length: inside the 40 the laptop waits (a test holds the sum).
SOAK_OVERRUN_S = 180
#: What the -X dev pass's stderr counts as a warning: the warnings module's
#: lines, asyncio's debug mode on a slow callback or a coroutine never
#: awaited, and an exception Python could only print.
DEV_WARNING = re.compile(r"\b[A-Za-z]*Warning: |^Executing <.*> took [0-9.]+ seconds|was never awaited|"
                         r"^Exception ignored")

#: The soak's script, written on the body and started detached under
#: `nohup setsid`, so a dropped SSH session or a sleeping laptop does not end
#: it (RELEASE_PROCESS.md §10, the 0.4 row). It samples each of its two
#: listeners each minute, stops one that outlives its bound, frees the
#: runner's lock when it ends if the lock is still its run's, and writes
#: `done`. The sound cards are read before the first listener and after the
#: second, and so are the audio files written meanwhile. The lines above it
#: set SOAK, CI, PY, LISTEN_BIN, SOUL, WAVS, SOAK_S (its length in seconds),
#: OVERRUN_S, DEV_S (SOAK_OVERRUN_S and DEV_PASS_S), SAMPLE_S, STAMP and
#: TOKEN.
SOAK_SH = r"""set -u
cd "$SOAK" || exit 3
echo "$$" > pid
PIPE="$CI/tmp/soak-$STAMP.pipe"
ASOUND=/proc/asound
LISTEN=""
FEED=""
pcm_open() {
""" + PCM_CHECK.format(root='"$ASOUND"') + r"""}
finish() {
  code=$?
  [ -n "$LISTEN" ] && kill -INT "$LISTEN" 2>/dev/null
  [ -n "$FEED" ] && kill "$FEED" 2>/dev/null
  rm -f "$PIPE"
  echo "soak_exit=$code" >> status
  if grep -qx "token $TOKEN" "$CI/lock" 2>/dev/null; then rm -f "$CI/lock"; echo "lock=released" >> status; else echo "lock=not ours" >> status; fi
  date -Iseconds > done
}
trap finish EXIT
trap 'exit 130' INT TERM
sample() {
  # $1: the listener's output, whose `listening for` line says it booted.
  local st="/proc/$LISTEN/status" rss thr fds snd cpu temp flags pcm mine heard
  rss=$(sed -n 's/^VmRSS:[[:space:]]*\([0-9]*\).*/\1/p' "$st" 2>/dev/null)
  thr=$(sed -n 's/^Threads:[[:space:]]*\([0-9]*\).*/\1/p' "$st" 2>/dev/null)
  fds=$(ls -1 "/proc/$LISTEN/fd" 2>/dev/null | wc -l)
  snd=$(find "/proc/$LISTEN/fd" -maxdepth 1 -lname '/dev/snd/*' 2>/dev/null | wc -l)
  cpu=$(sed 's/^.*) //' "/proc/$LISTEN/stat" 2>/dev/null | awk '{print $12 + $13}')
  if command -v vcgencmd >/dev/null 2>&1; then
    temp=$(vcgencmd measure_temp 2>&1 | sed "s/^temp=//; s/'C\$//")
    flags=$(vcgencmd get_throttled 2>&1 | sed 's/^throttled=//')
  else
    temp=none
    flags=none
  fi
  pcm=$(pcm_open | wc -l)
  mine=$(pcm_open | grep -c "owner_pid : $LISTEN\$")
  heard=$(grep -c '^listening for ' "$1" 2>/dev/null)
  echo "t=$(date +%s) rss_kb=${rss:-none} fds=$fds threads=${thr:-none} snd=$snd cpu_ticks=${cpu:-none} temp=$temp throttled=$flags pcm=$pcm pcm_mine=$mine listening=${heard:-0}"
}
follow() {
  # One pass: LISTEN and FEED started by the caller. $1 is the listener's
  # output, $2 the file its samples go to, $3 the seconds it may run and $4
  # the pass's name in `status`. A feeder gone for two samples while the
  # listener still runs means the listener is waiting on a pipe nobody
  # writes, and one that outlives its seconds is hung: either is stopped as
  # Ctrl-C would, and a listener past its seconds that is still there two
  # samples later is killed.
  local out="$1" into="$2" ends late=0 gone=0
  ends=$(( $(date +%s) + $3 ))
  while kill -0 "$LISTEN" 2>/dev/null; do
    sleep "$SAMPLE_S" & wait $!
    if kill -0 "$LISTEN" 2>/dev/null; then sample "$out" >> "$into"; fi
    if ! kill -0 "$FEED" 2>/dev/null; then
      gone=$((gone + 1))
      if [ "$gone" -ge 2 ]; then kill -INT "$LISTEN" 2>/dev/null; fi
    fi
    if kill -0 "$LISTEN" 2>/dev/null && [ "$(date +%s)" -ge "$ends" ]; then
      late=$((late + 1))
      if [ "$late" -eq 1 ]; then
        echo "${4}_stopped=it was still running $3 s after it started" >> status
        kill -INT "$LISTEN" 2>/dev/null
      elif [ "$late" -ge 3 ]; then
        echo "${4}_killed=it outlived Ctrl-C by two samples" >> status
        kill -KILL "$LISTEN" 2>/dev/null
      fi
    fi
  done
}
date -Iseconds > started
pcm_open > pcm-before.txt
if [ -s pcm-before.txt ]; then echo "refused=a sound card was open when the soak was to start" >> status; exit 3; fi
echo "clk_tck=$(getconf CLK_TCK)" >> status
mkfifo "$PIPE" || { echo "refused=no pipe could be made in $CI/tmp" >> status; exit 3; }
"$PY" feed.py "$PIPE" realtime "$SOAK_S" "$WAVS" > feed.txt 2>&1 &
FEED=$!
PYTHONUNBUFFERED=1 "$LISTEN_BIN" soak.yaml "$SOUL" --replay "$PIPE" --transcribe --stats --keys empty-keys.env --state state > listen.out 2> listen.err &
LISTEN=$!
echo "$LISTEN" > listen.pid
follow listen.out samples.txt $((SOAK_S + OVERRUN_S)) listen
wait "$LISTEN"
echo "listen_exit=$?" >> status
LISTEN=""
if kill -0 "$FEED" 2>/dev/null; then sleep 2; kill "$FEED" 2>/dev/null; fi
wait "$FEED"
echo "feed_exit=$?" >> status
FEED=""
rm -f "$PIPE"
# The -X dev pass: each recording once, as fast as it is read, sampled as
# the timed pass is for the sound devices it opens, and held to DEV_S
# seconds.
mkfifo "$PIPE" || { echo "dev_refused=no pipe could be made in $CI/tmp" >> status; exit 3; }
"$PY" feed.py "$PIPE" once 0 "$WAVS" > dev-feed.txt 2>&1 &
FEED=$!
PYTHONUNBUFFERED=1 "$PY" -X dev "$LISTEN_BIN" soak.yaml "$SOUL" --replay "$PIPE" --transcribe --stats --keys empty-keys.env --state dev-state > dev.out 2> dev.err &
LISTEN=$!
echo "$LISTEN" > dev.pid
follow dev.out dev-samples.txt "$DEV_S" dev
wait "$LISTEN"
echo "dev_exit=$?" >> status
LISTEN=""
if kill -0 "$FEED" 2>/dev/null; then sleep 2; kill "$FEED" 2>/dev/null; fi
wait "$FEED"
FEED=""
rm -f "$PIPE"
# After both passes: a card open now, or an audio file written since the
# soak started, is a failure of the soak.
pcm_open > pcm-after.txt
find "$CI" -newer started -type f \( AUDIO_FIND \) -print 2>/dev/null > audio.txt
find "$HOME" -maxdepth 1 -newer started -type f \( AUDIO_FIND \) -print 2>/dev/null >> audio.txt
exit 0
""".replace("AUDIO_FIND", " -o ".join(f"-iname '*{s}'" for s in AUDIO_SUFFIXES))

#: The budget table's measures, in the order and with the names of
#: planning/templates/plan.md §3, and the unit each is recorded in.
MEASURES = (
    ("Boot to first frame", "ms"),
    ("First chunk at the null sink after the final, local path", "ms"),
    # EV3 stage 1: the wait a person feels, last loud frame to first sound,
    # at the median of the one breath's turns. DESIGN.md §13 gives it a
    # budget of 2.0 s from Shiwa et al. (HRI 2008, CITATIONS.md); the runner
    # holds it to the last release's value, as every row here.
    ("Turn gap, median, local path", "ms"),
    ("Per-frame mean", "ms"),
    ("Per-frame 99th percentile", "ms"),
    ("Frames over 80 ms", "frames"),
    ("Resident memory after boot", "MB"),
    ("Resident memory at the end", "MB"),
    ("Idle CPU", "% of one core"),
    ("Board lines (temperature, throttle, load)", ""),
)
BOARD = MEASURES[-1][0]
TURN_GAP = MEASURES[2][0]
#: Stage 4: a measure is within when it rose by no more than the larger of
#: 25% and the spread repeated runs of it show. A value to tune. Holding each
#: release's numbers to the last one's on the same machine is the idea of
#: airspeed velocity's benchmark history (asv, BSD-3-Clause), and a stated
#: bound is REP 2004's performance regression policy (public domain or
#: CC0-1.0, whichever is more permissive, by its own text, verified
#: 2026-11-26): ideas both, no code (research/sota-2026-11-25/release-process.md
#: RP6 and ci-cd.md CI8; CITATIONS.md).
BOUND_PCT = 25.0
#: The budget's recordings on the body: the bank's ten minutes for the
#: per-frame numbers and the boot, one breath for the first chunk, and a
#: quiet minute fed at real time for the idle CPU.
BUDGET_WAV = "~/voicebank/audio/soak-10min.wav"
SPEAK_WAV = "~/voicebank/audio/one-breath-0.5.2.wav"
IDLE_S = 60
REPEAT_MAX = 10

#: The network cut (P12, D3): the root-owned copy of tools/body/netcut.sh
#: and the bounds the runner asks within. The probe is a TCP connection to
#: the host the runner already fetches from, so no vendor is reached.
NETCUT_BIN = "/usr/local/sbin/emet-netcut"
NETCUT_MIN_S, NETCUT_MAX_S = 30, 600
PROBE = "timeout 8 bash -c 'exec 3<>/dev/tcp/github.com/443' >/dev/null 2>&1 && echo open || echo shut"

#: The nightly (P11): its reports on the body, and how old the newest may be
#: before `--nightly-report` says the nightly has stopped.
NIGHTLY_DIR = "~/emet-ci/nightly"
NIGHTLY_STALE_H = 48


class RunnerError(Exception):
    """A step cannot go on, with the reason a person reads."""


class Unreachable(Exception):
    """ssh got no answer from the body, which is off, asleep or off the network."""


# --------------------------------------------------------------------------
# Talking to the body
# --------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str
    err: str = ""
    seconds: float = 0.0
    raw: bytes = b""

    def values(self) -> dict[str, list[str]]:
        """The `key=value` lines a script printed, every value of each key."""
        found: dict[str, list[str]] = {}
        for line in self.out.splitlines():
            key, sep, value = line.partition("=")
            if sep and re.fullmatch(r"[a-z_]+", key):
                found.setdefault(key, []).append(value)
        return found

    def value(self, key: str, default: str = "") -> str:
        return (self.values().get(key) or [default])[-1]


class Remote:
    """The body, over ssh. Each call is one ssh session."""

    def __init__(self, host: str) -> None:
        self.host = host

    def _ssh(self, command: str, stdin: bytes, timeout: float) -> Result:
        args = ["ssh", *SSH_OPTIONS, self.host, command]
        start = time.monotonic()
        try:
            done = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise Unreachable("ssh is not on this machine's PATH") from exc
        except subprocess.TimeoutExpired:
            return Result(124, "", f"no answer within {timeout:.0f} s", timeout)
        err = done.stderr.decode("utf-8", "replace")
        if done.returncode == 255:
            # ssh's own failure; no script here exits 255.
            raise Unreachable(err.strip().splitlines()[-1] if err.strip() else "ssh exited 255")
        return Result(done.returncode, done.stdout.decode("utf-8", "replace"), err,
                      time.monotonic() - start, done.stdout)

    def run(self, script: str, *, timeout: float = 600.0) -> Result:
        """Run a bash script on the body, sent on stdin after the prelude."""
        return self._ssh("bash -s", (PRELUDE + script).encode("utf-8"), timeout)

    def send(self, command: str, data: bytes, *, timeout: float = 600.0) -> Result:
        """Run a one-line command on the body with `data` on its stdin."""
        if "\n" in command:
            raise ValueError("send() takes a one-line command")
        return self._ssh(command, data, timeout)


def marked(step: str, script: str) -> str:
    """A script with its step named in its first line, for the record and for
    the tests' stand-in body."""
    return f"# body-run: {step}\n{script}"


def remote_path(path: str, home: str, tree: str) -> str:
    """A path on the body as an absolute path: `~/` is the home directory and
    a relative path is inside the synced tree."""
    if path.startswith("~/"):
        return f"{home}/{path[2:]}"
    if path.startswith("/"):
        return path
    return f"{tree}/{path}"


# --------------------------------------------------------------------------
# The laptop's tree
# --------------------------------------------------------------------------


class Laptop:
    """Read-only questions to the laptop's git. Nothing here writes to the
    index or the refs."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def git(self, *args: str) -> bytes:
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True)
        if done.returncode != 0:
            raise RunnerError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
        return done.stdout

    def config(self, key: str) -> str:
        done = subprocess.run(["git", "-C", str(self.repo), "config", "--get", key], capture_output=True)
        return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""


def as_committed(data: bytes) -> bytes:
    """The bytes git would commit from a file checked out under
    `core.autocrlf=true`: CRLF becomes LF in a text file. Git treats a file
    holding a NUL or a lone CR as binary and leaves it alone (convert.c,
    convert_is_binary), and so does this; git's printable-ratio test is not
    copied, so the comparison with HEAD's blobs is what shows the two agree."""
    if b"\r\n" not in data or b"\0" in data:
        return data
    if data.count(b"\r") != data.count(b"\r\n"):
        return data
    return data.replace(b"\r\n", b"\n")


def blob_id(data: bytes) -> str:
    """Git's object id for a file with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_digest(entries: Iterable[tuple[str, bytes]]) -> str:
    """The sha256 of `sha256sum`'s lines for every file, sorted by path, which
    the body recomputes with find, sort and sha256sum after unpacking."""
    lines = [f"{hashlib.sha256(data).hexdigest()}  {path}\n"
             for path, data in sorted(entries, key=lambda entry: entry[0].encode("utf-8"))]
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


#: The body's half of `tree_digest`, run in the unpacked tree. sha256sum
#: marks a path with "  " on Linux and " *" under Git for Windows, where the
#: tests run it, so the sed takes either.
REMOTE_DIGEST = (r"find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum "
                 r"| sed 's|^\([0-9a-f]*\) [ *]\./|\1  |' | sha256sum | cut -c1-64")


@dataclass
class Packed:
    data: bytes
    files: int
    size: int
    digest: str
    converted: int
    blobs: dict[str, str]
    skipped: list[str]


def pack(repo: Path, names: Iterable[str], *, autocrlf: bool, mtime: float) -> Packed:
    """A gzipped tar of the named files as git would commit them."""
    buf = io.BytesIO()
    entries: list[tuple[str, bytes]] = []
    blobs: dict[str, str] = {}
    skipped: list[str] = []
    converted = size = 0
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(set(n for n in names if n)):
            path = repo / name
            if path.is_symlink() or not path.is_file():
                skipped.append(name)  # deleted since it was staged, or not a file
                continue
            data = path.read_bytes()
            if autocrlf:
                fixed = as_committed(data)
                converted += fixed is not data
                data = fixed
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, int(mtime)
            tar.addfile(info, io.BytesIO(data))
            entries.append((name, data))
            blobs[name] = blob_id(data)
            size += len(data)
    return Packed(buf.getvalue(), len(entries), size, tree_digest(entries), converted, blobs, skipped)


def head_blobs(laptop: Laptop) -> dict[str, str]:
    """Path to blob id for every file in HEAD."""
    found: dict[str, str] = {}
    for entry in laptop.git("ls-tree", "-r", "-z", "HEAD").decode("utf-8").split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            found[path] = parts[2]
    return found


# --------------------------------------------------------------------------
# Windows, night and the models
# --------------------------------------------------------------------------


def parse_moment(text: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DDTHH:MM, the laptop's local time") from exc


def at_night(moment: dt.datetime) -> bool:
    clock = moment.time()
    return clock >= NIGHT_STARTS or clock < NIGHT_ENDS


def window_refusal(now: dt.datetime, seconds: float, window: tuple[dt.datetime, dt.datetime] | None,
                   *, allow_night: bool) -> str | None:
    """Why a hardware step of `seconds` may not start now, or None.

    A step that starts at 21:55 and ends at 22:06 is refused as one starting
    at 23:00 is. A step lasts minutes and the night ten hours, so a step with
    any minute between 22:00 and 08:00 has its start or its end there.
    """
    if window is None:
        return "no --window named: the microphone and the speaker are used only inside a window the maintainer allowed"
    start, end = window
    finish = now + dt.timedelta(seconds=seconds)
    if now < start:
        return f"the window opens at {start:%Y-%m-%d %H:%M}; it is {now:%H:%M}"
    if finish > end:
        return (f"the step would end at {finish:%H:%M:%S}, after the window closes at "
                f"{end:%Y-%m-%d %H:%M}")
    if not allow_night:
        for moment in (now, finish):
            if at_night(moment):
                return f"{moment:%H:%M} is between 22:00 and 08:00, and --allow-night was not given"
    return None


def check_models(models: Any) -> None:
    """Refuse a manifest that could send audio off the body or keep it.

    The transcriber and the language model must be the mocks; the voice the
    mock or local Piper. `record_turns` would write each utterance's bytes to
    disk, so it is refused wherever it appears."""
    if not isinstance(models, dict):
        raise RunnerError("the runner's manifest has no models block")
    for stage, allowed in (("stt", {"mock"}), ("chat", {"mock"}), ("tts", {"mock", "piper"})):
        block = models.get(stage)
        provider = block.get("provider") if isinstance(block, dict) else None
        if provider not in allowed:
            raise RunnerError(f"models.{stage} is {provider!r}; the body runner allows only {sorted(allowed)}")
    if "record_turns" in json.dumps(models):
        raise RunnerError("the runner's manifest sets record_turns, which keeps audio on disk")


# --------------------------------------------------------------------------
# Reading what came back
# --------------------------------------------------------------------------


@dataclass
class Counts:
    collected: int
    passed: int
    skipped: int
    failed: int
    seconds: float


def junit_counts(xml_text: str) -> Counts | None:
    """Collected, passed, skipped and failed from pytest's --junitxml."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        return None
    tests = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    seconds = sum(float(s.get("time", 0.0)) for s in suites)
    return Counts(tests, tests - skipped - failed, skipped, failed, seconds)


def throttle_meaning(text: str) -> list[str]:
    match = re.search(r"0x([0-9a-fA-F]+)", text)
    if match is None:
        return ["unread"]
    bits = int(match.group(1), 16)
    return [THROTTLE_BITS.get(bit, f"bit {bit}") for bit in range(bits.bit_length()) if bits >> bit & 1] or ["none"]


def busy_lines(ps_text: str) -> list[str]:
    """The processes that mean the robot is in use."""
    return [line.strip() for line in ps_text.splitlines() if BUSY.search(line)]


def busy_reasons(values: dict[str, list[str]]) -> list[str]:
    """Why the robot is in use, from the preflight's `ps=` and `pcm=` lines."""
    return busy_lines("\n".join(values.get("ps", []))) + [
        f"a sound card is open: {pcm}" for pcm in values.get("pcm", [])]


ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SCRIPT_START = re.compile(r"^Script started on (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
HEADER_COMMAND = re.compile(r'COMMAND="([^"]*)"')
PROMPT_COMMAND = re.compile(r"\$ (\S*\bemet-(?:listen|talk)\b.*)$")
LISTENING = re.compile(r"""^(?:(?P<name>.+?) is )?listening for (?:'[^']*'|"[^"]*")\s*$""")
ENDED = re.compile(r"^(?:stopped|source ended)\.")
AUDIO_SECONDS = re.compile(r"^  frames +\d+ +\((\d+(?:\.\d+)?)s of audio\)")
PROVIDER = re.compile(r"^  (stt|llm|voice|words|answers) +([^\s,]+)")
SOURCE = re.compile(r"^  (?:source +(\S+)|wake +\S+ on ([^\s,]+))")
PROGRAM = re.compile(r"(?:^|[\s/])(emet-listen|emet-talk)(?=\s|$)")


@dataclass
class LoggedRun:
    """One run of emet-listen or emet-talk in a typescript."""
    where: str
    line: int
    date: str | None
    clock: str | None
    program: str
    flags: frozenset[str]
    replay: bool
    ended: bool = False
    audio_s: float | None = None
    providers: dict[str, str] = field(default_factory=dict)
    in_header: bool = True  # until the blank line after the boot header


def command_flags(command: str | None) -> frozenset[str]:
    if not command:
        return frozenset()
    found = PROGRAM.search(command)
    rest = command[found.end():] if found else command
    try:
        words = shlex.split(rest)
    except ValueError:
        words = rest.split()
    return frozenset(w.split("=", 1)[0] for w in words if w.startswith("--"))


def logged_runs(text: str, where: str) -> list[LoggedRun]:
    """Every run in a typescript: its date from script's header, its command
    from the header or from the prompt line above it, and whether it reached
    its end line."""
    # A pty ends each line with CR LF, and a caption's redraw is a lone CR:
    # the line's own CR is dropped and each redraw becomes a line of its own.
    pairs = [(n, piece) for n, line in enumerate(text.split("\n"), 1)
             for piece in ESCAPE.sub("", line).rstrip("\r").split("\r")]
    numbers = [n for n, _ in pairs]
    lines = [piece for _, piece in pairs]
    runs: list[LoggedRun] = []
    date = clock = header = prompt = None
    current: LoggedRun | None = None
    for index, line in enumerate(lines):
        started = SCRIPT_START.match(line)
        if started:
            date, clock = started.group(1), started.group(2)
            found = HEADER_COMMAND.search(line)
            header, prompt, current = (found.group(1) if found else None), None, None
            continue
        typed = PROMPT_COMMAND.search(line)
        if typed and not LISTENING.match(line):
            prompt, current = typed.group(1), None
            continue
        heard = LISTENING.match(line)
        if heard:
            command = header or prompt
            program = "emet-talk" if heard.group("name") else "emet-listen"
            flags = command_flags(command)
            current = LoggedRun(where, numbers[index], date, clock, program, flags, "--replay" in flags)
            runs.append(current)
            header = prompt = None
            continue
        if current is None:
            continue
        if current.in_header:
            # The boot header names the source and the providers; the footer
            # has lines of the same shape ("  stt final ..."), so only the
            # header is read for them.
            if not line.strip():
                current.in_header = False
            elif (named := PROVIDER.match(line)) is not None:
                current.providers[named.group(1)] = named.group(2)
            elif (source := SOURCE.match(line)) is not None:
                current.replay = (source.group(1) or source.group(2)) == "wav"
            continue
        if ENDED.match(line):
            current.ended = True
        elif (seconds := AUDIO_SECONDS.match(line)) is not None:
            current.audio_s = float(seconds.group(1))
    return runs


#: Flags that make a run something other than "a plain run" or "emet-talk
#: with no flags". --keys and --state say where things are, and every runner
#: run names both.
BEHAVIOUR = frozenset({"--replay", "--echo", "--speak", "--reply", "--transcribe", "--stats",
                       "--explain", "--chains"})


@dataclass(frozen=True)
class FlagRow:
    label: str
    program: str
    wants: Callable[[frozenset[str]], bool]
    live_only: bool = False
    replay_only: bool = False
    min_audio_s: float | None = None


#: RELEASE_PROCESS.md §4: "a plain run, `--replay`, `--stats` live to the end
#: of ten minutes, `--echo`, `--transcribe`, `--reply`, `--speak`; and
#: `emet-talk` with no flags". --speak implies --reply, which implies
#: --transcribe (emet-listen's help).
FLAG_ROWS = (
    FlagRow("a plain run", "emet-listen", lambda f: not f & BEHAVIOUR, live_only=True),
    FlagRow("--replay", "emet-listen", lambda f: "--replay" in f, replay_only=True),
    FlagRow("--stats, live, ten minutes", "emet-listen", lambda f: "--stats" in f,
            live_only=True, min_audio_s=float(STATS_S)),
    FlagRow("--echo", "emet-listen", lambda f: "--echo" in f),
    FlagRow("--transcribe", "emet-listen", lambda f: bool(f & {"--transcribe", "--reply", "--speak"})),
    FlagRow("--reply", "emet-listen", lambda f: bool(f & {"--reply", "--speak"})),
    FlagRow("--speak", "emet-listen", lambda f: "--speak" in f),
    FlagRow("emet-talk with no flags", "emet-talk", lambda f: not f & BEHAVIOUR, live_only=True),
)


def latest(runs: Iterable[LoggedRun]) -> LoggedRun | None:
    best: LoggedRun | None = None
    for run in runs:
        if best is None or (run.date, run.clock) > (best.date, best.clock):
            best = run
    return best


def flag_hits(row: FlagRow, runs: Iterable[LoggedRun]) -> tuple[list[LoggedRun], list[LoggedRun]]:
    """The runs that count for one flag, live and from a file: each booted,
    reached its end line, carries script's date, is the row's program with
    the row's flags, and read as much audio as the row asks. The flag table
    of all history and `flags_run` both count by this."""
    hits = [r for r in runs if r.ended and r.date and r.program == row.program and row.wants(r.flags)
            and (row.min_audio_s is None or (r.audio_s or 0.0) >= row.min_audio_s)]
    live = [] if row.replay_only else [r for r in hits if not r.replay]
    replay = [] if row.live_only else [r for r in hits if r.replay]
    return live, replay


def flag_table(runs: list[LoggedRun]) -> list[dict[str, Any]]:
    """Each flag's last run on the body, live and from a file, from the runs
    that booted, reached their end line and carry script's date."""
    rows = []
    for row in FLAG_ROWS:
        lives, replays = flag_hits(row, runs)
        live, replay = latest(lives), latest(replays)
        cited = latest(r for r in (live, replay) if r is not None)
        rows.append({
            "flag": row.label,
            "live": live.date if live else None,
            "replay": replay.date if replay else None,
            "where": f"{cited.where}:{cited.line}" if cited else None,
            "with": ", ".join(f"{k} {v}" for k, v in sorted(cited.providers.items())) if cited else "",
        })
    return rows


def flags_exercised(typescripts: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """`run.json`'s `flags_run`: each flag, and way, that this run exercised.

    `typescripts` is (sub-step, its typescript's text) for each speaker or
    live sub-step of this run that passed, in the order they ran. A flag
    counts by `flag_hits`, as it does in the flag table, so a `--stats` run
    whose footer read under ten minutes of audio adds no entry though its
    step passed. One entry for each flag and way, naming the first sub-step
    that showed it, in `FLAG_ROWS`' order."""
    first: dict[tuple[str, str], str] = {}
    for step, text in typescripts:
        runs = logged_runs(text, step)
        for row in FLAG_ROWS:
            for how, hits in zip(("live", "replay"), flag_hits(row, runs)):
                if hits:
                    first.setdefault((row.label, how), step)
    return [{"flag": row.label, "how": how, "step": first[row.label, how]}
            for row in FLAG_ROWS for how in ("live", "replay") if (row.label, how) in first]


# --------------------------------------------------------------------------
# The soak's summary
# --------------------------------------------------------------------------

FOOTER_FRAMES = re.compile(r"^  frames +(\d+) +\((\d+(?:\.\d+)?)s of audio\)")
FOOTER_PER_FRAME = re.compile(r"^  per frame +mean ([\d.]+) ms +p50 ([\d.]+) +p95 ([\d.]+) +max ([\d.]+)")
FOOTER_OVER = re.compile(r"^  budget +(\d+) ms/frame, (\d+) frame\(s\) over")


def key_values(text: str) -> dict[str, str]:
    """The last value of each `key=value` line."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[a-z_]+", key):
            found[key] = value.strip()
    return found


def parse_samples(text: str) -> list[dict[str, str]]:
    """The soak's samples, one `t=... rss_kb=... ...` line each."""
    rows = []
    for line in text.splitlines():
        if line.startswith("t="):
            rows.append(dict(word.split("=", 1) for word in line.split() if "=" in word))
    return rows


def number(row: dict[str, str], key: str) -> float | None:
    try:
        return float(row[key])
    except (KeyError, ValueError):
        return None


def footer_numbers(text: str) -> dict[str, float]:
    """What the engine's `--stats` footer says of the frames: the last
    footer in the text, which is the run's. `budget_ms` is the engine's own
    budget a frame, its frame's length, and `over` counts the frames over
    that, whatever it is."""
    found: dict[str, float] = {}
    for line in text.splitlines():
        if (m := FOOTER_FRAMES.match(line)) is not None:
            found = {"frames": float(m.group(1)), "audio_s": float(m.group(2))}
        elif (m := FOOTER_PER_FRAME.match(line)) is not None:
            found.update(mean=float(m.group(1)), p95=float(m.group(3)), max=float(m.group(4)))
        elif (m := FOOTER_OVER.match(line)) is not None:
            found.update(budget_ms=float(m.group(1)), over=float(m.group(2)))
    return found


def frames_over_budget(footer: dict[str, float]) -> int | None:
    """Frames over FRAME_BUDGET_MS from a footer: the engine's own count when
    its budget line names FRAME_BUDGET_MS, 0 when its longest frame is
    within it, and otherwise not known."""
    if footer.get("budget_ms") == FRAME_BUDGET_MS and "over" in footer:
        return int(footer["over"])
    longest = footer.get("max")
    return 0 if longest is not None and longest <= FRAME_BUDGET_MS else None


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """The least-squares slope of (seconds, MB), in MB an hour."""
    if len(points) < 2:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    spread = sum((t - mean_t) ** 2 for t, _ in points)
    if spread == 0:
        return None
    return sum((t - mean_t) * (v - mean_v) for t, v in points) / spread * 3600.0


def throttle_bits(text: str) -> int | None:
    match = re.search(r"0x([0-9a-fA-F]+)", text or "")
    return int(match.group(1), 16) if match else None


def throttle_flags(samples: list[dict[str, str]]) -> list[str]:
    """The throttle flags the soak raised: a bit of the "now" half set in any
    sample, and a bit of the "has occurred" half set in a sample and not in
    the first, since those stay set from whatever happened since the boot."""
    bits = [b for b in (throttle_bits(s.get("throttled", "")) for s in samples) if b is not None]
    if not bits:
        return []
    raised = 0
    for value in bits:
        raised |= value & 0xF
        raised |= value & 0xF0000 & ~bits[0]
    return [THROTTLE_BITS.get(bit, f"bit {bit}") for bit in range(raised.bit_length()) if raised >> bit & 1]


def first_last_max(values: list[float]) -> dict[str, int | None]:
    if not values:
        return {"first": None, "last": None, "max": None}
    return {"first": int(values[0]), "last": int(values[-1]), "max": int(max(values))}


def summarize_soak(minutes: int, files: dict[str, str]) -> dict[str, Any]:
    """`run.json`'s `soak` from the files the soak's script wrote. `flat`
    is Stage 4's exit, each threshold a named constant above, held over the
    soak's whole length; `reasons` says what made it false. A soak that read
    less audio than its length, that a signal stopped, or whose -X dev pass
    did not run is never flat."""
    status = key_values(files.get("status", ""))
    refused = status.get("refused")
    samples = parse_samples(files.get("samples", ""))
    dev_samples = parse_samples(files.get("dev_samples", ""))
    listening = [s for s in samples if s.get("listening", "0") not in ("", "0") and number(s, "t") is not None]
    reasons: list[str] = []
    if refused:
        reasons.append(f"the soak did not start: {refused}")
    start_t = number(listening[0], "t") if listening else None
    judged = [s for s in listening if start_t is not None and (number(s, "t") or 0) - start_t >= WARMUP_S]
    rss = [(number(s, "t"), number(s, "rss_kb")) for s in judged]
    points = [(t, kb / 1024.0) for t, kb in rss if t is not None and kb is not None]
    growth_whole = slope_per_hour(points)
    late = points[int(len(points) * RSS_JUDGED_FROM):]
    growth = slope_per_hour(late) if len(late) >= MIN_SOAK_SAMPLES else growth_whole
    fds = [v for v in (number(s, "fds") for s in judged) if v is not None]
    threads = [v for v in (number(s, "threads") for s in judged) if v is not None]
    if not refused:
        if len(judged) < MIN_SOAK_SAMPLES:
            reasons.append(f"{len(judged)} sample(s) after the {WARMUP_S // 60}-minute warm-up are too few to judge; "
                           f"{MIN_SOAK_SAMPLES} are needed")
        elif growth is None:
            reasons.append(f"resident memory was read in {len(points)} of the {len(judged)} sample(s) after the "
                           "warm-up, too few to say whether it grew")
        elif growth >= RSS_GROWTH_MB_PER_H:
            reasons.append(f"resident memory grew {growth:.2f} MB an hour over the second half of the soak, at or "
                           f"over {RSS_GROWTH_MB_PER_H:g}")
        if fds and min(fds) != max(fds):
            reasons.append(f"open files were not constant: {int(fds[0])} after the warm-up, {int(fds[-1])} at the "
                           f"end, {int(max(fds))} at most")
        if judged and not threads:
            reasons.append(f"threads were read in none of the {len(judged)} sample(s) after the warm-up")
        if threads and min(threads) != max(threads):
            reasons.append(f"threads were not constant: {int(threads[0])} after the warm-up, {int(threads[-1])} at "
                           f"the end, {int(max(threads))} at most")
    # Every sample of both listeners counts, the warm-up's included.
    devices = [f"{int(number(s, 'snd') or 0)} sound device file(s) and {int(number(s, 'pcm_mine') or 0)} PCM(s) "
               f"open at t={s.get('t')}{where}" for group, where in ((samples, ""), (dev_samples, " under -X dev"))
               for s in group if (number(s, "snd") or 0) > 0 or (number(s, "pcm_mine") or 0) > 0]
    if devices:
        reasons.append("the soak's listener opened a sound device: " + devices[0])
    flags = throttle_flags(samples)
    if flags:
        reasons.append("the board raised a throttle flag: " + ", ".join(flags))
    footer = footer_numbers(files.get("listen", ""))
    over = frames_over_budget(footer)
    audio_s = footer.get("audio_s")
    if not refused:
        if "frames" not in footer:
            reasons.append("the listener printed no footer, so its frames were not counted")
        elif over:
            reasons.append(f"{over} frame(s) took over {FRAME_BUDGET_MS:g} ms")
        elif footer.get("max") is not None and footer["max"] > FRAME_BUDGET_MS:
            reasons.append(f"the longest frame took {footer['max']:g} ms, over {FRAME_BUDGET_MS:g} ms")
        elif over is None:
            reasons.append(f"the footer gives neither its longest frame nor a count against {FRAME_BUDGET_MS:g} ms, "
                           "so the frames were not judged")
        # The soak's length less one sample: the feeder sends exactly the
        # soak's seconds, and the listener reads them all when it runs its
        # length.
        if audio_s is not None and audio_s < minutes * 60 - SAMPLE_S:
            reasons.append(f"the listener read {audio_s / 60:.1f} minute(s) of audio of the {minutes} asked")
        for name, said in (("listen", "the listener"), ("dev", "the -X dev pass")):
            if status.get(f"{name}_stopped"):
                killed = f"; {status[f'{name}_killed']}" if status.get(f"{name}_killed") else ""
                reasons.append(f"{said} was stopped by the soak's script: {status[f'{name}_stopped']}{killed}")
        if "listen_exit" not in status:
            reasons.append("the listener's exit was not recorded: the soak's script ended before it did")
        elif status["listen_exit"] != "0":
            reasons.append(f"the listener exited {status['listen_exit']}")
        if status.get("feed_exit", "0") != "0":
            reasons.append(f"the feeder exited {status['feed_exit']}")
        if "dev_exit" not in status:
            reasons.append("the -X dev pass did not run" + (f": {status['dev_refused']}"
                                                             if status.get("dev_refused") else ""))
        elif status["dev_exit"] != "0":
            reasons.append(f"the -X dev pass exited {status['dev_exit']}")
        if status.get("soak_exit", "0") != "0":
            reasons.append(f"the soak's script exited {status['soak_exit']}")
    pcm_after = [line for line in files.get("pcm_after", "").splitlines() if line.strip()]
    if pcm_after and not refused:
        reasons.append("a sound card was open when the soak ended: " + pcm_after[0].removeprefix("pcm="))
    audio = [line for line in files.get("audio", "").splitlines() if line.strip()]
    if audio:
        reasons.append("audio was written during the soak: " + ", ".join(audio[:3]))
    warnings = [line.strip() for line in files.get("dev_err", "").splitlines() if DEV_WARNING.search(line)]
    unique = list(dict.fromkeys(warnings))
    cpu = None
    ticks = [(number(s, "t"), number(s, "cpu_ticks")) for s in judged]
    ticks = [(t, c) for t, c in ticks if t is not None and c is not None]
    clk = number(status, "clk_tck") or 100.0
    if len(ticks) >= 2 and ticks[-1][0] > ticks[0][0]:
        cpu = round((ticks[-1][1] - ticks[0][1]) / clk / (ticks[-1][0] - ticks[0][0]) * 100.0, 2)
    rss_all = [v for v in (number(s, "rss_kb") for s in listening) if v is not None]
    return {
        "minutes": minutes,
        "samples": len(samples),
        "judged": len(judged),
        "rss_start_mb": round(rss_all[0] / 1024.0, 1) if rss_all else None,
        "rss_end_mb": round(rss_all[-1] / 1024.0, 1) if rss_all else None,
        "rss_growth_mb_per_h": round(growth, 3) if growth is not None else None,
        "rss_growth_whole_mb_per_h": round(growth_whole, 3) if growth_whole is not None else None,
        "open_files": first_last_max(fds),
        "threads": first_last_max(threads),
        "frames": int(footer["frames"]) if "frames" in footer else None,
        "frames_over_80ms": over,
        "per_frame_ms": {k: footer.get(k) for k in ("mean", "p95", "max")},
        "cpu_pct": cpu,
        "throttle_flags": flags,
        "devices_opened": devices,
        "pcm_before": [line for line in files.get("pcm_before", "").splitlines() if line.strip()],
        "pcm_after": pcm_after,
        # A count only of a pass that ran.
        "dev_warnings": len(warnings) if "dev_exit" in status else None,
        "dev_warning_lines": unique[:20],
        "listen_exit": status.get("listen_exit"),
        "dev_exit": status.get("dev_exit"),
        "flat": not reasons,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------
# The budgets
# --------------------------------------------------------------------------


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def spread_pct(values: list[float]) -> float | None:
    """How far repeated runs of one measure lie apart, as a share of their
    median: the spread Stage 4's bound takes when it is over 25%."""
    if len(values) < 2:
        return None
    middle = median(values)
    return round((max(values) - min(values)) / abs(middle) * 100.0, 1) if middle else None


def board_text(preflight: dict[str, Any]) -> str | None:
    """The board lines as the preflight read them, in one line."""
    if not preflight or not preflight.get("temp"):
        return None
    temp = str(preflight.get("temp", "")).removeprefix("temp=")
    throttled = str(preflight.get("throttled", "")).removeprefix("throttled=")
    load = " ".join(str(preflight.get("loadavg", "")).split()[:3])
    means = ", ".join(preflight.get("throttled_means") or throttle_meaning(throttled))
    return f"{temp}, throttled {throttled} ({means}), load {load}"


def machine_of(record: dict[str, Any]) -> str | None:
    kernel = str((record.get("preflight") or {}).get("kernel") or "").split()
    return kernel[-1] if kernel else None


def passing(record: dict[str, Any]) -> bool:
    """A run that was done and in which no step failed or was refused."""
    return (str(record.get("verdict", "")).startswith("body run done")
            and not any(s.get("status") in ("failed", "refused") for s in record.get("steps", [])))


def measures_of(record: dict[str, Any]) -> dict[str, tuple[Any, float | None]]:
    """Each measure a run recorded, with the spread recorded beside it. A run
    of format 1 recorded only the board lines, in its preflight."""
    found: dict[str, tuple[Any, float | None]] = {}
    for entry in record.get("budgets") or []:
        if isinstance(entry, dict) and entry.get("value") is not None:
            found[entry.get("measure")] = (entry["value"], entry.get("spread_pct"))
    if BOARD not in found and (text := board_text(record.get("preflight") or {})) is not None:
        found[BOARD] = (text, None)
    return found


def earlier_runs(top: Path, release: str, host: str, machine: str | None, *, same_release: bool = False,
                 skip: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """The passing runs on this body (the same host, the same machine) of
    releases before this one, or up to and including it with
    `same_release`, highest release first and newest run first within one."""
    found = []
    for path in (top / "releases").glob("v*/body/*/run.json"):
        version = path.relative_to(top / "releases").parts[0][1:]
        if not VERSION.match(version) or (skip is not None and path.resolve() == skip.resolve()):
            continue
        if version_key(version) > version_key(release) or (version == release and not same_release):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or not passing(record):
            continue
        if record.get("host") != host or machine is None or machine_of(record) != machine:
            continue
        found.append((version_key(version), str(record.get("started", "")), path, record))
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(path, record) for _, _, path, record in found]


def relative(path: Path, top: Path) -> str:
    try:
        return path.relative_to(top).as_posix()
    except ValueError:
        return path.as_posix()


def judge(value: Any, last: Any, spread: float | None) -> tuple[float | None, float | None, bool | None]:
    """(change_pct, bound_pct, within). A rise past the bound is out of
    bounds; a fall never is. With no earlier value nothing is judged."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None, None, None
    bound = round(max(BOUND_PCT, spread or 0.0), 1)
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return None, bound, None
    if last == 0:
        return (0.0 if value == 0 else None), bound, value <= 0
    change = round((value - last) / abs(last) * 100.0, 1)
    return change, bound, change <= bound


def budget_rows(values: dict[str, list[float]], board: str | None, *, top: Path, release: str, host: str,
                machine: str | None, skip: Path | None = None) -> list[dict[str, Any]]:
    """`run.json`'s `budgets`: one object for each measure of the template's
    table. `last` is read from the newest passing run of the highest earlier
    release on the same body that has the measure; the bound takes this
    run's spread when it repeated the measure, and otherwise the newest
    spread recorded on this body."""
    before = earlier_runs(top, release, host, machine, skip=skip)
    spreads = earlier_runs(top, release, host, machine, same_release=True, skip=skip)
    rows = []
    for label, unit in MEASURES:
        measured = values.get(label) or []
        value: Any = board if label == BOARD else (round(median(measured), 2) if measured else None)
        last = last_file = None
        for path, record in before:
            held = measures_of(record).get(label)
            if held is not None:
                last, last_file = held[0], relative(path, top)
                break
        spread = spread_pct(measured)
        if spread is None:
            spread = next((held[1] for _, record in spreads
                           if (held := measures_of(record).get(label)) is not None and held[1] is not None), None)
        change, bound, within = judge(value, last, spread)
        rows.append({"measure": label, "value": value, "unit": unit, "last": last, "last_file": last_file,
                     "change_pct": change, "bound_pct": bound, "within": within, "spread_pct": spread,
                     "values": [round(v, 2) for v in measured]})
    return rows


def budget_values(results: dict[str, dict[str, Any]]) -> dict[str, list[float]]:
    """Each measure's repeated values from the budget's driver results, named
    `frames-N`, `speak-N` and `idle-N`. A run that did not exit 0 gives none.
    The turn gap of a run is the median of its turns' gaps, so `--repeat`
    gives one value a run and the row is their median, as every row's is."""
    def runs(kind: str) -> list[dict[str, Any]]:
        return [r for name, r in sorted(results.items()) if name.startswith(kind + "-") and r.get("exit") == 0]

    frames, speak, idle = runs("frames"), runs("speak"), runs("idle")
    values: dict[str, list[float]] = {label: [] for label, _ in MEASURES}
    for r in frames:
        if isinstance(r.get("first_ns"), int) and isinstance(r.get("start_ns"), int):
            values["Boot to first frame"].append((r["first_ns"] - r["start_ns"]) / 1e6)
        if r.get("frames"):
            values["Per-frame mean"].append(float(r["mean_ms"]))
            values["Per-frame 99th percentile"].append(float(r["p99_ms"]))
            # The engine counts frames over its own frame's length, which is
            # this measure only when that length is FRAME_BUDGET_MS.
            if r.get("budget_ms", FRAME_BUDGET_MS) == FRAME_BUDGET_MS:
                values["Frames over 80 ms"].append(float(r["over"]))
        if isinstance(r.get("rss_boot_kb"), int):
            values["Resident memory after boot"].append(r["rss_boot_kb"] / 1024.0)
    for r in speak:
        firsts = r.get("voice_first_ms") or []
        if firsts:
            values["First chunk at the null sink after the final, local path"].append(sum(firsts) / len(firsts))
        gaps = [g for g in r.get("gap_ms") or [] if isinstance(g, (int, float)) and not isinstance(g, bool)]
        if gaps:
            values[TURN_GAP].append(float(median(gaps)))
    for r in idle:
        wall = (r.get("wall_end") or 0) - (r.get("wall_start") or 0)
        if wall > 0 and r.get("cpu_end") is not None and r.get("cpu_start") is not None:
            values["Idle CPU"].append((r["cpu_end"] - r["cpu_start"]) / wall * 100.0)
    return values


def shown(value: Any, unit: str) -> str:
    if value is None:
        return "not measured"
    if not isinstance(value, (int, float)):
        return str(value)
    if unit == "ms":
        return f"{value:.2f} ms" if value < 10 else f"{value:.0f} ms"
    if unit == "MB":
        return f"{value:.1f} MB"
    if unit == "frames":
        return f"{int(value)}"
    return f"{value:.1f}{unit if unit.startswith('%') else ' ' + unit}"


def release_of(path_text: str | None) -> str:
    match = re.search(r"(?:^|/)v(\d+\.\d+\.\d+)/", path_text or "")
    return match.group(1) if match else "?"


def budget_table(record: dict[str, Any], here: str) -> str:
    """The Markdown table of the plan's §3 and the pull request, from a
    `run.json` with `budgets`, and the line naming the platform, the commit
    and the bank version every number belongs to."""
    lines = ["| Measure | Last release | This release | Change | Bound | File |", "|---|---|---|---|---|---|"]
    judged = within = out = fresh = 0
    for row in record.get("budgets") or []:
        last = row.get("last")
        last_cell = f"{shown(last, row.get('unit', ''))} ({release_of(row.get('last_file'))})" if last is not None \
            else "none recorded"
        change = "" if row.get("change_pct") is None else f"{row['change_pct']:+.1f}%"
        if row.get("within") is False:
            change = (change + ", " if change else "") + "**out of bounds**"
            out += 1
        if row.get("within") is not None:
            judged += 1
            within += bool(row["within"])
        elif row.get("value") is not None and row.get("bound_pct") is not None:
            fresh += 1
        bound = "" if row.get("bound_pct") is None else f"{row['bound_pct']:g}%"
        if row.get("spread_pct") and row.get("bound_pct") and row["spread_pct"] >= BOUND_PCT:
            bound += " (spread)"
        files = f"`{here}`" + (f"; last `{row['last_file']}`" if row.get("last_file") else "")
        lines.append(f"| {row.get('measure')} | {last_cell} | {shown(row.get('value'), row.get('unit', ''))} | "
                     f"{change} | {bound} | {files} |")
    source = record.get("source") or {}
    budgets_step = next((s for s in record.get("steps", []) if s.get("name") == "budgets"), {})
    bank = (budgets_step.get("details") or {}).get("bank_version") or "not read"
    machine = machine_of(record) or "?"
    lines.append("")
    lines.append(f"Platform linux-{machine} ({record.get('host')}, {(record.get('preflight') or {}).get('kernel', '?')}), "
                 f"commit {str(source.get('head') or '?')[:7]} ({source.get('kind', '?')}), bank {bank}, "
                 f"repeats {(budgets_step.get('details') or {}).get('repeat', '?')}. Judged {judged}: {within} within, "
                 f"{out} out of bounds; {fresh} with no earlier value.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The audit and the nightly
# --------------------------------------------------------------------------


def pep503(name: str) -> str:
    """A distribution's name as PEP 503 normalises it."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Locked:
    """One requirement of a hashed lock: the requirement with its marker,
    its hashes, and the names `# via` says it is there for."""
    requirement: str
    hashes: list[str] = field(default_factory=list)
    via: set[str] = field(default_factory=set)

    def line(self) -> str:
        """The requirement and its hashes on one line, as pip reads a line
        whose continuations are joined."""
        return " ".join([self.requirement, *self.hashes])


def lock_entries(text: str) -> dict[str, Locked]:
    """Each requirement of a lock `uv pip compile --generate-hashes` wrote,
    by its PEP 503 name, in the lock's order. A `# via` comment names the
    requirements it is there for on its own line or on the lines under it;
    `-r FILE` there names a file, which is no requirement."""
    found: dict[str, Locked] = {}
    current: Locked | None = None
    in_via = False
    for raw in text.splitlines():
        words = raw.strip().rstrip("\\").strip()
        if not words or raw.startswith("#"):
            # A blank line, or the header's own comments at the margin.
            in_via = False
            continue
        if not raw[:1].isspace():
            spec = [w for w in words.split() if not w.startswith("--hash=")]
            current = Locked(" ".join(spec), [w for w in words.split() if w.startswith("--hash=")])
            found[pep503(re.split(r"[=<>!~;\s\[]", words, maxsplit=1)[0])] = current
            in_via = False
            continue
        if current is None:
            continue
        if words.startswith("--hash="):
            current.hashes += words.split()
        elif words.startswith("# via"):
            in_via = True
            rest = words.removeprefix("# via").strip()
            if rest and not rest.startswith("-"):
                current.via.add(pep503(rest))
        elif in_via and words.startswith("#"):
            item = words.lstrip("#").strip()
            if item and not item.startswith("-"):
                current.via.add(pep503(item))
        else:
            in_via = False
    return found


def audit_requirements(text: str) -> tuple[str, str]:
    """pip-audit's part of the CI lock, one requirement a line with its
    hashes in the lock's order, and pip-audit's own pin. The part is
    pip-audit and every requirement the lock holds for it, or for one of
    those, however deep. Raises RunnerError when the lock holds no pip-audit
    or a requirement of that part has no hash."""
    entries = lock_entries(text)
    tool = entries.get(AUDIT_TOOL)
    if tool is None:
        raise RunnerError(f"the CI lock holds no {AUDIT_TOOL}")
    needed = {AUDIT_TOOL}
    grew = True
    while grew:
        grew = False
        for name, entry in entries.items():
            if name not in needed and entry.via & needed:
                needed.add(name)
                grew = True
    unhashed = sorted(name for name in needed if not entries[name].hashes)
    if unhashed:
        raise RunnerError(f"the CI lock pins {', '.join(unhashed)} with no hash")
    lines = [entry.line() for name, entry in entries.items() if name in needed]
    pin = tool.requirement.split(";")[0].strip()
    return "\n".join(lines) + "\n", pin


def audit_record(values: dict[str, list[str]], code: str, pin: str = AUDIT_TOOL) -> tuple[dict[str, Any], str]:
    """`run.json`'s `audit`, and the step's reason, from what the audit's
    script printed. Never a failure: no network, a tool that would not
    install and a finding all leave the run's verdict alone. `pin` is the
    lock's, `pip-audit==2.10.1`."""
    tool = (values.get("version") or [""])[-1].strip() or pin.replace("==", " ")
    tail = " / ".join(values.get("tail", []))[-200:]
    state = (values.get("state") or [""])[-1]
    if state:
        why = {
            "no-venv": "the audit's venv could not be made with python3 -m venv",
            "no-install": f"{pin} could not be installed in ~/emet-ci/{AUDIT_VENV} from the lock's hashes",
            "no-runner-venv": "the runner's venv did not say where its packages are",
        }.get(state, state)
        if NO_NETWORK.search(tail):
            why += ": no network"
        return {"status": "not run", "reason": why, "tool": tool, "findings": []}, why
    if (values.get("parsed") or ["no"])[-1] != "yes":
        why = "pip-audit did not finish" + (": no network" if NO_NETWORK.search(tail) else
                                             f" (exit {code or 'unknown'}): {tail or 'no output'}")
        return {"status": "not run", "reason": why, "tool": tool, "findings": []}, why
    findings = []
    for line in values.get("finding", []):
        name, version, ident, fix = (line.split("|") + ["", "", "", ""])[:4]
        findings.append({"name": name, "version": version, "id": ident, "fix": fix})
    packages = (values.get("packages") or ["?"])[-1]
    if findings:
        named = sorted({f"{f['name']} {f['version']}" for f in findings})
        why = f"{len(findings)} finding(s) in {', '.join(named)}, of {packages} package(s) audited"
    else:
        why = f"no known vulnerability in {packages} package(s) audited"
    return {"status": "ran", "reason": why, "tool": tool, "findings": findings}, why


def parse_nightly(text: str) -> dict[str, Any]:
    """One nightly report of tools/body/nightly.sh: its fields, and the
    suites one line a package."""
    found: dict[str, Any] = {"suites": {}}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[a-z_][a-z0-9_.-]*", key):
            continue
        if key.startswith("suite_"):
            found["suites"][key.removeprefix("suite_")] = value
        else:
            found[key] = value
    return found


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The events files and the release sitting's sheet
# --------------------------------------------------------------------------


def events_reader() -> Any:
    """tools/events_file.py, loaded by its path, since run.py runs as a
    script and the reader sits in the folder above it."""
    if not EVENTS_READER.is_file():
        raise RunnerError(f"{EVENTS_READER.name} is not in tools/, so no events file can be read")
    spec = importlib.util.spec_from_file_location("emet_events_file", EVENTS_READER)
    if spec is None or spec.loader is None:
        raise RunnerError(f"{EVENTS_READER} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def turn_records(path: Path) -> list[dict[str, Any]]:
    """The `turn` records of an events file, in the file's order. The reader
    refuses a format other than emet-events 1.x and skips a kind it does not
    know; a refusal is a RunnerError naming the file."""
    reader = events_reader()
    try:
        found = reader.read(path)
    except (reader.EventsError, OSError, ValueError) as exc:
        raise RunnerError(f"{path.name} could not be read: {exc}") from exc
    return list(found.turns)


def events_summary(path: Path) -> dict[str, Any]:
    """A hardware step's `details.events`: the file's name, its turns, and the
    median of their gaps in whole ms as the reader sums it, null when no turn
    made a sound."""
    turns = turn_records(path)
    gap = events_reader().gaps(turns)
    return {"file": path.name, "turns": len(turns), "gap_ms_median": gap["median_ms"] if gap else None}


#: The release sitting's sheet in tools/body/sitting.md (EV4 stage 1): its
#: columns, its rows, and the rows whose turns run under `--stats --events`,
#: joined in order to the events file's turn records. The idea of putting a
#: person's judgement of each turn beside its measured costs is PARADISE's
#: (Walker, Litman, Kamm and Abella, ACL 1997; an idea, CITATIONS.md); the
#: anchors of the wait's 1 to 5 are this project's words (0.5.7-D7).
SHEET_COLUMNS = ("#", "Asked", "Whole", "Held", "Voice", "Wait (1 to 5)", "Tries", "Body", "Notes")
SHEET_BOXES = {"Asked": "asked", "Whole": "whole", "Held": "held", "Voice": "voice", "Body": "body"}
SHEET_ROWS = tuple(range(1, 11))
JOINED_ROWS = tuple(range(3, 11))
SITTING_FORMAT = "emet-sitting/1"
SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")


class SheetError(ValueError):
    """A filled sheet the runner cannot read, naming the row and the column."""


def table_cells(line: str) -> list[str] | None:
    """A Markdown table row's cells, stripped, or None for a line that is no
    table row."""
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|") or len(text) < 2:
        return None
    return [cell.strip() for cell in text[1:-1].split("|")]


def read_sheet(text: str) -> list[dict[str, Any]]:
    """The ten rows of a filled sheet, the table under sitting.md's columns
    with the maintainer's answers written in. A box is `[x]` (true), `[ ]`
    (false) or empty (null, a box the turn does not have); the wait is 1 to
    5 or empty; the tries a whole number from 1 or empty; the notes any text,
    and a `|` in them is kept. Raises SheetError naming the row and column."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if table_cells(line) == list(SHEET_COLUMNS)), None)
    if header is None:
        raise SheetError("no table has the sheet's columns: | " + " | ".join(SHEET_COLUMNS) + " |")
    if header + 1 >= len(lines) or not SEPARATOR.match(lines[header + 1].strip()):
        raise SheetError("the sheet's header has no |---| line under it")
    rows: list[dict[str, Any]] = []
    for line in lines[header + 2:]:
        cells = table_cells(line)
        if cells is None:
            break
        if len(cells) < len(SHEET_COLUMNS):
            raise SheetError(f"the row {line.strip()!r} has {len(cells)} cells, and the sheet has "
                             f"{len(SHEET_COLUMNS)} columns")
        # The notes are the last column, and a `|` in them splits them: put
        # them back together as they were written.
        raw = line.strip()[1:-1].split("|")
        cells = cells[:len(SHEET_COLUMNS) - 1] + ["|".join(raw[len(SHEET_COLUMNS) - 1:]).strip()]
        named = dict(zip(SHEET_COLUMNS, cells))
        expected = len(rows) + 1
        if named["#"] != str(expected):
            raise SheetError(f"row {expected} is numbered {named['#']!r}; the rows run 1 to {len(SHEET_ROWS)} "
                             "in order")
        row: dict[str, Any] = {"turn": expected}
        for column, key in SHEET_BOXES.items():
            value = named[column].lower()
            if value not in ("[x]", "[ ]", ""):
                raise SheetError(f"row {expected}, {column}: {named[column]!r} is not [x], [ ] or empty")
            row[key] = {"[x]": True, "[ ]": False, "": None}[value]
        wait, tries = named["Wait (1 to 5)"], named["Tries"]
        if wait and wait not in ("1", "2", "3", "4", "5"):
            raise SheetError(f"row {expected}, Wait: {wait!r} is not a rating from 1 to 5")
        if tries and not (tries.isdigit() and int(tries) >= 1):
            raise SheetError(f"row {expected}, Tries: {tries!r} is not a whole number from 1")
        row.update(wait=int(wait) if wait else None, tries=int(tries) if tries else None,
                   notes=named["Notes"] or None)
        rows.append({key: row[key] for key in ("turn", "asked", "whole", "held", "voice", "wait", "tries", "body",
                                                "notes")})
    if len(rows) != len(SHEET_ROWS):
        raise SheetError(f"the sheet has {len(rows)} rows, and a sitting has {len(SHEET_ROWS)}")
    return rows


def sitting_record(rows: list[dict[str, Any]], records: list[dict[str, Any]] | None, release: str,
                   problem: str | None = None) -> dict[str, Any]:
    """sitting.json: the sheet's rows, each with `record`, the events file's
    turn record for rows 3 to 10 in order. The ratings are kept whether or
    not the turns join; a join needs exactly one turn record a joined row,
    and anything else leaves every `record` null and says why."""
    turns = [dict(row, record=None) for row in rows]
    if problem is not None or records is None:
        joined = f"not joined: {problem or 'no events file'}"
    elif len(records) != len(JOINED_ROWS):
        joined = f"not joined: {len(records)} turn records for {len(JOINED_ROWS)} rows"
    else:
        joined = "joined"
        for turn, record in zip((t for t in turns if t["turn"] in JOINED_ROWS), records):
            turn["record"] = record
    return {"format": SITTING_FORMAT, "release": release, "joined": joined, "turns": turns}


@dataclass
class Step:
    name: str
    status: str  # passed, failed, not run, refused
    reason: str = ""
    seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


class BodyRun:
    def __init__(self, args: argparse.Namespace, *, remote: Any, laptop: Any,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 log_check: Path = LOG_CHECK, history: list[Path] | None = None,
                 sleep: Callable[[float], None] = time.sleep, top: Path = TOP) -> None:
        self.args = args
        self.remote = remote
        self.laptop = laptop
        self.clock = clock
        self.log_check = log_check
        self.history = history
        self.sleep = sleep
        #: `Project Emet/`, whose releases/ the budgets read the last
        #: release's numbers from.
        self.top = top
        #: `run.json`'s `audit`, `soak` and `budgets`, each absent until its
        #: step ran.
        self.audit_result: dict[str, Any] | None = None
        self.soak_result: dict[str, Any] | None = None
        self.budget_result: list[dict[str, Any]] | None = None
        self.budget_values: dict[str, list[float]] = {}
        #: The soak's folder on the body while one this run started is
        #: running, so the lock is left to it if the laptop goes.
        self.soak_dir = ""
        self.started = clock()
        self.stamp = self.started.strftime("%Y%m%d-%H%M%S")
        self.token = uuid.uuid4().hex
        self.steps: list[Step] = []
        self.preflight: dict[str, Any] = {}
        self.source: dict[str, Any] = {}
        self.home = ""
        self.locked = False
        self.verdict = ""
        self.flags: list[dict[str, Any]] = []
        self.flags_run: list[dict[str, str]] = []
        #: The body's fingerprint, in every run's record. Until `fingerprint`
        #: has read it, the hashes are null and the note says why.
        self.body: dict[str, Any] = {
            "manifest": args.manifest, "manifest_sha256": None, "state": None, "state_sha256": None,
            "note": "not read: the run ended before the body's manifest was hashed",
        }
        self.out_dir: Path | None = None
        #: Whether the body's engine has `--events`; prepare_body reads it.
        self.events_ok = True

    # paths on the body, known once preflight has read $HOME
    @property
    def ci(self) -> str:
        return f"{self.home}/emet-ci"

    @property
    def run_dir(self) -> str:
        return f"{self.ci}/runs/{self.stamp}"

    @property
    def tree(self) -> str:
        return f"{self.ci}/emet"

    def script(self, step: str, body: str) -> str:
        return marked(step, f'RUN={shlex.quote(self.run_dir)}\nmkdir -p "$RUN"\n{body}')

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step

    # ------------------------------------------------------------ preflight

    PREFLIGHT = r"""
echo "home=$HOME"
echo "date=$(date -Iseconds)"
echo "uptime=$(uptime)"
echo "loadavg=$(cat /proc/loadavg)"
echo "disk=$(df -P "$HOME" | tail -n 1)"
if command -v vcgencmd >/dev/null 2>&1; then
  echo "temp=$(vcgencmd measure_temp 2>&1)"
  echo "throttled=$(vcgencmd get_throttled 2>&1)"
else
  echo "temp=no vcgencmd"
  echo "throttled=no vcgencmd"
fi
echo "kernel=$(uname -srm)"
echo "python=$(python3 --version 2>&1)"
echo "emet_env=$EMET_SET"
ps -eo pid=,args= | sed 's/^ */ps=/'
""" + PCM_CHECK.format(root="/proc/asound") + r"""
if [ -e "$CI/lock" ]; then echo "lock=held"; sed 's/^/lock_line=/' "$CI/lock"; else echo "lock=free"; fi
""" + r"""soak=$(cat "$CI/soak.current" 2>/dev/null || true)
if [ -n "$soak" ] && [ -f "$soak/pid" ] && [ ! -f "$soak/done" ] && kill -0 "$(cat "$soak/pid")" 2>/dev/null; then
  echo "soak=$soak"; sed -n 's/^\(started\|minutes\|release\)=/soak_\1=/p' "$soak/meta.txt" 2>/dev/null
fi
"""

    def soak_refusal(self, values: dict[str, list[str]]) -> str | None:
        """Why a soak running on the body stops this run, or None."""
        if not values.get("soak"):
            return None
        meta = {key: (values.get(f"soak_{key}") or ["?"])[-1] for key in ("started", "minutes", "release")}
        return (f"a soak is in progress on the body, for {meta['release']}, started {meta['started']} for "
                f"{meta['minutes']} minute(s); it holds ~/emet-ci/lock until it ends, and "
                "`run.py --soak-collect` brings it home after")

    def check_busy(self) -> str | None:
        """Why the robot is not free right now, or None."""
        done = self.remote.run(marked("busy", self.PREFLIGHT), timeout=60)
        values = done.values()
        if (soak := self.soak_refusal(values)) is not None:
            return soak
        busy = busy_reasons(values)
        if busy:
            return "the robot is in use: " + "; ".join(busy[:3])
        if values.get("lock", ["free"])[-1] == "held" and not self.locked:
            return "~/emet-ci/lock is held: " + " / ".join(values.get("lock_line", []))
        return None

    def run_preflight(self) -> str | None:
        done = self.remote.run(marked("preflight", self.PREFLIGHT), timeout=60)
        if done.code != 0:
            return f"the preflight script exited {done.code}: {done.err.strip()[:200]}"
        values = done.values()
        self.home = done.value("home")
        if not self.home.startswith("/"):
            return "the body did not say where its home directory is"
        throttled = done.value("throttled")
        self.preflight = {
            "date": done.value("date"),
            "uptime": done.value("uptime").strip(),
            "loadavg": done.value("loadavg"),
            "disk": done.value("disk"),
            "temp": done.value("temp"),
            "throttled": throttled,
            "throttled_means": throttle_meaning(throttled),
            "kernel": done.value("kernel"),
            "python": done.value("python"),
            "emet_variables_unset": done.value("emet_env").split(),
            "lock": done.value("lock", "free"),
        }
        busy = busy_reasons(values)
        self.preflight["busy"] = busy
        # A soak's own emet-listen is busy too; the soak is named first, with
        # the way to bring it home.
        if (soak := self.soak_refusal(values)) is not None:
            return soak
        if busy:
            return "the robot is in use: " + "; ".join(busy[:3])
        if self.preflight["lock"] == "held":
            return "~/emet-ci/lock is held: " + " / ".join(values.get("lock_line", []))
        return None

    def take_lock(self) -> str | None:
        # A lock that already holds this run's token is this run's: after a
        # soak that did not free it, the run goes on holding it.
        body = (f'mkdir -p "$CI" && ( set -C; printf "token %s\\nby tools/body/run.py\\nstarted %s\\n" '
                f'{self.token} "$(date -Iseconds)" > "$CI/lock" ) 2>/dev/null '
                f'&& echo "lock=taken" || {{ if grep -qx "token {self.token}" "$CI/lock" 2>/dev/null; then '
                'echo "lock=taken"; else echo "lock=held"; sed "s/^/lock_line=/" "$CI/lock"; fi; }\n')
        done = self.remote.run(marked("lock", body), timeout=60)
        if done.value("lock") != "taken":
            return "~/emet-ci/lock is held: " + " / ".join(done.values().get("lock_line", []))
        self.locked = True
        return None

    def release_lock(self) -> None:
        """Free the lock if it is this run's. A soak this run started that is
        still running keeps it: the soak frees it when it ends."""
        if not self.locked:
            return
        soak = shlex.quote(self.soak_dir) if self.soak_dir else "''"
        body = (f's={soak}; if [ -n "$s" ] && [ -f "$s/pid" ] && [ ! -f "$s/done" ] && kill -0 "$(cat "$s/pid")" '
                '2>/dev/null; then echo "lock=kept for the soak"; '
                f'elif grep -qx "token {self.token}" "$CI/lock" 2>/dev/null; then rm -f "$CI/lock"; '
                'echo "lock=released"; else echo "lock=not ours"; fi\n')
        try:
            self.remote.run(marked("unlock", body), timeout=60)
            self.locked = False
        except Unreachable:
            pass

    # ------------------------------------------------------------- sync

    def sync(self) -> Step:
        start = time.monotonic()
        if self.args.commit:
            step = self.sync_commit()
        else:
            step = self.sync_tree()
        step.seconds = time.monotonic() - start
        return self.add(step)

    def sync_tree(self) -> Step:
        names = self.laptop.git("ls-files", "-co", "--exclude-standard", "-z").decode("utf-8").split("\0")
        autocrlf = self.laptop.config("core.autocrlf") == "true"
        packed = pack(self.laptop.repo, names, autocrlf=autocrlf, mtime=time.time())
        head = self.laptop.git("rev-parse", "HEAD").decode().strip()
        subject = self.laptop.git("log", "-1", "--format=%s").decode("utf-8", "replace").strip()
        known = head_blobs(self.laptop)
        changed = sorted(p for p, b in packed.blobs.items() if p in known and known[p] != b)
        added = sorted(p for p in packed.blobs if p not in known)
        removed = sorted(p for p in known if p not in packed.blobs)
        self.source = {
            "kind": "working tree", "head": head, "subject": subject, "files": packed.files,
            "bytes": packed.size, "digest": packed.digest, "crlf_converted": packed.converted,
            "changed": changed, "added": added, "removed": removed,
        }
        up = self.remote.send('rm -rf "$HOME/emet-ci/incoming" && mkdir -p "$HOME/emet-ci/incoming" '
                              '&& tar -xzf - -C "$HOME/emet-ci/incoming"', packed.data, timeout=600)
        if up.code != 0:
            return Step("sync", "failed", f"the tree did not unpack on the body: {up.err.strip()[:200]}")
        return self.swap(packed.digest)

    def sync_commit(self) -> Step:
        """Fetch the repository's branches and tags, and take the commit only
        when one of them holds it.

        Pull requests are never fetched. A pull request is anyone's code, and
        the body runs it as the maintainer's user, who can read the keys
        file; a commit reached only through one, or through a ref an older
        clone kept, is refused."""
        ref = self.args.commit
        body = f"""set -e
GIT="git -C $CI/repo.git"
mkdir -p "$CI"
[ -d "$CI/repo.git" ] || git init -q --bare "$CI/repo.git"
$GIT fetch -q --prune --no-tags {UPSTREAM} '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
sha=$($GIT rev-parse --verify --quiet {shlex.quote(ref + '^{commit}')} || $GIT rev-parse --verify --quiet {shlex.quote('refs/remotes/origin/' + ref + '^{commit}')} || true)
[ -n "$sha" ] || {{ echo "unknown=yes"; exit 3; }}
held=$($GIT for-each-ref --contains "$sha" --format='%(refname)' refs/remotes/origin refs/tags | grep -v '^refs/remotes/origin/pull/' || true)
[ -n "$held" ] || {{ echo "unheld=yes"; exit 3; }}
echo "sha=$sha"
echo "subject=$($GIT log -1 --format=%s "$sha")"
rm -rf "$CI/incoming" && mkdir -p "$CI/incoming"
$GIT archive --format=tar "$sha" | tar -x -C "$CI/incoming"
cd "$CI/incoming" && echo "digest=$({REMOTE_DIGEST})"
"""
        done = self.remote.run(marked("fetch", body), timeout=900)
        if done.value("unknown"):
            return Step("sync", "failed", f"{ref} is not a commit the public repository has")
        if done.value("unheld"):
            return Step("sync", "failed", f"{ref} is on no branch or tag of the repository; "
                                          "pull requests are not run on the body")
        if done.code != 0:
            return Step("sync", "failed", f"the fetch exited {done.code}: {done.err.strip()[-200:]}")
        self.source = {"kind": "pushed commit", "ref": ref, "head": done.value("sha"),
                       "subject": done.value("subject"), "upstream": UPSTREAM, "digest": done.value("digest")}
        return self.swap(done.value("digest"))

    def swap(self, digest: str) -> Step:
        record = json.dumps(self.source, sort_keys=True)
        body = f"""cd "$CI/incoming" || exit 3
got=$({REMOTE_DIGEST})
echo "digest=$got"
[ "$got" = {shlex.quote(digest)} ] || exit 4
cd "$CI" && rm -rf "$CI/emet" && mv "$CI/incoming" "$CI/emet"
printf '%s\\n' {shlex.quote(record)} > "$CI/source.json"
echo "swapped=yes"
"""
        done = self.remote.run(marked("swap", body), timeout=300)
        if done.value("swapped") != "yes":
            return Step("sync", "failed", f"the body's digest {done.value('digest')[:12] or 'none'} is not the "
                                          f"tree's {digest[:12]}; the tree was not swapped in")
        return Step("sync", "passed", f"{self.source.get('files', 'all')} files, digest {digest[:12]} on both machines",
                    details=self.source)

    def read_source(self) -> None:
        done = self.remote.run(marked("source", 'cat "$CI/source.json" 2>/dev/null || true\n'), timeout=60)
        try:
            self.source = json.loads(done.out.strip() or "{}")
        except json.JSONDecodeError:
            self.source = {}
        self.source.setdefault("kind", "the tree last synced (no sync this run)")

    # ----------------------------------------------------------- install

    def install(self) -> Step:
        # The provider extras, piper among them, are in every install now, so
        # --voice piper needs no other line and never reinstalls. pip itself
        # is upgraded first (ledger L27), and its pin is part of the key.
        extras = list(INSTALL)
        quoted = " ".join(f"-e {shlex.quote(e)}" for e in extras)
        body = f"""cd "$CI/emet" || exit 3
key=$( {{ printf '%s\\n' {shlex.quote(' '.join([*extras, RUNNER_PIP]))}; cat {' '.join(p + '/pyproject.toml' for p in PACKAGES)}; }} | sha256sum | cut -c1-64)
echo "key=$key"
if [ ! -x "$PY" ]; then python3 -m venv "$CI/.venv" || exit 5; echo "venv=made"; fi
if [ "$(cat "$CI/installed.key" 2>/dev/null)" != "$key" ]; then
  rm -f "$CI/installed.key"
  "$PY" -m pip install --disable-pip-version-check -q --upgrade {shlex.quote(RUNNER_PIP)} > "$RUN/install.txt" 2>&1 || {{ echo "upgrade=failed"; tail -n 5 "$RUN/install.txt" | sed 's/^/tail=/'; exit 6; }}
  "$PY" -m pip install --disable-pip-version-check -q {quoted} >> "$RUN/install.txt" 2>&1 || {{ echo "pip=failed"; tail -n 5 "$RUN/install.txt" | sed 's/^/tail=/'; exit 6; }}
  echo "$key" > "$CI/installed.key"
  echo "installed=yes"
else
  echo "installed=no"
fi
echo "pip_version=$("$PY" -m pip --version --disable-pip-version-check 2>/dev/null | cut -d' ' -f2)"
"$PY" -m pip freeze --disable-pip-version-check > "$RUN/pip-freeze.txt" 2>&1
"$PY" -c "import emet_sdk, emet_hal, emet_providers, emet_engine as e; print('versions=' + ' '.join(m.__version__ for m in (emet_sdk, emet_hal, emet_providers, e)))"
"""
        start = time.monotonic()
        done = self.remote.run(self.script("install", body), timeout=2400)
        seconds = time.monotonic() - start
        if done.code != 0 or not done.value("versions"):
            tail = " / ".join(done.values().get("tail", [])) or done.err.strip()[-200:]
            why = (f"{RUNNER_PIP} could not be installed in the runner's venv (exit {done.code}): {tail}"
                   if done.value("upgrade") == "failed" else f"the install exited {done.code}: {tail}")
            return self.add(Step("install", "failed", why, seconds))
        how = "installed, a pyproject.toml, the extras or the pip pin changed" if done.value("installed") == "yes" \
            else "kept, no pyproject.toml changed"
        pip = done.value("pip_version") or None
        return self.add(Step("install", "passed", f"{how}; versions {done.value('versions')}; pip {pip or 'unread'}",
                             seconds, {"key": done.value("key"), "versions": done.value("versions").split(),
                                       "extras": extras, "pip": pip}))

    # ------------------------------------------------------- the body's name

    def fingerprint(self) -> None:
        """Hash the maintainer's manifest, in canonical form, and the carried
        state file, on the body, into `self.body` (BVB1).

        The runner's venv reads them when it is there, since it holds PyYAML
        and the engine that says where the state file is; the body's own
        python3 otherwise. Whatever cannot be read is null with a note, and
        the run goes on: the fingerprint names the body and judges nothing."""
        manifest = remote_path(self.args.manifest, self.home, self.tree)
        body = f"""if [ -x "$PY" ]; then python="$PY"; else python=python3; fi
"$python" - {shlex.quote(manifest)} <<'EMET_PY'
{FINGERPRINT_PY}EMET_PY
echo "code=$?"
"""
        done = self.remote.run(marked("fingerprint", body), timeout=120)
        values = done.values()
        notes = list(values.get("note", []))
        if done.value("code") != "0":
            # stderr may quote the manifest, so only the exit code is kept.
            notes.append(f"the fingerprint script exited {done.value('code') or 'without a code'}")
        state = done.value("state")
        if state.startswith(self.home + "/"):
            state = "~" + state[len(self.home):]
        self.body = {
            "manifest": self.args.manifest,
            "manifest_sha256": done.value("manifest_hash") if HEX64.fullmatch(done.value("manifest_hash")) else None,
            "state": state or None,
            "state_sha256": done.value("state_hash") if HEX64.fullmatch(done.value("state_hash")) else None,
            "note": "; ".join(notes),
        }

    # ------------------------------------------------------------ suites

    def suites(self) -> Step:
        counts: dict[str, Any] = {}
        failed: list[str] = []
        start = time.monotonic()
        for package in PACKAGES:
            body = f"""cd "$CI/emet/{package}" || exit 3
s=$(date +%s%N)
timeout 1800 "$PY" -m pytest -q -p no:cacheprovider --junitxml="$RUN/junit-{package}.xml" > "$RUN/suite-{package}.txt" 2>&1
echo "code=$?"
echo "ms=$(( ($(date +%s%N) - s) / 1000000 ))"
tail -n 1 "$RUN/suite-{package}.txt" | sed 's/^/summary=/'
echo "--- junit"
cat "$RUN/junit-{package}.xml" 2>/dev/null
"""
            done = self.remote.run(self.script(f"suite {package}", body), timeout=2000)
            xml_text = done.out.partition("--- junit\n")[2]
            found = junit_counts(xml_text)
            code = int(done.value("code", "1") or 1)
            entry: dict[str, Any] = {"code": code, "summary": done.value("summary"),
                                     "wall_s": round(int(done.value("ms", "0") or 0) / 1000, 1)}
            if found is not None:
                entry.update(collected=found.collected, passed=found.passed, skipped=found.skipped,
                             failed=found.failed, pytest_s=round(found.seconds, 1))
            counts[package] = entry
            if code != 0 or found is None or found.failed:
                failed.append(package)
        seconds = time.monotonic() - start
        line = "; ".join(f"{p.removeprefix('emet-')} {c.get('collected', '?')} collected, "
                         f"{c.get('passed', '?')} passed, {c.get('skipped', '?')} skipped"
                         for p, c in counts.items())
        if failed:
            return self.add(Step("suites", "failed", f"failed: {', '.join(failed)}. {line}", seconds, counts))
        return self.add(Step("suites", "passed", line, seconds, counts))

    def checks(self) -> Step:
        """The layering, release and style checks. A tree that has
        tools/ci.py runs its `layering` job, the same steps CI's `layering`
        check runs, the three among them; an older tree, such as master
        before 0.5.6, runs the three scripts as 0.5.5 did."""
        old = "".join(
            f'  "$PY" tools/{c} . > "$RUN/check-{c[:-3]}.txt" 2>&1; echo "{c[:-3]}=$?"\n'
            f'  tail -n 1 "$RUN/check-{c[:-3]}.txt" | sed "s/^/{c[:-3]}_said=/"\n' for c in CHECKS)
        body = ('cd "$CI/emet" || exit 3\n'
                'if [ -f tools/ci.py ]; then\n'
                '  "$PY" tools/ci.py layering > "$RUN/check-ci-layering.txt" 2>&1; echo "ci=$?"\n'
                '  grep -E "^(ok: |!!! FAILED: |!!! failed, advisory: |\\.\\.\\. not run here: )" '
                '"$RUN/check-ci-layering.txt" | sed "s/^/ci_line=/"\n'
                '  tail -n 1 "$RUN/check-ci-layering.txt" | sed "s/^/ci_said=/"\n'
                'else\n' + old + 'fi\n')
        start = time.monotonic()
        done = self.remote.run(self.script("checks", body), timeout=900)
        if done.value("ci"):
            code, said = done.value("ci"), done.value("ci_said")
            lines = done.values().get("ci_line", [])
            details = {"via": "tools/ci.py layering", "code": code, "said": said, "lines": lines}
            failed = [line.removeprefix("!!! FAILED: ") for line in lines if line.startswith("!!! FAILED: ")]
            line = f"tools/ci.py layering exit {code}: {said or 'no verdict line'}"
            if failed:
                line += "; " + "; ".join(failed[:3])
            return self.add(Step("checks", "passed" if code == "0" else "failed", line,
                                 time.monotonic() - start, details))
        results = {c[:-3]: {"code": done.value(c[:-3], "missing"), "said": done.value(c[:-3] + "_said")}
                   for c in CHECKS}
        bad = [name for name, r in results.items() if r["code"] != "0"]
        line = "; ".join(f"{name.removeprefix('check_')} {'ok' if r['code'] == '0' else 'exit ' + r['code']}"
                         for name, r in results.items())
        return self.add(Step("checks", "failed" if bad else "passed", line, time.monotonic() - start, results))

    # -------------------------------------------------------------- bank

    def bank(self) -> Step:
        bank = remote_path(self.args.bank, self.home, self.tree)
        extra = " ".join(shlex.quote(a) for a in self.args.bank_arg)
        body = f"""cd "$CI/emet" || exit 3
if [ ! -f tools/bank/replay.py ]; then echo "missing=tools/bank/replay.py is not in the synced tree"; exit 0; fi
if [ ! -f {shlex.quote(bank)}/bank.json ]; then echo "missing=there is no bank at "{shlex.quote(bank)}; exit 0; fi
# On the body the recordings sit in the bank's audio/ folder, away from the
# paths the manifests give, which are the laptop's (tools/tests/conftest.py).
set --
if [ -d {shlex.quote(bank)}/audio ]; then set -- --search {shlex.quote(bank)}/audio; fi
timeout 3600 "$PY" tools/bank/replay.py {shlex.quote(bank)} "$@" --report "$RUN/bank-report.json" {extra} > "$RUN/bank-replay.txt" 2>&1
echo "code=$?"
tail -n 5 "$RUN/bank-replay.txt" | sed 's/^/tail=/'
"$PY" - "$RUN/bank-report.json" <<'EMET_PY'
{BANK_REPORT_PY}EMET_PY
"""
        start = time.monotonic()
        done = self.remote.run(self.script("bank", body), timeout=3700)
        seconds = time.monotonic() - start
        if done.value("missing"):
            return self.add(Step("bank", "not run", done.value("missing"), seconds))
        code = done.value("code", "missing")
        # replay.py's last line is its total; the lines before name files.
        tail = done.values().get("tail", [])
        held = done.value("held")
        replayed = int(held) if held.isdigit() else None
        unreleased = done.values().get("unreleased", [])
        reason = f"replay.py exit {code}: {tail[-1] if tail else 'no output'}"
        named = "unreleased and held to nothing: " + ", ".join(unreleased)
        if unreleased and not reason.endswith(named):
            # The total line names them; a replay.py that says it another way
            # still has them named here.
            reason += f"; {len(unreleased)} {named}"
        status = "passed" if code == "0" else "failed"
        if status == "passed" and replayed is None:
            status = "failed"
            reason = ("the replay's report gives no count of what it held to a golden (a replay.py from before "
                      f"0.5.6 writes none); {reason}")
        elif status == "passed" and replayed == 0:
            status = "failed"
            reason = f"no recording was held to a golden, so the bank checked nothing; {reason}"
        return self.add(Step("bank", status, reason, seconds,
                             {"code": code, "tail": tail, "replayed": replayed, "unreleased": unreleased}))

    # -------------------------------------------------------------- audit

    def audit(self) -> Step:
        """pip-audit over the runner's venv (BVP2 stage 2), run from a venv of
        its own, ~/emet-ci/audit-venv. Never a failure: the step passes when
        it audited, with findings or none, and is `not run` when it could
        not, and `audit.status` says which.

        pip-audit comes from the CI lock's hashed pins (ledger L21), written
        to the audit's venv as requirements.txt and installed with
        `--require-hashes`. The venv is made again when the pins change:
        `installed` holds their sha256."""
        start = time.monotonic()
        try:
            requirements, pin = audit_requirements(AUDIT_LOCK.read_text(encoding="utf-8"))
        except (OSError, RunnerError) as exc:
            why = f"no hashed pin of {AUDIT_TOOL} to install: {exc}"
            self.audit_result = {"status": "not run", "reason": why, "tool": AUDIT_TOOL, "findings": []}
            return self.add(Step("audit", "not run", why, time.monotonic() - start,
                                 {"lock": relative(AUDIT_LOCK, REPO)}))
        digest = hashlib.sha256(requirements.encode("utf-8")).hexdigest()
        body = f"""AV="$CI/{AUDIT_VENV}"
if [ ! -x "$AV/bin/pip-audit" ] || [ "$(cat "$AV/installed" 2>/dev/null)" != {digest} ]; then
  rm -rf "$AV"
  python3 -m venv "$AV" > "$RUN/audit-install.txt" 2>&1 || {{ echo "state=no-venv"; tail -n 3 "$RUN/audit-install.txt" | sed 's/^/tail=/'; exit 0; }}
  cat > "$AV/requirements.txt" <<'EMET_LOCK'
{requirements}EMET_LOCK
  "$AV/bin/python" -m pip install --disable-pip-version-check -q --require-hashes -r "$AV/requirements.txt" >> "$RUN/audit-install.txt" 2>&1 || {{ echo "state=no-install"; tail -n 3 "$RUN/audit-install.txt" | sed 's/^/tail=/'; exit 0; }}
  echo {digest} > "$AV/installed"
fi
site=$("$PY" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" 2>/dev/null) || {{ echo "state=no-runner-venv"; exit 0; }}
echo "version=$("$AV/bin/pip-audit" --version 2>&1 | head -n 1)"
"$AV/bin/pip-audit" --path "$site" --skip-editable -f json --progress-spinner off --desc off -o "$RUN/audit.json" > "$RUN/audit.txt" 2>&1
echo "code=$?"
tail -n 3 "$RUN/audit.txt" | sed 's/^/tail=/'
"$AV/bin/python" - "$RUN/audit.json" <<'EMET_PY'
{AUDIT_REPORT_PY}EMET_PY
"""
        done = self.remote.run(self.script("audit", body), timeout=900)
        values = done.values()
        self.audit_result, reason = audit_record(values, done.value("code"), pin)
        status = "passed" if self.audit_result["status"] == "ran" else "not run"
        details = {"code": done.value("code"), "tail": values.get("tail", []),
                   "skipped": values.get("skipped", []), "findings": len(self.audit_result["findings"]),
                   "pin": pin, "lock_sha256": digest}
        return self.add(Step("audit", status, reason, time.monotonic() - start, details))

    # ------------------------------------------------------------ budgets

    def null_manifests(self, step: str, folder: str, targets: list[tuple[str, dict[str, Any]]],
                       extra: str = "") -> tuple[Result, str, str]:
        """Write the runner's manifests with the null sink on the body, under
        `folder`, and hold each to the null sink and the allowed models
        before anything runs. Returns the script's result, the body id and
        the carried state's path; raises RunnerError on any breach."""
        for _, models in targets:
            check_models(models)
        manifest = remote_path(self.args.manifest, self.home, self.tree)
        pairs = " ".join(f'"$F"/{shlex.quote(name)} {shlex.quote(json.dumps(models))}' for name, models in targets)
        body = f"""cd "$CI/emet" || exit 3
F={shlex.quote(folder)}
rm -rf "$F" && mkdir -p "$F/state" "$F/dev-state" && : > "$F/empty-keys.env"
{extra}"$PY" - {shlex.quote(manifest)} {pairs} <<'EMET_PY'
{NULL_MANIFEST_PY}EMET_PY
echo "manifest_code=$?"
"""
        done = self.remote.run(self.script(step, body), timeout=300)
        if done.value("missing"):
            return done, "", ""
        if done.value("manifest_code") != "0" or not done.value("body"):
            why = done.value("problem") or f"exit {done.value('manifest_code') or done.code}"
            raise RunnerError(f"the runner's manifests with the null sink could not be written: {why}")
        written = [json.loads(line) for line in done.values().get("written", [])]
        if len(written) != len(targets):
            raise RunnerError(f"{len(written)} of {len(targets)} manifests with the null sink were written")
        for entry in written:
            if entry.get("sink") != "null":
                raise RunnerError(f"{entry.get('path')} names the sink {entry.get('sink')!r}, not null: "
                                  "nothing that opens a sound card runs here")
            check_models(entry.get("models"))
        named, read = self.body.get("manifest_sha256"), done.value("manifest_hash")
        if named is not None and read != named:
            raise RunnerError(f"the body's manifest changed during the run: its fingerprint is {named[:12]}, "
                              f"and this step read {read[:12] or 'one with no hash'}")
        body_id, state = done.value("body"), done.value("state")
        if not re.fullmatch(r"[a-z0-9_]+", body_id) or not state.startswith("/"):
            raise RunnerError(f"the body id {body_id!r} or its state path {state!r} is not usable")
        return done, body_id, state

    def budgets(self) -> Step:
        """The measures of the plan template's budget table (RP6, CI8): the
        bank's ten minutes as fast as the loop goes, one breath spoken by
        Piper into the null sink, and a quiet minute at real time, each
        `--repeat` times, through the engine's own `emet-listen` with
        `SessionStats` read by BUDGET_PY. Each measure is held to the last
        release's value on this body when there is one."""
        start = time.monotonic()
        repeat = self.args.repeat
        listen = json.loads(json.dumps(MODELS))
        speak = json.loads(json.dumps(MODELS))
        speak["tts"] = dict(PIPER_TTS)
        wav = remote_path(self.args.budget_wav, self.home, self.tree)
        spoken = remote_path(self.args.budget_speak_wav, self.home, self.tree)
        bank = remote_path(self.args.bank, self.home, self.tree)
        folder = f"{self.run_dir}/budget"
        extra = (f'for w in {shlex.quote(wav)} {shlex.quote(spoken)}; do [ -f "$w" ] || {{ echo "missing=$w is not '
                 'on the body"; exit 0; }; done\n'
                 '"$PY" -c \'import json, sys; print("bank_version=" + str(json.load(open(sys.argv[1])).get("version")))\' '
                 f'{shlex.quote(bank)}/bank.json 2>/dev/null\n')
        try:
            prepared, body_id, state = self.null_manifests(
                "budget-prepare", folder, [("listen.yaml", listen), ("speak.yaml", speak)], extra)
        except RunnerError as exc:
            return self.add(Step("budgets", "failed", str(exc), time.monotonic() - start))
        if prepared.value("missing"):
            return self.add(Step("budgets", "not run", prepared.value("missing"), time.monotonic() - start))
        soul = shlex.quote(remote_path(self.args.soul, self.home, self.tree))
        tail = '--keys "$B/empty-keys.env" --state "$B/state"'
        body = f"""cd "$CI/emet" || exit 3
B={shlex.quote(folder)}
STATE={shlex.quote(state)}
fresh() {{ rm -rf "$B/state"; mkdir -p "$B/state"; if [ -f "$STATE" ]; then cp "$STATE" "$B/state/{body_id}.json"; fi; }}
cat > "$B/driver.py" <<'EMET_PY'
{BUDGET_PY}EMET_PY
cat > "$B/feed.py" <<'EMET_PY'
{FEEDER_PY}EMET_PY
PIPE="$CI/tmp/budget-$$.pipe"
for i in $(seq 1 {repeat}); do
  fresh
  timeout -s INT -k 30 {REPLAY_LIMIT_S} "$PY" "$B/driver.py" "$(date +%s%N)" "$B/frames-$i.json" "$B/listen.yaml" {soul} --replay {shlex.quote(wav)} --transcribe --stats {tail} > "$B/frames-$i.out" 2>&1
  echo "ran=frames-$i $?"
  fresh
  timeout -s INT -k 30 {REPLAY_LIMIT_S} "$PY" "$B/driver.py" "$(date +%s%N)" "$B/speak-$i.json" "$B/speak.yaml" {soul} --replay {shlex.quote(spoken)} --speak --stats {tail} > "$B/speak-$i.out" 2>&1
  echo "ran=speak-$i $?"
  fresh
  rm -f "$PIPE" && mkfifo "$PIPE"
  "$PY" "$B/feed.py" "$PIPE" quiet {IDLE_S} . > "$B/idle-feed-$i.txt" 2>&1 &
  feed=$!
  timeout -s INT -k 30 {IDLE_S + 120} "$PY" "$B/driver.py" "$(date +%s%N)" "$B/idle-$i.json" "$B/listen.yaml" {soul} --replay "$PIPE" --stats {tail} > "$B/idle-$i.out" 2>&1
  echo "ran=idle-$i $?"
  kill "$feed" 2>/dev/null; wait "$feed" 2>/dev/null; rm -f "$PIPE"
done
for f in "$B"/frames-*.json "$B"/speak-*.json "$B"/idle-*.json; do
  [ -f "$f" ] && echo "result=$(basename "$f" .json) $(tr -d '\\n' < "$f")"
done
"""
        done = self.remote.run(self.script("budget-run", body), timeout=repeat * (2 * REPLAY_LIMIT_S + IDLE_S + 300))
        results: dict[str, dict[str, Any]] = {}
        for line in done.values().get("result", []):
            name, _, text = line.partition(" ")
            try:
                results[name] = json.loads(text)
            except json.JSONDecodeError:
                results[name] = {"exit": None}
        ran = dict(line.split(" ", 1) for line in done.values().get("ran", []) if " " in line)
        broken = sorted(f"{name} exit {code}" for name, code in ran.items() if code != "0")
        self.budget_values = budget_values(results)
        step = self.add(Step("budgets", "passed", "", time.monotonic() - start,
                             {"repeat": repeat, "bank_version": prepared.value("bank_version") or None,
                              "runs": ran, "wav": wav, "speak_wav": spoken}))
        self.judge_budgets(step, broken)
        return step

    def judge_budgets(self, step: Step, broken: list[str] | None = None) -> None:
        """Hold each measure to the last release's and set the step's status:
        failed when a measure is out of bounds or a run of the budget's
        failed; a measure with no earlier value never fails."""
        if broken is None:
            broken = step.details.get("broken", [])
        step.details["broken"] = broken
        rows = budget_rows(self.budget_values, board_text(self.preflight), top=self.top, release=self.args.release,
                           host=self.args.host, machine=machine_of({"preflight": self.preflight}))
        self.budget_result = rows
        out = [r for r in rows if r["within"] is False]
        judged = [r for r in rows if r["within"] is not None]
        fresh = [r["measure"] for r in rows if r["within"] is None and isinstance(r["value"], (int, float))]
        missing = [r["measure"] for r in rows if r["value"] is None]
        parts = [f"{len(judged)} judged against the last release, {len(judged) - len(out)} within"]
        if out:
            # A row is out of bounds only against a last value, so each names
            # it; a last value of 0 has no percentage, and any rise from it
            # is out of bounds.
            parts.append("out of bounds: " + "; ".join(
                f"{r['measure']} {shown(r['value'], r['unit'])} against {shown(r['last'], r['unit'])}, "
                + (f"{'+' if r['change_pct'] >= 0 else ''}{r['change_pct']}% over the bound of {r['bound_pct']:g}%"
                   if r["change_pct"] is not None else "a rise from 0, which no bound allows")
                for r in out))
        if fresh:
            parts.append(f"{len(fresh)} with no earlier value: " + ", ".join(fresh))
        if missing:
            parts.append("not measured: " + ", ".join(missing))
        if broken:
            parts.append("runs that failed: " + ", ".join(broken))
        step.status = "failed" if out or broken else "passed"
        step.reason = "; ".join(parts)

    # --------------------------------------------------------------- soak

    def soak(self) -> Step:
        """The soak (RP6 stage 1, BVP7 stage 2): the bank's recordings fed at
        real time through a pipe to one `emet-listen --replay --transcribe
        --stats` for `--soak` minutes, with the null sink, the mock
        transcriber and no microphone, sampled each minute by a script on the
        body that runs detached; then each recording once under `python -X
        dev`, untimed. The laptop asks each minute whether it has ended."""
        minutes = self.args.soak
        start = time.monotonic()
        refusal = self.check_busy()
        if refusal is not None:
            return self.add(Step("soak", "refused", refusal))
        folder = f"{self.run_dir}/soak"
        wavs = remote_path(self.args.soak_wavs, self.home, self.tree)
        soul = remote_path(self.args.soul, self.home, self.tree)
        meta = {"release": self.args.release, "stamp": self.stamp, "minutes": str(minutes), "token": self.token,
                "started": self.clock().isoformat(timespec="seconds")}
        record = json.dumps({"source": self.source, "body": self.body, **meta}, sort_keys=True)
        params = "".join(f"{key}={shlex.quote(value)}\n" for key, value in (
            ("SOAK", folder), ("CI", self.ci), ("PY", f"{self.ci}/.venv/bin/python"),
            ("LISTEN_BIN", f"{self.ci}/.venv/bin/emet-listen"), ("SOUL", soul), ("WAVS", wavs),
            ("SOAK_S", str(minutes * 60)), ("OVERRUN_S", str(SOAK_OVERRUN_S)), ("DEV_S", str(DEV_PASS_S)),
            ("SAMPLE_S", str(SAMPLE_S)), ("STAMP", self.stamp), ("TOKEN", self.token)))
        extra = (f'W={shlex.quote(wavs)}\n'
                 'n=$(ls -1 "$W"/*.wav 2>/dev/null | wc -l); echo "wavs=$n"\n'
                 '[ "$n" -gt 0 ] || { echo "missing=no recording in $W"; exit 0; }\n')
        try:
            prepared, body_id, state = self.null_manifests(
                "soak-prepare", folder, [("soak.yaml", json.loads(json.dumps(MODELS)))], extra)
        except RunnerError as exc:
            return self.add(Step("soak", "failed", str(exc), time.monotonic() - start))
        if prepared.value("missing"):
            return self.add(Step("soak", "not run", prepared.value("missing"), time.monotonic() - start))
        meta_lines = " ".join(shlex.quote(f"{key}={value}") for key, value in meta.items())
        body = f"""S={shlex.quote(folder)}
STATE={shlex.quote(state)}
if [ -f "$STATE" ]; then cp "$STATE" "$S/state/{body_id}.json"; cp "$STATE" "$S/dev-state/{body_id}.json"; fi
cat > "$S/feed.py" <<'EMET_PY'
{FEEDER_PY}EMET_PY
cat > "$S/soak.sh" <<'EMET_SOAK'
{params}{SOAK_SH}EMET_SOAK
printf '%s\\n' {meta_lines} > "$S/meta.txt"
printf '%s\\n' {shlex.quote(record)} > "$S/record.json"
printf '%s\\n' "$S" > "$CI/soak.current"
nohup setsid bash "$S/soak.sh" > "$S/soak.log" 2>&1 < /dev/null &
echo "started=$!"
"""
        done = self.remote.run(self.script("soak-start", body), timeout=120)
        if not done.value("started"):
            return self.add(Step("soak", "failed", f"the soak did not start: exit {done.code}",
                                 time.monotonic() - start))
        self.soak_dir = folder
        ended = self.wait_for_soak(folder, minutes)
        if ended is not None:
            return self.add(Step("soak", "not run", ended, time.monotonic() - start, {"dir": folder}))
        step = self.add(self.read_soak(folder, minutes, time.monotonic() - start))
        self.soak_dir = ""
        # The soak freed the lock when it ended, if it was still this run's.
        # It is the last step on the body, so only the collect, which needs
        # no lock, comes after; a lock someone took meanwhile is named.
        self.locked = False
        taken = self.take_lock()
        if taken is not None:
            step.reason += f"; the run did not take ~/emet-ci/lock again after the soak: {taken}"
            step.details["lock"] = taken
        return step

    def wait_for_soak(self, folder: str, minutes: int) -> str | None:
        """Ask the body each minute until the soak has ended. None once it
        has; otherwise why the laptop stopped waiting, the soak going on."""
        q = shlex.quote(folder)
        poll = (f'S={q}; [ -f "$S/done" ] && echo "done=yes"; if [ -f "$S/pid" ] && kill -0 "$(cat "$S/pid")" '
                '2>/dev/null; then echo "alive=yes"; fi\n')
        deadline = self.clock() + dt.timedelta(seconds=minutes * 60 + DEV_PASS_S + 600)
        lost: dt.datetime | None = None
        after = "`run.py --soak-collect` brings it home when it ends"
        while True:
            self.sleep(POLL_S)
            try:
                said: Result | None = self.remote.run(marked("soak-poll", poll), timeout=60)
            except Unreachable:
                said = None
            # A poll with no answer, ssh's timeout among them, says nothing
            # of the soak: only a poll that ran says it ended.
            if said is None or said.code != 0:
                lost = lost or self.clock()
                if (self.clock() - lost).total_seconds() > LOST_S:
                    return f"the body stopped answering while the soak ran; the soak goes on there, and {after}"
                continue
            lost = None
            if said.value("done") == "yes" or said.value("alive") != "yes":
                return None
            if self.clock() > deadline:
                return f"the soak had not ended by {deadline:%Y-%m-%d %H:%M}; it goes on, and {after}"

    def read_soak(self, folder: str, minutes: int, seconds: float) -> Step:
        """Bring the soak's files' text home and summarize them."""
        fence = f"=== {self.token} "
        names = {"status": "status", "samples.txt": "samples", "dev-samples.txt": "dev_samples",
                 "pcm-before.txt": "pcm_before", "pcm-after.txt": "pcm_after", "audio.txt": "audio"}
        body = (f"S={shlex.quote(folder)}\n"
                + "".join(f'echo "{fence}{key}"; cat "$S/{name}" 2>/dev/null\n' for name, key in names.items())
                + f'echo "{fence}listen"; tail -n 80 "$S/listen.out" 2>/dev/null\n'
                + f'echo "{fence}dev_err"; head -n 2000 "$S/dev.err" 2>/dev/null\n')
        done = self.remote.run(marked("soak-read", body), timeout=300)
        files: dict[str, str] = {}
        for chunk in done.out.split(fence)[1:]:
            key, _, text = chunk.partition("\n")
            files[key.strip()] = text
        self.soak_result = summary = summarize_soak(minutes, files)
        if self.budget_result is not None and summary.get("rss_end_mb") is not None:
            self.budget_values["Resident memory at the end"] = [summary["rss_end_mb"]]
            budgets = next((s for s in self.steps if s.name == "budgets"), None)
            if budgets is not None:
                self.judge_budgets(budgets)
        if summary["flat"]:
            # A flat soak's listener read its whole length (summarize_soak).
            heard = footer_numbers(files.get("listen", "")).get("audio_s", 0.0)
            reason = (f"flat over {minutes} minute(s), {heard / 60:.1f} of audio read: resident memory "
                      f"{summary['rss_start_mb']} to "
                      f"{summary['rss_end_mb']} MB ({summary['rss_growth_mb_per_h']} MB an hour), open files "
                      f"{summary['open_files']['first']}, threads {summary['threads']['first']}, "
                      f"{summary['frames']} frames and none over {FRAME_BUDGET_MS:g} ms; "
                      f"{summary['dev_warnings']} warning(s) under -X dev")
        else:
            reason = "not flat: " + "; ".join(summary["reasons"])
        return Step("soak", "passed" if summary["flat"] else "failed", reason, seconds, {"dir": folder})

    def soak_collect(self) -> int:
        """`--soak-collect`: bring home a soak that outlived the laptop's run,
        once it has ended, with its summary in a run.json of its own."""
        find = (r"""echo "home=$HOME"
s=$(cat "$CI/soak.current" 2>/dev/null || true)
echo "soak=$s"
[ -n "$s" ] || exit 0
[ -f "$s/done" ] && echo "done=yes"
if [ -f "$s/pid" ] && kill -0 "$(cat "$s/pid")" 2>/dev/null; then echo "alive=yes"; fi
cat "$s/meta.txt" 2>/dev/null
echo "record=$(cat "$s/record.json" 2>/dev/null)"
""")
        try:
            found = self.remote.run(marked("soak-find", find), timeout=60)
        except Unreachable as exc:
            return self.not_done(f"the body did not answer over SSH ({exc})")
        self.home = found.value("home")
        folder = found.value("soak")
        if not folder:
            return self.not_done("no soak has been started on the body")
        if found.value("alive") == "yes" and found.value("done") != "yes":
            return self.not_done(f"the soak in {folder}, started {found.value('started')} for "
                                 f"{found.value('minutes')} minute(s), is still running")
        if found.value("release") != self.args.release:
            return self.not_done(f"the soak in {folder} was started for {found.value('release') or 'no release'}; "
                                 f"collect it with --release {found.value('release')}")
        try:
            saved = json.loads(found.value("record") or "{}")
        except json.JSONDecodeError:
            saved = {}
        self.source = saved.get("source") or {}
        self.body = {**self.body, **(saved.get("body") or {})}
        minutes = int(found.value("minutes") or 0)
        try:
            step = self.add(self.read_soak(folder, minutes, 0.0))
            out = self.out_dir = self.output_dir()
            out.mkdir(parents=True, exist_ok=True)
            kept, problems = self.fetch_tar("soak-files", folder, out / "soak")
            token = found.value("token")
            if token:
                self.remote.run(marked("soak-unlock", f'if grep -qx "token {token}" "$CI/lock" 2>/dev/null; then '
                                                      'rm -f "$CI/lock"; echo "lock=released"; fi\n'), timeout=60)
        except Unreachable as exc:
            return self.not_done(f"the body stopped answering over SSH ({exc})")
        self.add(Step("collect", "failed" if problems else "passed",
                      "; ".join(problems) or f"{len(kept)} file(s) to {out / 'soak'}", 0.0, {"files": kept}))
        self.verdict = (f"soak collected: {'flat' if self.soak_result and self.soak_result['flat'] else 'not flat'}, "
                        f"{len(kept)} file(s)")
        return 0 if step.status == "passed" and not problems else 1

    # ------------------------------------------------------------ netcut

    def netcut(self) -> Step:
        """The network cut (P12, D3): the root-owned emet-netcut through
        `sudo -n`, detached, its output in a file (the 0.4 row). It checks
        that the body reached port 443 before, did not during, and did after,
        and that SSH answered all the while. `not run` when the sudo rule is
        not installed.

        Two scripts go to the body. The first only looks: the installed
        script's owner, mode and hash, the synced tree's hash, whether the
        sudo rule is there, and port 443 before. The laptop judges what it
        printed, and only when every check holds does it send the second,
        which asks sudo to run the script, and runs it only while the owner,
        the mode and the hash are still the ones judged. So sudo never runs
        a script that is not root's, that others may write, or that differs
        from the tree's.

        The step shows the cut itself: no emet-listen or emet-talk runs
        under it. Every model stage of the runner is a mock, so an engine run
        under the cut would have no vendor path to lose; that run waits for a
        step with one, and no roadmap row names it yet."""
        seconds = self.args.netcut
        start = time.monotonic()
        if not self.locked:
            return self.add(Step("netcut", "refused", "the runner's lock is not held"))
        refusal = self.check_busy()
        if refusal is not None:
            return self.add(Step("netcut", "refused", refusal))
        look = f"""cd "$CI/emet" || exit 3
bin={shlex.quote(NETCUT_BIN)}
[ -e "$bin" ] || {{ echo "state=absent"; exit 0; }}
echo "owner=$(stat -c '%U %a' "$bin")"
echo "installed=$(sha256sum < "$bin" | cut -c1-64)"
[ -f tools/body/netcut.sh ] && echo "tree=$(sha256sum < tools/body/netcut.sh | cut -c1-64)"
sudo -n -l "$bin" {seconds} > /dev/null 2>&1 || {{ echo "state=no-rule"; exit 0; }}
echo "before=$({PROBE})"
"""
        done = self.remote.run(self.script("netcut", look), timeout=120)
        state, owner = done.value("state"), done.value("owner").split()
        details: dict[str, Any] = {"seconds": seconds, "owner": " ".join(owner)}
        if state in ("absent", "no-rule"):
            why = "the sudo rule is not installed" + (f": {NETCUT_BIN} is not there either" if state == "absent"
                                                      else "")
            return self.add(Step("netcut", "not run", why + " (tools/body/README.md, the network cut)",
                                 time.monotonic() - start, details))
        if len(owner) != 2 or owner[0] != "root" or not re.fullmatch(r"[0-7]{3,4}", owner[1]) \
                or int(owner[1], 8) & 0o022:
            return self.add(Step("netcut", "not run", f"{NETCUT_BIN} is {' '.join(owner) or 'unreadable'}: it must "
                                 "be root's and writable by root alone before the runner asks sudo to run it",
                                 time.monotonic() - start, details))
        installed = done.value("installed")
        if not HEX64.fullmatch(installed) or installed != done.value("tree"):
            return self.add(Step("netcut", "not run", f"{NETCUT_BIN} is not the synced tree's tools/body/netcut.sh; "
                                 "install it again with the README's block", time.monotonic() - start, details))
        details["before"] = done.value("before")
        if details["before"] != "open":
            return self.add(Step("netcut", "not run", "the body reached nothing on port 443 before the cut, so the "
                                 "cut could not be seen", time.monotonic() - start, details))
        go = f"""bin={shlex.quote(NETCUT_BIN)}
[ "$(stat -c '%U %a' "$bin")" = {shlex.quote(' '.join(owner))} ] && [ "$(sha256sum < "$bin" | cut -c1-64)" = {installed} ] || {{ echo "state=changed"; exit 0; }}
nohup setsid sudo -n "$bin" {seconds} > "$RUN/netcut.txt" 2>&1 < /dev/null &
echo "started=$!"
sleep 3
echo "during=$({PROBE})"
"""
        cut = time.monotonic()
        begun = self.remote.run(self.script("netcut-start", go), timeout=120)
        if begun.value("state") == "changed":
            return self.add(Step("netcut", "not run", f"{NETCUT_BIN} changed between the runner's look and the "
                                 "start, so sudo was asked nothing", time.monotonic() - start, details))
        if not begun.value("started"):
            return self.add(Step("netcut", "failed", f"the cut did not start: exit {begun.code}",
                                 time.monotonic() - start, details))
        details["during"] = begun.value("during")
        self.sleep(max(0.0, seconds / 2 - (time.monotonic() - cut)))
        try:
            alive = self.remote.run(marked("netcut-alive", 'echo "alive=yes"\n'), timeout=60).value("alive")
        except Unreachable:
            alive = ""
        details["ssh_during"] = alive == "yes"
        self.sleep(max(0.0, seconds + 6 - (time.monotonic() - cut)))
        after = self.remote.run(self.script("netcut-after", f'echo "after=$({PROBE})"\n'
                                                            'sed "s/^/said=/" "$RUN/netcut.txt" 2>/dev/null\n'),
                                timeout=60)
        said = after.values().get("said", [])
        details["after"], details["said"] = after.value("after"), said
        problems = []
        if details["during"] != "shut":
            problems.append("port 443 was still reached during the cut")
        if not details["ssh_during"]:
            problems.append("SSH did not answer during the cut")
        if details["after"] != "open":
            problems.append("port 443 was not reached again after the cut")
        if not any(line.startswith("cut:") for line in said):
            problems.append("emet-netcut did not say it cut")
        if not any(line.startswith("restored:") for line in said):
            problems.append("emet-netcut did not say it restored")
        if problems:
            return self.add(Step("netcut", "failed", "; ".join(problems), time.monotonic() - start, details))
        return self.add(Step("netcut", "passed", f"port 443 cut for {seconds} s with SSH up, and restored",
                             time.monotonic() - start, details))

    # ------------------------------------------------- speaker and microphone

    def hardware_plan(self) -> list[tuple[str, str, list[str], float]]:
        """(step, what, command, bound in seconds) for each hardware step."""
        bin_dir = f"{self.ci}/.venv/bin"
        body = f"{self.run_dir}/body.yaml"
        soul = remote_path(self.args.soul, self.home, self.tree)
        wav = remote_path(self.args.speaker_wav, self.home, self.tree)
        keys = f"{self.run_dir}/empty-keys.env"
        state = f"{self.run_dir}/state"
        tail = ["--keys", keys, "--state", state]
        listen, talk = f"{bin_dir}/emet-listen", f"{bin_dir}/emet-talk"

        # `--foreground` sends SIGINT, and SIGKILL 30 s later, to the program
        # alone, which stays in the terminal's foreground group as it does
        # when typed at a prompt. Without it a timeout signals its own
        # process group too. GNU's then waits the 30 s, but uutils' timeout
        # 0.10.0, which Ubuntu 26.04 and so GitHub's ubuntu-latest ship as
        # `timeout`, sends SIGKILL a moment after the SIGINT: the program
        # exits 137 before its end line (tried under WSL with uutils' own
        # build and read in its timeout.rs, 2026-11-26). The only children
        # either program starts, git at boot and vcgencmd under --stats, end
        # by their own 2 s timeouts.
        def bounded(seconds: int, *command: str) -> list[str]:
            return ["timeout", "--foreground", "-s", "INT", "-k", "30", "--preserve-status", str(seconds), *command]

        def events(step: str) -> list[str]:
            # An engine from before 0.5.7 has no --events; prepare_body asks.
            return ["--events", f"{self.run_dir}/{step}{EVENTS_SUFFIX}"] if self.events_ok else []

        plan = []
        if "speaker" in self.wanted:
            plan += [
                ("speaker-echo", "--replay --echo, on the speaker",
                 bounded(REPLAY_LIMIT_S, listen, body, soul, "--replay", wav, "--echo", "--stats", *tail),
                 REPLAY_LIMIT_S),
                ("speaker-speak", "--replay --speak, on the speaker",
                 bounded(REPLAY_LIMIT_S, listen, body, soul, "--replay", wav, "--speak", "--stats", *tail),
                 REPLAY_LIMIT_S),
                ("speaker-talk", "emet-talk --replay, on the speaker",
                 bounded(REPLAY_LIMIT_S, talk, body, soul, "--replay", wav, "--stats", *events("speaker-talk"),
                         *tail),
                 REPLAY_LIMIT_S),
            ]
        if "live" in self.wanted:
            plan += [
                ("live-plain", "a plain run, 60 s on the microphone",
                 bounded(PLAIN_S + BOOT_S, listen, body, soul, *tail), PLAIN_S + BOOT_S),
                # The one row of FLAG_ROWS no other step shows. Nobody speaks,
                # so it boots, listens and is stopped; SIGINT is Ctrl-C to it,
                # and it prints "stopped. N exchange(s)." on the way down.
                ("live-talk", "emet-talk with no flags, 60 s on the microphone",
                 bounded(PLAIN_S + BOOT_S, talk, body, soul, *tail), PLAIN_S + BOOT_S),
                ("live-stats", "--stats, ten minutes on the microphone",
                 bounded(STATS_S + BOOT_S, listen, body, soul, "--stats", *events("live-stats"), *tail),
                 STATS_S + BOOT_S),
            ]
        return plan

    def models(self) -> dict[str, Any]:
        models = json.loads(json.dumps(MODELS))
        if self.args.voice == "piper":
            models["tts"] = dict(PIPER_TTS)
        return models

    def prepare_body(self) -> tuple[str, str]:
        """Write the runner's manifest on the body: the maintainer's manifest
        with every model stage taken over. Returns the body id and the
        carried state's path.

        The manifest is hashed again as it is read here, before its models
        block is replaced, and a hash other than the run's fingerprint stops
        the hardware steps: the body they would run on is no longer the one
        `run.json` names."""
        models = self.models()
        check_models(models)
        manifest = remote_path(self.args.manifest, self.home, self.tree)
        body = f"""cd "$CI/emet" || exit 3
: > "$RUN/empty-keys.env"
mkdir -p "$RUN/state"
"$PY" - {shlex.quote(manifest)} "$RUN/body.yaml" {shlex.quote(json.dumps(models))} <<'EMET_PY'
{CANONICAL_PY}
import sys
from pathlib import Path
try:
    import yaml
    from emet_engine.state import state_path
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    print("manifest_hash=" + canonical_sha256(doc))
    doc["models"] = json.loads(sys.argv[3])
    target.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    print("body=" + written["body"]["id"])
    print("state=" + str(state_path(written["body"]["id"])))
    print("models=" + json.dumps(written.get("models"), sort_keys=True))
    import importlib.util
    print("events=" + ("yes" if importlib.util.find_spec("emet_engine.events") else "no"))
except Exception as exc:
    print("problem=" + type(exc).__name__)
    sys.exit(3)
EMET_PY
"""
        done = self.remote.run(self.script("prepare", body), timeout=120)
        if done.code != 0 or not done.value("body"):
            # The Python above says only the kind of what stopped it: a YAML
            # parser's message quotes the manifest, and nothing of the
            # manifest leaves the body (the collect leaves the runner's
            # manifests there too, fetch_tar). Without that line it never got
            # as far as an error of its own, and stderr is the shell's.
            why = done.value("problem") or f"exit {done.code}: {done.err.strip()[-200:]}"
            raise RunnerError(f"the runner's manifest could not be written: {why}")
        check_models(json.loads(done.value("models") or "null"))
        body_id, state = done.value("body"), done.value("state")
        if not re.fullmatch(r"[a-z0-9_]+", body_id) or not state.startswith("/"):
            raise RunnerError(f"the body id {body_id!r} or its state path {state!r} is not usable")
        named, read = self.body.get("manifest_sha256"), done.value("manifest_hash")
        if named is not None and read != named:
            raise RunnerError(f"the body's manifest changed during the run: its fingerprint is {named[:12]}, "
                              f"and the hardware steps read {read[:12] or 'one with no hash'}")
        # An engine with no emet_engine.events (before 0.5.7) has no --events
        # either, and its two steps run without one.
        self.events_ok = done.value("events") != "no"
        return body_id, state

    def hardware(self) -> None:
        plan = self.hardware_plan()
        if not plan:
            return
        try:
            body_id, state = self.prepare_body()
        except RunnerError as exc:
            for name, *_ in plan:
                self.add(Step(name, "failed", str(exc)))
            return
        # Again, now that the body has said whether its engine has --events.
        plan = self.hardware_plan()
        for name, what, command, bound in plan:
            refusal = window_refusal(self.clock(), bound + MARGIN_S, self.args.window,
                                     allow_night=self.args.allow_night)
            if refusal is None:
                refusal = self.check_busy()
            if refusal is not None:
                self.add(Step(name, "refused", refusal, details={"what": what}))
                continue
            self.add(self.hardware_step(name, what, command, body_id, state))

    def hardware_step(self, name: str, what: str, command: list[str], body_id: str, state: str) -> Step:
        mark = f"$RUN/.mark-{name}"
        audio = " -o ".join(f"-iname '*{s}'" for s in AUDIO_SUFFIXES)
        # A step given --events must leave its file, with the header at least.
        events = command[command.index("--events") + 1] if "--events" in command[:-1] else ""
        look = f'[ -s {shlex.quote(events)} ] || echo "events=missing"\n' if events else ""
        body = f"""cd "$CI/emet" || exit 3
STATE={shlex.quote(state)}
COPY="$RUN/state/{body_id}.json"
rm -f "$COPY"
if [ -f "$STATE" ]; then echo "state_before=$(sha256sum < "$STATE" | cut -c1-64)"; cp "$STATE" "$COPY"; else echo "state_before=none"; fi
mixer() {{ for c in /proc/asound/card[0-9]*; do [ -d "$c" ] || continue; n=${{c##*card}}; echo "card $n"; amixer -c "$n" scontents 2>&1; done; }}
mixer > "$RUN/{name}-mixer-before.txt"
touch "{mark}"
script -qae "$RUN/{name}.typescript" -c {shlex.quote(shlex.join(command))} < /dev/null > /dev/null 2>&1
echo "code=$?"
mixer > "$RUN/{name}-mixer-after.txt"
cmp -s "$RUN/{name}-mixer-before.txt" "$RUN/{name}-mixer-after.txt" && echo "mixer=same" || echo "mixer=changed"
if [ -f "$STATE" ]; then echo "state_after=$(sha256sum < "$STATE" | cut -c1-64)"; else echo "state_after=none"; fi
find "$CI" -newer "{mark}" -type f \\( {audio} \\) -print 2>/dev/null | sed 's/^/audio=/'
find "$HOME" -maxdepth 1 -newer "{mark}" -type f \\( {audio} \\) -print 2>/dev/null | sed 's/^/audio=/'
sed 's/\\x1b\\[[0-9;?]*[A-Za-z]//g' "$RUN/{name}.typescript" | tr '\\r' '\\n' | grep -E -c '^(stopped|source ended)\\.' | sed 's/^/ended=/'
{look}"""
        start = time.monotonic()
        done = self.remote.run(self.script(name, body), timeout=STATS_S + BOOT_S + 300)
        seconds = time.monotonic() - start
        values = done.values()
        details = {"what": what, "command": shlex.join(command), "code": done.value("code"),
                   "state_before": done.value("state_before"), "state_after": done.value("state_after"),
                   "mixer": done.value("mixer"), "audio_files": values.get("audio", [])}
        problems = []
        if values.get("audio"):
            problems.append("audio was written during the step: " + ", ".join(values["audio"][:3]))
        if done.value("state_before") != done.value("state_after"):
            problems.append("the carried state file changed; the runner should only have read it")
        if done.value("mixer") != "same":
            problems.append("the mixer settings changed during the step")
        if done.value("code") != "0":
            problems.append(f"the command exited {done.value('code') or 'without a code'}")
        if (done.value("ended", "0") or "0") == "0":
            problems.append("the typescript has no end line")
        if done.value("events") == "missing":
            problems.append(f"the events file {Path(events).name} was not written")
        if problems:
            return Step(name, "failed", "; ".join(problems), seconds, details)
        return Step(name, "passed", what, seconds, details)

    # ------------------------------------------------------------- collect

    def output_dir(self) -> Path:
        if self.args.out:
            return Path(self.args.out)
        base = TOP / "releases" / f"v{self.args.release}" / "body"
        day = self.started.strftime("%Y-%m-%d")
        candidate, n = base / day, 2
        while (candidate / "run.json").exists():
            candidate, n = base / f"{day}-{n}", n + 1
        return candidate

    def collect(self) -> Step:
        start = time.monotonic()
        out = self.out_dir = self.output_dir()
        out.mkdir(parents=True, exist_ok=True)
        problems: list[str] = []
        kept: list[str] = []
        if self.home:
            kept, problems = self.fetch_tar("collect", self.run_dir, out)
            for path in self.args.fetch:
                name = self.fetch(path, out, problems)
                if name:
                    kept.append(name)
        # BVP12: each events file a hardware step wrote, read where it
        # landed; the typescripts are still read as text below, as every
        # earlier release's are.
        for step in self.steps:
            if step.name in EVENTS_STEPS and f"{step.name}{EVENTS_SUFFIX}" in kept:
                try:
                    step.details["events"] = events_summary(out / f"{step.name}{EVENTS_SUFFIX}")
                except RunnerError as exc:
                    problems.append(str(exc))
        sitting = self.join_sitting(out, kept, problems) if getattr(self.args, "sheet_rows", None) else None
        logs = sorted(p for p in out.rglob("*") if p.suffix in (".typescript", ".log"))
        checks: dict[str, Any] = {}
        for log in logs:
            checks[log.relative_to(out).as_posix()] = verdict = self.check_log(log)
            if verdict["code"] == 1:
                problems.append(f"body_log_check.py fails {log.name}")
        runs = []
        for where, text in self.history_texts(out):
            runs += logged_runs(text, where)
        self.flags = flag_table(runs)
        # What this run itself exercised: only a sub-step that passed, and only
        # the typescript this run brought home, so a file an earlier run left
        # in a folder named with --out is never counted.
        passed = [s.name for s in self.steps if s.status == "passed" and s.name in HARDWARE_SUBSTEPS]
        self.flags_run = flags_exercised(
            (name, (out / f"{name}.typescript").read_bytes().decode("utf-8", "replace"))
            for name in passed if f"{name}.typescript" in kept)
        details = {"files": sorted(kept), "log_checks": checks, "out": str(out)}
        if sitting is not None:
            details["sitting"] = sitting
        status = "failed" if problems else "passed"
        reason = "; ".join(problems) or f"{len(kept)} file(s) to {out}"
        return self.add(Step("collect", status, reason, time.monotonic() - start, details))

    def join_sitting(self, out: Path, kept: list[str], problems: list[str]) -> dict[str, Any]:
        """`--sheet` (EV4 stage 1): the sitting's ratings joined to its turns'
        timings in sitting.json beside the typescript. The turns are the
        fetched `sitting-*.events.jsonl`'s; the ratings are kept whether or
        not they join. An events file the reader refuses is a problem of the
        collect, as it would be for a hardware step's."""
        files = sorted(n for n in kept if n.startswith("sitting-") and n.endswith(EVENTS_SUFFIX))
        records: list[dict[str, Any]] | None = None
        problem = None
        if not files:
            problem = f"no events file was fetched (--fetch '~/sitting-{self.args.release}{EVENTS_SUFFIX}')"
        elif len(files) > 1:
            problem = f"{len(files)} events files were fetched ({', '.join(files)}), and a sitting has one"
        else:
            try:
                records = turn_records(out / files[0])
            except RunnerError as exc:
                problem = str(exc)
                problems.append(problem)
        doc = sitting_record(self.args.sheet_rows, records, self.args.release, problem)
        (out / "sitting.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return {"file": "sitting.json", "events": files[0] if len(files) == 1 else None, "joined": doc["joined"],
                "rated": sum(1 for t in doc["turns"] if t["wait"] is not None)}

    def fetch_tar(self, step: str, folder: str, out: Path) -> tuple[list[str], list[str]]:
        """A folder of the body's as text files under `out`: the state
        copies, the empty keys file, the pipes, the step markers and the
        runner's manifests stay behind, and an audio file is refused by name.
        The manifests are the maintainer's with the models and the sink
        changed, so nothing of the manifest's content beyond its hash comes
        home."""
        body = (f'mkdir -p {shlex.quote(folder)} && cd {shlex.quote(folder)} && tar -czf - --exclude=state --exclude=dev-state --exclude=".mark-*" '
                '--exclude=empty-keys.env --exclude="*.pipe" --exclude="*.yaml" .\n')
        done = self.remote.run(marked(step, body), timeout=600)
        kept: list[str] = []
        problems: list[str] = []
        if done.code != 0 or not done.raw:
            return kept, [f"the run's files did not come back: exit {done.code}"]
        out.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(done.raw), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                if member.name.lower().endswith(AUDIO_SUFFIXES):
                    problems.append(f"{member.name} is audio and was not copied")
                    continue
                tar.extract(member, out, filter="data")
                kept.append(member.name.removeprefix("./"))
        return kept, problems

    def fetch(self, path: str, out: Path, problems: list[str]) -> str | None:
        """Copy one typescript from the body's home folder, such as the
        sitting's. The name is checked before anything is read: only
        `~/pi-*.log` and `~/sitting-*.log`, so a keys file, an SSH key or a
        recording never reaches releases/."""
        if not FETCHABLE.match(path) or SECRET.search(path):
            problems.append(f"{path} was not fetched: a fetch names ~/pi-*.log or ~/sitting-*.log")
            return None
        target = remote_path(path, self.home, self.tree)
        body = (f'f={shlex.quote(target)}; [ -f "$f" ] || exit 3; '
                f'[ "$(stat -c %s "$f")" -le 20000000 ] || exit 4; cat "$f"\n')
        done = self.remote.run(marked("fetch-file", body), timeout=300)
        if done.code != 0:
            problems.append(f"{path} was not fetched: exit {done.code}")
            return None
        name = Path(path).name
        (out / name).write_bytes(done.raw)
        return name

    def check_log(self, log: Path) -> dict[str, Any]:
        if not self.log_check.is_file():
            return {"code": None, "said": f"not run: {self.log_check} is not there"}
        done = subprocess.run([sys.executable, "-B", str(self.log_check), str(log)], capture_output=True)
        said = done.stdout.decode("utf-8", "replace")
        log.with_name(log.name + ".check.txt").write_text(said, encoding="utf-8")
        last = [line for line in said.splitlines() if line.startswith("exit")]
        return {"code": done.returncode, "said": last[-1] if last else said.strip()[-120:]}

    def history_texts(self, out: Path) -> list[tuple[str, str]]:
        """Every typescript the flag table reads: this run's, the laptop's
        under releases/, and the maintainer's ~/pi-*.log and ~/sitting-*.log
        on the body, read and not copied."""
        seen: set[Path] = set()
        found: list[tuple[str, str]] = []
        paths = sorted(p for p in out.rglob("*") if p.suffix in (".typescript", ".log"))
        if self.history is None:
            releases = TOP / "releases"
            paths += sorted(releases.glob("v*/pi-*.log")) + sorted(releases.glob("v*/sitting*.log"))
            paths += sorted(releases.glob("v*/body/*/*.typescript")) + sorted(releases.glob("v*/body/*/*.log"))
        else:
            paths += list(self.history)
        for path in paths:
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            try:
                where = path.relative_to(TOP).as_posix()
            except ValueError:
                where = path.as_posix()
            found.append((where, path.read_bytes().decode("utf-8", "replace")))
        if self.home and not self.args.no_body_history:
            fence = f"=== {self.token} "
            body = (f'for f in "$HOME"/pi-*.log "$HOME"/sitting-*.log; do [ -f "$f" ] || continue; '
                    f'echo "{fence}$f"; cat "$f"; echo; done\n')
            try:
                done = self.remote.run(marked("history", body), timeout=300)
            except Unreachable:
                return found
            for chunk in done.out.split(fence)[1:]:
                name, _, text = chunk.partition("\n")
                found.append((f"{self.args.host}:~/{Path(name.strip()).name}", text))
        return found

    # ---------------------------------------------------------------- run

    @property
    def wanted(self) -> list[str]:
        """The steps this run takes. `--soak` and `--netcut` add theirs; the
        speaker and the microphone need `--window`."""
        if self.args.collect_only or self.args.soak_collect:
            return []
        asked = {"soak": self.args.soak is not None, "netcut": self.args.netcut is not None}
        if self.args.steps:
            chosen = set(self.args.steps)
        else:
            chosen = {s for s in STEPS if s not in HARDWARE_STEPS or self.args.window is not None}
        return [s for s in STEPS if (s in chosen and s not in ASKED_STEPS) or asked.get(s, False)]

    def run(self) -> int:
        try:
            if self.args.soak_collect:
                return self.soak_collect()
            return self._run()
        finally:
            self.release_lock()
            self.write()

    def _run(self) -> int:
        try:
            refusal = self.run_preflight()
        except Unreachable as exc:
            return self.not_done(f"the body did not answer over SSH ({exc})")
        if refusal:
            self.add(Step("preflight", "refused", refusal, details=self.preflight))
            return self.not_done(refusal)
        self.add(Step("preflight", "passed", self.board_line(), details=self.preflight))
        try:
            refusal = self.take_lock()
            if refusal:
                return self.not_done(refusal)
            if "sync" in self.wanted:
                if self.sync().status != "passed":
                    return self.not_done("the code under test did not reach the body")
            elif not self.args.collect_only:
                self.read_source()
            if "install" in self.wanted and self.install().status != "passed":
                return self.not_done("the code under test could not be installed on the body")
            # After the install, so the runner's venv is there to read it;
            # in every run that got this far, a --collect-only one included.
            self.fingerprint()
            if "audit" in self.wanted:
                self.audit()
            if "suites" in self.wanted:
                self.suites()
            if "checks" in self.wanted:
                self.checks()
            if "bank" in self.wanted:
                self.bank()
            if "budgets" in self.wanted:
                self.budgets()
            for name in HARDWARE_STEPS:
                if name not in self.wanted:
                    reason = ("no --window named: the microphone and the speaker are used only inside a "
                              "window the maintainer allowed") if self.args.window is None else "not asked for"
                    if not self.args.collect_only:
                        self.add(Step(name, "not run", reason))
            self.hardware()
            # After the speaker and the microphone, so a window is spent on
            # them alone; the soak last, since the lock is freed when it ends.
            if "netcut" in self.wanted:
                self.netcut()
            if "soak" in self.wanted:
                self.soak()
            self.collect()
        except Unreachable as exc:
            return self.not_done(f"the body stopped answering over SSH ({exc})")
        failed = [s for s in self.steps if s.status in ("failed", "refused")]
        ran = [s for s in self.steps if s.status == "passed"]
        skipped = [s for s in self.steps if s.status == "not run"]
        self.verdict = (f"body run done: {len(ran)} step(s) passed, {len(failed)} failed or refused, "
                        f"{len(skipped)} not run")
        return 1 if failed else 0

    def not_done(self, why: str) -> int:
        self.verdict = f"body run not done: {why}"
        return 2

    def board_line(self) -> str:
        p = self.preflight
        load = " ".join(p.get("loadavg", "").split()[:3])
        temp = p.get("temp", "").removeprefix("temp=")
        throttled = p.get("throttled", "").removeprefix("throttled=")
        line = f"load {load}; {temp}; throttled {throttled} ({', '.join(p.get('throttled_means', []))})"
        disk = p.get("disk", "").split()
        if len(disk) >= 4 and disk[1].isdigit() and disk[3].isdigit():
            # df -P counts 1024-byte blocks.
            line += f"; {int(disk[3]) / 2**20:.1f} GiB free of {int(disk[1]) / 2**20:.1f}"
        return line

    # -------------------------------------------------------------- report

    def report(self) -> str:
        lines = [f"body run on {self.args.host}, started {self.started:%Y-%m-%d %H:%M:%S} (laptop clock)"]
        if self.preflight:
            lines.append(f"  board    {self.preflight.get('uptime', '')} (the body's clock, "
                         f"{self.preflight.get('date', '')})")
            lines.append(f"           {self.board_line()}")
            if self.preflight.get("emet_variables_unset"):
                lines.append(f"  unset    {' '.join(self.preflight['emet_variables_unset'])}")
        if self.source:
            s = self.source
            what = s.get("kind", "")
            if s.get("head"):
                what += f" at {s['head'][:7]} ({s.get('subject', '')})"
            if "changed" in s:
                what += (f"; against it {len(s['changed'])} changed, {len(s['added'])} added, "
                         f"{len(s['removed'])} removed; {s.get('crlf_converted', 0)} file(s) CRLF to LF")
            lines.append(f"  source   {what}")
        b = self.body
        lines.append(f"  body     {b['manifest']} sha256 {(b['manifest_sha256'] or 'not read')[:12]}; "
                     f"state {b['state'] or 'not found'} sha256 {(b['state_sha256'] or 'not read')[:12]}")
        if b["note"]:
            lines.append(f"           {b['note']}")
        lines.append("")
        lines.append(f"  {'step':<15} {'status':<8} {'s':>6}  detail")
        for step in self.steps:
            lines.append(f"  {step.name:<15} {step.status:<8} {step.seconds:>6.0f}  {step.reason}")
        suites = next((s for s in self.steps if s.name == "suites"), None)
        if suites and suites.details:
            lines.append("")
            lines.append(f"  {'package':<16} {'collected':>9} {'passed':>7} {'skipped':>8} {'failed':>7} "
                         f"{'pytest s':>9} {'wall s':>7}")
            for package, c in suites.details.items():
                lines.append(f"  {package:<16} {c.get('collected', '?'):>9} {c.get('passed', '?'):>7} "
                             f"{c.get('skipped', '?'):>8} {c.get('failed', '?'):>7} "
                             f"{c.get('pytest_s', '?'):>9} {c.get('wall_s', '?'):>7}")
        if self.flags:
            lines.append("")
            lines.append(f"  {'flag':<28} {'last live':<11} {'last replay':<12} log")
            for row in self.flags:
                where = row["where"] or "not in the logs read"
                note = f"  ({row['with']})" if row["with"] else ""
                lines.append(f"  {row['flag']:<28} {row['live'] or '-':<11} {row['replay'] or '-':<12} "
                             f"{where}{note}")
        if self.flags_run:
            lines.append("")
            lines.append(f"  {'flag this run exercised':<28} {'how':<11} step")
            for entry in self.flags_run:
                lines.append(f"  {entry['flag']:<28} {entry['how']:<11} {entry['step']}")
        events = [(s.name, s.details["events"]) for s in self.steps if isinstance(s.details.get("events"), dict)]
        if events:
            lines.append("")
            for name, read in events:
                lines.append(f"  events   {name}: {read['turns']} turn(s), turn gap median "
                             f"{shown(read['gap_ms_median'], 'ms')} ({read['file']})")
        collected = next((s for s in self.steps if s.name == "collect"), None)
        if collected is not None and collected.details.get("sitting"):
            sat = collected.details["sitting"]
            lines.append("")
            lines.append(f"  sitting  {sat['file']}: {sat['rated']} of {len(SHEET_ROWS)} turn(s) rated; {sat['joined']}")
        if self.audit_result is not None:
            a = self.audit_result
            lines.append("")
            lines.append(f"  audit    {a['tool']}, {a['status']}: {a['reason']}")
            for f in a["findings"]:
                lines.append(f"           {f['name']} {f['version']}  {f['id']}  fixed in {f['fix'] or 'no release yet'}")
        if self.budget_result is not None:
            lines.append("")
            lines.append(f"  {'measure':<58} {'this run':>16} {'last':>16} {'change':>8} {'bound':>6}  within")
            for r in self.budget_result:
                value = shown(r["value"], r["unit"])
                last = shown(r["last"], r["unit"]) if r["last"] is not None else "none"
                if r["measure"] == BOARD:
                    lines.append(f"  {r['measure']:<58} {value}")
                    lines.append(f"  {'':<58} last: {last}")
                    continue
                change = "" if r["change_pct"] is None else f"{r['change_pct']:+.1f}%"
                bound = "" if r["bound_pct"] is None else f"{r['bound_pct']:g}%"
                within = {True: "yes", False: "NO", None: "-"}[r["within"]]
                lines.append(f"  {r['measure']:<58} {value:>16} {last:>16} {change:>8} {bound:>6}  {within}")
        if self.soak_result is not None:
            s = self.soak_result
            lines.append("")
            lines.append(f"  soak     {s['minutes']} minute(s), {s['samples']} sample(s), {s['judged']} after the "
                         f"warm-up: {'flat' if s['flat'] else 'NOT FLAT'}")
            lines.append(f"           resident memory {s['rss_start_mb']} to {s['rss_end_mb']} MB, "
                         f"{s['rss_growth_mb_per_h']} MB an hour; open files {s['open_files']}; threads {s['threads']}")
            lines.append(f"           frames {s['frames']}, over {FRAME_BUDGET_MS:g} ms {s['frames_over_80ms']}; "
                         f"throttle flags {', '.join(s['throttle_flags']) or 'none'}; CPU {s['cpu_pct']}% of one core")
            for reason in s["reasons"]:
                lines.append(f"           ! {reason}")
            lines.append("           -X dev: " + ("did not run" if s["dev_warnings"] is None
                                                  else f"{s['dev_warnings']} warning(s)"))
            for warning in s["dev_warning_lines"]:
                lines.append(f"             {warning[:150]}")
        lines.append("")
        lines.append(self.verdict)
        return "\n".join(lines)

    def write(self) -> None:
        text = self.report()
        print(text)
        if self.out_dir is None:
            try:
                self.out_dir = self.output_dir()
                self.out_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        record = {
            "format": FORMAT,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": self.clock().isoformat(timespec="seconds"),
            "host": self.args.host,
            "release": self.args.release,
            "window": [w.isoformat(timespec="minutes") for w in self.args.window] if self.args.window else None,
            "allow_night": self.args.allow_night,
            "preflight": self.preflight,
            "source": self.source,
            "body": self.body,
            "steps": [vars(s) for s in self.steps],
            "flags": self.flags,
            "flags_run": self.flags_run,
        }
        # Each absent until its step ran (contract §3).
        for name, value in (("audit", self.audit_result), ("soak", self.soak_result), ("budgets", self.budget_result)):
            if value is not None:
                record[name] = value
        record["verdict"] = self.verdict
        (self.out_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        (self.out_dir / "report.txt").write_text(text + "\n", encoding="utf-8")


def bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        if not re.fullmatch(r"\d{1,6}", text) or not low <= int(text) <= high:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number from {low} to {high}")
        return int(text)
    return parse


def print_budget_table(args: argparse.Namespace, top: Path = TOP) -> int:
    """`--budget-table`: the table from a run.json, or from the newest of the
    release's runs on the laptop that has budgets."""
    if args.budget_table:
        path = Path(args.budget_table)
    else:
        found = []
        for candidate in (top / "releases" / f"v{args.release}" / "body").glob("*/run.json"):
            try:
                record = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("budgets"):
                found.append((str(record.get("started", "")), candidate))
        if not found:
            print(f"no run.json with budgets under releases/v{args.release}/body/", file=sys.stderr)
            return 1
        path = max(found)[1]
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{path} could not be read: {exc}", file=sys.stderr)
        return 1
    if not isinstance(record, dict) or not record.get("budgets"):
        print(f"{path} holds no budgets", file=sys.stderr)
        return 1
    print(budget_table(record, relative(path.resolve(), top.resolve())))
    return 0


def nightly_report(args: argparse.Namespace, remote: Any, *, top: Path = TOP,
                   now: Callable[[], dt.datetime] = dt.datetime.now) -> int:
    """`--nightly-report`: the body's nightly reports, the newest read home to
    releases/vX.Y.Z/body/nightly/. Exit 0 when the newest passed and is
    younger than NIGHTLY_STALE_H hours, 1 otherwise, 2 when the body did not
    answer."""
    fence = f"=== {uuid.uuid4().hex} "
    body = (f'cd "$HOME"/{shlex.quote(NIGHTLY_DIR.removeprefix("~/"))} 2>/dev/null || exit 0\n'
            'for f in $(ls -1 [0-9]*.txt 2>/dev/null | sort | tail -n 30); do echo "listed=$f"; done\n'
            f'newest=$(ls -1 [0-9]*.txt 2>/dev/null | sort | tail -n 1)\n'
            f'[ -n "$newest" ] && {{ echo "{fence}$newest"; cat "$newest"; }}\n'
            'true\n')
    try:
        done = remote.run(marked("nightly-report", body), timeout=120)
    except Unreachable as exc:
        print(f"nightly report not read: the body did not answer over SSH ({exc})")
        return 2
    head, _, rest = done.out.partition(fence)
    listed = [line.removeprefix("listed=") for line in head.splitlines() if line.startswith("listed=")]
    if not rest:
        print(f"no nightly report on the body: {NIGHTLY_DIR}/ holds none (tools/body/README.md, the nightly)")
        return 1
    name, _, text = rest.partition("\n")
    report = parse_nightly(text)
    out = top / "releases" / f"v{args.release}" / "body" / "nightly"
    out.mkdir(parents=True, exist_ok=True)
    saved = out / Path(name.strip()).name
    saved.write_text(text, encoding="utf-8")
    print(f"nightly reports on the body: {len(listed)}, from {listed[0] if listed else '-'} to "
          f"{listed[-1] if listed else '-'}")
    # The report's lines in nightly.sh's order, and any line this does not
    # know after them, so a new one is never dropped.
    known = ("started", "head", "subject", "installed", "checks", "bank", "pcm_after", "note", "verdict", "reason")
    for key in known + tuple(k for k in report if k not in known + ("format", "ended", "suites")):
        if report.get(key):
            print(f"  {key:<10} {report[key]}")
    for package, said in report["suites"].items():
        print(f"  {'suite':<10} {package}: {said}")
    print(f"  saved      {relative(saved, top)}")
    try:
        started = dt.datetime.fromisoformat(report.get("started", "")).replace(tzinfo=None)
    except ValueError:
        started = None
    if started is None:
        print(f"the newest nightly report, {name.strip()}, has no start time this could read; open it at "
              f"{relative(saved, top)}")
        return 1
    age = (now() - started).total_seconds() / 3600
    if age > NIGHTLY_STALE_H:
        print(f"the newest nightly report started {report['started']}, {age:.0f} h ago, past {NIGHTLY_STALE_H} h: "
              "the schedule has stopped or the body was off, and `ssh emet-pi crontab -l` shows the schedule")
        return 1
    return 0 if report.get("verdict") == "passed" else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run.py", description="Run the body's checks over SSH.")
    parser.add_argument("--release", required=True, help="X.Y.Z, the release this run is evidence for")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--tree", action="store_true", help="send the laptop's working tree")
    what.add_argument("--commit", metavar="REF", help="a pushed commit, branch or tag of the public repository")
    what.add_argument("--collect-only", action="store_true",
                      help="run nothing; fetch, check and tabulate (the release sitting's typescript)")
    what.add_argument("--soak-collect", action="store_true",
                      help="bring home a soak that outlived the laptop's run, once it has ended, in a run.json "
                           "of its own")
    what.add_argument("--nightly-report", action="store_true",
                      help="read the body's nightly reports home (~/emet-ci/nightly/) and say whether the last "
                           "one passed and how old it is")
    what.add_argument("--budget-table", nargs="?", const="", metavar="RUN_JSON",
                      help="print the budget table of the plan's section 3 from a run.json (the newest with budgets "
                           "under releases/vX.Y.Z/body/ when none is named); the body is not reached")
    parser.add_argument("--soak", type=bounded_int(SOAK_MIN, SOAK_MAX), metavar="MINUTES",
                        help=f"add the soak: the bank's recordings at real time through one emet-listen for "
                             f"MINUTES, {SOAK_MIN} to {SOAK_MAX} ({SOAK_MINUTES['patch']} for a patch, "
                             f"{SOAK_MINUTES['minor']} for a minor), detached on the body, then once under -X dev; "
                             "replay-only, no window needed")
    parser.add_argument("--soak-wavs", default=SOAK_WAVS, metavar="DIR",
                        help="the folder on the body whose 16 kHz mono wavs the soak loops")
    parser.add_argument("--repeat", type=bounded_int(1, REPEAT_MAX), default=1, metavar="N",
                        help="run each budget measure N times; with N of 2 or more the run records their "
                             "spread, which widens the bound past 25%% when it is larger")
    parser.add_argument("--budget-wav", default=BUDGET_WAV, help="the recording for the per-frame measures")
    parser.add_argument("--budget-speak-wav", default=SPEAK_WAV, help="the recording for the first chunk")
    parser.add_argument("--netcut", type=bounded_int(NETCUT_MIN_S, NETCUT_MAX_S), metavar="SECONDS",
                        help="add the network cut: port 443 dropped for SECONDS with SSH up, through the "
                             "root-owned emet-netcut and its one sudo rule")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--steps", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                        help=f"a comma list from {','.join(STEPS)}; the default is every step but soak and "
                             "netcut, which --soak and --netcut add, and speaker and live, which --window adds")
    parser.add_argument("--window", nargs=2, metavar=("START", "END"), type=parse_moment,
                        help="the window the maintainer allowed for the microphone and the speaker, "
                             "YYYY-MM-DDTHH:MM in the laptop's local time")
    parser.add_argument("--allow-night", action="store_true",
                        help="let a hardware step run between 22:00 and 08:00, on the maintainer's separate yes")
    parser.add_argument("--voice", choices=("mock", "piper"), default="mock")
    parser.add_argument("--manifest", default=MANIFEST, help="the body's manifest on the Pi")
    parser.add_argument("--soul", default=SOUL, help="the soul, relative to the synced tree")
    parser.add_argument("--speaker-wav", default=SPEAKER_WAV, help="the replay for the speaker steps")
    parser.add_argument("--bank", default=BANK)
    parser.add_argument("--bank-arg", action="append", default=[], help="passed on to replay.py")
    parser.add_argument("--fetch", action="append", default=[], metavar="~/FILE",
                        help="copy this typescript or events file from the body into the output: "
                             "~/sitting-X.Y.Z.log and ~/sitting-X.Y.Z.events.jsonl, or another ~/sitting-* or "
                             "~/pi-* file ending .log or .events.jsonl; nothing else is fetched")
    parser.add_argument("--sheet", metavar="PATH",
                        help="with --collect-only: the release sitting's filled sheet, a Markdown file holding "
                             "tools/body/sitting.md's table; its ratings and the fetched events file's turns go "
                             "into sitting.json")
    parser.add_argument("--no-body-history", action="store_true",
                        help="read only the laptop's logs for the flag table")
    parser.add_argument("--out", help="the output directory; the default is releases/vX.Y.Z/body/<date>/")
    args = parser.parse_args(argv)
    if not VERSION.match(args.release):
        parser.error(f"--release {args.release!r} is not X.Y.Z")
    if args.commit and (not REF.match(args.commit) or ".." in args.commit):
        parser.error(f"--commit {args.commit!r} is not a plain commit, branch or tag name")
    if args.commit and re.match(r"(?:refs/)?pulls?/", args.commit):
        parser.error(f"--commit {args.commit!r} names a pull request; the body runs the repository's "
                     "own branches and tags")
    if args.steps and set(args.steps) - set(STEPS):
        parser.error(f"--steps names {sorted(set(args.steps) - set(STEPS))}; the steps are {', '.join(STEPS)}")
    for step, flag in (("soak", "soak"), ("netcut", "netcut")):
        if args.steps and step in args.steps and getattr(args, flag) is None:
            parser.error(f"--steps {step} needs --{flag}, which says how long")
    if args.window is not None:
        args.window = tuple(args.window)
        if args.window[1] <= args.window[0]:
            parser.error("--window ends before it starts")
    # The sheet is read here, so one the runner cannot read stops the run
    # before the body is reached.
    args.sheet_rows = None
    if args.sheet is not None:
        if not args.collect_only:
            parser.error("--sheet goes with --collect-only, which brings the sitting's files home")
        try:
            args.sheet_rows = read_sheet(Path(args.sheet).read_text(encoding="utf-8"))
        except OSError as exc:
            parser.error(f"--sheet {args.sheet} could not be read: {exc}")
        except SheetError as exc:
            parser.error(f"--sheet {args.sheet}: {exc}")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except AttributeError:
        pass
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.budget_table is not None:
        return print_budget_table(args)
    if args.nightly_report:
        return nightly_report(args, Remote(args.host))
    return BodyRun(args, remote=Remote(args.host), laptop=Laptop(REPO)).run()


if __name__ == "__main__":
    raise SystemExit(main())
