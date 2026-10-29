"""tools/bank/replay.py: what it reads from a replay, the checks that stop a
replay it cannot trust, its guards, and its goldens."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import manifest as bank_files
import replay
from conftest import BANK_TOOLS, real_bank_search

RATE, FS = 16000, 1280
FB = 2 * FS


def noise(seconds: float, seed: int = 1) -> bytes:
    """Random samples: no two frames of a test recording alike."""
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    return b"".join(rnd.randrange(-3000, 3000).to_bytes(2, "little", signed=True) for _ in range(n))


def engine_turn(pcm: bytes, end_frame: int, lag_ms: float, name_ms: float, after: int) -> bytes:
    """A turn's bytes as the session feeds them: the handover (the last
    `lag_ms` before the wake's frame end, its first `name_ms` silenced,
    padded to whole frames), then `after` frames read on from the wake."""
    end = 2 * end_frame * FS
    heard, name = 2 * int(lag_ms * 16), 2 * int(name_ms * 16)
    tail = bytes(name) + pcm[end - heard + name:end]
    return bytes((-len(tail)) % FB) + tail + pcm[end:end + after * FB]


def turn_record(n: int, raw: bytes) -> dict:
    return {"turn": n, "bytes": len(raw), "feed_sizes": [FB] * (len(raw) // FB),
            "fed_sha256": hashlib.sha256(raw).hexdigest(), "cassette": None, "final": "", "partials": 0, "raw": raw}


CONSOLE = """\
emet-listen: emet-sdk 0.5.5, emet-hal 0.5.5, emet-providers 0.5.5, emet-engine 0.5.5; revision abc, working tree matches
listening for 'hey emet'
  engine   pocketsphinx
  source   wav
  audio    16000 Hz, 80 ms frames
  stt      mock (streaming)

  heard 'hey emet'  (confidence 1.00)
    lag 170 ms, name cut 0 ms, level stop alone 0 ms
    then 1.8s of speech, ended on silence
    said nothing the provider could make out
  heard 'hey emet'  (confidence 1.00)
    lag 190 ms, name cut 130 ms, level stop alone 130 ms
    then 1.0s of speech, ended on silence
    said nothing the provider could make out

source ended. heard it 2 time(s).

  frames        50  (4.0s of audio)
  wakes         2   turns 2
  name cut      1 of 2 handover(s)   mean 130 ms   max 130   (the end of the name written as silence)
  verdict       kept up
"""


def a_doc(pcm: bytes) -> dict:
    frames = len(pcm) // 2
    return {
        "format": "emet-bank/1", "id": "t-0.1", "path": "../rec/t-0.1.wav", "sha256": "0" * 64,
        "sample_rate": RATE, "channels": 1, "bits": 16, "frames": frames, "duration_s": frames / RATE,
        "recorded": "2026-10-27", "start_time": None, "device": None, "capture_level": None,
        "room": "unrecorded", "position": None, "distance": "near", "distance_m": None, "facing": None,
        "speaker": "s01", "noise": None, "kind": "takes", "lead_s": 0.0, "prompt_every_s": 2.0,
        "made_with": "a test",
        "takes": [
            {"id": f"t-0.1/0{i + 1}", "kind": "one_breath", "text": "hey emet, stop", "question": "stop",
             "first_sound": "s", "slot": {"start": i * 2 * RATE, "end": (i + 1) * 2 * RATE},
             "expect_wake": True, "retake_of": None, "exclude": None}
            for i in range(2)
        ],
    }


@pytest.fixture
def run():
    """A recording, its manifest, the console and the turns of two wakes:
    frame 10 at a lag of 170 ms, and frame 30 at 190 ms with 130 ms cut."""
    pcm = noise(4.0)
    turns = [turn_record(1, engine_turn(pcm, 10, 170, 0, 5)), turn_record(2, engine_turn(pcm, 30, 190, 130, 4))]
    return a_doc(pcm), pcm, CONSOLE, turns


# ---------------------------------------------------------- reading a run


def test_a_run_is_read_wake_by_wake(run):
    doc, pcm, console, turns = run
    record = replay.read_run(doc, pcm, console, turns)
    assert record["footer"] == {"frames": 50, "wakes": 2, "turns": 2, "name_cut": "1 of 2"}
    first, second = record["wakes"]
    assert (first["take"], first["wake_end_sample"], first["lag_ms"], first["name_ms"]) == ("t-0.1/01", 12800, 170, 0)
    assert (second["take"], second["wake_end_sample"], second["name_ms"], second["level_ms"]) == ("t-0.1/02", 38400, 130, 130)
    assert first["handover_sha256"] == hashlib.sha256(turns[0]["raw"][:3 * FB]).hexdigest()
    assert first["fed_sha256"] == turns[0]["fed_sha256"] and first["cut_short"] is False
    assert record["identity"].startswith("emet-listen: ")
    assert record["all_fed_sha256"] == hashlib.sha256(turns[0]["raw"] + turns[1]["raw"]).hexdigest()


def test_a_tail_cut_short_is_read_as_heard(run):
    doc, pcm, _, _ = run
    console = CONSOLE.replace("    lag 190 ms, name cut 130 ms, level stop alone 130 ms",
                              "    lag 700 ms, name cut 0 ms, level stop alone 0 ms; the last 640 ms handed over as heard")
    console = console.replace("name cut      1 of 2", "name cut      0 of 2")
    turns = [turn_record(1, engine_turn(pcm, 10, 170, 0, 5)), turn_record(2, engine_turn(pcm, 30, 640, 0, 4))]
    second = replay.read_run(doc, pcm, console, turns)["wakes"][1]
    assert (second["lag_ms"], second["heard_ms"], second["cut_short"]) == (700, 640, True)


def test_a_handover_that_is_not_the_recordings_cut_stops_the_run(run):
    doc, pcm, console, turns = run
    raw = bytearray(turns[1]["raw"])
    raw[2 * FB - 2] ^= 1  # one sample inside the handover
    turns[1] = turn_record(2, bytes(raw))
    with pytest.raises(replay.RunProblem, match="wake 2: the handover fed is not the recording's"):
        replay.read_run(doc, pcm, console, turns)


def test_a_misread_name_cut_stops_the_run(run):
    doc, pcm, console, turns = run
    with pytest.raises(replay.RunProblem, match="the handover fed is not the recording's"):
        replay.read_run(doc, pcm, console.replace("name cut 130 ms", "name cut 120 ms"), turns)


def test_bytes_fed_after_the_handover_must_be_the_recordings(run):
    doc, pcm, console, turns = run
    raw = bytearray(turns[0]["raw"])
    raw[-1] ^= 1  # the last frame read after the wake
    turns[0] = turn_record(1, bytes(raw))
    with pytest.raises(replay.RunProblem, match="nowhere in the recording"):
        replay.read_run(doc, pcm, console, turns)


def test_counts_that_disagree_stop_the_run(run):
    doc, pcm, console, turns = run
    with pytest.raises(replay.RunProblem, match="the mock recorded 1 turns"):
        replay.read_run(doc, pcm, console, turns[:1])
    with pytest.raises(replay.RunProblem, match="no --stats footer"):
        replay.read_run(doc, pcm, console.split("source ended")[0], turns)


def test_a_line_under_no_wake_stops_the_run(run):
    doc, pcm, console, turns = run
    moved = console.replace("    then 1.8s of speech, ended on silence\n", "")
    moved = moved.replace("    said nothing the provider could make out\n  heard",
                          "    said nothing the provider could make out\n"
                          "    lag 170 ms, name cut 0 ms, level stop alone 0 ms\n  heard", 1)
    with pytest.raises(replay.RunProblem, match="a line under no wake"):
        replay.read_run(doc, pcm, moved, turns)


def test_the_header_must_say_wav_and_mock(run):
    doc, pcm, console, turns = run
    with pytest.raises(replay.RunProblem, match="transcriber is 'deepgram'"):
        replay.read_run(doc, pcm, console.replace("stt      mock", "stt      deepgram"), turns)
    with pytest.raises(replay.RunProblem, match="source is 'microphone'"):
        replay.read_run(doc, pcm, console.replace("source   wav", "source   microphone"), turns)


def test_a_wake_outside_every_slot_has_no_take(run):
    doc, pcm, console, turns = run
    doc["takes"] = doc["takes"][1:]
    assert replay.read_run(doc, pcm, console, turns)["wakes"][0]["take"] is None


def test_turns_on_disk_are_checked_against_their_index(tmp_path):
    raw = noise(0.5)
    (tmp_path / "turn-0001.raw").write_bytes(raw)
    line = {"turn": 1, "bytes": len(raw), "feed_sizes": [len(raw)], "fed_sha256": "0" * 64,
            "cassette": None, "final": "", "partials": 0}
    (tmp_path / "turns.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(replay.RunProblem, match="do not hash to"):
        replay.read_turns(tmp_path)
    line["fed_sha256"] = hashlib.sha256(raw).hexdigest()
    (tmp_path / "turns.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert replay.read_turns(tmp_path)[0]["raw"] == raw


# ------------------------------------------------------------- comparing


def golden_of(record: dict, doc: dict) -> dict:
    return replay.golden_doc(doc, record, {"threshold": "1e-15", "start_mean": "carried",
                                           "cmninit": replay.MEANS["carried"], "flags": replay.FLAGS},
                             "win32-amd64")


def test_a_golden_is_a_sound_file_and_compares_equal_to_itself(run):
    doc, pcm, console, turns = run
    doc["sha256"] = "1" * 64
    golden = golden_of(replay.read_run(doc, pcm, console, turns), doc)
    assert bank_files.problems(golden, "golden") == []
    assert replay.compare(golden, copy.deepcopy(golden)) == []


def test_every_changed_wake_is_one_line(run):
    doc, pcm, console, turns = run
    doc["sha256"] = "1" * 64
    old = golden_of(replay.read_run(doc, pcm, console, turns), doc)
    new = copy.deepcopy(old)
    new["wakes"][1]["lag_ms"] = 200.0
    new["wakes"][1]["handover_sha256"] = "2" * 64
    new["footer"]["name_cut"] = "0 of 2"
    lines = replay.compare(old, new)
    assert lines == ["t-0.1 footer: name_cut \"1 of 2\" -> \"0 of 2\"",
                     "t-0.1 wake 2 (t-0.1/02): lag_ms 190 -> 200, handover_sha256 " + old["wakes"][1]["handover_sha256"][:8] + " -> 22222222"]
    gone = copy.deepcopy(old)
    gone["wakes"].pop()
    assert replay.compare(old, gone) == ["t-0.1 wake 2: gone, was take t-0.1/02 at sample 38400"]


WAKE_CHANGES = {
    "take": "t-0.1/09", "wake_end_sample": 1, "lag_ms": 999.0, "name_ms": 999.0, "level_ms": 999.0,
    "heard_ms": 999.0, "cut_short": True, "handover_sha256": "9" * 64, "fed_sha256": "8" * 64,
    "final": "a word", "cassette": "hit",
}


@pytest.mark.parametrize("key", list(WAKE_CHANGES))
def test_any_field_of_a_wake_that_changes_is_named(run, key):
    doc, pcm, console, turns = run
    doc["sha256"] = "1" * 64
    old = golden_of(replay.read_run(doc, pcm, console, turns), doc)
    assert set(WAKE_CHANGES) == set(old["wakes"][0]) - {"n"}
    new = copy.deepcopy(old)
    new["wakes"][0][key] = WAKE_CHANGES[key]
    lines = replay.compare(old, new)
    assert len(lines) == 1 and lines[0].startswith("t-0.1 wake 1 ") and f" {key} " in lines[0], lines


@pytest.mark.parametrize("part, key, value", [
    ("footer", "frames", 49), ("footer", "wakes", 3), ("footer", "turns", 3), ("footer", "name_cut", "2 of 2"),
    ("conditions", "threshold", "1e-20"), ("conditions", "start_mean", "default"), ("conditions", "cmninit", None),
    ("conditions", "flags", ["--replay"]),
])
def test_any_field_of_the_footer_or_conditions_that_changes_is_named(run, part, key, value):
    doc, pcm, console, turns = run
    doc["sha256"] = "1" * 64
    old = golden_of(replay.read_run(doc, pcm, console, turns), doc)
    new = copy.deepcopy(old)
    new[part][key] = value
    lines = replay.compare(old, new)
    assert len(lines) == 1 and lines[0].startswith(f"t-0.1 {part}: {key} "), lines


def test_a_wake_is_placed_after_everything_the_turn_before_read(run):
    """Turn 2's bytes also sit inside the audio turn 1 read on from its
    wake; the second wake is where turn 2 was, after turn 1's audio."""
    doc, _, _, _ = run
    pcm = bytearray(noise(4.0))
    pcm[15 * FB:18 * FB] = pcm[40 * FB:43 * FB]
    pcm = bytes(pcm)
    console = CONSOLE.replace("    lag 170 ms, name cut 0 ms, level stop alone 0 ms\n", "").replace(
        "    lag 190 ms, name cut 130 ms, level stop alone 130 ms\n", "").replace(
        "  name cut      1 of 2 handover(s)   mean 130 ms   max 130   (the end of the name written as silence)\n", "")
    turns = [turn_record(1, pcm[10 * FB:20 * FB]), turn_record(2, pcm[40 * FB:43 * FB])]
    wakes = replay.read_run(doc, pcm, console, turns)["wakes"]
    assert [w["wake_end_sample"] for w in wakes] == [10 * FS, 40 * FS]


def test_bytes_found_in_two_places_stop_the_run():
    clip = noise(2.0)
    pcm = clip + clip  # frames 25 to 49 repeat 0 to 24, byte for byte
    turn = turn_record(1, pcm[10 * FB:15 * FB])
    with pytest.raises(replay.RunProblem, match=r"at frames \[10, 35\]; which one the wake was cannot be told"):
        replay.locate_wake(turn, 0, pcm, FB, replay.frame_index(pcm, FB), 0)


def test_a_golden_of_another_recording_is_never_compared(run):
    doc, pcm, console, turns = run
    doc["sha256"] = "1" * 64
    old = golden_of(replay.read_run(doc, pcm, console, turns), doc)
    new = {**copy.deepcopy(old), "sha256": "3" * 64}
    assert replay.compare(old, new) == ["t-0.1: the golden was made from another recording (11111111, this is 33333333)"]


def test_the_body_written_for_a_replay_is_the_harmless_one(tmp_path):
    from emet_sdk.validate import load_yaml

    path = tmp_path / "body.yaml"
    path.write_text(replay.body_yaml(replay.MEANS["carried"], "1e-15", tmp_path / "t u", None, None), encoding="utf-8")
    body = load_yaml(path)
    assert body["audio"]["output"]["sink"] == "null"
    assert body["audio"]["wake"]["params"] == {"threshold": 1e-15, "cmninit": replay.MEANS["carried"]}
    assert body["models"]["stt"] == {"provider": "mock", "params": {"record_turns": str(tmp_path / "t u")}}
    path.write_text(replay.body_yaml(None, "1e-20", tmp_path, "c.json", {"provider": "deepgram"}), encoding="utf-8")
    body = load_yaml(path)
    assert "cmninit" not in body["audio"]["wake"]["params"] and body["audio"]["wake"]["params"]["threshold"] == 1e-20
    assert body["models"]["stt"]["params"]["cassette_request"] == {"provider": "deepgram"}


# ------------------------------------------------------------------ guards

GUARD_PROBE = """
import asyncio, socket, sys, _socket
sys.path.insert(0, sys.argv[1])
import replay
refused = []
replay.install_guards(refused)
try:
    import sounddevice
    print("sounddevice imported")
except ImportError:
    print("no sounddevice")

def attempt(what, fn):
    try:
        fn()
        print(what, "connected")
    except OSError as exc:
        print(what, "refused", type(exc).__name__)

for host in ("192.0.2.1", "api.deepgram.com"):
    attempt("create_connection " + host, lambda: socket.create_connection((host, 443), timeout=1))
attempt("socket.socket", lambda: socket.socket().connect(("192.0.2.1", 443)))
attempt("_socket.socket", lambda: _socket.socket().connect(("192.0.2.1", 443)))

async def through_the_loop():
    # A literal address skips the loop's name lookup, so this reaches the
    # loop's own connect: ConnectEx under the proactor on Windows.
    await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 443), 2)

attempt("asyncio", lambda: asyncio.run(through_the_loop()))
a, b = socket.socketpair()
a.sendall(b"x")
print("loopback", b.recv(1))
print("counted", len(refused))
"""


def test_the_guards_refuse_the_network_and_portaudio_and_keep_loopback(tmp_path):
    """Every way out is refused before a packet leaves: a name lookup, a
    Python socket, the C socket under it, and the event loop's own connect
    (ConnectEx on Windows, which no socket audit event sees). Loopback
    stays open for the loop's self-pipe."""
    probe = tmp_path / "probe.py"
    probe.write_text(GUARD_PROBE, encoding="utf-8")
    out = subprocess.run([sys.executable, str(probe), str(BANK_TOOLS)], capture_output=True, text=True, timeout=60).stdout
    assert "no sounddevice" in out
    assert "create_connection 192.0.2.1 refused PermissionError" in out
    assert "create_connection api.deepgram.com refused PermissionError" in out
    assert "socket.socket refused PermissionError" in out and "_socket.socket refused PermissionError" in out
    assert "asyncio refused PermissionError" in out
    assert "loopback b'x'" in out
    assert "counted 5" in out


def fake_child(tmp_path: Path, monkeypatch, result: dict) -> None:
    """replay_one's child process, faked: it writes `result` and exits 0."""

    def run(cmd, **kwargs):
        cfg = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        Path(cfg["result"]).write_text(json.dumps(result), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(replay.subprocess, "run", run)


def conditions() -> dict:
    return {"threshold": "1e-15", "start_mean": "carried", "cmninit": replay.MEANS["carried"], "flags": replay.FLAGS}


def test_a_run_that_tried_the_network_fails(tmp_path, monkeypatch):
    fake_child(tmp_path, monkeypatch, {"exit": 0, "refused": ["connect 192.0.2.1"], "preflight": []})
    with pytest.raises(replay.RunProblem, match="1 network attempt"):
        replay.replay_one(a_doc(noise(1.0)), tmp_path / "x.wav", tmp_path / "work", conditions(), None, None)


def test_a_run_refused_before_it_started_fails(tmp_path, monkeypatch):
    fake_child(tmp_path, monkeypatch, {"exit": None, "refused": [], "preflight": ["the sink is 'speaker', not null"]})
    with pytest.raises(replay.RunProblem, match="refused before the run: the sink is 'speaker'"):
        replay.replay_one(a_doc(noise(1.0)), tmp_path / "x.wav", tmp_path / "work", conditions(), None, None)


def child_config(tmp_path: Path) -> Path:
    cfg = {name: str(tmp_path / name) for name in ("manifest", "soul", "wav", "keys", "state", "console", "result")}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_the_child_guards_then_preflights_then_runs_with_the_empty_keys_file(tmp_path, monkeypatch):
    import emet_engine.cli

    calls: list = []
    monkeypatch.setattr(replay, "install_guards", lambda refused: calls.append("guards"))
    monkeypatch.setattr(replay, "preflight", lambda m, s: calls.append(("preflight", str(m), str(s))) or [])
    monkeypatch.setattr(emet_engine.cli, "main", lambda argv: calls.append(("main", argv)) or 0)
    path = child_config(tmp_path)
    assert replay.run_child(path) == 0
    t = str(tmp_path)
    assert calls[0] == "guards"
    assert calls[1] == ("preflight", f"{t}{replay.os.sep}manifest", f"{t}{replay.os.sep}soul")
    assert calls[2][1] == [str(tmp_path / "manifest"), str(tmp_path / "soul"), "--replay", str(tmp_path / "wav"),
                           "--transcribe", "--stats", "--keys", str(tmp_path / "keys"), "--state", str(tmp_path / "state")]
    assert json.loads((tmp_path / "result").read_text(encoding="utf-8"))["exit"] == 0


def test_the_child_never_runs_when_the_preflight_refuses(tmp_path, monkeypatch):
    import emet_engine.cli

    monkeypatch.setattr(replay, "install_guards", lambda refused: None)
    monkeypatch.setattr(replay, "preflight", lambda m, s: ["the transcriber is 'deepgram', not mock"])
    monkeypatch.setattr(emet_engine.cli, "main", lambda argv: pytest.fail("ran past a refused preflight"))
    replay.run_child(child_config(tmp_path))
    result = json.loads((tmp_path / "result").read_text(encoding="utf-8"))
    assert result == {"exit": None, "refused": [], "preflight": ["the transcriber is 'deepgram', not mock"]}


def test_preflight_refuses_a_real_sink_and_a_real_transcriber(tmp_path):
    pytest.importorskip("emet_hal")
    body, soul = tmp_path / "body.yaml", tmp_path / "soul.yaml"
    soul.write_text(replay.SOUL_YAML, encoding="utf-8")
    body.write_text(replay.body_yaml(None, "1e-15", tmp_path / "turns", None, None), encoding="utf-8")
    assert replay.preflight(body, soul) == []
    body.write_text(body.read_text(encoding="utf-8").replace('sink: "null"', 'sink: "speaker"'), encoding="utf-8")
    assert replay.preflight(body, soul) == ["the sink is 'speaker', not null"]
    body.write_text(replay.body_yaml(None, "1e-15", tmp_path / "turns", None, None).replace(
        "provider: mock", "provider: deepgram"), encoding="utf-8")
    assert replay.preflight(body, soul) == ["the transcriber is 'deepgram', not mock"]


# --------------------------------------------------------- whole replays


def tiny_bank(tmp_path: Path) -> Path:
    from conftest import write_wav

    wav = write_wav(tmp_path / "rec" / "t-0.1.wav", noise(4.0))
    doc = a_doc(noise(4.0))
    doc["sha256"] = bank_files.sha256_file(wav)
    root = tmp_path / "bank"
    bank_files.write(root / "manifests" / "t-0.1.json", doc, "manifest")
    bank_files.write(root / "bank.json", {
        "format": "emet-bank/1", "version": "2026-10-27", "files": ["manifests/t-0.1.json"],
        "rooms": {"unrecorded": {"description": "no record names the room"}},
        "speakers": {"s01": {"consent": "consent/s01.txt"}}}, "bank")
    return root


def test_update_then_compare_on_a_recording_with_no_wake(tmp_path, capsys):
    """The whole path, emet-listen included: no device, no network, a golden
    written and then matched; a golden whose conditions differ is refused."""
    pytest.importorskip("pocketsphinx")
    root, golden = tiny_bank(tmp_path), tmp_path / "golden"
    args = [str(root), "--golden", str(golden), "--platform", "linux-test"]
    assert replay.main(args + ["--update"]) == 0
    written = bank_files.read(golden / "linux-test" / "t-0.1.json", "golden")
    assert written["footer"] == {"frames": 50, "wakes": 0, "turns": 0, "name_cut": None}
    assert replay.main(args) == 0
    assert "same as the golden" in capsys.readouterr().out
    assert replay.main(args + ["--start-mean", "default"]) == 1
    assert "the golden was made under" in capsys.readouterr().out
    written["footer"]["frames"] = 49
    bank_files.write(golden / "linux-test" / "t-0.1.json", written, "golden")
    assert replay.main(args) == 1
    assert "t-0.1 footer: frames 49 -> 50" in capsys.readouterr().out


def test_a_recording_with_no_golden_fails_the_compare(tmp_path, capsys):
    pytest.importorskip("pocketsphinx")
    root = tiny_bank(tmp_path)
    assert replay.main([str(root), "--golden", str(tmp_path / "golden"), "--platform", "linux-test"]) == 1
    assert "t-0.1: NO GOLDEN for linux-test" in capsys.readouterr().out


def test_a_mistyped_recording_id_is_an_error(tmp_path, capsys):
    root = tiny_bank(tmp_path)
    assert replay.main([str(root), "--only", "t-0.2", "--golden", str(tmp_path / "golden")]) == 2
    assert "no recording in the bank is called t-0.2; it holds t-0.1" in capsys.readouterr().err


# -------------------------------------------------------------- cassettes


def cassette_file(path: Path, raw: bytes, smart_format: str, final: str) -> Path:
    from emet_providers import cassette

    request = replay.shipped_request(16000)
    request["query"]["smart_format"] = smart_format
    recorder = cassette.TurnRecorder(request)
    recorder.feed(raw)
    return cassette.save(path, [recorder.turn(final, recorded="2026-10-28", source="a test")])


def test_the_shipped_request_is_what_the_plugin_sends():
    from emet_providers.deepgram import FIXED_QUERY

    want = {k: v for k, v in FIXED_QUERY.items()}
    assert replay.shipped_request(16000) == {"provider": "deepgram", "model": "nova-3", "sample_rate": 16000,
                                             "query": dict(sorted(want.items()))}


def test_under_the_shipped_request_a_turn_recorded_with_another_query_never_answers(tmp_path):
    """Two cassettes hold the same bytes, the first by name recorded with
    smart_format true. Without a request the first wins; with the shipped
    one, the turn recorded as the plugin sends today answers."""
    import asyncio

    from emet_providers.mock import MockTranscriber
    from emet_sdk.types import AudioFormat

    raw = noise(0.4)
    (tmp_path / "c").mkdir()
    cassette_file(tmp_path / "c" / "a-old.json", raw, "true", "What's 22")
    cassette_file(tmp_path / "c" / "b-shipped.json", raw, "false", "What's two plus two")

    def final(request):
        params = {"cassette": str(tmp_path / "c")}
        if request is not None:
            params["cassette_request"] = request
        mock = MockTranscriber({"provider": "mock", "params": params}, AudioFormat(sample_rate=16000))

        async def go():
            await mock.start()
            await mock.feed(raw)
            return (await mock.finish()).text

        return asyncio.run(go())

    assert final(None) == "What's 22"
    assert final(replay.shipped_request(16000)) == "What's two plus two"


def test_a_cassette_run_stands_for_the_shipped_request_unless_told_otherwise(tmp_path, monkeypatch, capsys):
    root = tiny_bank(tmp_path)
    seen: list = []

    def fake(doc, wav, work, conditions, cassette, request):
        seen.append((cassette, request))
        raise replay.RunProblem("stopped by the test")

    monkeypatch.setattr(replay, "replay_one", fake)
    args = [str(root), "--golden", str(tmp_path / "golden"), "--cassette", "cassettes"]
    assert replay.main(args) == 1
    other = {"provider": "deepgram", "model": "nova-2", "sample_rate": 16000, "query": {}}
    assert replay.main(args + ["--cassette-request", json.dumps(other)]) == 1
    assert replay.main([str(root), "--golden", str(tmp_path / "golden")]) == 1
    assert seen == [("cassettes", replay.shipped_request(16000)), ("cassettes", other), (None, None)]


def test_the_miss_list_holds_each_missed_turns_bytes_for_the_paid_replay(tmp_path):
    import paid_replay

    raw = noise(0.32)
    misses = [{"recording": "t-0.1", "recording_sha256": "0" * 64, "turn": 3, "take": "t-0.1/02",
               "fed_sha256": hashlib.sha256(raw).hexdigest(), "fed_bytes": len(raw), "feed_sizes": [FB] * 4,
               "handover_feeds": 2, "raw": raw}]
    replay.write_misses(tmp_path / "m" / "misses.json", misses)
    doc = json.loads((tmp_path / "m" / "misses.json").read_text(encoding="utf-8"))
    assert doc["format"] == "emet-misses/1" and doc["turns"][0]["raw"] == "t-0.1-turn-0003.raw"
    assert (tmp_path / "m" / "t-0.1-turn-0003.raw").read_bytes() == raw
    back = paid_replay.read_misses(tmp_path / "m" / "misses.json")
    assert back[0]["bytes"] == raw and back[0]["handover_feeds"] == 2


def test_the_banks_cassettes_answer_two_plus_two_as_the_shipped_plugin_heard_it(real_bank, tmp_path, capsys):
    """dg05 (smart_format true) and dg08 (false) hold the same bytes for
    two-plus-two; the replay takes dg08's, and how-far, which no paid
    replay heard, goes on the miss list."""
    pytest.importorskip("pocketsphinx")
    runs = real_bank.parent / "releases" / "v0.5.3" / "harness" / "runs"
    if not (real_bank / "cassettes" / "dg08.json").is_file() or not runs.is_dir():
        pytest.skip("no seeded cassettes or no paid runs on this machine")
    dg08 = json.loads(next(runs.glob("dg08_*")).joinpath("deepgram.json").read_text(encoding="utf-8"))
    report, misses = tmp_path / "r.json", tmp_path / "m" / "misses.json"
    args = [str(real_bank), "--only", "two-plus-two-0.5.2", "--only", "how-far-0.5.3", "--cassette",
            str(real_bank / "cassettes"), "--report", str(report), "--misses", str(misses)]
    args += [a for d in real_bank_search() for a in ("--search", str(d))]
    try:
        assert replay.main(args) == 0, capsys.readouterr().out
        got = json.loads(report.read_text(encoding="utf-8"))["recordings"]
        assert [w["cassette"] for w in got["two-plus-two-0.5.2"]["wakes"]] == ["hit"] * 7
        assert [w["final"] for w in got["two-plus-two-0.5.2"]["wakes"]] == [t["final"]["text"] for t in dg08["turns"]]
        import paid_replay

        assert len(paid_replay.read_misses(misses)) == len(got["how-far-0.5.3"]["wakes"]) == 8
    finally:
        shutil.rmtree(tmp_path / "m", ignore_errors=True)  # the speaker's bytes
        report.unlink(missing_ok=True)  # and the words they were heard as


def test_a_bank_recording_still_matches_this_platforms_golden(real_bank, capsys):
    """Two-plus-two, the shortest bank recording with a moved cut, against
    the golden for this machine's platform."""
    pytest.importorskip("pocketsphinx")
    if not (replay.GOLDEN / replay.platform_name() / "two-plus-two-0.5.2.json").is_file():
        pytest.skip(f"no golden for {replay.platform_name()}")
    args = [str(real_bank), "--only", "two-plus-two-0.5.2"] + [a for d in real_bank_search() for a in ("--search", str(d))]
    assert replay.main(args) == 0, capsys.readouterr().out
