"""What the language model is told, and what it remembers of the conversation.

The seed of `DESIGN.md` section 7. Today the system prompt is the soul's own
persona and a rule about speaking aloud. The self-model (0.5) and the
memories the sensitivity floor lets through (0.6) join it here, in this file,
and nothing below this line changes when they do: a `Prompt` is what leaves,
whatever went into it.

The conversation is short on purpose. A robot on a desk is spoken to in
bursts, and a language model is billed on everything it is shown, so only
the most recent turns ride along. Memory, the long kind, is a different
mechanism with its own release.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from emet_sdk.types import Message, Prompt, ToolSpec

__all__ = ["SPOKEN_ALOUD", "DEFAULT_MAX_TOKENS", "DEFAULT_TURNS", "system_prompt", "Conversation"]

#: Appended to every system prompt. A reply is heard, so it has to be
#: listenable: short, plain, said rather than laid out. Honesty about limits
#: is principle 6, and it belongs in the prompt before the self-model arrives
#: to make it specific.
SPOKEN_ALOUD = (
    "You are speaking aloud through a small robot's speaker. Answer in one or "
    "two short sentences a person can listen to. Plain speech: no lists, no "
    "markdown, no emoji, no stage directions. If you cannot do something or do "
    "not know, say so plainly."
)

#: One spoken reply, bounded. Two sentences are well under this; the cap is
#: there so a model that ignores the rule is cut off rather than read a page.
DEFAULT_MAX_TOKENS = 300

#: How many recent messages travel with each request. Twelve turns each way.
DEFAULT_TURNS = 24


def system_prompt(soul: Mapping[str, Any]) -> str:
    """The soul's persona, then the rule about speaking aloud.

    `persona.system_prompt` is used when the soul wrote one; otherwise the
    prompt is built from `identity.name` and `persona.summary`, which every
    soul has. The soul names no hardware here and neither does this: the body
    enters the prompt in 0.5, through the self-model.
    """
    identity = soul.get("identity") or {}
    persona = soul.get("persona") or {}
    written = str(persona.get("system_prompt") or "").strip()
    if not written:
        name = str(identity.get("name") or "Emet").strip()
        summary = " ".join(str(persona.get("summary") or "").split())
        written = f"You are {name}." + (f" {summary}" if summary else "")
    return f"{written}\n\n{SPOKEN_ALOUD}"


class Conversation:
    """The recent turns, as the model will be shown them.

    `add_user` and `add_assistant` append; `prompt` packages the tail with a
    system prompt. Trimming drops the oldest turns in pairs so the history
    still begins with the person speaking, which some providers require.
    """

    def __init__(self, *, turns: int = DEFAULT_TURNS) -> None:
        self.turns = max(2, turns)
        self.messages: list[Message] = []

    def add_user(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))
        self._trim()

    def add_assistant(self, text: str) -> None:
        self.messages.append(Message(role="assistant", content=text))
        self._trim()

    def _trim(self) -> None:
        while len(self.messages) > self.turns:
            del self.messages[:2]
        while self.messages and self.messages[0].role != "user":
            del self.messages[0]

    def prompt(
        self,
        system: str,
        *,
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Prompt:
        return Prompt(system=system, messages=tuple(self.messages), tools=tuple(tools), max_tokens=max_tokens)
