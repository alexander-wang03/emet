"""The bank's jobs in `tools/ci.py` and the workflows, and the sound job's live run.

`bank` replays the generated bank's small set on every pull request and
`bank-full` the full set weekly (P10, CI5); the `sound` job's last step
listens live on the virtual card while the generated clip plays (CI6). No
test here replays, plays or opens anything: `test_ci.Fake` stands in for
every process, and the trees are small ones under `tmp_path`. One test
starts a real process: it fetches the packed small set from a file URL
with the real `generate.py`, twice, in about three seconds, and skips on a
machine without that archive (GitHub has none). The workflow tests read the
repository's own files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from test_ci import REPO, TEMP, Fake, Said, ci, context, last_line, make_tree, snapshot

URL = "https://github.com/OWNER/NAME/releases/download/v2026-11-26/genbank-small-2026-11-26.tar.gz"
FULL_URL = "https://github.com/OWNER/NAME/releases/download/v2026-11-26/genbank-full-2026-11-26.tar.gz"
HEX = "56cac3a06b8be4f2145c4f381b09afdb14fce066bbe89e9389835b5fb56f628f"
IDS = ("gen-g01-near-1", "gen-g02-3m-2")
PY = os.path.join("TMP", "py", "python")
WORKFLOWS = REPO / ".github" / "workflows"


def store(small: tuple[str, str] = ("", ""), full: tuple[str, str] = ("", "")) -> dict:
    return {"format": ci.BANK_STORE_FORMAT, "note": "a test's",
            "sets": {"small": {"url": small[0], "sha256": small[1]}, "full": {"url": full[0], "sha256": full[1]}}}


def bank_tree(tmp_path: Path, doc: dict | None = None, goldens: tuple[str, ...] = IDS,
              platform: str = "linux-x86_64") -> Path:
    """A tree with the bank's tools, the store file and some goldens."""
    root = make_tree(tmp_path, "tools/bank/generate.py", "tools/bank/replay.py", ci.LIVE_CLIP)
    (root / ci.BANK_STORE).parent.mkdir(parents=True, exist_ok=True)
    (root / ci.BANK_STORE).write_text(json.dumps(doc if doc is not None else store((URL, HEX))), encoding="utf-8")
    for rid in goldens:
        path = root / ci.BANK_GOLDEN / platform / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    return root


def fetched(ids: tuple[str, ...] = IDS, cassettes: bool = False):
    """Stands in for `generate.py --fetch`: a bank under `--to`, named after
    the archive, as the real one unpacks it."""

    def fetch(argv: list[str]) -> None:
        url, to = argv[argv.index("--fetch") + 1], Path(argv[argv.index("--to") + 1])
        bank = to / url.rsplit("/", 1)[-1][: -len(".tar.gz")]
        (bank / "manifests").mkdir(parents=True)
        for rid in ids:
            (bank / "manifests" / f"{rid}.json").write_text(json.dumps({"id": rid}), encoding="utf-8")
        (bank / "bank.json").write_text(json.dumps({"files": [f"manifests/{rid}.json" for rid in ids]}),
                                        encoding="utf-8")
        if cassettes:
            (bank / "cassettes").mkdir()
            (bank / "cassettes" / "genbank-small.json").write_text("{}", encoding="utf-8")

    return fetch


def linux(root: Path, fake: Fake, **how):
    return context(root, fake, os_name="linux", machine="x86_64", **how)


def words(line: str) -> list[str]:
    return line.split(" ")


# --------------------------------------------------------------- the table


def test_the_bank_jobs_are_advisory_and_say_where_they_run(capsys):
    assert ci.main(["--list"]) == 0
    rows = {re.split(r"\s{2,}", line)[0]: re.split(r"\s{2,}", line) for line in capsys.readouterr().out.splitlines()
            if line.startswith("bank")}
    assert rows["bank"][1:4] == ["bank (advisory)", "advisory", "-"]
    assert rows["bank-full"][1:4] == ["bank-full (advisory)", "advisory", "-"]
    assert "every pull request" in rows["bank"][4] and "never on a pull request" in rows["bank-full"][4]
    assert not ci.BY_KEY["bank"].blocking and not ci.BY_KEY["bank-full"].ruleset


# ------------------------------------------- the store has published no set


def test_an_empty_store_runs_neither_job_and_each_warns_once(tmp_path):
    """While the store's fields are empty nothing is installed, fetched or
    replayed, the line says why, and each advisory job that was not run
    warns on GitHub once, in its own name."""
    root = bank_tree(tmp_path, store())
    fake = Fake()
    ctx = linux(root, fake, install=True, env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["bank", "bank-full"]) == 0
    assert fake.lines == []
    assert ci.NO_STORE == "the generated bank's public store, emet-testaudio (0.5.6-D4), has published no set yet"
    warnings = [line for line in ctx.out.text.splitlines() if line.startswith("::warning")]
    assert warnings == [f"::warning title=bank (advisory)::not run: {ci.NO_STORE}",
                        f"::warning title=bank-full (advisory)::not run: {ci.NO_STORE}"]
    assert "=== bank: bank (advisory)\n" in ctx.out.text
    assert last_line(ctx) == "ci: all green, 0 jobs, 2 not run here (bank, bank-full)"
    laptop = context(root, Fake())
    results = [ci.run_job(laptop, ci.BY_KEY[key]) for key in ("bank", "bank-full")]
    assert [(r.status, r.reason) for r in results] == [(ci.NOT_RUN, ci.NO_STORE)] * 2


def test_the_repositorys_store_reads_as_whole_or_empty_for_each_set():
    """The store file as it ships, held only to what stays true when the
    pull request that publishes a set fills in its two fields."""
    doc = json.loads((REPO / ci.BANK_STORE).read_text(encoding="utf-8"))
    for name in ("small", "full"):
        url, digest = ci.store_entry(doc, name)
        assert (url, digest) == ("", "") or (url and digest), name


def test_the_store_entry_is_whole_or_empty_and_names_its_own_set():
    assert ci.store_entry(store(), "small") == ("", "")
    assert ci.store_entry(store((URL, HEX)), "small") == (URL, HEX)
    assert ci.store_entry(store(full=(FULL_URL, HEX)), "full") == (FULL_URL, HEX)
    refused = {
        "a url and no sha256": store((URL, "")),
        "a sha256 and no url": store(("", HEX)),
        "neither https:// nor a file URL": store((URL.replace("https", "http"), HEX)),
        "where genbank-small-<version>.tar.gz was expected": store((FULL_URL, HEX)),
        "not 64 lower-case hex digits": store((URL, HEX.upper())),
        "is not an emet-genbank-store/1 file": {**store(), "format": "emet-genbank-store/0"},
        "has no url and sha256 for the small set": {"format": ci.BANK_STORE_FORMAT, "sets": {}},
    }
    for words_, doc in refused.items():
        try:
            ci.store_entry(doc, "small")
        except ValueError as error:
            assert words_ in str(error), (words_, str(error))
        else:
            raise AssertionError(f"accepted a store with {words_}")
    assert ci.store_entry(store((URL, ""), (FULL_URL, HEX)), "full") == (FULL_URL, HEX)   # one set, its own entry


def test_a_half_filled_store_fails_the_job_in_its_own_words_and_installs_nothing(tmp_path):
    root = bank_tree(tmp_path, store((URL, "")))
    fake = Fake()
    ctx = linux(root, fake, install=True, env={"GITHUB_ACTIONS": "true"})
    result = ci.run_job(ctx, ci.BY_KEY["bank"])
    said = "tools/bank/generated/store.json: the small set has a url and no sha256; fill both or neither"
    assert result.status == ci.FAILED and result.reason == f"store: {said}"
    assert fake.lines == []
    warnings = [line for line in ctx.out.text.splitlines() if line.startswith("::warning")]
    assert warnings == [f"::warning title=bank (advisory)::failed: store: {said}"]
    assert ci.verdict([result], strict=True) == ("ci: all green, 0 jobs, 0 not run here, 1 failed advisory (bank)", 0)


# -------------------------------------------------- a store with a name


def test_a_named_store_is_fetched_held_to_its_hash_and_replayed_against_the_goldens(tmp_path):
    root = bank_tree(tmp_path)
    fake = Fake().on("--fetch", do=fetched())
    before = snapshot(root)
    ctx = linux(root, fake, install=True)
    result = ci.run_job(ctx, ci.BY_KEY["bank"])
    assert result.status == ci.PASSED, ctx.out.text
    assert [line.split("pip install ")[1] for line in fake.ran("pip install")] == [
        "-e emet-sdk", "-e emet-hal[wake]", "-e emet-providers", "-e emet-engine"]
    fetch, replay = fake.ran("generate.py"), fake.ran("replay.py")
    assert len(fetch) == 1 and words(fetch[0])[:7] == [PY, "tools/bank/generate.py", "--fetch", URL, "--sha256",
                                                        HEX, "--to"]
    assert words(fetch[0])[7].startswith(TEMP) and words(fetch[0])[7].endswith("bank-small")
    assert len(replay) == 1
    bank = words(replay[0])[2]
    assert words(replay[0])[:2] == [PY, "tools/bank/replay.py"] and bank.startswith(TEMP)
    assert bank.endswith("genbank-small-2026-11-26")
    assert words(replay[0])[3:] == ["--platform", "linux-x86_64", "--golden", ci.BANK_GOLDEN]
    assert "no cassettes in the store yet" in ctx.out.text
    assert snapshot(root) == before
    ctx.close()
    assert list((tmp_path / "temp").iterdir()) == []


def test_an_archive_the_fetch_refuses_stops_the_job_before_any_replay(tmp_path):
    """`generate.py --fetch` keeps nothing whose sha256 is not the store's;
    the job must stop there and never replay what it did not keep."""
    root = bank_tree(tmp_path)
    refused = (f"generate: {URL}: its sha256 is {'0' * 64}, and {HEX} was asked for; nothing was kept\n")
    fake = Fake().on("--fetch", code=1, out=refused)
    ctx = linux(root, fake, env={"GITHUB_ACTIONS": "true"})
    assert ci.run_jobs(ctx, ["bank"]) == 0
    assert fake.ran("--fetch") and not fake.ran("replay.py")
    assert "::warning title=bank (advisory)::failed: fetch: generate.py --fetch exited 1, so no bank was kept" \
        in ctx.out.text
    empty = ci.run_job(linux(root, Fake()), ci.BY_KEY["bank"])
    assert empty.status == ci.FAILED and "the fetch kept no bank at genbank-small-2026-11-26" in empty.reason


RELEASE = REPO.parent / "genbank" / "release"
ARCHIVE = RELEASE / "genbank-small-2026-11-26.tar.gz"


@pytest.mark.skipif(not ARCHIVE.is_file(), reason="the generated bank's packed small set is not on this machine")
def test_the_real_fetch_keeps_the_archive_only_at_the_stores_hash(tmp_path):
    """The job's fetch with the real `generate.py`, from a file URL of the
    packed small set (stream C3's, outside the repository): a hash that is
    not the archive's keeps nothing and stops the job; the archive's own
    keeps a bank that passed `check.py`. The copied tree has no `replay.py`,
    so nothing is replayed."""
    root = tmp_path / "emet"
    shutil.copytree(REPO / "tools" / "bank", root / "tools" / "bank",
                    ignore=shutil.ignore_patterns("golden", "__pycache__", "replay.py"))
    digest = (RELEASE / f"{ARCHIVE.name}.sha256").read_text(encoding="utf-8").split()[0]
    for given in ("0" * 64, digest):
        (root / ci.BANK_STORE).write_text(json.dumps(store((ARCHIVE.as_uri(), given))), encoding="utf-8")
        ctx = ci.Context(root=root, env=dict(os.environ), out=Said(), python=sys.executable, temp=tmp_path)
        try:
            result = ci.run_job(ctx, ci.BY_KEY["bank"])
            outcomes = {r.step.key: r.outcome for r in result.steps}
            kept = ctx.state.get("bank")
            if given == digest:
                assert outcomes["fetch"].status == ci.PASSED and (kept / "bank.json").is_file(), ctx.out.text
                assert "total: 5 of 5 recording(s) sound, 75 take(s)" in ctx.out.text, ctx.out.text
                assert outcomes["replay"] == ci.Outcome(ci.NOT_RUN, "tools/bank/replay.py is missing")
            else:
                assert result.status == ci.FAILED and kept is None, ctx.out.text
                assert result.reason == "fetch: generate.py --fetch exited 1, so no bank was kept"
                assert "nothing was kept" in ctx.out.text and "replay" not in outcomes
                assert not any((ctx._scratch / "bank-small").iterdir())
        finally:
            ctx.close()


def test_the_stores_cassettes_answer_for_the_transcriber_when_the_bank_carries_them(tmp_path):
    root = bank_tree(tmp_path)
    fake = Fake().on("--fetch", do=fetched(cassettes=True))
    ctx = linux(root, fake)
    assert ci.run_job(ctx, ci.BY_KEY["bank"]).status == ci.PASSED
    replay = words(fake.ran("replay.py")[0])
    assert replay[-2] == "--cassette" and replay[-1].endswith(os.path.join("genbank-small-2026-11-26", "cassettes"))
    assert "the store's cassettes: cassettes/ in genbank-small-2026-11-26" in ctx.out.text


def test_a_recording_with_no_golden_here_fails_and_its_golden_is_written_for_review(tmp_path):
    root = bank_tree(tmp_path, goldens=IDS[:1])
    out = str(tmp_path / "runner-temp" / "emet-bank-goldens")
    fake = Fake().on("--fetch", do=fetched())
    ctx = linux(root, fake, env={ci.GOLDEN_OUT: out})
    result = ci.run_job(ctx, ci.BY_KEY["bank"])
    compare, update = fake.ran("replay.py")
    assert words(compare)[3:] == ["--platform", "linux-x86_64", "--golden", ci.BANK_GOLDEN, "--only", IDS[0]]
    assert words(update)[3:] == ["--platform", "linux-x86_64", "--golden", out.replace(str(tmp_path), "TMP"),
                                 "--update", "--only", IDS[1]]
    assert result.status == ci.FAILED and result.reason == "replay: 1 of 2 recording(s) have no golden for linux-x86_64"
    assert result.notes and "come back in a pull request into tools/bank/golden/generated/linux-x86_64/" \
        in result.notes[0]

    # The first run on a platform: nothing held, every golden written, never green.
    none_held = Fake().on("--fetch", do=fetched())
    first = ci.run_job(linux(bank_tree(tmp_path / "first", goldens=()), none_held, env={ci.GOLDEN_OUT: out}),
                       ci.BY_KEY["bank"])
    assert [w for w in words(none_held.ran("replay.py")[0]) if w in IDS or w == "--update"] == ["--update", *[
        rid for rid in IDS]] and len(none_held.ran("replay.py")) == 1
    assert first.status == ci.FAILED and "2 of 2 recording(s) have no golden" in first.reason

    # Without the folder to write into, nothing is written and the commands are named: the
    # fetch, since the job's bank is gone when it ends, and an update of the lacking alone.
    laptop = Fake().on("--fetch", do=fetched())
    ctx = linux(bank_tree(tmp_path / "laptop", goldens=IDS[:1]), laptop)
    assert ci.run_job(ctx, ci.BY_KEY["bank"]).status == ci.FAILED
    assert len(laptop.ran("replay.py")) == 1 and not laptop.ran("--update")
    hint = next(line for line in ctx.out.text.splitlines() if line.startswith("to write only these"))
    assert f"python tools/bank/generate.py --fetch {URL} --sha256 {HEX} --to DIR, then " in hint
    assert hint.endswith(f"replay.py DIR/genbank-small-2026-11-26 --golden {ci.BANK_GOLDEN} --update "
                         f"--platform linux-x86_64 --only {IDS[1]}")


def test_a_fetched_bank_that_lists_no_recording_fails_and_replays_nothing(tmp_path):
    """A step that held nothing to a golden is never green."""
    fake = Fake().on("--fetch", do=fetched(ids=()))
    result = ci.run_job(linux(bank_tree(tmp_path), fake), ci.BY_KEY["bank"])
    replay = {r.step.key: r.outcome for r in result.steps}["replay"]
    assert replay == ci.Outcome(ci.FAILED, "genbank-small-2026-11-26 lists no recording")
    assert result.status == ci.FAILED and not fake.ran("replay.py")


def test_a_golden_that_moved_fails_the_job(tmp_path):
    root = bank_tree(tmp_path)
    fake = Fake().on("--fetch", do=fetched()).on("replay.py", code=1, out="gen-g01-near-1: CHANGED\n")
    result = ci.run_job(linux(root, fake), ci.BY_KEY["bank"])
    assert result.status == ci.FAILED and "replay.py exited 1 against the goldens for linux-x86_64" in result.reason


def test_the_full_set_reads_its_own_entry(tmp_path):
    root = bank_tree(tmp_path, store((URL, HEX), (FULL_URL, "f" * 64)))
    fake = Fake().on("--fetch", do=fetched())
    assert ci.run_job(linux(root, fake), ci.BY_KEY["bank-full"]).status == ci.PASSED
    assert f"--fetch {FULL_URL} --sha256 {'f' * 64}" in fake.ran("--fetch")[0]
    assert words(fake.ran("replay.py")[0])[2].endswith("genbank-full-2026-11-26")


# ------------------------------------------------------------ the workflows


def workflow(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True, doc.get("on"))         # YAML 1.1 reads a bare `on` as true
    return doc


#: A `tools/ci.py` command in a step's `run:` text, its words up to a shell
#: separator or a comment.
CI_CALL = re.compile(r"python[0-9.]*\s+tools/ci\.py((?:\s+[^\s#|&;]+)*)")


def calls(job: dict, key: str) -> list[dict]:
    """The steps of a workflow job that run this `tools/ci.py` key, wherever
    the key sits among the command's words."""
    return [step for step in job.get("steps", [])
            if any(key in [word for word in match.group(1).split() if not word.startswith("-")]
                   for match in CI_CALL.finditer(str(step.get("run", ""))))]


def test_a_job_key_is_read_wherever_it_sits_among_the_words():
    for run in ("python tools/ci.py bank bank-full --install", "python tools/ci.py --install bank-full",
                "python tools/ci.py bank --install && python tools/ci.py bank-full"):
        assert calls({"steps": [{"run": run}]}, "bank-full"), run
    assert not calls({"steps": [{"run": "python tools/ci.py bank --install  # not bank-full"}]}, "bank-full")


def test_the_full_set_runs_weekly_and_never_on_a_pull_request():
    """Every workflow job that runs `tools/ci.py bank-full` is in a workflow
    whose only triggers are the schedule and a run by hand."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = workflow(path.name)
        for key, job in doc["jobs"].items():
            if calls(job, "bank-full"):
                found.append(path.name)
                assert set(doc["on"]) <= {"schedule", "workflow_dispatch"}, (path.name, doc["on"])
                assert "schedule" in doc["on"] and "if" not in job
    assert found == ["weekly.yml"]


def test_the_small_set_runs_on_every_pull_request():
    doc = workflow("ci.yml")
    jobs = [job for job in doc["jobs"].values() if calls(job, "bank")]
    assert len(jobs) == 1 and "pull_request" in doc["on"] and "if" not in jobs[0]
    assert jobs[0]["name"] == "bank (advisory)" and jobs[0]["runs-on"] == "ubuntu-latest"
    assert [s["with"]["python-version"] for s in jobs[0]["steps"] if "setup-python" in s.get("uses", "")] == ["3.13"]


def test_each_bank_job_uploads_the_folder_it_writes_the_goldens_into():
    """`if-no-files-found: ignore` would hide a path that differs from the
    folder the job writes, so the two are held equal here."""
    for name, key, artifact in (("ci.yml", "bank", "generated-goldens-small"),
                                ("weekly.yml", "bank-full", "generated-goldens-full")):
        job = next(job for job in workflow(name)["jobs"].values() if calls(job, key))
        written = calls(job, key)[0]["env"][ci.GOLDEN_OUT]
        uploads = [s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")]
        assert len(uploads) == 1 and job["steps"][-1] is uploads[0]
        assert uploads[0]["with"]["path"] == written == "${{ runner.temp }}/emet-bank-goldens"
        assert uploads[0]["with"]["name"] == artifact and job["timeout-minutes"] > 0


# ------------------------------------------------------------ the live run

SINKS = "0\temet_null\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE\n"

#: What emet-listen prints on a live run that heard its name once.
HEARD_ONCE = ("listening for 'hey emet'\n  engine   pocketsphinx\n  source   microphone\n"
              "  audio    16000 Hz, 80 ms frames\n\n  heard 'hey emet'  (confidence 1.00)\n"
              "    lag 170 ms, name cut 0 ms, level stop alone 0 ms\n\nstopped. heard it 1 time(s).\n"
              "live run: the player exited 0, the listener 0\n")

TOOLS = ("bash", "timeout", "pactl", "paplay")


def sound(root: Path, fake: Fake, **how):
    return context(root, fake, os_name="linux", machine="x86_64", env={"EMET_VIRTUAL_AUDIO": "1"},
                   on_path=TOOLS, **how)


def test_the_live_run_names_only_the_virtual_card_and_hears_one_wake(tmp_path):
    root = make_tree(tmp_path, ci.LIVE_CLIP)
    manifests, keys = [], []

    def listen(argv: list[str]) -> None:
        manifests.append(Path(argv[argv.index("--transcribe") - 2]).read_text(encoding="utf-8"))
        keys.append(Path(argv[argv.index("--keys") + 1]).read_text(encoding="utf-8"))

    fake = Fake().on("pactl list short sinks", out=SINKS).on("bash -c", out=HEARD_ONCE, do=listen)
    ctx = sound(root, fake, install=True)
    result = ci.run_job(ctx, ci.BY_KEY["sound"])
    live = {r.step.key: r.outcome for r in result.steps}["live"]
    assert live.status == ci.PASSED and result.status == ci.PASSED, ctx.out.text
    # After the tests: the three installs, then the run, in that order.
    tests_at = next(i for i, line in enumerate(fake.lines) if "test_audio_virtual.py" in line)
    assert [line.split("pip install ")[1] for line in fake.lines[tests_at + 1:tests_at + 4]] == [
        "-e emet-hal[wake]", "-e emet-providers", "-e emet-engine"]
    runs = fake.ran("bash -c")
    assert len(runs) == 1 and len(manifests) == 1
    after_script = runs[0].split(ci.LIVE_SCRIPT, 1)[1].split()
    assert after_script[:5] == ["live-run", "30", "5", ci.NULL_SINK,
                                str(root / ci.LIVE_CLIP).replace(str(tmp_path), "TMP")]
    assert "emet-listen" in after_script[5] and after_script[7].endswith("emet-soul.yaml")
    assert after_script[8:10] == ["--transcribe", "--stats"] and "--replay" not in after_script
    # No key and no carried mean of the runner's: an empty keys file and a scratch state folder.
    work = after_script[13]
    assert after_script[10:] == ["--keys", os.path.join(work, "keys.env"), "--state", work]
    assert work.startswith(TEMP) and keys == [""]
    # A player that failed is the run's failure, whatever the listener said.
    assert re.search(r'^if \[ "\$played" -ne 0 \]; then\n  exit 3\nfi\nexit "\$listened"\n\Z', ci.LIVE_SCRIPT,
                     re.MULTILINE)

    # Every name the run opens or plays into: the manifest's two devices, and
    # the sink the script hands paplay, which is its third argument.
    assert 'paplay --device="$sink"' in ci.LIVE_SCRIPT and "sink=$3" in ci.LIVE_SCRIPT
    named = set(re.findall(r'device: "([^"]+)"', manifests[0])) | {after_script[3]}
    assert named == {ci.VIRTUAL_SOURCE, ci.VIRTUAL_SINK, ci.NULL_SINK}
    assert 'output: {sink: "null"' in manifests[0] and f'input: {{device: "{ci.VIRTUAL_SOURCE}"' in manifests[0]
    tests = (REPO / "emet-hal" / "tests" / "test_audio_virtual.py").read_text(encoding="utf-8")
    assert dict(re.findall(r'^(SINK|SOURCE|NULL_SINK) = "([^"]+)"$', tests, re.MULTILINE)) == {
        "SINK": ci.VIRTUAL_SINK, "SOURCE": ci.VIRTUAL_SOURCE, "NULL_SINK": ci.NULL_SINK}
    for word in ("default", "hw:", "plughw", "USB", "sysdefault"):
        assert word not in manifests[0] and word not in " ".join(after_script)
    assert result.notes == [f"live run on the virtual card: one wake in 30 s, the clip hey-emet-ljspeech.wav "
                            "played into the null sink"]


def test_the_live_run_refuses_a_card_without_the_null_sink_and_a_count_other_than_one(tmp_path):
    root = make_tree(tmp_path, ci.LIVE_CLIP)
    no_sink = Fake().on("pactl list short sinks", out="0\tauto_null\tmodule-null-sink.c\n")
    result = ci.run_job(sound(root, no_sink), ci.BY_KEY["sound"])
    live = {r.step.key: r.outcome for r in result.steps}["live"]
    assert live == ci.Outcome(ci.FAILED, "PulseAudio lists no emet_null sink, so nothing was played or opened")
    assert not no_sink.ran("bash -c") and result.status == ci.PASSED       # advisory: the check stays green

    twice = HEARD_ONCE.replace("  heard", "  heard 'hey emet'  (confidence 0.90)\n  heard", 1).replace(
        "heard it 1 time", "heard it 2 time")
    fake = Fake().on("pactl list short sinks", out=SINKS).on("bash -c", out=twice)
    result = ci.run_job(sound(root, fake), ci.BY_KEY["sound"])
    assert result.parts_failed_advisory == ["sound/live"] and result.status == ci.PASSED
    assert "it heard its name 2 time(s); the clip says it once" in {r.step.key: r.outcome for r in result.steps}[
        "live"].reason


def test_the_live_runs_installs_are_advisory_inside_the_blocking_sound_job(tmp_path):
    """The live run's three installs sit in `virtual sound card`, a check the
    audit holds a merge on: one that fails warns and leaves it green."""
    root = make_tree(tmp_path, ci.LIVE_CLIP)
    fake = Fake().on("-e emet-engine", code=1).on("pactl list short sinks", out=SINKS)
    ctx = context(root, fake, os_name="linux", machine="x86_64", on_path=TOOLS, install=True,
                  env={"EMET_VIRTUAL_AUDIO": "1", "GITHUB_ACTIONS": "true"})
    result = ci.run_job(ctx, ci.BY_KEY["sound"])
    assert result.status == ci.PASSED and result.parts_failed_advisory == ["sound/install"]
    assert ci.verdict([result], strict=True)[1] == 0 and not fake.ran("bash -c")
    assert "=== sound: virtual sound card (blocking)\n" in ctx.out.text


def test_a_live_run_reads_only_a_live_listener_stopped_at_the_end_of_its_window():
    assert ci.live_problems(0, HEARD_ONCE) == []
    replayed = HEARD_ONCE.replace("source   microphone", "source   wav").replace(
        "stopped. heard it 1", "source ended. heard it 1")
    assert ci.live_problems(0, replayed) == [
        "emet-listen did not listen through the microphone source (its header says wav)",
        "the run did not end on emet-listen's stopped line for that count"]
    assert ci.live_problems(3, HEARD_ONCE) == ["the run exited 3"]
    silent = HEARD_ONCE.replace("  heard 'hey emet'  (confidence 1.00)\n", "").replace("heard it 1", "heard it 0")
    assert ci.live_problems(0, silent) == ["it heard its name 0 time(s); the clip says it once"]


def test_the_live_run_is_not_run_without_its_tools_or_its_card(tmp_path):
    root = make_tree(tmp_path, ci.LIVE_CLIP)
    no_player = context(root, Fake(), os_name="linux", env={"EMET_VIRTUAL_AUDIO": "1"},
                        on_path=("bash", "timeout", "pactl"))
    result = ci.run_job(no_player, ci.BY_KEY["sound"])
    assert result.parts_not_run == ["sound/live"] and result.status == ci.PASSED
    assert {r.step.key: r.outcome for r in result.steps}["live"].reason == "needs paplay"
    laptop = ci.run_job(context(root, Fake()), ci.BY_KEY["sound"])
    assert laptop.status == ci.NOT_RUN and laptop.reason.startswith("needs the virtual sound card")
