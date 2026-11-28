#!/usr/bin/env python3
"""Audit every version made under the rule that every merge to master is one.

A version's sentence lives in five places: the pull request title, the
squash commit on master, the signed tag, the `CHANGELOG.md` entry and, for a
minor or a major, the GitHub release page. `release_check.py` and
`tag_release.py` hold each one when it is written; this checks, afterwards
and for every version at once, that they still agree:

    the commit     every commit on master since 0.4.1 is titled
                   `X.Y.Z: Summary`, carries that version in all four
                   packages, and has a tag
    the tag        annotated, verified by GitHub, on that commit, its message
                   the changelog entry, dated the entry's day
    the entry      its summary is the commit's, its date the commit's
    the page       a minor or a major has one, a pre-release before 1.0,
                   titled and worded as the entry; a patch has none
    the PR         the one the entry cites, merged as that commit, titled
                   the same
    attribution    none in the commit, the tag, the page or the PR body

The newest version may be unfinished, merged and not yet tagged or tagged
and waiting for its page, and is reported as pending. `--complete` makes
pending a failure; the release run ends with it.

`--pr N` is the check before merging. Every push can hold five of its
questions: the version bumped above master's, the title the changelog entry's,
an entry that cites pull request N and no other, no attribution, not yet
tagged. The merge day holds the rest: ready, every check green, mergeable,
this checkout at the pull request's head with nothing uncommitted, and the
entry dated today. The date is the one a script can hold only on the day:
0.4.1, 0.5.0 and 0.5.1 were each dated the day their pull requests were
finished, a day before they merged. A wrong date prints the command that
fixes it.

The merge day also reads the release's own evidence, which a person used to
check by hand, from the release's folder beside the repository
(`--evidence DIR`, by default `../releases/vX.Y.Z` for the version the
packages declare): the plan, `plan.md`, through `plan_file.py`, and the body
runner's records, `body/*/run.json`. It refuses:

    the plan       none there, one for another version, one the parser finds
                   a problem in, or one whose kind its number does not bear
                   out: X.Y.0 is a minor or a major, any other a patch or a
                   hotfix
    the stages     a stage 0 to 6 neither exited nor skipped, or Stage 7 not
                   entered; with `--merge`, Stage 7 without an exit date
    the ledger     an S1 still open or carried; for a minor or a major an S2
                   as well; for a patch or a hotfix, an S2 that the pull
                   request's body does not name
    the body run   no passing run of the head: a `run.json` of format 2 or
                   later, for this release, whose suites, checks and bank
                   steps passed, the bank with a take held to a golden, with
                   no step failed, and whose source is the head. A commit
                   that differs from the head only in `CHANGELOG.md` is the
                   head, since the entry's closing line is written after the
                   run. A working-tree run counts only with no file changed.
                   A later run of the head that failed once its code was on
                   the body refuses too, whatever passed before it and
                   whatever its verdict says, and so does a `run.json` that
                   cannot be read, since it may be that run
    the flags      a body flag of the runner's `FLAG_ROWS` that no run of a
                   pushed commit on this branch exercised in this release,
                   live where its row says so, unless the plan holds the
                   maintainer's dated answer for it. A hotfix is not asked

Each prints the command that fixes it, and one line counts them: `body
evidence: ok`, or the number of problems. Without `--merge` that `ok` is what
the plan's Stage 7 exits on, while the pull request is still a draft and the
merge day's own questions still fail; the merge then wants Stage 7 exited
too. 0.5.3's changelog entry credited the body with a run of a tree the body
had not run, and its signed tag still does. No check read the body's runs
then.

`--merge` with `--pr` merges pull request N, squashed under its title with a
sign-off body and pinned to the head commit it checked, only when every check
passed. 0.5.1 went out with the wrong date because the check and
`gh pr merge` were two lines of one pasted block, and the second ran after
the first refused.

`--ci` with `--pr` asks only the five questions every push can hold, for the
CI run on a pull request. A Dependabot pull request fails it, as it should:
merged as it stands it puts a commit on master that is not a version. It
reads no evidence, since the release's folder is not in the repository, and
says so: `body evidence: checked at merge, on the laptop`.

Read-only, except that `--merge` runs `gh pr merge`: git and the GitHub CLI
(`gh`, authenticated).

Usage:
    python tools/release_audit.py
    python tools/release_audit.py --complete
    python tools/release_audit.py --pr 12
    python tools/release_audit.py --pr 12 --evidence ../releases/v0.5.6
    python tools/release_audit.py --pr 12 --merge
    python tools/release_audit.py --pr 12 --ci
Exit:   0 nothing wrong (and merged, with --merge), 1 a problem found, GitHub
        not answering, or the merge failed (each is printed), 2 an option
        given where it does not apply.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan_file  # noqa: E402  a tool beside this file, imported by its bare name
from tag_release import PACKAGES, changelog_section, tag_message  # noqa: E402  a tool beside this file, imported by its bare name

#: The first version under the rule (RELEASING.md). The tags before it keep
#: their two-part names and their own notes.
FIRST = (0, 4, 1)

#: Tags before this went through git's default message cleanup, which drops
#: every line that starts with `#`, and so lost the entry's `###` headings.
VERBATIM_FROM = (0, 5, 1)

#: The one address besides a GitHub noreply one that may sign a merge: it
#: lands in master's history, and a personal address must never.
PROJECT_ADDRESS = "wake.up.emet@gmail.com"

TITLE = re.compile(r"^(\d+)\.(\d+)\.(\d+): (.+)$")
HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})\s*$")
CITED = re.compile(r"\(#(\d+)\)")

#: An AI attribution line in anything that reaches GitHub. A model's name in
#: a sentence ("Anthropic claude-opus-5 on the same questions") is not one.
ATTRIBUTION = re.compile(
    r"generated with \[?claude|co-authored-by:[^\n]*(claude|anthropic)|claude\.com/claude-code",
    re.IGNORECASE,
)

#: What gh prints when the thing asked for does not exist: `gh api` on a 404,
#: `gh release view` with no release, `gh pr view` with no such pull request
#: (gh 2.100.0, verified 2026-10-01). Any other failure raises Unanswered.
#: Read as absence, a single HTTP 502 fails the required check under the
#: wrong cause, and passes a patch with its release page unread.
#: `gh release view` prints its text after some 502s too; `page()` checks it.
NOT_FOUND = ("(HTTP 404)", "release not found", "Could not resolve to a PullRequest")

#: A body run's record names its format, `emet-body-run/N`. Format 2 (0.5.6)
#: is the first that says which flags the run itself exercised and how many
#: takes the bank step held to a golden, so the merge reads it and any later.
RUN_FORMAT = re.compile(r"emet-body-run/([0-9]+)")
RUN_FORMAT_FROM = 2

#: The steps of a body run that must each read `passed` for the run to count.
RUN_STEPS = ("suites", "checks", "bank")

#: The steps of a body run that come before its code is on the body. A run
#: that failed in one of these never ran the code. The verdict cannot say
#: so: the runner writes "body run not done" for these and also when the
#: body stops answering after the suites, the checks and the bank have run
#: (`tools/body/run.py`, `_run`, read 2026-11-26).
BEFORE_THE_CODE = ("preflight", "sync", "install")

#: The one file the head may differ in from the commit the body ran: the
#: entry's closing line is written after the run (RELEASE_PROCESS.md §4).
CLOSING_FILE = "CHANGELOG.md"

#: A commit as `run.json` records it, whole. Anything else in that field is
#: never handed to git, where a value that starts with `-` reads as an option.
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

#: What git can run a flag's evidence on. A working tree is nobody's commit.
PUSHED, TREE = "pushed commit", "working tree"

#: The kinds of release that may merge past an S2 by naming it
#: (RELEASE_PROCESS.md §1.8, the plan's row; contract §4, E3).
MAY_NAME_AN_S2 = ("patch", "hotfix")


class Unanswered(Exception):
    """A question the audit asked got no answer it can use: a gh call failed
    without gh's not-found text, `gh release view` found no release that the
    list of releases has, or `git ls-remote` failed. When GitHub did not
    answer (a 502, a timeout, no network), a second run can get through; a
    403, or a --json field this gh does not know, fails the same way every
    run. Nothing is known about the thing asked about, so the run stops
    there, with a message that names the cause."""


@dataclass(frozen=True)
class Entry:
    version: str
    date: str
    summary: str
    message: str


@dataclass(frozen=True)
class BodyRun:
    """One `run.json` of the body runner (`tools/body/run.py`), as read.
    Every field is read defensively: the file is evidence a person can edit,
    and a field of the wrong shape counts as absent."""

    folder: Path
    record: dict[str, Any]
    #: Why the file could not be read; empty when it was.
    unread: str = ""

    @property
    def name(self) -> str:
        return self.folder.name

    def text(self, key: str) -> str:
        value = self.record.get(key)
        return value if isinstance(value, str) else ""

    @property
    def started(self) -> str:
        return self.text("started")

    @property
    def release(self) -> str:
        return self.text("release")

    @property
    def format(self) -> int:
        m = RUN_FORMAT.fullmatch(self.text("format"))
        return int(m.group(1)) if m else 0

    @property
    def source(self) -> dict[str, Any]:
        value = self.record.get("source")
        return value if isinstance(value, dict) else {}

    @property
    def kind(self) -> str:
        value = self.source.get("kind")
        return value if isinstance(value, str) else ""

    @property
    def head(self) -> str:
        """The commit the run names, or nothing when it names none whole."""
        value = self.source.get("head")
        return value if isinstance(value, str) and COMMIT.fullmatch(value) else ""

    @property
    def steps(self) -> list[dict[str, Any]]:
        value = self.record.get("steps")
        return [step for step in value if isinstance(step, dict)] if isinstance(value, list) else []

    def step(self, name: str) -> dict[str, Any] | None:
        return next((step for step in self.steps if step.get("name") == name), None)

    @property
    def flags_run(self) -> list[tuple[str, str]]:
        """Each flag the run itself exercised on the body's sound hardware,
        with how: `live` or `replay`."""
        value = self.record.get("flags_run")
        if not isinstance(value, list):
            return []
        return [(str(e.get("flag")), str(e.get("how"))) for e in value if isinstance(e, dict)]


def read_runs(body: Path) -> list[BodyRun]:
    """Every `run.json` one folder below `body`, the newest first by its
    `started`, the folder's name breaking a tie. The runner writes local ISO
    time with no offset, so the text sorts as the moments do except across a
    clock change: in the hour the clocks go back, or after the laptop's
    time zone moves, a later run can sort as the older. A file that cannot
    be read is kept, with the reason."""
    runs: list[BodyRun] = []
    for path in sorted(body.glob("*/run.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            runs.append(BodyRun(path.parent, {}, _first_line(str(error)) or type(error).__name__))
            continue
        if isinstance(record, dict):
            runs.append(BodyRun(path.parent, record))
        else:
            runs.append(BodyRun(path.parent, {}, "it holds no JSON object"))
    return sorted(runs, key=lambda run: (run.started, run.name), reverse=True)


def body_flag_rows() -> tuple[Any, ...]:
    """`FLAG_ROWS` of `tools/body/run.py`: the body flags a release runs
    (RELEASE_PROCESS.md §4), each with its `label` and whether only a live
    run counts for it (`live_only`). The runner's table is the only list;
    it is imported when the evidence is read, so the history audit and a CI
    run never load the runner."""
    name = "emet_body_run_for_the_audit"
    module = sys.modules.get(name)
    if module is None:
        path = Path(__file__).resolve().parent / "body" / "run.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path} cannot be imported")
        module = importlib.util.module_from_spec(spec)
        # A dataclass looks its own module up by name while it is defined.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return tuple(module.FLAG_ROWS)


class Audit:
    def __init__(self, root: Path, changelog: Path, ref: str) -> None:
        self.root = root
        self.changelog = changelog
        self.ref = ref
        self.problems: list[str] = []
        self.pending: list[str] = []
        #: Commands that fix a problem found, printed after the problems.
        self.fixes: list[str] = []
        #: The commit the pre-merge check read, which the merge is pinned to.
        self.head = ""
        self.repo = ""
        #: The tags in GitHub's list of releases, read once by `listed()`.
        self.release_tags: set[str] | None = None
        #: The version the four packages declare, once `before_merge` read
        #: one; the evidence is that release's.
        self.release = ""
        #: The pull request's body, once read. A patch names there each S2
        #: it merges past.
        self.pr_body: str | None = None
        #: How the evidence check ended, the line printed after the problems,
        #: and under it what the check took: the run, the body's hashes, the
        #: flag table, each S2 carried.
        self.evidence_line = ""
        self.evidence_notes: list[str] = []
        #: Where each commit a body run names sits against this branch, asked
        #: of git once a commit (`outside_release`).
        self.placed: dict[str, str] = {}
        #: The files each commit a body run names differs from the head in,
        #: as `git diff --name-only` gave them (`off_the_head`).
        self.diffed: dict[str, list[str]] = {}

    # ----------------------------------------------------------- plumbing

    def run(self, *args: str) -> tuple[int, str]:
        """The exit code, and stdout; on a failure stderr, then stdout. Stdout
        is kept since `gh pr checks` prints its table there and exits
        non-zero. Stderr goes first so the first line is the tool's own:
        `gh api` prints the response body to stdout, a 502's included."""
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            return 0, result.stdout.strip()
        return result.returncode, (result.stderr.strip() + "\n" + result.stdout.strip()).strip()

    def git(self, *args: str) -> str:
        code, out = self.run("git", *args)
        if code != 0:
            raise RuntimeError(f"git {' '.join(args)}: {out}")
        return out

    def gh(self, *args: str) -> object | None:
        """The JSON `gh` prints, or None when gh says the thing asked for does
        not exist (`NOT_FOUND`). Any other failure raises Unanswered."""
        code, out = self.run("gh", *args)
        if code == 0:
            return json.loads(out) if out else None
        if any(text in out for text in NOT_FOUND):
            return None
        raise Unanswered(_first_line(out) or f"gh {args[0]} exited {code}")

    def problem(self, version: str, msg: str) -> None:
        self.problems.append(f"{version}: {msg}")

    def attribution(self, version: str, where: str, text: str) -> None:
        if ATTRIBUTION.search(text or ""):
            self.problem(version, f"{where} carries an AI attribution line")

    def entry(self, version: str) -> Entry | None:
        text = self.changelog.read_text(encoding="utf-8")
        dates = {m.group(1): m.group(2) for m in map(HEADING.match, text.splitlines()) if m}
        section = changelog_section(self.changelog, version)
        if section is None or version not in dates:
            return None
        summary, rest = section
        return Entry(version, dates[version], summary, tag_message(version, summary, rest))

    def start(self) -> bool:
        code, out = self.run("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if code != 0:
            print(f"release-audit: the GitHub CLI cannot see this repository: {out}", file=sys.stderr)
            return False
        self.repo = out
        return True

    # ------------------------------------------------------------ history

    def history(self) -> None:
        log = self.git("log", "--first-parent", "--format=%H%x09%s", self.ref).splitlines()
        commits: list[tuple[str, str]] = []
        for line in log:
            sha, _, subject = line.partition("\t")
            commits.append((sha, subject))
            m = TITLE.match(subject)
            if m and tuple(int(n) for n in m.groups()[:3]) == FIRST:
                break
        else:
            self.problems.append(f"{self.ref}: no commit titled for {'.'.join(map(str, FIRST))}")
            return
        for index, (sha, subject) in enumerate(commits):
            m = TITLE.match(subject)
            if not m:
                self.problems.append(f"{sha[:7]}: on master and not a version: {subject!r}")
                continue
            self.version(sha, m, newest=index == 0)

    def version(self, sha: str, m: re.Match[str], *, newest: bool) -> None:
        numbers = tuple(int(n) for n in m.groups()[:3])
        version, summary = ".".join(map(str, numbers)), m.group(4)
        tag = f"v{version}"

        for pkg in PACKAGES:
            code, text = self.run("git", "show", f"{sha}:{pkg}/pyproject.toml")
            declared = re.search(r'^version = "([^"]+)"', text, re.MULTILINE) if code == 0 else None
            if not declared or declared.group(1) != version:
                self.problem(version, f"{pkg} at {sha[:7]} declares {declared.group(1) if declared else 'nothing'}")

        entry = self.entry(version)
        if entry is None:
            self.problem(version, "CHANGELOG.md has no entry")
            return
        if entry.summary != summary:
            self.problem(version, f"the commit says {summary!r}, the entry {entry.summary!r}")
        committed = self.git("log", "-1", "--format=%cd", "--date=short", sha)
        if entry.date != committed:
            self.problem(version, f"the entry is dated {entry.date}, the commit {committed}")
        self.attribution(version, "the commit message", self.git("log", "-1", "--format=%B", sha))

        self.pull_request(version, entry, sha)

        if not self.git("tag", "--list", tag):
            if newest:
                self.pending.append(f"{version}: merged, not yet tagged")
            else:
                self.problem(version, f"{tag} does not exist")
            return
        self.tag(version, tag, entry, sha)
        self.page(version, tag, entry, numbers, newest=newest)

    def tag(self, version: str, tag: str, entry: Entry, sha: str) -> None:
        if self.git("cat-file", "-t", tag) != "tag":
            self.problem(version, f"{tag} is a lightweight tag, with no message and no signature")
            return
        pointed = self.git("rev-list", "-n", "1", tag)
        if pointed != sha:
            self.problem(version, f"{tag} points at {pointed[:7]}, the version's commit is {sha[:7]}")
        dated = self.git("for-each-ref", f"refs/tags/{tag}", "--format=%(taggerdate:short)")
        if dated != entry.date:
            self.problem(version, f"{tag} was made on {dated}, the entry is dated {entry.date}")
        message = self.git("for-each-ref", f"refs/tags/{tag}", "--format=%(contents:subject)%0a%0a%(contents:body)")
        expected = entry.message
        if tuple(int(n) for n in version.split(".")) < VERBATIM_FROM:
            expected = _cleaned(expected)
        if message.strip() != expected.strip():
            self.problem(version, f"{tag}'s message is not the changelog entry")
        self.attribution(version, f"{tag}'s message", message)

        ref = self.gh("api", f"repos/{self.repo}/git/refs/tags/{tag}")
        if not isinstance(ref, dict):
            self.problem(version, f"{tag} is not on GitHub")
            return
        obj = ref.get("object") or {}
        if obj.get("type") != "tag":
            self.problem(version, f"{tag} on GitHub is not an annotated tag")
            return
        signed = self.gh("api", f"repos/{self.repo}/git/tags/{obj.get('sha')}")
        verification = (signed or {}).get("verification") or {} if isinstance(signed, dict) else {}
        if not verification.get("verified"):
            self.problem(version, f"GitHub does not verify {tag}'s signature ({verification.get('reason', 'unknown')})")

    def page(self, version: str, tag: str, entry: Entry, numbers: tuple[int, ...], *, newest: bool) -> None:
        release = self.gh("release", "view", tag, "--json", "name,body,isPrerelease,isDraft")
        # `gh release view` asks for the published release and for a draft at
        # once, and says "release not found" when one lookup says so and the
        # other gets a 502 (gh 2.100.0, verified 2026-10-01). Read as absence,
        # a minor's page is reported missing and a patch's page passes unread.
        if release is None and self.listed(tag):
            raise Unanswered(f"gh release view {tag} found no release, and the list of releases has one")
        if numbers[2] != 0:
            if release is not None:
                self.problem(version, "a patch has a release page; a patch's tag is its release")
            return
        if not isinstance(release, dict):
            if newest:
                self.pending.append(f"{version}: tagged, no release page yet")
            else:
                self.problem(version, "a minor or major with no release page")
            return
        title = f"{version}: {entry.summary}"
        if release.get("name") != title:
            self.problem(version, f"the release page is titled {release.get('name')!r}, not {title!r}")
        if (release.get("body") or "").replace("\r\n", "\n").strip() != entry.message.strip():
            self.problem(version, "the release page is not the changelog entry")
        if bool(release.get("isPrerelease")) != (numbers[0] < 1):
            self.problem(version, "a release before 1.0 is a pre-release, and one after is not")
        if release.get("isDraft"):
            self.problem(version, "the release page is still a draft")
        self.attribution(version, "the release page", release.get("body") or "")

    def listed(self, tag: str) -> bool:
        """Whether GitHub's list of releases has one for `tag`, a draft
        included when the token can push. The list is one request, made at
        most once a run, and holds up to a hundred releases; the project
        makes one per minor and major."""
        if self.release_tags is None:
            releases = self.gh("api", f"repos/{self.repo}/releases?per_page=100") or []
            self.release_tags = {r.get("tag_name") for r in releases if isinstance(r, dict)}
        return tag in self.release_tags

    def pull_request(self, version: str, entry: Entry, sha: str) -> None:
        cited = sorted(set(CITED.findall(entry.message)), key=int)
        if len(cited) != 1:
            self.problem(version, f"the entry cites {len(cited)} pull requests; a version is one ({cited})")
            return
        number = cited[0]
        pr = self.gh("pr", "view", number, "--json", "state,title,body,mergeCommit")
        if not isinstance(pr, dict):
            self.problem(version, f"#{number} could not be read")
            return
        if pr.get("state") != "MERGED":
            self.problem(version, f"#{number} is {pr.get('state')}, not merged")
        merged = (pr.get("mergeCommit") or {}).get("oid")
        if merged and merged != sha:
            self.problem(version, f"#{number} merged as {merged[:7]}, the version's commit is {sha[:7]}")
        title = f"{version}: {entry.summary}"
        if pr.get("title") != title:
            self.problem(version, f"#{number} is titled {pr.get('title')!r}, not {title!r}")
        self.attribution(version, f"#{number}'s body", pr.get("body") or "")

    # ----------------------------------------------------- before a merge

    def before_merge(self, number: str, *, ci: bool = False) -> str | None:
        """Pull request N, checked before its merge. Returns the title it
        merges under, or None when it cannot be read; raises Unanswered when
        GitHub does not answer.

        With `ci`, only what holds on every push: the bump, the title, the
        cited pull request, no attribution, not yet tagged. The day, the
        draft, the head, a clean tree, the checks and whether GitHub can
        merge it are the merge day's questions.
        """
        declared = set()
        for pkg in PACKAGES:
            text = (self.root / pkg / "pyproject.toml").read_text(encoding="utf-8")
            m = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
            declared.add(m.group(1) if m else "")
        if len(declared) != 1:
            self.problems.append(f"the packages declare {sorted(declared)}; release_check.py says more")
            return None
        (version,) = declared
        self.release = version
        code, text = self.run("git", "show", f"{self.ref}:emet-sdk/pyproject.toml")
        on_master = re.search(r'^version = "([^"]+)"', text, re.MULTILINE) if code == 0 else None
        if on_master is None:
            self.problems.append(f"{self.ref} declares no version to compare with")
        elif _numbers(version) <= _numbers(on_master.group(1)):
            self.problem(
                version,
                f"not above {on_master.group(1)}, what master declares: every merge is a version, "
                f"and a change that is not one (a Dependabot update) goes into the next pull request "
                f"that is (RELEASING.md)",
            )
        entry = self.entry(version)
        if entry is None:
            self.problem(version, "CHANGELOG.md has no entry")
            return None
        cited = sorted(set(CITED.findall(entry.message)), key=int)
        if cited != [str(number)]:
            self.problem(version, f"the entry cites {', '.join('#' + c for c in cited) or 'no pull request'}, not #{number}")
        if not ci:
            today = datetime.date.today().isoformat()
            if entry.date != today:
                self.problem(version, f"the entry is dated {entry.date}; merged today it is {today}")
                self.fixes.append(
                    f"sed -i 's/^## \\[{version}\\] - {entry.date}/## [{version}] - {today}/' CHANGELOG.md"
                    f" && git add CHANGELOG.md && git commit -s -m \"Date the {version} entry the day it merges\""
                    f" && git push"
                )
        pr = self.gh(
            "pr", "view", number, "--json", "state,isDraft,title,body,headRefOid,baseRefName,mergeable"
        )
        if not isinstance(pr, dict):
            self.problems.append(f"#{number} could not be read")
            return None
        self.pr_body = pr.get("body") or ""
        title = f"{version}: {entry.summary}"
        if pr.get("title") != title:
            self.problem(version, f"#{number} is titled {pr.get('title')!r}, not {title!r}")
        self.attribution(version, f"#{number}'s body", pr.get("body") or "")
        # `--exit-code` exits 2 when origin has no such tag. Any other failure
        # is origin not answering, and read as "not tagged" it passes this
        # question without an answer.
        code, out = self.run("git", "ls-remote", "--exit-code", "--tags", "origin", f"v{version}")
        if code not in (0, 2):
            raise Unanswered(_first_line(out) or f"git ls-remote exited {code}")
        if code == 0 or self.git("tag", "--list", f"v{version}"):
            self.problem(version, f"v{version} already exists")
        if ci:
            return title
        if pr.get("state") != "OPEN" or pr.get("isDraft"):
            self.problem(version, f"#{number} is {'a draft' if pr.get('isDraft') else pr.get('state')}")
        if pr.get("baseRefName") != "master":
            self.problem(version, f"#{number} would merge into {pr.get('baseRefName')}, not master")
        mergeable = pr.get("mergeable")
        if mergeable == "UNKNOWN":
            self.problem(version, f"#{number}: GitHub has not yet worked out whether it can merge; run this again")
        elif mergeable != "MERGEABLE":
            self.problem(version, f"#{number} is {mergeable}, not mergeable")
        head = self.git("rev-parse", "HEAD")
        if pr.get("headRefOid") != head:
            self.problem(version, f"#{number}'s head is {str(pr.get('headRefOid'))[:7]}, this checkout is {head[:7]}")
        # What was read is the working tree; what GitHub merges is the pull
        # request's commit. They are the same files only when nothing is
        # left uncommitted, and the merge is pinned to that commit.
        if self.git("status", "--porcelain", "--untracked-files=no"):
            self.problem(version, "the working tree has uncommitted changes; the check read them, the merge would not take them")
        self.head = head
        code, out = self.run("gh", "pr", "checks", number)
        if code != 0:
            # gh prints a row per check, its fields split by tabs, and an
            # error as one line with no tab. A failure with no row, other than
            # gh's "no checks reported", is GitHub not answering: read as a red
            # check, it sends the reader to a page that is green. Counting tabs
            # would drop the last row, since run() strips the tab before its
            # empty description.
            rows = [line for line in out.splitlines() if "\t" in line]
            if not rows and "no checks reported" not in out:
                raise Unanswered(_first_line(out) or f"gh pr checks exited {code}")
            waiting = [line for line in rows if "\tpass\t" not in line] or [_first_line(out)]
            self.problem(version, "the checks are not all green: " + "; ".join(waiting[:4]))
        return title

    # ------------------------------------------------------- the evidence
    #
    # Two of these gates are other projects' ideas. Only the ideas are taken,
    # and no text is copied (CITATIONS.md, "Release gates from other
    # projects' release processes"). A release held until the code being
    # shipped has passed on hardware is PX4's exit from alpha, where every test card
    # passes across a hardware matrix (the PX4 User Guide's release process,
    # CC BY 4.0). A release held while a defect of the top severity is open
    # is Python's release blocker (PEP 101, public domain), Zephyr's blocker
    # bug (its release process, in a repository under Apache-2.0) and Rust's
    # P-critical (the Rust Forge, MIT or Apache-2.0). The scale itself, S1 to
    # S4, is Emet's (RELEASE_PROCESS.md §1.7), after the triage rules of
    # Everchronicle, the maintainer's other project. Read by the survey of
    # 2026-11-25; each page and licence read again on 2026-11-26.

    def evidence(self, number: str, folder: Path | None = None, *, merge: bool = False) -> None:
        """The release's own evidence, read on the merge day: the plan and
        the body runner's records under `folder`, by default
        `../releases/vX.Y.Z` beside the repository. Each thing missing is a
        problem with the command that fixes it, and `evidence_line` counts
        them. Without `merge` this is the check the plan's Stage 7 exits on;
        with it, Stage 7 must have exited as well.

        It reads files and asks git where a commit sits. It asks GitHub one
        thing, the pull request's body, and only when `before_merge` did not
        get as far as reading it and a patch has an S2 to name; that raises
        Unanswered as every gh question does.
        """
        version = self.release
        if not version:
            self.evidence_line = "body evidence: not read, the packages declare no one version"
            return
        folder = folder or self.default_folder(version)
        before = len(self.problems)
        head = self.git("rev-parse", "HEAD")
        plan = self.plan(version, folder)
        carried: list[str] = []
        if plan is not None:
            self.stages(version, plan, merge=merge)
            carried = self.ledger(version, plan, number)
        runs = read_runs(folder / "body")
        self.body_run(version, folder, runs, head)
        if plan is not None:
            self.flags(version, folder, plan, runs, head)
        else:
            self.evidence_notes.append("flags     not read: no plan names the kind of release or the gaps")
        self.evidence_notes.extend(carried)
        found = len(self.problems) - before
        self.evidence_line = f"body evidence: {found} problem(s)" if found else "body evidence: ok"

    def fix(self, command: str) -> None:
        """One fix for one kind of refusal, however many problems share it."""
        if command not in self.fixes:
            self.fixes.append(command)

    def shown(self, path: Path) -> str:
        """A path as a command typed at the root names it: relative, with
        forward slashes, and quoted when it holds a space."""
        try:
            text = os.path.relpath(path, self.root)
        except ValueError:  # another drive, on Windows
            text = str(path)
        text = text.replace("\\", "/")
        return f'"{text}"' if " " in text else text

    def default_folder(self, version: str) -> Path:
        """The release's folder when `--evidence` names none, and where
        `tools/body/run.py` and `planning/check_plan.py` find it."""
        return self.root.parent / "releases" / f"v{version}"

    def plan_fix(self, version: str, path: Path) -> None:
        """The fix for anything wrong in a plan: the command that checks it,
        run after editing the file. `check_plan.py` reads the default
        folder's plan, so a plan elsewhere is named as the one it does not
        read."""
        checker = self.shown(self.root.parent / "planning" / "check_plan.py")
        default = self.default_folder(version) / "plan.md"
        where = "" if path.resolve() == default.resolve() else f"; it reads {self.shown(default)}, not that file"
        self.fix(f"python {checker} {version}  # after editing {self.shown(path)}{where}")

    def moved(self, version: str, folder: Path) -> str:
        """What a body run made for this check needs after it: nothing when
        the check reads the runner's own folder, or the move into the one it
        reads."""
        default = self.default_folder(version)
        if folder.resolve() == default.resolve():
            return ""
        return f"move the run's folder from {self.shown(default / 'body')} to {self.shown(folder / 'body')}"

    def run_the_head(self, version: str, folder: Path, head: str) -> None:
        """The fix for a head with no passing run, which is the body run of it."""
        moved = self.moved(version, folder)
        then = f"  # then {moved}" if moved else ""
        self.fix(f"python tools/body/run.py --release {version} --commit {head}{then}")

    def plan(self, version: str, folder: Path) -> plan_file.Plan | None:
        """The release's plan, or None when there is none for this version
        to read the stages, the ledger and the flag gaps from. A plan the
        parser finds a problem in is refused and still read: every row it
        kept is as written.

        A plan whose kind its number does not bear out is refused, as
        `planning/blocks.py close` refuses it, and read by the stricter of
        the two: the ledger and the flags read X.Y.0 as a minor or a major
        whatever the plan calls it."""
        path = folder / "plan.md"
        try:
            plan = plan_file.read_plan(path)
        except plan_file.PlanError as error:
            # The parser's text starts with the path, whole; it is named here
            # as every other path is.
            why = str(error).removeprefix(f"{path}: ")
            self.problem(version, f"no plan the merge can read at {self.shown(path)}: {why}")
            if path.is_file():
                self.plan_fix(version, path)
            else:
                template = self.shown(self.root.parent / "planning" / "templates" / "plan.md")
                process = self.shown(self.root.parent / "planning" / "RELEASE_PROCESS.md")
                # Plain ASCII, as every line this composes is: a Windows
                # console shows a section sign as a box.
                self.fix(f"mkdir -p {self.shown(folder)} && cp {template} {self.shown(path)}"
                         f"  # then fill it as {process} section 1.6 says")
            return None
        if plan.problems:
            more = len(plan.problems) - 3
            self.problem(
                version,
                f"{self.shown(path)} has {len(plan.problems)} problem(s) the plan's parser names: "
                + "; ".join(plan.problems[:3]) + (f"; and {more} more" if more > 0 else ""),
            )
            self.plan_fix(version, path)
        if plan.version and plan.version != version:
            self.problem(
                version, f"{self.shown(path)} is the plan for {plan.version}, and the packages declare {version}"
            )
            self.plan_fix(version, path)
            return None
        paged = _paged(version)
        if plan.kind in plan_file.KINDS and (plan.kind in ("minor", "major")) != paged:
            self.problem(
                version,
                f"the plan's kind is {plan.kind}, and {version} is numbered as a "
                f"{'minor or a major' if paged else 'patch'}; the ledger's S2 rule and the flags go by the kind, "
                f"so the two must agree",
            )
            self.plan_fix(version, path)
        return plan

    def stages(self, version: str, plan: plan_file.Plan, *, merge: bool) -> None:
        """Stages 0 to 6 behind the release, and Stage 7 entered. Stage 7
        exits on this very check passing, so only the merge asks for its
        exit, and no kind of release skips it (RELEASE_PROCESS.md §1.1): its
        exit is a date. A stage with no row is the parser's problem, already
        told."""
        before = len(self.problems)
        for number in range(7):
            row = plan_file.stage(plan, number)
            if row is not None and not row.done:
                self.problem(version, f"stage {number}, {row.name}, is neither exited nor skipped in the plan")
        seven = plan_file.stage(plan, 7)
        if seven is not None:
            if not plan_file.is_date(seven.entered):
                self.problem(version, f"stage 7, {seven.name}, is not entered in the plan")
            elif merge and not plan_file.is_date(seven.exited):
                self.problem(
                    version,
                    f"stage 7, {seven.name}, has no exit date in the plan, and the merge waits for one: "
                    f"it exits on this check passing without --merge",
                )
        if len(self.problems) > before:
            self.plan_fix(version, plan.path)

    def ledger(self, version: str, plan: plan_file.Plan, number: str) -> list[str]:
        """An S1 still open or carried holds every merge, and an S2 a minor's
        or a major's (RELEASE_PROCESS.md §1.8, the plan's row). A patch or a
        hotfix merges past an S2 that the pull request's body names; each
        such one is returned, to be printed. X.Y.0 is a minor's or a major's
        number, so a plan that calls it a patch or a hotfix names no S2 past
        the merge."""
        before = len(self.problems)
        carried: list[str] = []
        for row in plan.ledger:
            if not row.unresolved or row.severity not in ("S1", "S2"):
                continue
            what = f"{row.id} is an {row.severity}, {row.state} in the plan's ledger"
            if row.severity == "S1":
                self.problem(version, f"{what}; an S1 holds every merge")
            elif plan.kind not in MAY_NAME_AN_S2 or _paged(version):
                if plan.kind in ("minor", "major"):
                    whose = f"a {plan.kind}'s merge"
                elif plan.kind in MAY_NAME_AN_S2:
                    whose = f"the merge of {version}, numbered as a minor or a major"
                else:
                    whose = "the merge of a plan whose kind is not patch, minor, major or hotfix"
                self.problem(version, f"{what}; an S2 holds {whose}")
            elif self.named(number, row.id):
                carried.append(
                    f"carries   {row.id}, an S2 {row.state} in the plan's ledger and named in #{number}'s body"
                )
            else:
                self.problem(
                    version,
                    f"{what}, and #{number}'s body does not name it; a {plan.kind} merges past an S2 only by naming it",
                )
        if len(self.problems) > before:
            self.plan_fix(version, plan.path)
        return carried

    def named(self, number: str, finding: str) -> bool:
        """Whether the pull request's body names a finding by its ID, whole:
        `L21` does not name `L2`."""
        if self.pr_body is None:
            pr = self.gh("pr", "view", number, "--json", "body")
            self.pr_body = (pr.get("body") or "") if isinstance(pr, dict) else ""
        return re.search(rf"(?<![0-9A-Za-z]){re.escape(finding)}(?![0-9A-Za-z])", self.pr_body) is not None

    def body_run(self, version: str, folder: Path, runs: list[BodyRun], head: str) -> None:
        """A passing body run of the head: the newest by `started` when
        several count, named with the body's hashes. None is a problem that
        says, run by run, what each one lacks. A later run of the head that
        failed is a problem too: the pass before it does not count. So is a
        `run.json` that cannot be read beside a run that counts: it has no
        `started` to place it, and it may be that later run."""
        counting: list[BodyRun] = []
        lacking: list[str] = []
        # Runs newer than the one taken that do not count: each is printed,
        # since a person reading the check should see what came after.
        passed_over: list[str] = []
        for run in runs:
            why = self.short_of_passing(run, version) or self.off_the_head(run, head)
            if why:
                lacking.append(f"{run.name}: {why}")
                if not counting:
                    passed_over.append(f"{run.name}: {why}")
            else:
                counting.append(run)
        if not counting:
            if lacking:
                why = "; ".join(lacking[:4]) + (f"; and {len(lacking) - 4} older" if len(lacking) > 4 else "")
            else:
                why = f"{self.shown(folder / 'body')} holds no run.json"
            self.problem(version, f"no passing body run of the head {head[:7]}: {why}")
            self.run_the_head(version, folder, head)
            return
        taken = counting[0]
        unread = [run for run in runs if run.unread]
        for run in unread:
            record = self.shown(run.folder / "run.json")
            self.problem(version, f"{record} cannot be read ({run.unread}), so whether a newer run of the "
                                  f"head failed is not known")
            self.fix(f"python -m json.tool {record}  # shows where it breaks; mend it, or move its folder out of "
                     f"{self.shown(folder / 'body')}")
        failed_since = self.failed_since(runs, taken, head)
        if failed_since:
            self.problem(version, failed_since)
            self.run_the_head(version, folder, head)
        if unread or failed_since:
            return
        ran = taken.head
        which = f", the newest of {len(counting)} that count" if len(counting) > 1 else ""
        if ran == head:
            where = "the head"
        elif CLOSING_FILE in self.diffed.get(ran, []):
            where = f"the head {head[:7]} but for {CLOSING_FILE}"
        else:
            where = f"the same files as the head {head[:7]}"
        self.evidence_notes.append(
            f"run       {taken.name}{which}: started {taken.started or 'at no recorded time'}, "
            f"{taken.kind} {ran[:7]} ({where}), in {self.shown(taken.folder)}"
        )
        for newer in passed_over[:3]:
            self.evidence_notes.append(f"newer     {newer}")
        if len(passed_over) > 3:
            self.evidence_notes.append(f"newer     and {len(passed_over) - 3} more that do not count")
        body = taken.record.get("body")
        if isinstance(body, dict):
            note = f"; {body['note']}" if isinstance(body.get("note"), str) and body["note"] else ""
            self.evidence_notes.append(
                f"body      manifest {body.get('manifest') or 'unnamed'} sha256 {body.get('manifest_sha256') or 'none'}; "
                f"state {body.get('state') or 'unnamed'} sha256 {body.get('state_sha256') or 'none'}{note}"
            )
        else:
            self.evidence_notes.append("body      the run records no manifest or state hash")

    def failed_since(self, runs: list[BodyRun], taken: BodyRun, head: str) -> str:
        """The problem when a run of the head newer than `taken` failed, or
        nothing. The same code passed once and then failed, on a body that
        may have changed or a bank that does not always pass, and the later
        answer is the one that stands.

        A run is of the head by the test a passing one meets. It failed when
        a step after the code reached the body reads `failed`, whatever the
        verdict says: a run the runner calls not done because the body
        stopped answering may have failed its suites, checks or bank first,
        and keeps those steps. A step `refused` or `not run` did not run the
        code, and neither did a run that failed its preflight, sync or
        install (`BEFORE_THE_CODE`). The run's release and format are not
        asked, since the rule names the source alone.
        """
        for run in runs:  # the newest first
            if run is taken:
                break
            failed = next((step for step in run.steps
                           if step.get("status") == "failed" and step.get("name") not in BEFORE_THE_CODE), None)
            if failed is None or self.off_the_head(run, head):
                continue
            reason = _first_line(str(failed.get("reason") or "")) or "no reason recorded"
            return (f"the newest run of the head, {run.name} ({run.started or 'at no recorded time'}), "
                    f"failed {failed.get('name')}: {reason}; a passing run before it does not count")
        return ""

    def short_of_passing(self, run: BodyRun, version: str) -> str:
        """What `run` lacks to be a passing run of this release, whatever it
        ran: the first thing found, or nothing."""
        if run.unread:
            return f"run.json cannot be read ({run.unread})"
        if run.format < RUN_FORMAT_FROM:
            return (f"its format is {run.record.get('format')!r}, and the merge reads "
                    f"emet-body-run/{RUN_FORMAT_FROM} or later")
        if run.release != version:
            return f"it is a run of {run.release or 'no named release'}"
        for name in RUN_STEPS:
            step = run.step(name)
            if step is None:
                return f"it has no {name} step"
            if step.get("status") != "passed":
                return f"its {name} step reads {step.get('status')!r}, not 'passed'"
        details = (run.step("bank") or {}).get("details")
        replayed = details.get("replayed") if isinstance(details, dict) else None
        if not isinstance(replayed, int) or isinstance(replayed, bool) or replayed < 1:
            return f"its bank step held no take to a golden (details.replayed is {replayed!r})"
        failed = [str(step.get("name")) for step in run.steps if step.get("status") == "failed"]
        if failed:
            return f"its {' and '.join(failed)} step{'s' if len(failed) > 1 else ''} failed"
        return ""

    def off_the_head(self, run: BodyRun, head: str) -> str:
        """Why the source of `run` is not the head, or nothing when it is.

        A pushed commit is the head when it is the checkout's `HEAD`, or
        when `git diff --name-only` between the two names nothing but
        `CHANGELOG.md`. A working tree is the head when its commit passes
        that test and the runner found no file changed, added or removed.
        """
        if not run.kind:
            return "its source records no kind, and a run counts from a pushed commit or a working tree"
        if run.kind not in (PUSHED, TREE):
            return f"its source is {run.kind!r}, neither a pushed commit nor a working tree"
        ran = run.head
        if not ran:
            return "its source names no commit"
        if run.kind == TREE:
            loose: list[str] = []
            for key in ("changed", "added", "removed"):
                names = run.source.get(key)
                if not isinstance(names, list):
                    return f"it ran a working tree and does not say which files it {key}"
                loose.extend(str(name) for name in names)
            if loose:
                return f"it ran a working tree {len(loose)} file(s) away from {ran[:7]}: {_some(loose)}"
        if ran == head:
            return ""
        code, out = self.run("git", "diff", "--name-only", ran, "HEAD")
        if code != 0:
            return f"it ran {ran[:7]}, which git cannot compare with the head ({_first_line(out)}); git fetch origin"
        names = [name.strip() for name in out.splitlines() if name.strip()]
        self.diffed[ran] = names
        others = [name for name in names if name != CLOSING_FILE]
        if others:
            return f"it ran {ran[:7]}, which differs from the head in {_some(others)}"
        return ""

    def flags(self, version: str, folder: Path, plan: plan_file.Plan, runs: list[BodyRun], head: str) -> None:
        """Every body flag run in this release, or answered for. The table
        is printed either way, each flag with its run and that run's commit,
        since a flag keeps a run made before a later fix on the same branch.
        A hotfix is not held to it (RELEASE_PROCESS.md §1.1), and X.Y.0 is
        never a hotfix's number."""
        rows = body_flag_rows()
        asked = plan.kind != "hotfix" or _paged(version)
        # A flag written twice keeps the row that carries the dated answer.
        gaps: dict[str, plan_file.FlagGap] = {}
        for gap in plan.flag_gaps:
            held = gaps.get(_flag_key(gap.flag))
            if held is None or not (held.answer and plan_file.is_date(held.date)):
                gaps[_flag_key(gap.flag)] = gap
        known = {_flag_key(row.label) for row in rows}
        table = [f"{'flag':<28} {'how':<7} {'run':<14} commit"]
        if not asked:
            table.insert(0, "flags     a hotfix is not held to them; the table is for the record")
        # Each flag not run, with why. The flags no run names at all share one
        # problem, where it would otherwise be said once a flag: before the
        # release's window that is every flag.
        told: list[tuple[str, str]] = []
        nowhere: list[str] = []
        for row in rows:
            label = row.label
            ran, why = self.flag_run(label, bool(getattr(row, "live_only", False)), version, runs, head)
            gap = gaps.get(_flag_key(label))
            if ran is not None:
                run, how = ran
                table.append(f"{label:<28} {how:<7} {run.name:<14} {run.head[:7]}")
            elif gap is not None and gap.answer and plan_file.is_date(gap.date):
                table.append(f"{label:<28} {'gap':<7} {'the plan':<14} {gap.answer} ({gap.date})")
            else:
                table.append(f"{label:<28} {'-':<7} not run")
                if gap is not None:
                    why = (f"{why}; " if why else "") + "its row in the plan's flag-gap table has no answer with a date"
                if why:
                    told.append((label, why))
                else:
                    if not nowhere:
                        told.append(("", ""))
                    nowhere.append(label)
        for key, gap in gaps.items():
            if key not in known:
                table.append(f"the plan's flag-gap table names {gap.flag!r}, which is no flag of tools/body/run.py")
        self.evidence_notes.extend(table)
        if not asked:
            return
        body = self.shown(folder / "body")
        for label, why in told:
            if label:
                self.problem(version, f"the flag {label!r} was not run on the body in this release: {why}")
            elif len(nowhere) == 1:
                self.problem(
                    version,
                    f"the flag {nowhere[0]!r} was not run on the body in this release: "
                    f"no run under {body} has it in flags_run",
                )
            else:
                names = ", ".join(repr(name) for name in nowhere[:-1]) + f" and {nowhere[-1]!r}"
                self.problem(
                    version,
                    f"{len(nowhere)} flags were not run on the body in this release, {names}: "
                    f"no run under {body} has one of them in flags_run",
                )
        if told:
            moved = self.moved(version, folder)
            self.fix(
                f"python tools/body/run.py --release {version} --commit {head} --window <START> <END>"
                f"  # in a window named in a reply first{', then ' + moved if moved else ''}; or the maintainer's"
                f" dated answer under \"Flag not run\" in {self.shown(plan.path)}"
            )

    def flag_run(
        self, label: str, live_only: bool, version: str, runs: list[BodyRun], head: str
    ) -> tuple[tuple[BodyRun, str] | None, str]:
        """The newest run that exercised a flag in this release, with how;
        or None and why the newest run that names the flag does not count,
        with no reason when no run names it at all.

        A run counts for a flag when its `flags_run` names the flag, live
        for a flag only a live run shows, it is a run of this release, and
        it ran a pushed commit that is the head or one of this branch's
        own: an ancestor of the head that is not an ancestor of the base.
        """
        nearest = ""
        for run in runs:
            hows = {how for flag, how in run.flags_run if flag == label}
            if not hows:
                continue
            how = "live" if "live" in hows else "replay" if "replay" in hows else ""
            if not how:
                why = f"{run.name} does not say whether it ran it live or from a file"
            elif live_only and how != "live":
                why = f"{run.name} ran it from a file only, and only a live run counts for it"
            elif run.format < RUN_FORMAT_FROM:
                why = (f"{run.name} is {run.record.get('format')!r}, and flags_run is read from "
                       f"emet-body-run/{RUN_FORMAT_FROM} on")
            elif run.release != version:
                why = f"{run.name} is a run of {run.release or 'no named release'}"
            elif not run.kind:
                why = f"{run.name}'s source records no kind, and a flag counts on a pushed commit"
            elif run.kind != PUSHED:
                ran = "a working tree" if run.kind == TREE else f"what it calls {run.kind!r}"
                why = f"{run.name} ran {ran}, and a flag counts on a pushed commit"
            elif not run.head:
                why = f"{run.name} names no commit"
            else:
                why = self.outside_release(run.head, head)
                why = f"{run.name} ran {why}" if why else ""
            if not why:
                return (run, how), ""
            nearest = nearest or why
        return None, nearest

    def outside_release(self, sha: str, head: str) -> str:
        """Why commit `sha` is no commit of this release's branch, or nothing
        when it is one: the head, or an ancestor of the head that is not an
        ancestor of the base. `git merge-base --is-ancestor` exits 0 for
        yes, 1 for no, and otherwise for a commit or a ref git does not have
        (git 2.52.0, verified 2026-11-26). Then nothing is proved either way:
        the run does not count, and the reason says to fetch."""
        if sha == head:
            return ""
        if sha not in self.placed:
            code, out = self.run("git", "merge-base", "--is-ancestor", sha, "HEAD")
            if code == 1:
                why = f"{sha[:7]}, which is outside the head's history"
            elif code != 0:
                why = f"{sha[:7]}, which git does not have here ({_first_line(out)}); git fetch origin"
            else:
                code, out = self.run("git", "merge-base", "--is-ancestor", sha, self.ref)
                if code == 0:
                    why = f"{sha[:7]}, which is on {self.ref}: a commit from before this release's branch"
                elif code != 1:
                    why = f"{sha[:7]}, which git cannot compare with {self.ref} ({_first_line(out)}); git fetch origin"
                else:
                    why = ""
            self.placed[sha] = why
        return self.placed[sha]

    def merge(self, number: str, title: str) -> int:
        """Squash-merge pull request N under `title`, with a sign-off body.

        The sign-off is git's own identity, and it lands in master's history,
        so it has to be an address that may be public: a GitHub noreply
        address or the project's.
        """
        _, name = self.run("git", "config", "user.name")
        _, email = self.run("git", "config", "user.email")
        if not name or not email:
            print("release-audit: git has no user.name or user.email to sign the merge with", file=sys.stderr)
            return 1
        if not (email.endswith("@users.noreply.github.com") or email == PROJECT_ADDRESS):
            print(
                f"release-audit: git's user.email is {email!r}; the sign-off lands in master's history, "
                f"so it must be a GitHub noreply address or {PROJECT_ADDRESS}",
                file=sys.stderr,
            )
            return 1
        if not self.head:
            print("release-audit: no head commit was checked, so there is nothing to pin the merge to", file=sys.stderr)
            return 1
        print(f"release-audit: merging #{number} at {self.head[:7]} as {title!r}")
        # `--match-head-commit`: GitHub refuses the merge if the pull request
        # moved after it was checked.
        return subprocess.run(
            ["gh", "pr", "merge", number, "--squash", "--subject", title,
             "--body", f"Signed-off-by: {name} <{email}>", "--match-head-commit", self.head,
             "--delete-branch"],
            cwd=self.root,
        ).returncode


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in version.split(".") if n.isdigit())


def _paged(version: str) -> bool:
    """Whether a version is numbered as a minor or a major, X.Y.0, which
    has a release page (`Audit.page`); any other number is a patch's."""
    return _numbers(version)[2:3] == (0,)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _some(names: list[str], limit: int = 3) -> str:
    """The first few of a list of files, and how many more there are."""
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f" and {more} more" if more > 0 else "")


def _flag_key(label: str) -> str:
    """A flag's label as a plan and the runner both spell it: the plan may
    write it as code, in another case, or with its spaces doubled."""
    return " ".join(label.replace("`", "").split()).casefold()


def _cleaned(message: str) -> str:
    """A message as git's default cleanup leaves it: `#` lines dropped and
    runs of blank lines folded to one."""
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    folded: list[str] = []
    for line in lines:
        if line.strip() or (folded and folded[-1].strip()):
            folded.append(line.rstrip())
    return "\n".join(folded)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="release_audit.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--complete", action="store_true", help="fail on a newest version still pending")
    parser.add_argument("--pr", metavar="N", help="check pull request N before merging it, instead")
    parser.add_argument("--merge", action="store_true", help="with --pr: merge it, only when every check passed")
    parser.add_argument("--ci", action="store_true", help="with --pr: only what a CI run can hold on every push")
    parser.add_argument("--evidence", metavar="DIR",
                        help="with --pr, on the merge day: the release's folder, with its plan.md and the body's "
                             "runs under body/ (default: ../releases/vX.Y.Z beside the repository)")
    parser.add_argument("--ref", default="origin/master", help="the history to audit (default: origin/master)")
    parser.add_argument("--changelog", default="CHANGELOG.md", help="the changelog to audit against")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv[1:])
    if (args.merge or args.ci) and not args.pr:
        parser.error("--merge and --ci go with --pr N")
    if args.merge and args.ci:
        parser.error("--merge is for the merge day, --ci for every push; not both")
    if args.evidence and (not args.pr or args.ci):
        parser.error("--evidence goes with --pr N on the merge day; a CI run and the history audit read none")
    # A title quoted back may hold what a Windows console cannot print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    root = Path(args.root).resolve()
    audit = Audit(root, (root / args.changelog).resolve(), args.ref)
    if not audit.start():
        return 1
    title: str | None = None
    try:
        if args.pr:
            title = audit.before_merge(args.pr, ci=args.ci)
            if args.ci:
                audit.evidence_line = "body evidence: checked at merge, on the laptop"
            else:
                audit.evidence(
                    args.pr, (root / args.evidence).resolve() if args.evidence else None, merge=args.merge
                )
            scope = f"#{args.pr} " + ("as CI can see it" if args.ci else "before its merge")
        else:
            audit.history()
            scope = f"every version on {args.ref} since {'.'.join(map(str, FIRST))}"
    except Unanswered as unanswered:
        # This line alone: the problems found before it are part of an
        # audit, and a count of them would read as the whole.
        print(
            f"release-audit: GitHub did not answer ({unanswered}); run it again"
            + (". Nothing was merged." if args.merge else ""),
            file=sys.stderr,
        )
        return 1

    for note in audit.pending:
        print(f"release-audit: pending, {note}")
    for msg in audit.problems:
        print(f"  x {msg}")
    if args.complete:
        for note in audit.pending:
            print(f"  x not finished: {note}")
    for fix in audit.fixes:
        print(f"\n  fix: {fix}")
    if audit.evidence_line:
        # After the problems, and counting its own only: without --merge
        # this line is what Stage 7 exits on, on a pull request still a draft.
        if audit.problems:
            print()
        print(audit.evidence_line)
        for note in audit.evidence_notes:
            print(f"  {note}")
    failed = bool(audit.problems) or (args.complete and bool(audit.pending))
    if failed:
        count = len(audit.problems) + (len(audit.pending) if args.complete else 0)
        print(f"\n{count} problem(s) in {scope}." + (" Nothing was merged." if args.merge else ""))
        return 1
    print(f"release-audit: ok, {scope}")
    if args.merge and title is not None:
        return audit.merge(args.pr, title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
