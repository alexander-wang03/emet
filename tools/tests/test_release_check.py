"""`tools/release_check.py`, against small repositories written here.

Each test starts from the least tree the script passes: four `pyproject.toml`
at one version with the nine entry-point groups between them, the READMEs
that name those entry points and the root README, a changelog with the
release's entry and the one before it, and an `__init__.py` in the SDK and
in the engine. Most tests change one thing and see the script refuse it in
its own words, at the line the change was made on.

The script keeps its problems and notes in two module lists, so every test
starts and ends with both empty.

No test reads the real repository: other work changes it every day, and the
`layering` job runs the script on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import release_check  # noqa: E402

# `release_audit` and `tag_release` are imported by the two tests at the
# end that use them, so a break in either leaves the rest of this file
# running.

VERSION = "0.5.6"
FLOOR = f'"emet-sdk>={VERSION}"'

SDK = f"""\
[project]
name = "emet-sdk"
version = "{VERSION}"
dependencies = ["pyyaml>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=9.2.0"]
"""

HAL = f"""\
[project]
name = "emet-hal"
version = "{VERSION}"

# The SDK only. A comment may quote an old floor, "emet-sdk>=0.1", freely.
dependencies = [{FLOOR}]

[project.optional-dependencies]
audio = ["sounddevice>=0.4"]
dev = ["pytest>=9.2.0"]

[project.entry-points."emet.actuators"]
"mock" = "emet_hal.mock:MockActuator"

[project.entry-points."emet.sensors"]
"mock_sensor" = "emet_hal.mock:MockSensor"

[project.entry-points."emet.locomotion"]
"differential" = "emet_hal.differential:DifferentialDrive"

[project.entry-points."emet.wake"]
"pocketsphinx" = "emet_hal.wake:PocketSphinxWake"

[project.entry-points."emet.audio"]
"microphone" = "emet_hal.audio:MicrophoneSource"

[project.entry-points."emet.audio_out"]
"speaker" = "emet_hal.audio:Speaker"
"""

PROVIDERS = f"""\
[project]
name = "emet-providers"
version = "{VERSION}"
dependencies = [{FLOOR}]

[project.optional-dependencies]
deepgram = ["websockets>=13", "httpx>=0.27"]
dev = ["pytest>=9.2.0"]

[project.entry-points."emet.stt"]
"deepgram" = "emet_providers.deepgram:DeepgramTranscriber"

[project.entry-points."emet.llm"]
"openai" = "emet_providers.openai:OpenAILanguageModel"

[project.entry-points."emet.tts"]
"piper" = "emet_providers.piper:PiperVoice"
"""

ENGINE = f"""\
[project]
name = "emet-engine"
version = "{VERSION}"
dependencies = [
    {FLOOR},
]

[project.optional-dependencies]
dev = ["pytest>=9.2.0"]
"""

HAL_README = (
    "# emet-hal\n\nWhat ships: `mock`, `mock_sensor`, `differential`, `pocketsphinx`, "
    "`microphone` and `speaker`.\n"
)
PROVIDERS_README = "# emet-providers\n\nWhat ships: `deepgram`, `openai` and `piper`.\n"
ROOT_README = "# Emet\n\nStatus: 0.5, it knows what body it is in.\n"

CHANGELOG = f"""\
# Changelog

## [{VERSION}] - 2026-11-26

Run every release by one playbook.

### Added
- A thing a person would notice

### Fixed
- Another thing

Verified on the laptop, 2026-11-26.

## [0.5.5] - 2026-11-26

Replay a voice bank and run the body's checks over SSH.

### Added
- An earlier thing
"""

GHSA = "GHSA-6q48-6c7w-9gw2"
CVE = "CVE-2026-12345"
PYSEC = "PYSEC-2026-4320"


@pytest.fixture(autouse=True)
def empty_lists():
    release_check.problems.clear()
    release_check.notes.clear()
    yield
    release_check.problems.clear()
    release_check.notes.clear()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """The least repository the script passes."""
    root = tmp_path / "emet"
    files = {
        "emet-sdk/pyproject.toml": SDK,
        "emet-hal/pyproject.toml": HAL,
        "emet-providers/pyproject.toml": PROVIDERS,
        "emet-engine/pyproject.toml": ENGINE,
        "emet-hal/README.md": HAL_README,
        "emet-providers/README.md": PROVIDERS_README,
        "README.md": ROOT_README,
        "CHANGELOG.md": CHANGELOG,
        "emet-sdk/emet_sdk/__init__.py": '"""The SDK."""\n',
        "emet-engine/emet_engine/__init__.py": '"""The engine."""\n',
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def run(root: Path) -> tuple[int, list[str], list[str]]:
    """The script's exit code, its problems and its notes."""
    code = release_check.main(["release_check.py", str(root)])
    return code, list(release_check.problems), list(release_check.notes)


def swap(path: Path, old: str, new: str) -> None:
    """Change one thing in one file, which must hold it exactly once."""
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{path.name} holds {old!r} {text.count(old)} times"
    path.write_text(text.replace(old, new), encoding="utf-8")


def line_of(path: Path, needle: str) -> int:
    """The one line of the file that holds this."""
    hits = [n for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if needle in line]
    assert len(hits) == 1, f"{path.name} holds {needle!r} on lines {hits}"
    return hits[0]


def only(problems: list[str]) -> str:
    assert len(problems) == 1, problems
    return problems[0]


# ---------------------------------------------------------------- the tree


def test_the_least_tree_passes(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, problems, notes = run(tree)
    assert (code, problems) == (0, [])
    assert notes == [
        f"version {VERSION} across 4 packages",
        f"3 first-party floor(s), each >={VERSION}",
        "9 entry points across 9 groups",
        f"changelog {VERSION}: Run every release by one playbook.",
    ]
    out = capsys.readouterr().out.splitlines()
    assert out[: len(notes)] == [f"release-check: {note}" for note in notes]
    assert out[len(notes)] == "release-check: ok"


def test_a_failing_tree_prints_each_problem_and_the_count(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    swap(tree / "emet-hal" / "pyproject.toml", FLOOR, '"emet-sdk>=0.5.1"')
    swap(tree / "emet-engine" / "pyproject.toml", FLOOR, '"emet-sdk>=0.5.1"')
    code, problems, _ = run(tree)
    out = capsys.readouterr().out
    assert code == 1 and len(problems) == 2
    assert [line for line in out.splitlines() if line.startswith("  x ")] == [f"  x {p}" for p in problems]
    assert out.endswith("\n2 problem(s).\n")
    assert "release-check: ok" not in out


def test_a_folder_that_is_no_repository_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = release_check.main(["release_check.py", str(tmp_path)])
    assert code == 1
    assert "does not look like the Emet repository" in capsys.readouterr().err


# ------------------------------------------- what the script refused before


def test_a_package_with_no_pyproject_is_refused(tree: Path) -> None:
    (tree / "emet-engine" / "pyproject.toml").unlink()
    code, problems, _ = run(tree)
    assert code == 1
    assert "emet-engine: no pyproject.toml" in problems


def test_a_package_that_declares_no_version_is_refused(tree: Path) -> None:
    swap(tree / "emet-engine" / "pyproject.toml", f'version = "{VERSION}"\n', "")
    code, problems, _ = run(tree)
    assert code == 1
    assert "emet-engine: pyproject.toml declares no version" in problems


def test_packages_at_two_versions_are_refused(tree: Path) -> None:
    swap(tree / "emet-hal" / "pyproject.toml", f'version = "{VERSION}"', 'version = "0.5.5"')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == (
        "packages disagree about the version: emet-engine=0.5.6, emet-hal=0.5.5, "
        "emet-providers=0.5.6, emet-sdk=0.5.6"
    )


def test_a_version_written_in_the_source_is_refused(tree: Path) -> None:
    (tree / "emet-sdk" / "emet_sdk" / "__init__.py").write_text('__version__ = "0.5.6"\n', encoding="utf-8")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith("emet-sdk/emet_sdk/__init__.py hardcodes __version__.")


def test_a_group_with_nothing_shipped_is_refused(tree: Path) -> None:
    swap(
        tree / "emet-providers" / "pyproject.toml",
        '[project.entry-points."emet.tts"]\n"piper" = "emet_providers.piper:PiperVoice"\n',
        "",
    )
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith("entry-point group 'emet.tts' has no shipped implementation.")


def test_a_readme_that_states_an_older_version_is_refused(tree: Path) -> None:
    swap(tree / "README.md", "Status: 0.5,", "Status: 0.4,")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith("README.md:3 still says 0.4, but this is 0.5:")


def test_an_entry_point_its_readme_does_not_name_is_refused(tree: Path) -> None:
    swap(tree / "emet-providers" / "README.md", "`openai`", "the OpenAI one")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith("emet-providers/README.md does not name the emet.llm entry point `openai`.")


def test_a_missing_changelog_is_refused(tree: Path) -> None:
    (tree / "CHANGELOG.md").unlink()
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == "CHANGELOG.md is missing"


def test_a_version_with_no_entry_is_refused(tree: Path) -> None:
    swap(tree / "CHANGELOG.md", f"## [{VERSION}] - 2026-11-26", "## [0.5.6-rc1] - 2026-11-26")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md has no entry `## [{VERSION}] - YYYY-MM-DD`.")


def test_an_entry_with_no_summary_is_refused(tree: Path) -> None:
    swap(tree / "CHANGELOG.md", "Run every release by one playbook.\n\n", "")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md: the {VERSION} entry needs a one-line summary")


def test_a_marker_left_in_shipped_source_is_refused(tree: Path) -> None:
    marker = "TO" + "DO"
    (tree / "emet-engine" / "emet_engine" / "loop.py").write_text(f"x = 1\n# {marker}: finish this\n", encoding="utf-8")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).replace("\\", "/") == "marker left in shipped source: emet-engine/emet_engine/loop.py:2"


# -------------------------------------------------------------- the floors


@pytest.mark.parametrize(
    "written",
    [
        "emet-sdk>=0.5.1",                          # 0.5.5's floor: below the release
        "emet-sdk>=0.1",                            # the floor FUSE's package met
        "emet-sdk>=0.5.7",                          # above the release
        "emet-sdk==0.5.6",
        "emet-sdk~=0.5.6",
        "emet-sdk>0.5.5",
        "emet-sdk>=0.5.6,<1",                       # a second clause
        "emet-sdk>=0.5.6; python_version>='3.11'",  # a marker
        "emet-sdk[dev]>=0.5.6",
        "emet_sdk>=0.5.6",                          # the same package, spelled another way
        "Emet.SDK>=0.5.6",
        "emet-sdk",                                 # no floor at all
    ],
)
def test_any_floor_but_the_release_is_refused_with_the_line_to_write(tree: Path, written: str) -> None:
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, FLOOR, f'"{written}"')
    code, problems, notes = run(tree)
    assert code == 1
    n = line_of(path, f'"{written}"')
    assert only(problems).startswith(
        f'emet-providers/pyproject.toml:{n} requires "{written}" in its dependencies. Write "emet-sdk>=0.5.6": '
    )
    assert f"2 first-party floor(s), each >={VERSION}" in notes


@pytest.mark.parametrize("written", ["emet-sdk >= 0.5.6", " emet-sdk>=0.5.6 ", "emet-sdk>= 0.5.6"])
def test_spaces_in_the_floor_change_nothing(tree: Path, written: str) -> None:
    swap(tree / "emet-providers" / "pyproject.toml", FLOOR, f'"{written}"')
    code, problems, _ = run(tree)
    assert (code, problems) == (0, [])


def test_a_floor_in_single_quotes_is_found_on_its_line(tree: Path) -> None:
    path = tree / "emet-engine" / "pyproject.toml"
    swap(path, FLOOR, "'emet-sdk>=0.5.1'")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f"emet-engine/pyproject.toml:{line_of(path, 'emet-sdk>=0.5.1')} requires \"emet-sdk>=0.5.1\""
    )


def test_a_floor_quoted_in_a_comment_is_not_the_line_named(tree: Path) -> None:
    """emet-hal's comment quotes "emet-sdk>=0.1" two lines above the real one."""
    path = tree / "emet-hal" / "pyproject.toml"
    swap(path, f"dependencies = [{FLOOR}]", 'dependencies = ["emet-sdk>=0.1"]')
    code, problems, _ = run(tree)
    assert code == 1
    n = line_of(path, "dependencies = [")
    assert line_of(path, "A comment may quote") == n - 1
    assert only(problems).startswith(f'emet-hal/pyproject.toml:{n} requires "emet-sdk>=0.1"')


def test_a_floor_in_an_extra_is_held_too_and_named_by_its_extra(tree: Path) -> None:
    path = tree / "emet-engine" / "pyproject.toml"
    swap(path, 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0"]\nrobot = ["emet-sdk>=0.5.1"]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f'emet-engine/pyproject.toml:{line_of(path, "robot = ")} requires "emet-sdk>=0.5.1" in the `robot` extra. '
        f'Write "emet-sdk>=0.5.6": '
    )


def test_the_same_wrong_floor_twice_names_both_lines(tree: Path) -> None:
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, f"dependencies = [{FLOOR}]", 'dependencies = ["emet-sdk>=0.5.1"]')
    swap(path, 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0"]\nsdk = ["emet-sdk>=0.5.1"]')
    code, problems, _ = run(tree)
    assert code == 1
    base, extra = line_of(path, "dependencies = ["), line_of(path, "sdk = [")
    assert [p.split(" requires ")[0] for p in problems] == [
        f"emet-providers/pyproject.toml:{base}",
        f"emet-providers/pyproject.toml:{extra}",
    ]


@pytest.mark.parametrize("package", ["emet-hal", "emet-providers", "emet-engine"])
def test_a_package_that_names_no_sdk_is_refused(tree: Path, package: str) -> None:
    path = tree / package / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    assert text.count(FLOOR) == 1
    path.write_text(text.replace(FLOOR, '"pyyaml>=6.0"'), encoding="utf-8")
    code, problems, _ = run(tree)
    assert code == 1
    n = min(i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if line.startswith("dependencies"))
    assert only(problems) == (
        f'{package}/pyproject.toml:{n} names no emet-sdk in its dependencies. Write "emet-sdk>=0.5.6" '
        f"there: the package imports emet_sdk, and its floor is the release's own version."
    )


def test_a_floor_only_in_an_extra_leaves_the_base_without_one(tree: Path) -> None:
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, f"dependencies = [{FLOOR}]", "dependencies = []")
    swap(path, 'dev = ["pytest>=9.2.0"]', f'dev = ["pytest>=9.2.0", {FLOOR}]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f'emet-providers/pyproject.toml:{line_of(path, "dependencies = []")} names no emet-sdk in its dependencies.'
    )


def test_a_package_with_no_dependencies_key_is_sent_to_its_project_table(tree: Path) -> None:
    """A comment first, so the `[project]` line is not the line-1 default."""
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, f"dependencies = [{FLOOR}]\n", "")
    path.write_text("# emet-providers\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert line_of(path, "[project]") == 2
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == (
        'emet-providers/pyproject.toml:2 has no dependencies. Write dependencies = ["emet-sdk>=0.5.6"] '
        "under [project]: the package imports emet_sdk, and its floor is the release's own version."
    )


def test_the_floor_refusal_gives_a_reason_true_of_every_name(tree: Path) -> None:
    """The reason is the event behind the rule, which happened to emet-sdk alone."""
    path = tree / "emet-engine" / "pyproject.toml"
    swap(path, 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0"]\nrobot = ["emet-hal>=0.5.1"]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == (
        f'emet-engine/pyproject.toml:{line_of(path, "robot = ")} requires "emet-hal>=0.5.1" in the `robot` extra. '
        'Write "emet-hal>=0.5.6": every first-party requirement is the name, ">=" and the release\'s own '
        'version, since an unrelated project\'s emet-sdk 0.1.3 on PyPI met "emet-sdk>=0.1".'
    )


def test_an_extra_on_a_sibling_at_the_release_floor_passes(tree: Path) -> None:
    swap(tree / "emet-engine" / "pyproject.toml", 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0"]\nrobot = ["emet-hal[audio]>=0.5.6"]')
    code, problems, notes = run(tree)
    assert (code, problems) == (0, [])
    assert f"4 first-party floor(s), each >={VERSION}" in notes


def test_an_extra_on_a_sibling_stays_in_the_line_to_write(tree: Path) -> None:
    path = tree / "emet-engine" / "pyproject.toml"
    swap(path, 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0"]\nrobot = ["emet_hal[ audio ]>=0.5.1"]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f'emet-engine/pyproject.toml:{line_of(path, "robot = ")} requires "emet_hal[ audio ]>=0.5.1" in the `robot` extra. '
        'Write "emet-hal[audio]>=0.5.6": '
    )


def test_an_extra_on_the_sdk_is_refused_in_its_own_words(tree: Path) -> None:
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, FLOOR, '"emet-sdk[dev]>=0.5.6"')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == (
        f'emet-providers/pyproject.toml:{line_of(path, "emet-sdk[dev]")} requires "emet-sdk[dev]>=0.5.6" in its '
        'dependencies. Write "emet-sdk>=0.5.6": the SDK is required with no extra; its one extra, dev, holds '
        "its own test tools."
    )


def test_a_requirement_written_with_toml_escapes_is_found_by_its_name(tree: Path) -> None:
    """No line holds the decoded string letter for letter, so the name finds it."""
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, f"dependencies = [{FLOOR}]", 'dependencies = ["emet-sdk>=0.5.1; python_version>=\\"3.11\\""]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f"emet-providers/pyproject.toml:{line_of(path, 'dependencies = [')} requires "
        "\"emet-sdk>=0.5.1; python_version>=\"3.11\"\" in its dependencies. Write \"emet-sdk>=0.5.6\": "
    )


def test_a_requirement_no_search_finds_is_named_at_line_1(tree: Path) -> None:
    """An escaped name defeats both searches. A problem still names a line."""
    path = tree / "emet-providers" / "pyproject.toml"
    swap(path, f"dependencies = [{FLOOR}]", 'dependencies = ["emet\\u002dsdk>=0.5.1"]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        'emet-providers/pyproject.toml:1 requires "emet-sdk>=0.5.1" in its dependencies. Write "emet-sdk>=0.5.6": '
    )


def test_a_hash_inside_a_string_with_an_escaped_quote_is_no_comment() -> None:
    assert release_check.toml_code('x = "a\\"#b" # a note') == 'x = "a\\"#b" '
    assert release_check.toml_code("x = 'a#b' # a note") == "x = 'a#b' "


def test_a_floor_on_another_emet_package_is_the_release_too(tree: Path) -> None:
    path = tree / "emet-engine" / "pyproject.toml"
    swap(path, 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0"]\nrobot = ["emet-hal>=0.5.1", "emet-providers>=0.5.6"]')
    code, problems, notes = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f'emet-engine/pyproject.toml:{line_of(path, "robot = ")} requires "emet-hal>=0.5.1" in the `robot` extra. '
        f'Write "emet-hal>=0.5.6": '
    )
    assert f"4 first-party floor(s), each >={VERSION}" in notes


def test_the_sdk_is_held_to_the_same_floor_on_a_sibling(tree: Path) -> None:
    path = tree / "emet-sdk" / "pyproject.toml"
    swap(path, 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0", "emet-hal"]')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f'emet-sdk/pyproject.toml:{line_of(path, "dev = ")} requires "emet-hal" in the `dev` extra. Write "emet-hal>=0.5.6": '
    )


def test_a_stranger_whose_name_starts_like_ours_is_not_first_party(tree: Path) -> None:
    swap(tree / "emet-engine" / "pyproject.toml", 'dev = ["pytest>=9.2.0"]', 'dev = ["pytest>=9.2.0", "emet-sdk-extras>=0.1"]')
    code, problems, _ = run(tree)
    assert (code, problems) == (0, [])


def test_floors_are_not_judged_while_the_versions_disagree(tree: Path) -> None:
    """There is no release version to hold them to: one problem, the versions'."""
    swap(tree / "emet-hal" / "pyproject.toml", f'version = "{VERSION}"', 'version = "0.5.5"')
    swap(tree / "emet-engine" / "pyproject.toml", FLOOR, '"emet-sdk>=0.1"')
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith("packages disagree about the version")


def test_the_floors_follow_the_release(tree: Path) -> None:
    """At 0.5.7 the three floors of 0.5.6 are each one release behind."""
    for package in release_check.PACKAGES:
        swap(tree / package / "pyproject.toml", f'version = "{VERSION}"', 'version = "0.5.7"')
    swap(tree / "CHANGELOG.md", f"## [{VERSION}] - 2026-11-26", "## [0.5.7] - 2026-11-27")
    code, problems, _ = run(tree)
    assert code == 1
    assert sorted(p.split(":")[0] for p in problems) == [
        "emet-engine/pyproject.toml",
        "emet-hal/pyproject.toml",
        "emet-providers/pyproject.toml",
    ]
    assert all('Write "emet-sdk>=0.5.7"' in p for p in problems)


# ---------------------------------------------------- the Security section


def with_security(tree: Path, section: str, *, after: str = "### Fixed\n- Another thing\n") -> Path:
    """Put a section under the 0.5.6 entry's Fixed list."""
    path = tree / "CHANGELOG.md"
    swap(path, after, after + "\n" + section)
    return path


@pytest.mark.parametrize("advisory", [GHSA, CVE, PYSEC, "CVE-1999-0001", "CVE-2026-1234567", "PYSEC-2021-9"])
def test_a_security_line_with_its_advisory_id_passes(tree: Path, advisory: str) -> None:
    with_security(tree, f"### Security\n- A crafted manifest could run code at boot ({advisory})\n")
    code, problems, notes = run(tree)
    assert (code, problems) == (0, [])
    assert f"changelog {VERSION}: 1 security line(s), naming {advisory}" in notes


def test_a_tree_with_no_security_section_says_nothing_of_one(tree: Path) -> None:
    _, _, notes = run(tree)
    assert not any("security" in note for note in notes)


def test_a_security_line_without_an_id_is_refused(tree: Path) -> None:
    path = with_security(tree, "### Security\n- A crafted manifest could run code at boot\n")
    code, problems, _ = run(tree)
    assert code == 1
    n = line_of(path, "A crafted manifest")
    assert only(problems) == (
        f"CHANGELOG.md:{n} is a Security line of the {VERSION} entry that names no advisory id. "
        "Write the id the advisory was given in the line: GHSA- and three groups of four characters, "
        "CVE-YYYY-NNNN, or PYSEC-YYYY-NNNN."
    )


def test_a_security_heading_with_no_line_is_refused(tree: Path) -> None:
    path = with_security(tree, "### Security\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == (
        f'CHANGELOG.md:{line_of(path, "### Security")} is a "### Security" heading with no list item under it, in '
        f'the {VERSION} entry. Write each fix as a "- " item with its advisory id, or take the heading out.'
    )


def test_prose_under_the_heading_is_no_list_item(tree: Path) -> None:
    """A sentence with an id and no list mark: the section still has no item."""
    path = with_security(tree, f"### Security\nFixed {GHSA} in the manifest reader.\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f'CHANGELOG.md:{line_of(path, "### Security")} is a "### Security" heading with no list item'
    )


@pytest.mark.parametrize("mark", ["-", "*", "+"])
def test_each_security_line_needs_its_own_id(tree: Path, mark: str) -> None:
    """Each mark starts its own item, so none can borrow the id above it."""
    path = with_security(
        tree,
        f"### Security\n- The first fix ({GHSA})\n{mark} The second fix, with no id\n- The third fix ({CVE})\n",
    )
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'The second fix')} is a Security line")


def test_a_loose_list_holds_each_item_to_an_id(tree: Path) -> None:
    path = with_security(tree, f"### Security\n- The first fix ({GHSA})\n\n- The second fix, with no id\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'The second fix')} is a Security line")


def test_the_section_ends_at_the_next_heading(tree: Path) -> None:
    """The Fixed list after it is no Security line."""
    path = tree / "CHANGELOG.md"
    swap(path, "### Fixed\n- Another thing\n", f"### Security\n- A fix ({GHSA})\n\n### Fixed\n- Another thing\n")
    code, problems, notes = run(tree)
    assert (code, problems) == (0, [])
    assert f"changelog {VERSION}: 1 security line(s), naming {GHSA}" in notes


@pytest.mark.parametrize(
    "second",
    [
        "1. A second fix with no id",       # a numbered list may start inside a paragraph at 1
        "1) A second fix with no id",
        "\n2. A second fix with no id",     # after a blank line, at any number
        "    - A second fix with no id",    # nested
        "  - A second fix with no id",
        "\t- A second fix with no id",
    ],
)
def test_a_numbered_or_nested_item_needs_its_own_id(tree: Path, second: str) -> None:
    path = with_security(tree, f"### Security\n- The first fix ({GHSA})\n{second}\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'A second fix')} is a Security line")


def test_a_numbered_list_holds_each_item_to_an_id(tree: Path) -> None:
    """Every item numbered: the message names the item, never an empty heading."""
    path = with_security(tree, f"### Security\n1. The first fix ({GHSA})\n2. The second fix, with no id\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'The second fix')} is a Security line")


@pytest.mark.parametrize("second", ["2. continues the line above", "2) continues the line above"])
def test_a_number_that_cannot_start_a_list_continues_its_item(tree: Path, second: str) -> None:
    """Markdown renders it as part of the bullet above, which carries the id."""
    with_security(tree, f"### Security\n- The first fix ({GHSA}), and it\n{second}\n")
    code, problems, notes = run(tree)
    assert (code, problems) == (0, [])
    assert f"changelog {VERSION}: 1 security line(s), naming {GHSA}" in notes


def test_a_paragraph_indented_under_an_item_is_part_of_it(tree: Path) -> None:
    with_security(tree, f"### Security\n- The first fix\n\n  It was reported privately ({GHSA}).\n")
    code, problems, _ = run(tree)
    assert (code, problems) == (0, [])


def test_text_before_the_list_is_refused(tree: Path) -> None:
    path = with_security(tree, f"### Security\nA crafted manifest could run code at boot.\n\n- A fix ({GHSA})\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems) == (
        f"CHANGELOG.md:{line_of(path, 'A crafted manifest')} is text in the Security section of the {VERSION} "
        'entry outside its list. Write each fix as a "- " item with its advisory id. Only the entry\'s closing '
        "paragraph may follow the list."
    )


def test_text_after_the_list_other_than_the_closing_paragraph_is_refused(tree: Path) -> None:
    """The entry's "Verified on" paragraph follows; the one before it is a fix with no item."""
    path = with_security(tree, f"### Security\n- A fix ({GHSA})\n\nAlso fixed a path traversal in the bundle reader.\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'Also fixed')} is text in the Security section")


def test_text_between_the_list_and_another_section_is_refused(tree: Path) -> None:
    """Only the paragraph the entry's next heading follows is its closing one."""
    path = tree / "CHANGELOG.md"
    swap(
        path,
        "### Fixed\n- Another thing\n",
        f"### Security\n- A fix ({GHSA})\n\nAlso fixed a path traversal.\n\n### Fixed\n- Another thing\n",
    )
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'Also fixed')} is text in the Security section")


def test_an_id_on_a_wrapped_line_counts_for_its_item(tree: Path) -> None:
    with_security(
        tree,
        "### Security\n"
        "- `certifi` is floored at 2026.10.18 in the three vendor extras, the\n"
        f"  first version past the advisory ({PYSEC},\n"
        f"  {GHSA})\n"
        f"- A second fix ({CVE})\n",
    )
    code, problems, notes = run(tree)
    assert (code, problems) == (0, [])
    assert f"changelog {VERSION}: 2 security line(s), naming {PYSEC}, {GHSA}, {CVE}" in notes


def test_the_closing_paragraph_after_the_list_needs_no_id(tree: Path) -> None:
    """Security is the entry's last section here, so the "Verified on" line follows it."""
    path = tree / "CHANGELOG.md"
    swap(path, "### Fixed\n- Another thing\n", f"### Security\n- A fix ({GHSA})\n")
    text = path.read_text(encoding="utf-8")
    assert text.index("### Security") < text.index("Verified on the laptop") < text.index("## [0.5.5]")
    code, problems, _ = run(tree)
    assert (code, problems) == (0, [])


def test_an_id_in_the_closing_paragraph_does_not_stand_in_for_a_line(tree: Path) -> None:
    path = tree / "CHANGELOG.md"
    swap(path, "### Fixed\n- Another thing\n", "### Security\n- A fix with no id\n")
    swap(path, "Verified on the laptop, 2026-11-26.", f"Verified on the laptop, 2026-11-26. See {GHSA}.")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'A fix with no id')} is a Security line")


@pytest.mark.parametrize(
    "lookalike",
    [
        "GHSA-xxxx-xxxx-xxxx",       # the form itself, which the alphabet allows
        "GHSA-1234-abcd-efgh",       # 1, a, b, d and e are outside GitHub's alphabet
        "GHSA-6q48-6c7w",            # two groups
        "GHSA-6q48-6c7w-9gw2x",      # five in the last group
        "GHSA-6Q48-6C7W-9GW2",       # upper case
        "ghsa-6q48-6c7w-9gw2",
        "CVE-2026-123",              # three digits
        "CVE-2026-" + "1" * 20,      # twenty, one past the schema's nineteen
        "CVE-26-12345",
        "CVE-YYYY-NNNN",
        "PYSEC-26-4320",
        "PYSEC-2026",
        "XGHSA-6q48-6c7w-9gw2",
        "advisory 4320",
    ],
)
def test_what_only_looks_like_an_id_is_refused(tree: Path, lookalike: str) -> None:
    path = with_security(tree, f"### Security\n- A fix ({lookalike})\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, '- A fix (')} is a Security line")


def test_an_older_entry_is_held_to_the_rule_too(tree: Path) -> None:
    path = tree / "CHANGELOG.md"
    swap(path, "- An earlier thing\n", "- An earlier thing\n\n### Security\n- An old fix with no id\n")
    code, problems, notes = run(tree)
    assert code == 1
    assert only(problems).startswith(
        f"CHANGELOG.md:{line_of(path, 'An old fix')} is a Security line of the 0.5.5 entry that names no advisory id."
    )
    assert not any("security" in note for note in notes)


@pytest.mark.parametrize(
    "heading",
    ["### security", "###Security", "### Security fixes", "### SECURITY", " ### Security", "   ### Security"],
)
def test_a_heading_spelled_another_way_is_still_the_section(tree: Path, heading: str) -> None:
    path = with_security(tree, f"{heading}\n- A fix with no id\n")
    code, problems, _ = run(tree)
    assert code == 1
    assert only(problems).startswith(f"CHANGELOG.md:{line_of(path, 'A fix with no id')} is a Security line")


def test_the_word_security_in_another_section_is_not_a_section(tree: Path) -> None:
    swap(tree / "CHANGELOG.md", "- Another thing\n", "- SECURITY.md says what follows a report\n")
    code, problems, _ = run(tree)
    assert (code, problems) == (0, [])


# -------------------------- the section reaches the tag and the audit whole


SECURITY_ENTRY = f"""\
### Fixed
- Another thing

### Security
- `certifi` is floored at 2026.10.18 in the three vendor extras
  ({PYSEC}, {GHSA})
"""


def test_the_tag_message_keeps_the_security_section(tree: Path) -> None:
    """`tag_release.py` takes the entry's lines as they stand, headings included."""
    import tag_release

    path = tree / "CHANGELOG.md"
    swap(path, "### Fixed\n- Another thing\n", SECURITY_ENTRY)
    assert run(tree)[0] == 0
    summary, rest = tag_release.changelog_section(path, VERSION)
    message = tag_release.tag_message(VERSION, summary, rest)
    assert message.startswith(f"{VERSION}: Run every release by one playbook\n\n### Added\n")
    assert SECURITY_ENTRY in message
    assert message.rstrip().endswith("Verified on the laptop, 2026-11-26.")


def test_the_audit_reads_the_same_entry_the_tag_carries(tree: Path) -> None:
    """`release_audit.py` compares a tag and a release page to this text."""
    import release_audit
    import tag_release

    path = tree / "CHANGELOG.md"
    swap(path, "### Fixed\n- Another thing\n", SECURITY_ENTRY)
    entry = release_audit.Audit(tree, path, "origin/master").entry(VERSION)
    assert entry is not None and entry.date == "2026-11-26"
    summary, rest = tag_release.changelog_section(path, VERSION)
    assert entry.message == tag_release.tag_message(VERSION, summary, rest)
    assert SECURITY_ENTRY in entry.message
    assert release_audit.CITED.findall(entry.message) == []
