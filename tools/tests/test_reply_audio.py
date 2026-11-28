"""tools/reply_audio.py: the gaps, joins and loudness steps it measures in
WAV files built here with known ones, what it refuses, and the replies it
says through emet-say into files."""

from __future__ import annotations

import json
import math
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import reply_audio  # noqa: E402  a tool, imported by its bare name from tools/

RATE = 22050


def speech(seconds: float, dbfs: float = -12.0, hz: float = 220.0, fade: bool = True) -> list[int]:
    """A sine at an RMS of `dbfs`, faded in and out over 10 ms so no
    sample steps out of silence, unless `fade` is false."""
    n = int(round(seconds * RATE))
    peak = 32768 * 10 ** (dbfs / 20.0) * math.sqrt(2)
    ramp = int(0.010 * RATE)
    out = []
    for i in range(n):
        g = min(1.0, i / ramp, (n - 1 - i) / ramp) if fade else 1.0
        out.append(int(peak * g * math.sin(2 * math.pi * hz * i / RATE + (0 if fade else math.pi / 2))))
    return out


def quiet(seconds: float) -> list[int]:
    return [0] * int(round(seconds * RATE))


def wav(path: Path, samples: list[int], rate: int = RATE, channels: int = 1, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            w.writeframes(struct.pack(f"<{len(samples) * channels}h", *(s for s in samples for _ in range(channels))))
        else:
            w.writeframes(bytes(128 for _ in samples))
    return path


def three_sentences(gap_s: float = 0.30, second_dbfs: float = -12.0, third_dbfs: float = -12.0) -> list[int]:
    return (quiet(0.2) + speech(0.8) + quiet(gap_s) + speech(0.6, dbfs=second_dbfs) + quiet(gap_s)
            + speech(1.0, dbfs=third_dbfs) + quiet(0.4))


def test_a_clean_reply_measures_its_sentences_and_crosses_nothing(tmp_path, capsys):
    path = wav(tmp_path / "reply-1.wav", three_sentences())
    m = reply_audio.measure_file(path)
    assert m.sentences == 3 and m.rate == RATE
    assert m.first_sentence_s == pytest.approx(0.8, abs=0.021)
    assert m.largest_gap_ms == pytest.approx(300, abs=21) and m.gap_at_s == pytest.approx(1.0, abs=0.021)
    assert m.join_step < 0.01 and m.loudness_step_db < 0.5
    assert reply_audio.crossings(m) == []
    assert reply_audio.main(["--measure", str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "thresholds: largest gap at most 700 ms, join step at most 0.10, loudness step at most 6 dB"
    assert out[1].startswith(f"{path}  3.60 s, 3 sentence(s), first 0.8")
    assert out[-1] == "verdict: 1 file(s) within every threshold"


def test_a_gap_over_700_ms_is_named_with_its_file_and_time(tmp_path, capsys):
    path = wav(tmp_path / "reply-2.wav", quiet(0.2) + speech(0.8) + quiet(0.85) + speech(0.6) + quiet(0.2))
    m = reply_audio.measure_file(path)
    assert m.largest_gap_ms == pytest.approx(850, abs=21)
    assert reply_audio.main(["--measure", str(path)]) == 1
    over = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  over: ")]
    assert len(over) == 1
    assert re.fullmatch(re.escape(f"  over: {path}: the largest gap is ") + r"8[3-6]\d ms at 1\.0\d s, "
                        r"over the 700 ms threshold", over[0])


def test_a_step_out_of_near_silence_is_a_join_step(tmp_path, capsys):
    """A sentence that starts at full swing after silence, as a click at a
    chunk's join does: the step is read where it lands."""
    samples = quiet(0.5) + speech(0.6, dbfs=-12.0, fade=False) + quiet(0.3)
    path = wav(tmp_path / "reply-3.wav", samples)
    m = reply_audio.measure_file(path)
    assert m.join_step > 0.3 and m.join_at_s == pytest.approx(0.5, abs=0.001)
    assert reply_audio.main(["--measure", str(path)]) == 1
    assert f"  over: {path}: a join step of " in capsys.readouterr().out


def test_a_step_inside_loud_speech_is_no_join_step(tmp_path):
    """A loud 2 kHz sine moves by about 0.5 between samples at its zero
    crossings, and its preceding 5 ms are loud, so no join is read there:
    what is read is the faded start, out of silence."""
    samples = speech(1.0, dbfs=-4.0, hz=2000.0)
    assert max(abs(b - a) for a, b in zip(samples, samples[1:])) / 32768 > 0.4
    m = reply_audio.measure_file(wav(tmp_path / "r.wav", quiet(0.2) + samples + quiet(0.2)))
    assert m.join_step < 0.05 and m.join_at_s == pytest.approx(0.2, abs=0.006)


def test_a_rise_out_of_a_consonants_quiet_is_no_join_step(tmp_path):
    """The reference voice opens a consonant's release out of 5 ms at -42 to
    -47 dBFS (0.5.7's review, critic#6): a rise out of a -45 dBFS bed is no
    join, and the same rise out of digital silence still is one."""
    bed = speech(0.1, dbfs=-45.0, fade=False)
    rise = speech(0.6, dbfs=-12.0, fade=False)
    over_bed = reply_audio.measure_file(wav(tmp_path / "bed.wav", quiet(0.2) + bed + rise + quiet(0.3)))
    assert over_bed.join_step < 0.01 and reply_audio.crossings(over_bed) == []
    after_silence = reply_audio.measure_file(wav(tmp_path / "click.wav", quiet(0.3) + rise + quiet(0.3)))
    assert after_silence.join_step > 0.3 and after_silence.join_at_s == pytest.approx(0.3, abs=0.001)


def test_fewer_sentences_than_said_leaves_the_first_and_the_loudness_unmeasured(tmp_path, capsys):
    """Three sentences said, the first two 100 ms apart, shorter than a gap:
    the gaps find two, so the first "sentence" is two of them and the
    loudness step compares the wrong spans. Both are reported as not
    measured and the loudness threshold is not applied (critic#6)."""
    samples = (quiet(0.2) + speech(0.8) + quiet(0.10) + speech(0.6) + quiet(0.3) + speech(1.0, dbfs=-21.0)
               + quiet(0.4))
    path = wav(tmp_path / "reply-1.wav", samples)
    m = reply_audio.measure_file(path)
    assert m.sentences == 2 and m.first_sentence_s == pytest.approx(1.5, abs=0.021)
    assert m.loudness_step_db == pytest.approx(9.0, abs=0.3) and reply_audio.crossings(m)
    held = reply_audio.held_to_said(reply_audio.measure_file(path), 3)
    assert held.short and held.said == 3
    assert held.first_sentence_s is None and held.loudness_step_db is None and reply_audio.crossings(held) == []
    assert reply_audio.held_to_said(reply_audio.measure_file(path), 2).first_sentence_s is not None
    assert reply_audio.report([path], as_json=False, said=[3]) == 0
    out = capsys.readouterr().out.splitlines()
    assert ", 2 sentence(s), of 3 said; first sentence not measured; largest gap " in out[1], out[1]
    assert out[1].endswith("; loudness step not measured"), out[1]
    assert out[-1].endswith("; the loudness step not measured in 1 file(s), whose gaps found fewer sentences "
                            "than were said")
    assert reply_audio.report([path], as_json=True, said=[3]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["files"][0]["said"] == 3 and doc["files"][0]["not_measured"] == ["first_sentence_s", "loudness_step_db"]


@pytest.mark.parametrize("output, count", [
    ("emet-say: ...\n  said     3 sentence(s) and 1 filler(s); voice first 412 ms\n", 4),
    ("  said     2 sentence(s); no sound reached the sink\n", 2),
    ("said 2 sentence(s)\n", 2),
    ("nothing of the kind\n", None),
    ("", None),
])
def test_the_count_said_is_read_from_emet_says_said_line(output, count):
    assert reply_audio.said_count(output) == count


def test_a_loudness_step_between_neighbours_is_named_at_the_later_sentence(tmp_path, capsys):
    path = wav(tmp_path / "reply-4.wav", three_sentences(second_dbfs=-22.0, third_dbfs=-14.0))
    m = reply_audio.measure_file(path)
    assert m.loudness_step_db == pytest.approx(10.0, abs=0.2)
    assert m.loudness_at_s == pytest.approx(1.3, abs=0.021)
    assert m.sentence_levels_dbfs[0] == pytest.approx(-12.0, abs=0.2)
    assert reply_audio.main(["--measure", str(path)]) == 1
    assert re.search(r"a loudness step of 10\.\d dB at 1\.3\d s, over the 6 dB threshold", capsys.readouterr().out)


def test_short_silences_and_short_sounds_are_neither_gaps_nor_sentences(tmp_path):
    """A pause under 150 ms joins its neighbours into one sentence; a blip
    under 200 ms is no sentence, and still bounds the gaps beside it; the
    silence before the first sound and after the last is no gap."""
    samples = (quiet(1.0) + speech(0.5) + quiet(0.10) + speech(0.5) + quiet(0.4) + speech(0.05)
               + quiet(0.4) + speech(0.5) + quiet(1.5))
    m = reply_audio.measure_file(wav(tmp_path / "r.wav", samples))
    assert m.sentences == 2
    assert m.first_sentence_s == pytest.approx(1.1, abs=0.021)
    assert m.largest_gap_ms == pytest.approx(400, abs=21)


def test_silence_alone_measures_nothing_and_crosses_nothing(tmp_path):
    m = reply_audio.measure_file(wav(tmp_path / "r.wav", quiet(1.0)))
    assert (m.sentences, m.first_sentence_s, m.largest_gap_ms, m.loudness_step_db) == (0, None, None, None)
    assert m.join_step == 0.0 and reply_audio.crossings(m) == []


@pytest.mark.parametrize("kw, says", [
    ({"channels": 2}, "is 2 channel(s) of 16-bit samples; this tool measures mono 16-bit PCM only"),
    ({"width": 1}, "is 1 channel(s) of 8-bit samples; this tool measures mono 16-bit PCM only"),
])
def test_a_file_that_is_not_mono_16_bit_is_refused_naming_it(tmp_path, capsys, kw, says):
    path = wav(tmp_path / "stereo.wav", speech(0.3), **kw)
    with pytest.raises(reply_audio.AudioRefused) as caught:
        reply_audio.measure_file(path)
    assert str(caught.value) == f"{path} {says}"
    assert reply_audio.main(["--measure", str(path)]) == 2
    assert capsys.readouterr().err == f"reply_audio: {path} {says}\n"


def test_a_file_that_is_no_wav_is_refused_naming_it(tmp_path, capsys):
    path = tmp_path / "notes.wav"
    path.write_text("not audio", encoding="utf-8")
    assert reply_audio.main(["--measure", str(path)]) == 2
    assert capsys.readouterr().err.startswith(f"reply_audio: {path} is not a PCM WAV file this tool can read: ")


def test_json_holds_every_measure_and_the_verdict(tmp_path, capsys):
    good = wav(tmp_path / "a.wav", three_sentences())
    bad = wav(tmp_path / "b.wav", quiet(0.2) + speech(0.8) + quiet(0.9) + speech(0.6))
    assert reply_audio.main(["--measure", str(good), str(bad), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["format"] == "emet-reply-audio/1" and doc["verdict"] == "crossed"
    assert doc["thresholds"] == {"max_gap_ms": 700, "max_join_step": 0.1, "max_loudness_step_db": 6.0}
    assert [f["file"] for f in doc["files"]] == [str(good), str(bad)]
    assert doc["files"][0]["crossed"] == [] and len(doc["files"][1]["crossed"]) == 1
    assert set(doc["files"][0]) >= {"sentences", "first_sentence_s", "largest_gap_ms", "gap_at_s", "join_step",
                                    "join_at_s", "loudness_step_db", "loudness_at_s", "duration_s", "rate"}


def test_the_shipped_replies_are_five_of_two_to_four_sentences():
    replies = reply_audio.read_replies(reply_audio.REPLIES)
    assert len(replies) == 5
    for text in replies:
        words = re.sub(r"\[[a-z_.]+\]", "", text)
        assert 2 <= len(re.findall(r"[.?!](?:\s|$)", words)) <= 4, text
    assert sum("[express." in r for r in replies) == 1
    # The module docstring gives what a run spends at a cloud voice.
    assert f"the shipped replies are {sum(len(r) for r in replies)} characters a run" in " ".join(
        reply_audio.__doc__.split())


def test_each_reply_goes_to_emet_say_with_its_own_file(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        wav(Path(cmd[-1]), three_sentences())
        return subprocess.CompletedProcess(cmd, 0, "said 2 sentence(s)\n", "")

    monkeypatch.setattr(reply_audio, "say_command", lambda: ["emet-say"])
    written, failure, said = reply_audio.say_all("body.yaml", "soul.yaml", ["One. Two.", "Three. Four."],
                                                 tmp_path / "out", run=fake_run)
    assert failure is None and written == [tmp_path / "out" / "reply-1.wav", tmp_path / "out" / "reply-2.wav"]
    assert said == [2, 2], "the count emet-say printed for each file"
    assert seen == [["emet-say", "body.yaml", "soul.yaml", "One. Two.", "--to", str(written[0])],
                    ["emet-say", "body.yaml", "soul.yaml", "Three. Four.", "--to", str(written[1])]]


def test_a_reply_emet_say_cannot_say_stops_the_run_with_its_words(tmp_path, monkeypatch, capsys):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "the voice 'piper' cannot start, so there is nothing to say "
                                                       "the words with. It will not run. no model\n")

    monkeypatch.setattr(reply_audio, "say_command", lambda: ["emet-say"])
    monkeypatch.setattr(reply_audio.subprocess, "run", fake_run)
    replies = tmp_path / "replies.txt"
    replies.write_text("# one\nOne. Two.\n\n", encoding="utf-8")
    assert reply_audio.main(["body.yaml", "soul.yaml", "--replies", str(replies), "--out", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("reply_audio: emet-say exited 1 on reply 1:\nthe voice 'piper' cannot start")


def test_usage_errors_exit_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        reply_audio.main(["body.yaml"])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        reply_audio.main(["body.yaml", "soul.yaml", "--measure", "x.wav"])
    assert caught.value.code == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    assert reply_audio.main(["body.yaml", "soul.yaml", "--replies", str(empty)]) == 2
    assert "holds no reply" in capsys.readouterr().err


BODY = """\
manifest_version: "0.1"
body: {id: reply_audio_test, name: "a body with no parts", scale: desk, power: plugged_in,
       description: "A test body that says replies into a file."}
compute: {platform: other, ram_gb: 8, accelerator: none}
audio:
  input: {device: "file", sample_rate: 16000, channels: 1, aec: none, doa: false}
  output: {sink: "null", device: "none", gain_db: 0.0, sample_rate: 16000}
capabilities: []
safety: {estop_gpio: null, max_continuous_motion_s: 30, thermal_throttle_c: 75}
"""

SOUL = """\
bundle_version: "0.1"
identity: {name: "Emet", wake_word: "hey emet", line: emet, pronouns: "it/its", author: "The Emet Authors",
           created: "2026-08-08", license: null}
voice: {rate: 1.0, pitch_shift_semitones: 0}
persona:
  summary: "Earnest and plain-spoken."
  system_prompt: "You are Emet."
  traits: {warmth: 0.7, humor: 0.4, verbosity: 0.4, curiosity: 0.9, formality: 0.3}
  drift: {enabled: false, rate: 0.0, locked_traits: []}
  lines: {declined: "I'd rather not answer that one.", failed: "I lost the thread there.", nothing_heard: null}
interaction: {patience_ms: 900, extend_on_incomplete: true, barge_in: true, backchannel: true,
              greeting: {on_recognized: true, on_unknown: true}}
idle: {enabled: true, interval_s: [20, 90], weights: {settle: 0.4, look_around: 0.4, fidget: 0.2}, proactivity: 0.0}
memory: {personalization: true, cross_person_disclosure: true, consolidation: nightly, retention_days: 0}
models:
  tts: {provider: mock}
"""


def test_the_replies_reach_files_through_the_real_emet_say(tmp_path, monkeypatch, capsys):
    """The whole path with the mock voice, which opens no device and reaches
    no network: each reply is a WAV file, and each file is measured. The
    mock writes a word as a few samples of its letters, so a file lasts a
    few milliseconds and holds no sentence; this holds the path, and the
    files built above hold the measures."""
    pytest.importorskip("emet_engine.say")
    empty = tmp_path / "keys.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("EMET_KEYS", str(empty))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "body.yaml").write_text(BODY, encoding="utf-8")
    (tmp_path / "soul.yaml").write_text(SOUL, encoding="utf-8")
    replies = tmp_path / "replies.txt"
    replies.write_text("It is four. [express.curiosity] Is it late?\nOne. Two. Three.\n", encoding="utf-8")
    out = tmp_path / "out"
    code = reply_audio.main([str(tmp_path / "body.yaml"), str(tmp_path / "soul.yaml"),
                             "--replies", str(replies), "--out", str(out)])
    printed = capsys.readouterr().out.splitlines()
    assert sorted(p.name for p in out.iterdir()) == ["reply-1.wav", "reply-2.wav"]
    chars = len("It is four. [express.curiosity] Is it late?") + len("One. Two. Three.")
    assert printed[0] == f"saying 2 replies, {chars} characters, into {out}"
    assert [ln.split("  ")[0] for ln in printed[2:4]] == [str(out / "reply-1.wav"), str(out / "reply-2.wav")]
    # emet-say said 2 sentences and a filler into the first file and 3
    # sentences into the second; the mock's files hold none a gap divides.
    assert printed[2].endswith(", 0 sentence(s), of 3 said; first sentence not measured; no gap; "
                               "no sample after near silence; loudness step not measured"), printed[2]
    assert code == 0 and printed[-1] == ("verdict: 2 file(s) within every threshold; the loudness step not "
                                         "measured in 2 file(s), whose gaps found fewer sentences than were said")
