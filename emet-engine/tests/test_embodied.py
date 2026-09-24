"""The session with a body: 0.5 end to end, offline.

A wav for a microphone, the mock detector, the mock providers, and a `wav`
or `null` sink, as in `test_session.py`. What is new is everything between
the words and the voice: the self-model in the prompt, the reply as `speak`
intents, tags performed where they stood, the engine's own signals, and the
body's state file carried from one boot to the next.
"""

from __future__ import annotations

import asyncio
import copy
import json
import wave
from pathlib import Path

import pytest

from emet_sdk.types import ReplyDone, TextDelta, Transcript
from emet_sdk.validate import MissingPluginError, load_yaml

from emet_engine.session import EngineError, ListenSession

PHRASE = "hey emet"
FRAME_BYTES = 2560
EXAMPLES = Path(__file__).resolve().parents[2] / "emet-sdk" / "examples"


def run(coro):
    return asyncio.run(coro)


def write_wav(path: Path, pcm: bytes) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return str(path)


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())


def frame(payload: bytes = b"") -> bytes:
    return payload + bytes(FRAME_BYTES - len(payload))


def loud_frame(amplitude: int = 4000) -> bytes:
    from array import array

    return array("h", [amplitude, -amplitude] * (FRAME_BYTES // 4)).tobytes()


def spoken(*words: bytes) -> bytes:
    return frame() + frame(PHRASE.encode()) + b"".join(frame(w) for w in words) + frame() * 20


def heard(path: str) -> str:
    from emet_providers.mock import read_back  # a test may name the layer above

    return read_back(read_wav(path))


def body_like(example: str, wav: str, out: str | None = None, **models) -> dict:
    """An example manifest with its ears pointed at a file, its mouth at a
    file or a bin, and every stage taken over by the mocks."""
    manifest = copy.deepcopy(load_yaml(EXAMPLES / example))
    manifest["audio"]["input"] = {"source": "wav", "device": "file", "params": {"path": wav}, "aec": manifest["audio"]["input"].get("aec", "none")}
    manifest["audio"]["output"] = (
        {"sink": "wav", "device": "none", "params": {"path": out}} if out else {"sink": "null", "device": "none"}
    )
    manifest["audio"]["wake"] = {"engine": "mock", "params": {}}
    manifest["models"] = {"stt": {"provider": "mock"}, "chat": {"provider": "mock"}, "tts": {"provider": "mock"}}
    for stage, params in models.items():
        manifest["models"][stage] = {"provider": "mock", "params": params}
    return manifest


def soul(**extra) -> dict:
    doc = {"identity": {"name": "Emet", "wake_word": PHRASE}}
    doc.update(extra)
    return doc


def talking(manifest: dict, soul_doc: dict | None = None, **kw) -> ListenSession:
    return ListenSession(manifest, soul_doc or soul(), transcribe=True, reply=True, speak=True, **kw)


async def drain(events):
    return [e async for e in events]


# ------------------------------------------------------------------- the gate


def test_the_gate_the_model_is_told_it_cannot_come_to_you(tmp_path):
    """The roadmap's test, on the prompt itself: the bodiless body's
    assembled prompt says it cannot come to you, and it is the prompt the
    language model was actually handed."""
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "g.wav", spoken(b"can you", b"come here"))))

    async def scenario():
        async with session:
            return [x async for x in session.talk()], session._llm.prompts

    (exchange,), prompts = run(scenario())
    system = prompts[0].system
    assert "cannot come to you" in system
    assert "I have no wheels" in system
    assert "YOUR BODY" in system
    assert system.index("You are Emet") < system.index("YOUR BODY") < system.index("speaking aloud")
    assert exchange.heard == "can you come here"


def test_the_gate_on_a_scripted_reply_it_declines_in_its_own_voice(tmp_path):
    """The model's side of the gate, scripted: it declines and colours the
    sentence with concern. On a bodiless body the concern is a filler in the
    same voice, and every word of the refusal reaches the speaker."""
    out = str(tmp_path / "said.wav")
    manifest = body_like(
        "bodiless.yaml",
        write_wav(tmp_path / "s.wav", spoken(b"can you", b"come here")),
        out,
        chat={"reply": "[express.concern] I cannot come to you. I have no wheels."},
    )
    session = talking(manifest)

    async def scenario():
        async with session:
            return [x async for x in session.talk()]

    (exchange,) = run(scenario())
    assert [s.text for s in exchange.spoken] == ["oh.", "I cannot come to you.", "I have no wheels."]
    assert heard(out) == "oh. I cannot come to you. I have no wheels."
    assert [i.name for i in exchange.intents] == ["express.concern"]


def test_the_chain_layer_declines_in_the_same_words_as_the_prompt(tmp_path):
    """The other door. A model that tries to come anyway tags `move.approach`;
    the bodiless body's chain ends in `explain`, which says what the prompt
    already said, word for word."""
    out = str(tmp_path / "said.wav")
    manifest = body_like(
        "bodiless.yaml",
        write_wav(tmp_path / "c.wav", spoken(b"come", b"here")),
        out,
        chat={"reply": "[move.approach] On my way."},
    )
    session = talking(manifest)

    async def scenario():
        async with session:
            exchanges = [x async for x in session.talk()]
            return exchanges, session.system_prompt, session.self_model

    (exchange,), system, model = run(scenario())
    line = model.explanations["cannot_move"]
    assert line in system
    # The explanation does not silence what the model wrote after its tag:
    # a model that ignored its prompt is heard ignoring it, after the truth.
    assert [s.text for s in exchange.spoken] == sentences(line) + ["On my way."]


def sentences(text: str) -> list[str]:
    from emet_engine.session import sentences_of

    return sentences_of(text)


def test_a_body_with_treads_is_not_told_it_cannot_come(tmp_path):
    session = talking(body_like("mock-scout.yaml", write_wav(tmp_path / "t.wav", frame())))

    async def scenario():
        async with session:
            return session.system_prompt, session.tags

    system, tags = run(scenario())
    assert "cannot come to you" not in system
    assert "treads" in system
    assert "move.approach" in tags and "[move.approach] to come closer" in system


def test_a_bodiless_body_is_offered_only_the_tags_its_voice_can_answer(tmp_path):
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "o.wav", frame())))

    async def scenario():
        async with session:
            return session.tags

    tags = run(scenario())
    assert tags and all(t.startswith("express.") for t in tags)


def test_the_prompt_names_no_part_of_the_manifest(tmp_path):
    """Principle 1 at the prompt: a builder's ids and the drivers' names never
    reach the model; the head and the treads do. The ids are made unusual so
    that a match cannot be an ordinary word."""
    manifest = body_like("mock-scout.yaml", write_wav(tmp_path / "n.wav", frame()))
    for i, cap in enumerate(manifest["capabilities"]):
        cap["id"] = f"zq_part_{i}"
    session = talking(manifest)

    async def scenario():
        async with session:
            return session.system_prompt, session.binding_table

    system, table = run(scenario())
    assert table["express.curiosity"].capability_id == "zq_part_0", "chains bind by role, not id"
    assert "zq_part" not in system and "emet_hal" not in system and "mock" not in system
    assert "head" in system and "treads" in system


# ------------------------------------------------------------ tags and speech


def test_a_tag_on_the_scout_moves_the_head_and_says_nothing_extra(tmp_path):
    out = str(tmp_path / "scout.wav")
    manifest = body_like(
        "mock-scout.yaml",
        write_wav(tmp_path / "h.wav", spoken(b"hello")),
        out,
        chat={"reply": "[express.curiosity] What was that?"},
    )
    session = talking(manifest)

    async def scenario():
        async with session:
            exchanges = [x async for x in session.talk()]
            return exchanges, list(session.body.plugin("head").applied)

    (exchange,), applied = run(scenario())
    assert [s.text for s in exchange.spoken] == ["What was that?"]
    assert [a.name for a in applied] == ["tilt"]


def test_a_tag_is_acted_on_where_it_stood_inside_one_delta(tmp_path):
    """A real model streams a tag and the sentences around it in one delta.
    The filler goes before the sentence the tag opened, which comes before
    the next one, whatever the delta held."""
    out = str(tmp_path / "order.wav")
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "d.wav", frame()), out))

    async def scenario():
        async with session:

            async def one_delta(prompt):
                yield TextDelta("[express.curiosity] Oh! Tell me more. ")
                yield ReplyDone(text="[express.curiosity] Oh! Tell me more.", stop_reason="end", model="mock")

            session._llm.reply = one_delta
            await drain(session.answer_aloud("guess what"))
            return session.last_spoken

    spoken = run(scenario())
    assert [s.text for s in spoken] == ["hm?", "Oh!", "Tell me more."]


def test_a_boot_that_fails_part_way_puts_the_parts_it_started_back_to_rest(tmp_path, monkeypatch):
    """`__aexit__` never runs for a failed `__aenter__`, and from 1.0 a
    started part can be a servo. The scout's parts are started before the
    audio source, which does not exist here, so the boot fails after them."""
    from emet_hal import mock

    stopped: list[str] = []
    real = mock.MockActuator.shutdown

    async def shutdown(self):
        stopped.append(self.capability_id)
        await real(self)

    monkeypatch.setattr(mock.MockActuator, "shutdown", shutdown)
    manifest = body_like("mock-scout.yaml", write_wav(tmp_path / "f.wav", frame()))
    manifest["audio"]["input"]["source"] = "carrier_pigeon"
    session = talking(manifest)

    async def boot():
        async with session:
            pass

    with pytest.raises(MissingPluginError):
        run(boot())
    assert sorted(stopped) == ["base", "eyes", "head", "ring"]


def test_every_sentence_of_a_reply_travels_as_a_speak_intent(tmp_path):
    manifest = body_like("bodiless.yaml", write_wav(tmp_path / "p.wav", frame()), chat={"reply": "One. Two?"})
    session = talking(manifest)

    async def scenario():
        async with session:
            await drain(session.answer_aloud("hello"))
            return session.performed

    performed = run(scenario())
    speaks = [p for p in performed if p.intent.kind == "speak"]
    assert [p.action.params["text"] for p in speaks] == ["One.", "Two?"]
    assert [p.action.params["contour"] for p in speaks] == ["level", "rising"]
    order = [p.intent.name for p in performed if p.intent.kind == "signal"]
    assert order[-2:] == ["signal.thinking", "signal.speaking"]


def test_reserved_and_engine_only_tags_are_neither_reported_nor_performed(tmp_path):
    manifest = body_like(
        "mock-scout.yaml",
        write_wav(tmp_path / "r.wav", frame()),
        chat={"reply": "[manipulate.grasp] [signal.offline] Sure."},
    )
    session = talking(manifest)

    async def scenario():
        async with session:
            events = await drain(session.answer_aloud("hand me that"))
            return events, session.last_intents, session.performed, session.last_spoken

    events, intents, performed, spoken = run(scenario())
    assert intents == []
    assert not any(p.intent.kind in ("manipulate",) for p in performed)
    assert not any(p.intent.name == "signal.offline" for p in performed)
    assert [s.text for s in spoken] == ["Sure."]


# ---------------------------------------------------------------- signals


def test_the_engine_signals_its_own_state_through_the_chains(tmp_path):
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "sig.wav", spoken(b"hello"))))

    async def scenario():
        async with session:
            [x async for x in session.talk()]
            during = [p.intent.name for p in session.performed if p.intent.kind == "signal"]
        return during

    during = run(scenario())
    assert during == ["signal.booting", "signal.listening", "signal.thinking", "signal.speaking"]


def test_the_tones_reach_the_sink_and_leave_the_words_readable(tmp_path):
    out = str(tmp_path / "tones.wav")
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "t.wav", spoken(b"what", b"time")), out))
    tones: list = []
    session.on_performed = lambda p: tones.append(p) if p.audio_ms else None

    async def scenario():
        async with session:
            [x async for x in session.talk()]

    run(scenario())
    assert [p.intent.name for p in tones] == ["signal.booting", "signal.listening", "signal.muted"]
    assert heard(out) == "You said: what time"
    assert len(read_wav(out)) > 0


def test_a_part_that_does_not_work_at_boot_is_said_out_loud_where_nothing_else_can_show_it(tmp_path):
    manifest = body_like("bodiless.yaml", write_wav(tmp_path / "e.wav", frame()), str(tmp_path / "err.wav"))
    manifest["capabilities"] = [
        {
            "id": "neck",
            "type": "joint_group",
            "role": "head",
            "driver": {"plugin": "emet_hal.mock", "params": {"fail_on_start": True}},
            "joints": [{"id": "pan", "axis": "yaw", "range_deg": [-90, 90]}],
        }
    ]
    session = talking(manifest)

    async def scenario():
        async with session:
            return [p for p in session.performed if p.intent.name == "signal.error"]

    (error,) = run(scenario())
    assert error.said == "Part of me is not working: my head."
    assert heard(str(tmp_path / "err.wav")) == "Part of me is not working: my head."


def test_a_lost_network_is_said_once_through_the_offline_signal(tmp_path):
    out = str(tmp_path / "lost.wav")
    session = talking(
        body_like("bodiless.yaml", write_wav(tmp_path / "l.wav", spoken(b"hello", b"there")), out),
        soul(persona={"lines": {"failed": "I lost the thread there."}}),
    )

    async def scenario():
        async with session:

            async def finish():
                return Transcript(text="", final=True, error="OSError: name resolution failed")

            session._stt.finish = finish
            return [x async for x in session.talk()], session.performed

    (exchange,), performed = run(scenario())
    assert exchange.line == "I lost the thread there."
    assert [s.text for s in exchange.spoken] == ["I lost the thread there."]
    assert heard(out) == "I lost the thread there.", "said once, not twice"
    assert any(p.intent.name == "signal.offline" and p.binding.action == "explain" for p in performed)


def test_where_a_status_light_shows_offline_the_line_is_still_said(tmp_path):
    out = str(tmp_path / "ring.wav")
    session = talking(
        body_like("mock-scout.yaml", write_wav(tmp_path / "r.wav", spoken(b"hello", b"there")), out),
        soul(persona={"lines": {"failed": "Lost it."}}),
    )

    async def scenario():
        async with session:

            async def finish():
                return Transcript(text="", final=True, error="timeout")

            session._stt.finish = finish
            exchanges = [x async for x in session.talk()]
            return exchanges, list(session.body.plugin("ring").applied)

    (exchange,), ring = run(scenario())
    assert [a.name for a in ring if a.name == "pulse"], "the ring showed it"
    assert heard(out) == "Lost it."


# --------------------------------------------------------- the listening click


def test_on_a_body_without_echo_cancellation_its_own_click_does_not_start_a_turn(tmp_path):
    """The click plays into the room and the microphone hears it. Two loud
    frames straight after the wake are that echo: without the deaf window
    they start a turn, which then ends on silence one patience later; with
    it, the lead-in keeps waiting for the person."""
    pcm = frame() + frame(PHRASE.encode()) + loud_frame() * 2 + frame() * 40

    def one_turn(latency_s, *, live=True):
        manifest = body_like("pi-speakerphone.yaml", write_wav(tmp_path / f"k{latency_s}{live}.wav", pcm))
        session = talking(manifest)

        async def scenario():
            async with session:
                if latency_s is not None:
                    session._sink.stream_latency_s = latency_s  # a sink playing into a room
                if live:
                    # The file stands in for a microphone: a recording never
                    # heard the click, and a replay deafens nothing.
                    session.source_name = "microphone"
                return [u async for _, u in session.turns()]

        return run(scenario())[0]

    assert one_turn(None).had_speech, "a sink that plays into no room deafens nothing"
    assert one_turn(0.05, live=False).had_speech, "a recording never heard the click"
    deaf = one_turn(0.05)
    assert not deaf.had_speech and deaf.reason.value == "no_speech"


def test_a_chain_override_can_silence_the_click(tmp_path):
    override = tmp_path / "quiet.yaml"
    override.write_text(
        "signal.listening:\n  rungs:\n    - actuator: {voice: true}\n      action: silence\n",
        encoding="utf-8",
    )
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "q.wav", spoken(b"hi"))), chains=[str(override)])

    async def scenario():
        async with session:
            [x async for x in session.talk()]
            return [p for p in session.performed if p.intent.name == "signal.listening"]

    (listening,) = run(scenario())
    assert listening.outcome == "silent"


def test_a_chain_file_that_does_not_load_stops_the_boot_before_the_microphone(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("signal.listening:\n  rungs:\n    - actuator: {role: eyes}\n      action: blink\n", encoding="utf-8")
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "b.wav", frame())), chains=[str(broken)])
    with pytest.raises(EngineError, match="chains could not be loaded"):
        run(session.start())
    assert session._audio is None
    run(session.stop())


# ----------------------------------------------------------- body-local state


def test_the_wake_engine_carries_what_it_learned_to_the_next_boot(tmp_path):
    """Boot one: the mock detector says it learned a mean, and the engine
    keeps it in this body's state file. Boot two: the detector is handed it,
    over the manifest's own value, without anybody editing a manifest."""
    path = tmp_path / "state.json"
    manifest = body_like("pi-speakerphone.yaml", write_wav(tmp_path / "w.wav", frame()))
    manifest["audio"]["wake"]["params"] = {"cmninit": "manual", "carry_over": {"cmninit": "learned"}}

    # A file stands in for the microphone, so the session is told to carry
    # as it would on a live run.
    first = talking(manifest, state=path, carry=True)
    run(first.start())
    run(first.stop())
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert kept["carry"]["wake.mock"] == {"cmninit": "learned"}
    assert kept["body_id"] == "pi_speakerphone"
    assert kept["bindings"]["express.curiosity"]["action"] == "inflect"

    manifest["audio"]["wake"]["params"] = {"cmninit": "manual"}
    second = talking(manifest, state=path, carry=True)

    async def boot():
        async with second:
            return dict(second._wake.params), second.wake_carried

    params, carried = run(boot())
    assert params["cmninit"] == "learned", "the state file wins over the manifest"
    assert carried == ["cmninit"]


def test_boot_records_the_bindings_and_each_parts_health(tmp_path):
    path = tmp_path / "scout.json"
    session = talking(body_like("mock-scout.yaml", write_wav(tmp_path / "s.wav", frame())), state=path)

    async def boot():
        async with session:
            return json.loads(path.read_text(encoding="utf-8"))

    kept = run(boot())
    assert kept["bindings"]["express.curiosity"] == {"target": "head", "action": "tilt", "rung": 0}
    assert kept["health"]["head"]["ok"] is True
    assert set(kept) >= {"doa_reference_deg", "camera_intrinsics", "odometry", "trims", "devices"}


def test_a_manifest_without_a_body_id_keeps_nothing(tmp_path):
    manifest = body_like("bodiless.yaml", write_wav(tmp_path / "n.wav", frame()))
    del manifest["body"]["id"]
    session = talking(manifest)

    async def boot():
        async with session:
            return session.body_state.enabled, session.remember()

    assert run(boot()) == (False, None)


def test_a_driver_that_is_not_installed_stops_the_boot_before_the_microphone(tmp_path):
    manifest = copy.deepcopy(load_yaml(EXAMPLES / "scout-01.yaml"))
    manifest["audio"]["input"] = {"source": "wav", "device": "file", "params": {"path": write_wav(tmp_path / "x.wav", frame())}}
    manifest["audio"]["output"] = {"sink": "null", "device": "none"}
    manifest["audio"]["wake"] = {"engine": "mock"}
    session = ListenSession(manifest, soul())
    with pytest.raises(MissingPluginError):
        run(session.start())
    assert session._audio is None
    run(session.stop())


# ------------------------------------------------------- after the review


def reply_on(session: ListenSession, *deltas: str, stop: str = "end"):
    """Make the session's model stream exactly these deltas."""

    async def scripted(prompt):
        for delta in deltas:
            yield TextDelta(delta)
        yield ReplyDone(text="".join(deltas), stop_reason=stop, model="mock")

    session._llm.reply = scripted


def test_a_printed_reply_has_its_tags_lifted_and_acted_on(tmp_path):
    """`emet-listen --reply` prints the reply and speaks nothing. The prompt
    offers tags there too, so they are lifted out of what is printed and
    performed: the scout's head tilts, and a reserved one is dropped."""
    session = ListenSession(
        body_like("mock-scout.yaml", write_wav(tmp_path / "p.wav", frame())), soul(), transcribe=True, reply=True
    )

    async def scenario():
        async with session:
            reply_on(session, "[express.curiosity] What ", "was that? [manipulate.grasp]")
            events = await drain(session.answer_acted("listen"))
            return events, list(session.body.plugin("head").applied), session.last_intents

    events, applied, intents = run(scenario())
    printed = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert printed == "What was that? " and "[" not in printed
    assert [a.name for a in applied] == ["tilt"]
    assert [i.name for i in intents] == ["express.curiosity"]
    assert isinstance(events[-1], ReplyDone)


@pytest.mark.parametrize(
    "deltas,expected",
    [
        (("Well... [express.thinking] Let me see.",), ["Well...", "hmm.", "Let me see."]),
        (("Nice to meet you.[express.delight] What is your name?",), ["Nice to meet you.", "oh!", "What is your name?"]),
        (("That is great news![express.delight]",), ["That is great news!", "oh!"]),
        (("Okay, [express.curiosity] why?",), ["hm?", "Okay, why?"]),
        (("Ask Dr.", "[express.curiosity]", " Smith now."), ["hm?", "Ask Dr. Smith now."]),
        (("It costs 3.", "[express.curiosity]", "50 dollars."), ["hm?", "It costs 3.50 dollars."]),
    ],
)
def test_a_filler_lands_after_a_sentence_already_ended_and_before_the_one_it_is_in(tmp_path, deltas, expected):
    """A sentence whose end mark is written and not yet confirmed by the next
    word ("Well..." waiting for a capital, a tag glued on after "news!") is
    said before the tag's filler. A tag inside a sentence is heard before
    that sentence, which is the unit of speech, and "Dr." or "3." before a
    tag is inside one: the splitter's own rules decide."""
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "o.wav", frame())))

    async def scenario():
        async with session:
            reply_on(session, *deltas)
            await drain(session.answer_aloud("go on"))
            return session.last_spoken

    assert [s.text for s in run(scenario())] == expected


def test_a_tag_cut_short_by_the_token_cap_is_neither_printed_nor_said(tmp_path):
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "c.wav", frame())))

    async def scenario():
        async with session:
            reply_on(session, "Of course. ", "[express.del", stop="length")
            events = await drain(session.answer_aloud("hi"))
            return events, session.last_spoken

    events, spoken = run(scenario())
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Of course. "
    assert [s.text for s in spoken] == ["Of course."]
    assert isinstance(events[-1], ReplyDone), "ReplyDone stays last"


def test_an_open_bracket_that_is_not_a_tag_is_printed_as_it_is_said(tmp_path):
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "b.wav", frame())))

    async def scenario():
        async with session:
            reply_on(session, "It is in section ", "[4b")
            events = await drain(session.answer_aloud("where"))
            return events, session.last_spoken

    events, spoken = run(scenario())
    printed = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert printed == "It is in section [4b"
    assert " ".join(s.text for s in spoken) == printed
    assert isinstance(events[-1], ReplyDone)


def test_booted_and_said_so_before_the_microphone_opens(tmp_path):
    """The rising pair is not in the first frames the wake engine hears, and
    no backlog builds while it plays."""
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "m.wav", frame())))
    mic_open_at_boot: list[bool] = []
    session.on_performed = lambda p: mic_open_at_boot.append(session._audio is not None) if p.intent.name == "signal.booting" else None

    async def scenario():
        async with session:
            return session._audio is not None

    assert run(scenario()) is True
    assert mic_open_at_boot == [False]


def test_a_source_that_fails_to_stop_still_lets_every_part_rest(tmp_path, monkeypatch):
    from emet_hal import audio, mock

    stopped: list[str] = []
    real = mock.MockActuator.shutdown

    async def shutdown(self):
        stopped.append(self.capability_id)
        await real(self)

    async def broken_stop(self):
        raise audio.AudioError("the USB microphone went away")

    monkeypatch.setattr(mock.MockActuator, "shutdown", shutdown)
    monkeypatch.setattr(audio.WavSource, "stop", broken_stop)
    session = talking(body_like("mock-scout.yaml", write_wav(tmp_path / "s.wav", frame())))

    async def scenario():
        async with session:
            pass

    with pytest.raises(audio.AudioError):
        run(scenario())
    assert sorted(stopped) == ["base", "eyes", "head", "ring"]
    assert session._wake is None, "the wake engine was shut down too"


def test_interrupted_mid_line_the_voice_is_hushed_before_the_muted_tone(tmp_path):
    """Ctrl-C while the robot says its failure line. The rest of the line is
    dropped at once, so the falling pair is not queued behind it."""
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "h.wav", frame())))
    order: list[str] = []

    async def scenario():
        async with session:
            real_cancel = session._sink.cancel

            async def cancel():
                order.append("hush")
                await real_cancel()

            session._sink.cancel = cancel
            session._actor.on_performed = lambda p: order.append(p.intent.name) if p.intent.kind == "signal" else None
            task = asyncio.ensure_future(session.speak_text("One. Two. Three. Four. Five."))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not session._mouth.is_open, "the interrupted line was hushed"

    run(scenario())
    assert order.index("hush") < order.index("signal.muted")


def test_a_second_cancel_on_the_way_down_still_stops_every_piece(tmp_path):
    """Ctrl-C, then SIGTERM while the falling pair plays. The rest of the
    shutdown still runs, and the cancellation is passed on once it has."""
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "d.wav", frame())))
    stopped: list[str] = []

    async def scenario():
        await session.start()
        sink, source, wake = session._sink, session._audio, session._wake

        async def stuck(pcm):
            await asyncio.sleep(5)

        sink.play = stuck
        for name, piece, method in (("sink", sink, "stop"), ("source", source, "stop"), ("wake", wake, "shutdown")):

            async def record(real=getattr(piece, method), name=name):
                stopped.append(name)
                await real()

            setattr(piece, method, record)
        task = asyncio.ensure_future(session.stop())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert stopped == ["sink", "source", "wake"]


def test_a_tone_cut_off_part_way_is_dropped_from_the_sink(tmp_path):
    """Ctrl-C during the boot chime. Its tail is dropped at once, so the sink
    holds nothing `stop()` would take for a speaker that stopped asking for
    audio, and the falling pair still plays."""
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "t.wav", frame())))

    async def scenario():
        async with session:
            sink = session._sink
            real_play, real_cancel = sink.play, sink.cancel
            cancelled: list[bool] = []

            async def stuck(pcm):
                await asyncio.sleep(5)

            async def cancel():
                cancelled.append(True)
                await real_cancel()

            sink.play, sink.cancel = stuck, cancel
            task = asyncio.ensure_future(session.say(bytes(640)))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            sink.play = real_play
            return cancelled

    assert run(scenario()) == [True]


def test_a_filler_is_heard_and_not_counted_as_a_sentence_of_the_reply(tmp_path):
    session = talking(body_like("bodiless.yaml", write_wav(tmp_path / "n.wav", frame())))

    async def scenario():
        async with session:
            reply_on(session, "[express.curiosity] Well. That is odd.")
            await drain(session.answer_aloud("look"))
            return session.last_spoken, session.stats

    spoken, stats = run(scenario())
    assert [s.text for s in spoken] == ["hm?", "Well.", "That is odd."]
    assert [s.from_reply for s in spoken] == [False, True, True]
    assert stats.sentences_spoken == 2


def test_the_replayed_body_is_the_body_the_live_run_described():
    """A replay swaps the source and keeps what the rest of `audio.input` says
    about the body, which the self-model reads."""
    from emet_engine.cli import _replay

    manifest = copy.deepcopy(load_yaml(EXAMPLES / "mock-scout.yaml"))
    replayed = _replay(manifest, "recording.wav")["audio"]["input"]
    assert replayed["source"] == "wav" and replayed["params"] == {"path": "recording.wav"}
    assert (replayed["channels"], replayed["doa"], replayed["aec"]) == (4, True, "hardware")
