# Releasing

A checklist for every version. Every merge to master is one, a patch, a
minor or a major, so this runs for every pull request; items 1 and 2 are the
roadmap-row work of a minor or a major.

Each item is here because something went wrong without it, and the note under
each says what. A checklist of plausible-sounding good practice gets skipped;
one where every line has a scar does not.

Run the mechanical half first. It is fast and it fails loudly:

```sh
python tools/check_layering.py .
python tools/release_check.py .
python tools/check_style.py .
```

On the day of the merge, before merging, check the pull request itself:

```sh
python tools/release_audit.py --pr <N>
```

It refuses a draft, a red or pending check, a head other than this
checkout's, a title other than the changelog entry's, an AI attribution line,
a version already tagged, and an entry not dated today.

The tag itself is made by `tools/tag_release.py`, which runs the first three
again and refuses a dirty tree, a branch other than master, a version the
packages do not declare, a commit title other than the entry's summary, an
unsigned key, or a tag that already exists. After the tag, and for a minor or
a major after its release page, the whole chain is checked once more:

```sh
python tools/release_audit.py --complete
```

The `release audit` CI job runs the same audit on every pull request, merge
and tag, allowing only the newest version to be unfinished.

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

## 3. Check every deferred decision

**List every "later, when X" from this cycle. For each, has X happened?**

Deferrals are made in conversation and lost there. Write them down as they are
made, with the trigger, wherever you plan the release.

> **The scar.** The version number lived in six places across three packages.
> It was deferred twice with the trigger "the 0.3 release PR", and when that
> PR arrived, the trigger only fired because somebody remembered. By then the
> installed metadata said 0.1.0 while the source said 0.2.0.

## 4. Look for rules that live in only one place

**Anything enforced in CI but not in a test will be broken by the next change
and found by the pipeline.**

> **The scar.** `examples/invalid/` has a contract: plain `emet validate`
> rejects everything in it. That rule existed only in `ci.yml`. A fixture was
> added that merely warned, every local test passed, and CI went red on three
> Python versions afterwards. There is now a test that sweeps the directory.

Ask the reverse too: anything a test asserts that CI does not run.

## 5. Run what CI runs, not what you usually run

The suites are not the pipeline. As of this release the pipeline also builds
wheels, installs them outside the source tree, and checks that entry points and
versions survive packaging.

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
- The `CHANGELOG.md` entry is written. Commits stay short; the tag carries
  the entry, and the pull request carries the measurements.
- Every commit in the release is signed off, or the DCO check fails the PR.
- The entry is dated the day of the merge. `release_audit.py --pr` refuses
  any other day.

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
> was edited out. Each passed every check that existed at the time.

---

## The pattern behind all of it

Every scar above is the same mistake: **checking work against a memory of the
requirement instead of the requirement.** Memory summarises, and summaries drop
the item you were least involved with, which is reliably the one that is
missing.

Hence the shape of this list. Open the file. Copy the sentence. Tick the nouns.
