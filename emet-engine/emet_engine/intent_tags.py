"""How the soul's intents leave the language model: inline tags in the text.

The decision the deferred register held for the loop batch, and the reason
it went this way. A language model has two channels back to the engine: the
text it streams, and tool calls. A tool call ends the text (the model stops
with `tool` and waits for a result), so an expressive intent that should
land on a sentence, a tilt of the head as the surprising word is said,
cannot travel as a call without stopping the speech it belongs to. A tag in
the text stream arrives with the words around it, at the place in the
sentence the model put it, and costs nothing to the flow of speech. So:

* **Expressive intents are inline tags**: `[express.curiosity]`,
  `[attend.speaker]`, the names from `emet_sdk.intents`, in square brackets,
  anywhere in the reply. This module lifts them out of the stream before
  the words reach the voice, so a tag is never spoken.
* **Side effects are tool calls**: a memory written with its sensitivity, a
  fact looked up. Those need a result before the model goes on, which is
  what a tool round trip is for.

**What is stripped, and what is reported.** Every bracketed token with no
whitespace inside it is removed from the spoken text, because the prompt
forbids stage directions and models write `[laughs]` anyway, and reading
"laughs" aloud is worse than dropping it. Only tokens that name an intent in
the closed vocabulary can be reported as intents; the rest are logged and
lost. A tag opens at the last `[` before its `]`, so a stray bracket earlier
in the reply stays text and the tag after it is still lifted out.

**Some of the vocabulary is not the model's to use.** Two groups of valid
names are lifted out, logged at debug level and dropped, and never reported:

* Reserved kinds (`manipulate`, `navigate`, `gesture`, `attend_joint`).
  DESIGN 5.2: a soul may emit them and the engine drops them until the
  release that gives them a behaviour, so a soul written for a later body
  loads today and is merely ineffective.
* Engine-only kinds. `signal` is system state. The vocabulary keeps it
  apart from `express` so a soul cannot suppress it: a muted robot must look
  muted. The same holds the other way round: a model that could write
  `[signal.offline]` or `[signal.muted]` could make the robot show a state
  it is not in. `speak` is the text itself; a tag that asks for speech has
  nothing to add to the words around it.

`ignored` keeps the names dropped this way, beside `removed`, so a test or a
log can tell a rule from a typo.

**Streaming.** A tag can be split across deltas (`[express.` then
`curiosity]`), so the filter holds an unclosed bracket back until it closes
or turns out to be ordinary text (whitespace inside, or too long), and
`flush()` releases whatever it still holds when the reply ends.

**Keeping the tag's place.** `pieces()` returns a delta as the text and the
intents in the order the model wrote them, so the engine can act on a tag
where it stood: a filler lands before the sentence its tag opened, and a
head tilts as soon as the model writes the tag, ahead of the words after
it. `feed()` is the same filter for a caller
that only wants the words and a list of what was asked for.

**What happens to a reported intent** is the engine's business: it acts on
each one through the binding the body resolved at boot, which may be a
filler in the reply's voice, a head tilt, or a light.
"""

from __future__ import annotations

import logging
import re

from emet_sdk import intents as _intents
from emet_sdk.types import Intent

__all__ = ["ENGINE_ONLY_KINDS", "IntentTags", "MAX_TAG_CHARS", "intent_from_tag"]

log = logging.getLogger("emet_engine.intent_tags")

#: A bracket group longer than this is prose, not a tag, and is released
#: unchanged rather than held back waiting for a `]`.
MAX_TAG_CHARS = 40

#: Kinds in the vocabulary that only the engine raises. A model's tag naming
#: one is lifted out and dropped. The module docstring says why for each.
ENGINE_ONLY_KINDS = frozenset({"signal", "speak"})

_TAG = re.compile(r"^[A-Za-z_]+(?:\.[A-Za-z_]+)?$")

#: A held bracket that could only have become a tag: letters, underscores and
#: at most one dot after the `[`, and at least one letter.
_TAG_START = re.compile(r"^\[[A-Za-z_]+(?:\.[A-Za-z_]*)?$")


def intent_from_tag(name: str) -> Intent | None:
    """The intent a tag names, or None when it names nothing in the vocabulary."""
    kind, argument = _intents.parse(name.lower())
    try:
        return _intents.make(kind, argument)
    except _intents.UnknownIntentError:
        return None


class IntentTags:
    """Lift `[kind.argument]` tags out of a stream of text deltas."""

    def __init__(self) -> None:
        self._held = ""
        #: The last character passed through, so a space after a removed tag
        #: can be dropped when one already came before it, whichever delta
        #: each arrived in.
        self._last = ""
        self._collapse = False
        #: Every bracketed token removed since construction, tag or not.
        self.removed: list[str] = []
        #: The tokens, as written, that named a valid intent the model may not
        #: raise: reserved kinds and engine-only kinds. A subset of `removed`.
        self.ignored: list[str] = []

    def _pass(self, piece: str) -> str:
        """Let text through, closing the gap a removed tag left behind."""
        if not piece:
            return piece
        if self._collapse and piece[:1] == " " and (self._last == "" or self._last.isspace()):
            piece = piece[1:]
        if piece:
            self._collapse = False
            self._last = piece[-1]
        return piece

    def _judge(self, token: str) -> Intent | None:
        """The intent a removed token reports, or None when it reports nothing."""
        intent = intent_from_tag(token) if _TAG.match(token) else None
        if intent is None:
            log.debug("intent tags: dropped %r, which names no intent", token)
            return None
        if _intents.is_reserved(intent.kind):
            self.ignored.append(token)
            log.debug("intent tags: dropped %r, a reserved intent with no behaviour yet", token)
            return None
        if intent.kind in ENGINE_ONLY_KINDS:
            self.ignored.append(token)
            log.debug("intent tags: dropped %r, which only the engine may raise", token)
            return None
        return intent

    def pieces(self, delta: str) -> list[str | Intent]:
        """The delta with its tags lifted, as text and intents in the order the
        model wrote them.

        Text pieces are never empty, and within one call two never sit side
        by side: text split only by a dropped token comes back joined. An
        intent sits where its tag stood, so the text before it was written
        before it.
        """
        text = self._held + delta
        self._held = ""
        out: list[str | Intent] = []

        def emit(piece: str) -> None:
            piece = self._pass(piece)
            if not piece:
                return
            if out and isinstance(out[-1], str):
                out[-1] += piece
            else:
                out.append(piece)

        pos = 0
        while True:
            i = text.find("[", pos)
            if i == -1:
                emit(text[pos:])
                break
            j = text.find("]", i)
            if j == -1:
                # Only the last `[` can still become a tag.
                i = text.rfind("[", i)
                tail = text[i:]
                if len(tail) <= MAX_TAG_CHARS and not any(c.isspace() for c in tail):
                    # Might still become a tag when the next delta arrives.
                    emit(text[pos:i])
                    self._held = tail
                else:
                    emit(text[pos:])
                break
            # A tag opens at the last `[` before its `]`, so a stray bracket
            # earlier on is text and cannot carry the tag into the speech.
            i = text.rfind("[", i, j)
            token = text[i + 1 : j]
            if not token or any(c.isspace() for c in token) or len(token) > MAX_TAG_CHARS:
                # Ordinary bracketed prose. Keep it, brackets and all.
                emit(text[pos : j + 1])
                pos = j + 1
                continue
            emit(text[pos:i])
            self.removed.append(token)
            intent = self._judge(token)
            if intent is not None:
                out.append(intent)
            pos = j + 1
            self._collapse = True
        return out

    def feed(self, delta: str) -> tuple[str, list[Intent]]:
        """The delta with its tags removed, and the intents those tags named."""
        text: list[str] = []
        found: list[Intent] = []
        for piece in self.pieces(delta):
            if isinstance(piece, str):
                text.append(piece)
            else:
                found.append(piece)
        return "".join(text), found

    def flush(self) -> str:
        """Whatever an unclosed bracket was holding back, when the reply ends.

        A held bracket that can only be the start of a tag (`[express.del`,
        a reply cut off at its token cap) is dropped, like the whole tag it
        would have been, rather than read out as "express dot del". Anything
        else held, `[4b` in "see section [4b", is released as the text it is.
        """
        held, self._held = self._held, ""
        if _TAG_START.match(held):
            self.removed.append(held[1:])
            log.debug("intent tags: dropped %r, a tag the reply ended inside", held)
            return ""
        return self._pass(held)
