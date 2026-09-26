"""What the language model is told, and what it remembers of the conversation.

The system prompt is assembled here, in four parts and in this order: the
soul's persona; the self-model, which is the body told plainly, presences and
absences both (`DESIGN.md` section 7); the tags this body can act on; and a
rule about speaking aloud. The memories the sensitivity floor lets through
join it here in 0.6, and nothing below this line changes when they do: a
`Prompt` is what leaves, whatever went into it.

**The body enters the prompt only through the self-model.** The soul names no
hardware (principle 1), the language model plugin knows nothing about the
robot it speaks for, and the persona is the same on every body. What differs
between a speakerphone and a treaded scout is the paragraph the engine
compiles from the manifest at boot, and the list of tags the scout's head and
treads can perform and the speakerphone's voice can only answer with a sound.

**Which tags are offered.** Expressive intents leave the model as inline tags
(`[express.curiosity]`), decided in 0.4 and asked for from 0.5, now that a
body acts on them. The guide offers a tag only when it would do something on
this body: every `express` tag, because its chain always ends in a filler in
the reply's voice; `attend` and `move` tags only where a part performs them.
A speakerphone asked to come here reads in its self-model that it has no
wheels, and is not handed a `[move.approach]` it could only explain away.

The conversation is short on purpose. A robot on a desk is spoken to in
bursts, and a language model is billed on everything it is shown, so only
the most recent turns ride along. Memory, the long kind, is a different
mechanism with its own release.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from emet_sdk import intents as _intents
from emet_sdk.resolve import BindingTable
from emet_sdk.types import Message, Prompt, SelfModel, ToolSpec

__all__ = [
    "SPOKEN_ALOUD",
    "SELF_MODEL_HEADING",
    "SELF_MODEL_PREFACE",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TURNS",
    "DEFAULT_LINES",
    "system_prompt",
    "body_section",
    "offered_tags",
    "tag_guide",
    "persona_lines",
    "Conversation",
]

#: Appended to every system prompt. A reply is heard, so it has to be
#: listenable: short, plain, said rather than laid out. The body itself is
#: the self-model's to describe, so this names none of it.
SPOKEN_ALOUD = (
    "You are speaking aloud. Answer in one or two short sentences a person "
    "can listen to. Plain speech: no lists, no markdown, no emoji, no stage "
    "directions. If you cannot do something or do not know, say so plainly."
)

#: The self-model's heading and first line, before the facts. The heading is
#: the one `DESIGN.md` section 7 shows.
SELF_MODEL_HEADING = "YOUR BODY"
SELF_MODEL_PREFACE = (
    "This is the body you are in right now, and all of it is true. Never "
    "offer to do anything it cannot do."
)

#: What a tag does, where the name alone does not say. `express` tags need
#: no gloss.
_TAG_GLOSS: Mapping[str, str] = {
    "attend.speaker": "look at whoever is speaking",
    "attend.none": "look ahead again",
    "move.approach": "come closer",
    "move.retreat": "back away",
    "move.turn_to": "turn toward the person",
    "move.wander": "wander about",
    "move.stop": "stop moving",
}

#: Kinds the engine emits and the model does not: system state, speech
#: itself, the idle loop, and the backchannel, which is the micro-model's.
_NOT_OFFERED = frozenset({"signal", "speak", "idle", "acknowledge"})

#: Voice actions that would make a tag pointless to offer: nothing happens,
#: or the body explains that it cannot, which the self-model already says.
_EMPTY_RUNGS = frozenset({"silence", "explain", "backchannel"})

#: One spoken reply, bounded. Two sentences are well under this; the cap is
#: there so a model that ignores the rule is cut off rather than read a page.
DEFAULT_MAX_TOKENS = 300

#: How many recent messages travel with each request. Twelve turns each way.
DEFAULT_TURNS = 24

#: What the robot says when the model does not. `declined`: the provider
#: refused the request; `failed`: the network or the provider broke mid-turn;
#: `nothing_heard`: the name was heard and no words followed, where None
#: means stay quiet. A soul overrides any of them under `persona.lines`, in
#: its own voice: the design says the robot fails loudly and in character,
#: and these are the words.
DEFAULT_LINES: Mapping[str, str | None] = {
    "declined": "I would rather not answer that.",
    "failed": "I lost my train of thought. Ask me again.",
    "nothing_heard": None,
}


def persona(soul: Mapping[str, Any]) -> str:
    """`persona.system_prompt` when the soul wrote one; otherwise one built
    from `identity.name` and `persona.summary`, which every soul has."""
    identity = soul.get("identity") or {}
    written_persona = soul.get("persona") or {}
    written = str(written_persona.get("system_prompt") or "").strip()
    if not written:
        name = str(identity.get("name") or "Emet").strip()
        summary = " ".join(str(written_persona.get("summary") or "").split())
        written = f"You are {name}." + (f" {summary}" if summary else "")
    return written


def body_section(self_model: SelfModel) -> str:
    """The self-model as the prompt carries it: heading, preface, a fact a line.

    One block with no blank line inside it, so the prompt's four parts stay
    four paragraphs."""
    return "\n".join([SELF_MODEL_HEADING, SELF_MODEL_PREFACE, self_model.text])


def offered_tags(table: BindingTable) -> list[str]:
    """The intents worth offering the model as tags on this body, in the
    order the guide lists them: `express`, then `attend`, then `move`.

    Offered when the binding would do something: a part performs it, or the
    voice answers with a filler or the words. Left out: the engine's own
    kinds, reserved kinds, intents that need a target a tag cannot carry
    (`attend.person`, `attend.bearing`), and anything bound to silence, to
    `explain` or to the backchannel placeholder.
    """
    rank = {"express": 0, "attend": 1, "move": 2}
    offered: list[tuple[int, str]] = []
    for name in table:
        kind, argument = _intents.parse(name)
        if kind in _NOT_OFFERED or _intents.is_reserved(kind) or kind not in rank:
            continue
        if name in ("attend.person", "attend.bearing"):
            continue
        binding = table[name]
        if binding.is_voice and binding.action in _EMPTY_RUNGS:
            continue
        offered.append((rank[kind], name))
    return [name for _, name in sorted(offered)]


def tag_guide(tags: Sequence[str]) -> str:
    """How to ask the body to act, for the tags `offered_tags` chose."""
    listed = []
    for tag in tags:
        gloss = _TAG_GLOSS.get(tag)
        listed.append(f"[{tag}]" + (f" to {gloss}" if gloss else ""))
    return (
        "Your body can show what you feel. Write one of these tags in square "
        "brackets at the start of the sentence it belongs to: "
        + ", ".join(listed)
        + ". A tag is never read aloud: your body does it however it can. Use "
        "them sparingly, at most one in a reply, and only when you mean it."
    )


def system_prompt(
    soul: Mapping[str, Any],
    *,
    self_model: SelfModel | None = None,
    tags: Sequence[str] = (),
) -> str:
    """The persona, the body, the tags, and the rule about speaking aloud.

    Without a self-model (a caller that has not booted a body) the prompt is
    the persona and the rule. The rule names no speaker since 0.5: the
    self-model describes the body.
    """
    parts = [persona(soul)]
    if self_model is not None and self_model.facts:
        parts.append(body_section(self_model))
    if tags:
        parts.append(tag_guide(tags))
    parts.append(SPOKEN_ALOUD)
    return "\n\n".join(parts)


def persona_lines(soul: Mapping[str, Any]) -> dict[str, str | None]:
    """The soul's own words for the moments a model has none, over the defaults.

    A key set to null in the soul means silence for that moment, which is
    how a soul turns `nothing_heard` off after turning it on.
    """
    lines: dict[str, str | None] = dict(DEFAULT_LINES)
    given = (soul.get("persona") or {}).get("lines") or {}
    for key in lines:
        if key in given:
            value = given[key]
            lines[key] = str(value).strip() or None if value is not None else None
    return lines


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
