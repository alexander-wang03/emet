"""The one table of the failure lines `emet-listen` and `emet-talk` print.

Each row is one way a run can fail that a person must be told about in
plain words: what failed, when, and what to do. The session picks a turn's
row with `find()` from four facts it has at hand (when the failure was
found, which stage failed, the turn's outcome, and the fault kind the
stage's `health()` names), and the two programs print the row's text. A
boot row is taken by its case with `row()`: a provider's boot row serves
every fault its stage can refuse the boot with, and its `fault` names the
kind it was written for. Neither program reads an error's wording to
choose a line, so a vendor that rewords its message changes the detail a
person reads and never which line or spoken reply they get.

A row's `console_talk` and `console_listen` are format strings over four
fields: `{name}` (the plugin's name), `{detail}` (the plugin's own
sentence, from `health().detail`, `ReplyDone.error` or `Transcript.error`),
`{cap}` (the reply's token cap) and `{exc}` (an exception's text).
`spoken` names a key of `prompting.DEFAULT_LINES`, the line the robot says
aloud; `signal` names the `signal` intent the session performs with it.
`(when, stage, outcome, fault)` is unique across the table, which
`tests/test_failures.py` holds.

A `boot` row is printed by the boot as a refusal (`\\n{text}` on stderr,
exit 1), except `llm-unavailable-at-boot`, which is a notice and boots on.
A provider's refusal and the registry's come before any device opens. The
audio sink's comes after the body's parts have started, and the audio
source's after the sink has opened: a replay's file before the boot tone,
and a microphone after it, since the tone plays before the microphone
opens on purpose. A `before` or `during` row is printed for one turn and
the run goes on.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

__all__ = [
    "Failure",
    "FAILURES",
    "FIELDS",
    "OUTCOMES",
    "PROGRAMS",
    "fields_of",
    "find",
    "row",
]

#: The two programs that print these lines.
PROGRAMS: frozenset[str] = frozenset({"emet-listen", "emet-talk"})

#: The fields a row's console text may name.
FIELDS: frozenset[str] = frozenset({"name", "detail", "cap", "exc"})

#: How a turn can end, the per-turn record's `outcome` (contract 0.5.7
#: section 2.1). A row's `outcome` is one of these, or None for a boot row.
OUTCOMES: frozenset[str] = frozenset({
    "answered",
    "cut_at_cap",
    "empty_at_cap",
    "declined",
    "failed",
    "unavailable",
    "out_of_credit",
    "words_lost",
    "nothing_heard",
    "no_speech",
    "not_answered",
})


@dataclass(frozen=True, slots=True)
class Failure:
    """One failure line, and what the robot says and shows with it.

    `case` names the row, with no program in it. `when` is `boot`,
    `before` (a wake the model cannot answer, so nothing was sent) or
    `during` (a fault found on this turn's request). `stage` is `stt`,
    `llm`, `voice`, `source`, `sink` or `registry`. `outcome` is the
    turn's outcome, None for a boot row; `fault` is a kind from
    `emet_sdk.types.FAULT_KINDS`, or None. `stream` is `stdout` or
    `stderr`. `spoken` is a key of `prompting.DEFAULT_LINES`, or None for
    a row the robot says nothing for; `signal` is `offline`, `unavailable`
    or None. `programs` are the programs that print the row.
    """

    case: str
    when: str
    stage: str
    outcome: str | None
    fault: str | None
    console_talk: str
    console_listen: str
    stream: str
    spoken: str | None = None
    signal: str | None = None
    programs: frozenset[str] = PROGRAMS

    def template(self, program: str) -> str:
        """The format string `program` prints for this row."""
        if program == "emet-talk":
            return self.console_talk
        if program == "emet-listen":
            return self.console_listen
        raise ValueError(f"no failure line for the program {program!r}; the programs are emet-listen and emet-talk")

    def text(self, program: str, **values: object) -> str:
        """The line `program` prints, its fields filled from `values`.

        A field left out is empty, and the space it would have followed at
        the end of the line goes with it, so a refusal with no detail ends
        at its own full stop. A name `FIELDS` does not hold is refused.
        """
        unknown = set(values) - FIELDS
        if unknown:
            raise ValueError(f"no field {sorted(unknown)[0]!r} in a failure line; the fields are {', '.join(sorted(FIELDS))}")
        filled = {name: values.get(name, "") for name in FIELDS}
        return self.template(program).format(**filled).rstrip()


def _boot(case: str, stage: str, fault: str | None, text: str) -> Failure:
    """A boot refusal, the same words in both programs, on stderr."""
    return Failure(case, "boot", stage, None, fault, text, text, "stderr")


#: The language model's line when it cannot start, today's refusal.
_LLM_CANNOT_START = (
    "the language model {name!r} cannot start, so the robot would hear questions it can never answer. "
    "It will not run. {detail}"
)
_UNAVAILABLE_NOTICE = (
    "the language model {name!r} cannot answer, so each wake gets one line and nothing is sent to the "
    "transcriber or the model. {detail}"
)
_UNAVAILABLE_TURN = "the language model cannot answer; nothing was sent to the transcriber or the model"
_EMPTY_AT_CAP = (
    "no words: the reply stopped at its cap of {cap} tokens before any text, which a reasoning model "
    "does when it spends them thinking; a lower reasoning effort in models.chat.params leaves room for "
    "the words"
)

FAILURES: tuple[Failure, ...] = (
    # "Nothing is sent" names the two stages that get nothing: a cloud voice
    # still gets the line's words, which the turn's `sent` counts.
    Failure(
        "llm-unavailable-at-boot", "boot", "llm", None, "model_unavailable",
        f"  ! {_UNAVAILABLE_NOTICE}",
        f"  ! {_UNAVAILABLE_NOTICE}",
        "stderr",
    ),
    Failure(
        "llm-unavailable-before", "before", "llm", "unavailable", "model_unavailable",
        f"  ({_UNAVAILABLE_TURN})",
        f"    {_UNAVAILABLE_TURN}",
        "stdout", "unavailable", "unavailable",
    ),
    Failure(
        "llm-unavailable-on-reply", "during", "llm", "unavailable", "model_unavailable",
        "  (the language model cannot answer: {detail})",
        "    the language model cannot answer: {detail}",
        "stdout", "unavailable", "unavailable",
    ),
    Failure(
        "llm-empty-at-cap", "during", "llm", "empty_at_cap", None,
        f"  ({_EMPTY_AT_CAP})",
        f"    {_EMPTY_AT_CAP}",
        "stdout", "failed",
    ),
    Failure(
        "llm-cut-at-cap", "during", "llm", "cut_at_cap", None,
        "  (cut off at the reply's token cap)",
        "    (cut off at the reply's token cap)",
        "stdout",
    ),
    Failure(
        "llm-out-of-credit", "during", "llm", "out_of_credit", "out_of_credit",
        "  (the language model cannot answer: {detail})",
        "    the language model cannot answer: {detail}",
        "stdout", "out_of_credit",
    ),
    Failure(
        "llm-failed", "during", "llm", "failed", None,
        "  (the language model failed: {detail})",
        "    the language model failed: {detail}",
        "stdout", "failed", "offline",
    ),
    _boot("llm-cannot-start", "llm", "start_failed", _LLM_CANNOT_START),
    _boot(
        "stt-out-of-credit-at-boot", "stt", "out_of_credit",
        "the speech recognition provider {name!r} cannot start, so what people say would go nowhere. "
        "It will not run half-deaf. {detail}",
    ),
    _boot(
        "voice-out-of-credit-at-boot", "voice", "out_of_credit",
        "the voice {name!r} cannot start, so the robot would answer and never be heard. It will not run. {detail}",
    ),
    # The hearing's own words wait on the maintainer (contract section 9,
    # C6), so an unpaid transcriber says the failed line until then.
    Failure(
        "stt-out-of-credit", "during", "stt", "out_of_credit", "out_of_credit",
        "  (the words never arrived: {detail})",
        "    the words never arrived: {detail}",
        "stdout", "failed",
    ),
    Failure(
        "stt-model-rejected", "during", "stt", "words_lost", "model_unavailable",
        "  (the words never arrived: {detail})",
        "    the words never arrived: {detail}",
        "stdout", "failed",
    ),
    Failure(
        "stt-words-lost", "during", "stt", "words_lost", None,
        "  (the words never arrived: {detail})",
        "    the words never arrived: {detail}",
        "stdout", "failed", "offline",
    ),
    _boot(
        "source-cannot-start", "source", None,
        "the audio source {name!r} cannot start, so the robot would listen to nothing. It will not run. {exc}",
    ),
    _boot(
        "sink-cannot-start", "sink", None,
        "the audio sink {name!r} cannot start, so nothing the robot says could be heard. It will not run. {exc}",
    ),
    _boot("duplicate-plugin", "registry", None, "{exc}"),
)

_BY_KEY: dict[tuple[str, str, str | None, str | None], Failure] = {
    (f.when, f.stage, f.outcome, f.fault): f for f in FAILURES
}
_BY_CASE: dict[str, Failure] = {f.case: f for f in FAILURES}


def find(when: str, stage: str, outcome: str | None, fault: str | None) -> Failure | None:
    """The row for these four facts, or None for a turn with no failure line."""
    return _BY_KEY.get((when, stage, outcome, fault))


def row(case: str) -> Failure:
    """The row named `case`, such as `source-cannot-start`."""
    try:
        return _BY_CASE[case]
    except KeyError:
        raise KeyError(f"no failure row {case!r}; the rows are {', '.join(sorted(_BY_CASE))}") from None


def fields_of(template: str) -> set[str]:
    """The field names a format string reads, without conversions or specs."""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
