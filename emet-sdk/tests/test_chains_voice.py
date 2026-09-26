"""What a voice rung can do, and the one loader every caller shares.

Every chain ends in a voice rung, so the voice actions are what a bare body
does with every intent it is given (`DESIGN.md` section 6.1). Until 0.5 a
voice rung could name any action and bind to it; the engine performed none of
them, so nothing noticed. Now the engine performs them, and the SDK names the
closed set it performs: `VOICE_ACTIONS`, and for `explain`, the topics in
`EXPLAIN_TOPICS` that the self-model compiler writes words for. A chain
naming anything else would bind and then say nothing, which is the quiet
failure the terminal-rung rule was written to prevent.

`load_chains` is the one loader `emet explain` and the engine's boot both go
through, so the table a builder reads on a laptop starts from the chains the
robot resolves. `emet explain` also names the body now, because a table with
only an id in its header does not say which robot it is about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from emet_sdk.chains import EXPLAIN_TOPICS, TONE_PRESETS, VOICE_ACTIONS, parse_chain_set
from emet_sdk.cli import main
from emet_sdk.resolve import load_chains, shipped_chain_files
from emet_sdk.validate import load_yaml, validate_chain_document

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def codes(report) -> set[str]:
    return {f.code for f in report.errors}


def voice_chain(action: str, intent: str = "express.curiosity", **params: Any) -> dict:
    """A chain document with one intent: a head rung, then the voice rung.
    The voice rung sits at index 1, which a finding's path has to name."""
    return {
        intent: {
            "mode": "first",
            "rungs": [
                {"actuator": {"role": "head", "axis": "pitch"}, "action": "tilt", "params": {}},
                {"actuator": {"voice": True}, "action": action, "params": params},
            ],
        }
    }


def write_chains(path: Path, doc: dict) -> Path:
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


# ------------------------------------------------------ the shipped chains


def test_the_sdk_ships_the_core_and_express_chains_in_load_order():
    files = shipped_chain_files()
    assert [p.name for p in files] == ["core.yaml", "express.yaml"]
    assert all(p.is_file() for p in files)


def test_the_shared_loader_reads_every_shipped_chain():
    by_hand: dict = {}
    for path in shipped_chain_files():
        by_hand.update(parse_chain_set(load_yaml(path)))
    assert load_chains() == by_hand
    assert "move.approach" in by_hand and "express.curiosity" in by_hand


def test_every_shipped_voice_rung_names_an_action_the_engine_performs():
    for name, chain in load_chains().items():
        assert chain.voice_rung.action in VOICE_ACTIONS, name


def test_every_shipped_explain_rung_names_a_topic_the_engine_has_words_for():
    explained = {
        name: chain.voice_rung.params.get("topic")
        for name, chain in load_chains().items()
        if chain.voice_rung.action == "explain"
    }
    assert explained, "no shipped chain explains anything; the glob or the loader is wrong"
    for name, topic in explained.items():
        assert topic in EXPLAIN_TOPICS, f"{name} explains {topic!r}"


def test_every_voice_action_and_every_topic_is_used_by_a_shipped_chain():
    """The other direction. A topic no shipped chain asks about is words the
    compiler writes and nobody says; a voice action nothing uses is an engine
    branch no body reaches. Adding either to the SDK without a chain is a
    decision worth making on purpose, and this test is where it shows."""
    chains = load_chains().values()
    assert {c.voice_rung.action for c in chains} == VOICE_ACTIONS
    assert {
        c.voice_rung.params.get("topic") for c in chains if c.voice_rung.action == "explain"
    } == EXPLAIN_TOPICS


def test_every_shipped_sounding_rung_carries_what_it_sounds_like():
    """An `inflect` needs a filler to say and a `tone` needs a preset to
    play. A rung missing one binds and makes no sound, and on a bare body
    that is the whole of the intent. (`backchannel` fires nothing until 1.0,
    so its fillers are not checked here.)"""
    for name, chain in load_chains().items():
        rung = chain.voice_rung
        if rung.action == "inflect":
            fillers = rung.params.get("filler")
            assert isinstance(fillers, list) and fillers, name
            assert all(isinstance(f, str) and f.strip() for f in fillers), name
        elif rung.action == "tone":
            assert isinstance(rung.params.get("preset"), str), name


def test_movement_explains_the_body_and_signals_explain_the_robots_state():
    """`cannot_move` and `cannot_turn` are facts about the body, compiled
    with the self-model; `offline` and `error` are the robot's own state, the
    soul's words for failure or the parts that are not working. The movement
    chains ask for the first two and the signal chains for the other two."""
    chains = load_chains()
    assert chains["move.approach"].voice_rung.params == {"topic": "cannot_move"}
    assert chains["move.retreat"].voice_rung.params == {"topic": "cannot_move"}
    assert chains["move.turn_to"].voice_rung.params == {"topic": "cannot_turn"}
    assert chains["signal.offline"].voice_rung.params == {"topic": "offline"}
    assert chains["signal.error"].voice_rung.params == {"topic": "error"}


# ---------------------------------------------------------------- validation


#: What each sounding voice action needs to make its sound.
SOUNDING = {"inflect": {"filler": ["hm?"]}, "tone": {"preset": "soft_click"}}


@pytest.mark.parametrize("action", sorted(VOICE_ACTIONS - {"explain"}))
def test_a_chain_may_end_in_any_voice_action(action: str):
    report = validate_chain_document(voice_chain(action, **SOUNDING.get(action, {})))
    assert report.ok, report.errors


@pytest.mark.parametrize("preset", sorted(TONE_PRESETS))
def test_a_chain_may_play_any_tone_the_engine_renders(preset: str):
    assert validate_chain_document(voice_chain("tone", preset=preset)).ok


@pytest.mark.parametrize("params", [{}, {"preset": "soft_clik"}, {"preset": ["soft_click"]}])
def test_a_tone_the_engine_cannot_render_is_rejected(params):
    """A typo in a preset would bind, play nothing, and leave one warning in
    a log: on a body with only a speaker, the listening click would vanish."""
    report = validate_chain_document(voice_chain("tone", intent="signal.listening", **params))
    assert codes(report) == {"unknown_tone_preset"}


@pytest.mark.parametrize("params", [{}, {"filler": []}, {"filler": ["  "]}, {"filler": 5}])
def test_an_inflect_with_nothing_to_say_is_rejected(params):
    report = validate_chain_document(voice_chain("inflect", **params))
    assert codes(report) == {"empty_inflect"}


@pytest.mark.parametrize("filler", [["hm?", None], ["hm?", 3], ["hm?", ""], ["hm?", ["hmm"]]])
def test_one_word_does_not_carry_a_filler_list_that_is_not_all_words(filler):
    """YAML turns a trailing `-` into a null, and the voice would say "None"
    for it the second time the robot was curious."""
    report = validate_chain_document(voice_chain("inflect", filler=filler))
    assert codes(report) == {"empty_inflect"}


def test_a_topic_that_is_not_a_word_is_reported_and_does_not_crash_the_validator():
    report = validate_chain_document(voice_chain("explain", intent="move.approach", topic=["cannot_move"]))
    assert codes(report) == {"unknown_explain_topic"}


@pytest.mark.parametrize("topic", sorted(EXPLAIN_TOPICS))
def test_a_chain_may_explain_any_topic(topic: str):
    report = validate_chain_document(voice_chain("explain", topic=topic))
    assert report.ok, report.errors


def test_a_voice_rung_that_sings_is_rejected():
    """The same document with a known action validates clean, so this is
    the action being refused and nothing else about the chain."""
    report = validate_chain_document(voice_chain("sing"))
    assert not report.ok
    assert codes(report) == {"unknown_voice_action"}
    (finding,) = report.errors
    assert finding.path == "/express.curiosity/rungs/1/action"
    assert "'sing'" in finding.message
    assert all(action in finding.message for action in VOICE_ACTIONS)


def test_an_explain_rung_about_flying_is_rejected():
    report = validate_chain_document(voice_chain("explain", intent="move.approach", topic="cannot_fly"))
    assert not report.ok
    assert codes(report) == {"unknown_explain_topic"}
    (finding,) = report.errors
    assert finding.path == "/move.approach/rungs/1/params/topic"
    assert "'cannot_fly'" in finding.message
    assert all(topic in finding.message for topic in EXPLAIN_TOPICS)


def test_an_explain_rung_with_no_topic_is_rejected():
    report = validate_chain_document(voice_chain("explain", intent="move.approach"))
    assert codes(report) == {"unknown_explain_topic"}


def test_hardware_actions_stay_the_plugins_business():
    """Only the voice rung's action is a closed set. A hardware rung binds by
    its selector, and its action goes to that part's plugin, which says in
    `describe()` what it performs. The validator has no plugin to ask, so it
    lets the action through."""
    doc = voice_chain("inflect", filler=["hm?"])
    doc["express.curiosity"]["rungs"][0]["action"] = "sing"
    assert validate_chain_document(doc).ok


def test_a_voice_rung_that_sings_on_disk_fails_emet_validate(tmp_path, capsys):
    """The rule reaches the command line, where a soul author meets it."""
    path = write_chains(tmp_path / "chains.yaml", voice_chain("sing"))
    assert main(["validate", str(path)]) == 1
    out = capsys.readouterr().out
    assert "unknown_voice_action" in out


# -------------------------------------------------------------- overriding


def test_a_later_chain_file_wins(tmp_path):
    """How a soul changes one ladder: it ships only that chain, loaded after
    the SDK's, and every chain it does not mention stays as shipped."""
    first = write_chains(tmp_path / "first.yaml", voice_chain("inflect", filler=["ooh?"]))
    second = write_chains(tmp_path / "second.yaml", voice_chain("inflect", filler=["eh?"]))

    shipped = load_chains()
    once = load_chains([first])
    twice = load_chains([first, second])

    assert shipped["express.curiosity"].voice_rung.params["filler"] == ["hm?", "hmm."]
    assert once["express.curiosity"].voice_rung.params["filler"] == ["ooh?"]
    assert twice["express.curiosity"].voice_rung.params["filler"] == ["eh?"]
    assert len(once["express.curiosity"].rungs) == 2, "the override replaces the whole ladder"

    assert set(once) == set(shipped)
    assert once["express.delight"] == shipped["express.delight"]
    assert once["move.approach"] == shipped["move.approach"]


def test_an_override_does_not_leak_into_the_next_load(tmp_path):
    override = write_chains(tmp_path / "soul.yaml", voice_chain("silence"))
    assert load_chains([override])["express.curiosity"].voice_rung.action == "silence"
    assert load_chains()["express.curiosity"].voice_rung.action == "inflect"


def test_the_loader_takes_path_strings_too(tmp_path):
    override = write_chains(tmp_path / "soul.yaml", voice_chain("silence"))
    assert load_chains([str(override)])["express.curiosity"].voice_rung.action == "silence"


@pytest.mark.parametrize(
    "document,word",
    [
        (voice_chain("sing"), "sing"),
        (voice_chain("explain", topic="cannot_fly"), "cannot_fly"),
        (voice_chain("inflect", intent="express.joy", filler=["yay"]), "joy"),
    ],
)
def test_an_override_the_validator_rejects_is_refused_before_it_is_laid_over_anything(tmp_path, document, word):
    """`emet explain --chains` and the engine's `--chains` both load through
    here, so a voice rung that sings, a topic nobody has words for, or an
    intent the vocabulary lacks stops the load, naming the file, where it
    would otherwise bind and then say nothing."""
    from emet_sdk.chains import ChainError

    override = write_chains(tmp_path / "bad.yaml", document)
    with pytest.raises(ChainError) as exc:
        load_chains([override])
    assert "bad.yaml" in str(exc.value) and word in str(exc.value)


# ------------------------------------------------------------- emet explain


def header(out: str) -> str:
    lines = [line for line in out.splitlines() if line.startswith("BINDING TABLE")]
    assert len(lines) == 1, out
    return lines[0]


def test_explain_names_the_body_in_its_header(capsys):
    """The header also holds the manifest's path, which contains the word
    bodiless, so the id is looked for inside the body label."""
    assert main(["explain", str(EXAMPLES / "bodiless.yaml")]) == 0
    line = header(capsys.readouterr().out)
    assert "(body: bodiless, " in line
    assert "a speakerphone on a desk" in line


def test_explain_names_the_reference_body_too(capsys):
    assert main(["explain", str(EXAMPLES / "pi-speakerphone.yaml")]) == 0
    line = header(capsys.readouterr().out)
    assert "(body: pi_speakerphone, " in line
    assert "a Raspberry Pi with a USB microphone and speaker" in line


def test_explain_shows_the_id_alone_for_a_body_with_no_name(tmp_path, capsys):
    """`body.name` is optional. A header must not print `None` for it."""
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    del doc["body"]["name"]
    path = tmp_path / "unnamed.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    assert main(["explain", str(path)]) == 0
    line = header(capsys.readouterr().out)
    assert line.endswith("(body: bodiless)")
    assert "None" not in line
