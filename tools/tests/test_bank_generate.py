"""The generated bank's builder, `tools/bank/generate.py`, and `check.py`'s rules for it.

Nothing here needs Piper, numpy or the network. The voice is a fake that
gives a tone for each text, the voice list is a fake whose cards are
written here, and the rooms are tiny, so the plain-Python arithmetic builds
a bank in seconds. A test that needs the built bank (`Project Emet/genbank/`,
or `EMET_GENBANK`) skips without it, as the real bank's tests do.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import sys
import tarfile
from pathlib import Path

import pytest

import check as bank_check
import generate as gen
import manifest as bank_files

REPO = Path(__file__).resolve().parents[2]
REV = "0" * 40
HOST = "https://example.com/voices"


# ================================================================ fixtures


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_card(name: str, training: str = "Trained from scratch on tones, for the tests.",
              licence: str = "public domain") -> str:
    return (f"# Model card for {name}\n\n## Dataset\n\n* URL: https://example.com/{name}\n"
            f"* License: {licence}\n\n## Training\n\n{training}\n")


def voice_files(name: str, card: str) -> dict[str, bytes]:
    return {"model": f"model of {name}".encode(), "config": b"{}", "card": card.encode("utf-8")}


def fake_voices(names: list[str]) -> tuple[dict, dict[str, dict[str, bytes]]]:
    """A voice list in the pinned list's format, and each voice's three files."""
    doc = {
        "format": "emet-generated-voices/1", "read": "2026-11-26", "note": "for the tests",
        "engine": {"name": "piper-tts", "version": "0.0.0", "licence": "GPL-3.0-or-later",
                   "source": "https://example.com/piper", "runtime": "onnxruntime 0.0.0, MIT"},
        "list": {"url": "https://example.com/COMMERCIAL_USE.md", "commit": REV, "committed": "2026-11-23",
                 "sha256": "0" * 64},
        "repository": HOST, "revision": REV, "voices": [], "bases": {},
    }
    blobs = {}
    for i, name in enumerate(names, 1):
        card = fake_card(name)
        files = voice_files(name, card)
        blobs[name] = files
        doc["voices"].append({
            "id": f"g{i:02d}", "name": name, "describes": f"test voice {i}",
            "files": {key: {"name": fname, "url": f"{HOST}/resolve/{REV}/{fname}", "sha256": sha(files[key]),
                            "bytes": len(files[key])}
                      for key, fname in (("model", f"{name}.onnx"), ("config", f"{name}.onnx.json"),
                                         ("card", f"{name}.MODEL_CARD"))},
            "dataset": f"https://example.com/{name}", "licence": "public domain",
            "training": "Trained from scratch on tones, for the tests.", "base": None,
            "listed": "Voices trained from scratch: Public domain", "credit": f"Tones, for test voice {i}.",
        })
    return doc, blobs


ROOMS = {
    "format": "emet-generated-rooms/1", "speed_of_sound_m_s": 343, "gain_at_1m_q15": 8192,
    "rooms": {"sim-box": {"description": "a simulated test box", "size_m": [3.0, 2.5, 2.4], "absorption": 0.6,
                          "length_s": 0.04}},
    "conditions": [
        {"id": "near", "room": "sim-box", "microphone_m": [1.0, 1.0, 1.0], "source_m": [1.5, 1.2, 1.2],
         "distance": "near", "noise": {"kind": "pink", "rms": 58, "level_dbfs": -55.0}},
        {"id": "far", "room": "sim-box", "microphone_m": [0.5, 0.5, 1.0], "source_m": [2.5, 2.0, 1.2],
         "distance": "about 2 m", "noise": None},
    ],
}

PHRASES = {
    "format": "emet-generated-phrases/1", "wake": "hey emet", "say_as": {},
    "groups": [
        {"id": "one-breath", "kind": "one_breath", "slot_s": 2.0, "source": "the tests",
         "phrases": [{"id": "ob01", "text": "hey emet, what time is it", "question": "what time is it",
                      "first_sound": "w"},
                     {"id": "ob02", "text": "hey emet, find my phone", "question": "find my phone",
                      "first_sound": "f"}]},
        {"id": "wake-pause", "kind": "wake_pause", "slot_s": 2.5, "pause_s": 0.3, "source": "the tests",
         "phrases": [{"id": "wp01", "text": "hey emet ... what time is it", "question": "what time is it",
                      "first_sound": "w"}]},
        {"id": "decoy", "kind": "decoy", "slot_s": 2.0, "source": "the tests",
         "phrases": [{"id": "de01", "text": "helmet", "question": None, "first_sound": None}]},
    ],
}

SETS = {
    "format": "emet-generated-sets/1", "version": "2026-11-26", "seed": 7, "parts": 2, "lead_s": 5.0,
    "length_scales": [1.0, 1.12], "sentence_silence_s": 0.2,
    "sets": {"small": {"rule": "one-a-condition", "for": "the tests"},
             "full": {"rule": "every-cell", "for": "the tests"}},
}


def fake_voice(voice: dict, length_scale: float, texts) -> list[tuple[int, bytes]]:
    """A tone for each text at 22050 Hz: its pitch from the voice and the
    text, its length from the text and the rate. The same every time."""
    out = []
    for text in texts:
        seed = int(sha(f"{voice['name']}|{text}".encode())[:6], 16)
        hz = 180 + seed % 400
        n = int((0.2 + 0.02 * len(text)) * float(length_scale) * 22050)
        samples = [int(6000 * math.sin(2 * math.pi * hz * i / 22050)) for i in range(n)]
        out.append((22050, b"".join(int.to_bytes(v & 0xFFFF, 2, "little") for v in samples)))
    return out


class Lists:
    """The four lists written to a folder, read back through `load_inputs`."""

    def __init__(self, root: Path, names=("tone-a", "tone-b"), rooms=None, phrases=None, sets=None) -> None:
        self.data = root / "generated"
        self.data.mkdir(parents=True)
        voices, self.blobs = fake_voices(list(names))
        for name, doc in (("voices", voices), ("rooms", rooms or ROOMS), ("phrases", phrases or PHRASES),
                          ("sets", sets or SETS)):
            (self.data / f"{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        self.voices_dir = root / "voices"
        self.voices_dir.mkdir()
        for name, files in self.blobs.items():
            for key, suffix in (("model", ".onnx"), ("config", ".onnx.json"), ("card", ".MODEL_CARD")):
                (self.voices_dir / f"{name}{suffix}").write_bytes(files[key])

    def inputs(self) -> dict:
        return gen.load_inputs(self.data)

    def cards(self) -> dict[str, str]:
        return {name: files["card"].decode("utf-8") for name, files in self.blobs.items()}


def build(lists: Lists, out: Path, set_name: str = "small", inputs: dict | None = None) -> dict:
    inputs = inputs or lists.inputs()
    return gen.build(out, set_name, inputs, fake_voice, engine={"name": "fake-voice", "version": "1"},
                     cards=lists.cards(), be=gen.backend(plain=True), say=lambda _line: None)


@pytest.fixture
def lists(tmp_path) -> Lists:
    return Lists(tmp_path / "lists")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One tiny small set, built once for the tests that only read it."""
    root = tmp_path_factory.mktemp("built")
    lists = Lists(root / "lists")
    build(lists, root / "bank")
    return lists, root / "bank"


def check_lines(bank: Path, lists: Lists) -> tuple[list[str], int]:
    lines, _good, bad = bank_check.check(bank, [], False, voices=lists.inputs()["voices"])
    return lines, bad


def audio_hashes(bank: Path) -> dict[str, str]:
    return {doc["id"]: doc["sha256"] for _p, doc in bank_files.manifests(bank)}


# ============================================ the lists in the repository


def test_the_lists_in_the_repository_are_sound_and_every_voice_is_an_allowed_one():
    inputs = gen.load_inputs()
    assert not bank_check.voice_list_problems(inputs["voices"])
    names = [v["name"] for v in inputs["voices"]["voices"]]
    assert "en_US-ljspeech-medium" in names, "the reference voice, LJ Speech, is in the list"
    for voice in inputs["voices"]["voices"]:
        assert bank_check.licence_allowed(voice["licence"])
        assert "lessac" not in voice["name"] and "amy" not in voice["name"]
        assert voice["files"]["model"]["url"].startswith(f"{inputs['voices']['repository']}/resolve/"
                                                         f"{inputs['voices']['revision']}/")


def test_the_repository_small_set_says_every_phrase_once_and_is_part_of_the_full_set():
    inputs = gen.load_inputs()
    small, full = gen.plan(inputs, "small"), gen.plan(inputs, "full")
    said = [t["phrase"] for rec in small for t in rec["takes"]]
    every = [p["id"] for _g, p in gen.numbered_phrases(inputs)]
    assert sorted(said) == sorted(every) and len(said) == len(set(said))
    by_id = {rec["id"]: rec for rec in full}
    for rec in small:
        assert rec == by_id[rec["id"]], "a small recording is one of full's, take for take"
    assert len(full) == (len(inputs["voices"]["voices"]) * len(inputs["rooms"]["conditions"])
                         * inputs["sets"]["parts"])


def test_the_lead_before_the_first_take_is_long_enough_for_the_decoder():
    sets = dict(SETS, lead_s=3.0)
    found = gen.input_problems({"rooms": ROOMS, "phrases": PHRASES, "sets": sets})
    assert any("lead_s is at least 5 s" in p for p in found)


# =================================================== the two sizes' rule


def test_both_sizes_come_from_one_rule(lists):
    inputs = lists.inputs()
    full, small = gen.plan(inputs, "full"), gen.plan(inputs, "small")
    assert len(full) == 2 * 2 * 2
    assert [(r["voice"]["id"], r["condition"]["id"], r["part"]) for r in small] == [("g01", "near", 1),
                                                                                  ("g02", "far", 2)]
    assert {r["id"] for r in small} <= {r["id"] for r in full}
    parts = {r["part"]: [t["phrase"] for t in r["takes"]] for r in small}
    assert parts == {1: ["ob01", "wp01"], 2: ["ob02", "de01"]}, "part k holds every phrase numbered k modulo parts"
    assert [(t["phrase"], t["expect_wake"]) for r in small for t in r["takes"]] == [
        ("ob01", True), ("wp01", True), ("ob02", True), ("de01", False)], "a decoy should not wake it"


def test_a_takes_speaking_rate_steps_with_its_phrase_its_voice_and_its_condition(lists):
    """sets.json's rule: length_scales[(phrase + voice + condition) modulo
    their count], the phrase numbered through the list from 0 (ob01 0, ob02
    1, wp01 2, de01 3), so the rates are spread over voices and rooms."""
    full = gen.plan(lists.inputs(), "full")
    got = {(r["voice"]["id"], r["condition"]["id"], t["phrase"]): t["length_scale"] for r in full for t in r["takes"]}
    number = {"ob01": 0, "ob02": 1, "wp01": 2, "de01": 3}
    for (vid, cid, pid), scale in got.items():
        i, j = int(vid[1:]) - 1, ["near", "far"].index(cid)
        assert scale == SETS["length_scales"][(number[pid] + i + j) % 2], (vid, cid, pid)
    assert got[("g01", "near", "ob01")] == 1.0 and got[("g02", "near", "ob01")] == 1.12
    assert got[("g01", "far", "ob01")] == 1.12 and got[("g02", "far", "ob02")] == 1.12


def test_a_set_the_list_does_not_have_is_refused(lists):
    with pytest.raises(gen.GenerateError, match="no set is called 'weekly'"):
        gen.plan(lists.inputs(), "weekly")


def test_a_take_is_said_as_pieces_with_its_pause_between():
    group, phrase = PHRASES["groups"][1], PHRASES["groups"][1]["phrases"][0]
    assert gen.segments(group, phrase, {}) == [{"say": "Hey emet."}, {"pause_s": 0.3}, {"say": "What time is it?"}]
    decoy = PHRASES["groups"][2]
    assert gen.segments(decoy, decoy["phrases"][0], {}) == [{"say": "Helmet."}]
    assert gen.spoken("hey emet, read it", ".", {"emet": "emmet"}) == "Hey emmet, read it."


# ======================================================= the pinned hash


def test_a_voice_file_whose_hash_is_not_the_pinned_one_is_refused(lists):
    inputs = lists.inputs()
    assert set(gen.verify_voices(inputs, lists.voices_dir)) == {"tone-a", "tone-b"}
    (lists.voices_dir / "tone-a.onnx").write_bytes(b"a retrained voice under the same name")
    with pytest.raises(gen.GenerateError, match="tone-a.onnx has sha256 .* not the file that was read"):
        gen.verify_voices(inputs, lists.voices_dir)
    assert set(gen.verify_voices(inputs, lists.voices_dir, ["tone-b"])) == {"tone-b"}, \
        "a set that does not use the voice does not need it"


@pytest.mark.parametrize("suffix, other", [
    (".onnx.json", b'{"audio": {"sample_rate": 16000}}'),
    (".MODEL_CARD", fake_card("tone-a").replace("# Model card", "# A card").encode("utf-8")),
])
def test_a_config_or_a_card_whose_hash_is_not_the_pinned_one_is_refused(lists, suffix, other):
    """The config goes to Piper as `-c` (its rate and phoneme map), and the
    card is what the licence was read from: each is held to its pin as the
    model is, and the refusal says how to fetch the pinned files."""
    (lists.voices_dir / f"tone-a{suffix}").write_bytes(other)
    with pytest.raises(gen.GenerateError, match=rf"tone-a{re.escape(suffix)} has sha256 .* not the file") as caught:
        gen.verify_voices(lists.inputs(), lists.voices_dir)
    assert "fetch the pinned files with: python tools/bank/generate.py --fetch-voices" in str(caught.value)


def test_a_missing_voice_file_is_refused(lists):
    (lists.voices_dir / "tone-b.onnx.json").unlink()
    with pytest.raises(gen.GenerateError, match="no tone-b.onnx.json"):
        gen.verify_voices(lists.inputs(), lists.voices_dir)


def test_a_card_whose_training_is_not_the_lists_is_refused_by_the_builder(lists):
    """verify_voices reads each card it is given: a list whose Training
    words differ from the card's refuses the voice, though every hash is
    the pinned one."""
    inputs = lists.inputs()
    inputs["voices"]["voices"][0]["training"] = "Trained from scratch on other tones."
    with pytest.raises(gen.GenerateError, match="tone-a: the card's Training section reads") as caught:
        gen.verify_voices(inputs, lists.voices_dir)
    assert "--fetch-voices" not in str(caught.value), "a pinned file fetched again would not change the card"


def test_a_card_that_names_a_lessac_base_or_a_non_commercial_licence_is_refused():
    voice = fake_voices(["tone-a"])[0]["voices"][0]
    assert bank_check.card_problems(fake_card("tone-a"), voice) == []
    lessac = fake_card("tone-a", training="Finetuned from U.S. English lessac voice.")
    found = bank_check.card_problems(lessac, {**voice, "training": "Finetuned from U.S. English lessac voice."})
    assert any("fine-tuned from lessac or ryan" in p for p in found)
    nc = fake_card("tone-a", licence="CC BY-NC-SA 4.0")
    assert any("not one a generated bank may use" in p
               for p in bank_check.card_problems(nc, {**voice, "licence": "CC BY-NC-SA 4.0"}))
    assert not bank_check.licence_allowed("CC BY-NC 4.0") and bank_check.licence_allowed("CC0")


def test_an_allowed_licence_with_a_non_commercial_or_no_derivatives_tail_is_refused():
    """An allowed form may be followed by more words, so the words that take
    commercial use or derivatives away are refused on their own."""
    assert bank_check.licence_allowed("CC BY 4.0") and bank_check.licence_allowed("CC BY 4.0 (a dataset)")
    for licence in ("CC BY 4.0 non-commercial", "CC BY 4.0 NC", "CC BY 4.0 ND", "cc by 4.0 research only",
                    "public domain (non-commercial use only)"):
        assert not bank_check.licence_allowed(licence), licence


def test_a_voice_with_no_base_must_say_it_was_trained_from_scratch():
    assert bank_check.training_problems("Trained from scratch on tones.", None) == []
    assert bank_check.training_problems("Finetuned from an unlisted voice.", None) == [
        "the list records no base, and the Training section does not say trained from scratch"]


def test_a_card_whose_facts_are_not_the_lists_is_refused():
    voice = fake_voices(["tone-a"])[0]["voices"][0]
    found = bank_check.card_problems(fake_card("tone-a", licence="CC BY 4.0"), voice)
    assert found == ["the card's License line reads 'CC BY 4.0', and the list records 'public domain'"]


def test_a_fine_tuned_voice_must_name_the_base_the_list_records():
    doc = fake_voices(["tone-a"])[0]
    doc["bases"] = {"b": {"name": "base-b", "dataset": "https://example.com/b", "licence": "CC0",
                          "training": "Trained from scratch on tones.", "credit": "Base b's tones.",
                          "card": {"url": f"{HOST}/resolve/{REV}/b/MODEL_CARD", "sha256": "0" * 64}}}
    voice = doc["voices"][0]
    voice["base"] = "b"
    assert any("does not say the voice is fine-tuned from b" in p for p in bank_check.voice_list_problems(doc))
    voice["training"] = "Finetuned from the b base model on tones."
    assert bank_check.voice_list_problems(doc) == []


def test_a_voice_file_away_from_the_pinned_revision_is_refused():
    doc = fake_voices(["tone-a"])[0]
    doc["voices"][0]["files"]["model"]["url"] = f"{HOST}/resolve/main/tone-a.onnx"
    assert bank_check.voice_list_problems(doc) == [
        f"voices: tone-a: the model's url is not at the pinned revision, {HOST}/resolve/{REV}/"]


def test_a_term_a_dataset_adds_is_text_with_its_url_and_the_day_it_was_read():
    doc = fake_voices(["tone-a"])[0]
    doc["voices"][0]["terms"] = {"text": "No use derogatory to the reader.", "url": "https://example.com/t",
                                 "read": "2026-11-26"}
    assert bank_check.voice_list_problems(doc) == []
    for bad in ("No use derogatory to the reader.", {"text": "x", "url": "https://example.com/t"},
                {"text": "x", "url": "https://example.com/t", "read": "26 November"}):
        doc["voices"][0]["terms"] = bad
        assert any("terms, when given" in p for p in bank_check.voice_list_problems(doc)), bad


def test_the_alba_corpus_term_is_in_the_list_as_its_licence_file_words_it():
    """The Alba corpus's own licence file (datashare.ed.ac.uk, 10283/3270,
    license_text.txt, read 2026-11-26) adds a sentence to CC BY 4.0, which
    the bank's ATTRIBUTION.md must carry."""
    alba = next(v for v in gen.load_inputs()["voices"]["voices"] if v["name"] == "en_GB-alba-medium")
    assert alba["terms"]["text"] == "Any use derogatory to the voice talent is prohibited."
    assert alba["terms"]["url"] == "https://datashare.ed.ac.uk/bitstream/handle/10283/3270/license_text.txt"


def test_fetch_voices_keeps_only_a_file_with_the_pinned_hash(lists, tmp_path):
    """The download side, run against file URLs, keeps no wrong file."""
    inputs = lists.inputs()
    for voice in inputs["voices"]["voices"]:
        for key in ("model", "config", "card"):
            voice["files"][key]["url"] = (lists.voices_dir / voice["files"][key]["name"]).as_uri()
    target = tmp_path / "fetched"
    gen.fetch_voices(inputs, target, say=lambda _l: None)
    assert gen.verify_voices(inputs, target)
    inputs["voices"]["voices"][0]["files"]["model"]["sha256"] = "1" * 64
    (target / "tone-a.onnx").unlink()
    with pytest.raises(gen.GenerateError, match="nothing was kept"):
        gen.fetch_voices(inputs, target, say=lambda _l: None)
    assert not (target / "tone-a.onnx").exists() and not (target / "tone-a.onnx.part").exists()


# ================================================ the arithmetic, the room


def test_the_sine_series_is_the_maths_librarys_sine():
    for num in range(-50, 51):
        assert abs(gen._sin_pi(num, 17) - math.sin(math.pi * num / 17)) < 1e-14


def test_the_resampler_keeps_a_constant_and_each_phase_sums_to_one():
    kernel = gen.resample_kernel(320, 441)
    assert len(kernel) == 320 and all(sum(row) == gen.ONE for row in kernel)
    out = gen.resample([1000] * 2000, 22050, 16000, be=gen.backend(plain=True))
    assert len(out) == (2000 * 320 + 440) // 441
    assert set(out[40:-40]) == {1000}


def test_a_room_response_is_whole_numbers_with_the_direct_sound_where_it_belongs():
    size, src, mic = [3.0, 2.5, 2.4], [1.5, 1.2, 1.2], [1.0, 1.0, 1.0]
    taps = gen.room_response(size, src, mic, 0.6, 0.04, 8192)
    assert taps == gen.room_response(size, src, mic, 0.6, 0.04, 8192)
    assert all(isinstance(v, int) for v in taps)
    d = gen.distance_m(src, mic)
    direct = int(d * 16000 / 343 + 0.5)
    assert taps[:direct] == [0] * direct, "nothing arrives before the direct sound"
    assert taps[direct] >= round(8192 / d) - 1, "the direct sound is the gain over the distance"
    assert sum(1 for v in taps[direct + 1:] if v) > 10, "the walls give reflections after it"
    assert gen.room_response(size, src, mic, 0.9, 0.04, 8192)[direct + 1:] != taps[direct + 1:]


def test_a_room_refuses_a_talker_or_a_microphone_outside_it():
    with pytest.raises(gen.GenerateError, match="must stand inside the room"):
        gen.room_response([3.0, 2.5, 2.4], [3.5, 1.2, 1.2], [1.0, 1.0, 1.0], 0.6, 0.04, 8192)
    with pytest.raises(gen.GenerateError, match="absorption is between 0 and 1"):
        gen.room_response([3.0, 2.5, 2.4], [1.5, 1.2, 1.2], [1.0, 1.0, 1.0], 1.0, 0.04, 8192)


def test_the_decay_is_read_from_the_response():
    # A tail that falls 60 dB in 300 ms, 6 dB each 30 ms: 10**(-6/20) a 480 samples.
    taps = [int(20000 * 10 ** (-60 * (i / 16000) / 0.3 / 20)) for i in range(16000)]
    assert abs(gen.decay_ms(taps) - 300) <= 5
    assert gen.decay_ms([1000]) is None


def test_pink_noise_is_the_same_from_the_same_seed_and_at_its_level():
    be = gen.backend(plain=True)
    a, b = be.pink(48000, 12345, 58), be.pink(48000, 12345, 58)
    assert a == b and a != be.pink(48000, 12346, 58)
    rms = math.sqrt(sum(v * v for v in a) / len(a))
    assert 40 < rms < 80


def test_a_noise_level_that_is_not_its_rms_is_refused():
    assert bank_check.noise_problems({"kind": "pink", "rms": 58, "level_dbfs": -55.0}) == []
    assert bank_check.noise_problems({"kind": "pink", "rms": 58, "level_dbfs": -40.0})
    assert bank_check.noise_problems({"kind": "white", "rms": 58, "level_dbfs": -55.0})


def test_numpy_gives_the_same_whole_numbers_as_plain_python():
    """The published bank was built with numpy and these tests build with
    plain Python, so this is the test that holds the two to the same
    integers. Neither the shared venv nor CI's test jobs hold numpy (its
    licence expression is outside the test jobs' gate), so it skips there.
    It ran on 2026-11-26 with numpy 2.5.4 (tools/bank/requirements.txt's pin)
    and 2.5.5 (the build's) put on the path of the shared venv's pytest;
    a CI step that installs numpy beside pytest in a venv of its own is the
    one that would hold it on every change."""
    np = pytest.importorskip("numpy", reason="numpy is optional: the builder's fast arithmetic")
    plain, fast = gen.backend(plain=True), gen.backend()
    x = [int(9000 * math.sin(2 * math.pi * (300 + 2000 * i / 6615) * i / 22050)) for i in range(6615)]
    assert plain.resample(x, 320, 441) == fast.resample(x, 320, 441).tolist()
    taps = gen.room_response([3.0, 2.5, 2.4], [1.5, 1.2, 1.2], [1.0, 1.0, 1.0], 0.6, 0.04, 8192)
    sig = plain.resample(x, 320, 441)
    assert plain.convolve(sig, taps) == fast.convolve(np.asarray(sig), taps).tolist()
    assert plain.pink(9000, 5, 58) == fast.pink(9000, 5, 58).tolist()


# ============================================================ the build


def test_a_built_set_passes_check_and_says_what_made_every_take(built):
    lists, bank = built
    lines, bad = check_lines(bank, lists)
    assert bad == 0, lines
    assert lines[-1].endswith("generated")
    index = bank_files.read(bank / "bank.json", "bank")
    assert index["speakers"] == {"g01": {"voice": "voices/g01.json"}, "g02": {"voice": "voices/g02.json"}}
    for _path, doc in bank_files.manifests(bank):
        made = doc["generated"]
        assert set(made["takes"]) == {t["id"] for t in doc["takes"]}
        assert made["engine"]["noise_scale"] == 0 and made["engine"]["noise_w_scale"] == 0
        assert made["room"]["taps_sha256"] and made["seed"] == SETS["seed"]
        assert doc["sha256"] == bank_files.sha256_file(bank / doc["path"])
    for name in ("DATASHEET.md", "ATTRIBUTION.md", "SHA256SUMS", "voices/g01.json", "voices/tone-a.MODEL_CARD"):
        assert (bank / name).is_file(), name
    decoys = [t for _p, doc in bank_files.manifests(bank) for t in doc["takes"] if t["kind"] == "decoy"]
    assert decoys and not any(t["expect_wake"] for t in decoys), "a decoy should not wake it"


def test_the_datasheet_says_what_it_may_not_decide_and_names_no_local_path(built):
    _lists, bank = built
    text = (bank / "DATASHEET.md").read_text(encoding="utf-8")
    assert "It may not set or move a default." in text
    assert "tools/bank/replay.py BANK --golden tools/bank/golden/generated" in text
    assert str(bank) not in text and str(Path.home()) not in text
    every = "".join(p.read_text(encoding="utf-8") for p in bank.rglob("*.json"))
    assert str(Path.home()).replace("\\", "\\\\") not in every and str(Path.home()) not in every


def test_the_datasheet_promises_only_the_checks_a_tool_makes(built):
    """A rebuild with other bytes has no tool to hold it, a platform with
    no goldens holds the bank to nothing, BANK is named, and the frame
    phases are counted, not claimed."""
    _lists, bank = built
    text = (bank / "DATASHEET.md").read_text(encoding="utf-8")
    assert text.startswith("# The generated bank's small set, version 2026-11-26\n")
    assert "held to its replay" not in text and "every phase" not in text and "no person's voice" not in text
    assert "no tool yet holds the rebuild to this bank" in text and "a rebuild with other bytes is unverified" in text
    assert "--golden tools/bank/golden/generated --update --platform PLATFORM" in text
    assert "every recording prints `NO GOLDEN` and the replay exits 1" in text
    assert text.count("# BANK is this set's folder, the one that holds this datasheet "
                      "(genbank-small-2026-11-26 once --fetch unpacks it).") == 2
    phases = {r["at_sample"] % gen.FRAME_SAMPLES for _p, doc in bank_files.manifests(bank)
              for r in doc["generated"]["takes"].values()}
    assert f"{len(phases)} of its 1,280 sample phases in this set" in text
    assert "COMMERCIAL_USE.md takes no position on it" in text


def test_the_bank_names_the_engine_that_spoke_where_the_list_pinned_another(built):
    """The tests' fake voice is not the list's Piper: ATTRIBUTION.md and the
    voice records say what spoke, as the manifests do."""
    _lists, bank = built
    assert "made by fake-voice 1 (GPL-3.0-or-later)" in (bank / "ATTRIBUTION.md").read_text(encoding="utf-8")
    record = json.loads((bank / "voices" / "g01.json").read_text(encoding="utf-8"))
    manifest = next(doc for _p, doc in bank_files.manifests(bank) if doc["speaker"] == "g01")
    assert record["engine"] == manifest["generated"]["engine"] and record["engine"]["name"] == "fake-voice"


def test_a_term_a_dataset_adds_reaches_the_attribution_and_the_datasheet(tmp_path):
    lists = Lists(tmp_path / "lists")
    path = lists.data / "voices.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    terms = {"text": "No use derogatory to the reader.", "url": "https://example.com/terms", "read": "2026-11-26"}
    doc["voices"][1]["terms"] = terms
    path.write_text(json.dumps(doc), encoding="utf-8")
    build(lists, tmp_path / "bank")
    bank = tmp_path / "bank"
    attribution = (bank / "ATTRIBUTION.md").read_text(encoding="utf-8")
    assert f'- Its dataset adds: "{terms["text"]}" ({terms["url"]}, read 2026-11-26)' in attribution
    assert f"tone-b's dataset adds to its licence: \"{terms['text']}\"" in (bank / "DATASHEET.md").read_text(
        encoding="utf-8")
    assert json.loads((bank / "voices" / "g02.json").read_text(encoding="utf-8"))["terms"] == terms
    (bank / "ATTRIBUTION.md").write_text(attribution.replace(terms["text"], "Be kind."), encoding="utf-8")
    gen.write_sums(bank)
    lines, bad = check_lines(bank, lists)
    assert bad and "ATTRIBUTION.md does not quote the term tone-b's dataset adds" in lines


def test_everything_after_the_voice_is_the_same_bytes_twice(built, tmp_path):
    lists, first = built
    second = tmp_path / "again"
    build(lists, second)
    assert (first / "SHA256SUMS").read_text(encoding="utf-8") == (second / "SHA256SUMS").read_text(encoding="utf-8")
    lines, differ = gen.compare(first, second)
    assert differ == 0 and lines[-1] == "compare: 2 of 2 recording(s) the same"


def test_another_seed_is_other_audio_and_compare_says_so(built, tmp_path):
    lists, first = built
    inputs = lists.inputs()
    inputs["sets"]["seed"] = SETS["seed"] + 1
    other = tmp_path / "seed"
    build(lists, other, inputs=inputs)
    assert set(audio_hashes(first).values()).isdisjoint(audio_hashes(other).values())
    lines, differ = gen.compare(first, other)
    assert differ == 2 and all("DIFFERENT audio" in line for line in lines[:2])


def test_the_small_set_is_the_full_sets_recordings_byte_for_byte(built, tmp_path):
    lists, small = built
    full = tmp_path / "full"
    build(lists, full, "full")
    whole, part = audio_hashes(full), audio_hashes(small)
    assert len(whole) == 8 and len(part) == 2
    assert {rid: whole[rid] for rid in part} == part


def test_a_build_goes_into_a_new_folder_only(built, lists):
    _l, bank = built
    with pytest.raises(gen.GenerateError, match="is not empty"):
        build(lists, bank)


def test_a_take_longer_than_its_slot_is_refused(lists, tmp_path):
    phrases = json.loads(json.dumps(PHRASES))
    phrases["groups"][0]["slot_s"] = 1.0
    tight = Lists(tmp_path / "tight", phrases=phrases)
    with pytest.raises(gen.GenerateError, match="give its group a longer slot_s"):
        build(tight, tmp_path / "bank")


def test_a_rebuild_from_the_cache_takes_a_kept_utterance_only_when_it_is_the_one_kept(lists, tmp_path):
    """A cached wav is used only when its samples hash to what the index
    recorded; a changed one is said again, so the bank is the same bytes
    and its manifests' hashes describe its audio."""
    asked: list = []

    def voice(v, scale, texts):
        asked.append((v["name"], scale, list(texts)))
        return fake_voice(v, scale, texts)

    def build_cached(out: Path) -> None:
        gen.build(out, "small", lists.inputs(), voice, engine={"name": "fake-voice", "version": "1"},
                  cards=lists.cards(), synth_dir=tmp_path / "cache", be=gen.backend(plain=True), say=quiet)

    build_cached(tmp_path / "a")
    index = json.loads((tmp_path / "cache" / "index.json").read_text(encoding="utf-8"))
    key, entry = next((k, e) for k, e in index.items() if e["text"] == "What time is it?")
    gen.write_wav(tmp_path / "cache" / f"{key}.wav", bytes(2 * entry["frames"]), entry["sample_rate"])
    asked.clear()
    build_cached(tmp_path / "b")
    assert asked == [(entry["voice"], entry["length_scale"], ["What time is it?"])], "only the changed one again"
    assert (tmp_path / "a" / "SHA256SUMS").read_bytes() == (tmp_path / "b" / "SHA256SUMS").read_bytes()


# ======================================================== check.py's rules


def copy_bank(src: Path, dst: Path) -> Path:
    for path in src.rglob("*"):
        if path.is_file():
            (dst / path.relative_to(src)).parent.mkdir(parents=True, exist_ok=True)
            (dst / path.relative_to(src)).write_bytes(path.read_bytes())
    return dst


def edit_manifest(bank: Path, change) -> dict:
    path = next((bank / "manifests").glob("*.json"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def test_a_take_without_its_record_of_what_made_it_is_refused(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    doc = edit_manifest(bank, lambda d: d["generated"]["takes"].pop(d["takes"][0]["id"]))
    lines, bad = check_lines(bank, lists)
    assert bad and f"    take {doc['takes'][0]['id']}: no record of what made it" in lines


def test_a_recording_with_no_provenance_at_all_is_refused(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    edit_manifest(bank, lambda d: d.pop("generated"))
    lines, bad = check_lines(bank, lists)
    assert bad and any("says what made it under generated" in line for line in lines)


def test_a_piece_said_without_the_hash_of_what_the_voice_gave_back_is_refused(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")

    def drop(d):
        first = d["generated"]["takes"][d["takes"][0]["id"]]
        first["said"][0].pop("sha256")

    edit_manifest(bank, drop)
    lines, _bad = check_lines(bank, lists)
    assert any("what was said with the sha256" in line for line in lines)


def test_a_changed_file_is_caught_by_the_sums(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    (bank / "DATASHEET.md").write_text("an edited datasheet\n", encoding="utf-8")
    (bank / "extra.txt").write_text("a file nobody listed\n", encoding="utf-8")
    lines, bad = check_lines(bank, lists)
    assert bad and "SHA256SUMS: DATASHEET.md is not the file that was listed" in lines
    assert "SHA256SUMS: extra.txt is in the bank and is not listed" in lines


def test_a_voice_record_that_is_not_the_pinned_one_is_refused(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    record = json.loads((bank / "voices" / "g01.json").read_text(encoding="utf-8"))
    record["licence"] = "CC BY 4.0"
    (bank / "voices" / "g01.json").write_text(json.dumps(record), encoding="utf-8")
    lines, bad = check_lines(bank, lists)
    assert bad and any(line.startswith("speaker g01: the voice record is not the pinned one") and "licence" in line
                       for line in lines)


def test_a_voice_the_list_does_not_have_is_refused(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    voices = lists.inputs()["voices"]
    voices["voices"] = voices["voices"][1:]
    lines, _good, bad = bank_check.check(bank, [], False, voices=voices)
    assert bad and "speaker g01: not in the voice list, so nothing says the voice may be used" in lines


def test_a_generated_bank_holds_no_recorded_voice(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    index = json.loads((bank / "bank.json").read_text(encoding="utf-8"))
    index["speakers"]["s01"] = {"consent": "consent/s01.txt"}
    (bank / "bank.json").write_text(json.dumps(index), encoding="utf-8")
    lines, bad = check_lines(bank, lists)
    assert bad and any("a recorded voice in a generated bank" in line for line in lines)


def first_manifest(bank: Path) -> tuple[Path, dict]:
    """The first manifest is gen-g01-near-1, with speaker g01, the room sim-box and pink noise at -55 dBFS."""
    path = sorted((bank / "manifests").glob("*.json"))[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _edit(change):
    """A spoiling of the bank's first manifest, the sums written again so
    only the rule under test can refuse it."""
    def spoil(bank: Path) -> None:
        path, doc = first_manifest(bank)
        change(doc)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return spoil


def _json_file(rel: str, change):
    def spoil(bank: Path) -> None:
        doc = json.loads((bank / rel).read_text(encoding="utf-8"))
        change(doc)
        (bank / rel).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return spoil


def _text_file(rel: str, change):
    def spoil(bank: Path) -> None:
        (bank / rel).write_text(change((bank / rel).read_text(encoding="utf-8")), encoding="utf-8")
    return spoil


def _first_take(doc: dict) -> dict:
    return doc["generated"]["takes"][doc["takes"][0]["id"]]


RULES = {
    # the room: the manifest against its record, and bank.json's room against both
    "room id": (_edit(lambda d: d["generated"]["room"].update(id="sim-other")),
                "generated: the room is sim-other, and the manifest names sim-box", True),
    "distance": (_edit(lambda d: d["generated"]["room"].update(distance_m=9.9)),
                 "generated: the room puts the talker 9.9 m away, and the manifest says", True),
    "room size": (_json_file("bank.json", lambda d: d["rooms"]["sim-box"].update(size_m=[9.9, 9.9, 9.9])),
                  "generated: the room's size_m is [3.0, 2.5, 2.4], and bank.json gives sim-box [9.9, 9.9, 9.9]", True),
    "absorption": (_json_file("bank.json", lambda d: d["rooms"]["sim-box"].update(absorption=0.9)),
                   "generated: the room's absorption is 0.6, and bank.json gives sim-box 0.9", True),
    # the noise
    "noise or none": (_edit(lambda d: d.update(noise=None)),
                      "generated: the manifest's noise and the record's do not agree on whether there is any", True),
    "noise words": (_edit(lambda d: d.update(noise="pink noise at -40 dBFS")),
                    "generated: the manifest says the noise is 'pink noise at -40 dBFS', and its record is "
                    "'pink noise at -55 dBFS'", True),
    # the provenance of each take
    "record with no take": (_edit(lambda d: d["generated"]["takes"].update({"x/99": _first_take(d)})),
                            "generated: a record for x/99, which is no take of this recording", True),
    "take outside its slot": (_edit(lambda d: _first_take(d).update(at_sample=d["takes"][0]["slot"]["end"])),
                              "its audio lies inside its slot, from at_sample for samples", True),
    # the voice the manifest names, against the pin
    "voice hash": (_edit(lambda d: d["generated"]["voice"].update(model_sha256="1" * 64)),
                   "generated: the voice's model has sha256 111111111111..., and the list pins", True),
    "voice name": (_edit(lambda d: d["generated"]["voice"].update(name="tone-b")),
                   "generated: the voice is tone-b, and speaker g01 is tone-a in the list", True),
    # the store's files
    "no sums": (lambda bank: (bank / "SHA256SUMS").unlink(),
                "no SHA256SUMS: a generated bank lists every file with its sha256", False),
    "a listed file gone": (lambda bank: (bank / "voices" / "tone-b.MODEL_CARD").unlink(),
                           "SHA256SUMS: voices/tone-b.MODEL_CARD is listed and is not there", False),
    "no card": (lambda bank: (bank / "voices" / "tone-a.MODEL_CARD").unlink(),
                "speaker g01: no tone-a.MODEL_CARD beside the voice record; a voice travels with its card", True),
    "another card": (_text_file("voices/tone-a.MODEL_CARD", lambda t: t.replace("# Model card", "# A card")),
                     "speaker g01: tone-a.MODEL_CARD is not the card the list was written from", True),
    "no name": (_text_file("ATTRIBUTION.md", lambda t: t.replace("tone-b", "a tone")),
                "ATTRIBUTION.md does not name tone-b", True),
    "no credit": (_text_file("ATTRIBUTION.md", lambda t: t.replace("- Credit: Tones, for test voice 2.\n", "")),
                  "ATTRIBUTION.md does not give tone-b's credit, 'Tones, for test voice 2.'", True),
    "no datasheet": (lambda bank: (bank / "DATASHEET.md").unlink(),
                     "no DATASHEET.md: a generated bank says what it is and whose voices it holds", True),
    "no attribution": (lambda bank: (bank / "ATTRIBUTION.md").unlink(),
                       "no ATTRIBUTION.md: a generated bank says what it is and whose voices it holds", True),
}


@pytest.mark.parametrize("rule", list(RULES))
def test_check_holds_a_generated_bank_to_each_of_its_rules(built, tmp_path, rule):
    """Each refusal of check.py's generated-bank rules, by its own words.
    The sums are written again after the spoiling, as a builder that wrote
    a wrong bank would write them, so the rule itself must refuse it."""
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    spoil, message, resum = RULES[rule]
    assert check_lines(bank, lists)[1] == 0
    spoil(bank)
    if resum:
        gen.write_sums(bank)
    lines, bad = check_lines(bank, lists)
    assert bad and any(message in line for line in lines), lines


def test_the_base_a_voice_was_fine_tuned_from_is_credited_too():
    doc = fake_voices(["tone-a"])[0]
    doc["bases"] = {"b": {"name": "base-b", "dataset": "d", "licence": "CC0", "training": "Trained from scratch.",
                          "credit": "Base b's tones, CC0."}}
    entry = {**doc["voices"][0], "base": "b"}
    text = f"tone-a\n- Credit: {entry['credit']}\n"
    assert bank_check.attribution_problems(text, [entry], doc) == [
        "ATTRIBUTION.md does not give the credit of tone-a's base, b"]
    assert bank_check.attribution_problems(text + "- Credit: Base b's tones, CC0.\n", [entry], doc) == []


def test_a_list_that_grows_fails_no_bank_already_built(built, tmp_path):
    """A voice added on a later day, at a later revision, with a newer
    Piper, moves every field the list's voices share. A bank built before
    still passes: its voices are held by their own fields and hashes."""
    lists, bank = built
    voices = lists.inputs()["voices"]
    new_rev = "1" * 40
    voices.update(read="2027-01-15", revision=new_rev, engine={**voices["engine"], "version": "9.9.9"},
                  list={**voices["list"], "commit": new_rev})
    for voice in voices["voices"]:
        for entry in voice["files"].values():
            entry["url"] = entry["url"].replace(f"/resolve/{REV}/", f"/resolve/{new_rev}/")
    grown = fake_voices(["tone-a", "tone-b", "tone-c"])[0]["voices"][2]
    for entry in grown["files"].values():
        entry["url"] = entry["url"].replace(f"/resolve/{REV}/", f"/resolve/{new_rev}/")
    voices["voices"].append(grown)
    assert bank_check.voice_list_problems(voices) == []
    lines, _good, bad = bank_check.check(bank, [], False, voices=voices)
    assert bad == 0, lines
    voices["voices"][0]["files"]["model"]["sha256"] = "2" * 64
    lines, _good, bad = bank_check.check(bank, [], False, voices=voices)
    assert bad and any("the voice record is not the pinned one for tone-a: files differ(s)" in line
                       for line in lines), "a voice's own file is still held hash for hash"


# ================================================== the public store


def quiet(_line: str) -> None:
    pass


def test_pack_then_fetch_from_a_file_url_gives_the_same_bank(built, tmp_path):
    lists, bank = built
    voices = lists.inputs()["voices"]
    store = tmp_path / "store"
    archive = gen.pack(bank, store, say=quiet, voices=voices)
    assert archive.name == f"genbank-{bank.name}-2026-11-26.tar.gz"
    again = gen.pack(bank, tmp_path / "store2", say=quiet, voices=voices)
    assert archive.read_bytes() == again.read_bytes(), "the same files pack to the same bytes"
    digest = bank_files.sha256_file(archive)
    assert (store / f"{archive.name}.sha256").read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"
    root = gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices)
    assert root.name == f"genbank-{bank.name}-2026-11-26"
    assert audio_hashes(root) == audio_hashes(bank)
    assert gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices) == root, "used again"


def test_a_bank_that_does_not_pass_check_is_not_packed(built, tmp_path):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    edit_manifest(bank, lambda d: d.pop("generated"))
    with pytest.raises(gen.GenerateError, match="does not pass check.py, so it is not packed"):
        gen.pack(bank, tmp_path / "store", say=quiet, voices=lists.inputs()["voices"])
    assert not (tmp_path / "store").exists()


def test_fetch_refuses_an_archive_whose_hash_is_not_the_one_given_and_keeps_nothing(built, tmp_path):
    lists, bank = built
    voices = lists.inputs()["voices"]
    archive = gen.pack(bank, tmp_path / "store", say=quiet, voices=voices)
    with pytest.raises(gen.GenerateError, match="nothing was kept"):
        gen.fetch(archive.as_uri(), "f" * 64, tmp_path / "ci", say=quiet, voices=voices)
    assert list((tmp_path / "ci").iterdir()) == []
    with pytest.raises(gen.GenerateError, match="64 hex digits"):
        gen.fetch(archive.as_uri(), "abc", tmp_path / "ci", say=quiet, voices=voices)


def test_fetch_refuses_a_member_outside_the_bank(tmp_path):
    archive = tmp_path / "genbank-small-2026-11-26.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"not a bank file"
        info = tarfile.TarInfo("genbank-small-2026-11-26/../../outside.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(gen.GenerateError, match="is not a file inside genbank-small-2026-11-26/"):
        gen.fetch(archive.as_uri(), bank_files.sha256_file(archive), tmp_path / "ci", say=lambda _l: None)
    assert not (tmp_path / "outside.txt").exists()


def test_a_folder_there_already_from_another_archive_is_refused(built, tmp_path):
    """A sound bank whose marker names another archive is never taken for
    this one, and a folder with no marker is refused the same way."""
    lists, bank = built
    voices = lists.inputs()["voices"]
    archive = gen.pack(bank, tmp_path / "store", say=quiet, voices=voices)
    digest = bank_files.sha256_file(archive)
    root = gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices)
    marker = root.with_name(root.name + ".fetched")
    marker.write_text(f"{'e' * 64}  {archive.name}\n", encoding="utf-8")
    assert bank_check.check(root, [], False, voices=voices)[2] == 0, "the folder itself is a sound bank"
    with pytest.raises(gen.GenerateError, match="is there already, from another archive or none: move it away"):
        gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices)
    marker.unlink()
    with pytest.raises(gen.GenerateError, match="from another archive or none"):
        gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices)


def test_a_folder_from_this_archive_that_no_longer_passes_check_is_refused(built, tmp_path):
    lists, bank = built
    voices = lists.inputs()["voices"]
    archive = gen.pack(bank, tmp_path / "store", say=quiet, voices=voices)
    digest = bank_files.sha256_file(archive)
    root = gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices)
    (root / "DATASHEET.md").write_text("edited after the fetch\n", encoding="utf-8")
    with pytest.raises(gen.GenerateError, match="came from this archive and no longer passes check.py") as caught:
        gen.fetch(archive.as_uri(), digest, tmp_path / "ci", say=quiet, voices=voices)
    assert "SHA256SUMS: DATASHEET.md is not the file that was listed" in str(caught.value)


def test_fetch_removes_a_bank_that_does_not_pass_check(built, tmp_path):
    """The archive's hash is the one given, and the bank in it fails
    check.py against the list it is read with: here a list without g02."""
    lists, bank = built
    archive = gen.pack(bank, tmp_path / "store", say=quiet, voices=lists.inputs()["voices"])
    with pytest.raises(gen.GenerateError, match="does not pass check.py, so it was removed"):
        gen.fetch(archive.as_uri(), bank_files.sha256_file(archive), tmp_path / "ci", say=quiet,
                  voices=fake_voices(["tone-a"])[0])
    assert list((tmp_path / "ci").iterdir()) == [], "no folder and no marker are kept"


def a_tarball(path: Path, top: str, members: list[tarfile.TarInfo]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info in members:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return path


@pytest.mark.parametrize("member", ["link", "other folder"])
def test_fetch_refuses_a_link_or_a_member_under_another_folder(tmp_path, member):
    top = "genbank-small-2026-11-26"
    if member == "link":
        info = tarfile.TarInfo(f"{top}/bank.json")
        info.type, info.linkname = tarfile.SYMTYPE, "manifests/x.json"
    else:
        info = tarfile.TarInfo("other-folder/x")
        info.size = 1
    archive = a_tarball(tmp_path / f"{top}.tar.gz", top, [info])
    with pytest.raises(gen.GenerateError, match=f"is not a file inside {top}/"):
        gen.fetch(archive.as_uri(), bank_files.sha256_file(archive), tmp_path / "ci", say=quiet)
    assert list((tmp_path / "ci").iterdir()) == [], "nothing is unpacked"


def test_fetch_refuses_a_url_whose_name_is_not_a_packed_banks(tmp_path):
    archive = tmp_path / "Bad Name.tar.gz"
    archive.write_bytes(b"not read")
    with pytest.raises(gen.GenerateError, match="a packed bank's name ends .tar.gz, as `--pack` writes it"):
        gen.fetch(archive.as_uri(), bank_files.sha256_file(archive), tmp_path / "ci", say=quiet)
    assert not (tmp_path / "ci").exists(), "--to is never made"


def a_cassette() -> dict:
    return {"format": "emet-cassette/1", "turns": [{
        "request": {"provider": "deepgram", "model": "nova-3", "sample_rate": 16000,
                    "query": {"channels": "1", "encoding": "linear16", "interim_results": "true",
                              "mip_opt_out": "true", "punctuate": "true", "smart_format": "false"}},
        "fed_sha256": "3" * 64, "fed_bytes": 64000,
        "partials": [{"text": "What", "after_bytes": 10560, "prefix_sha256": "4" * 64}],
        "final": {"text": "What time is it?", "confidence": 0.99, "error": None},
        "model_version": "2025-04-17.21547", "recorded": "2026-11-27", "ledger_entry": 12,
        "source": "misses.json turn gen-g01-near-1/1"}]}


def test_sums_take_a_cassette_into_a_built_bank_so_it_checks_and_packs(built, tmp_path, capsys):
    """The store's cassettes travel in the bank (`cassettes/`), so the sums
    must list them: `--sums BANK` writes them again, after reading each
    file there as a cassette."""
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    (bank / "cassettes").mkdir()
    (bank / "cassettes" / "paid-2026-11-27.json").write_text(json.dumps(a_cassette()), encoding="utf-8")
    voices = lists.inputs()["voices"]
    assert "SHA256SUMS: cassettes/paid-2026-11-27.json is in the bank and is not listed" in check_lines(bank, lists)[0]
    gen.sums_again(bank, say=quiet, voices=voices)
    assert check_lines(bank, lists)[1] == 0
    assert gen.pack(bank, tmp_path / "store", say=quiet, voices=voices).is_file()
    (bank / "cassettes" / "notes.json").write_text("{}", encoding="utf-8")
    with pytest.raises(gen.GenerateError, match="so the sums were not written:\n  cassettes/notes.json: cassette: "):
        gen.sums_again(bank, say=quiet, voices=voices)


def test_sums_on_the_command_line_write_and_check(built, tmp_path, monkeypatch, capsys):
    lists, bank = built
    bank = copy_bank(bank, tmp_path / "bank")
    (bank / "cassettes").mkdir()
    (bank / "cassettes" / "c.json").write_text(json.dumps(a_cassette()), encoding="utf-8")
    monkeypatch.setattr(bank_check, "VOICES", lists.data / "voices.json")
    assert gen.main(["--sums", str(bank)]) == 0
    assert "SHA256SUMS: written; total: 2 of 2 recording(s) sound" in capsys.readouterr().out
    assert check_lines(bank, lists)[1] == 0


# ====================================================== Piper as a tool

FAKE_PIPER = '''\
import argparse, time, wave
from pathlib import Path
p = argparse.ArgumentParser()
for flag in ("-m", "-c", "-i", "-d", "--length-scale", "--noise-scale", "--noise-w-scale",
             "--sentence-silence", "--output-dir-naming"):
    p.add_argument(flag)
a = p.parse_args()
assert a.noise_scale == "0" and a.noise_w_scale == "0", "both noise scales are 0"
out = Path(a.d)
out.mkdir(parents=True, exist_ok=True)
for line in Path(a.i).read_text(encoding="utf-8").splitlines():
    with wave.open(str(out / f"{time.monotonic_ns()}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(bytes(2 * 50 * len(line)))
    time.sleep(0.02)
'''


def test_piper_runs_as_a_child_process_one_wav_a_line_in_order(tmp_path, monkeypatch):
    package = tmp_path / "fake" / "piper"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(FAKE_PIPER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fake"))
    tool = gen.PiperTool(sys.executable, {"tone-a": {"model": tmp_path / "a.onnx", "config": tmp_path / "a.json"}})
    assert tool.command == [sys.executable, "-m", "piper"]
    said = tool({"name": "tone-a"}, 1.12, ["Hey emet.", "What time is it?", "No."])
    assert [len(pcm) for _rate, pcm in said] == [2 * 50 * 9, 2 * 50 * 16, 2 * 50 * 3]
    assert all(rate == 22050 for rate, _pcm in said)
    assert "--length-scale" in tool.arguments({"name": "tone-a"}, 1.12)


def test_the_builder_imports_nothing_of_piper():
    source = (REPO / "tools" / "bank" / "generate.py").read_text(encoding="utf-8")
    assert "import piper" not in source and "from piper" not in source


STALLED_PIPER = '''\
import time
time.sleep(30)
'''


def test_a_piper_that_stalls_is_stopped_with_its_voice_and_rate_named(tmp_path, monkeypatch):
    package = tmp_path / "fake" / "piper"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(STALLED_PIPER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fake"))
    tool = gen.PiperTool(sys.executable, {"tone-a": {"model": tmp_path / "a.onnx", "config": tmp_path / "a.json"}})
    tool.BATCH_S, tool.LINE_S = 1.0, 0.0
    with pytest.raises(gen.GenerateError, match="piper did not finish 1 line.s. of tone-a at length scale 0.9 "
                                                "within 1 s, so it was stopped"):
        tool({"name": "tone-a"}, 0.9, ["Hey emet."])
    assert tool.bound_s(1.12, 10) == pytest.approx((1.0 + 0.0 * 10) * 1.12)


# ================================================================ the clip


def test_the_clip_is_made_with_its_lead_and_its_record_beside_it(tmp_path):
    """make_clip with the fake voice standing in for the reference one: the
    record names the file it wrote, and the phrase starts after the lead."""
    lists = Lists(tmp_path / "lists", names=("tone-a", gen.CLIP_VOICE))
    out = tmp_path / "clip.wav"
    record = gen.make_clip(out, lists.inputs(), fake_voice, engine={"name": "fake-voice", "version": "1"},
                           be=gen.backend(plain=True))
    assert record == json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert record["phrase_at_sample"] >= 5 * 16000 and record["phrase_at_sample"] == int(gen.CLIP_LEAD_S * 16000)
    assert record["sha256"] == bank_files.sha256_file(out) and record["bytes"] == out.stat().st_size < 1_000_000
    assert gen.read_wav(out)[0] == 16000 and record["frames"] == len(gen.read_wav(out)[1]) // 2
    assert record["voice"]["name"] == gen.CLIP_VOICE and record["condition"]["id"] == gen.CLIP_CONDITION
    assert record["command"] == "python tools/bank/generate.py --clip FILE --voices VOICES --piper PIPER"
    assert record["inputs"] == lists.inputs()["sha256"]


def test_the_committed_clips_record_names_the_pinned_reference_voice():
    """The clip in emet-hal/tests/clips/ was made by the voice the list
    pins: its record's three hashes are that voice's."""
    record = json.loads((REPO / "emet-hal" / "tests" / "clips" / "hey-emet-ljspeech.json").read_text(encoding="utf-8"))
    pinned = next(v for v in gen.load_inputs()["voices"]["voices"] if v["name"] == gen.CLIP_VOICE)
    assert {k: record["voice"][f"{k}_sha256"] for k in ("model", "config", "card")} == {
        k: pinned["files"][k]["sha256"] for k in ("model", "config", "card")}
    assert record["voice"]["licence"] == pinned["licence"] == "public domain"


# ============================================================ the command


def test_plan_prints_every_set_unless_one_is_named(lists, capsys):
    data = str(lists.data)
    assert gen.main(["--plan", "--data", data]) == 0
    heads = [line.split(":")[0] for line in capsys.readouterr().out.splitlines() if not line.startswith(" ")]
    assert heads == ["small", "full"]
    assert gen.main(["--plan", "--set", "full", "--data", data]) == 0
    heads = [line.split(":")[0] for line in capsys.readouterr().out.splitlines() if not line.startswith(" ")]
    assert heads == ["full"]


def test_plan_refuses_a_set_the_list_does_not_have(lists, capsys):
    assert gen.main(["--plan", "--set", "smal", "--data", str(lists.data)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "no set is called 'smal'; the list has small, full" in captured.err


# =============================================== the goldens and the bank


def test_the_generated_goldens_sit_apart_under_names_that_cannot_collide():
    """0.5.6 commits win32-amd64's 100, one a recording of the full set, so
    a folder gone is a failure, never a skip."""
    golden = REPO / "tools" / "bank" / "golden"
    generated = golden / "generated"
    ids = {rec["id"] for rec in gen.plan(gen.load_inputs(), "full")}
    windows = {p.stem for p in (generated / "win32-amd64").glob("*.json")}
    assert windows == ids, "win32-amd64 holds one golden a recording of the full set"
    for path in generated.glob("*/*.json"):
        doc = bank_files.read(path, "golden")
        assert doc["recording"] in ids and doc["recording"].startswith("gen-"), path
    assert not any(p.stem.startswith("gen-") for p in golden.glob("*/*.json")), "bank v0's own folder holds none"


def genbank_root() -> Path | None:
    named = os.environ.get("EMET_GENBANK")
    root = Path(named) if named else REPO.parent / "genbank" / "small"
    return root if (root / "bank.json").is_file() else None


def test_the_built_small_set_passes_check_and_is_the_plan():
    root = genbank_root()
    if root is None:
        pytest.skip("no generated bank on this machine: set EMET_GENBANK to its small set's root")
    lines, _good, bad = bank_check.check(root, [], False)
    assert bad == 0, lines
    assert set(audio_hashes(root)) == {rec["id"] for rec in gen.plan(gen.load_inputs(), "small")}


def test_a_generated_recording_still_matches_its_golden(capsys):
    """One recording of the built small set, the one with a single wake,
    replayed at the shipped threshold against its golden in
    golden/generated/, as the real bank's parity test does for bank v0.
    About 10 s; skips without the built bank or the wake engine."""
    root = genbank_root()
    if root is None:
        pytest.skip("no generated bank on this machine: set EMET_GENBANK to its small set's root")
    pytest.importorskip("pocketsphinx")
    import replay

    rid = "gen-g01-5m-hall-5"
    if not (REPO / "tools" / "bank" / "golden" / "generated" / replay.platform_name() / f"{rid}.json").is_file():
        pytest.skip(f"no generated golden for {replay.platform_name()}")
    code = replay.main([str(root), "--golden", str(REPO / "tools" / "bank" / "golden" / "generated"), "--only", rid])
    out = capsys.readouterr().out
    assert code == 0 and f"{rid}: same as the golden" in out, out
