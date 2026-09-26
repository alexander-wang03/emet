"""The self-model: what the robot is told about its own body.

`DESIGN.md` section 7. At boot the manifest, and what the started plugins
reported about themselves, are compiled into a few plain sentences in the
second person, and those go into the system prompt. The same compilation
produces what the `explain` voice rung says, in the first person, when the
body is asked for something it cannot do. One function writes both, from the
same inputs, because section 6.1 requires that the two never disagree: a
robot whose prompt says it has no wheels and whose chain says "my wheels are
not working" has told its owner two different things about one body.

**A deterministic template.** The sentences are fixed strings chosen by the
manifest's fields, and no model is asked. The same manifest and the same
reports give the same text, byte for byte, which is what lets a test assert
what the robot believes and lets a builder read it on a laptop. A language
model asked to describe a body would describe a plausible one.

**Absences are facts too.** "You cannot see: you have no camera" does more
work than any presence: it stops the robot offering to look at something.
Every part the robot might be asked about gets a sentence either way, and
`BodyFact.present` is False for the ones that say what it cannot do.

**Reported, then declared.** A part's health comes from its descriptor, which
the plugin reported after `start()`; the manifest supplies what a descriptor
does not carry (a camera's mount, a drive's kinematics and limits). A part
the manifest declares and no plugin reported on is treated as not working,
the side of caution. With no reports at all, on a laptop, the manifest alone
is compiled and every declared part is assumed to work.

**Plain physical language.** Parts are named by what they are and where they
sit ("your head", "your treads", "a four-microphone array"), never by a
component or product name and never by the capability ids a builder chose,
which exist for the manifest's own cross-references. Driver plugin names are
never read.

**What the drive plugin reports wins.** A locomotion plugin reports the speed
and turning rate it can actually deliver, and the tracked plugin already
reduces its turning rate for scrub. The compiler states those numbers and
never a faster one from the manifest's limits. A drive with no report gets
no turning claims at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from emet_sdk.chains import EXPLAIN_TOPICS
from emet_sdk.resolve import descriptors_from_manifest
from emet_sdk.types import BodyFact, CapabilityDescriptor, LocomotionDescriptor, SelfModel

from emet_engine.body import RESERVED_TYPES as _RESERVED_TYPES
from emet_engine.prompting import DEFAULT_LINES

__all__ = ["BODY_TOPICS", "compile_self_model"]

#: Explain topics that are facts about the body. Each has a fact of the same
#: key exactly when it has an explanation, and the fact quotes it verbatim.
#: The other topics (`offline`, `error`) are the robot's state, the soul's
#: words for failure or the parts that are not working, and have no fact.
BODY_TOPICS: tuple[str, ...] = ("cannot_move", "cannot_turn")

# Capability types the schema accepts with no plugin category yet (`DESIGN.md`
# section 4.2) are `emet_engine.body.RESERVED_TYPES`, the one list: nothing
# starts them and nothing reports on them, so their presence is read from the
# manifest's blocks.

#: What a body moves on, by kinematics. Anything unlisted rolls.
_DRIVE_NOUNS: Mapping[str, str] = {"differential": "wheels", "tracked": "treads", "legged": "legs"}

_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
)

#: Head axes in the order they are said. The first move present names "your
#: head" and the rest say "it", so the sentence reads whichever axes exist.
_HEAD_MOVES: tuple[tuple[str, str], ...] = (
    ("yaw", "turn {} left and right"),
    ("pitch", "tilt {} up and down"),
    ("roll", "tip {} to one side"),
)

_SCALES: Mapping[str, str] = {
    "desk": "You are small: you sit on a desk or a table.",
    "floor": "You live on the floor.",
    "large": "You are large, about the size of a person.",
}

#: By `body.power`: what is said with a working drive, and without one.
_POWER: Mapping[str, tuple[str, str]] = {
    "plugged_in": (
        "You are plugged into the wall, so you cannot go far: only as far as your cable reaches.",
        "You are plugged into the wall.",
    ),
    "battery": (
        "You run on a battery, so you can go wherever you can drive until it runs down.",
        "You run on a battery.",
    ),
}

_NO_ARMS = (
    "You have no arms and no hands, so you cannot pick anything up, point at "
    "anything, or hand anything over."
)
_NO_EYES = (
    "You have no eyes and no face, so nobody can see your expression. Your "
    "voice is all the expression you have."
)
_SCREEN_EYES: Mapping[str, str] = {
    "dual_round": "You have two round eyes on small screens, and they show your expression.",
    "single_round": "You have one round eye on a small screen, and it shows your expression.",
}
_LIGHT_FORMS: Mapping[str, str] = {
    "ring": "You have a ring of lights that can glow and pulse.",
    "strip": "You have a strip of lights that can glow and pulse.",
}

#: Where a camera sits, by the type and role of the part it is mounted on.
#: A display that is not a face or eyes is a screen; anything else is the body.
_MOUNTS: Mapping[tuple[str, str], str] = {
    ("joint_group", "head"): "head",
    ("joint_group", "arm"): "arm",
    ("display", "eyes"): "face",
    ("display", "face"): "face",
}


@dataclass(frozen=True, slots=True)
class _Part:
    """One declared part: what its plugin reported, and the block behind it."""

    descriptor: CapabilityDescriptor
    block: Mapping[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return self.descriptor.capability_type

    @property
    def role(self) -> str | None:
        return self.descriptor.role

    @property
    def form(self) -> str | None:
        return self.descriptor.form

    @property
    def healthy(self) -> bool:
        return self.descriptor.healthy


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int | None:
    """An integer from the manifest, or None. YAML's `true` is not a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(n: int) -> str:
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _article(said: str) -> str:
    """The article before a number as it is said: an eight, an 11, an 18, an 80."""
    if said == "eight" or said.startswith("8"):
        return "an"
    if said.isdigit() and len(said) % 3 == 2 and said.startswith(("11", "18")):
        return "an"
    return "a"


def _join(items: Sequence[str], *, serial: bool) -> str:
    """Two items with "and"; three or more with commas, and a comma before the
    last "and" only when `serial`."""
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + (", and " if serial else " and ") + items[-1]


def _real(value: Any) -> float:
    """A finite number from the manifest or a plugin's report, else 0.0. A
    speed that is missing, infinite or not a number is left unsaid."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return 0.0


def _metres(value: float) -> str:
    """A positive speed as it is read out: 0.35, 1, 0.1. One too slow to show
    in two decimal places gets two significant figures: 0.001, 0.00001."""
    shown = f"{value:.2f}".rstrip("0").rstrip(".")
    if shown != "0":
        return shown
    places = 1 - math.floor(math.log10(value))
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def _pace(mps: float) -> str:
    """How a speed compares to walking. Coarse on purpose: a person hears
    "slower than a person walks" and knows what to expect."""
    if mps < 0.1:
        return "barely faster than a crawl"
    if mps < 0.8:
        return "slower than a person walks"
    if mps < 1.6:
        return "about as fast as a person walks"
    return "faster than a person walks"


def _readable(block: Mapping[str, Any]) -> Mapping[str, Any]:
    """A block as `descriptors_from_manifest` can read it. `joints` that are
    not a list, and joints whose axis is set to something other than a
    string, are dropped, so a malformed block becomes a part with fewer axes
    and never an exception."""
    joints = block.get("joints")
    if joints is None:
        return block
    kept = [
        j for j in (joints if isinstance(joints, list) else [])
        if isinstance(j, Mapping) and isinstance(j.get("axis"), (str, type(None)))
    ]
    return {**block, "joints": kept}


def _parts(
    blocks: Sequence[Mapping[str, Any]],
    capabilities: Sequence[CapabilityDescriptor] | None,
) -> list[_Part]:
    """Every declared part in manifest order, with what its plugin reported.

    Reserved types are left out: they have no plugin to report, and the one
    fact that mentions them reads the blocks. A part declared and not
    reported on is marked not working; one reported on and not declared is
    kept, at the end.
    """
    declared = [
        d
        for d in descriptors_from_manifest({"capabilities": [_readable(b) for b in blocks]})
        if d.capability_type not in _RESERVED_TYPES
    ]
    if capabilities is None:
        described = declared
    else:
        reported: dict[str, CapabilityDescriptor] = {}
        for descriptor in capabilities:
            reported.setdefault(descriptor.capability_id, descriptor)
        described = [reported.pop(d.capability_id, None) or replace(d, healthy=False) for d in declared]
        described += [d for d in reported.values() if d.capability_type not in _RESERVED_TYPES]
    by_id = {str(b.get("id", "")): b for b in blocks}
    return [_Part(d, by_id.get(d.capability_id, {})) for d in described]


def _first(parts: Sequence[_Part]) -> _Part | None:
    """The first working part, else the first declared one, else None."""
    return next((p for p in parts if p.healthy), parts[0] if parts else None)


def compile_self_model(
    manifest: Mapping[str, Any],
    *,
    capabilities: Sequence[CapabilityDescriptor] | None = None,
    locomotion: LocomotionDescriptor | None = None,
    lines: Mapping[str, str | None] | None = None,
) -> SelfModel:
    """Compile a body into what the robot is told about it and what it says.

    `capabilities` is what the started plugins reported, one descriptor per
    part; None compiles the manifest alone, with every part assumed to work.
    `locomotion` is the started drive's report, or None when there is no
    drive, the drive failed, or nothing was started. `lines` are the soul's
    persona lines (`declined`, `failed`, `nothing_heard`); None means the
    engine's defaults. A field that is missing or unknown is skipped, never
    raised on: a manifest with no `body` block still compiles.
    """
    manifest = _mapping(manifest)
    lines = DEFAULT_LINES if lines is None else lines
    body = _mapping(manifest.get("body"))
    heard = _mapping(_mapping(manifest.get("audio")).get("input"))
    raw = manifest.get("capabilities")
    blocks = [b for b in raw if isinstance(b, Mapping)] if isinstance(raw, list) else []
    parts = _parts(blocks, capabilities)

    def of(cap_type: str, *roles: str) -> list[_Part]:
        return [p for p in parts if p.type == cap_type and (not roles or p.role in roles)]

    drive = _first(of("drive"))
    moving = drive is not None and drive.healthy
    if moving and locomotion is not None:
        kinematics = locomotion.kinematics
    else:
        kinematics = str(drive.block.get("kinematics") or "") if drive is not None else ""
    wheels = _DRIVE_NOUNS.get(kinematics, "wheels")

    # The head `move.turn_to` binds when it cannot use the drive: a working
    # one with a yaw axis, if any head has one. Otherwise a second head that
    # turns would perform a turn the prompt had just denied.
    heads = of("joint_group", "head")
    head = next((p for p in heads if p.healthy and "yaw" in p.descriptor.axes), None) or _first(heads)
    head_works = head is not None and head.healthy
    head_turns = head_works and "yaw" in head.descriptor.axes

    explanations = _explanations(drive, moving, wheels, head, head_works, head_turns, parts, lines)

    facts: list[BodyFact] = []

    def fact(key: str, text: str, present: bool = True) -> None:
        facts.append(BodyFact(key, text, present))

    def honest_answer(topic: str, request: str) -> None:
        # The fact quotes the explanation, so the prompt and the chain say
        # the same words, and it exists exactly when the explanation does.
        words = explanations[topic]
        if words is not None:
            fact(topic, f'If someone asks you to {request}, the honest answer is "{words}"', False)

    description = " ".join(str(body.get("description") or "").split())
    if description:
        fact("description", f'Your builder describes you like this: "{description}"')

    scale = _SCALES.get(str(body.get("scale")))
    if scale:
        fact("scale", scale)

    power = _POWER.get(str(body.get("power")))
    if power:
        with_drive, without = power
        fact("power", with_drive if moving else without)

    # One microphone unless the manifest says more: a body that downmixes an
    # array before the engine sees it still hears through all of them.
    channels = _count(heard.get("channels"))
    if channels is not None and channels > 1:
        said = _number(channels)
        fact("hearing", f"You hear through {_article(said)} {said}-microphone array.")
    else:
        fact("hearing", "You hear through one microphone.")

    if heard.get("doa") is True:
        fact("direction", "You can tell roughly which direction a voice comes from.")
    else:
        fact("direction", "You cannot tell which direction a voice comes from.", False)

    fact("voice", "You speak through a speaker.")

    if moving:
        fact("locomotion", _moving(wheels, locomotion, drive.block))
    elif drive is not None:
        fact("locomotion", f"Your {wheels} are not working right now, so you cannot move.", False)
    else:
        fact("locomotion", "You have no wheels and no legs, so you cannot move from where you are.", False)
    honest_answer("cannot_move", "come to them")

    if head is None:
        fact("head", "You have no head, so you cannot turn to look at anyone, nod, or shake your head.",
             False)
    elif not head_works:
        fact("head", "Your head is not working right now, so you cannot move it.", False)
    else:
        fact("head", _head(head.descriptor.axes))
    honest_answer("cannot_turn", "turn toward them")

    arm = _first(of("joint_group", "arm"))
    if any(b.get("type") == "manipulator" for b in blocks):
        fact("arms", "You have a gripper, but you cannot use it yet, so you cannot pick anything up.",
             False)
    elif arm is not None and arm.healthy:
        fact("arms", "You have an arm you can move, but no hand, so you cannot pick anything up.")
    elif arm is not None:
        fact("arms", "Your arm is not working right now, so you cannot move it or pick anything up.",
             False)
    else:
        fact("arms", _NO_ARMS, False)

    fact("eyes", *_eyes(of("display", "eyes"), of("display", "face"), of("light", "eyes")))

    lights = [p for p in of("light") if p.role != "eyes"]
    if lights:
        fact("lights", *_lights(lights))

    fact("camera", _camera(_first(of("camera")), {str(b.get("id", "")): b for b in blocks}), False)

    return SelfModel(facts=tuple(facts), explanations=explanations)


def _explanations(
    drive: _Part | None,
    moving: bool,
    wheels: str,
    head: _Part | None,
    head_works: bool,
    head_turns: bool,
    parts: Sequence[_Part],
    lines: Mapping[str, str | None],
) -> dict[str, str | None]:
    """What `explain` says, first person, one entry per topic in `EXPLAIN_TOPICS`."""
    out: dict[str, str | None] = {}

    if moving:
        out["cannot_move"] = None
    elif drive is None:
        out["cannot_move"] = "I cannot come to you. I have no wheels."
    else:
        out["cannot_move"] = f"I cannot come to you. My {wheels} are not working."

    if moving or head_turns:
        out["cannot_turn"] = None
    elif drive is None and head is None:
        out["cannot_turn"] = "I cannot turn toward you. I have no wheels and no head."
    else:
        no_drive = "I have no wheels" if drive is None else f"My {wheels} are not working"
        if head is None:
            no_head = "I have no head"
        elif not head_works:
            no_head = "my head is not working"
        else:
            no_head = "my head does not turn"
        out["cannot_turn"] = f"I cannot turn toward you. {no_drive}, and {no_head}."

    out["offline"] = lines.get("failed")
    broken = _broken(parts, wheels)
    out["error"] = f"Part of me is not working: {_join(broken, serial=False)}." if broken else lines.get("failed")

    for topic in sorted(EXPLAIN_TOPICS - out.keys()):
        # A topic the SDK gained after this template was written: silence
        # until the compiler has words for it, never somebody else's words.
        out[topic] = None
    return out


def _moving(wheels: str, locomotion: LocomotionDescriptor | None, block: Mapping[str, Any]) -> str:
    """How a working drive moves, as far as its plugin reported."""
    verb = "walk" if wheels == "legs" else "drive"
    said = [f"You move on {wheels}."]
    if locomotion is None:
        # No report: presence, and a top speed when the manifest states one.
        # Turning is the locomotion plugin's to describe, so none is claimed.
        speed = _real(_mapping(block.get("limits")).get("max_linear_mps"))
    else:
        if locomotion.can_turn_in_place:
            said.append(f"You can {verb} forward and back and turn on the spot.")
        else:
            said.append(f"You cannot turn on the spot: to face another way you have to {verb} "
                        f"forward in an arc.")
        if locomotion.holonomic:
            said.append("You can also slide sideways without turning.")
        speed = _real(locomotion.max_linear_mps)

    if speed > 0:
        shown = _metres(speed)
        said.append(f"At most you go {shown} {'metre' if shown == '1' else 'metres'} a second, "
                    f"{_pace(speed)}.")

    if locomotion is not None:
        turning = _real(locomotion.max_angular_rps)
        if locomotion.can_turn_in_place and turning > 0:
            seconds = 2 * math.pi / turning
            if seconds < 1:
                said.append("A full turn takes you less than a second.")
            else:
                n = round(seconds)
                said.append(f"A full turn takes you about {_number(n)} second{'s' if n != 1 else ''}.")
        if locomotion.kinematics == "tracked":
            said.append("Your treads scrub when you turn, so your turns are slow and never quite exact.")
    return " ".join(said)


def _head(axes: frozenset[str]) -> str:
    """What a working head can do, by its axes."""
    moves = [move for axis, move in _HEAD_MOVES if axis in axes]
    if not moves:
        return "You have a head, but you cannot turn it to look around."
    said = [move.format("it" if i else "your head") for i, move in enumerate(moves)]
    sentence = "You can " + _join(said, serial=True)
    if "yaw" not in axes:
        sentence += ", but you cannot turn it to look around"
    return sentence + "."


def _eyes(screens: Sequence[_Part], faces: Sequence[_Part], lamps: Sequence[_Part]) -> tuple[str, bool]:
    """Eyes on a screen first, then a face on a screen, then lights for eyes."""
    working = next((p for p in (*screens, *faces, *lamps) if p.healthy), None)
    if working is None and not (screens or faces or lamps):
        return _NO_EYES, False
    if working is None:
        whose = "Your face is" if faces and not (screens or lamps) else "Your eyes are"
        return f"{whose} not working right now, so nobody can see your expression.", False
    if working.type == "light":
        return "You have lights for eyes that glow and change colour.", True
    if working.role == "face":
        return "You have a face on a screen, and it shows your expression.", True
    return _SCREEN_EYES.get(str(working.form), "You have eyes on a screen, and they show your expression."), True


def _lights(lights: Sequence[_Part]) -> tuple[str, bool]:
    """One fact for every light that is not an eye: the first ambient one, else status."""
    working = [p for p in lights if p.healthy]
    if not working:
        return "Your lights are not working right now.", False
    ambient = next((p for p in working if p.role != "status"), None)
    if ambient is None:
        return "You have a status light that shows what you are doing.", True
    return _LIGHT_FORMS.get(str(ambient.form), "You have a light that can glow and pulse."), True


def _camera(camera: _Part | None, blocks: Mapping[str, Mapping[str, Any]]) -> str:
    """Always an absence, until what a camera sees reaches the model."""
    if camera is None:
        return "You cannot see: you have no camera."
    if not camera.healthy:
        return "Your camera is not working, so you cannot see."
    mounted = str(camera.block.get("mounted_on") or "body")
    mount = {} if mounted == "body" else _mapping(blocks.get(mounted))
    key = (str(mount.get("type")), str(mount.get("role")))
    place = _MOUNTS.get(key, "screen" if key[0] == "display" else "body")
    return f"You have a camera on your {place}, but nothing it sees reaches you yet, so you cannot see."


def _broken(parts: Sequence[_Part], wheels: str) -> list[str]:
    """What the robot calls each part that reported unhealthy, once each, in
    manifest order."""
    nouns: list[str] = []
    for part in parts:
        if part.healthy:
            continue
        if part.type == "joint_group":
            noun = {"head": "my head", "arm": "my arm", "torso": "my body"}.get(
                str(part.role), "one of my joints"
            )
        elif part.type == "drive":
            noun = f"my {wheels}"
        elif part.type == "display":
            noun = {"eyes": "my eyes", "face": "my face"}.get(str(part.role), "my screen")
        elif part.type == "light":
            noun = "my eyes" if part.role == "eyes" else "my lights"
        elif part.type == "camera":
            noun = "my camera"
        elif part.type == "sensor":
            noun = "one of my senses"
        else:
            noun = "one of my parts"
        if noun not in nouns:
            nouns.append(noun)
    return nouns
