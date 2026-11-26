# The reference body as a test host

Two files for the release's evidence from the body, a Raspberry Pi 5 with a
USB microphone and a USB speaker:

| File | What it is | Who runs it |
|---|---|---|
| `run.py` | the body runner: the suites, the checks, the voice bank's replays, and the speaker and microphone steps, started over SSH from the laptop | the agent, or the maintainer by hand |
| `sitting.md` | the release sitting: about 15 minutes, ten turns of `emet-talk`, judged against a printed sheet | the maintainer, once a release, at the robot |

## What the runner needs

- Key-based SSH to the body under the alias `emet-pi`:
  `ssh -o BatchMode=yes emet-pi true` must succeed with no prompt.
- Python 3.12 or newer on the laptop, standard library only. The body
  needs bash, `python3 -m venv`, `git`, `script` (util-linux) and
  `timeout` (coreutils).
- The runner owns `~/emet-ci/` on the body: its tree (`emet/`), its venv
  (`.venv/`), its bare clone (`repo.git/`), its lock, its temporary files
  and pip cache (`tmp/`, `cache/`), and one folder of outputs per run
  (`runs/`). Nothing else on the body is written, and the maintainer's
  checkout is never touched.

## Running it

From the repository's root on the laptop:

```sh
python tools/body/run.py --release 0.5.5 --tree
python tools/body/run.py --release 0.5.5 --commit 1a2b3c4
python tools/body/run.py --release 0.5.5 --tree --window 2026-10-28T14:00 2026-10-28T15:00
python tools/body/run.py --release 0.5.5 --collect-only --fetch '~/sitting-0.5.5.log'
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
- `--steps` picks among `sync`, `install`, `suites`, `checks`, `bank`,
  `speaker` and `live`. Without it every step runs except `speaker` and
  `live`, which `--window` adds.
- The output goes to `releases/vX.Y.Z/body/<date>/` beside the repository
  (`--out` names another place): each step's text, the typescripts, the
  output of `body_log_check.py` on each, `run.json` and `report.txt`.

Exit codes: 0 when every step that ran passed; 1 when a step failed or was
refused; 2 when the body run was not done, because the body did not answer,
it was in use, or the code could not be synced or installed.

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
| `live-stats` | `emet-listen --stats`, stopped with SIGINT after ten minutes | 620 s |

Each runs under `script -qae` into its own typescript, with the runner's
manifest (the maintainer's `~/pi-speakerphone.yaml` with a mock for every
model stage), the reference soul from the synced tree, `--keys` naming an
empty file, and `--state` naming a fresh copy of the carried state.

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
