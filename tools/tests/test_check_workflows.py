"""The workflows' house rules, `tools/ci/check_workflows.py`, mostly against
copies of the repository's workflows.

Most tests copy the repository's three workflows, its Dependabot file and
the real `tools/ci.py` into a tree under `tmp_path`, change a line or two
of a copy, and read what the check says. Some write a small workflow of
their own or hand the reader a string. One holds the repository's own files
to every rule, and the last group reads them with PyYAML, a reader apart
from the checker's, for what these workflows in particular must say: their
triggers, runners and grants. The repository's own files are only read.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

CHECK = Path(__file__).resolve().parents[1] / "ci" / "check_workflows.py"
REPO = CHECK.parents[2]
GITHUB = REPO / ".github"
WORKFLOWS = GITHUB / "workflows"
SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
CHECKOUT = f"      - uses: actions/checkout@{SHA} # v7.0.1\n        with:\n          persist-credentials: false\n"
SETUP = ('      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0\n'
         '        with:\n          python-version: "3.13"\n')
LAYERING = "      - run: python tools/ci.py layering --install\n"


def _load():
    spec = importlib.util.spec_from_file_location("emet_check_workflows", CHECK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    writes = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes
    return module


cw = _load()

needs_workflows = pytest.mark.skipif(not (WORKFLOWS / "ci.yml").is_file() or not (REPO / "tools" / "ci.py").is_file(),
                                     reason="the workflows or tools/ci.py are not in this tree")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    for path in [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]:
        shutil.copy2(path, tmp_path / ".github" / "workflows" / path.name)
    shutil.copy2(GITHUB / "dependabot.yml", tmp_path / ".github" / "dependabot.yml")
    (tmp_path / "tools").mkdir()
    shutil.copy2(REPO / "tools" / "ci.py", tmp_path / "tools" / "ci.py")
    return tmp_path


def edit_file(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name} no longer holds {old!r}; the test needs a new anchor"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def edit(root: Path, name: str, old: str, new: str, count: int = 1) -> None:
    edit_file(root / ".github" / "workflows" / name, old, new, count)


def append(root: Path, name: str, text: str) -> None:
    path = root / ".github" / "workflows" / name
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def problems(root: Path) -> list[str]:
    return cw.check(root)


def one_file(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "w.yml"
    path.write_text(text, encoding="utf-8")
    return cw.check_file(path, "w.yml")[0]


HEAD = "permissions:\n  contents: read\njobs:\n  one:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"


@needs_workflows
def test_the_repository_workflows_keep_every_rule(capsys):
    assert cw.main([str(REPO)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("workflows: ok")


@needs_workflows
def test_a_broken_rule_is_exit_1_with_the_count_last(tree, capsys):
    """The layering job reads the script's exit code alone."""
    edit(tree, "ci.yml", f"actions/checkout@{SHA} # v7.0.1", "actions/checkout@v7.0.1")
    assert cw.main([str(tree)]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "workflows: 1 problem(s)", out
    assert "actions/checkout@v7.0.1 is not pinned to a full commit SHA" in out[0]


@needs_workflows
def test_the_check_leaves_no_bytecode_in_the_tree(tree):
    cw.check(tree)
    assert not (tree / "tools" / "__pycache__").exists()


@needs_workflows
def test_an_action_pinned_to_a_tag_fails(tree):
    edit(tree, "ci.yml", f"actions/checkout@{SHA} # v7.0.1", "actions/checkout@v7.0.1")
    found = problems(tree)
    assert any("actions/checkout@v7.0.1 is not pinned to a full commit SHA" in p for p in found), found


@needs_workflows
def test_a_short_sha_fails(tree):
    edit(tree, "weekly.yml", "@23f22cd43608bae7a1f1fa2603d7314dee9538a5", "@23f22cd")
    assert any("not pinned to a full commit SHA" in p for p in problems(tree))


@needs_workflows
def test_a_sha_without_its_version_comment_fails(tree):
    edit(tree, "release-audit.yml", "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0",
         "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0")
    found = problems(tree)
    assert any("release-audit.yml" in p and "no version comment" in p for p in found), found


@needs_workflows
def test_a_uses_that_is_not_the_steps_first_key_is_held_to_the_pin(tree):
    edit(tree, "ci.yml", SETUP, '      - name: Set up Python\n        uses: actions/setup-python@v7.1.0\n'
                                '        with:\n          python-version: "3.13"\n')
    found = problems(tree)
    assert any("actions/setup-python@v7.1.0 is not pinned to a full commit SHA" in p for p in found), found


@pytest.mark.parametrize("uses, wanted", [
    (f"actions/checkout@{SHA}x # v7.0.1", f"actions/checkout@{SHA}x is not pinned to a full commit SHA"),
    (f"actions/checkout@{SHA} # pinned", "has no version comment beside it"),
    (f'"actions/checkout@{SHA}" # v7.0.1', None),
], ids=["sha-and-more", "comment-no-version", "quoted-target"])
def test_the_pin_rule_at_its_edges(tmp_path, uses, wanted):
    found = one_file(tmp_path, HEAD + f"    steps:\n      - uses: {uses}\n        with:\n"
                                      "          persist-credentials: false\n")
    if wanted is None:
        assert found == []
    else:
        assert len(found) == 1 and wanted in found[0], found


def test_a_local_action_is_exempt_and_a_docker_image_needs_its_digest(tmp_path):
    found = one_file(tmp_path, HEAD + "    steps:\n      - uses: ./.github/actions/local\n"
                                      "      - uses: docker://alpine:3.20\n"
                                      "      - uses: docker://alpine@sha256:" + "a" * 64 + "\n")
    assert found == ["w.yml:9: docker://alpine:3.20 is not pinned to an image digest (@sha256:...)"]


@needs_workflows
@pytest.mark.parametrize("step_timeout", [False, True], ids=["none", "on-a-step"])
def test_a_job_without_a_timeout_fails(tree, step_timeout):
    """A step's timeout bounds that step alone, so it never stands in for
    the job's."""
    edit(tree, "ci.yml", "    name: wheel install\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n",
         "    name: wheel install\n    runs-on: ubuntu-latest\n")
    if step_timeout:
        edit(tree, "ci.yml", "      - run: python tools/ci.py wheel --install\n",
             "      - run: python tools/ci.py wheel --install\n        timeout-minutes: 5\n")
    found = problems(tree)
    assert any("job wheel has no timeout-minutes" in p for p in found), found


def test_a_job_that_calls_a_reusable_workflow_needs_no_timeout_and_a_pin(tmp_path):
    assert one_file(tmp_path, "permissions:\n  contents: read\njobs:\n  call:\n"
                              "    uses: ./.github/workflows/other.yml\n") == []
    found = one_file(tmp_path, "permissions:\n  contents: read\njobs:\n  call:\n"
                               "    uses: octo/repo/.github/workflows/other.yml@v1\n")
    assert found == ["w.yml:5: octo/repo/.github/workflows/other.yml@v1 is not pinned to a full commit SHA"]


@needs_workflows
def test_an_expression_inside_a_run_line_fails_and_one_in_env_passes(tree):
    edit(tree, "weekly.yml", "      - run: python tools/ci.py pip-audit --install",
         "      - run: python tools/ci.py pip-audit --install ${{ github.ref }}")
    edit(tree, "ci.yml", "          pactl info\n", "          pactl info ${{ github.sha }}\n")
    found = [p for p in problems(tree) if "expression inside a run" in p]
    assert len(found) == 2, found
    assert any(p.startswith(".github/workflows/weekly.yml:") for p in found)
    assert any(p.startswith(".github/workflows/ci.yml:") for p in found)


@needs_workflows
def test_a_folded_block_is_shell_too(tree):
    edit(tree, "ci.yml", "        run: |\n          sudo apt-get update\n",
         "        run: >\n          sudo apt-get update ${{ github.sha }}\n")
    found = problems(tree)
    assert any("ci.yml:" in p and "expression inside a run" in p for p in found), found


@needs_workflows
def test_a_yaml_workflow_is_read_too(tree):
    (tree / ".github" / "workflows" / "extra.yaml").write_text(
        HEAD + "    steps:\n      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n",
        encoding="utf-8")
    found = problems(tree)
    assert any(p.startswith(".github/workflows/extra.yaml:") and "not pinned" in p for p in found), found


@needs_workflows
@pytest.mark.parametrize("new", ["permissions: write-all\n",
                                 "permissions:\n  contents: write\n",
                                 "permissions:\n  contents: read\n  issues: write\n",
                                 ""], ids=["write-all", "contents-write", "issues-write", "none"])
def test_the_top_level_permissions_are_contents_read_alone(tree, new):
    edit(tree, "ci.yml", "permissions:\n  contents: read\n", new)
    found = problems(tree)
    assert any(p.startswith(".github/workflows/ci.yml:") and "permissions" in p for p in found), found


@needs_workflows
@pytest.mark.parametrize("name, old, new, wanted", [
    ("ci.yml", "    name: wheel install\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n",
     "    name: wheel install\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n"
     "    permissions:\n      contents: write\n      actions: write\n",
     "job wheel: permissions contents: write has no comment saying why"),
    ("release-audit.yml", "      pull-requests: read # `gh pr view` reads this pull request's title and body\n",
     "      pull-requests: read\n", "job audit: permissions pull-requests: read has no comment saying why"),
    ("weekly.yml", "    name: pip-audit (advisory)\n    runs-on: ubuntu-latest\n",
     "    name: pip-audit (advisory)\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n"
     "      issues: write\n", "job pip-audit: permissions issues: write has no comment saying why"),
    ("weekly.yml", "    name: pip-audit (advisory)\n    runs-on: ubuntu-latest\n",
     "    name: pip-audit (advisory)\n    runs-on: ubuntu-latest\n    permissions: write-all\n",
     "job pip-audit: permissions: write-all names no entry"),
    ("weekly.yml", "    name: pip-audit (advisory)\n    runs-on: ubuntu-latest\n",
     "    name: pip-audit (advisory)\n    runs-on: ubuntu-latest\n    permissions: read-all\n",
     "job pip-audit: permissions: read-all names no entry"),
], ids=["wheel-writes", "audit-reason-dropped", "pip-audit-issues-write", "write-all", "read-all"])
def test_a_job_given_more_permissions_says_why(tree, name, old, new, wanted):
    edit(tree, name, old, new)
    found = problems(tree)
    assert any(wanted in p for p in found), found


def test_a_job_may_take_fewer_permissions_without_a_reason(tmp_path):
    assert one_file(tmp_path, HEAD + "    permissions: {}\n  two:\n    runs-on: ubuntu-latest\n"
                                     "    timeout-minutes: 5\n    permissions:\n      contents: read\n"
                                     "      issues: none\n") == []


@needs_workflows
@pytest.mark.parametrize("new", ["          fetch-depth: 0\n", "          fetch-depth: 0\n          persist-credentials: true\n"],
                         ids=["line-removed", "set-true"])
def test_a_checkout_that_keeps_its_token_fails(tree, new):
    edit(tree, "release-audit.yml", "          fetch-depth: 0\n          persist-credentials: false\n", new)
    found = problems(tree)
    assert any("job audit: a checkout without persist-credentials: false" in p for p in found), found


@needs_workflows
@pytest.mark.parametrize("name, old, new", [
    ("ci.yml", "  group: ci-${{ github.event_name }}-${{ github.ref }}\n", "  group: ci-${{ github.ref }}\n"),
    ("weekly.yml", "  group: weekly-${{ github.event_name }}-${{ github.ref }}\n", "  group: weekly-${{ github.ref }}\n"),
    ("ci.yml", "    name: layering\n    runs-on: ubuntu-latest\n",
     "    name: layering\n    runs-on: ubuntu-latest\n    concurrency: layering-${{ github.ref }}\n"),
], ids=["ci-by-ref", "weekly-by-ref", "job-by-ref"])
def test_a_scheduled_workflow_names_the_event_in_its_concurrency_group(tree, name, old, new):
    edit(tree, name, old, new)
    found = problems(tree)
    assert any(f"{name}:" in p and "a concurrency group without github.event_name" in p for p in found), found


@needs_workflows
def test_a_workflow_no_schedule_starts_may_group_by_ref(tree):
    edit(tree, "release-audit.yml", "permissions:\n  contents: read\n",
         "permissions:\n  contents: read\n\nconcurrency:\n  group: audit-${{ github.ref }}\n")
    assert problems(tree) == []


@needs_workflows
def test_a_renamed_required_check_fails(tree):
    edit(tree, "ci.yml", "    name: test (no extras)\n", "    name: test (bare)\n")
    found = problems(tree)
    assert any("job bare is named test (bare)" in p for p in found), found
    assert any("the check test (no extras) (job bare) is no workflow job's name" in p for p in found), found


@needs_workflows
def test_the_matrix_is_expanded_into_the_check_names(tree):
    edit(tree, "ci.yml", 'python-version: ["3.11", "3.12", "3.13"]', 'python-version: ["3.11", "3.13"]')
    found = problems(tree)
    assert any("the check test (py3.12) (job test) is no workflow job's name" in p for p in found), found


@needs_workflows
def test_a_matrix_entry_tools_ci_does_not_report_fails(tree):
    edit(tree, "ci.yml", 'python-version: ["3.11", "3.12", "3.13"]', 'python-version: ["3.11", "3.12", "3.13", "3.14"]')
    found = problems(tree)
    assert any("job test is named" in p and "test (py3.14)" in p for p in found), found


@needs_workflows
def test_a_check_with_no_workflow_job_fails(tree):
    (tree / ".github" / "workflows" / "weekly.yml").unlink()
    found = problems(tree)
    assert any("the check pip-audit (advisory) (job pip-audit) is no workflow job's name" in p for p in found), found


@needs_workflows
def test_a_job_key_tools_ci_does_not_have_fails(tree):
    edit(tree, "ci.yml", "run: python tools/ci.py docs --install", "run: python tools/ci.py doc --install")
    found = problems(tree)
    assert any("runs tools/ci.py doc, which is no job of tools/ci.py" in p for p in found), found


DECOY = "\n  decoy:\n    name: {name}\n    runs-on: ubuntu-latest\n    timeout-minutes: 1\n    steps:\n      - run: {run}\n"


@needs_workflows
def test_one_check_name_on_two_jobs_fails(tree):
    append(tree, "ci.yml", DECOY.format(name="layering", run="python tools/ci.py layering --install"))
    found = problems(tree)
    assert any("the check name layering is also .github/workflows/ci.yml:" in p for p in found), found


@needs_workflows
@pytest.mark.parametrize("name, check", [("ci.yml", "layering"), ("weekly.yml", "test (py3.12)"),
                                         ("release-audit.yml", "release audit")],
                         ids=["ci", "weekly", "release-audit"])
def test_a_job_named_for_a_check_that_runs_no_tools_ci_fails(tree, name, check):
    append(tree, name, DECOY.format(name=f'"{check}"', run="echo ok"))
    found = problems(tree)
    assert any(f"job decoy is named {check}, the check of tools/ci.py" in p and "runs no tools/ci.py" in p
               for p in found), found


@needs_workflows
def test_a_job_renamed_to_a_check_as_it_stands_fails(tree):
    edit(tree, "weekly.yml", "    name: scorecard\n", "    name: layering\n")
    found = problems(tree)
    assert any("job scorecard is named layering, the check of tools/ci.py layering, and runs no tools/ci.py" in p
               for p in found), found


@needs_workflows
@pytest.mark.parametrize("name, old, new", [
    ("ci.yml", LAYERING, "      - run: python tools/ci.py layering --install || true\n"),
    ("release-audit.yml", "        run: python tools/ci.py audit --install\n",
     "        run: python tools/ci.py audit --install; exit 0\n"),
    ("ci.yml", LAYERING, "      - run: |\n          python tools/ci.py layering --install\n          exit 0\n"),
    ("ci.yml", LAYERING, "      - run: echo start && python tools/ci.py layering --install\n"),
    ("weekly.yml", "      - run: python tools/ci.py pip-audit --install\n", "      - run: python tools/ci.py pip-audit\n"),
], ids=["or-true", "semicolon-exit-0", "block-exit-0", "before-the-call", "no-install"])
def test_a_step_that_calls_tools_ci_runs_the_call_and_nothing_else(tree, name, old, new):
    edit(tree, name, old, new)
    found = problems(tree)
    assert any("a step that calls tools/ci.py runs `python tools/ci.py <key> --install` and nothing else" in p
               for p in found), found


@needs_workflows
def test_a_tools_ci_call_alone_in_a_block_counts(tree):
    edit(tree, "ci.yml", LAYERING, "      - run: |\n          python tools/ci.py layering --install\n")
    assert problems(tree) == []


@needs_workflows
@pytest.mark.parametrize("name, old, new, wanted", [
    ("release-audit.yml", "      - env:\n          GH_TOKEN:", "      - continue-on-error: true\n        env:\n          GH_TOKEN:",
     "job audit: the step that runs tools/ci.py takes no continue-on-error:"),
    ("ci.yml", LAYERING, "      - if: false\n        run: python tools/ci.py layering --install\n",
     "job layering: the step that runs tools/ci.py takes no if:"),
    ("ci.yml", LAYERING, "      - shell: sh\n        run: python tools/ci.py layering --install\n",
     "job layering: the step that runs tools/ci.py takes no shell:"),
    ("ci.yml", "    name: layering\n    runs-on: ubuntu-latest\n",
     "    name: layering\n    runs-on: ubuntu-latest\n    continue-on-error: true\n",
     "job layering runs tools/ci.py and takes no continue-on-error:"),
    ("ci.yml", SETUP, SETUP + "        continue-on-error: true\n",
     "job layering runs tools/ci.py, so no step of it takes continue-on-error:"),
    ("ci.yml", "permissions:\n  contents: read\n", "permissions:\n  contents: read\n\ndefaults:\n  run:\n    shell: sh\n",
     "job layering runs tools/ci.py under a default shell the workflow names"),
    ("ci.yml", "    name: layering\n    runs-on: ubuntu-latest\n",
     "    name: layering\n    runs-on: ubuntu-latest\n    defaults:\n      run:\n        shell: sh\n",
     "job layering runs tools/ci.py under a default shell it names"),
], ids=["step-continue-on-error", "step-if", "step-shell", "job-continue-on-error", "other-step-continue-on-error",
        "workflow-default-shell", "job-default-shell"])
def test_nothing_turns_a_tools_ci_job_green_over_the_scripts_exit(tree, name, old, new, wanted):
    edit(tree, name, old, new)
    found = problems(tree)
    assert any(wanted in p for p in found), found


@needs_workflows
@pytest.mark.parametrize("old, new, wanted", [
    (CHECKOUT, "      -   uses: actions/checkout@v4\n          with:\n            persist-credentials: true\n",
     ["actions/checkout@v4 is not pinned", "job layering: a checkout without persist-credentials: false"]),
    (SETUP, "      - {uses: actions/setup-python@v7.1.0}\n", ["a flow mapping"]),
    (SETUP, "      - uses: actions/setup-python@v7.1.0\n        with: {python-version: \"3.13\"}\n",
     ["a flow mapping", "actions/setup-python@v7.1.0 is not pinned"]),
    (LAYERING, LAYERING + "          ${{ github.event.pull_request.title }}\n", ["the value of line"]),
    (LAYERING, '      - run: "python tools/ci.py layering --install\n          ${{ github.run_id }}"\n',
     ["a quoted value left open"]),
    (LAYERING, "      - run:\n          python tools/ci.py layering --install\n", ["the value of line"]),
    (SETUP, "      - uses: *setup\n", ["an anchor, an alias or a tag"]),
], ids=["dash-and-three-spaces", "flow-mapping-step", "flow-mapping-value", "plain-value-goes-on",
        "quoted-value-goes-on", "value-on-the-next-line", "alias"])
def test_a_spelling_the_reader_does_not_follow_fails(tree, old, new, wanted):
    """Each spelling is valid YAML that a line reader could pass over."""
    edit(tree, "ci.yml", old, new)
    found = problems(tree)
    for text in wanted:
        assert any(p.startswith(".github/workflows/ci.yml:") and text in p for p in found), (text, found)


def test_a_list_at_its_keys_indentation_fails(tmp_path):
    found = one_file(tmp_path, HEAD + "    steps:\n    - uses: actions/checkout@v4\n")
    assert "w.yml:8: a list at its key's indentation; indent it under the key" in found, found
    assert any("actions/checkout@v4 is not pinned" in p for p in found), found


@needs_workflows
@pytest.mark.parametrize("old, new, wanted", [
    ("    directory: /tools/ci\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 7\n",
     "    directory: /tools/ci\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 3\n",
     "the uv entry for /tools/ci waits default-days: 3"),
    ("    directory: /emet-sdk\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 7\n",
     "    directory: /emet-sdk\n    schedule:\n      interval: monthly\n",
     "the pip entry for /emet-sdk has no cooldown:"),
    ("    directory: /tools/bank\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 7\n",
     "    directory: /tools/bank\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 7\n"
     "      semver-patch-days: 1\n",
     "the pip entry for /tools/bank waits semver-patch-days: 1"),
    ("    directory: /emet-hal\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 7\n",
     "    directory: /emet-hal\n    schedule:\n      interval: monthly\n    cooldown:\n      default-days: 7\n"
     '      exclude: ["*"]\n',
     "the pip entry for /emet-hal narrows the wait with exclude:"),
], ids=["uv-3-days", "sdk-no-cooldown", "bank-patch-1-day", "hal-exclude"])
def test_every_dependabot_entry_waits_seven_days(tree, old, new, wanted):
    edit_file(tree / ".github" / "dependabot.yml", old, new)
    found = problems(tree)
    assert any(p.startswith(".github/dependabot.yml:") and wanted in p for p in found), found


@needs_workflows
def test_a_missing_dependabot_file_is_a_problem(tree):
    (tree / ".github" / "dependabot.yml").unlink()
    assert ".github/dependabot.yml is missing, so no pin is kept current" in problems(tree)


def test_a_missing_ci_script_is_a_problem_never_a_pass(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "w.yml").write_text(
        "permissions:\n  contents: read\njobs:\n  one:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n",
        encoding="utf-8")
    (tmp_path / ".github" / "dependabot.yml").write_text(
        "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n"
        "    cooldown:\n      default-days: 7\n", encoding="utf-8")
    assert cw.check(tmp_path) == ["tools/ci.py is missing, so the check names cannot be held to it"]


def test_comments_are_cut_outside_quotes_only():
    assert cw.strip_comment('run: echo "a # b" # why') == 'run: echo "a # b"'
    assert cw.strip_comment("uses: x@0123 # v1") == "uses: x@0123"
    assert cw.strip_comment("url: https://example.invalid/#anchor") == "url: https://example.invalid/#anchor"


def test_a_block_scalar_is_shell_and_never_keys():
    lines = cw.read_lines("jobs:\n  one:\n    steps:\n      - run: |\n          name: not a key\n"
                          "          echo ${{ x }}\n      - uses: a@b\n")
    assert [(line.code, line.scalar) for line in lines][3:6] == [
        ("- run: |", False), ("name: not a key", True), ("echo ${{ x }}", True)]
    assert [text for _, text in cw.run_texts(lines)] == ["name: not a key", "echo ${{ x }}"]


# ------------------------------------------- what these workflows must say


def workflow(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True, doc.get("on"))         # YAML 1.1 reads a bare `on` as true
    return doc


@needs_workflows
@pytest.mark.parametrize("name", ["ci.yml", "weekly.yml"])
def test_the_weekly_runs_start_off_the_hour_and_by_hand(name):
    """CI7 stage 1: a weekly schedule, at a minute GitHub names as less busy
    than the hour's start, and `workflow_dispatch`."""
    on = workflow(name)["on"]
    assert "workflow_dispatch" in on, on
    crons = [entry["cron"] for entry in on["schedule"]]
    assert len(crons) == 1, crons
    minute, hour, day, month, weekday = crons[0].split()
    assert minute.isdigit() and minute != "0" and day == "*" and month == "*" and weekday.isdigit(), crons


@needs_workflows
def test_dco_runs_on_pull_requests_alone_and_mutmut_skips_master():
    jobs = workflow("ci.yml")["jobs"]
    assert jobs["dco"]["if"] == "github.event_name == 'pull_request'"
    assert jobs["mutmut"]["if"] == "github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch'"


@needs_workflows
def test_the_jobs_that_need_a_runner_keep_it():
    """arm64 on another runner is "not run" and green, so only this test
    would see it move; sound's apt names are 24.04's; Scorecard publishes
    from a GitHub-hosted Ubuntu runner alone."""
    ci, weekly = workflow("ci.yml")["jobs"], workflow("weekly.yml")["jobs"]
    assert ci["arm64"]["runs-on"] == "ubuntu-24.04-arm"
    assert ci["sound"]["runs-on"] == "ubuntu-24.04"
    assert weekly["scorecard"]["runs-on"] == "ubuntu-latest"


@needs_workflows
def test_scorecard_publishes_with_exactly_its_two_permissions():
    job = workflow("weekly.yml")["jobs"]["scorecard"]
    assert job["permissions"] == {"security-events": "write", "id-token": "write"}
    (step,) = [s for s in job["steps"] if s.get("uses", "").startswith("ossf/scorecard-action@")]
    assert step["with"]["publish_results"] is True


@needs_workflows
def test_only_scorecard_writes_and_only_what_it_needs():
    """No job opens an issue, pushes or changes a file; Scorecard's two
    writes are its SARIF and its signed results."""
    writes = set()
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        doc = workflow(path.name)
        assert doc["permissions"] == {"contents": "read"}, path.name
        for key, job in doc["jobs"].items():
            grants = job.get("permissions", {})
            assert isinstance(grants, dict), (path.name, key, grants)
            writes |= {(path.name, key, scope) for scope, level in grants.items() if level == "write"}
    assert writes == {("weekly.yml", "scorecard", "security-events"), ("weekly.yml", "scorecard", "id-token")}


@needs_workflows
def test_dependabot_watches_every_package_and_pinned_list():
    """BVP2 stage 1: the pins do not rot. Every folder under tools/ that
    holds a requirements list has an entry, as do the four packages and the
    actions, and each entry waits seven days."""
    doc = yaml.safe_load((GITHUB / "dependabot.yml").read_text(encoding="utf-8"))
    entries = {entry["directory"]: entry for entry in doc["updates"]}
    lists = {"/" + path.parent.relative_to(REPO).as_posix()
             for path in (REPO / "tools").rglob("requirements*.txt") if ".venv" not in path.parts}
    wanted = {"/", "/emet-sdk", "/emet-hal", "/emet-providers", "/emet-engine", *lists}
    assert {"/tools/ci", "/tools/bank"} <= lists, lists
    assert wanted <= set(entries), sorted(wanted - set(entries))
    assert entries["/"]["package-ecosystem"] == "github-actions"
    assert entries["/tools/ci"]["package-ecosystem"] == "uv"
    for folder in wanted:
        assert entries[folder]["cooldown"]["default-days"] >= 7, folder
