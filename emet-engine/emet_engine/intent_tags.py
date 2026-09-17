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
the closed vocabulary are reported as intents; the rest are logged and lost.

**Streaming.** A tag can be split across deltas (`[express.` then
`curiosity]`), so the filter holds an unclosed bracket back until it closes
or turns out to be ordinary text (whitespace inside, or too long), and
`flush()` releases whatever it still holds when the reply ends.

**What happens to a reported intent** is 0.5's question, when the self-model
and the chains give a body something to do with it. Here the engine collects
them per reply and hands them to a callback; nothing moves yet.
"""

from __future__ import annotations

import logging
import re

from emet_sdk import intents as _intents
from emet_sdk.types import Intent

__all__ = ["IntentTags", "MAX_TAG_CHARS", "intent_from_tag"]

log = logging.getLogger("emet_engine.intent_tags")

#: A bracket group longer than this is prose, not a tag, and is released
#: unchanged rather than held back waiting for a `]`.
MAX_TAG_CHARS = 40

_TAG = re.compile(r"^[A-Za-z_]+(?:\.[A-Za-z_]+)?$")


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

    def feed(self, delta: str) -> tuple[str, list[Intent]]:
        """The delta with its tags removed, and the intents those tags named."""
        text = self._held + delta
        self._held = ""
        out: list[str] = []
        found: list[Intent] = []
        pos = 0
        while True:
            i = text.find("[", pos)
            if i == -1:
                out.append(self._pass(text[pos:]))
                break
            j = text.find("]", i)
            if j == -1:
                tail = text[i:]
                if len(tail) <= MAX_TAG_CHARS and not any(c.isspace() for c in tail):
                    # Might still become a tag when the next delta arrives.
                    out.append(self._pass(text[pos:i]))
                    self._held = tail
                else:
                    out.append(self._pass(text[pos:]))
                break
            token = text[i + 1 : j]
            if not token or any(c.isspace() for c in token) or len(token) > MAX_TAG_CHARS:
                # Ordinary bracketed prose. Keep it, brackets and all.
                out.append(self._pass(text[pos : j + 1]))
                pos = j + 1
                continue
            out.append(self._pass(text[pos:i]))
            self.removed.append(token)
            intent = intent_from_tag(token) if _TAG.match(token) else None
            if intent is not None:
                found.append(intent)
            else:
                log.debug("intent tags: dropped %r, which names no intent", token)
            pos = j + 1
            self._collapse = True
        return "".join(out), found

    def flush(self) -> str:
        """Whatever an unclosed bracket was holding back, when the reply ends."""
        held, self._held = self._held, ""
        return self._pass(held)
