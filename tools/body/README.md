# The reference body as a test host

The files for the release's evidence from the body, a Raspberry Pi 5 with a
USB microphone and a USB speaker:

| File | What it is | Who runs it |
|---|---|---|
| `run.py` | runs the suites, the checks, the voice bank's replays, the audit, the budgets, the speaker and microphone steps, the network cut and the soak on the body, over SSH from the laptop | the agent, or the maintainer by hand |
| `nightly.sh` | runs master's suites, checks and bank replays on the body each night, by cron | the body's cron, installed by the lead |
| `netcut.sh` | drops port 443 for a fixed time with SSH up, installed root-owned as `/usr/local/sbin/emet-netcut` | `run.py --netcut`, through one sudo rule |
| `sitting.md` | guides the release sitting, about 15 minutes and ten turns of `emet-talk` judged against a printed sheet | the maintainer, once a release, at the robot |

## What the runner needs

- Key-based SSH to the body under the alias `emet-pi`:
  `ssh -o BatchMode=yes emet-pi true` must succeed with no prompt.
- Python 3.12 or newer on the laptop, standard library only. The body
  needs bash, `python3 -m venv`, `git`, `script` (util-linux) and
  `timeout` (coreutils).
- The runner owns `~/emet-ci/` on the body: its tree (`emet/`), its venv
  (`.venv/`), the audit's venv (`audit-venv/`), its bare clone
  (`repo.git/`), its lock, its temporary files and pip cache (`tmp/`,
  `cache/`), one folder of outputs per run (`runs/`), the nightly's script
  and reports (`bin/`, `nightly/`) and the pointer to the last soak
  (`soak.current`). Nothing else on the body is written, and the
  maintainer's checkout is never touched.
- **The install line** is RELEASE_PROCESS.md §3's with the provider extras
  the maintainer's venv on the body holds:
  `emet-providers[dev,deepgram,anthropic,openai,piper]`, so that the
  runner's providers suite skips what the maintainer's skips, the 4 live
  vendor tests, where it skipped 9 in 0.5.5. No vendor is reached all the
  same: every program the runner starts gets `--keys` naming an empty file
  it made, and every command on the body unsets each `EMET_` variable first.
- **Piper is GPL-3.0.** It is installed on the body through the `piper`
  extra, as an owner would install it, and imported from there; Emet never
  bundles it (`CITATIONS.md`). The budget's first chunk on the local path
  is the one measure that needs it.

## Running it

From the repository's root on the laptop:

```sh
python tools/body/run.py --release 0.5.5 --tree
python tools/body/run.py --release 0.5.5 --commit 1a2b3c4
python tools/body/run.py --release 0.5.5 --tree --window 2026-10-28T14:00 2026-10-28T15:00
python tools/body/run.py --release 0.5.5 --collect-only --fetch '~/sitting-0.5.5.log'
python tools/body/run.py --release 0.5.6 --commit 1a2b3c4 --soak 60 --repeat 5
python tools/body/run.py --release 0.5.6 --soak-collect
python tools/body/run.py --release 0.5.6 --nightly-report
python tools/body/run.py --release 0.5.6 --budget-table
```

- `--tree` sends the laptop's working tree, the files
  `git ls-files -co --exclude-standard` lists, as git would commit them
  (CRLF back to LF where `core.autocrlf` made it). The run's record lists
  each file that differs from HEAD.
- `--commit REF` fetches a pushed commit, branch or tag anonymously from
  the public repository into the runner's own clone. Only the repository's
  branches and tags are fetched, and a commit none of them holds is
  refused: a pull request is anyone's code, and the body would run it as
  the maintainer's user, who can read the keys file.
- `--fetch` copies a typescript from the body's home folder into the
  output: `~/sitting-*.log` or `~/pi-*.log`, and nothing else, so a keys
  file or an SSH key never reaches `releases/`.
- `--steps` picks among `sync`, `install`, `audit`, `suites`, `checks`,
  `bank`, `budgets`, `speaker`, `live`, `netcut` and `soak`, the order they
  run in. Without it every step runs except `speaker`, `live`, `netcut` and
  `soak`. `--soak MINUTES` adds the soak and `--netcut SECONDS` the network
  cut, with or without `--steps`; `--window` adds the speaker and the
  microphone.
- `--repeat N` (1 to 10, default 1) runs each budget measure N times and
  records their spread. `--budget-wav` and `--budget-speak-wav` name the
  budget's recordings on the body, `--soak-wavs` the folder the soak loops.
- `--soak-collect`, `--nightly-report` and `--budget-table [RUN_JSON]` are
  modes of their own, in place of `--tree`, `--commit` or `--collect-only`.
- The output goes to `releases/vX.Y.Z/body/<date>/` beside the repository
  (`--out` names another place): each step's text, the typescripts, the
  output of `body_log_check.py` on each, `run.json` and `report.txt`.

Exit codes: 0 when every step that ran passed; 1 when a step failed or was
refused; 2 when the body run was not done, because the body did not answer,
it was in use, or the code could not be synced or installed.

## What `run.json` holds

Its `format` is `emet-body-run/2`. Every field of format 1 is still there:
`started`, `ended`, `host`, `release`, `window`, `allow_night`, `preflight`,
`source`, `steps`, `flags` and `verdict`. Format 2 adds two, and the merge
gate, `tools/release_audit.py`, reads them:

| Field | What it is |
|---|---|
| `body` | The body's fingerprint: `manifest`, the path given with `--manifest`; `manifest_sha256`; `state`, the carried state file's path; `state_sha256`; and `note`, empty when both were read |
| `flags_run` | A list of `{"flag", "how", "step"}`: each body flag this run itself exercised, `live` or `replay`, and the step that showed it. Empty when no speaker or live step passed |

Three more are there only when their step ran, and the merge gate reads none
of them; a failed `soak` or `budgets` step is a failed step all the same:

| Field | What it is |
|---|---|
| `audit` | `{"status": "ran" or "not run", "reason", "tool": "pip-audit 2.10.1", "findings": [{"name", "version", "id", "fix"}]}` |
| `soak` | The soak's summary: `minutes`, `samples`, `judged` (those after the warm-up), `rss_start_mb`, `rss_end_mb`, `rss_growth_mb_per_h`, `open_files` and `threads` (each `first`, `last`, `max`), `frames`, `frames_over_80ms`, `per_frame_ms`, `cpu_pct`, `throttle_flags`, `devices_opened`, `pcm_before`, `pcm_after`, `dev_warnings`, `dev_warning_lines`, `listen_exit`, `dev_exit`, `flat` and `reasons` |
| `budgets` | One object for each row of the plan template's budget table: `measure`, `value`, `unit`, `last`, `last_file`, `change_pct`, `bound_pct`, `within` (true, false or null), and beside them `spread_pct` and `values`, the repeats |

The steps, in `steps`, each `{"name", "status", "reason", "seconds",
"details"}` with a status of `passed`, `failed`, `refused` or `not run`:

| Step | When it is there |
|---|---|
| `preflight` | every run the body answered |
| `sync`, `install`, `audit`, `suites`, `checks`, `bank`, `budgets` | unless `--steps` left it out |
| `speaker`, `live` | as `not run`, when the run had no window or `--steps` left it out |
| `speaker-echo`, `speaker-speak`, `speaker-talk` | in place of `speaker`, when the run took that step |
| `live-plain`, `live-talk`, `live-stats` | in place of `live`, when the run took that step |
| `netcut` | with `--netcut SECONDS` |
| `soak` | with `--soak MINUTES` |
| `collect` | every run that was done |

A run that was not done still writes `run.json`, with `body` unread and its
note saying so.

## The body's fingerprint

Two runs are evidence about the same robot only when its settings were the
same, so every run that reaches the body names it by two hashes. The idea
is EasyNavigation's roadmap item RD032, a configuration fingerprint
(Apache-2.0, `CITATIONS.md`).

- **The manifest** is the maintainer's own, `--manifest`, by default
  `~/pi-speakerphone.yaml`, hashed on the body as it is read and before the
  runner writes its copy with the mock models. It is hashed in canonical
  form: the YAML is parsed, each mapping's keys become text and are sorted,
  a sequence keeps its order, and the result is written as JSON with `,`
  and `:` and no spaces, in ASCII. `manifest_sha256` is the sha256 of those
  bytes. A comment, the order of keys, the quoting and the layout change
  nothing. A value changes it, and so does the order of a sequence and a
  number YAML reads as another type (`-6` for `-6.0`).
- **The state file** is the one the engine would load for the manifest's
  `body.id`, `~/.local/state/emet/pi_speakerphone.json` on the reference
  body, hashed byte for byte. The engine writes it, so it has one layout.
  Each speaker and microphone step records the same hash as `state_before`.
- **Only the hashes and the state file's path come home.** The path holds
  the `body.id` and nothing else of the manifest. A manifest that does not
  parse is reported by the kind of its error, since a parser's message
  quotes the file. The runner's own manifests, the maintainer's with the
  models and the sink changed, stay on the body: the collect leaves every
  `.yaml` behind.
- **It fails nothing.** A file that is not there, or a Python on the body
  with no PyYAML, gives a null hash and a sentence in `note`. The runner's
  venv reads the files when it exists, and the body's `python3` otherwise.
- **Three checks use it.** The speaker and microphone steps, the budgets
  and the soak hash the manifest again as they read it. A hash other than
  the fingerprint means the file changed during the run: every speaker and
  microphone step fails, and so does the budgets or soak step that read it.

## The robot stays the maintainer's

- **In use.** The runner refuses to start while an `emet-listen`,
  `emet-talk`, `arecord`, `aplay` or the voice bank's `record.py` runs on
  the body, or while any sound card's PCM is open, whoever opened it
  (`/proc/asound/card*/pcm*/sub*/status` reads other than `closed`). It
  checks again before each speaker or microphone step.
- **The lock.** It holds `~/emet-ci/lock` for the length of a run and
  removes it only when the token inside is its own. To keep the runner
  away, create the file: `touch ~/emet-ci/lock`. Remove it to let the
  runner back. A run that lost the body partway leaves its lock behind, and
  the next run refuses and prints it; `rm ~/emet-ci/lock` on the body
  clears it.
- **Windows.** The speaker and microphone steps run only inside a window
  the maintainer allowed in a reply beforehand, given as `--window START
  END` in the laptop's local time. A step that would start before the
  window or end after it is refused, and so is any step with a minute
  between 22:00 and 08:00 unless `--allow-night` is given on a separate
  yes.
- **How long a window.** A step is held to the window by its bound, the
  longest it may run, and keeps 30 s more before the window closes. The
  six bounds in the table below come to 1,680 s, so a window must hold 28
  and a half minutes from the moment the first of them starts, and the few
  seconds the runner spends between steps. A replay ends when its file
  does, so the steps use less than their bounds. In a run of every step
  the sync, the install, the audit, the suites, the checks, the bank and
  the budgets come first. The five of those that 0.5.5 had took 164 s on
  2026-11-25, 26 s of it a reinstall
  (`releases/v0.5.5/body/2026-11-25-3/run.json`). The audit's first run
  installs pip-audit in a venv of its own, and each `--repeat` of the
  budgets is bounded at 780 s: two replays of up to 300 s each and a quiet
  minute fed at real time, with 120 s more to boot and stop. Neither has
  been timed on the body yet. The network cut and the soak run after the
  speaker and the microphone and take none of the window. A window sized
  for the six bounds alone suits `--steps speaker,live`, which runs the two
  on the tree last synced, after a run of the rest.
- **No audio kept or sent.** On those steps the body's model stages are all
  mocks (the voice may be local Piper with `--voice piper`), `--keys` names
  an empty file, and every EMET_ variable is unset before each command. A
  step fails if any audio file appeared under `~/emet-ci` or in the home
  folder while it ran, if the carried state file changed (the run uses a
  copy), or if the mixer's settings changed.

## What each speaker and microphone step runs

| Step | Command on the body | Bound |
|---|---|---|
| `speaker-echo` | `emet-listen --replay ~/one-breath.wav --echo --stats` | 300 s |
| `speaker-speak` | `emet-listen --replay ~/one-breath.wav --speak --stats` | 300 s |
| `speaker-talk` | `emet-talk --replay ~/one-breath.wav --stats` | 300 s |
| `live-plain` | `emet-listen`, stopped with SIGINT after 60 s of listening | 80 s |
| `live-talk` | `emet-talk` with no flags, stopped with SIGINT after 60 s of listening | 80 s |
| `live-stats` | `emet-listen --stats`, stopped with SIGINT after ten minutes | 620 s |

Each runs under `script -qae` into its own typescript, with the runner's
manifest (the maintainer's `~/pi-speakerphone.yaml` with a mock for every
model stage), the reference soul from the synced tree, `--keys` naming an
empty file, and `--state` naming a fresh copy of the carried state.

The three live steps run with nobody speaking, and each bound is the 60 s
or the ten minutes of listening and 20 s to boot. `timeout -s INT` stops
them, which is Ctrl-C to both programs: `emet-listen` prints `stopped.
heard it N time(s).` and `emet-talk` prints `stopped. N exchange(s).`, and
a step with no such line in its typescript fails. `live-talk` is there
because `emet-talk` with no flags is a row of the flag table that no other
step shows: `speaker-talk` reads a file. If the microphone hears its name
by mistake during it, the mock models answer and the speaker makes a short
sound, inside the same window.

## The bank step

`bank` runs `tools/bank/replay.py` over `~/voicebank/` and passes when
replay.py exits 0 and held at least one recording to a golden. Its
`details` hold `replayed`, the number of recordings replayed and held to a
golden, and `unreleased`, the ids of recordings named and held to nothing.

- **A missing golden still fails.** A recording with no golden for the
  body's platform makes replay.py exit 1, as it has since 0.5.5.
- **A sitting still being recorded is named.** The one case replay.py holds
  to nothing is a recording made under a plan, read from its manifest's
  `made_with`, when no recording of that plan has a golden on any platform.
  The Pi's bank has such recordings while bank v1's sittings go on. Each is
  still replayed, printed as `UNRELEASED`, and named in the step's reason
  and in `details.unreleased`. Once any recording of the plan has a golden,
  on any platform, every recording of the plan is held to one, and a
  recording without one fails.
- **A step that held nothing fails.** So does one whose report gives no
  count, as a replay.py from before 0.5.6 would write it.

## The audit

`audit` runs pip-audit 2.10.1 (Apache-2.0, verified 2026-11-26 on PyPI) over
the runner's venv after the install. pip-audit lives in a venv of its own,
`~/emet-ci/audit-venv/`, made once and again when the pinned version
changes, and reads the runner's packages with `--path`; installed in the
runner's venv, its own dependencies would join the venv the suites run in
and be audited as the robot's. The four Emet packages are editable installs,
which `--skip-editable` leaves out; each is named in the step's
`details.skipped`.

- **It never fails the run.** With findings or none the step passes and
  `audit.status` is `ran`; with no network, or a pip-audit that would not
  install or finish, the step is `not run` and the reason says which. The
  merge gate does not read it.
- Each finding is in `audit.findings` and in `report.txt` with the version
  that fixes it. A finding goes in the next plan's ledger.

## The budgets

`budgets` measures the rows of the plan template's budget table
(`planning/templates/plan.md` §3) on the body, `--repeat` times:

| Measure | How |
|---|---|
| Boot to first frame | the time from starting `emet-listen --replay <the bank's ten minutes> --transcribe --stats` to its first frame |
| First chunk at the null sink after the final, local path | the engine's `voice first` mean: one breath, the mock transcriber and language model, Piper speaking into the null sink |
| Per-frame mean, Per-frame 99th percentile, Frames over 80 ms | the ten minutes, as fast as the loop goes |
| Resident memory after boot | at the first frame |
| Resident memory at the end | the soak's last sample; only a run with `--soak` has it |
| Idle CPU | the engine's share of one core over a minute of quiet noise fed at real time through a pipe |
| Board lines (temperature, throttle, load) | the preflight's, recorded and never judged |

Each run is the engine's own `emet-listen`, as `cli.main` in a small driver
(`BUDGET_PY` in `run.py`) that wraps two methods of `SessionStats`,
`record_frame` and `report`, to read what the engine already measures: the
footer prints no 99th percentile and no time of the first frame. Both
wrapped methods call the engine's own; a wrapped method the engine stopped
calling leaves its measure null. Every run has the null sink, `--keys`
naming an empty file and a fresh copy of the carried state.

- **The last value** is read from the newest passing `run.json` of the
  highest earlier release under `releases/` that has the measure, on the
  same body: the same `host` and the same machine (the last word of the
  preflight's `kernel`). A run of another body, of another platform, or one
  with a failed step is never the last value. 0.5.5's runs recorded the
  board lines alone, in their preflight, and those are the table's first
  row.
- **The bound** is the larger of 25% and the measure's spread, its repeats'
  range over their median: this run's when `--repeat` is 2 or more, and
  otherwise the newest spread recorded on this body.
- **Out of bounds fails the step**: a measure that rose by more than its
  bound. A fall never does. A measure with no earlier value is recorded with
  `within: null` and never fails. A run of the budget that did not exit 0
  fails the step.
- `--budget-table` prints the Markdown table the plan's §3 and the pull
  request take, from a `run.json`, or the newest of the release's with
  budgets, and the line under it names the platform, the commit, the bank
  version and the repeats. It reaches nothing on the body.

## The soak

`--soak MINUTES` adds the soak (RP6): 60 for a patch and 480 for a minor
(RELEASE_PROCESS.md §1.1), from 10 to 2160. It is replay-only and needs no
window, overnight included (E9 (a)). The 72 hours before a major are out of
its reach: one listener reads one wav stream, and a wav holds at most 37.3
hours of 16 kHz mono, so a longer soak needs the listener started again on
a second stream, which the soak does not do yet. It is first needed before
the 1.0 freeze, in 0.10 (ROADMAP.md, RP6 stage 3). The soak is
the run's last step on the body, after the speaker and the microphone, so a
`--soak` beside `--window` takes none of the window.

- **One listener for the whole length.** The bank's 16 kHz mono recordings
  in `~/voicebank/audio/` are looped at real time through a named pipe into
  one `emet-listen --replay <pipe> --transcribe --stats`, with the runner's
  manifest (every model stage a mock, the sink `null`), `--keys` naming an
  empty file and a copy of the carried state. The pipe's wav header names
  the largest size a wav holds, so an hour of audio needs no file of that
  length on disk. A process per recording would start fresh each time, and
  its memory could not grow.
- **Detached.** The soak's script runs on the body under `nohup setsid`, so
  a dropped SSH session or a laptop asleep does not end it (the 0.4 row of
  RELEASE_PROCESS.md §10). It samples the listener once a minute: resident
  memory, open files, threads, CPU time, its open files under `/dev/snd`,
  the PCMs it owns, and the board's temperature and throttle flags. A
  listener still running 3 minutes past the soak's length is hung: the
  script stops it as Ctrl-C would, kills it two samples later if it is
  still there, and the soak is not flat.
- **No audio device.** The PCM check of the preflight runs before the first
  listener (an open card refuses the soak) and after the -X dev pass (an
  open card then is a failure), and each sample of either pass counts the
  listener's own sound devices; one is a failure. An audio file written
  under `~/emet-ci` or in the home folder while the soak ran is a failure
  too.
- **The -X dev pass.** After the timed minutes each recording is replayed
  once under `python -X dev`, as fast as it is read and for at most 30
  minutes. Its warnings (the warnings module's lines, asyncio's slow
  callbacks and coroutines never awaited) are counted in
  `soak.dev_warnings` and listed in the report. They judge nothing; a pass
  that did not run or did not exit 0 does, and its count is then null.
- **Flat** is Stage 4's exit, each threshold a constant in `run.py`, judged
  on the samples after a five-minute warm-up: resident memory growing under
  1 MB an hour (`RSS_GROWTH_MB_PER_H`, the least-squares slope), open files
  and threads constant, no frame over 80 ms (`FRAME_BUDGET_MS`, held to the
  footer's longest frame, and to its count of frames over when the
  engine's own budget line names 80 ms), no throttle flag raised during the
  soak (a "now" bit in any sample, or a "has occurred" bit the first sample
  did not have), and at least three samples. It is held over the soak's
  whole length: a listener that read less audio than the soak's length less
  a minute, a memory or thread count read in too few samples, a listener,
  feeder or script that did not exit 0, and a signal that stopped any of
  them each make it not flat. A soak that is not flat fails its step, with
  each reason in `soak.reasons`.
- **The lock.** The soak holds `~/emet-ci/lock` with its run's token until
  it ends, and frees it then. A run that starts meanwhile refuses and names
  the soak; the nightly gives way on the soak's listener or on its lock.
  When the laptop's run ends first, the lock stays with the soak. When the
  laptop is still there, its run takes the lock again for the collect and
  names in the soak's reason anyone who took it first.
- **The laptop waits** and asks once a minute. When the body stops
  answering for 15 minutes, or the soak outlasts its length by more than
  40 minutes, the step is `not run` and the soak goes on:
  `run.py --release X.Y.Z --soak-collect` brings it home once it has ended,
  in a `run.json` of its own, and frees a lock the soak left.

## The nightly

`nightly.sh` (P11) runs on the body each night from master: it fetches the
public repository anonymously into the runner's bare clone, swaps master
into `~/emet-ci/emet`, installs with the runner's line when a
`pyproject.toml` changed, and runs the four suites, the checks and the
bank's replays with the runner's venv. It is replay-only and opens no audio
device, gives way exactly as the runner's preflight does (a program that
means the robot is in use, an open PCM, a held lock), takes the lock with a
token of its own before it touches anything, needs no GitHub credential and
keeps the newest 30 reports, `~/emet-ci/nightly/YYYY-MM-DD.txt`. A nightly
that gave way writes `YYYY-MM-DD.gave-way.txt` and leaves the day's report
and folder as they were, so a second start on the same day, by hand or by
cron, never erases a run.

- A windowed `--steps speaker,live` after a night runs on master, the tree
  the nightly left; `source.kind` is then `nightly`. Sync first.
- Until 0.5.6 merges, master's bank step fails on the Pi's bank (ledger
  L4), so each nightly reports `failed` until then.
- `run.py --release X.Y.Z --nightly-report` lists the reports, prints the
  newest, saves it under `releases/vX.Y.Z/body/nightly/`, and exits 1 when
  it failed, gave way, has no start time it can read, or is older than 48
  hours, so a schedule that stopped is seen.

Installed by the lead on the body, from the laptop, after a run that synced
a tree holding `nightly.sh` (the synced copy has LF line endings):

```sh
ssh emet-pi 'mkdir -p ~/emet-ci/bin ~/emet-ci/nightly && install -m 0755 ~/emet-ci/emet/tools/body/nightly.sh ~/emet-ci/bin/nightly.sh && bash -n ~/emet-ci/bin/nightly.sh'
ssh emet-pi '( crontab -l 2>/dev/null | grep -v "emet-ci/bin/nightly.sh"; echo "17 3 * * * /bin/bash \$HOME/emet-ci/bin/nightly.sh > \$HOME/emet-ci/nightly/cron.log 2>&1" ) | crontab - && crontab -l'
```

The crontab line it writes, at 03:17 each night:

```
17 3 * * * /bin/bash $HOME/emet-ci/bin/nightly.sh > $HOME/emet-ci/nightly/cron.log 2>&1
```

## The network cut

`netcut.sh` (P12, D3) drops the body's outbound TCP and UDP to port 443 for
a fixed number of seconds and leaves SSH up. Raspberry Pi OS ships nftables
and no iptables package (the 2026-10-06 trixie image's package list,
verified 2026-11-26), so it uses `nft`, in a table of its own. Its one
argument is a whole number of seconds from 5 to 600 and nothing else. The
cut always ends: a trap deletes the table on every way out, and the rule's
one element has a timeout of the length plus 5 s, which the kernel enforces
when the script is killed outright. It prints a line starting `cut:` and
one starting `restored:`.

`run.py --netcut SECONDS` (30 to 600) runs it through `sudo -n`, detached,
with its output in a file. It checks that port 443 was reached before, not
during and again after, and that a new SSH session answered during the cut.
It asks sudo to run nothing but a root-owned script, writable by root alone,
that is byte for byte the synced tree's `netcut.sh`: a first script only
looks at the installed file, the sudo rule and port 443, the laptop judges
what it printed, and only then does a second script ask sudo to run it,
and only while its owner, mode and hash are still the ones judged. Until
the rule is installed, which is the Pi's state today, the step is `not run:
the sudo rule is not installed`.

The step shows the cut itself, with no `emet-listen` or `emet-talk`
running under it. Every model stage of the runner is a mock, so an engine
run under the cut would have no vendor path to lose; that run waits for a
step with one, and no roadmap row names it yet.

The maintainer's block, on the body, after a run that synced a tree holding
`netcut.sh`. The rule is checked with `visudo -cf` before it is put in
place, since a broken file in `/etc/sudoers.d/` stops sudo:

```sh
sudo install -o root -g root -m 0755 ~/emet-ci/emet/tools/body/netcut.sh /usr/local/sbin/emet-netcut
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/emet-netcut ^[0-9]+$\n' "$USER" > /tmp/emet-netcut && sudo visudo -cf /tmp/emet-netcut && sudo install -o root -g root -m 0440 /tmp/emet-netcut /etc/sudoers.d/emet-netcut && rm /tmp/emet-netcut
sudo visudo -c && sudo -n -l /usr/local/sbin/emet-netcut 30
```

The rule's argument is a regular expression, which sudo reads in a rule
from version 1.9.10 (sudo's NEWS, "What's new in Sudo 1.9.10", verified
2026-11-26); the 2026-10-06 trixie image's package list has sudo 1.9.16p2,
a version not yet read on the Pi itself. So the rule allows a number and
nothing else; the script checks the number's bounds itself.

## The flag table

The collect step prints, for each body flag the release process lists, the
date it last ran on the body, live and from a file, read from the
typescripts: this run's, every `releases/v*/pi-*.log` and
`releases/v*/body/*/` log on the laptop, and the maintainer's `~/pi-*.log`
and `~/sitting-*.log` on the body, which are read and never copied. A run
counts when it booted, reached its end line and carries `script`'s date.
`--speak` counts as `--reply` and `--transcribe`, as emet-listen's help
says it implies them, and the ten live minutes of `--stats` count only from
a footer that read at least 600 s of audio.

That table is all history. `flags_run` in `run.json` is this run alone:
the same counting, over the typescripts this run brought home from its own
speaker and live steps, and only from a step that passed. One run inside a
window shows all eight rows:

| Flag | How | Step |
|---|---|---|
| a plain run | live | `live-plain` |
| `--replay` | replay | `speaker-echo` |
| `--stats`, live, ten minutes | live | `live-stats` |
| `--echo` | replay | `speaker-echo` |
| `--transcribe`, `--reply`, `--speak` | replay | `speaker-speak` |
| `emet-talk` with no flags | live | `live-talk` |

The bank's replays add nothing to it, and neither does a typescript of the
maintainer's or one fetched with `--fetch`.
