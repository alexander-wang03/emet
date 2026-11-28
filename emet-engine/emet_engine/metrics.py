"""Measuring whether the loop keeps up.

0.3 is not done when it works once. The acceptance bar is ten minutes without
drift, and "without drift" is not something you can watch for. It is a number
or it is a feeling. This module makes it a number.

**The one that decides everything is the real-time factor**: processing time
divided by the audio it processed. Audio arrives at a fixed rate whatever the
CPU is doing, so at an RTF of 1.0 the loop is exactly keeping up and has no
margin; above 1.0 it is falling behind and frames are being dropped somewhere.
This is the number that will decide whether a Raspberry Pi can run Emet at all,
and it cannot be guessed from a laptop. It can be *compared*, though, which is why
it is worth recording on both.

**Frames over budget matters more than the average.** A loop averaging 20 ms
per 80 ms frame is comfortable; the same loop is not comfortable if one frame
in fifty takes 200 ms, because that frame is a dropped word. Averages hide
exactly the failure that people notice, so this counts the overruns
separately.

**Drift means two different things and only one is measurable offline.**
Processing drift, the loop failing to keep pace, shows up in the RTF and can
be measured against a file. Clock drift, where the sound card's idea of a
second and the system's slowly diverge, only appears with real hardware. This
module reports the first honestly and refuses to invent the second: for a file
source, wall-clock comparisons are meaningless and are labelled as such.
"""

from __future__ import annotations

import ipaddress
import math
import statistics
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

__all__ = ["OUTCOMES", "STAGES", "Handover", "Sent", "SessionStats", "Spread", "Stopwatch", "Tokens", "TurnRecord"]

#: How many per-frame timings to keep for percentiles. Ten minutes at 80 ms
#: frames is 7500, so this holds a whole soak run; a robot left on for a week
#: keeps the most recent hour and a half and its running totals stay exact.
SAMPLE_CAP = 8192


class Stopwatch:
    """Times a block, in milliseconds, without pretending to be precise about
    anything the operating system will not promise."""

    __slots__ = ("_start", "elapsed_ms")

    def __init__(self) -> None:
        self._start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Stopwatch":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0

    def peek_ms(self) -> float:
        """Milliseconds so far, without stopping. For a first-token mark
        inside a block that keeps running."""
        return (time.perf_counter() - self._start) * 1000.0


@dataclass(frozen=True)
class Handover:
    """One wake's handover to the transcriber, in figures.

    `--stats` prints it under the wake, so a turn whose first word came back
    odd can be read against its cut. On 2026-10-03 the reference body's
    footer counted 6 moved cuts in 26 and could not say whether "Was two
    plus two." or "Hey. What's two plus two?" was one of them.

    `lag_ms` is the lag the wake engine reported. `heard_ms` is the audio
    after the phrase that was handed over: the lag, or less when the tail
    was cut short by `MAX_WAKE_LAG_MS` or by what the loop kept. `name_ms`
    is how much of its start was taken for the end of the name and written
    as silence. `level_ms` is what the level stop alone, the rule without
    its first-difference stop, would have written; it is never applied, and
    the two differ only where the first-difference stop kept the wake
    engine's cut. A tail cut short is never moved, so both are 0 there.
    Every figure is audio, never wall time, so two replays of one recording
    print the same lines.
    """

    lag_ms: float
    heard_ms: float
    name_ms: float
    level_ms: float
    cut_short: bool = False

    def text(self) -> str:
        line = f"lag {self.lag_ms:.0f} ms, name cut {self.name_ms:.0f} ms, level stop alone {self.level_ms:.0f} ms"
        if self.cut_short:
            line += f"; the last {self.heard_ms:.0f} ms handed over as heard"
        return line


#: What a turn came to, in the order the contract lists them. `answered` is a
#: reply with words that ended well; `cut_at_cap` one with words that stopped
#: at the token cap and `empty_at_cap` one that stopped there before any;
#: `declined` the provider's refusal; `failed` a reply whose request failed;
#: `unavailable` a model that cannot answer; `out_of_credit` an unpaid
#: account; `words_lost` a final transcript that carried an error and no
#: words; `nothing_heard` an empty final with no error; `no_speech` a wake and
#: then nothing; `not_answered` a run that asked no model.
OUTCOMES = (
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
)

#: The three stages that can send something off the body, in the order the
#: per-turn line and the footer name them.
STAGES = ("stt", "llm", "voice")


def _on_this_machine(host: str) -> bool:
    """Whether a stage's host is the machine the engine runs on.

    `localhost`, a loopback address (127.0.0.0/8, `::1`) and the unspecified
    address (`0.0.0.0`, `::`), which a client reaches as this machine: a
    model server on the body, such as llama-server at `127.0.0.1:9380`. Any
    other host is off the body, an address on the home network included,
    so a doubtful host is counted as having left. `host` is a descriptor's,
    `127.0.0.1:9380` or `[::1]:8080`, the port optional.
    """
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


class Sent(NamedTuple):
    """What one stage sent to one host, as seconds of audio for the
    transcriber and as characters for the language model and the voice."""

    stage: str
    host: str
    amount: float

    def text(self) -> str:
        """The entry as the per-turn line and the footer print it."""
        if self.stage == "stt":
            return f"{self.amount:.1f} s of audio to {self.host}"
        return f"{self.amount:.0f} characters to {self.host}"


class Spread(NamedTuple):
    """The median, the mean and the largest of some turns' gaps, and how many."""

    median_ms: float
    mean_ms: float
    max_ms: float
    n: int


class Tokens(NamedTuple):
    """Tokens summed over the turns that counted any.

    A side no turn counted is None, so a vendor that reports only what it
    wrote never reads as having read nothing. `n` is how many turns counted
    either side.
    """

    input: int | None
    output: int | None
    n: int


def _whole_terms(total: float, terms: Sequence[float]) -> list[int]:
    """Whole milliseconds for each term that add up to the rounded total.

    Rounding each term alone can leave "2260 = 960 + 410 + 620 + 271", a sum
    a reader checks and finds wrong. Each term is rounded down and the
    milliseconds left over go to the terms with the largest remainders, so
    no term moves by a whole millisecond and the line's sum holds. The total
    is rounded as `:.0f` rounds it, so the gap reads as the events file has
    it. A term can therefore read 1 ms off the same figure rounded alone, as
    the events file and the footer's means print it: 620 in the line where
    the file says `"reply_first_ms": 621`. The line keeps its sum, and the
    file keeps each figure.
    """
    whole = round(total)
    floors = [math.floor(term) for term in terms]
    short = whole - sum(floors)
    by_remainder = sorted(range(len(terms)), key=lambda i: terms[i] - floors[i], reverse=True)
    for i in by_remainder[:short]:
        floors[i] += 1
    return floors


def _total(counts: Sequence[int]) -> int | None:
    """The sum of some counts, or None when there are none to sum."""
    return sum(counts) if counts else None


def _tokens_text(input_tokens: int | None, output_tokens: int | None, *, long: bool) -> str | None:
    """`967 tokens in, 18 out`, or the one side counted, or None for neither.
    The footer's form leaves out the word `tokens`, which its label holds."""
    word = " tokens" if long else ""
    if input_tokens is not None and output_tokens is not None:
        return f"{input_tokens}{word} in, {output_tokens} out"
    if input_tokens is not None:
        return f"{input_tokens}{word} in"
    if output_tokens is not None:
        return f"{output_tokens}{word} out"
    return None


@dataclass
class TurnRecord:
    """One turn in figures, from the person's last loud frame to the robot's
    first sound.

    The session makes one at each turn's endpoint, appends it to
    `SessionStats.turn_records`, and fills it in place as the turn goes: the
    wait for the final transcript, the reply's first word, the first sound at
    the sink, the tokens each reply counted, and what each stage was handed.
    `--stats` prints `line()` as the turn's last line, the footer sums the
    records, and `--events` writes each record once its turn is done, its keys
    in the order of the fields below.

    The gap is the wait a person feels. It is the quiet the endpointer
    counted before it ended the turn, the wait for the final transcript, and
    the wait from there to the first sound, whatever that sound said. TT8 asks
    for it because the footer's three latencies each start at a different
    moment, so none of them, and no sum of them, is the wait.

    A figure is None, or 0 for a count, until the stage that makes it has
    run. A host is the descriptor's, read at boot, and None for a stage that
    runs on this body; such a stage is left out of what was sent, and so is
    one whose host is this machine, such as a model server at 127.0.0.1. Every
    figure is a number or a verdict, never a word anybody said, so a record
    can be written to a file and pasted into a public pull request.
    """

    #: 1-based: `SessionStats.turns` at the endpoint.
    turn: int
    #: One of `OUTCOMES`. A value outside them raises `ValueError` where it is
    #: set, so a misspelt outcome fails the test that set it.
    outcome: str = "not_answered"
    #: Last loud frame to the endpoint, `Utterance.tail_ms`: the quiet the
    #: endpointer had counted, in whole frames. The patience on a plain turn,
    #: twice it on an extended turn whose extension ran out in silence. On a
    #: turn the transcriber's words ended, with no loud frame, it counts from
    #: the end of the click's deaf window, speech under the energy detector's
    #: threshold included.
    tail_ms: float | None = None
    #: Endpoint to the final transcript; None when no transcriber ran.
    final_ms: float | None = None
    #: Final transcript to the reply's first word; None when no model was asked.
    reply_first_ms: float | None = None
    #: The final transcript, or the endpoint when `final_ms` is None, to the
    #: turn's first sound at the sink; None when the turn made no sound.
    voice_first_ms: float | None = None
    #: Summed over every reply of the turn; None when no reply carried a count.
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Seconds of audio handed to the transcriber from the wake to the final.
    stt_audio_s: float = 0.0
    stt_host: str | None = None
    #: Characters of every prompt handed to the language model this turn,
    #: counted as each is handed to the plugin. The engine cannot see the
    #: socket, so a request that never connected counts too, and the figure
    #: errs toward saying more left the body than did.
    llm_chars: int = 0
    llm_host: str | None = None
    #: Characters of everything handed to the voice, lines and fillers too.
    voice_chars: int = 0
    voice_host: str | None = None
    #: Reply sentences handed to the voice, and how many of them cleaning
    #: changed or emptied.
    sentences: int = 0
    cleaned: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.turn, bool) or not isinstance(self.turn, int) or self.turn < 1:
            raise ValueError(f"a turn is counted from 1, and this record was given {self.turn!r}")

    def __setattr__(self, name: str, value: object) -> None:
        # The session sets the outcome after the reply, so the check is made
        # wherever it is set, construction included.
        if name == "outcome" and value not in OUTCOMES:
            raise ValueError(f"unknown turn outcome {value!r}. A turn's outcome is one of {', '.join(OUTCOMES)}.")
        object.__setattr__(self, name, value)

    @property
    def gap_ms(self) -> float | None:
        """Last loud frame to first sound, or None when either end is unknown."""
        if self.tail_ms is None or self.voice_first_ms is None:
            return None
        return self.tail_ms + (self.final_ms or 0.0) + self.voice_first_ms

    @property
    def ran_a_stage(self) -> bool:
        """Whether the transcriber, the language model or the voice ran this
        turn, on this body or off it."""
        return bool(
            self.final_ms is not None
            or self.stt_audio_s > 0
            or self.reply_first_ms is not None
            or self.llm_chars > 0
            or self.input_tokens is not None
            or self.output_tokens is not None
            or self.voice_chars > 0
            or self.sentences > 0
        )

    def sent(self) -> list[Sent]:
        """What this turn handed to a host off the body, stage by stage: a
        stage with no host, or whose host is this machine, sent nothing off it."""
        entries = [
            ("stt", self.stt_host, self.stt_audio_s),
            ("llm", self.llm_host, self.llm_chars),
            ("voice", self.voice_host, self.voice_chars),
        ]
        return [
            Sent(stage, host, amount)
            for stage, host, amount in entries
            if host is not None and amount > 0 and not _on_this_machine(host)
        ]

    def line(self) -> str | None:
        """The per-turn line under `--stats`, with no indent, or None for a
        turn that ran no stage and made no sound.

        `turn gap 2260 ms = quiet 960 + stt final 410 + llm first 620 + voice
        270; 967 tokens in, 18 out; sent 3.3 s of audio to api.deepgram.com,
        2071 characters to api.openai.com`. A term with no value is left out
        with its `+`; `voice` is the wait from the reply's first word to the
        first sound. The terms are whole milliseconds that add up to the gap
        (`_whole_terms`), so a term can read 1 ms off the events file's
        figure. The line holds no `lag` or `cut`, so the readers of the line
        under each wake pass over it.
        """
        if not self.ran_a_stage and self.voice_first_ms is None:
            return None
        parts = [self._gap_text()]
        tokens = _tokens_text(self.input_tokens, self.output_tokens, long=True)
        if tokens is not None:
            parts.append(tokens)
        sent = self.sent()
        parts.append("sent " + ", ".join(entry.text() for entry in sent) if sent else "nothing left the body")
        return "; ".join(parts)

    def _gap_text(self) -> str:
        """The line's first part, the gap and the terms it is the sum of."""
        gap = self.gap_ms
        if gap is None:
            if self.voice_first_ms is None:
                return "turn gap - (nothing was spoken)"
            # The endpointer heard nothing loud, so the gap has no start:
            # a wake, then silence, answered with the soul's line.
            return "turn gap - (no speech to count from)"
        assert self.tail_ms is not None and self.voice_first_ms is not None
        names = ["quiet"]
        terms = [self.tail_ms]
        if self.final_ms is not None:
            names.append("stt final")
            terms.append(self.final_ms)
        if self.reply_first_ms is not None:
            names.append("llm first")
            terms.append(self.reply_first_ms)
        names.append("voice")
        terms.append(self.voice_first_ms - (self.reply_first_ms or 0.0))
        shown = _whole_terms(gap, terms)
        return f"turn gap {round(gap)} ms = " + " + ".join(f"{name} {ms}" for name, ms in zip(names, shown))


@dataclass
class SessionStats:
    """What one run of the listen loop did, and whether it kept up."""

    #: Milliseconds of audio each frame represents. Set from the format so the
    #: budget is not hardcoded to one frame size.
    frame_ms: float = 80.0

    frames: int = 0
    wakes: int = 0
    turns: int = 0
    #: Turns whose silence window was extended once because the words so
    #: far looked unfinished.
    extended: int = 0
    #: Frames the source discarded: audio that was never seen at all. Not the
    #: same as being slow. `dropped_busy` below says how many of them were
    #: lost while the loop was not listening on purpose; the rest mean it
    #: fell behind.
    dropped: int = 0
    #: Callbacks PortAudio flagged with an input overflow: the sound card
    #: lost audio before the source saw it. A count of callbacks, not of
    #: frames, since PortAudio does not say how much each one lost. A
    #: different cause from `dropped`, the same kind of loss, and the first
    #: thing to check when a live run's clock skew looks like drift.
    overflows: int = 0
    #: Of `dropped`, the frames lost while the loop was deliberately not
    #: reading: playing audio, waiting on a language model, or waiting for
    #: the final transcript after the person stopped. Those are the loop's
    #: own doing until barge-in arrives, and they say nothing about whether
    #: it keeps up while listening, so the verdict leaves them out.
    dropped_busy: int = 0

    process_ms_total: float = 0.0
    process_ms_max: float = 0.0
    over_budget: int = 0

    #: Milliseconds from the endpoint to the final transcript, one per
    #: transcribed turn. The first number a person feels in 0.4: the robot
    #: cannot start thinking until this has elapsed, so it is measured on
    #: every turn rather than guessed from a vendor's page.
    final_ms: list[float] = field(default_factory=list)

    #: Per wake whose words said with the name went to the transcriber: how
    #: much of the start of that audio was taken for the end of the name and
    #: written as silence, in milliseconds, 0 when the cut stayed where the
    #: wake engine put it. The footer counts these; `--stats` prints the
    #: whole record of each under its wake, from the session's `on_handover`.
    name_cut_ms: list[float] = field(default_factory=list)

    #: Per answered turn: milliseconds from the final transcript to the first
    #: word of the reply, and to the whole reply. The first is what a person
    #: waits in silence; the second is what a speaker would need to keep up.
    reply_first_ms: list[float] = field(default_factory=list)
    reply_done_ms: list[float] = field(default_factory=list)

    #: Per spoken reply: milliseconds from the final transcript to the first
    #: sound at the sink, and to the last. The first is the silence a person
    #: actually sits through, measured where they hear it rather than where
    #: the model produced it; the second is how long the robot held the
    #: floor. Sentences the voice managed and lost are counted beside them.
    speech_first_ms: list[float] = field(default_factory=list)
    speech_done_ms: list[float] = field(default_factory=list)
    sentences_spoken: int = 0
    sentences_lost: int = 0

    #: One `TurnRecord` per turn, in order, appended at each endpoint and
    #: filled as the turn goes. The footer's turn gap, tokens, sent and
    #: cleaned lines are their sums.
    turn_records: list[TurnRecord] = field(default_factory=list)

    _samples: deque[float] = field(default_factory=lambda: deque(maxlen=SAMPLE_CAP))
    #: Set by the first frame, so that loading the acoustic model and opening
    #: the card are left out. What remains is the card's clock against the
    #: system's, which is what drift means.
    _started: float | None = None

    # ----------------------------------------------------------- recording

    def record_frame(self, process_ms: float) -> None:
        if self._started is None:
            self._started = time.perf_counter()
        self.frames += 1
        self.process_ms_total += process_ms
        self.process_ms_max = max(self.process_ms_max, process_ms)
        if process_ms > self.frame_ms:
            self.over_budget += 1
        self._samples.append(process_ms)

    def record_final(self, wait_ms: float) -> None:
        self.final_ms.append(wait_ms)

    def record_handover(self, handover: Handover) -> None:
        self.name_cut_ms.append(handover.name_ms)

    def record_reply(self, first_ms: float | None, done_ms: float) -> None:
        if first_ms is not None:
            self.reply_first_ms.append(first_ms)
        self.reply_done_ms.append(done_ms)

    def record_speech(self, first_ms: float | None, done_ms: float, *, spoken: int, lost: int) -> None:
        if first_ms is not None:
            self.speech_first_ms.append(first_ms)
        self.speech_done_ms.append(done_ms)
        self.sentences_spoken += spoken
        self.sentences_lost += lost

    def record_turn(self, record: TurnRecord) -> None:
        self.turn_records.append(record)

    # ------------------------------------------------------- the turns' sums

    def gap_spread(self) -> Spread | None:
        """The turn gap over every turn that has one, or None when none does."""
        gaps = [gap for gap in (record.gap_ms for record in self.turn_records) if gap is not None]
        if not gaps:
            return None
        return Spread(statistics.median(gaps), sum(gaps) / len(gaps), max(gaps), len(gaps))

    def token_totals(self) -> Tokens | None:
        """The tokens every turn's replies counted, or None when none counted any."""
        counted = [r for r in self.turn_records if r.input_tokens is not None or r.output_tokens is not None]
        if not counted:
            return None
        return Tokens(
            _total([r.input_tokens for r in counted if r.input_tokens is not None]),
            _total([r.output_tokens for r in counted if r.output_tokens is not None]),
            len(counted),
        )

    def sent_totals(self) -> list[Sent]:
        """What the run sent off the body, summed per stage and host, the
        stages in `STAGES` order and each stage's hosts as they first came."""
        totals: dict[tuple[str, str], float] = {}
        for record in self.turn_records:
            for entry in record.sent():
                key = (entry.stage, entry.host)
                totals[key] = totals.get(key, 0) + entry.amount
        ordered = sorted(totals.items(), key=lambda item: STAGES.index(item[0][0]))
        return [Sent(stage, host, amount) for (stage, host), amount in ordered]

    @property
    def ran_a_stage(self) -> bool:
        """Whether any turn ran the transcriber, the model or the voice."""
        return any(record.ran_a_stage for record in self.turn_records)

    def cleaned_totals(self) -> tuple[int, int] | None:
        """How many reply sentences cleaning changed, of how many reached the
        voice, or None when none reached it."""
        of = sum(record.sentences for record in self.turn_records)
        if not of:
            return None
        return sum(record.cleaned for record in self.turn_records), of

    def turn_lines(self) -> list[str]:
        """The footer's four lines from the turns' records, in its label
        column, after `voice done` and before `clock`.

        Each prints only when it has something to say, so a run that ran no
        provider stage prints the footer it printed before the records
        existed. None of them starts with `name cut`, which is how the
        readers outside the repository tell the footer's line from the line
        under each wake.
        """
        lines: list[str] = []
        spread = self.gap_spread()
        if spread is not None:
            lines.append(
                f"  turn gap      median {spread.median_ms:.0f} ms   mean {spread.mean_ms:.0f}   "
                f"max {spread.max_ms:.0f}   ({spread.n} turn(s), last loud frame to first sound)"
            )
        tokens = self.token_totals()
        if tokens is not None:
            counted = _tokens_text(tokens.input, tokens.output, long=False)
            lines.append(
                # A count of turns: a turn's tokens are summed over every
                # reply it asked for, which a tool round makes more than one.
                f"  tokens        {counted}   ({tokens.n} turn(s) that counted them; "
                f"cached tokens are counted from 0.6)"
            )
        if self.ran_a_stage:
            sent = self.sent_totals()
            lines.append(
                f"  sent          {', '.join(entry.text() for entry in sent)}   (what the turns sent off the body)"
                if sent else "  sent          nothing left the body"
            )
        cleaned = self.cleaned_totals()
        if cleaned is not None:
            lines.append(f"  cleaned       {cleaned[0]} of {cleaned[1]} sentence(s) changed before the voice")
        return lines

    # ------------------------------------------------------------ readings

    @property
    def audio_ms(self) -> float:
        """How much audio was seen. The denominator for everything below."""
        return self.frames * self.frame_ms

    @property
    def wall_ms(self) -> float:
        """Time since the first frame's audio began. Zero before any frame.

        The clock starts when the first frame is handed over, which is after
        its 80 ms was captured, while `audio_ms` counts that frame whole. Add
        the frame back, or the wall clock runs one frame behind the audio
        clock for the whole run and the skew is understated by exactly that.
        The reference body printed "skew +35.54s, of which 35.60s is the
        dropped frames" on 2026-09-20: a part larger than the whole, which
        reads as a broken instrument. Model-loading time before the first
        frame is still left out, which is what this clock is for.
        """
        if self._started is None:
            return 0.0
        return (time.perf_counter() - self._started) * 1000.0 + self.frame_ms

    @property
    def realtime_factor(self) -> float:
        """Processing time over audio duration. Below 1.0 keeps up.

        Deliberately excludes time spent waiting for audio to arrive: on a live
        microphone that wait is most of the wall clock and is not work. Timing
        it would flatter every measurement to about 1.0 and hide the thing this
        number exists to reveal.
        """
        if not self.audio_ms:
            return 0.0
        return self.process_ms_total / self.audio_ms

    @property
    def headroom(self) -> float:
        """How many times faster than real time. 12x is comfortable, 1x is not."""
        rtf = self.realtime_factor
        return (1.0 / rtf) if rtf else float("inf")

    @property
    def mean_ms(self) -> float:
        return self.process_ms_total / self.frames if self.frames else 0.0

    def percentile(self, p: float) -> float:
        """Nearest-rank over the retained samples. `p` in 0..100."""
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        i = min(len(ordered) - 1, max(0, round(p / 100.0 * len(ordered)) - 1))
        return ordered[i]

    @property
    def kept_up(self) -> bool:
        """Whether this run is evidence the loop is viable here.

        Three conditions, and all of them matter. Nothing was dropped while
        listening, no frame blew the budget, and there is real margin rather
        than a bare pass. A run at 0.99 kept up on a quiet machine and will not
        on a busy one. Frames dropped while the robot was speaking or thinking
        are counted, printed, and left out of this: the loop chose not to read
        the microphone then, and barge-in is what changes that.
        """
        return (
            self.dropped - self.dropped_busy == 0
            and self.over_budget == 0
            and self.realtime_factor < 0.5
        )

    # -------------------------------------------------------------- report

    def report(self, *, live: bool, board: Sequence[str] = ()) -> str:
        """A human-readable summary.

        `live` says whether the audio came from hardware, because it changes
        what can honestly be claimed: clock drift is only meaningful against a
        real sound card, and printing a wall-clock comparison for a file would
        be inventing a measurement.

        `board` is the board's lines from `emet_engine.board.footer_lines`,
        printed above the verdict: a throttled board or a busy one is the
        first explanation for a run that did not keep up, and the verdict
        stays the report's last line, where `body_log_check.py` ends its
        reading of a footer. What the state file kept and the notes print
        after the report.
        """
        lines = [
            f"  frames        {self.frames}  ({self.audio_ms / 1000:.1f}s of audio)",
            f"  wakes         {self.wakes}   turns {self.turns}"
            + (f"   extended {self.extended}" if self.extended else ""),
            f"  per frame     mean {self.mean_ms:.2f} ms   "
            f"p50 {self.percentile(50):.2f}   p95 {self.percentile(95):.2f}   "
            f"max {self.process_ms_max:.2f}",
            f"  budget        {self.frame_ms:.0f} ms/frame, "
            f"{self.over_budget} frame(s) over",
            f"  realtime      {self.realtime_factor:.4f}  "
            f"({self.headroom:.0f}x faster than realtime)",
            f"  dropped       {self.dropped}"
            + (f"   ({self.dropped_busy} while speaking, thinking or waiting for the words)"
               if self.dropped_busy else ""),
        ]
        if live:
            # A file has no card to overflow, so the line is a live run's.
            lines.append(
                f"  overflows     {self.overflows} callback(s) PortAudio flagged as an input "
                f"overflow; how much each lost is not reported"
            )
        if self.final_ms:
            lines.append(
                f"  stt final     mean {sum(self.final_ms) / len(self.final_ms):.0f} ms   "
                f"max {max(self.final_ms):.0f}   ({len(self.final_ms)} turn(s), "
                f"endpoint to final transcript)"
            )
        if self.name_cut_ms:
            moved = [ms for ms in self.name_cut_ms if ms > 0]
            detail = f"   mean {sum(moved) / len(moved):.0f} ms   max {max(moved):.0f}" if moved else ""
            lines.append(
                f"  name cut      {len(moved)} of {len(self.name_cut_ms)} handover(s){detail}   "
                f"(the end of the name written as silence)"
            )
        if self.reply_first_ms:
            lines.append(
                f"  llm first     mean {sum(self.reply_first_ms) / len(self.reply_first_ms):.0f} ms   "
                f"max {max(self.reply_first_ms):.0f}   ({len(self.reply_first_ms)} reply(ies), "
                f"transcript to first word)"
            )
        if self.reply_done_ms:
            lines.append(
                f"  llm done      mean {sum(self.reply_done_ms) / len(self.reply_done_ms):.0f} ms   "
                f"max {max(self.reply_done_ms):.0f}   (transcript to whole reply)"
            )
        if self.speech_first_ms:
            lines.append(
                f"  voice first   mean {sum(self.speech_first_ms) / len(self.speech_first_ms):.0f} ms   "
                f"max {max(self.speech_first_ms):.0f}   ({len(self.speech_first_ms)} reply(ies), "
                f"transcript to first sound)"
            )
        if self.speech_done_ms:
            lost = f", {self.sentences_lost} lost" if self.sentences_lost else ""
            lines.append(
                f"  voice done    mean {sum(self.speech_done_ms) / len(self.speech_done_ms):.0f} ms   "
                f"max {max(self.speech_done_ms):.0f}   (transcript to last sound; "
                f"{self.sentences_spoken} sentence(s) spoken{lost})"
            )
        lines.extend(self.turn_lines())
        if live:
            skew = self.wall_ms - self.audio_ms
            pct = (skew / self.audio_ms * 100.0) if self.audio_ms else 0.0
            # A steady percentage across runs is the card's clock against the
            # system's, a property of the hardware. A growing offset with
            # overflows or drops beside it is audio being lost, and every
            # dropped frame is one frame's worth of skew: on the reference
            # body 141 dropped frames read as an 18.9% "drift" until the
            # arithmetic was done by hand. So it is done here.
            lost_ms = self.dropped * self.frame_ms
            explained = ""
            if self.dropped:
                # What a person wants is the skew left after the dropped
                # frames are taken out, and it was being worked out by hand
                # from two figures on the same line. Print it.
                rest = skew - lost_ms
                rest_pct = (rest / self.audio_ms * 100.0) if self.audio_ms else 0.0
                explained = (
                    f"; {lost_ms / 1000:.2f}s of it is the {self.dropped} dropped "
                    f"frame(s), leaving {rest_pct:+.2f}%"
                )
            lines.append(
                f"  clock         wall {self.wall_ms / 1000:.1f}s vs audio "
                f"{self.audio_ms / 1000:.1f}s   skew {skew / 1000:+.2f}s ({pct:+.2f}%{explained})"
            )
        else:
            lines.append(
                "  clock         not measured: a file has no sound card, so "
                "wall time says nothing about drift"
            )
        lines.extend(board)
        lines.append(f"  verdict       {'kept up' if self.kept_up else 'DID NOT KEEP UP'}")
        return "\n".join(lines)
