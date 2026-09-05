# Releasing

A checklist for every minor and major release.

Each item is here because something went wrong without it, and the note under
each says what. A checklist of plausible-sounding good practice gets skipped;
one where every line has a scar does not.

Run the mechanical half first — it is fast and it fails loudly:

```sh
python tools/check_layering.py .
python tools/release_check.py .
```

Everything below is what a script cannot check.

---

## 1. Walk the scope line by line

**Open the roadmap. Copy the release's scope sentence. Tick each noun in it
against a file and a test.**

Not from memory. Read the written scope and check the items off one at a time,
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
> unmeasurable — nothing timed anything — so the claim would have been a
> feeling. `--stats` exists because a criterion you cannot produce a number for
> is one you cannot honestly sign off.

If part of the criterion needs hardware you do not have to hand, that is not a
reason to soften the criterion. It is the reason to stop and go get it.

## 3. Check every deferred decision

**List every "not now, but when X" from this cycle. For each, has X happened?**

Deferrals are made in conversation and lost there. Write them down as they are
made, with the trigger, wherever you plan the release.

> **The scar.** The version number lived in six places across three packages.
> It was deferred twice with the trigger "the 0.3 release PR" — and when that
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

- `README.md` — the status section, and any sample output
- each package `README.md`
- `emet_hal/__init__.py` and friends, which list what ships
- `CITATIONS.md`, if anything was taken from a paper or a repository
- the roadmap row for this release, and the release notes

## 8. Tag

- The version is bumped in all three `pyproject.toml` files and **nowhere
  else** — `release_check.py` enforces this.
- The tag message is written. Commits stay short; the tag carries the detail.
- Every commit in the release is signed off, or the DCO check fails the PR.

---

## The pattern behind all of it

Every scar above is the same mistake: **checking work against a memory of the
requirement instead of the requirement.** Memory summarises, and summaries drop
the item you were least involved with — which is reliably the one that is
missing.

Hence the shape of this list. Open the file. Copy the sentence. Tick the nouns.
