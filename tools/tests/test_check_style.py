"""`tools/check_style.py`, against small trees written here.

Every address a test plants that the script refuses is put together from
its parts at run time, and so is every dash, banned word, litotes and
negative parallelism. This file is part of the tree the script reads, and
written out whole they would fail the real check, which is the check
working.

The planted address is an invented one at a domain reserved for examples.
No real person's address is written anywhere, here or in the script.

No test reads the real repository: other work changes it every day, and the
`layering` job runs the script on it.
"""

from __future__ import annotations

import codecs
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import check_style  # noqa: E402  a tool, imported by its bare name from tools/

DASH = chr(0x2014)
PROJECT = "wake.up.emet@gmail.com"


def at(local: str, domain: str) -> str:
    """An address from its two halves, so this file holds none it refuses."""
    return local + "@" + domain


#: The address the tests plant: invented, at a reserved domain outside the four.
PLANTED = at("someone", "example.net")

#: One of each form the allowlist holds.
ALLOWED = [
    PROJECT,
    PROJECT.upper(),
    at("12345678+someone", "users.noreply.github.com"),
    at("someone", "users.noreply.github.com"),
    at("49699333+dependabot[bot]", "users.noreply.github.com"),
    at("41898282+github-actions[bot]", "users.noreply.github.com"),
    at("your.email", "example.com"),
    at("Your.Email", "Example.COM"),
    at("t", "example.invalid"),
]

#: Addresses one edit away from an allowed one, and the other kinds a file
#: has no business holding. Each is refused.
REFUSED = [
    PLANTED,
    at("not.wake.up.emet", "gmail.com"),            # another mailbox that ends like the project's
    at("wake.up.emet", "gmail.com.example.net"),    # the project's address as a prefix
    at("wake.up.emet", "googlemail.com"),
    at("wake-up-emet", "gmail.com"),
    at("someone", "users.noreply.github.com.example.net"),
    at("someone", "notusers.noreply.github.com"),
    at("someone", "users-noreply.github.com"),      # one character off the noreply domain's dot
    at("someone", "noreply.github.com"),
    at("noreply", "github.com"),                    # GitHub's web-flow committer, which only the history holds
    at("support", "github.com"),                    # Dependabot's sign-off, the same
    at("someone", "example.com.example.net"),       # example.com as a prefix
    at("someone", "notexample.com"),
    at("someone", "mail.example.com"),
    at("someone", "example.org"),
    at("someone", "example.invalid.example.net"),
    at("someone", "notexample.invalid"),
    at("t", "mail.example.invalid"),                # a subdomain of the reserved name
    at("noreply", "anthropic.com"),                 # an AI attribution line's address
    at("someone", "robot.local"),                   # a login at a host is an address too
    at("someone", "example.com.xn--p1ai"),          # a punycode last label after an allowed name
    at("someone", "mail.xn--p1ai"),
    at('"someone"', "example.net"),                 # a quoted local part
    at("someone!", "example.net"),                  # another character RFC 5322 allows before the @
    at("someone=", "example.net"),
]

#: What may follow an address on a line. A hyphen ends it: `--` is how a
#: dash gets typed where an em-dash is banned.
TAILS = ["--or not", "-old", "-", " today", ".", "_x"]

#: Strings with an `@` that are no address.
NOT_ADDRESSES = [
    "uses: actions/checkout@v6.0.1",
    "uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    "@pytest.mark.parametrize",
    "@dataclasses.dataclass(frozen=True)",
    "pip install piper-tts@1.8.0",
    "image@sha256:0123abcd",
    'f"{user}@{host}"',
    "a @ b.T",
    "someone at example dot net",
    "pip install pkg@1.0.dev1",                     # a version's last label holds a digit
    "pin " + at("someone", "example.com.co2"),      # the domain goes on past its letters label
    "https://example.com/@author.name/post",        # a link's `/@`
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """The least tree the script accepts as the repository, with clean text in it."""
    root = tmp_path / "emet"
    (root / "emet-sdk").mkdir(parents=True)
    (root / "README.md").write_text(f"# Emet\n\nWrite to <{PROJECT}>.\n", encoding="utf-8")
    (root / "emet-sdk" / "module.py").write_text('"""A plain sentence."""\n', encoding="utf-8")
    return root


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def address_problem(name: str, line: int, column: int) -> str:
    return (
        f"{name}:{line}: an address outside the allowlist, at column {column}. "
        f"Use the project's address, or a placeholder at example.com."
    )


# ------------------------------------------------------------------ the tree


def test_a_clean_tree_passes(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_style.check(tree) == []
    assert check_style.main(["check_style.py", str(tree)]) == 0
    assert capsys.readouterr().out == "style-check: ok\n"


def test_a_folder_that_is_no_repository_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_style.main(["check_style.py", str(tmp_path)]) == 1
    assert "does not look like the Emet repository" in capsys.readouterr().err


def test_problems_print_one_a_line_with_their_count(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree, "notes.md", f"A dash {DASH} here.\nWrite to {PLANTED}.\n")
    assert check_style.main(["check_style.py", str(tree)]) == 1
    assert capsys.readouterr().out == (
        "  x notes.md:1: em-dash. Use a comma, a colon, or two sentences.\n"
        f"  x {address_problem('notes.md', 2, 10)}\n"
        "\n2 problem(s).\n"
    )


# ------------------------------------------- the prose rules of 0.5.7-D10 (c)

#: Each litotes of the closed list, from two halves so this file holds none.
LITOTES_PARTS = [("not with", "out"), ("no sm", "all"), ("not a coinci", "dence"), ("not un", "like"),
                 ("not un", "common"), ("not un", "usual"), ("not insig", "nificant"), ("not unim", "portant"),
                 ("not unreason", "able"), ("no mean f", "eat")]
NOT_JUST = "not " + "just"
#: The rule's message, put together for the same reason.
NOT_JUST_SAID = '"' + NOT_JUST + ' ... b' + 'ut"; say what it is'


def prose(tree: Path) -> list[str]:
    return check_style.check_prose(tree)[1]


def test_the_closed_list_of_litotes_is_the_contracts() -> None:
    assert [a + b for a, b in LITOTES_PARTS] == list(check_style.LITOTES)


@pytest.mark.parametrize("parts", LITOTES_PARTS)
def test_each_litotes_warns_with_its_place(tree: Path, parts: tuple[str, str]) -> None:
    words = "".join(parts)
    write(tree, "notes.md", f"One line.\nIt is {words.upper()} a thing.\n")
    assert prose(tree) == [f'notes.md:2:7: litotes "{words.upper()}"; say the positive thing']
    assert check_style.check(tree) == prose(tree), "a problem since the sweep set PROSE_RULES_BLOCK"


def test_a_litotes_across_a_run_of_spaces_is_one(tree: Path) -> None:
    write(tree, "notes.md", "It is not  " + "un" + "like the last.\n")
    assert prose(tree) == ['notes.md:1:7: litotes "not ' + 'un' + 'like"; say the positive thing']


@pytest.mark.parametrize("adverb", ["just", "only", "merely", "simply"])
def test_not_just_but_on_one_line_warns(tree: Path, adverb: str) -> None:
    write(tree, "notes.md", f"It is not {adverb} a check, " + "b" + "ut a habit.\n")
    assert prose(tree) == ["notes.md:1:7: " + NOT_JUST_SAID]


def test_not_just_and_a_but_far_apart_or_on_two_lines_pass(tree: Path) -> None:
    far = f"It is {NOT_JUST} {'x' * 81} " + "b" + "ut far.\n"
    near = f"It is {NOT_JUST} {'x' * 70} " + "b" + "ut near.\n"
    write(tree, "far.md", far + f"It is {NOT_JUST} one line,\n" + "b" + "ut another.\n")
    write(tree, "near.md", near)
    assert prose(tree) == ["near.md:1:7: " + NOT_JUST_SAID]


@pytest.mark.parametrize("word", [
    "del" + "ves", "del" + "ved", "del" + "ving", "lever" + "ages", "lever" + "aged", "lever" + "aging",
    "rob" + "ustly", "rob" + "ustness", "seam" + "lessly", "compre" + "hensively", "compre" + "hensiveness",
    "under" + "scores", "under" + "scored", "under" + "scoring",
])
def test_each_inflection_of_a_banned_word_warns(tree: Path, word: str) -> None:
    write(tree, "notes.md", f"It {word} the point.\n")
    assert prose(tree) == [f"notes.md:1: {word!r}. Say the plain thing."]
    assert check_style.check(tree) == prose(tree)


def test_a_one_line_docstring_shaped_x_colon_y_warns(tree: Path) -> None:
    write(tree, "emet-sdk/module.py", '"""The module: what it holds."""\n\n\ndef f():\n    """Run it: now."""\n')
    assert prose(tree) == [
        'emet-sdk/module.py:1: a one-line docstring shaped "X: Y"; write one plain sentence',
        'emet-sdk/module.py:5: a one-line docstring shaped "X: Y"; write one plain sentence',
    ]


@pytest.mark.parametrize("docstring", [
    '"""Read `a: b` from the file."""',                   # the colon sits inside backticks
    '"""Read the file.\n\nIts shape: a list.\n"""',     # more than one line
    '"""Count 3:2 and stop."""',                          # no space after the colon
])
def test_a_docstring_of_another_shape_passes(tree: Path, docstring: str) -> None:
    write(tree, "emet-sdk/module.py", f"def f():\n    {docstring}\n")
    assert prose(tree) == []


def test_a_one_line_docstring_rule_reads_only_python(tree: Path) -> None:
    write(tree, "notes.md", '"""The module: what it holds."""\n')
    assert prose(tree) == []


def test_with_the_switch_off_the_prose_rules_print_warnings_and_exit_0(
        tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    write(tree, "notes.md", "It is " + "no sm" + "all thing.\n")
    monkeypatch.setattr(check_style, "PROSE_RULES_BLOCK", False)
    assert check_style.check(tree) == []
    assert check_style.main(["check_style.py", str(tree)]) == 0
    out = capsys.readouterr().out
    assert out == ('  warning: notes.md:1:7: litotes "no sm' + 'all"; say the positive thing\n'
                   "\n1 warning(s) from the prose rules of 0.5.7-D10 (c), which block once PROSE_RULES_BLOCK is "
                   "True.\nstyle-check: ok\n")


def test_the_prose_rules_block_as_the_sweep_left_them(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A12's sweep rewrote what the four rules found in the tree and set the
    switch, so a finding is a problem like an em-dash."""
    write(tree, "notes.md", "It is " + "no sm" + "all thing.\n")
    assert check_style.PROSE_RULES_BLOCK is True
    assert check_style.check(tree) == ['notes.md:1:7: litotes "no sm' + 'all"; say the positive thing']
    assert check_style.main(["check_style.py", str(tree)]) == 1
    assert capsys.readouterr().out.endswith("\n1 problem(s).\n")


def test_a_file_outside_the_tree_is_read_with_every_rule(tree: Path, tmp_path: Path) -> None:
    body = tmp_path / "releases" / "v0.9.1" / "pr-body.md"
    write(tmp_path, "releases/v0.9.1/pr-body.md",
          f"A dash {DASH} here.\nIt is " + "no sm" + f"all thing.\nWrite to {PLANTED}.\n")
    problems, found = check_style.check_prose(tree, [body])
    shown = body.as_posix()
    assert problems == [f"{shown}:1: em-dash. Use a comma, a colon, or two sentences.",
                        address_problem(shown, 3, 10)]
    assert found == [f'{shown}:2:7: litotes "no sm' + 'all"; say the positive thing']
    assert check_style.main(["check_style.py", str(tree), "--file", str(body)]) == 1


def test_a_missing_file_is_a_problem(tree: Path, tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.md"
    assert check_style.check(tree, [missing]) == [f"{missing.as_posix()}: no such file to read (--file)"]


def test_file_is_repeatable(tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    one = write(tmp_path, "one.md", "Plain.\n")
    two = write(tmp_path, "two.md", f"A dash {DASH} here.\n")
    assert check_style.main(["check_style.py", str(tree), "--file", str(one), "--file", str(two)]) == 1
    assert f"  x {two.as_posix()}:1: em-dash." in capsys.readouterr().out


# ------------------------------------------------- what the script refused before


def test_an_em_dash_is_refused_with_its_file_and_line(tree: Path) -> None:
    write(tree, "docs/guide.md", f"One line.\nA dash {DASH} here.\n")
    assert check_style.check(tree) == ["docs/guide.md:2: em-dash. Use a comma, a colon, or two sentences."]


@pytest.mark.parametrize("parts", [("del", "ve"), ("lever", "age"), ("rob", "ust"), ("seam", "less"), ("compre", "hensive"), ("under", "score")])
def test_each_banned_word_is_refused(tree: Path, parts: tuple[str, str]) -> None:
    word = "".join(parts)
    assert word in check_style.BANNED
    write(tree, "emet-sdk/module.py", f'"""A {word.title()} thing."""\n')
    assert check_style.check(tree) == [f"emet-sdk/module.py:1: {word.title()!r}. Say the plain thing."]


def test_a_banned_word_inside_a_longer_word_is_left_to_the_inflections_rule(tree: Path) -> None:
    """The bare word's rule leaves a longer word alone, and the inflections'
    rule of 0.5.7-D10 (c) refuses these two."""
    write(tree, "notes.md", "It was " + "rob" + "ustly built, with " + "under" + "scores.\n")
    problems, found = check_style.check_prose(tree)
    assert problems == []
    assert found == [
        "notes.md:1: " + repr("rob" + "ustly") + ". Say the plain thing.",
        "notes.md:1: " + repr("under" + "scores") + ". Say the plain thing.",
    ]
    assert check_style.check(tree) == found


def test_style_is_not_read_in_the_skipped_files(tree: Path) -> None:
    for name in sorted(check_style.SKIP_FILES):
        write(tree, name, f"A dash {DASH} and a " + "rob" + "ust word.\n")
    assert check_style.check(tree) == []


def test_style_is_read_only_in_the_text_suffixes(tree: Path) -> None:
    write(tree, "picture.svg", f"A dash {DASH} here.\n")
    assert check_style.check(tree) == []


@pytest.mark.parametrize("folder", [".venv", "__pycache__", "dist", "build", ".pytest_cache", "node_modules", ".git", "emet_sdk.egg-info"])
def test_nothing_is_read_under_a_skipped_folder(tree: Path, folder: str) -> None:
    write(tree, f"emet-sdk/{folder}/notes.md", f"A dash {DASH} here. Write to {PLANTED}.\n")
    assert check_style.check(tree) == []


@pytest.mark.parametrize("folder", ["build", "dist", ".venv"])
def test_a_checkout_inside_a_folder_named_like_a_skipped_one_is_still_read(tmp_path: Path, folder: str) -> None:
    """The skip is judged below the root. Judged on the whole path, a
    checkout at `~/build/emet` passed with not one file read."""
    root = tmp_path / folder / "emet"
    (root / "emet-sdk").mkdir(parents=True)
    write(root, "notes.md", f"A dash {DASH} here.\nWrite to {PLANTED}.\n")
    assert check_style.check(root) == [
        "notes.md:1: em-dash. Use a comma, a colon, or two sentences.",
        address_problem("notes.md", 2, 10),
    ]


# ------------------------------------------------------------- the addresses


def test_the_allowlist_has_four_entries() -> None:
    assert len(check_style.ALLOWED_ADDRESSES) == 4


@pytest.mark.parametrize("address", ALLOWED)
def test_each_allowed_form_passes(tree: Path, address: str) -> None:
    line = f"Signed-off-by: A Person <{address}>"
    assert check_style.ADDRESS_RE.findall(line) == [address], "the form has to be seen to be judged"
    write(tree, "CONTRIBUTING.md", line + "\n")
    assert check_style.check(tree) == []


def test_a_planted_address_is_refused_with_its_file_and_line(tree: Path) -> None:
    write(tree, "emet-sdk/tests/fixture.yaml", f"name: a body\nowner: plain\ncontact: {PLANTED}\n")
    assert check_style.check(tree) == [address_problem("emet-sdk/tests/fixture.yaml", 3, 10)]


def test_the_problem_never_prints_the_address(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The output lands in a public log. The file and the line are enough to find it."""
    write(tree, "notes.md", f"Write to {PLANTED}.\n")
    assert check_style.main(["check_style.py", str(tree)]) == 1
    captured = capsys.readouterr()
    assert "notes.md:1:" in captured.out
    for piece in (PLANTED, "someone", "example.net"):
        assert piece not in captured.out + captured.err


@pytest.mark.parametrize("address", REFUSED)
def test_an_address_outside_the_four_is_refused(tree: Path, address: str) -> None:
    write(tree, "notes.md", f"Write to <{address}> today.\n")
    assert check_style.check(tree) == [address_problem("notes.md", 1, 11)]


@pytest.mark.parametrize("address", REFUSED)
def test_a_refused_address_is_taken_whole(address: str) -> None:
    """A longer domain is judged as itself, never as the allowed name it starts with."""
    assert check_style.ADDRESS_RE.findall(f"<{address}>.") == [address]
    assert not check_style.allowed(address)


@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize("address", REFUSED)
def test_a_refused_address_is_found_whatever_follows_it(tree: Path, address: str, tail: str) -> None:
    write(tree, "notes.md", f"Write to {address}{tail}\n")
    assert check_style.check(tree) == [address_problem("notes.md", 1, 10)]


@pytest.mark.parametrize("tail", TAILS)
@pytest.mark.parametrize("address", ALLOWED)
def test_an_allowed_address_stays_allowed_whatever_follows_it(tree: Path, address: str, tail: str) -> None:
    """GitHub's noreply form with `--` after it was once cut to `users.noreply.github` and refused."""
    assert [m.group(0) for m in check_style.ADDRESS_RE.finditer(f"Write to {address}{tail}")] == [address]
    write(tree, "notes.md", f"Write to {address}{tail}\n")
    assert check_style.check(tree) == []


def test_an_address_starts_at_its_first_letter(tree: Path) -> None:
    """The quote and the `=` of code are no part of the address they hold."""
    line = f"email='{PROJECT}', owner={PLANTED}"
    assert check_style.ADDRESS_RE.findall(line) == [PROJECT, PLANTED]
    write(tree, "notes.py", line + "\n")
    assert check_style.check(tree) == [address_problem("notes.py", 1, line.index(PLANTED) + 1)]


@pytest.mark.parametrize("text", NOT_ADDRESSES)
def test_an_at_sign_alone_is_no_address(tree: Path, text: str) -> None:
    """A shorter match that happens to be allowed is no pass, since the address was not found at all."""
    assert check_style.ADDRESS_RE.findall(text) == []
    write(tree, "notes.md", text + "\n")
    assert check_style.check(tree) == []


def test_every_address_on_a_line_is_judged(tree: Path) -> None:
    line = f"From {PROJECT}, to {PLANTED}, copy {at('t', 'example.invalid')} and {at('second', 'example.org')}."
    write(tree, "notes.md", line + "\n")
    assert check_style.check(tree) == [
        address_problem("notes.md", 1, line.index(PLANTED) + 1),
        address_problem("notes.md", 1, line.index("second") + 1),
    ]


@pytest.mark.parametrize("name", ["Dockerfile", "CODEOWNERS", "docs/page.rst", "site/index.html", "CITATION.cff", ".mailmap", "data.csv"])
def test_an_address_is_read_in_any_text_file_whatever_its_suffix(tree: Path, name: str) -> None:
    write(tree, name, f"maintainer {PLANTED}\n")
    assert check_style.check(tree) == [address_problem(name, 1, 12)]


def test_an_address_is_read_in_the_files_the_style_rules_skip(tree: Path) -> None:
    """A code of conduct is where a contact address gets pasted."""
    for name in check_style.SKIP_FILES:
        write(tree, name, f"Report it to {PLANTED}.\n")
    # Sorted here: the script lists files in the platform's own path order,
    # which folds case on Windows and does not on Linux.
    assert sorted(check_style.check(tree)) == sorted(address_problem(name, 1, 14) for name in check_style.SKIP_FILES)


def test_an_address_inside_a_binary_file_is_not_read(tree: Path) -> None:
    (tree / "clip.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + PLANTED.encode("ascii") + b"\x00\x01\x02")
    (tree / "cache.pyc").write_bytes(bytes(16) + PLANTED.encode("ascii"))
    assert check_style.check(tree) == []


def test_a_nul_anywhere_in_the_first_8000_bytes_makes_a_file_binary(tree: Path) -> None:
    """git's own test. A NUL past the first 8000 bytes leaves the file text."""
    (tree / "late.dat").write_bytes(b"a" * 5000 + bytes(1) + PLANTED.encode("ascii") + b"\n")
    (tree / "later.dat").write_bytes(b"a\n" * 4000 + PLANTED.encode("ascii") + b"\n" + bytes(1))
    assert check_style.check(tree) == [address_problem("later.dat", 4001, 1)]


def test_an_address_under_dot_venv_is_not_read(tree: Path) -> None:
    write(tree, ".venv/Lib/site-packages/somebody/METADATA", f"Author-email: {PLANTED}\n")
    write(tree, "tools/.venv/Lib/site-packages/somebody/METADATA", f"Author-email: {PLANTED}\n")
    assert check_style.check(tree) == []


@pytest.mark.parametrize(
    ("mark", "codec"),
    [(codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")],
    ids=["little-endian", "big-endian"],
)
def test_an_address_in_a_file_windows_wrote_as_utf16_is_read(tree: Path, mark: bytes, codec: str) -> None:
    (tree / "paste.txt").write_bytes(mark + f"first line\ncontact {PLANTED}\n".encode(codec))
    assert check_style.check(tree) == [address_problem("paste.txt", 2, 9)]


def test_an_address_in_a_file_that_is_not_utf8_is_read(tree: Path) -> None:
    (tree / "old.txt").write_bytes("caf\xe9, contact ".encode("latin-1") + PLANTED.encode("ascii") + b"\n")
    assert check_style.check(tree) == [address_problem("old.txt", 1, 15)]


# ------------------------------------------------- an address in a file's name


def path_problem(shown: str) -> str:
    return f"{shown}: an address outside the allowlist in the path. Rename it."


def test_an_address_in_a_file_name_is_refused(tree: Path) -> None:
    write(tree, f"docs/{PLANTED}.md", "Clean text.\n")
    (tree / "docs" / f"{PLANTED}.wav").write_bytes(b"RIFF" + bytes(8))
    assert sorted(check_style.check(tree)) == [path_problem("docs/(an address)")] * 2


def test_a_path_that_holds_an_address_is_printed_with_it_cut_out(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every problem in that file names it, the style's included."""
    write(tree, f"{PLANTED}/notes.md", f"A dash {DASH} here.\nWrite to {at('other', 'example.org')}.\n")
    assert check_style.check(tree) == [
        "(an address)/notes.md:1: em-dash. Use a comma, a colon, or two sentences.",
        path_problem("(an address)/notes.md"),
        address_problem("(an address)/notes.md", 2, 10),
    ]
    assert check_style.main(["check_style.py", str(tree)]) == 1
    captured = capsys.readouterr()
    for piece in (PLANTED, "someone", "example.net", "other", "example.org"):
        assert piece not in captured.out + captured.err


def test_an_allowed_address_in_a_folder_name_passes(tree: Path) -> None:
    write(tree, f"{PROJECT}/notes.md", "Clean text.\n")
    assert check_style.check(tree) == []


# -------------------------------------------------- git's list, and the walk


def test_a_tree_with_no_git_folder_is_walked(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The body runner unpacks a tree with no `.git`, and git is never asked."""

    def no_git(*args: object, **kwargs: object) -> None:
        raise AssertionError("git was asked about a tree that is no checkout")

    monkeypatch.setattr(check_style.subprocess, "run", no_git)
    assert check_style.tracked_files(tree) is None
    write(tree, "notes.md", f"Write to {PLANTED}.\n")
    assert check_style.check(tree) == [address_problem("notes.md", 1, 10)]


@pytest.mark.parametrize("failure", ["missing", "refuses", "hangs"])
def test_a_checkout_git_cannot_list_is_walked(tree: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    (tree / ".git").mkdir()

    def broken(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if failure == "missing":
            raise FileNotFoundError("git")
        if failure == "hangs":
            raise subprocess.TimeoutExpired(command, 120)
        return subprocess.CompletedProcess(command, 128, b"", b"fatal: detected dubious ownership")

    monkeypatch.setattr(check_style.subprocess, "run", broken)
    assert check_style.tracked_files(tree) is None
    write(tree, "notes.md", f"Write to {PLANTED}.\n")
    write(tree, ".git/config", f"email = {PLANTED}\n")
    assert check_style.check(tree) == [address_problem("notes.md", 1, 10)]


@pytest.fixture
def checkout(tree: Path) -> Path:
    """The tree as a git checkout of its own, with one folder ignored."""
    if shutil.which("git") is None:
        pytest.skip("no git on this machine")
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True, timeout=60)
    write(tree, ".gitignore", "scratch/\nkeys.env\n")
    return tree


def test_in_a_checkout_git_says_which_files_are_read(checkout: Path) -> None:
    """A file git tracks is read even under a folder the walk skips."""
    tracked = write(checkout, "docs/tracked.md", f"Write to {PLANTED}.\n")
    built = write(checkout, "build/notes.md", f"Write to {PLANTED}.\n")
    subprocess.run(
        ["git", "-C", str(checkout), "add", "-f", "docs/tracked.md", "build/notes.md"],
        check=True, capture_output=True, timeout=60,
    )
    write(checkout, "new/untracked.md", f"Write to {PLANTED}.\n")
    write(checkout, "scratch/ignored.md", f"Write to {PLANTED}.\n")
    write(checkout, "keys.env", f"OWNER={PLANTED}\n")
    listed = check_style.tracked_files(checkout)
    assert listed is not None and tracked in listed and built in listed
    assert check_style.skipped(checkout, built)
    assert {path.relative_to(checkout).as_posix() for path in listed} == {
        ".gitignore", "README.md", "build/notes.md", "docs/tracked.md", "emet-sdk/module.py", "new/untracked.md",
    }
    assert check_style.check(checkout) == [
        address_problem("build/notes.md", 1, 10),
        address_problem("docs/tracked.md", 1, 10),
        address_problem("new/untracked.md", 1, 10),
    ]


def test_a_tracked_file_deleted_from_the_tree_is_passed_over(checkout: Path) -> None:
    gone = write(checkout, "docs/gone.md", f"Write to {PLANTED}.\n")
    subprocess.run(["git", "-C", str(checkout), "add", "docs/gone.md"], check=True, capture_output=True, timeout=60)
    gone.unlink()
    assert check_style.check(checkout) == []
