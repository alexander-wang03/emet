"""The merge gate's evidence check, in `tools/release_audit.py`, against a
stand-in repository.

No test reaches git, GitHub or a network. `Audit.run` is the seam: here it
answers every `git` and `gh` question from a table (`Stand`) and fails the
test on a question the table has no answer for. `subprocess.run` fails
outright, and `Audit.merge` only records that it was called, so a merge that
should have been refused shows as a call.

Each test starts from a release that is ready to merge: the plan and the
body run in `fixtures/audit/`, copied to a temporary `releases/v0.9.1/`
beside a made-up repository. The release has two runs, as a real one does:
every flag in a window on an earlier commit of the branch, then a plain run
of the head. A test changes one thing and sees the audit refuse it in its
own words, or pass it, and nothing else.

The flags are the runner's own, `FLAG_ROWS` of `tools/body/run.py`, read as
the audit reads them. No label is written here.
"""

from __future__ import annotations

import copy
import datetime
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import plan_file  # noqa: E402
import release_audit  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "audit"
PLAN = (FIXTURES / "plan.md").read_text(encoding="utf-8")
RUN = json.loads((FIXTURES / "run.json").read_text(encoding="utf-8"))

#: `Project Emet/`, the folder the repository sits in, where the real
#: releases keep their runs. CI has no such folder.
REAL_RUNS = Path(__file__).resolve().parents[3] / "releases" / "v0.5.5" / "body"

VERSION = "0.9.1"
TITLE = f"{VERSION}: Teach it to count"
TODAY = "2026-12-01"

CHANGELOG = f"""\
# Changelog

## [{VERSION}] - {TODAY}

Teach it to count.

### Added

- It counts to ten (#12).

## [0.9.0] - 2026-11-01

Give it numbers.

### Added

- Numbers (#11).
"""


def commit(start: str) -> str:
    return start + "0" * (40 - len(start))


HEAD = commit("1a2b3c4")       # the checkout's head, and the pull request's
EARLIER = commit("2b3c4d5")    # an earlier commit of the same branch
ON_MASTER = commit("3c4d5e6")  # a commit of the base, which the branch grew from
ELSEWHERE = commit("4d5e6f7")  # a commit git has, on another branch
CLOSING = commit("6f7a8b9")    # the commit before the entry's closing line
UNFETCHED = commit("5e6f7a8")  # a commit git does not have here

def plan_shown(version: str = VERSION) -> str:
    return f"../releases/v{version}/plan.md"


def body_shown(version: str = VERSION) -> str:
    return f"../releases/v{version}/body"


def plan_fix(version: str = VERSION) -> str:
    return f"python ../planning/check_plan.py {version}  # after editing {plan_shown(version)}"


def head_fix(version: str = VERSION) -> str:
    return f"python tools/body/run.py --release {version} --commit {HEAD}"


def flags_fix(version: str = VERSION) -> str:
    return (f"{head_fix(version)} --window <START> <END>  # in a window named in a reply first; or the "
            f"maintainer's dated answer under \"Flag not run\" in {plan_shown(version)}")


PLAN_SHOWN, BODY_SHOWN = plan_shown(), body_shown()
EDIT_PLAN, RUN_THE_HEAD, RUN_THE_FLAGS = plan_fix(), head_fix(), flags_fix()

#: The number each kind of release carries here: a minor's and a major's end
#: in 0, and a patch's and a hotfix's do not (the audit's `_paged`).
NUMBERED = {"patch": VERSION, "hotfix": VERSION, "minor": "0.10.0", "major": "1.0.0"}

#: What the audit says of the windowed run when it looks for a run of the
#: head: it ran an earlier commit, with code the head has since changed.
EARLIER_RUN = "2026-11-29: it ran 2b3c4d5, which differs from the head in tools/count.py"

ROWS = release_audit.body_flag_rows()
LIVE_ONLY = next(row.label for row in ROWS if row.live_only)
#: A flag the runner may write live or from a file: neither live only nor,
#: as `--replay` is, from a file only.
EITHER = next(row.label for row in ROWS if not row.live_only and not getattr(row, "replay_only", False))
#: A flag whose label holds a space.
SPACED = next(row.label for row in ROWS if " " in row.label)

KIND = "- **Kind:** patch, one batch."
STAGE_3 = "| 3. Review and fix | 2026-11-24 | 2026-11-26 |"
STAGE_7 = "| 7. Candidate and sitting | 2026-11-29 | 2026-11-30 |"
GAPS = "| Flag not run | Reason | Answer | Date |\n|---|---|---|---|\n"
L3 = "| L3 | Stage 4, 2026-11-27, the body | S4 | On the Pi | none | closed: a duplicate of L2 | |\n"


def finding(severity: str, state: str = "open") -> str:
    """Plan text that adds finding L4 to the ledger."""
    return L3 + f"| L4 | Stage 4, 2026-11-27, the body | {severity} | On the Pi | none yet | {state} | |\n"


def flags_run(*, without: tuple[str, ...] = (), how: dict[str, str] | None = None) -> list[dict[str, str]]:
    """What a windowed run writes: every flag of the runner's table, live
    where only a live run counts and from a file elsewhere."""
    how = how or {}
    return [
        {"flag": row.label, "how": how.get(row.label, "live" if row.live_only else "replay"),
         "step": "live-stats" if row.live_only else "speaker-speak"}
        for row in ROWS if row.label not in without
    ]


def a_run(**changes) -> dict:
    """The fixture's run, a passing plain run of the head, with these
    top-level fields changed."""
    record = copy.deepcopy(RUN)
    record.update(changes)
    return record


def step(record: dict, name: str) -> dict:
    return next(s for s in record["steps"] if s["name"] == name)


def pushed(head: str) -> dict:
    return {"kind": "pushed commit", "ref": "teach-it-to-count", "head": head, "subject": "Count to ten"}


def tree(head: str, changed: list[str] | None = None) -> dict:
    return {"kind": "working tree", "head": head, "subject": "Count to ten", "files": 199,
            "changed": changed or [], "added": [], "removed": []}


class Today(datetime.date):
    @classmethod
    def today(cls) -> "Today":
        return cls.fromisoformat(TODAY)


def no_subprocess(*args, **kwargs):
    raise AssertionError(f"a test reached a real subprocess: {args}")


class Stand:
    """git and gh as the audit sees them through `Audit.run`: every answer
    from this table, and every question kept in `asked`."""

    def __init__(self) -> None:
        self.head = HEAD
        self.branch = [EARLIER, CLOSING]
        self.master = [ON_MASTER]
        self.elsewhere = [ELSEWHERE]
        #: The files each commit differs from the head in.
        self.differs = {
            EARLIER: ["CHANGELOG.md", "tools/count.py"],
            CLOSING: ["CHANGELOG.md"],
            ON_MASTER: ["CHANGELOG.md", "emet-sdk/pyproject.toml", "tools/count.py"],
            ELSEWHERE: ["README.md"],
        }
        self.pr = {
            "state": "OPEN", "isDraft": False, "title": TITLE, "body": "It counts to ten.",
            "headRefOid": HEAD, "baseRefName": "master", "mergeable": "MERGEABLE",
        }
        self.checks = (0, "layering\tpass\t1m2s\thttps://example.invalid/1")
        self.dirty = ""
        #: GitHub has no such pull request, in gh's own words.
        self.no_pull_request = False
        self.asked: list[tuple[str, ...]] = []

    def has(self, sha: str) -> bool:
        return sha in (self.head, *self.branch, *self.master, *self.elsewhere)

    def answer(self, *args: str) -> tuple[int, str]:
        self.asked.append(args)
        match args:
            case ("gh", "repo", "view", *_):
                return 0, "example/emet"
            case ("git", "show", "origin/master:emet-sdk/pyproject.toml"):
                return 0, 'version = "0.9.0"'
            case ("git", "show", spec):
                return 128, f"fatal: invalid object name '{spec.partition(':')[0]}'."
            case ("gh", "pr", "view", "12", "--json", fields):
                if self.no_pull_request:
                    return 1, "GraphQL: Could not resolve to a PullRequest with the number of 12. (repository.pullRequest)"
                return 0, json.dumps({field: self.pr[field] for field in fields.split(",")})
            case ("git", "ls-remote", *_):
                return 2, ""
            case ("git", "tag", "--list", _):
                return 0, ""
            case ("git", "rev-parse", "HEAD"):
                return 0, self.head
            case ("git", "status", "--porcelain", "--untracked-files=no"):
                return 0, self.dirty
            case ("gh", "pr", "checks", "12"):
                return self.checks
            case ("git", "diff", "--name-only", sha, "HEAD"):
                if not self.has(sha):
                    return 128, f"fatal: bad object {sha}"
                return 0, "\n".join(self.differs.get(sha, []))
            case ("git", "merge-base", "--is-ancestor", sha, onto):
                if not self.has(sha):
                    return 128, f"fatal: Not a valid commit name {sha}"
                if onto == "HEAD":
                    return (0 if sha in (self.head, *self.branch, *self.master) else 1), ""
                if onto == "origin/master":
                    return (0 if sha in self.master else 1), ""
                return 128, f"fatal: Not a valid object name {onto}"
        raise AssertionError(f"the audit asked what the stand-in cannot answer: {args}")

    def asked_git(self, *start: str) -> list[tuple[str, ...]]:
        return [args for args in self.asked if args[: len(start) + 1] == ("git", *start)]


class World:
    """A repository at 0.9.1, its release folder beside it, and the stand-in
    for git and GitHub."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys) -> None:
        self.capsys = capsys
        self.root = tmp_path / "emet"
        self.version = VERSION
        self.release = tmp_path / "releases" / f"v{VERSION}"
        self.plan_path = self.release / "plan.md"
        self.stand = Stand()
        self.merged: list[tuple[str, str, str]] = []
        for package in release_audit.PACKAGES:
            self.declare(package, VERSION)
        (self.root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        self.release.mkdir(parents=True)
        self.plan_path.write_text(PLAN, encoding="utf-8")
        self.run("2026-11-29", a_run(started="2026-11-29T14:00:05", window=["2026-11-29T14:00", "2026-11-29T15:00"],
                                     source=pushed(EARLIER), flags_run=flags_run()))
        self.run("2026-11-30", a_run())

        stand, merged = self.stand, self.merged

        def merge(audit, number: str, title: str) -> int:
            merged.append((number, title, audit.head))
            return 0

        monkeypatch.setattr(release_audit.Audit, "run", lambda audit, *args: stand.answer(*args))
        monkeypatch.setattr(release_audit.Audit, "merge", merge)
        monkeypatch.setattr(release_audit.subprocess, "run", no_subprocess)
        monkeypatch.setattr(release_audit, "datetime", types.SimpleNamespace(date=Today))

    def declare(self, package: str, version: str) -> None:
        (self.root / package).mkdir(parents=True, exist_ok=True)
        (self.root / package / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "{version}"\n', encoding="utf-8")

    def plan_swap(self, old: str, new: str) -> None:
        text = self.plan_path.read_text(encoding="utf-8")
        assert old in text, f"the fixture plan has no {old!r}"
        self.plan_path.write_text(text.replace(old, new), encoding="utf-8")

    def renumber(self, version: str) -> None:
        """Move the release from 0.9.1 to `version`: the four packages, the
        entry, the folder with its plan and its runs, and the pull
        request's title. A run a test writes after this passes
        `release=world.version` itself."""
        for package in release_audit.PACKAGES:
            self.declare(package, version)
        changelog = self.root / "CHANGELOG.md"
        changelog.write_text(changelog.read_text(encoding="utf-8").replace(f"[{VERSION}]", f"[{version}]"),
                             encoding="utf-8")
        moved = self.release.parent / f"v{version}"
        self.release.rename(moved)
        self.version, self.release, self.plan_path = version, moved, moved / "plan.md"
        for path in (self.plan_path, *moved.glob("body/*/run.json")):
            path.write_text(path.read_text(encoding="utf-8").replace(VERSION, version), encoding="utf-8")
        self.stand.pr["title"] = f"{version}: Teach it to count"

    def kind(self, kind: str) -> None:
        """Make the release this kind, under the number that kind carries."""
        self.plan_swap(KIND, f"- **Kind:** {kind}")
        if NUMBERED[kind] != VERSION:
            self.renumber(NUMBERED[kind])

    def run(self, name: str, record: dict | str) -> Path:
        folder = self.release / "body" / name
        folder.mkdir(parents=True, exist_ok=True)
        text = record if isinstance(record, str) else json.dumps(record, indent=2) + "\n"
        (folder / "run.json").write_text(text, encoding="utf-8")
        return folder

    def no_run(self, name: str) -> None:
        shutil.rmtree(self.release / "body" / name)

    def audit(self, *options: str) -> tuple[int, str]:
        code = release_audit.main(["release_audit.py", "--pr", "12", "--root", str(self.root), *options])
        out = self.capsys.readouterr().out
        # A Windows console shows anything else as a box, in a line a person
        # is meant to paste.
        assert out.isascii(), [line for line in out.splitlines() if not line.isascii()]
        return code, out


@pytest.fixture
def world(tmp_path, monkeypatch, capsys) -> World:
    return World(tmp_path, monkeypatch, capsys)


def problems(out: str) -> list[str]:
    return [line[4:] for line in out.splitlines() if line.startswith("  x ")]


def fixes(out: str) -> list[str]:
    return [line[7:] for line in out.splitlines() if line.startswith("  fix: ")]


def said(out: str) -> str:
    """The line the evidence check ends on."""
    lines = [line for line in out.splitlines() if line.startswith("body evidence:")]
    assert len(lines) == 1, out
    return lines[0]


def notes(out: str) -> list[str]:
    """What the audit prints under its evidence line: the run, the body's
    hashes, the flag table and each S2 carried."""
    lines = out.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("body evidence:"))
    kept = []
    for line in lines[at + 1:]:
        if not line.startswith("  "):
            break
        kept.append(line[2:])
    return kept


def flag_line(out: str, label: str) -> list[str]:
    """The flag table's row for a flag, split on runs of spaces after the
    label."""
    row = next(line for line in notes(out) if line.startswith(f"{label:<28} "))
    return row[29:].split()


def refused(world: World, *expected: str, fix: str) -> str:
    """The merge is refused for exactly these evidence problems, the fix is
    printed once, and nothing was merged."""
    code, out = world.audit("--merge")
    assert problems(out) == [f"{world.version}: {problem}" for problem in expected], out
    assert said(out) == f"body evidence: {len(expected)} problem(s)"
    assert fixes(out) == [fix]
    assert out.rstrip().endswith(f"{len(expected)} problem(s) in #12 before its merge. Nothing was merged.")
    assert code == 1
    assert world.merged == []
    return out


def no_run_of_the_head(*lacking: str) -> str:
    return "no passing body run of the head 1a2b3c4: " + "; ".join(lacking)


def not_run(label: str, why: str) -> str:
    return f"the flag {label!r} was not run on the body in this release: {why}"


# ------------------------------------------------------------ what passes


def test_a_release_with_its_plan_its_ledger_and_its_runs_merges(world):
    code, out = world.audit("--merge")
    assert problems(out) == []
    assert said(out) == "body evidence: ok"
    assert f"release-audit: ok, #12 before its merge" in out.splitlines()
    assert code == 0
    assert world.merged == [("12", TITLE, HEAD)]


def test_a_passing_check_prints_the_run_the_bodys_hashes_and_the_flag_table(world):
    _, out = world.audit()
    printed = notes(out)
    assert printed[0] == (f"run       2026-11-30: started 2026-11-30T10:02:11, pushed commit 1a2b3c4 (the head), "
                          f"in {BODY_SHOWN}/2026-11-30")
    assert printed[1] == (f"body      manifest ~/pi-speakerphone.yaml sha256 {RUN['body']['manifest_sha256']}; "
                          f"state ~/.local/state/emet/pi_speakerphone.json sha256 {RUN['body']['state_sha256']}")
    assert printed[2].split() == ["flag", "how", "run", "commit"]
    # A flag keeps the run made before a later fix on the same branch, and
    # the table names that run's commit.
    for row in ROWS:
        assert flag_line(out, row.label) == ["live" if row.live_only else "replay", "2026-11-29", "2b3c4d5"]
    assert len(printed) == 3 + len(ROWS)


def test_the_check_without_merge_merges_nothing(world):
    code, out = world.audit()
    assert (code, said(out)) == (0, "body evidence: ok")
    assert world.merged == []


def test_the_folder_can_be_named_and_is_read_from_the_root(world, tmp_path):
    (tmp_path / "releases").rename(tmp_path / "kept")
    code, out = world.audit("--evidence", f"../kept/v{VERSION}")
    assert (code, said(out)) == (0, "body evidence: ok")
    assert notes(out)[0].endswith(f"in ../kept/v{VERSION}/body/2026-11-30")

    code, out = world.audit()
    assert code == 1
    assert said(out) == "body evidence: 2 problem(s)"
    assert problems(out)[1] == f"{VERSION}: " + no_run_of_the_head(f"{BODY_SHOWN} holds no run.json")


def test_a_path_with_a_space_is_quoted_where_a_command_names_it(world, tmp_path):
    (tmp_path / "releases").rename(tmp_path / "kept here")
    (tmp_path / "kept here" / f"v{VERSION}" / "plan.md").unlink()
    code, out = world.audit("--merge", "--evidence", f"../kept here/v{VERSION}")
    assert fixes(out) == [
        f'mkdir -p "../kept here/v{VERSION}" && cp ../planning/templates/plan.md "../kept here/v{VERSION}/plan.md"'
        f"  # then fill it as ../planning/RELEASE_PROCESS.md section 1.6 says"
    ]
    assert code == 1 and world.merged == []


def test_each_fix_for_a_named_folder_says_where_the_run_and_the_check_read(world, tmp_path):
    # The runner writes under ../releases/vX.Y.Z/body/ and check_plan.py
    # reads ../releases/vX.Y.Z/plan.md, whatever folder the audit was given.
    (tmp_path / "releases").rename(tmp_path / "kept")
    kept = f"../kept/v{VERSION}"
    (tmp_path / "kept" / f"v{VERSION}" / "body" / "2026-11-30").joinpath("run.json").unlink()
    (tmp_path / "kept" / f"v{VERSION}" / "body" / "2026-11-29").joinpath("run.json").unlink()
    plan = tmp_path / "kept" / f"v{VERSION}" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace(STAGE_3, "| 3. Review and fix | 2026-11-24 | |"),
                    encoding="utf-8")
    code, out = world.audit("--merge", "--evidence", kept)
    moved = f"move the run's folder from {BODY_SHOWN} to {kept}/body"
    assert fixes(out) == [
        f"python ../planning/check_plan.py {VERSION}  # after editing {kept}/plan.md; it reads {PLAN_SHOWN}, "
        f"not that file",
        f"{RUN_THE_HEAD}  # then {moved}",
        f"{RUN_THE_HEAD} --window <START> <END>  # in a window named in a reply first, then {moved}; or the "
        f"maintainer's dated answer under \"Flag not run\" in {kept}/plan.md",
    ]
    assert code == 1 and world.merged == []


def test_a_folder_on_another_drive_is_named_whole(world, monkeypatch):
    def another_drive(path, start):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    world.plan_swap(KIND, "- **Kind:** hotfix")
    shutil.rmtree(world.release / "body")
    with monkeypatch.context() as patched:
        patched.setattr(release_audit.os.path, "relpath", another_drive)
        code, out = world.audit("--merge")
    whole = str(world.release / "body").replace("\\", "/")
    whole = f'"{whole}"' if " " in whole else whole
    assert problems(out) == [f"{VERSION}: " + no_run_of_the_head(f"{whole} holds no run.json")]
    assert code == 1 and world.merged == []


# ----------------------------------------------------------- E1, the plan


def test_no_plan_is_refused_with_the_template_to_copy(world):
    world.plan_path.unlink()
    out = refused(
        world, f"no plan the merge can read at {PLAN_SHOWN}: no plan file there",
        fix=(f"mkdir -p ../releases/v{VERSION} && cp ../planning/templates/plan.md {PLAN_SHOWN}"
             f"  # then fill it as ../planning/RELEASE_PROCESS.md section 1.6 says"),
    )
    # With no plan nothing names the kind or the gaps, so the flags wait.
    assert "flags     not read: no plan names the kind of release or the gaps" in notes(out)


def test_a_file_with_no_stage_table_is_no_plan(world):
    world.plan_swap("## 2. Stages", "## 2. Steps")
    refused(world, f'no plan the merge can read at {PLAN_SHOWN}: no table under a "## 2. Stages" heading',
            fix=EDIT_PLAN)


def test_a_stage_table_an_unclosed_comment_hides_is_no_plan_and_says_where_it_opens(world):
    world.plan_swap("## 1. Decisions", "## 1. Decisions\n\n<!-- a note that never closes")
    opened = world.plan_path.read_text(encoding="utf-8").split("\n").index("<!-- a note that never closes") + 1
    refused(world, f'no plan the merge can read at {PLAN_SHOWN}: no table under a "## 2. Stages" heading '
                   f"(an HTML comment opens on line {opened} and never closes)", fix=EDIT_PLAN)


def test_a_plan_that_is_not_utf8_is_no_plan_and_says_why(world):
    world.plan_path.write_bytes(b"# 0.9.1: Teach it to count\n\n\xff\xfe\n")
    try:
        b"# 0.9.1: Teach it to count\n\n\xff\xfe\n".decode("utf-8-sig")
    except UnicodeDecodeError as error:
        why = str(error)
    refused(world, f"no plan the merge can read at {PLAN_SHOWN}: the plan cannot be read ({why})", fix=EDIT_PLAN)


def test_a_second_ledger_heading_holding_an_open_s1_is_refused(world):
    # Only the first ledger is read, so the parser names the second, and a
    # plan with a problem never merges.
    world.plan_swap("## 5. Register rows touched", (
        "## 4. Ledger, after the freeze\n\n"
        "| ID | Found (stage, date, by) | Severity | Reproduced how | Regression test, or the reason there is none "
        "| State | Fixed in |\n|---|---|---|---|---|---|---|\n"
        "| L9 | Stage 4, 2026-11-28, the body | S1 | On the Pi | none yet | open | |\n\n"
        "## 5. Register rows touched"))
    (told,) = plan_file.read_plan(world.plan_path).problems
    assert 'a second "## 4. Ledger" heading' in told
    refused(world, f"{PLAN_SHOWN} has 1 problem(s) the plan's parser names: {told}", fix=EDIT_PLAN)


def test_a_plan_for_another_version_is_refused(world):
    world.plan_swap("# 0.9.1: Teach it to count", "# 0.9.2: Teach it to count")
    refused(world, f"{PLAN_SHOWN} is the plan for 0.9.2, and the packages declare {VERSION}", fix=EDIT_PLAN)


def kind_against(kind: str, version: str) -> str:
    numbered = "minor or a major" if version.endswith(".0") else "patch"
    return (f"the plan's kind is {kind}, and {version} is numbered as a {numbered}; the ledger's S2 rule and the "
            f"flags go by the kind, so the two must agree")


@pytest.mark.parametrize("kind", ["minor", "major"])
def test_a_minor_or_a_major_numbered_as_a_patch_is_refused(world, kind):
    world.plan_swap(KIND, f"- **Kind:** {kind}")
    refused(world, kind_against(kind, VERSION), fix=EDIT_PLAN)


@pytest.mark.parametrize("kind", ["minor", "major"])
def test_a_minor_or_a_major_numbered_x_y_0_merges(world, kind):
    world.kind(kind)
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert world.merged == [("12", f"{world.version}: Teach it to count", HEAD)]


def test_a_patch_numbered_x_y_0_names_no_s2_past_the_merge(world):
    world.renumber("1.0.0")
    world.plan_swap(L3, finding("S2"))
    world.stand.pr["body"] = "Carries L4."
    refused(world, kind_against("patch", "1.0.0"),
            "L4 is an S2, open in the plan's ledger; an S2 holds the merge of 1.0.0, numbered as a minor or a major",
            fix=plan_fix("1.0.0"))


def test_a_hotfix_numbered_x_y_0_is_held_to_the_flags(world):
    world.renumber("1.0.0")
    world.plan_swap(KIND, "- **Kind:** hotfix")
    world.no_run("2026-11-29")
    code, out = world.audit("--merge")
    told = problems(out)
    assert told[0] == f"1.0.0: {kind_against('hotfix', '1.0.0')}"
    assert told[1].startswith(f"1.0.0: {len(ROWS)} flags were not run on the body in this release")
    assert len(told) == 2
    assert not any("a hotfix is not held" in line for line in notes(out))
    assert code == 1 and world.merged == []


def test_a_plan_with_a_parse_problem_is_refused_in_the_parsers_words(world):
    world.plan_swap("| S3 | By hand |", "| S9 | By hand |")
    (told,) = plan_file.read_plan(world.plan_path).problems
    assert "L2's severity is 'S9'" in told
    refused(world, f"{PLAN_SHOWN} has 1 problem(s) the plan's parser names: {told}", fix=EDIT_PLAN)


def test_a_plan_with_many_parse_problems_names_three_and_counts_the_rest(world):
    for old in ("| S2 | Ran it twice |", "| S3 | By hand |", "| S4 | On the Pi |"):
        world.plan_swap(old, old.replace("| S", "| T"))
    world.plan_swap(KIND, "- **Kind:** quick")
    told = plan_file.read_plan(world.plan_path).problems
    assert len(told) == 4
    code, out = world.audit("--merge")
    assert problems(out)[0] == (f"{VERSION}: {PLAN_SHOWN} has 4 problem(s) the plan's parser names: "
                                + "; ".join(told[:3]) + "; and 1 more")
    assert code == 1 and world.merged == []


# --------------------------------------------------------- E2, the stages


@pytest.mark.parametrize("row, still_open, named", [
    # Both ends of stages 0 to 6, and one between.
    ("| 0. Scope and research | 2026-11-20 | 2026-11-21 |", "| 0. Scope and research | 2026-11-20 | |",
     "stage 0, Scope and research"),
    (STAGE_3, "| 3. Review and fix | 2026-11-24 | |", "stage 3, Review and fix"),
    ("| 6. API and UX gate | 2026-11-28 | 2026-11-29 |", "| 6. API and UX gate | 2026-11-28 | |",
     "stage 6, API and UX gate"),
])
def test_an_earlier_stage_neither_exited_nor_skipped_is_refused(world, row, still_open, named):
    world.plan_swap(row, still_open)
    refused(world, f"{named}, is neither exited nor skipped in the plan", fix=EDIT_PLAN)


def test_a_skipped_stage_is_behind_the_release(world):
    world.plan_swap(STAGE_3, "| 3. Review and fix | | skipped: nothing to review |")
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


def test_stage_7_not_entered_is_refused(world):
    world.plan_swap(STAGE_7, "| 7. Candidate and sitting | | |")
    refused(world, "stage 7, Candidate and sitting, is not entered in the plan", fix=EDIT_PLAN)

    code, out = world.audit()
    assert code == 1
    assert said(out) == "body evidence: 1 problem(s)"


def test_stage_7_entered_passes_the_check_and_waits_at_the_merge(world):
    world.plan_swap(STAGE_7, "| 7. Candidate and sitting | 2026-11-29 | |")
    code, out = world.audit()
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")

    refused(
        world,
        "stage 7, Candidate and sitting, has no exit date in the plan, and the merge waits for one: "
        "it exits on this check passing without --merge",
        fix=EDIT_PLAN,
    )


def test_stage_7_is_never_skipped(world):
    world.plan_swap(STAGE_7, "| 7. Candidate and sitting | 2026-11-29 | skipped: in a hurry |")
    code, out = world.audit("--merge")
    assert code == 1 and world.merged == []
    assert problems(out)[0].startswith(f"{VERSION}: stage 7, Candidate and sitting, has no exit date in the plan")


def test_stages_8_and_9_are_not_the_merges_to_ask(world):
    assert "| 8. Ship | | |" in PLAN and "| 9. Close | | |" in PLAN
    code, _ = world.audit("--merge")
    assert code == 0


# --------------------------------------------------------- E3, the ledger


@pytest.mark.parametrize("state, word", [
    ("open", "open"),
    ("Open, waiting on a word", "open"),
    ("carried to 0.9.2", "carried"),
])
def test_an_s1_open_or_carried_is_refused(world, state, word):
    world.plan_swap(L3, finding("S1", state))
    refused(world, f"L4 is an S1, {word} in the plan's ledger; an S1 holds every merge", fix=EDIT_PLAN)


@pytest.mark.parametrize("state", ["fixed", "closed: could not be reproduced"])
def test_an_s1_fixed_or_closed_holds_nothing(world, state):
    world.plan_swap(L3, finding("S1", state))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


@pytest.mark.parametrize("kind", ["patch", "hotfix", "minor", "major"])
def test_an_s1_is_refused_even_when_the_pull_request_names_it(world, kind):
    world.kind(kind)
    world.plan_swap(L3, finding("S1"))
    world.stand.pr["body"] = "Carries L4."
    refused(world, "L4 is an S1, open in the plan's ledger; an S1 holds every merge", fix=plan_fix(world.version))


@pytest.mark.parametrize("state, word", [("open", "open"), ("carried to 0.9.2", "carried")])
@pytest.mark.parametrize("kind", ["minor", "major"])
def test_an_s2_open_or_carried_is_refused_for_a_minor_or_a_major_named_or_not(world, kind, state, word):
    world.kind(kind)
    world.plan_swap(L3, finding("S2", state))
    world.stand.pr["body"] = "Carries L4."
    refused(world, f"L4 is an S2, {word} in the plan's ledger; an S2 holds a {kind}'s merge",
            fix=plan_fix(world.version))


@pytest.mark.parametrize("kind", ["patch", "hotfix"])
@pytest.mark.parametrize("body", ["It counts to ten.", "Carries L41 and L40.", "See XL4 and L4a."])
def test_an_open_s2_on_a_patch_or_a_hotfix_is_refused_until_the_pull_request_names_it(world, body, kind):
    world.kind(kind)
    world.plan_swap(L3, finding("S2"))
    world.stand.pr["body"] = body
    refused(
        world,
        "L4 is an S2, open in the plan's ledger, and #12's body does not name it; "
        f"a {kind} merges past an S2 only by naming it",
        fix=EDIT_PLAN,
    )


@pytest.mark.parametrize("kind, state, word", [
    ("patch, one batch.", "open", "open"),
    ("hotfix", "carried to 0.9.2", "carried"),
])
def test_a_patch_or_a_hotfix_merges_past_an_s2_it_names_and_the_audit_prints_it(world, kind, state, word):
    world.plan_swap(KIND, f"- **Kind:** {kind}")
    world.plan_swap(L3, finding("S2", state))
    world.stand.pr["body"] = "It counts to ten.\n\nCarries L4 (S2): the count stops at nine on a cold boot."
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert notes(out)[-1] == f"carries   L4, an S2 {word} in the plan's ledger and named in #12's body"
    assert world.merged == [("12", TITLE, HEAD)]


@pytest.mark.parametrize("line", ["- **Kind:** quick", "- **Kind:** patch | minor | major | hotfix", "- **Sort:** patch"])
def test_an_open_s2_holds_a_plan_whose_kind_is_none_of_the_four_named_or_not(world, line):
    world.plan_swap(KIND, line)
    world.plan_swap(L3, finding("S2"))
    world.stand.pr["body"] = "Carries L4."
    code, out = world.audit("--merge")
    told = problems(out)
    assert len(told) == 2 and "the plan's parser names" in told[0]
    assert told[1] == (f"{VERSION}: L4 is an S2, open in the plan's ledger; "
                       f"an S2 holds the merge of a plan whose kind is not patch, minor, major or hotfix")
    assert code == 1 and world.merged == []


def test_a_pull_request_github_does_not_have_names_nothing(world):
    world.plan_swap(L3, finding("S2"))
    world.stand.no_pull_request = True
    unnamed = (f"{VERSION}: L4 is an S2, open in the plan's ledger, and #12's body does not name it; "
               f"a patch merges past an S2 only by naming it")
    # The check before the merge could not read it, and the evidence is read
    # all the same: the body is asked for once more and is still not there.
    code, out = world.audit("--merge")
    assert problems(out) == ["#12 could not be read", unnamed]
    assert said(out) == "body evidence: 1 problem(s)"
    assert world.stand.asked.count(("gh", "pr", "view", "12", "--json", "body")) == 1
    assert code == 1 and world.merged == []


def test_an_open_s3_or_s4_is_logged_and_holds_nothing(world):
    assert "| S3 | By hand | none: a wording fix | open |" in PLAN
    world.plan_swap(L3, finding("S4"))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


def test_the_body_is_asked_of_github_only_when_the_check_before_never_read_it(world):
    world.plan_swap(L3, finding("S2"))
    world.stand.pr["body"] = "Carries L4."
    only_the_body = ("gh", "pr", "view", "12", "--json", "body")

    world.audit()
    assert only_the_body not in world.stand.asked

    # With no changelog entry the check before the merge stops short of the
    # pull request, and the evidence is read all the same.
    (world.root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    world.stand.asked.clear()
    code, out = world.audit()
    assert problems(out) == [f"{VERSION}: CHANGELOG.md has no entry"]
    assert said(out) == "body evidence: ok"
    assert only_the_body in world.stand.asked
    assert code == 1


# ------------------------------------------------- E4, a run of the head


def test_no_run_of_the_head_is_refused_with_the_run_to_make(world):
    world.no_run("2026-11-30")
    refused(world, no_run_of_the_head(EARLIER_RUN), fix=RUN_THE_HEAD)


def test_no_run_json_at_all_is_refused(world):
    # A hotfix, so that the flags, which no run now shows, are not asked.
    world.plan_swap(KIND, "- **Kind:** hotfix")
    shutil.rmtree(world.release / "body")
    refused(world, no_run_of_the_head(f"{BODY_SHOWN} holds no run.json"), fix=RUN_THE_HEAD)


def test_a_format_1_run_does_not_count(world):
    world.run("2026-11-30", a_run(format="emet-body-run/1"))
    refused(
        world,
        no_run_of_the_head("2026-11-30: its format is 'emet-body-run/1', and the merge reads emet-body-run/2 or later",
                           EARLIER_RUN),
        fix=RUN_THE_HEAD,
    )


def test_a_later_format_counts(world):
    world.run("2026-11-30", a_run(format="emet-body-run/3"))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


@pytest.mark.parametrize("named", ["emet-body-run/2.5", "emet-body-run/02x", "body-run/2", None, 2])
def test_a_format_the_audit_cannot_read_does_not_count(world, named):
    world.run("2026-11-30", a_run(format=named))
    code, out = world.audit("--merge")
    assert code == 1 and world.merged == []
    assert f"2026-11-30: its format is {named!r}" in problems(out)[0]


def test_a_run_of_another_release_does_not_count(world):
    world.run("2026-11-30", a_run(release="0.9.0"))
    refused(world, no_run_of_the_head("2026-11-30: it is a run of 0.9.0", EARLIER_RUN), fix=RUN_THE_HEAD)


def test_a_failed_bank_step_does_not_count(world):
    record = a_run()
    step(record, "bank").update(status="failed", reason="replay.py exit 1")
    world.run("2026-11-30", record)
    refused(world, no_run_of_the_head("2026-11-30: its bank step reads 'failed', not 'passed'", EARLIER_RUN),
            fix=RUN_THE_HEAD)


def test_a_suites_step_that_did_not_run_does_not_count(world):
    record = a_run()
    step(record, "suites").update(status="not run", reason="--steps left it out")
    world.run("2026-11-30", record)
    refused(world, no_run_of_the_head("2026-11-30: its suites step reads 'not run', not 'passed'", EARLIER_RUN),
            fix=RUN_THE_HEAD)


def test_a_run_with_no_checks_step_does_not_count(world):
    record = a_run()
    record["steps"].remove(step(record, "checks"))
    world.run("2026-11-30", record)
    refused(world, no_run_of_the_head("2026-11-30: it has no checks step", EARLIER_RUN), fix=RUN_THE_HEAD)


@pytest.mark.parametrize("details, read", [
    ({"replayed": 0, "unreleased": ["v1s1-01"]}, "0"),
    ({"unreleased": []}, "None"),
    ({"replayed": True}, "True"),
    ({"replayed": "13"}, "'13'"),
])
def test_a_bank_step_that_held_no_take_to_a_golden_does_not_count(world, details, read):
    record = a_run()
    step(record, "bank")["details"] = details
    world.run("2026-11-30", record)
    refused(
        world,
        no_run_of_the_head(f"2026-11-30: its bank step held no take to a golden (details.replayed is {read})",
                           EARLIER_RUN),
        fix=RUN_THE_HEAD,
    )


def test_any_failed_step_keeps_a_run_from_counting(world):
    record = a_run()
    step(record, "speaker").update(status="failed", reason="the typescript has no end line")
    record["steps"].append({"name": "soak", "status": "failed", "reason": "not flat", "seconds": 3600.0, "details": {}})
    world.run("2026-11-30", record)
    refused(world, no_run_of_the_head("2026-11-30: its speaker and soak steps failed", EARLIER_RUN),
            fix=RUN_THE_HEAD)


def test_one_failed_step_is_named_alone(world):
    record = a_run()
    record["steps"].append({"name": "budgets", "status": "failed", "reason": "boot 40% over", "seconds": 1.0,
                            "details": {}})
    world.run("2026-11-30", record)
    refused(world, no_run_of_the_head("2026-11-30: its budgets step failed", EARLIER_RUN), fix=RUN_THE_HEAD)


def test_a_refused_or_unrun_step_beside_the_three_still_counts(world):
    record = a_run()
    step(record, "live").update(status="refused", reason="the window ends too soon")
    world.run("2026-11-30", record)
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


def test_a_run_of_a_commit_git_does_not_have_says_to_fetch(world):
    world.run("2026-11-30", a_run(source=pushed(UNFETCHED)))
    refused(
        world,
        no_run_of_the_head(
            f"2026-11-30: it ran 5e6f7a8, which git cannot compare with the head (fatal: bad object {UNFETCHED}); "
            f"git fetch origin",
            EARLIER_RUN,
        ),
        fix=RUN_THE_HEAD,
    )


def test_a_run_whose_commit_differs_only_in_the_changelog_is_a_run_of_the_head(world):
    world.run("2026-11-30", a_run(source=pushed(CLOSING)))
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert notes(out)[0] == (f"run       2026-11-30: started 2026-11-30T10:02:11, pushed commit 6f7a8b9 "
                             f"(the head 1a2b3c4 but for CHANGELOG.md), in {BODY_SHOWN}/2026-11-30")
    assert ("git", "diff", "--name-only", CLOSING, "HEAD") in world.stand.asked


def test_a_commit_with_the_same_files_as_the_head_is_the_head(world):
    world.stand.differs[CLOSING] = []
    world.run("2026-11-30", a_run(source=pushed(CLOSING)))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    # No difference is named where there is none.
    assert notes(out)[0] == (f"run       2026-11-30: started 2026-11-30T10:02:11, pushed commit 6f7a8b9 "
                             f"(the same files as the head 1a2b3c4), in {BODY_SHOWN}/2026-11-30")


@pytest.mark.parametrize("files, told", [
    (["CHANGELOG.md", "tools/count.py"], "tools/count.py"),
    (["docs/CHANGELOG.md"], "docs/CHANGELOG.md"),
    (["a.py", "b.py", "c.py", "d.py", "CHANGELOG.md", "e.py"], "a.py, b.py, c.py and 2 more"),
])
def test_a_difference_in_one_other_file_is_refused(world, files, told):
    world.stand.differs[CLOSING] = files
    world.run("2026-11-30", a_run(source=pushed(CLOSING)))
    refused(world, no_run_of_the_head(f"2026-11-30: it ran 6f7a8b9, which differs from the head in {told}",
                                      EARLIER_RUN),
            fix=RUN_THE_HEAD)


def test_a_working_tree_run_with_no_changed_file_counts(world):
    world.run("2026-11-30", a_run(source=tree(HEAD)))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert "working tree 1a2b3c4 (the head)" in notes(out)[0]


def test_a_working_tree_at_the_closing_commits_parent_counts_too(world):
    world.run("2026-11-30", a_run(source=tree(CLOSING)))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


@pytest.mark.parametrize("key", ["changed", "added", "removed"])
def test_a_working_tree_run_with_a_changed_file_is_refused(world, key):
    source = tree(HEAD)
    source[key] = ["tools/count.py"]
    world.run("2026-11-30", a_run(source=source))
    refused(
        world,
        no_run_of_the_head("2026-11-30: it ran a working tree 1 file(s) away from 1a2b3c4: tools/count.py",
                           EARLIER_RUN),
        fix=RUN_THE_HEAD,
    )


def test_a_working_tree_run_that_does_not_list_its_files_is_refused(world):
    source = tree(HEAD)
    del source["added"]
    world.run("2026-11-30", a_run(source=source))
    refused(
        world,
        no_run_of_the_head("2026-11-30: it ran a working tree and does not say which files it added", EARLIER_RUN),
        fix=RUN_THE_HEAD,
    )


def test_a_working_tree_of_another_commit_is_refused(world):
    world.run("2026-11-30", a_run(source=tree(EARLIER)))
    code, out = world.audit("--merge")
    assert code == 1 and world.merged == []
    assert "2026-11-30: it ran 2b3c4d5, which differs from the head in tools/count.py" in problems(out)[0]


def test_a_run_with_no_sync_is_a_run_of_nothing_the_audit_can_name(world):
    world.run("2026-11-30", a_run(source={"kind": "the tree last synced (no sync this run)", "head": HEAD}))
    refused(
        world,
        no_run_of_the_head("2026-11-30: its source is 'the tree last synced (no sync this run)', neither a pushed "
                           "commit nor a working tree", EARLIER_RUN),
        fix=RUN_THE_HEAD,
    )


@pytest.mark.parametrize("changes, why", [
    ({"release": None}, "it is a run of no named release"),
    ({"source": {"head": HEAD}}, "its source records no kind, and a run counts from a pushed commit or a working tree"),
    ({"source": "the head"}, "its source records no kind, and a run counts from a pushed commit or a working tree"),
    ({"steps": "all passed"}, "it has no suites step"),
    ({"steps": ["suites", "checks", "bank"]}, "it has no suites step"),
])
def test_a_record_of_another_shape_does_not_count_and_does_not_break_the_audit(world, changes, why):
    world.run("2026-11-30", a_run(**changes))
    refused(world, no_run_of_the_head(f"2026-11-30: {why}", EARLIER_RUN), fix=RUN_THE_HEAD)


def test_a_run_with_no_start_time_sorts_last_and_says_so_when_taken(world):
    world.run("2026-11-30", a_run(started=None))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert notes(out)[0].startswith("run       2026-11-30: started at no recorded time, pushed commit 1a2b3c4 (the head)")
    # The windowed run of an earlier commit has a time, so it is the newer.
    assert notes(out)[1] == f"newer     {EARLIER_RUN}"


def test_a_body_record_with_nothing_in_it_prints_as_unnamed(world):
    world.run("2026-11-30", a_run(body={}))
    _, out = world.audit()
    assert notes(out)[1] == "body      manifest unnamed sha256 none; state unnamed sha256 none"


@pytest.mark.parametrize("head", ["--output=/tmp/x", "1a2b3c4", "master~1", HEAD.upper(), None, 7])
def test_what_is_not_a_whole_commit_is_never_handed_to_git(world, head):
    world.run("2026-11-30", a_run(source=dict(pushed(HEAD), head=head)))
    refused(world, no_run_of_the_head("2026-11-30: its source names no commit", EARLIER_RUN), fix=RUN_THE_HEAD)
    assert all(head not in args for args in world.stand.asked)


@pytest.mark.parametrize("text, why", [
    ("{ not json", "run.json cannot be read ("),
    ("[1, 2]", "run.json cannot be read (it holds no JSON object)"),
])
def test_a_run_json_that_cannot_be_read_does_not_count_and_says_so(world, text, why):
    world.run("2026-11-30", text)
    code, out = world.audit("--merge")
    assert code == 1 and world.merged == []
    assert f"2026-11-30: {why}" in problems(out)[0]


def test_of_two_runs_that_count_the_newest_by_started_is_taken_and_named(world):
    # The folder's name says later; `started` says earlier, and decides.
    world.run("2026-11-30-2", a_run(started="2026-11-30T09:00:00", source=tree(HEAD)))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert notes(out)[0].startswith("run       2026-11-30, the newest of 2 that count: started 2026-11-30T10:02:11, "
                                    "pushed commit 1a2b3c4")

    world.run("2026-11-30-2", a_run(started="2026-11-30T16:30:00", source=tree(HEAD)))
    _, out = world.audit()
    assert notes(out)[0].startswith("run       2026-11-30-2, the newest of 2 that count: started 2026-11-30T16:30:00, "
                                    "working tree 1a2b3c4")


def test_two_runs_started_the_same_second_are_told_apart_by_the_folders_name(world):
    # The runner names a second run of one day `<day>-2`, so the greater
    # name is the later run.
    world.run("2026-11-30-2", a_run(source=tree(HEAD)))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert notes(out)[0].startswith("run       2026-11-30-2, the newest of 2 that count: started 2026-11-30T10:02:11, "
                                    "working tree 1a2b3c4")


def failed_run(started: str, source: dict, name: str = "bank", reason: str = "replay.py exit 1: 4 changed") -> dict:
    """A run whose one step failed, with everything else as the fixture has
    it."""
    record = a_run(started=started, source=source,
                   verdict="body run done: 6 step(s) passed, 1 failed or refused, 2 not run")
    if any(s["name"] == name for s in record["steps"]):
        step(record, name).update(status="failed", reason=reason)
    else:
        record["steps"].append({"name": name, "status": "failed", "reason": reason, "seconds": 9.0, "details": {}})
    return record


def failed_after(name: str, started: str, what: str) -> str:
    return f"the newest run of the head, {name} ({started}), failed {what}; a passing run before it does not count"


@pytest.mark.parametrize("source", [pushed(HEAD), pushed(CLOSING), tree(HEAD), tree(CLOSING)])
def test_a_newer_run_of_the_head_that_failed_holds_the_merge(world, source):
    # The head passed at 10:02 and failed at 18:00. The later answer stands.
    world.run("2026-11-30-2", failed_run("2026-11-30T18:00:00", source))
    out = refused(world, failed_after("2026-11-30-2", "2026-11-30T18:00:00", "bank: replay.py exit 1: 4 changed"),
                  fix=RUN_THE_HEAD)
    # No run is taken, so none is printed as the evidence.
    assert not any(line.startswith(("run  ", "body  ", "newer")) for line in notes(out))


def test_any_step_that_failed_in_a_newer_run_of_the_head_holds_the_merge(world):
    world.run("2026-11-30-2", failed_run("2026-11-30T18:00:00", pushed(HEAD), "soak", "not flat: memory grew"))
    refused(world, failed_after("2026-11-30-2", "2026-11-30T18:00:00", "soak: not flat: memory grew"),
            fix=RUN_THE_HEAD)


@pytest.mark.parametrize("name", ["suites", "checks", "bank"])
def test_a_newer_run_of_the_head_that_failed_and_then_lost_the_body_holds_the_merge(world, name):
    # The runner calls a run not done when the body stops answering, after
    # its suites, its checks and its bank may already have failed, and keeps
    # those steps. The step decides, never the verdict.
    record = failed_run("2026-11-30T18:00:00", pushed(HEAD), name, "failed: emet-engine")
    record["verdict"] = "body run not done: the body stopped answering over SSH (ssh exited 255)"
    world.run("2026-11-30-2", record)
    refused(world, failed_after("2026-11-30-2", "2026-11-30T18:00:00", f"{name}: failed: emet-engine"),
            fix=RUN_THE_HEAD)


@pytest.mark.parametrize("changes", [{"release": "0.9.0"}, {"format": "emet-body-run/1"}])
def test_a_newer_failed_run_of_the_head_holds_the_merge_whatever_its_release_or_format(world, changes):
    # A1's reading of the rule: it names the source alone.
    record = failed_run("2026-11-30T18:00:00", pushed(HEAD))
    record.update(changes)
    world.run("2026-11-30-2", record)
    refused(world, failed_after("2026-11-30-2", "2026-11-30T18:00:00", "bank: replay.py exit 1: 4 changed"),
            fix=RUN_THE_HEAD)


def test_the_newest_of_two_newer_failures_is_the_one_named_and_a_failure_with_no_reason_says_so(world):
    world.run("2026-11-30-2", failed_run("2026-11-30T18:00:00", pushed(HEAD)))
    world.run("2026-11-30-3", failed_run("2026-11-30T19:30:00", pushed(HEAD), "checks", ""))
    refused(world, failed_after("2026-11-30-3", "2026-11-30T19:30:00", "checks: no reason recorded"),
            fix=RUN_THE_HEAD)


def passes_and_prints_newer(world: World, name: str, why: str) -> None:
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert notes(out)[0].startswith("run       2026-11-30: started 2026-11-30T10:02:11")
    assert notes(out)[1] == f"newer     {name}: {why}"
    assert world.merged == [("12", TITLE, HEAD)]


@pytest.mark.parametrize("name", ["sync", "install"])
def test_a_newer_run_that_never_got_its_code_onto_the_body_does_not_hold_the_merge(world, name):
    # The sync or the install failed, so the body never ran the code: a
    # failed step, and a verdict that says the run was not done.
    record = failed_run("2026-11-30T18:00:00", pushed(HEAD), name, "the tree was not swapped in")
    record["verdict"] = "body run not done: the code under test did not reach the body"
    kept = ("preflight", "sync", "install")[: ("preflight", "sync", "install").index(name) + 1]
    record["steps"] = [s for s in record["steps"] if s["name"] in kept]
    world.run("2026-11-30-2", record)
    passes_and_prints_newer(world, "2026-11-30-2", "it has no suites step")


def test_a_newer_run_that_was_refused_at_the_preflight_does_not_hold_the_merge(world):
    record = a_run(started="2026-11-30T18:00:00", verdict="body run not done: the robot is listening")
    record["steps"] = [{"name": "preflight", "status": "refused", "reason": "the robot is listening",
                        "seconds": 0.0, "details": {}}]
    world.run("2026-11-30-2", record)
    passes_and_prints_newer(world, "2026-11-30-2", "it has no suites step")


def test_a_newer_run_whose_step_did_not_run_does_not_hold_the_merge(world):
    record = a_run(started="2026-11-30T18:00:00")
    step(record, "bank").update(status="not run", reason="tools/bank/replay.py is not in the synced tree")
    world.run("2026-11-30-2", record)
    passes_and_prints_newer(world, "2026-11-30-2", "its bank step reads 'not run', not 'passed'")


@pytest.mark.parametrize("source, why", [
    (pushed(EARLIER), "its bank step reads 'failed', not 'passed'"),
    (pushed(UNFETCHED), "its bank step reads 'failed', not 'passed'"),
    (tree(HEAD, changed=["tools/count.py"]), "its bank step reads 'failed', not 'passed'"),
])
def test_a_newer_failed_run_of_other_code_does_not_hold_the_merge(world, source, why):
    world.run("2026-11-30-2", failed_run("2026-11-30T18:00:00", source))
    passes_and_prints_newer(world, "2026-11-30-2", why)


def test_an_older_failed_run_of_the_head_does_not_hold_the_merge(world):
    # It failed at 08:00 and passed at 10:02: the later answer stands.
    world.run("2026-11-30-0", failed_run("2026-11-30T08:00:00", pushed(HEAD)))
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert notes(out)[0].startswith("run       2026-11-30: started 2026-11-30T10:02:11")
    assert not any(line.startswith("newer") for line in notes(out))
    assert world.merged == [("12", TITLE, HEAD)]


def test_a_failure_between_two_passes_does_not_hold_the_merge(world):
    world.run("2026-11-30-2", failed_run("2026-11-30T12:00:00", pushed(HEAD)))
    world.run("2026-11-30-3", a_run(started="2026-11-30T18:00:00"))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert notes(out)[0].startswith("run       2026-11-30-3, the newest of 2 that count: started 2026-11-30T18:00:00")
    assert world.merged == [("12", TITLE, HEAD)]


@pytest.mark.parametrize("text", [
    '{\n  "format": "emet-body-run/2",\n  "started": "2026-11-30T18:00:00",\n  "steps": [\n',  # cut off mid-write
    "[1, 2]",
])
def test_a_run_json_that_cannot_be_read_beside_one_that_counts_holds_the_merge(world, text):
    # It has no `started` to place it, so it may be a newer run of the head
    # that failed, and the rule on one cannot be held.
    world.run("2026-11-30-2", text)
    try:
        why = "it holds no JSON object" if isinstance(json.loads(text), list) else ""
    except ValueError as error:
        why = str(error)
    record = f"{BODY_SHOWN}/2026-11-30-2/run.json"
    out = refused(world, f"{record} cannot be read ({why}), so whether a newer run of the head failed is not known",
                  fix=f"python -m json.tool {record}  # shows where it breaks; mend it, or move its folder out of "
                      f"{BODY_SHOWN}")
    assert not any(line.startswith(("run  ", "body  ")) for line in notes(out))


def test_more_than_three_newer_runs_are_counted_and_not_listed(world):
    for hour in range(11, 16):
        world.run(f"2026-11-30-{hour}", a_run(started=f"2026-11-30T{hour}:00:00", release="0.9.0"))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert notes(out)[1:5] == [
        "newer     2026-11-30-15: it is a run of 0.9.0", "newer     2026-11-30-14: it is a run of 0.9.0",
        "newer     2026-11-30-13: it is a run of 0.9.0", "newer     and 2 more that do not count",
    ]


def test_the_refusal_names_the_four_newest_runs_and_counts_the_older(world):
    world.no_run("2026-11-30")
    for day in range(21, 27):
        world.run(f"2026-11-{day}", a_run(started=f"2026-11-{day}T10:00:00", release="0.9.0"))
    code, out = world.audit("--merge")
    told = problems(out)[0]
    assert told == f"{VERSION}: " + no_run_of_the_head(
        EARLIER_RUN, "2026-11-26: it is a run of 0.9.0", "2026-11-25: it is a run of 0.9.0",
        "2026-11-24: it is a run of 0.9.0") + "; and 3 older"
    assert code == 1


def test_a_run_without_the_bodys_hashes_still_counts_and_says_so(world):
    record = a_run()
    del record["body"]
    world.run("2026-11-30", record)
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert notes(out)[1] == "body      the run records no manifest or state hash"


def test_a_missing_manifest_prints_as_none_with_the_runners_note(world):
    world.run("2026-11-30", a_run(body={"manifest": "~/pi-speakerphone.yaml", "manifest_sha256": None,
                                        "state": "~/state.json", "state_sha256": "ab" * 32,
                                        "note": "no manifest at ~/pi-speakerphone.yaml"}))
    _, out = world.audit()
    assert notes(out)[1] == (f"body      manifest ~/pi-speakerphone.yaml sha256 none; state ~/state.json sha256 "
                             f"{'ab' * 32}; no manifest at ~/pi-speakerphone.yaml")


# -------------------------------------------------------- E5, the flags


@pytest.mark.parametrize("kind", ["patch", "minor", "major"])
def test_a_flag_missing_from_flags_run_is_refused_with_the_windowed_run(world, kind):
    # Every kind but a hotfix is held to the flags.
    world.kind(kind)
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER), release=world.version,
                                  flags_run=flags_run(without=(EITHER,))))
    out = refused(world, not_run(EITHER, f"no run under {body_shown(world.version)} has it in flags_run"),
                  fix=flags_fix(world.version))
    assert flag_line(out, EITHER) == ["-", "not", "run"]


def test_the_flags_no_run_names_share_one_problem_and_one_fix(world):
    # Before the release's window that is every flag, and one line says so.
    world.no_run("2026-11-29")
    labels = [repr(row.label) for row in ROWS]
    out = refused(
        world,
        f"{len(ROWS)} flags were not run on the body in this release, {', '.join(labels[:-1])} and {labels[-1]}: "
        f"no run under {BODY_SHOWN} has one of them in flags_run",
        fix=RUN_THE_FLAGS,
    )
    for row in ROWS:
        assert flag_line(out, row.label) == ["-", "not", "run"]


def test_a_flag_with_its_own_reason_keeps_its_own_problem_beside_the_shared_one(world):
    others = [row.label for row in ROWS if not row.live_only]
    assert ROWS[0].label == LIVE_ONLY and len(others) >= 2
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run(without=tuple(others[:2]), how={LIVE_ONLY: "replay"})))
    code, out = world.audit("--merge")
    assert problems(out) == [f"{VERSION}: {problem}" for problem in (
        not_run(LIVE_ONLY, "2026-11-29 ran it from a file only, and only a live run counts for it"),
        f"2 flags were not run on the body in this release, {others[0]!r} and {others[1]!r}: "
        f"no run under {BODY_SHOWN} has one of them in flags_run",
    )]
    assert said(out) == "body evidence: 2 problem(s)"
    assert fixes(out) == [RUN_THE_FLAGS]
    assert code == 1 and world.merged == []


def test_a_live_only_flag_run_only_from_a_file_is_refused(world):
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run(how={LIVE_ONLY: "replay"})))
    refused(world, not_run(LIVE_ONLY, "2026-11-29 ran it from a file only, and only a live run counts for it"),
            fix=RUN_THE_FLAGS)


def test_a_flag_that_counts_either_way_counts_live_and_shows_it(world):
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run() + [{"flag": EITHER, "how": "live", "step": "live-plain"}]))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert flag_line(out, EITHER) == ["live", "2026-11-29", "2b3c4d5"]


def test_a_flag_run_in_a_way_the_audit_does_not_know_is_refused(world):
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run(how={EITHER: "by hand"})))
    refused(world, not_run(EITHER, "2026-11-29 does not say whether it ran it live or from a file"),
            fix=RUN_THE_FLAGS)


def a_flag_only_in(world: World, name: str, **changes) -> None:
    """Take one flag out of the windowed run and give it to another run."""
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run(without=(EITHER,))))
    record = a_run(started="2026-11-10T14:00:05", flags_run=[{"flag": EITHER, "how": "replay", "step": "speaker-echo"}])
    record.update(changes)
    world.run(name, record)


def test_a_flag_run_only_on_a_commit_of_master_is_refused(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(ON_MASTER))
    out = refused(
        world,
        not_run(EITHER, "2026-11-10 ran 3c4d5e6, which is on origin/master: a commit from before this release's branch"),
        fix=RUN_THE_FLAGS,
    )
    assert ("git", "merge-base", "--is-ancestor", ON_MASTER, "HEAD") in world.stand.asked
    assert ("git", "merge-base", "--is-ancestor", ON_MASTER, "origin/master") in world.stand.asked
    assert EARLIER_RUN not in out


def test_a_flag_run_on_an_earlier_commit_of_the_branch_counts(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(CLOSING))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert flag_line(out, EITHER) == ["replay", "2026-11-10", "6f7a8b9"]


def test_a_flag_run_on_the_head_itself_counts_with_no_question_to_git(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(HEAD))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert flag_line(out, EITHER) == ["replay", "2026-11-10", "1a2b3c4"]
    assert world.stand.asked_git("merge-base", "--is-ancestor", HEAD) == []


def test_a_flag_run_on_another_branch_is_refused(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(ELSEWHERE))
    refused(world, not_run(EITHER, "2026-11-10 ran 4d5e6f7, which is outside the head's history"), fix=RUN_THE_FLAGS)


def test_a_flag_run_on_a_commit_git_does_not_have_says_to_fetch(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(UNFETCHED))
    refused(
        world,
        not_run(EITHER, f"2026-11-10 ran 5e6f7a8, which git does not have here "
                        f"(fatal: Not a valid commit name {UNFETCHED}); git fetch origin"),
        fix=RUN_THE_FLAGS,
    )


def test_a_base_git_does_not_have_is_no_proof_and_says_to_fetch(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(CLOSING))
    code, out = world.audit("--merge", "--ref", "origin/main")
    assert code == 1 and world.merged == []
    assert f"{VERSION}: " + not_run(
        EITHER, "2026-11-10 ran 6f7a8b9, which git cannot compare with origin/main "
                "(fatal: Not a valid object name origin/main); git fetch origin") in problems(out)


def test_a_flag_run_from_a_working_tree_is_refused(world):
    a_flag_only_in(world, "2026-11-10", source=tree(HEAD))
    refused(world, not_run(EITHER, "2026-11-10 ran a working tree, and a flag counts on a pushed commit"),
            fix=RUN_THE_FLAGS)


@pytest.mark.parametrize("source, why", [
    ({"kind": "the tree last synced (no sync this run)", "head": HEAD},
     "2026-11-10 ran what it calls 'the tree last synced (no sync this run)', and a flag counts on a pushed commit"),
    ({"head": HEAD}, "2026-11-10's source records no kind, and a flag counts on a pushed commit"),
])
def test_a_flag_run_from_no_source_git_can_name_is_refused(world, source, why):
    a_flag_only_in(world, "2026-11-10", source=source)
    refused(world, not_run(EITHER, why), fix=RUN_THE_FLAGS)


@pytest.mark.parametrize("head", ["--output=/tmp/x", "1a2b3c4", None])
def test_a_flag_run_that_names_no_whole_commit_is_refused_and_git_is_not_asked(world, head):
    a_flag_only_in(world, "2026-11-10", source=dict(pushed(HEAD), head=head))
    refused(world, not_run(EITHER, "2026-11-10 names no commit"), fix=RUN_THE_FLAGS)
    assert all(head not in args for args in world.stand.asked)


@pytest.mark.parametrize("release, said_of_it", [("0.9.0", "0.9.0"), (None, "no named release")])
def test_a_flag_run_for_another_release_is_refused(world, release, said_of_it):
    a_flag_only_in(world, "2026-11-10", source=pushed(CLOSING), release=release)
    refused(world, not_run(EITHER, f"2026-11-10 is a run of {said_of_it}"), fix=RUN_THE_FLAGS)


def test_a_flag_in_a_format_1_file_is_refused(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(CLOSING), format="emet-body-run/1")
    refused(world, not_run(EITHER, "2026-11-10 is 'emet-body-run/1', and flags_run is read from emet-body-run/2 on"),
            fix=RUN_THE_FLAGS)


def test_one_windowed_run_of_the_head_is_all_the_evidence_a_merge_needs(world):
    # Stage 7's `--commit --window` run: the run E4 takes, and the run every
    # flag comes from.
    world.no_run("2026-11-29")
    world.run("2026-11-30", a_run(window=["2026-11-30T10:00", "2026-11-30T11:00"], flags_run=flags_run()))
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert notes(out)[0].startswith("run       2026-11-30: started 2026-11-30T10:02:11, pushed commit 1a2b3c4 "
                                    "(the head)")
    for row in ROWS:
        assert flag_line(out, row.label) == ["live" if row.live_only else "replay", "2026-11-30", "1a2b3c4"]
    assert world.stand.asked_git("merge-base") == []
    assert world.merged == [("12", TITLE, HEAD)]


def test_a_flag_keeps_its_run_when_that_runs_other_steps_failed(world):
    # Only the flag's own sub-step need have passed, and the runner writes
    # the entry only then (contract §3).
    record = a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER), flags_run=flags_run())
    step(record, "bank").update(status="failed")
    world.run("2026-11-29", record)
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])


def test_of_two_runs_that_count_for_a_flag_the_table_names_the_newer(world):
    world.run("2026-11-29-2", a_run(started="2026-11-29T20:00:00", source=pushed(CLOSING),
                                    flags_run=[{"flag": EITHER, "how": "replay", "step": "speaker-echo"}]))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert flag_line(out, EITHER) == ["replay", "2026-11-29-2", "6f7a8b9"]
    assert flag_line(out, LIVE_ONLY) == ["live", "2026-11-29", "2b3c4d5"]


def test_of_two_runs_that_do_not_count_for_a_flag_the_newer_ones_reason_is_given(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(ON_MASTER))
    world.run("2026-11-12", a_run(started="2026-11-12T14:00:05", source=pushed(ELSEWHERE),
                                  flags_run=[{"flag": EITHER, "how": "replay", "step": "speaker-echo"}]))
    refused(world, not_run(EITHER, "2026-11-12 ran 4d5e6f7, which is outside the head's history"), fix=RUN_THE_FLAGS)


def test_each_commit_is_placed_once_however_many_flags_ran_on_it(world):
    world.audit()
    assert world.stand.asked_git("merge-base", "--is-ancestor", EARLIER) == [
        ("git", "merge-base", "--is-ancestor", EARLIER, "HEAD"),
        ("git", "merge-base", "--is-ancestor", EARLIER, "origin/master"),
    ]


def gap(flag: str, answer: str, date: str) -> str:
    return GAPS + f"| {flag} | The speaker was away for repair | {answer} | {date} |\n"


def without_one_flag(world: World) -> None:
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run(without=(EITHER,))))


@pytest.mark.parametrize("written", [f"`{EITHER}`", EITHER.upper(), f"  {EITHER} "])
def test_a_flag_with_a_dated_answer_in_the_plan_passes_and_prints_as_a_gap(world, written):
    without_one_flag(world)
    world.plan_swap(GAPS, gap(written, '"Fine, next release."', "2026-11-29"))
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert flag_line(out, EITHER) == ["gap", "the", "plan", '"Fine,', "next", 'release."', "(2026-11-29)"]
    assert world.merged == [("12", TITLE, HEAD)]


def test_a_flag_gap_written_with_its_spaces_doubled_covers_the_flag(world):
    world.run("2026-11-29", a_run(started="2026-11-29T14:00:05", source=pushed(EARLIER),
                                  flags_run=flags_run(without=(SPACED,))))
    world.plan_swap(GAPS, gap(SPACED.replace(" ", "  "), '"Fine, next release."', "2026-11-29"))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert flag_line(out, SPACED)[:3] == ["gap", "the", "plan"]


@pytest.mark.parametrize("answer, date", [
    ('"Fine, next release."', ""),
    ("", "2026-11-29"),
    ("", ""),
])
def test_a_flag_gap_without_its_answer_and_its_date_is_refused(world, answer, date):
    without_one_flag(world)
    world.plan_swap(GAPS, gap(f"`{EITHER}`", answer, date))
    refused(world, not_run(EITHER, "its row in the plan's flag-gap table has no answer with a date"),
            fix=RUN_THE_FLAGS)


def test_a_gap_with_no_date_beside_a_run_that_does_not_count_says_both(world):
    a_flag_only_in(world, "2026-11-10", source=pushed(ON_MASTER))
    world.plan_swap(GAPS, gap(f"`{EITHER}`", '"Fine."', ""))
    refused(
        world,
        not_run(EITHER, "2026-11-10 ran 3c4d5e6, which is on origin/master: a commit from before this release's "
                        "branch; its row in the plan's flag-gap table has no answer with a date"),
        fix=RUN_THE_FLAGS,
    )


@pytest.mark.parametrize("dated_first", [True, False])
def test_a_flag_written_twice_in_the_gap_table_keeps_its_dated_answer(world, dated_first):
    without_one_flag(world)
    dated = f"| `{EITHER}` | The speaker was away for repair | \"Fine, next release.\" | 2026-11-29 |\n"
    bare = f"| {EITHER} | Asked on the day | | |\n"
    world.plan_swap(GAPS, GAPS + (dated + bare if dated_first else bare + dated))
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert flag_line(out, EITHER)[:3] == ["gap", "the", "plan"]


def test_a_flag_that_ran_prints_its_run_whatever_the_gap_table_says(world):
    world.plan_swap(GAPS, gap(f"`{EITHER}`", '"Fine."', "2026-11-29"))
    code, out = world.audit("--merge")
    assert (code, problems(out)) == (0, [])
    assert flag_line(out, EITHER) == ["replay", "2026-11-29", "2b3c4d5"]


def test_a_gap_for_no_flag_of_the_runner_covers_nothing_and_is_named(world):
    without_one_flag(world)
    world.plan_swap(GAPS, gap("`--sing`", '"Fine."', "2026-11-29"))
    out = refused(world, not_run(EITHER, f"no run under {BODY_SHOWN} has it in flags_run"), fix=RUN_THE_FLAGS)
    assert "the plan's flag-gap table names '--sing', which is no flag of tools/body/run.py" in notes(out)


def test_a_hotfix_is_not_asked_for_the_flags_and_the_table_still_prints(world):
    world.plan_swap(KIND, "- **Kind:** hotfix")
    world.no_run("2026-11-29")
    code, out = world.audit("--merge")
    assert (code, problems(out), said(out)) == (0, [], "body evidence: ok")
    assert "flags     a hotfix is not held to them; the table is for the record" in notes(out)
    for row in ROWS:
        assert flag_line(out, row.label) == ["-", "not", "run"]
    assert world.merged == [("12", TITLE, HEAD)]


def test_the_flags_are_the_runners_table_and_no_list_of_the_audits_own(world, monkeypatch):
    sing = types.SimpleNamespace(label="--sing", live_only=False)
    monkeypatch.setattr(release_audit, "body_flag_rows", lambda: (sing,))
    refused(world, not_run("--sing", f"no run under {BODY_SHOWN} has it in flags_run"), fix=RUN_THE_FLAGS)


def test_the_runners_table_is_read_from_the_runner():
    module = sys.modules["emet_body_run_for_the_audit"]
    assert Path(module.__file__) == TOOLS / "body" / "run.py"
    assert ROWS == tuple(module.FLAG_ROWS) and len(ROWS) > 0
    for row in ROWS:
        assert isinstance(row.label, str) and row.label
        assert isinstance(row.live_only, bool)
    assert len({row.label for row in ROWS}) == len(ROWS)
    # The flag table's first column is 28 wide, as the runner's own is.
    assert max(len(row.label) for row in ROWS) <= 28


def test_the_format_the_runner_writes_is_one_the_merge_reads():
    # The runner's FORMAT, read with no default: a rename fails here.
    written = sys.modules["emet_body_run_for_the_audit"].FORMAT
    assert release_audit.BodyRun(Path("."), {"format": written}).format >= release_audit.RUN_FORMAT_FROM


# ------------------------------------------------- the line and the count


def test_the_evidence_line_counts_its_own_problems_only(world):
    # Stage 7's exit: the pull request is still a draft, so the run as a
    # whole fails, and the evidence line alone says the evidence is in.
    world.plan_swap(STAGE_7, "| 7. Candidate and sitting | 2026-11-29 | |")
    world.stand.pr["isDraft"] = True
    code, out = world.audit()
    assert problems(out) == [f"{VERSION}: #12 is a draft"]
    assert said(out) == "body evidence: ok"
    assert out.rstrip().endswith("1 problem(s) in #12 before its merge.")
    assert code == 1

    world.no_run("2026-11-30")
    code, out = world.audit()
    assert problems(out) == [f"{VERSION}: #12 is a draft", f"{VERSION}: " + no_run_of_the_head(EARLIER_RUN)]
    assert said(out) == "body evidence: 1 problem(s)"
    assert out.rstrip().endswith("2 problem(s) in #12 before its merge.")
    assert code == 1


def test_the_evidence_line_comes_after_the_problems_and_their_fixes(world):
    world.no_run("2026-11-30")
    _, out = world.audit()
    lines = out.splitlines()
    assert lines.index(f"  fix: {RUN_THE_HEAD}") < lines.index("body evidence: 1 problem(s)")
    assert lines.index("body evidence: 1 problem(s)") < lines.index("1 problem(s) in #12 before its merge.")


def test_one_problem_of_each_kind_is_five_problems_and_three_fixes(world):
    world.plan_swap(KIND, "- **Kind:** quick")
    world.plan_swap(STAGE_3, "| 3. Review and fix | 2026-11-24 | |")
    world.plan_swap(L3, finding("S1"))
    world.no_run("2026-11-30")
    without_one_flag(world)
    code, out = world.audit("--merge")
    told = problems(out)
    assert len(told) == 5
    assert told[0].startswith(f"{VERSION}: {PLAN_SHOWN} has 1 problem(s) the plan's parser names: line 3: the kind is")
    assert told[1:] == [f"{VERSION}: {problem}" for problem in (
        "stage 3, Review and fix, is neither exited nor skipped in the plan",
        "L4 is an S1, open in the plan's ledger; an S1 holds every merge",
        no_run_of_the_head(EARLIER_RUN),
        not_run(EITHER, f"no run under {BODY_SHOWN} has it in flags_run"),
    )]
    assert said(out) == "body evidence: 5 problem(s)"
    assert fixes(out) == [EDIT_PLAN, RUN_THE_HEAD, RUN_THE_FLAGS]
    assert code == 1 and world.merged == []


def test_packages_that_disagree_leave_the_evidence_unread(world):
    world.declare("emet-hal", "0.9.0")
    code, out = world.audit("--merge")
    assert problems(out) == ["the packages declare ['0.9.0', '0.9.1']; release_check.py says more"]
    assert said(out) == "body evidence: not read, the packages declare no one version"
    assert code == 1 and world.merged == []


# ----------------------------------------------- who reads no evidence


def refuse_to_read(monkeypatch) -> None:
    def read(*args, **kwargs):
        raise AssertionError("the evidence was read")

    monkeypatch.setattr(release_audit.Audit, "evidence", read)
    monkeypatch.setattr(release_audit.plan_file, "read_plan", read)
    monkeypatch.setattr(release_audit, "read_runs", read)
    monkeypatch.setattr(release_audit, "body_flag_rows", read)


def test_ci_reads_nothing_and_prints_the_line(world, monkeypatch):
    refuse_to_read(monkeypatch)
    shutil.rmtree(world.release)
    code, out = world.audit("--ci")
    assert out.splitlines() == ["body evidence: checked at merge, on the laptop",
                                "release-audit: ok, #12 as CI can see it"]
    assert code == 0
    for question in ("rev-parse", "diff", "merge-base", "status"):
        assert world.stand.asked_git(question) == []


def test_ci_prints_the_line_under_its_own_problems(world, monkeypatch):
    refuse_to_read(monkeypatch)
    world.stand.pr["title"] = "Teach it to count"
    code, out = world.audit("--ci")
    assert problems(out) == [f"{VERSION}: #12 is titled 'Teach it to count', not '{TITLE}'"]
    assert said(out) == "body evidence: checked at merge, on the laptop"
    assert code == 1


@pytest.mark.parametrize("options", [[], ["--complete"]])
def test_the_history_audit_reads_no_evidence_and_prints_no_line(world, monkeypatch, capsys, options):
    refuse_to_read(monkeypatch)
    monkeypatch.setattr(release_audit.Audit, "history", lambda audit: None)
    code = release_audit.main(["release_audit.py", "--root", str(world.root), *options])
    out = capsys.readouterr().out
    assert out.splitlines() == ["release-audit: ok, every version on origin/master since 0.4.1"]
    assert code == 0


@pytest.mark.parametrize("options", [
    ["--evidence", "../releases/v0.9.1"],
    ["--pr", "12", "--ci", "--evidence", "../releases/v0.9.1"],
    ["--complete", "--evidence", "../releases/v0.9.1"],
])
def test_the_folder_is_an_option_of_the_merge_day_only(world, capsys, options):
    with pytest.raises(SystemExit) as stopped:
        release_audit.main(["release_audit.py", "--root", str(world.root), *options])
    assert stopped.value.code == 2
    assert "--evidence goes with --pr N on the merge day" in capsys.readouterr().err
    assert world.stand.asked == []


# ------------------------------------- what the audit refused before 0.5.6


def test_an_entry_dated_another_day_is_still_refused_with_its_command(world):
    changelog = world.root / "CHANGELOG.md"
    changelog.write_text(CHANGELOG.replace(f"[{VERSION}] - {TODAY}", f"[{VERSION}] - 2026-11-30"), encoding="utf-8")
    code, out = world.audit("--merge")
    assert problems(out) == [f"{VERSION}: the entry is dated 2026-11-30; merged today it is {TODAY}"]
    assert fixes(out) == [
        "sed -i 's/^## \\[0.9.1\\] - 2026-11-30/## [0.9.1] - 2026-12-01/' CHANGELOG.md && git add CHANGELOG.md"
        " && git commit -s -m \"Date the 0.9.1 entry the day it merges\" && git push"
    ]
    assert said(out) == "body evidence: ok"
    assert out.rstrip().endswith("1 problem(s) in #12 before its merge. Nothing was merged.")
    assert code == 1 and world.merged == []


def test_a_draft_a_red_check_another_head_and_a_dirty_tree_are_still_refused(world):
    world.stand.pr.update(isDraft=True, headRefOid=EARLIER, mergeable="CONFLICTING")
    world.stand.checks = (1, "layering\tfail\t1m2s\thttps://example.invalid/1\nwheel install\tpass\t40s\tu")
    world.stand.dirty = " M tools/count.py"
    code, out = world.audit("--merge")
    assert problems(out) == [f"{VERSION}: {problem}" for problem in (
        "#12 is a draft",
        "#12 is CONFLICTING, not mergeable",
        "#12's head is 2b3c4d5, this checkout is 1a2b3c4",
        "the working tree has uncommitted changes; the check read them, the merge would not take them",
        "the checks are not all green: layering\tfail\t1m2s\thttps://example.invalid/1",
    )]
    assert said(out) == "body evidence: ok"
    assert code == 1 and world.merged == []


def test_a_version_not_above_masters_and_another_title_are_still_refused(world):
    for package in release_audit.PACKAGES:
        world.declare(package, "0.9.0")
    world.stand.pr["title"] = "Give it more numbers"
    code, out = world.audit("--merge", "--evidence", f"../releases/v{VERSION}")
    told = problems(out)
    assert told[0] == ("0.9.0: not above 0.9.0, what master declares: every merge is a version, and a change that "
                       "is not one (a Dependabot update) goes into the next pull request that is (RELEASING.md)")
    assert told[1] == "0.9.0: the entry cites #11, not #12"
    assert told[2] == "0.9.0: the entry is dated 2026-11-01; merged today it is 2026-12-01"
    assert told[3] == "0.9.0: #12 is titled 'Give it more numbers', not '0.9.0: Give it numbers'"
    # The folder named holds 0.9.1's plan and runs, and the packages say 0.9.0.
    assert told[4] == f"0.9.0: {PLAN_SHOWN} is the plan for {VERSION}, and the packages declare 0.9.0"
    assert code == 1 and world.merged == []


def test_github_not_answering_still_stops_the_run_with_its_own_line(world, capsys):
    world.stand.checks = (1, "HTTP 502: Bad Gateway (https://api.github.com/graphql)")
    code = release_audit.main(["release_audit.py", "--pr", "12", "--root", str(world.root), "--merge"])
    captured = capsys.readouterr()
    assert captured.err.strip() == ("release-audit: GitHub did not answer (HTTP 502: Bad Gateway "
                                    "(https://api.github.com/graphql)); run it again. Nothing was merged.")
    assert "body evidence" not in captured.out
    assert code == 1 and world.merged == []


# ------------------------------------------------- the real runs of 0.5.5


def test_the_real_runs_of_0_5_5_are_format_1_and_none_counts(tmp_path):
    if not REAL_RUNS.is_dir():
        pytest.skip("no releases/v0.5.5/body beside the repository on this machine")
    runs = release_audit.read_runs(REAL_RUNS)
    assert runs, "the folder holds no run.json"
    assert [run.started for run in runs] == sorted((run.started for run in runs), reverse=True)
    audit = release_audit.Audit(tmp_path, tmp_path / "CHANGELOG.md", "origin/master")
    for run in runs:
        assert run.format == 1 and run.release == "0.5.5" and run.flags_run == []
        assert audit.short_of_passing(run, "0.5.5") == (
            "its format is 'emet-body-run/1', and the merge reads emet-body-run/2 or later")
