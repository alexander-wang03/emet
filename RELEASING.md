# Releasing

A checklist for every version. Every merge to master is one, a patch, a
minor or a major, so this runs for every pull request; items 1 and 2 are the
roadmap-row work of a minor or a major.

Each item belongs to a stage of the maintainer's release playbook, which
lives outside the repository with each release's plan: scope and research
(item 3), the contract (item 12), the build (items 1, 4 and 5), the review
(item 10), the body's evidence (item 6), the API and UX gate (items 7 and
11), the candidate (item 2), and the ship (items 8 and 9).

Each item is here because something went wrong without it, and the note under
each says what. A checklist of plausible-sounding good practice gets skipped;
one where every line has a scar does not.

Run the mechanical half first. It fails loudly. One command runs every CI
job this machine can run, the three checks below among them, and names each
job it could not run and why:

```sh
python tools/ci.py --all
```

`--log FILE` keeps a copy of everything it prints, and `--list` names each
job, its check on GitHub, and whether it blocks. The three checks alone, for
a quick look:

```sh
python tools/check_layering.py . && python tools/release_check.py . && python tools/check_style.py .
```

`release_check.py` holds the version in the four `pyproject.toml` files and
the READMEs, and holds every requirement on an Emet package, in the
dependencies or an extra, to `>=` the release's own version.
`emet-providers` declared `emet-sdk>=0.1` until 0.5.5, a floor that another
project's `emet-sdk`, published on PyPI on 2026-10-30, meets. It also
refuses a `### Security` section of a changelog entry with no line in it,
and a Security line that names no advisory id. The id is a GitHub Security
Advisory's (GHSA), a CVE's, or a PYSEC id from the Python Packaging
Advisory Database.

On the day of the merge, check the pull request and merge it in one
command, so the merge cannot run when the check refuses:

```sh
python tools/release_audit.py --pr <N> --merge
```

It refuses a version not above master's, a draft, a red or pending check, a
head other than this checkout's, uncommitted changes in the checkout, a
title other than the changelog entry's, an entry that cites another pull
request, an AI attribution line, a version already tagged, and an entry not
dated today; for the date it prints the command that fixes it.

It also refuses what a person used to check by hand, read from the release's
folder beside the repository (the folder `--evidence` names, when it is
elsewhere), and each refusal prints the command that fixes it:

- a release with no plan there, a plan for another version, a plan its
  parser finds a problem in, or a plan whose kind its number does not bear
  out: X.Y.0 is a minor or a major, and any other number a patch or a
  hotfix;
- a stage before the candidate stage that is neither exited nor skipped, a
  candidate stage not entered, and at the merge a candidate stage with no
  exit date;
- an S1 in the plan's ledger that is open or carried; for a minor or a
  major an S2 as well; for a patch or a hotfix, an S2 that the pull
  request's body does not name (item 10);
- a head the reference body has not run: no `run.json` of the body runner
  for this release whose suites, checks and bank passed, with a recording
  of the bank held to a golden and no step failed. A head that differs
  from the run's commit only in `CHANGELOG.md` counts as run, since the
  entry's closing line is written after the run, and a working-tree run
  counts only with no file changed;
- a newer run of the same head that failed once its code was on the body,
  whatever passed before it, or a `run.json` beside the run that cannot be
  read;
- a flag the body did not run in this release on a pushed commit of the
  release's branch, live where only a live run shows it, unless the plan
  names the gap with the maintainer's dated answer. A hotfix is not held
  to this rule.

That part is counted on one line, `body evidence: ok` or its number of
problems, printed after every problem, with the run it took, the body's
hashes and the flag table under it. From a run without `--merge`, while the
pull request is still a draft, that `ok` closes the candidate stage.

In CI the plan and the body's records are absent, and the step says the
body evidence is checked at the merge, on the laptop.

Only when all of that passes does it squash-merge, under the entry's title,
with a sign-off body, pinned to the commit it checked.

0.5.3's changelog credited the body with a run of a tree the body had not
run, and its signed tag still does. The merge now reads the run.

The tag itself is made by `tools/tag_release.py`, which runs the three checks
again and refuses a dirty tree, a branch other than master, a version the
packages do not declare, a commit title other than the entry's summary, an
unsigned key, or a tag that already exists. After the tag, and for a minor or
a major after its release page, the whole chain is checked once more:

```sh
python tools/release_audit.py --complete
```

The `release audit` workflow runs the history audit on every pull request,
merge and tag, allowing only the newest version to be unfinished, and on a
pull request `--pr <N> --ci` as well: the bump, the title, the cited pull
request, no attribution. It reruns when a title is edited on GitHub.

**Dependabot.** Every merge is a version, so a Dependabot pull request is
never merged as it stands; the audit fails it. Its change goes into the next
versioned pull request (`git cherry-pick -s <its commit>` on that branch),
and the Dependabot pull request is closed.

Everything below is what a script cannot check.

---

## 1. Walk the scope line by line

**Open the roadmap. Copy the release's scope sentence. Tick each noun in it
against a file and a test.**

Read the written scope from the file and check the items off one at a time,
even the ones you are certain about.

> **The scar.** 0.3's scope read "Audio **in/out**, wake word, VAD,
> endpointing." Four of those five were built and it was called complete. Audio
> *out* had a class in `emet-hal` with no entry point, unreachable from the
> engine, used by nothing. It survived several rounds of "what is left?"
> because each round asked memory rather than the sentence.

A noun is done when it has **an implementation, a test, and a caller**. Two out
of three is how audio out passed for finished: it had an implementation and a
test, and nothing used it.

## 2. Meet the acceptance criterion in its own words

**Paste the criterion. Underneath it, paste the evidence.**

The roadmap states a gate per release, phrased as an observable outcome. Answer
it literally, not approximately.

> **The scar.** 0.3's gate is "...for ten minutes without drift." Drift was
> unmeasurable, since nothing timed anything, so the claim would have been a
> feeling. `--stats` exists because a criterion you cannot produce a number for
> is one you cannot honestly sign off.

If part of the criterion needs hardware you do not have to hand, that is not a
reason to soften the criterion. It is the reason to stop and go get it.

Every number in the evidence names the file that holds it, and the line
where the file is long, so a reader can find it and a script can check it.

> **The second scar.** 0.5.3 shipped two comments that claimed more than their
> evidence files held: one said every placement rose 13 to 21 dB, where one
> rose 6.1 dB, and the other rested on two recordings where five showed a
> 5.3 dB climb. Each was found only by opening the file.

## 3. Check every deferred decision

**List every "later, when X" from this cycle. For each, has X happened?**

Deferrals are made in conversation and lost there. Write them down as they are
made, with the trigger, wherever you plan the release. Then walk both
places a condition can wait: the roadmap's register of deferred decisions,
and the conditions written into this release's own list.

> **The scar.** The version number lived in six places across three packages.
> It was deferred twice with the trigger "the 0.3 release PR", and when that
> PR arrived, the trigger only fired because somebody remembered. By then the
> installed metadata said 0.1.0 while the source said 0.2.0.

> **The second scar.** 0.5.4's list allowed a paid replay only if a cut
> reached a first sound. The replays of 2026-10-06 and 07 met that
> condition, and nothing recorded it until a review walked the list.

## 4. Look for rules that live in only one place

**Anything enforced in CI but not in a test will be broken by the next change
and found by the pipeline.**

> **The scar.** `examples/invalid/` has a contract: plain `emet validate`
> rejects everything in it. That rule existed only in `ci.yml`. A fixture was
> added that merely warned, every local test passed, and CI went red on three
> Python versions afterwards. There is now a test that sweeps the directory.

Ask the reverse too: anything a test asserts that CI does not run.

Every CI job's steps live in `tools/ci.py`, which the workflows, the laptop
and the body runner all call. A workflow holds the runner, the matrix and
the machine's setup, and each job's one command is
`python tools/ci.py <job> --install`. Two kinds of step stay in the
workflows: the weekly Scorecard job, which is Scorecard's own action and
allows no `run:` step, and the upload of the goldens the bank jobs write.
No check holds this rule yet, so a step added to a workflow outside
`tools/ci.py` runs on GitHub and never under `--all`.

A second script holds the workflows to every house rule, and runs where
zizmor is not installed. Every check name `tools/ci.py --list` gives belongs
to exactly one job, and that job runs `tools/ci.py` with the job key the
name belongs to. A step that calls `tools/ci.py` runs that call alone, and
nothing in its job can turn the job green over the script's exit. Every
action is pinned to a commit, with its version in a comment beside it.
Every job sets a timeout. The top level grants `contents: read` alone, and
each permission a job takes beyond it carries its reason in a comment. In
a workflow that a schedule or a run by hand starts, every concurrency group
names the event. Every Dependabot entry waits seven days before it proposes
a release. No checkout keeps its token, and no `run:` line holds an
expression; zizmor finds those two and an unpinned action as well. The
script is `tools/ci/check_workflows.py`, and a test runs it over the
repository's own workflows and the Dependabot file.

## 5. Run what CI runs, not what you usually run

The suites are not the pipeline. The pipeline also builds wheels, installs
them outside the source tree, and checks that entry points and versions
survive packaging. It builds each wheel twice and fails when the two builds
differ, and it fails when an install would take an Emet package from the
package index. `python tools/ci.py --all` runs every job this machine can
and names each it could not run here: mutation testing needs `fork`, which
Windows lacks, the arm64 job needs Linux on arm64, the virtual sound card
job runs on GitHub, and the two bank jobs run nowhere until the generated
bank's archive is published and `tools/bank/generated/store.json` names it.

> **The scar.** A CLI smoke test was described as catching packaging
> regressions. CI installed with `-e`, so the packaged layout was never
> exercised at all. It took building a real wheel to notice.

## 6. Verify on hardware, or say plainly that you did not

Emet is a robot. A release verified only against wav files has been verified
against wav files.

**Do not describe a file replay as a hardware result.** State the platform
every number came from. If the reference body has not run this release, the
release notes say so.

> **Why it matters here.** A wav replay exercises everything except the sound
> card, so genuine clock drift cannot appear. And an x86 real-time factor says
> nothing about ARM except by comparison.

Since 0.5.5 most of the body's evidence is gathered with nobody at the
robot. `tools/bank/replay.py` replays the voice bank on the laptop and on the
reference body and holds every wake to the golden files in
`tools/bank/golden/`, exactly, per platform. A sitting still being recorded
is the one exception: while no recording of its plan has a golden on any
platform, each is replayed, printed as `UNRELEASED` and held to nothing,
and the plan's first golden ends that for all of them. `tools/body/run.py`
puts the tree on the reference body over SSH and runs the suites, the
checks, the replays and the flags, and prints when each flag last ran
there. Since 0.5.6 it also measures the budgets on the body and holds each
numeric measure to the last release's on the same body; the board lines
are recorded beside them and never judged. Its `--soak MINUTES` runs a
replay soak with `sink: "null"`, an hour long for a patch, and records
whether memory, open files, threads and frame times stayed flat. The merge
refuses a head the body has not run. A person is needed once, last, for the
release sitting in `tools/body/sitting.md`: for a minor or a major, and for
a patch only when it changes what a person judges. A replay keeps its name in the
pull request: its platform, its commit, the bank's version and the start
mean go beside every number, and a golden that changed is a diff with a
reason for each changed wake. A number from the generated bank
(`tools/bank/generate.py`) is labelled "generated": it screens a change and
never decides one.

> **The scar.** 0.4 shipped on 0.3's body runs of `--echo`, `--replay`, a
> plain run and the ten live minutes of `--stats`, while the code that opens
> the sink had changed.

## 7. Refresh the documents that state a version

`tools/release_check.py` catches the obvious ones. It cannot catch a paragraph
that is merely out of date, so re-read:

- `README.md`: the status section, and any sample output
- each package `README.md`
- `emet_hal/__init__.py` and friends, which list what ships
- `CITATIONS.md`, if anything was taken from a paper or a repository
- the roadmap row for this release, the `CHANGELOG.md` entry, and the
  pull request body

## 8. Tag

- The version is bumped in all four `pyproject.toml` files and **nowhere
  else**. `release_check.py` enforces this.
- The three sibling packages require `emet-sdk>=X.Y.Z`, the release's own
  version, and any other Emet package at the same floor.
  `release_check.py` enforces this.
- The `CHANGELOG.md` entry is written. Commits stay short; the tag carries
  the entry, and the pull request carries the measurements.
- Every commit in the release is signed off, or the DCO check fails the PR.
- The entry is dated the day of the merge. `release_audit.py --pr` refuses
  any other day, and `--merge` merges only past that check.

## 9. Audit the chain

**After the tag, and the release page for a minor or a major, run
`release_audit.py --complete`.** The pull request title, the squash commit,
the tag, the changelog entry and the release page carry one sentence and one
day, and nothing reaching GitHub carries an AI attribution line.

> **The scar.** 0.4.1 and 0.5.0 were both dated the day their pull requests
> were finished, a day before they merged. The v0.4.1 and v0.5.0 tags lost
> the entry's `### Added`, `### Changed` and `### Fixed` headings to git's
> default message cleanup, and so did the v0.5.0 release page made from the
> tag. The 0.4.1 pull request's body carried an attribution line until it
> was edited out. Each passed every check that existed at the time. Then
> 0.5.1 went out dated a day early too: the audit refused the merge, and the
> merge ran anyway, because the check and `gh pr merge` were two lines of
> one pasted block. The merge now runs from inside the check.

## 10. Log every finding with a severity

**Every finding from every stage becomes a row in the release's ledger, with
a severity, even when it is fixed at once. No row is ever deleted.**

| Severity | What it is | At the merge |
|---|---|---|
| S1 | A floor broken: a level-2 or level-3 memory in a prompt, a chain ending without a voice rung, audio, text or a key sent where the owner did not choose, credits spent or a part moved without the owner's say; the loop crashing or hanging on the reference body; `memory.db` lost or corrupted | Refuses every merge |
| S2 | A shipped capability wrong on the reference body: a documented flag that fails, a golden that moves with no reason, a wake or a turn the last release kept and this one loses, a budget broken; or a number a decision rests on, wrong in a document | Refuses a minor's or a major's merge; a patch or a hotfix may merge past one it does not touch, naming it |
| S3 | Degraded with a workaround, or wrong on a path the reference body does not take | Logged |
| S4 | Wording or cosmetics | Logged |

The audit holds the naming of an S2 and nothing more: a patch's or a
hotfix's pull request body that names it lets the merge past it. Whether
the change touches the finding is a person's judgment, made at the review
stage.

Reproduce a finding before fixing it, and give it a regression test or a
written reason why there is none.

> **The scar.** 0.5.4's review found 0.5.3's failures again: tests that
> pinned less than they said, and a changelog line that read as if more was
> fixed than was. 0.5.5's review found 23 of 36 deliberate breaks caught by
> no test. The findings lived in pull request bodies, with no state and no
> severity.

## 11. Show what a builder will see

**Every new or changed console line, flag, help text and error goes into the
pull request as it prints. Each failure state that prints without hardware
is pinned by a console snapshot, or the pull request names it as left out,
with the reason.**

The snapshots are under `tests/snapshots/` in `emet-sdk` and in
`emet-engine`, one file a case, for `emet validate`, `emet explain`,
`emet-listen` and `emet-talk`. A changed message fails its
test with the diff until it is rewritten on purpose with
`EMET_UPDATE_SNAPSHOTS=1`. A state that needs a sound card or real timing
has no snapshot, and the pull request shows it as it printed. A replay file
that is missing, at the wrong rate or of the wrong width stops with a
traceback, a finding of 0.5.6's ledger; its three cases are strict expected
failures that turn red the day the finding is fixed, and get their
snapshots then. A provider's preflight answered by a vendor has no snapshot
yet where no stub stands in for the vendor: Anthropic's and Deepgram's with
no network or a refused key, and OpenAI's answers other than 401. The pull
request's sheet lists them (read 2026-11-26).

> **The scar.** `audio.stt` became `models.stt` in 0.4's first batch, and
> the old name survived in an error string, a help text, three docstrings
> and a README until the release-day re-read.

## 12. Print the public API and the schema's diff

**Every pull request carries the diff of the public names, signatures,
entry points, console scripts, schema properties and CLI flags against
master, printed by `tools/api_diff.py --base origin/master`. The
changelog's Added, Changed, Deprecated, Removed and Security lines account
for every line of it.**

The `api-diff (advisory)` check prints the diff on every pull request and
never turns red: a person decides what each line means. The SDK's public
names are held a second way. `emet-sdk/tests/test_public_api.py` fails when
they differ from its golden file. The golden is rewritten on purpose, from
`emet-sdk/`, with `python tests/test_public_api.py --update`.

> **The scar.** `SCHEMA_VERSION`'s comment says a document from a newer SDK
> is a hard error and an older one is migrated forward, and nothing reads
> `SCHEMA_VERSION`. `voice.engine` and `voice.model` are accepted and
> ignored, with no warning to the soul that sets them (read 2026-11-26).

---

## The pattern behind all of it

Every scar above is the same mistake: **checking work against a memory of the
requirement instead of the requirement.** Memory summarises, and summaries drop
the item you were least involved with, which is reliably the one that is
missing.

Hence the shape of this list. Open the file. Copy the sentence. Tick the nouns.
