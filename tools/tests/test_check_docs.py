"""The documents' check, `tools/check_docs.py`, against small trees written here.

Each test writes a document or two into a temporary repository and runs the
check with its own table of rules, so a `run` command is a `python -c` and
the outcome is known. The last tests hold the real table: nothing it runs
may open audio hardware, reach a vendor or change git state.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import check_docs as cd  # noqa: E402
from check_docs import RUN, SKIP, UNKNOWN, Rule  # noqa: E402

FAKE_RULES = (
    Rule(r"cd (?![~/$])\S+", RUN, "moves the block's working folder"),
    Rule(r'python -c "raise SystemExit\(0\)"', RUN, "passes"),
    Rule(r'python -c "raise SystemExit\(3\)"', RUN, "fails"),
    Rule(r'python -c "import sys; print\(sys\.argv\)"', RUN, "a quoted semicolon"),
    Rule(r"git \S.*", SKIP, "changes git state"),
    Rule(r"pip install .*", SKIP, "installs packages"),
)


def repo(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "emet-sdk").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return tmp_path


def run_check(root: Path, **kwargs) -> cd.Result:
    return cd.check(root, documents=(("README.md", "."),), rules=FAKE_RULES, **kwargs)


def by_line(result: cd.Result) -> dict[int, cd.Outcome]:
    return {o.judged.command.line: o for o in result.outcomes}


# ------------------------------------------------------------- reading


def texts(document: str, rules=FAKE_RULES) -> list[str]:
    return [c.text for c in cd.commands_in("README.md", textwrap.dedent(document), rules)]


def test_an_unlabelled_block_is_read_when_its_first_line_is_a_command():
    assert texts("""\
        ```
        python -c "raise SystemExit(0)"
        ```
        ```
        a: yaml
        ```
        """) == ['python -c "raise SystemExit(0)"']


@pytest.mark.parametrize("first", [
    "sudo apt install libportaudio2", "printf 'x' > /tmp/emet-netcut", "rm /tmp/emet-netcut",
    "<tools-venv>/python tools/bank/label.py --bank ../voicebank", ".venv/bin/python -m pytest",
])
def test_an_unlabelled_block_of_a_command_the_rules_classify_is_read(first):
    assert texts(f"```\n{first}\n```\n", cd.RULES) == [first]


def test_a_heredoc_body_is_not_read_as_commands():
    assert texts("""\
        ```sh
        cat > notes.txt <<EOF
        git push origin master
        EOF
        python -c "raise SystemExit(0)"
        ```
        """) == ["cat > notes.txt <<EOF", 'python -c "raise SystemExit(0)"']


def test_a_line_ending_in_a_backslash_goes_on_with_the_next():
    assert texts("""\
        ```sh
        python -c "raise SystemExit(0)" \\
            --flag
        ```
        """) == ['python -c "raise SystemExit(0)" --flag']


# ------------------------------------------------------------- commands


def test_a_run_command_that_passes_and_one_that_fails(tmp_path):
    root = repo(tmp_path, {"README.md": """\
        ```sh
        python -c "raise SystemExit(0)"
        python -c "raise SystemExit(3)"
        ```
        """})
    found = by_line(run_check(root))
    assert found[2].ok and found[2].judged.kind == RUN
    assert not found[3].ok
    assert found[3].detail.startswith("exited 3, expected 0, in the repository's root")


def test_a_rule_that_expects_a_refusal_fails_a_command_that_exits_0(tmp_path):
    """The real table's invalid-example rule expects `emet validate` to exit 1."""
    rules = (Rule(r'python -c "raise SystemExit\([01]\)"', RUN, "an invalid example, refused", exit=1),)
    root = repo(tmp_path, {"README.md": """\
        ```sh
        python -c "raise SystemExit(1)"
        python -c "raise SystemExit(0)"
        ```
        """})
    found = by_line(cd.check(root, documents=(("README.md", "."),), rules=rules))
    assert found[2].ok
    assert not found[3].ok and found[3].detail.startswith("exited 0, expected 1")


def test_pytest_in_a_document_is_only_collected(tmp_path):
    rules = (Rule(r"pytest(?: \S+)*", RUN, "collected only", collect=True),)
    root = repo(tmp_path, {"README.md": "```sh\npytest emet-sdk/tests\n```\n"})
    ran: list[list[str]] = []
    result = cd.check(root, documents=(("README.md", "."),), rules=rules,
                      runner=lambda argv, cwd: ran.append(argv) or (0, ""))
    assert result.problems == 0
    assert ran[0][-5:] == ["emet-sdk/tests", "--collect-only", "-q", "-p", "no:cacheprovider"]


def test_a_skip_and_an_unknown(tmp_path):
    root = repo(tmp_path, {"README.md": """\
        ```sh
        git push origin master
        curl https://example.com
        ```
        """})
    result = run_check(root)
    found = by_line(result)
    assert found[2].ok and (found[2].judged.kind, found[2].judged.reason) == (SKIP, "changes git state")
    assert not found[3].ok and found[3].judged.kind == UNKNOWN
    assert found[3].detail == ("no rule matches 'curl https://example.com': add a Rule for it to RULES in "
                               "tools/check_docs.py, run or skip with its reason")
    assert result.problems == 1


def test_and_takes_the_strictest_part_and_cd_moves_the_block(tmp_path):
    root = repo(tmp_path, {"README.md": """\
        ```sh
        cd emet-sdk && git status
        cd nowhere
        python -c "raise SystemExit(0)"
        cd emet-sdk && curl https://example.com
        ```
        """})
    found = by_line(run_check(root))
    assert found[2].judged.kind == SKIP
    assert not found[3].ok and found[3].detail == "cd nowhere: no such folder from emet-sdk"
    assert found[5].judged.kind == UNKNOWN and not found[5].ok


def test_a_part_with_no_rule_fails_the_run_whatever_classified_it(tmp_path):
    """run_block holds an unmatched part on its own, so classify and it cannot drift apart."""
    command = cd.Command("README.md", 1, "cd emet-sdk && curl x", 0)
    judged = cd.Judged(command, RUN, "moves the block's working folder", [("cd emet-sdk", FAKE_RULES[0]),
                                                                          ("curl x", None)])
    (repo(tmp_path, {}) / "emet-sdk").mkdir(exist_ok=True)
    [outcome] = cd.run_block(tmp_path, ".", [judged], lambda argv, cwd: (0, ""))
    assert not outcome.ok and outcome.detail == "no rule matches 'curl x'"


def test_a_transcript_runs_its_dollar_lines_only(tmp_path):
    root = repo(tmp_path, {"README.md": """\
        ```console
        $ python -c "raise SystemExit(0)"
        output that is never run
        ```
        """})
    result = run_check(root)
    assert [o.judged.command.text for o in result.outcomes] == ['python -c "raise SystemExit(0)"']
    assert result.problems == 0


def test_a_quoted_semicolon_is_an_argument_and_an_unquoted_one_needs_a_shell(tmp_path):
    root = repo(tmp_path, {"README.md": """\
        ```sh
        python -c "import sys; print(sys.argv)"
        ```
        """})
    assert run_check(root).problems == 0
    rules = FAKE_RULES + (Rule(r"python -c 1; echo", RUN, "a shell's semicolon"),)
    root = repo(tmp_path, {"README.md": "```sh\npython -c 1; echo\n```\n"})
    result = cd.check(root, documents=(("README.md", "."),), rules=rules)
    assert result.outcomes[0].detail == "'python -c 1; echo' needs a shell, and a run command is a plain one"


def test_a_skipped_editable_install_still_needs_its_folder(tmp_path):
    root = repo(tmp_path, {"README.md": """\
        ```sh
        pip install -e "emet-sdk[dev]"
        pip install -e "emet-gone[dev]"
        ```
        """})
    found = by_line(run_check(root))
    assert found[2].ok
    assert found[3].detail == "would fail: pip install -e emet-gone: no such folder from the repository's root"


def test_list_prints_each_command_with_its_document_line_and_class(tmp_path, capsys):
    root = repo(tmp_path, {"README.md": "```sh\ngit log -1\n```\n"})
    cd.print_list(cd.collect(root, (("README.md", "."),), FAKE_RULES))
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "README.md:2  skip     git log -1"
    assert out[-1] == "check-docs: 1 command(s), 0 run, 1 skipped, 0 unknown"


# ------------------------------------------------------- the environment


def test_no_key_reaches_a_run_command(monkeypatch):
    for name in ("EMET_DEEPGRAM_KEY", "EMET_UPDATE_SNAPSHOTS", "OPENAI_API_KEY", "GH_TOKEN", "AWS_SECRET"):
        monkeypatch.setenv(name, "k")
    monkeypatch.setenv("EMET_KEEP_NOT", "k")
    monkeypatch.setenv("KEYBOARD_LAYOUT", "uk")
    env = cd.clean_env()
    assert not {"EMET_DEEPGRAM_KEY", "EMET_UPDATE_SNAPSHOTS", "EMET_KEEP_NOT", "OPENAI_API_KEY", "GH_TOKEN",
                "AWS_SECRET"} & set(env)
    assert env["KEYBOARD_LAYOUT"] == "uk"
    code, output = cd.real_runner([sys.executable, "-c", "import os; print(os.environ.get('EMET_DEEPGRAM_KEY'))"],
                                  Path.cwd())
    assert (code, output.strip()) == (0, "None")


# ---------------------------------------------------------------- main


def test_main_exits_1_on_a_problem_and_0_without(tmp_path, capsys):
    root = repo(tmp_path, {"README.md": "[gone](docs/GONE.md)\n"})
    assert cd.main([str(root)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "check-docs: 1 problem(s)"
    (root / "README.md").write_text("no links\n", encoding="utf-8")
    assert cd.main([str(root)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "check-docs: ok"


def test_main_refuses_a_folder_that_is_not_the_repository(tmp_path, capsys):
    assert cd.main([str(tmp_path)]) == 2


# ---------------------------------------------------------------- links


def test_a_good_link_a_dead_file_and_a_dead_heading(tmp_path):
    root = repo(tmp_path, {
        "README.md": """\
            # Emet

            See [the guide](docs/GUIDE.md#getting-started), [the top](#emet),
            [gone](docs/GONE.md), [no heading](docs/GUIDE.md#nowhere) and
            [a site](https://example.com), [not here](#nowhere) and
            `[in code](gone-too.md)`.

            [ref]: docs/GONE-AS-WELL.md

            ```sh
            [not a link](inside-a-block.md)
            ```
            """,
        "docs/GUIDE.md": "# Guide\n\n## Getting `started`\n",
    })
    result = run_check(root)
    dead = {link.target: why for link, why in result.dead}
    assert dead == {
        "docs/GONE.md": "docs/GONE.md: no such file or folder",
        "docs/GUIDE.md#nowhere": "#nowhere: no such heading in docs/GUIDE.md",
        "#nowhere": "#nowhere: no such heading in README.md",
        "docs/GONE-AS-WELL.md": "docs/GONE-AS-WELL.md: no such file or folder",
    }
    assert result.links == 7


def test_a_link_outside_the_repository_is_dead_even_when_the_file_exists(tmp_path):
    root = repo(tmp_path / "emet", {"README.md": "[the plan](../planning/ROADMAP.md)\n"})
    (tmp_path / "planning").mkdir()
    (tmp_path / "planning" / "ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    [(link, why)] = run_check(root).dead
    assert why == "../planning/ROADMAP.md is outside the repository, so GitHub cannot serve it"


def test_an_outside_link_is_asked_only_with_external_and_fails_as_a_warning(tmp_path):
    root = repo(tmp_path, {"README.md": "[a site](https://example.com/page)\n"})
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        return "HTTP 404"

    assert run_check(root, fetch=fetch).warnings == [] and asked == []
    result = run_check(root, external=True, fetch=fetch)
    assert asked == ["https://example.com/page"]
    assert result.warnings == ["README.md:1: https://example.com/page: HTTP 404"]
    assert result.problems == 0


def test_anchors_follow_githubs_rule():
    found = cd.anchors("# Hello, World!\n## Hello, World!\n## `tools/dev.py` setup\n## a_b\n")
    assert found == {"hello-world", "hello-world-1", "toolsdevpy-setup", "a_b"}


def test_a_repeat_steps_past_a_number_an_earlier_heading_took():
    """github-slugger gives the headings A, A 1, A the slugs a, a-1, a-2."""
    assert cd.anchors("# A\n## A 1\n## A\n") == {"a", "a-1", "a-2"}


def test_a_slug_drops_other_numbers_and_keeps_connector_punctuation():
    assert cd.slug("x² y‿z – ⑴") == "x-y‿z--"


# ------------------------------------------------------ the real table

#: Commands that open audio hardware or load PortAudio, reach a vendor or
#: the Pi, spend money, install something, change git state or a tracked
#: file, run the CI jobs, run this check itself or run arbitrary code. No
#: rule of class run may match one.
NEVER_RUN = (
    "emet-listen examples/pi-speakerphone.yaml examples/emet-soul.yaml",
    "emet-listen m.yaml s.yaml --replay r.wav",
    "emet-talk examples/pi-speakerphone.yaml examples/emet-soul.yaml",
    "git commit -s -m x", "git push origin master", "git tag -s v1",
    "python tools/release_audit.py --pr 16 --merge", "python tools/tag_release.py 0.5.6 --push",
    "python tools/body/run.py --release 0.5.6 --tree", "ssh emet-pi",
    "python tools/bank/paid_replay.py misses.json --ledger l.json --keys keys.env",
    "python tools/bank/sit.py --test-tone", "python3 tools/bank/record.py PLAN",
    "pip install -e emet-sdk[dev]", "python -m pip install httpx", "python -m venv .venv",
    "python -m piper.download_voices en_US-ljspeech-medium",
    "python tools/bank/generate.py --fetch-voices ../genbank-voices",
    "python tools/dev.py setup", "python tools/dev.py update --extras", "python tools/dev.py check",
    "emet-sdk/.venv/bin/python tools/dev.py check",
    "python tools/ci.py --all", "python tools/ci.py test --install",
    "python tools/check_docs.py . --external",
    "../genbank-tools/bin/python -m pip install numpy==2.5.5",
    "python tools/check_docs.py .", "curl https://api.deepgram.com", "python -c print(1)",
    "python tools/check_health.py . --update",
    "python tools/check_licences.py --requirements tools/ci/requirements.txt --python <venv's python>",
    "zizmor --offline --config tools/ci/zizmor.yml .github",
    "<scratch>/uv/Scripts/uv.exe pip compile --universal --python-version 3.11 --generate-hashes "
    "--exclude-newer <seven days ago>T00:00:00Z --output-file requirements.txt requirements.in",
)


@pytest.mark.parametrize("command", NEVER_RUN)
def test_no_run_rule_matches_a_command_that_touches_hardware_a_vendor_or_git(command):
    rule = cd.rule_for(command, cd.RULES)
    assert rule is None or rule.kind != RUN, f"{command!r} is run by the rule {rule.pattern!r}"


def test_the_ci_tools_readme_is_read_and_its_commands_are_classified():
    """tools/ci/README.md, new in 0.5.6: each of its commands has a rule,
    once its blocks are fenced."""
    assert ("tools/ci/README.md", ".") in cd.DOCUMENTS
    for command, kind in (
            ("python -m pip install --require-hashes -r tools/ci/requirements.txt", SKIP),
            ("python -m venv tools/.venv", SKIP),
            ("tools/.venv/Scripts/python.exe -m pip install --require-hashes -r tools/ci/requirements.txt", SKIP),
            ("<scratch>/uv/Scripts/python.exe -m pip install uv", SKIP),
            ("cd tools/ci", RUN),
            ("python tools/ci/check_workflows.py .", RUN),
            (NEVER_RUN[-3], SKIP), (NEVER_RUN[-2], SKIP), (NEVER_RUN[-1], SKIP)):
        rule = cd.rule_for(command, cd.RULES)
        assert rule is not None and rule.kind == kind, command


def test_dev_check_with_the_venvs_own_interpreter_is_classified():
    """The line CONTRIBUTING gives for checking emet-sdk/.venv (RB238)."""
    for command in ("emet-sdk/.venv/bin/python tools/dev.py check", "python tools/dev.py check"):
        rule = cd.rule_for(command, cd.RULES)
        assert rule is not None and rule.kind == SKIP, command


def test_every_run_rule_is_one_of_the_safe_kinds():
    safe_starts = ("cd ", "emet validate", "emet (?:validate", "emet --version", "emet(?:-listen|-talk)? (?:--help",
                   cd.PY + r" tools/(?:check_", r"(?:" + cd.PY + r" -m pytest",
                   cd.PY + r" tools/bank/generate\.py --plan", cd.PY + r" tools/ci\.py --list")
    for rule in cd.RULES:
        if rule.kind == RUN:
            assert rule.pattern.startswith(safe_starts), rule.pattern
            if "pytest" in rule.pattern:
                assert rule.collect, "pytest runs only with --collect-only"


def test_the_real_documents_run_only_what_the_table_calls_safe():
    """Each part of each real command classed run is a `cd` inside the
    tree, the SDK's command line reading files, a console script's help, a
    check of the tree, or pytest, which the check only collects."""
    pythons = {"python", "python3", '"$PY"', "$PY", ".venv/bin/python"}
    checks = ("tools/release_check.py", "tools/ci/check_workflows.py")
    seen = 0
    for _start, judged in cd.collect(REPO):
        for j in judged:
            if j.kind != RUN:
                continue
            for text, rule in j.parts:
                seen += 1
                words = text.split()
                assert rule is not None and rule.kind == RUN, text
                if words[0] == "cd":
                    assert not words[1].startswith(("~", "/", "$")), text
                elif words[0] == "emet":
                    assert words[1] in ("validate", "explain", "--version", "--help", "-h"), text
                elif words[0] in ("emet-listen", "emet-talk"):
                    assert words[1:] in (["--help"], ["-h"]), text
                elif words[0] in pythons and words[1:3] != ["-m", "pytest"]:
                    assert (words[1].startswith("tools/check_") or words[1] in checks
                            or words[1:3] in (["tools/bank/generate.py", "--plan"], ["tools/ci.py", "--list"])), text
                    assert "--update" not in words and "<" not in text, text
                else:
                    assert "pytest" in text and rule.collect, text
    assert seen > 0
