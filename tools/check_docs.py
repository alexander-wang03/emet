#!/usr/bin/env python3
"""Run the documents' commands and check their links (RP9, stage 1).

    python tools/check_docs.py .               run what is safe, check every link
    python tools/check_docs.py . --list        every command, its document, line and class
    python tools/check_docs.py . --external    also ask each outside link's site, as warnings

**Commands.** Every fenced block in `DOCUMENTS` labelled `sh`, `bash`,
`shell` or `console`, and every unlabelled block whose first line starts
with a known command or matches a rule of `RULES`, is read as what a
reader would paste. A block whose
lines start with `$ ` is a transcript: those lines are its commands and
the rest is output. A line ending in `\\` goes on with the next, a heredoc
runs to its closing word, and a `#` comment is dropped.

Each command is held to `RULES`, a small table in this file, and gets one
of three classes:

    run       safe in a clean checkout with the four packages installed:
              `emet validate`, `emet explain`, `emet --version`, the
              repository's own `tools/check_*.py`, `tools/ci.py --list`,
              `--help` of each console script, the generated bank's
              `--plan`, and pytest, which is only collected
              (`--collect-only`), since the test jobs run the suites
    skip      with its reason: it needs a microphone or a speaker, a key,
              the Pi, the voice bank, git state, or it installs something
    unknown   no rule matches. A failure: a new command in a document is
              classified in `RULES` before it merges

A `run` command that exits other than its rule expects fails the check,
with its document, its line and the last lines of its output. A command
joined with `&&` is judged part by part, and the strictest part wins. A
`cd` moves the block on; each block starts in the folder `DOCUMENTS`
gives its document, the place its paths are written from, since a block
is what a reader pastes at once. A skipped `pip install -e PATH` is not
run, and its PATH is still looked for, since a path that is not there
fails for every reader.

Nothing `run` may open audio hardware, reach a vendor or change git state:
`tools/tests/test_check_docs.py` holds the table to that. Every `EMET_`
variable, the names the shipped souls give their keys, and every variable
whose name ends in `_KEY`, `_TOKEN` or `_SECRET` is taken out of a
command's environment.

**Links.** Every Markdown link in the repository's Markdown files. A
relative link must name a file or folder that exists inside the
repository, and its `#fragment` a heading of the file it names, by the
anchors GitHub makes. The anchor rule is github-slugger's
(https://github.com/Flet/github-slugger, ISC, verified 2026-11-26), which
copies GitHub's: lower case; letters, marks, decimal and letter numbers,
connector punctuation such as `_`, `-` and spaces kept, every other
character dropped; each space a `-`; and a repeat numbered `-1`, `-2`,
stepping past a number an earlier heading already took. github-slugger
also keeps the few symbols Unicode gives the Alphabetic property, such as
the circled letters; the standard library cannot name that property, so
a heading holding one is where the two differ. An outside link is asked
only with `--external`, one HEAD request each, and a GET when the site
refuses HEAD, with a timeout and a plain client name; a failure is a
warning, since a CI run must not fail on a site's bad day.

**Where the idea comes from.** PX4's documentation workflow runs a link
check on every pull request that changes its documents
(`.github/workflows/docs-orchestrator.yml` in PX4/PX4-Autopilot,
BSD-3-Clause, verified 2026-11-26). The idea is taken; no code is. See
CITATIONS.md.

Standard library only.

Exit: 0 clean; 1 a command failed or is unknown, or a link is dead; 2 a
usage error.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

#: The documents whose commands are run, each with the folder its paths
#: are written from.
DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("README.md", "."),
    ("CONTRIBUTING.md", "."),
    ("RELEASING.md", "."),
    ("emet-sdk/README.md", "emet-sdk"),
    ("emet-hal/README.md", "emet-hal"),
    ("emet-providers/README.md", "emet-providers"),
    ("emet-engine/README.md", "emet-engine"),
    ("tools/bank/README.md", "."),
    ("tools/body/README.md", "."),
    ("tools/body/sitting.md", "."),
    ("tools/ci/README.md", "."),
)

SHELL_LABELS = {"sh", "bash", "shell", "console", "shell-session"}

#: The first words that make an unlabelled block a block of commands even
#: when no rule matches its first line, so the line is reported unknown. A
#: first line that a rule matches makes it one too (`is_command`).
KNOWN_COMMANDS = {
    "python", "python3", "pip", "pip3", "pytest", "emet", "emet-listen", "emet-talk", "emet-say", "git", "cd",
    "source", "mkdir", "cat", "chmod", "ssh", "script", '"$PY"', "$PY", ".venv/bin/pip", ".venv/bin/pytest",
    ".venv/bin/python", ".venv/Scripts/python", "sudo", "printf", "rm",
}

#: Names of environment variables a run command never gets, besides every
#: `EMET_` one: the endings other services give their keys.
SECRET_ENDINGS = ("_KEY", "_TOKEN", "_SECRET")

#: Never descend into these when collecting Markdown.
SKIP_DIRS = {".git", ".venv", "__pycache__", "dist", "build", ".pytest_cache", "node_modules"}

RUN, SKIP, UNKNOWN = "run", "skip", "unknown"


@dataclass(frozen=True)
class Rule:
    """One row of the table. `pattern` must match a command's part whole,
    after its comment is dropped and its spaces are made single."""

    pattern: str
    kind: str
    reason: str
    exit: int = 0
    collect: bool = False


PY = r'(?:python3?|"\$PY"|\$PY|\.venv/bin/python)'

RULES: tuple[Rule, ...] = (
    # Moving about the tree.
    Rule(r"cd (?![~/$])\S+", RUN, "moves the block's working folder"),
    Rule(r"cd ~/Documents/emet", SKIP, "the Pi: the maintainer's checkout there"),
    Rule(r"source \S*activate", SKIP, "activates a venv this check does not use"),
    # The SDK's command line, which reads files and opens nothing.
    Rule(r"emet validate examples/invalid/\S+\.yaml", RUN, "an invalid example, refused", exit=1),
    Rule(r"emet (?:validate|explain)(?: \S+)+", RUN, "reads the files it names"),
    Rule(r"emet --version", RUN, "prints the version"),
    Rule(r"emet(?:-listen|-talk|-say)? (?:--help|-h)", RUN, "prints the help and opens nothing"),
    # The repository's own checks. This script is left out: it would run itself.
    Rule(PY + r" tools/check_health\.py .*--update.*", SKIP, "rewrites tools/health_budget.json"),
    Rule(PY + r" tools/check_[a-z_]+\.py .*<[^<>]+>.*", SKIP, "names a file by a placeholder, which a reader "
         "fills in first"),
    Rule(PY + r" tools/(?:check_(?!docs\.py)[a-z_]+|release_check|ci/check_workflows)\.py(?: \S+)*", RUN,
         "a check of the tree"),
    Rule(PY + r" tools/ci\.py --list", RUN, "prints the CI jobs"),
    Rule(PY + r" tools/ci\.py .*", SKIP, "runs CI jobs, which install packages and run the suites"),
    Rule(PY + r" tools/dev\.py (?:setup|update)(?: \S+)*", SKIP, "makes or reinstalls a venv"),
    Rule(r"(?:" + PY + r"|\S*/python3?(?:\.exe)?) tools/dev\.py check", SKIP,
         "loads sounddevice, which starts PortAudio and lists the sound devices"),
    # The suites are collected, never run: the test jobs run them.
    Rule(r"(?:" + PY + r" -m pytest|pytest|\.venv/bin/pytest)(?: \S+)*", RUN,
         "collected only, with --collect-only and the cache off", collect=True),
    # Git and GitHub.
    Rule(r"git \S.*", SKIP, "changes git state"),
    Rule(PY + r" tools/release_audit\.py --pr .*", SKIP, "reads the pull request on GitHub, and merges it with --merge"),
    Rule(PY + r" tools/release_audit\.py --complete", SKIP, "reads the tags and the release pages on GitHub"),
    Rule(PY + r" tools/tag_release\.py .*", SKIP, "makes and pushes a signed tag"),
    # Installing.
    Rule(r"(?:\S*/)?pip3? install .*", SKIP, "installs packages"),
    Rule(r"\S*python3?(?:\.exe)? -m pip install .*", SKIP, "installs packages"),
    Rule(PY + r" -m venv .*", SKIP, "makes a venv"),
    Rule(PY + r" -m piper\.download_voices .*", SKIP, "downloads a voice model"),
    # The owner's keys.
    Rule(r"mkdir -p ~/\.config/emet", SKIP, "makes the folder of the owner's keys"),
    Rule(r"cat > ~/\.config/emet/keys\.env <<.*", SKIP, "writes the owner's keys file"),
    Rule(r"chmod 600 ~/\.config/emet/keys\.env", SKIP, "the owner's keys file"),
    # The robot itself.
    Rule(r"emet-talk .*", SKIP, "opens the microphone and the speaker, and needs a key for each stage the soul names"),
    Rule(r"emet-listen .*--replay .*", SKIP, "replays a recording that is not in the repository"),
    Rule(r"emet-listen .*", SKIP, "opens the microphone"),
    Rule(r"emet-say .*", SKIP, "plays through the manifest's sink, or writes a file, and needs a key for the "
         "voice the soul names"),
    Rule(r"script .*", SKIP, "the Pi: a typescript of the robot"),
    Rule(PY + r" tools/body/run\.py .*", SKIP, "the Pi, over SSH"),
    Rule(r"ssh .*", SKIP, "the Pi, over SSH"),
    Rule(r"sudo .*", SKIP, "the Pi: needs root, the maintainer's password"),
    Rule(r"printf .*/tmp/emet-netcut.*", SKIP, "the Pi: writes the network cut's sudo rule"),
    Rule(r"rm /tmp/emet-netcut", SKIP, "the Pi: removes the network cut's draft rule"),
    # The voice bank, which never enters the repository.
    Rule(r"python3 tools/bank/(?:record|check)\.py .*", SKIP, "the Pi: the voice bank's copy there"),
    Rule(PY + r" tools/bank/sit\.py .*", SKIP, "plays tones on the laptop's speaker and records on the Pi"),
    Rule(PY + r" tools/bank/(?:check|replay)\.py .*", SKIP, "needs a bank, which is kept outside the repository"),
    # The generated bank: its plan prints from the data files in the tree.
    Rule(PY + r" tools/bank/generate\.py --plan(?: --set \S+)?", RUN, "prints the generated bank's plan from "
         "tools/bank/generated/, and needs no voice"),
    Rule(r"\S*python3? tools/bank/generate\.py --fetch-voices .*", SKIP, "downloads the generated bank's voices"),
    Rule(r"\S*python3? tools/bank/generate\.py --compare .*", SKIP,
         "compares two built generated banks, which are kept outside the repository"),
    Rule(r"\S*python3? tools/bank/generate\.py \S+ --set .*", SKIP,
         "builds a generated bank outside the repository, with Piper in a venv of its own"),
    Rule(PY + r" tools/bank/paid_replay\.py .*--dry-run.*", SKIP, "needs the voice bank's miss list and ledger"),
    Rule(PY + r" tools/bank/paid_replay\.py .*", SKIP, "spends money: a paid Deepgram replay, with a key"),
    Rule(r"<tools-venv>/python tools/bank/label\.py .*", SKIP, "needs a tools venv and the voice bank"),
    # A local language model's own server, which its owner installs and runs (emet-providers/README.md).
    Rule(r"litert-lm (?:import|serve) .*", SKIP, "downloads a local model, or starts its server"),
    Rule(r"(?:OLLAMA_KEEP_ALIVE=\S+ )?ollama (?:pull|serve)(?: \S+)*", SKIP, "downloads a local model, or starts its server"),
    Rule(r"llama-server .*", SKIP, "starts a local model's server on a model file the reader downloads"),
    # CI's own tools, which go in tools/.venv (tools/ci/README.md).
    Rule(r"zizmor .*", SKIP, "needs the CI tools, which go in tools/.venv"),
    Rule(r"\S*uv(?:\.exe)? pip compile .*", SKIP, "makes the CI tools' lock again, with uv from a scratch venv"),
)


# --------------------------------------------------------------- reading


@dataclass
class Command:
    document: str
    line: int
    text: str                 # as written, the lines joined
    block: int                # which block of the document it came from
    heredoc: str = ""


@dataclass
class Link:
    document: str
    line: int
    target: str


def strip_comment(text: str) -> str:
    """The line with its shell comment dropped, a `#` at the start or after a space, outside quotes."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1].isspace()):
            return text[:i].rstrip()
    return text.rstrip()


def normal(text: str) -> str:
    return " ".join(strip_comment(text).split())


def fences(lines: Sequence[str]) -> Iterable[tuple[int, str, list[tuple[int, str]]]]:
    """Each fenced block, with its opening line's number, its label and its lines."""
    i = 0
    while i < len(lines):
        m = re.match(r"^( {0,3})(`{3,}|~{3,})\s*([^\s`]*)", lines[i])
        if not m:
            i += 1
            continue
        fence, label, start = m.group(2), m.group(3).lower(), i + 1
        body: list[tuple[int, str]] = []
        i += 1
        while i < len(lines) and not re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$",
                                              lines[i]):
            body.append((i + 1, lines[i]))
            i += 1
        i += 1
        yield start, label, body


def first_word(line: str) -> str:
    text = line.strip()
    if text.startswith("$ "):
        text = text[2:].strip()
    return text.split()[0] if text.split() else ""


def is_command(line: str, rules: Sequence[Rule]) -> bool:
    """Whether an unlabelled block's first line is a command: its first word
    is a known command, or a rule matches its first part."""
    if first_word(line) in KNOWN_COMMANDS:
        return True
    text = line.strip()
    text = text[2:] if text.startswith("$ ") else text
    parts = split_and(strip_comment(text))
    return bool(parts) and rule_for(normal(parts[0]), rules) is not None


def commands_in(document: str, text: str, rules: Sequence[Rule] = ()) -> list[Command]:
    """The commands of a document's blocks. `rules` are the ones that make
    an unlabelled block a block of commands, beside `KNOWN_COMMANDS`."""
    out: list[Command] = []
    lines = text.splitlines()
    for number, (start, label, body) in enumerate(fences(lines)):
        firsts = [line for _, line in body if line.strip() and not line.strip().startswith("#")]
        if label not in SHELL_LABELS and not (label == "" and firsts and is_command(firsts[0], rules or RULES)):
            continue
        transcript = any(line.lstrip().startswith("$ ") for _, line in body)
        i = 0
        while i < len(body):
            n, line = body[i]
            i += 1
            stripped = line.strip()
            if transcript:
                if not stripped.startswith("$ "):
                    continue
                stripped = stripped[2:]
            if not stripped or stripped.startswith("#"):
                continue
            joined = stripped
            while joined.endswith("\\") and i < len(body):
                joined = joined[:-1].rstrip() + " " + body[i][1].strip()
                i += 1
            heredoc = ""
            m = re.search(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1", strip_comment(joined))
            if m:
                ends, words = m.group(2), []
                while i < len(body) and body[i][1].strip() != ends:
                    words.append(body[i][1])
                    i += 1
                i += 1
                heredoc = "\n".join(words)
            out.append(Command(document, n, joined, number, heredoc))
    return out


# ------------------------------------------------------------ classifying


@dataclass
class Judged:
    command: Command
    kind: str
    reason: str
    parts: list[tuple[str, Rule | None]] = field(default_factory=list)


def split_and(text: str) -> list[str]:
    """The parts of a command joined by `&&`, outside quotes."""
    parts, quote, start, i = [], "", 0, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif text.startswith("&&", i):
            parts.append(text[start:i])
            start = i + 2
            i += 1
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def rule_for(part: str, rules: Sequence[Rule]) -> Rule | None:
    for rule in rules:
        if re.fullmatch(rule.pattern, part):
            return rule
    return None


def classify(command: Command, rules: Sequence[Rule] = RULES) -> Judged:
    parts = [(normal(p), None) for p in split_and(strip_comment(command.text))]
    judged = [(text, rule_for(text, rules)) for text, _ in parts]
    unknown = [text for text, rule in judged if rule is None]
    if unknown:
        return Judged(command, UNKNOWN, f"no rule matches {unknown[0]!r}: add a Rule for it to RULES in "
                                        f"tools/check_docs.py, run or skip with its reason", judged)
    skips = [rule for _, rule in judged if rule is not None and rule.kind == SKIP]
    if skips:
        return Judged(command, SKIP, skips[0].reason, judged)
    reasons = [rule.reason for text, rule in judged if rule is not None and not text.startswith("cd ")]
    return Judged(command, RUN, reasons[0] if reasons else "moves the block's working folder", judged)


# ---------------------------------------------------------------- running


@dataclass
class Outcome:
    judged: Judged
    ok: bool
    detail: str = ""


SHELL_SYNTAX = re.compile(r"[|;<>`]|\$\(")


def outside_quotes(text: str) -> str:
    """The text with `|`, `;`, `<` and `>` blanked inside quotes, where they
    are an argument's own characters. A single-quoted span is blanked
    whole; inside double quotes `$(` and a backtick still expand."""
    def blank(m: re.Match[str]) -> str:
        span = m.group(0)
        return " " * len(span) if span[0] == "'" else re.sub(r"[|;<>]", " ", span)
    return re.sub(r"'[^']*'|\"[^\"]*\"", blank, text)


def program(word: str) -> list[str] | None:
    """The argv a document's first word stands for, with this interpreter
    for `python` and its own console scripts for `emet`."""
    if word in ("python", "python3", "$PY", ".venv/bin/python", ".venv/Scripts/python"):
        return [sys.executable]
    if word in ("pytest", ".venv/bin/pytest"):
        return [sys.executable, "-m", "pytest"]
    if word in ("emet", "emet-listen", "emet-talk", "emet-say"):
        here = Path(sys.executable).parent
        for candidate in (here / word, here / f"{word}.exe", here / "Scripts" / f"{word}.exe"):
            if candidate.is_file():
                return [str(candidate)]
        found = shutil.which(word)
        return [found] if found else None
    return None


def clean_env() -> dict[str, str]:
    """This environment, less every `EMET_` variable and every one whose
    name ends as a key's does."""
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith("EMET_") and not k.upper().endswith(SECRET_ENDINGS)}


def tail(text: str, n: int = 12) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join("      " + line for line in lines[-n:])


Runner = Callable[[list[str], Path], tuple[int, str]]


def real_runner(argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, cwd=cwd, env=clean_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        return 124, "timed out after 600 s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def editable_paths(tokens: list[str]) -> list[str]:
    paths = []
    for i, token in enumerate(tokens):
        if token in ("-e", "--editable") and i + 1 < len(tokens):
            paths.append(tokens[i + 1])
        elif token.startswith("--editable="):
            paths.append(token.split("=", 1)[1])
    return [re.sub(r"\[.*\]$", "", p) for p in paths]


def run_block(root: Path, start: str, judged: list[Judged], runner: Runner) -> list[Outcome]:
    """One block's commands in order, from its document's folder."""
    cwd: Path | None = (root / start).resolve()
    out: list[Outcome] = []
    for j in judged:
        if j.kind == UNKNOWN:
            out.append(Outcome(j, False, j.reason))
            continue
        problem = ""
        for text, rule in j.parts:
            if rule is None:
                problem = f"no rule matches {text!r}"
                break
            try:
                tokens = shlex.split(text)
            except ValueError as exc:
                problem = f"cannot read {text!r}: {exc}"
                break
            if tokens and tokens[0] == "cd":
                target = tokens[1] if len(tokens) > 1 else "~"
                if target.startswith(("~", "/", "$")) or cwd is None:
                    cwd = None
                    continue
                moved = (cwd / target).resolve()
                if not moved.is_dir():
                    problem = f"cd {target}: no such folder from {display(root, cwd)}"
                    break
                cwd = moved
                continue
            if rule.kind == SKIP:
                if cwd is not None and re.match(r"(?:\S*/)?pip3? install |\S+ -m pip install ", text):
                    for path in editable_paths(tokens):
                        if not (cwd / path).resolve().is_dir():
                            problem = (f"would fail: pip install -e {path}: no such folder from "
                                       f"{display(root, cwd)}")
                    if problem:
                        break
                continue
            if j.kind != RUN:
                continue
            if cwd is None:
                problem = "the folder it runs in is unknown, after a cd outside the repository"
                break
            if SHELL_SYNTAX.search(outside_quotes(strip_comment(text))):
                problem = f"{text!r} needs a shell, and a run command is a plain one"
                break
            argv = program(tokens[0])
            if argv is None:
                problem = f"{tokens[0]} is not installed beside {sys.executable}"
                break
            argv = argv + tokens[1:]
            if rule.collect:
                argv += [a for a in ("--collect-only", "-q") if a not in argv] + ["-p", "no:cacheprovider"]
            code, output = runner(argv, cwd)
            if code != rule.exit:
                problem = (f"exited {code}, expected {rule.exit}, in {display(root, cwd)}:\n"
                           f"{tail(output)}")
                break
        out.append(Outcome(j, not problem, problem))
    return out


def display(root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)
    return rel if rel != "." else "the repository's root"


# ------------------------------------------------------------------ links


def strip_code_spans(line: str) -> str:
    return re.sub(r"(`+)(.+?)\1", lambda m: " " * len(m.group(0)), line)


INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")


def prose_lines(text: str) -> Iterable[tuple[int, str]]:
    """Each line outside fenced blocks and HTML comments."""
    lines = text.splitlines()
    inside: set[int] = set()
    for start, _label, body in fences(lines):
        inside.add(start)
        inside.update(n for n, _ in body)
        inside.add((body[-1][0] if body else start) + 1)
    comment = False
    for n, line in enumerate(lines, 1):
        if n in inside:
            continue
        if comment:
            if "-->" in line:
                comment = False
                line = line.split("-->", 1)[1]
            else:
                continue
        while "<!--" in line:
            before, _, after = line.partition("<!--")
            if "-->" in after:
                line = before + after.split("-->", 1)[1]
            else:
                line, comment = before, True
        yield n, line


def links_in(document: str, text: str) -> list[Link]:
    out = []
    for n, line in prose_lines(text):
        line = strip_code_spans(line)
        for pattern in (INLINE_LINK, REFERENCE, AUTOLINK):
            for m in pattern.finditer(line):
                out.append(Link(document, n, m.group(1).strip("<>")))
    return out


def slug(text: str) -> str:
    """github-slugger's rule: lower case; letters, marks, decimal and letter
    numbers (`Nd`, `Nl`, where `No` such as `²` is dropped), connector
    punctuation (`Pc`, `_` among it), `-` and spaces kept; every other
    character dropped; each space a `-`."""
    def kept(ch: str) -> bool:
        category = unicodedata.category(ch)
        return ch in " -" or category[0] in "LM" or category in ("Nd", "Nl", "Pc")

    return "".join(ch for ch in text.lower() if kept(ch)).replace(" ", "-")


def heading_text(raw: str) -> str:
    """A heading's text as GitHub renders it: links and images to their
    text, tags dropped, code kept as written, emphasis marks dropped."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", raw)
    text = re.sub(r"<[^>]+>", "", text)
    pieces = re.split(r"(`+[^`]*`+)", text)
    rendered = []
    for piece in pieces:
        if piece.startswith("`"):
            rendered.append(piece.strip("`").strip())
        else:
            piece = re.sub(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])", "", piece)
            rendered.append(piece.replace("*", ""))
    return html.unescape("".join(rendered)).strip()


def anchors(text: str) -> set[str]:
    """Every anchor a document's headings and `<a name>` tags make. A
    repeat takes github-slugger's next free number: headings `A`, `A 1`,
    `A` give `a`, `a-1`, `a-2`."""
    found: set[str] = set()
    made: dict[str, int] = {}          # github-slugger's occurrences: every slug made, and its count
    previous = ""
    for _n, line in prose_lines(text):
        m = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$", line)
        title = None
        if m:
            title = m.group(2) or ""
        elif re.match(r"^ {0,3}(=+|-+)[ \t]*$", line) and previous.strip() and \
                not re.match(r"^\s*([#>|*+-]|\d+\.|`{3}|~{3})", previous):
            title = previous.strip()
        if title is not None:
            base = slug(heading_text(title))
            result = base
            while result in made:
                made[base] += 1
                result = f"{base}-{made[base]}"
            made[result] = 0
            found.add(result)
        for a in re.finditer(r"<a\s+(?:name|id)=[\"']([^\"']+)[\"']", line):
            found.add(a.group(1))
        previous = line
    return found


def markdown_files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.md")):
        parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts):
            continue
        out.append(path)
    return out


def check_link(root: Path, link: Link, cache: dict[Path, set[str]]) -> str:
    """'' when the link resolves; otherwise why it does not."""
    target = link.target
    scheme = urllib.parse.urlsplit(target).scheme
    if scheme or target.startswith("//"):
        return ""
    path_part, _, fragment = target.partition("#")
    source = root / link.document
    if path_part:
        resolved = (source.parent / urllib.parse.unquote(path_part)).resolve()
    else:
        resolved = source.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return f"{path_part} is outside the repository, so GitHub cannot serve it"
    if not resolved.exists():
        return f"{path_part}: no such file or folder"
    if fragment and resolved.is_file() and resolved.suffix.lower() == ".md":
        if resolved not in cache:
            cache[resolved] = anchors(resolved.read_text(encoding="utf-8"))
        if urllib.parse.unquote(fragment).lower() not in cache[resolved]:
            return f"#{fragment}: no such heading in {resolved.relative_to(root.resolve()).as_posix()}"
    return ""


Fetch = Callable[[str], str]


def real_fetch(url: str) -> str:
    """'' when the site answers; otherwise why it did not."""
    headers = {"User-Agent": "emet-check-docs"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10):
                return ""
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue
            return f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return str(getattr(exc, "reason", exc))
    return "no answer"


# ------------------------------------------------------------------- main


@dataclass
class Result:
    outcomes: list[Outcome]
    dead: list[tuple[Link, str]]
    warnings: list[str]
    links: int

    @property
    def problems(self) -> int:
        return sum(not o.ok for o in self.outcomes) + len(self.dead)


def collect(root: Path, documents: Sequence[tuple[str, str]] = DOCUMENTS,
            rules: Sequence[Rule] = RULES) -> list[tuple[str, list[Judged]]]:
    """Each block's judged commands, with the folder the block starts in."""
    blocks: list[tuple[str, list[Judged]]] = []
    for document, start in documents:
        path = root / document
        if not path.is_file():
            continue
        by_block: dict[int, list[Judged]] = {}
        for command in commands_in(document, path.read_text(encoding="utf-8"), rules):
            by_block.setdefault(command.block, []).append(classify(command, rules))
        blocks.extend((start, judged) for _, judged in sorted(by_block.items()))
    return blocks


def check(root: Path, documents: Sequence[tuple[str, str]] = DOCUMENTS, rules: Sequence[Rule] = RULES,
          runner: Runner = real_runner, external: bool = False, fetch: Fetch = real_fetch) -> Result:
    outcomes: list[Outcome] = []
    for start, judged in collect(root, documents, rules):
        outcomes.extend(run_block(root, start, judged, runner))
    dead: list[tuple[Link, str]] = []
    warnings: list[str] = []
    cache: dict[Path, set[str]] = {}
    count = 0
    outside: dict[str, Link] = {}
    for path in markdown_files(root):
        document = path.relative_to(root).as_posix()
        for link in links_in(document, path.read_text(encoding="utf-8")):
            count += 1
            if urllib.parse.urlsplit(link.target).scheme in ("http", "https"):
                outside.setdefault(link.target, link)
                continue
            why = check_link(root, link, cache)
            if why:
                dead.append((link, why))
    if external:
        for url, link in sorted(outside.items()):
            why = fetch(url)
            if why:
                warnings.append(f"{link.document}:{link.line}: {url}: {why}")
    return Result(outcomes, dead, warnings, count)


def print_list(blocks: list[tuple[str, list[Judged]]]) -> None:
    counts = {RUN: 0, SKIP: 0, UNKNOWN: 0}
    for _start, judged in blocks:
        for j in judged:
            counts[j.kind] += 1
            text = " ".join(j.command.text.split())
            print(f"{j.command.document}:{j.command.line}  {j.kind:<7}  {text}")
            print(f"{'':>10}{j.reason}")
    print(f"check-docs: {sum(counts.values())} command(s), {counts[RUN]} run, {counts[SKIP]} skipped, "
          f"{counts[UNKNOWN]} unknown")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the documents' commands and check their links.")
    parser.add_argument("root", nargs="?", default=".", help="the repository's root")
    parser.add_argument("--list", action="store_true", help="print every command with its document, line and class")
    parser.add_argument("--external", action="store_true", help="ask each outside link's site too, as warnings")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "emet-sdk").is_dir():
        print(f"check-docs: {root} does not look like the Emet repository", file=sys.stderr)
        return 2
    if args.list:
        print_list(collect(root))
        return 0
    result = check(root, external=args.external)
    for o in result.outcomes:
        if o.ok:
            continue
        c = o.judged.command
        label = "unknown" if o.judged.kind == UNKNOWN else "failed"
        print(f"  x {c.document}:{c.line}: {label}: {' '.join(c.text.split())}\n    {o.detail}")
    for link, why in result.dead:
        print(f"  x {link.document}:{link.line}: dead link {link.target}: {why}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    kinds = [o.judged.kind for o in result.outcomes]
    print(f"check-docs: {len(kinds)} command(s), {kinds.count(RUN)} run, {kinds.count(SKIP)} skipped, "
          f"{kinds.count(UNKNOWN)} unknown; {sum(not o.ok for o in result.outcomes)} failed. "
          f"{result.links} link(s), {len(result.dead)} dead"
          + (f", {len(result.warnings)} outside warning(s)" if args.external else ""))
    if result.problems:
        print(f"check-docs: {result.problems} problem(s)")
        return 1
    print("check-docs: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
