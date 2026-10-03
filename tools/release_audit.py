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

`--merge` with `--pr` merges pull request N, squashed under its title with a
sign-off body and pinned to the head commit it checked, only when every check
passed. 0.5.1 went out with the wrong date because the check and
`gh pr merge` were two lines of one pasted block, and the second ran after
the first refused.

`--ci` with `--pr` asks only the five questions every push can hold, for the
CI run on a pull request. A Dependabot pull request fails it, as it should:
merged as it stands it puts a commit on master that is not a version.

Read-only, except that `--merge` runs `gh pr merge`: git and the GitHub CLI
(`gh`, authenticated).

Usage:
    python tools/release_audit.py
    python tools/release_audit.py --complete
    python tools/release_audit.py --pr 12
    python tools/release_audit.py --pr 12 --merge
    python tools/release_audit.py --pr 12 --ci
Exit:   0 nothing wrong (and merged, with --merge), 1 a problem found, GitHub
        not answering, or the merge failed (each is printed).
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_release import PACKAGES, changelog_section, tag_message  # noqa: E402

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


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


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
    parser.add_argument("--ref", default="origin/master", help="the history to audit (default: origin/master)")
    parser.add_argument("--changelog", default="CHANGELOG.md", help="the changelog to audit against")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    args = parser.parse_args(argv[1:])
    if (args.merge or args.ci) and not args.pr:
        parser.error("--merge and --ci go with --pr N")
    if args.merge and args.ci:
        parser.error("--merge is for the merge day, --ci for every push; not both")
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
