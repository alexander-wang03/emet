"""`tools/hardware_guard.py`, the guard every suite loads (0.5.7-D10 (a)).

The first tests call the guard's parts in this process, where the tools
suite's own conftest has installed it. The rest write a small tree to a
temporary folder, with a copy of the guard and of a suite's conftest, and
run pytest on it in a child process, so a refusal there fails a child's
test and never this one.

No test here reaches a device or the network. An address that is refused
is 192.0.2.1, TEST-NET-1, which RFC 5737 reserves for documentation; the
guard refuses it before any packet is sent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD_FILE = REPO / "tools" / "hardware_guard.py"
guard = sys.modules["emet_hardware_guard"]

#: The five suites' conftests, each of which loads the guard.
CONFTESTS = ("emet-sdk/tests/conftest.py", "emet-hal/tests/conftest.py", "emet-providers/tests/conftest.py",
             "emet-engine/tests/conftest.py", "tools/tests/conftest.py")

OFF_MACHINE = "192.0.2.1"


@pytest.fixture
def no_refusal_left():
    """A test here that makes a refusal on purpose clears it, so its own
    teardown does not fail on what it meant to provoke."""
    yield
    guard.STATE.refused.clear()


# ------------------------------------------------------- its parts, here


@pytest.mark.parametrize("address, here", [
    (("127.0.0.1", 80), True), (("127.9.9.9", 80), True), (("::1", 80, 0, 0), True), (("[::1]", 80), True),
    (("localhost", 80), True), (("LOCALHOST", 80), True), ("/tmp/emet.sock", True), (b"/tmp/emet.sock", True),
    ((OFF_MACHINE, 80), False), (("10.0.0.5", 80), False), (("api.openai.com", 443), False),
    (("localhost.example.com", 80), False), (("::ffff:192.0.2.1", 80), False), ((), False),
])
def test_only_loopback_and_unix_sockets_stay_on_this_machine(address, here):
    assert guard.on_this_machine(address) is here


@pytest.mark.parametrize("path, streams, connects", [
    ("emet-hal/tests/test_audio_virtual.py", True, False),
    ("copy/emet-hal/mutants/tests/test_audio_virtual.py", True, False),
    ("emet-hal/tests/test_audio.py", False, False),
    ("emet-engine/tests/test_audio_virtual.py", False, False),
    ("emet-providers/tests/test_llm_live.py", False, True),
    ("emet-providers/tests/test_deepgram_live.py", False, True),
    ("emet-engine/tests/test_talk_live.py", False, False),
    ("emet-providers/tests/test_openai.py", False, False),
])
def test_which_files_may_open_a_stream_or_connect(path, streams, connects):
    assert guard.may_open_streams(Path(path)) is streams
    assert guard.may_connect(Path(path)) is connects


def test_a_connection_off_this_machine_is_refused_and_kept_for_the_teardown(no_refusal_left, request):
    called = []
    connect = guard._guard_connect(lambda sock, address: called.append(address))
    with pytest.raises(guard.HardwareRefused) as refused:
        connect(None, (OFF_MACHINE, 443))
    assert called == [] and isinstance(refused.value, OSError)
    text = guard.STATE.refused[-1]
    assert text == str(refused.value)
    assert text.startswith(f"the hardware guard refused a connection to {OFF_MACHINE}:443 in "
                           f"{request.node.nodeid}. Tests open no sound device")
    assert text.endswith('declare sink "null" and source "wav", and use a fake transport. Only '
                         "test_audio_virtual.py may open a stream, and only a *_live.py file may connect.")
    connect(None, ("127.0.0.1", 443))
    assert called == [("127.0.0.1", 443)]


def test_a_live_file_may_connect(monkeypatch):
    called = []
    connect = guard._guard_connect(lambda sock, address: called.append(address))
    monkeypatch.setattr(guard.STATE, "path", REPO / "emet-providers" / "tests" / "test_llm_live.py")
    connect(None, (OFF_MACHINE, 443))
    assert called == [(OFF_MACHINE, 443)]


def test_the_socket_methods_and_the_proactor_are_wrapped_once():
    import socket

    assert socket.socket.connect.__wrapped__ is not None
    assert socket.socket.connect_ex.__wrapped__ is not None
    assert not hasattr(socket.socket.connect.__wrapped__, "__wrapped__"), "installed once"
    if sys.platform == "win32":
        from asyncio import windows_events

        assert windows_events.IocpProactor.connect.__wrapped__ is not None
    guard.install()
    assert not hasattr(socket.socket.connect.__wrapped__, "__wrapped__")


def test_a_fake_sounddevice_is_left_alone():
    fake = types.ModuleType("sounddevice")
    fake.InputStream = object
    guard.guard_sounddevice(fake)
    assert fake.InputStream is object


def test_the_real_module_is_known_by_its_portaudio_handle(no_refusal_left):
    real = types.ModuleType("sounddevice")
    real._lib = object()
    real.RawInputStream = lambda *a, **k: "opened"
    real.query_devices = lambda *a, **k: ["a device"]
    guard.guard_sounddevice(real)
    assert real.query_devices() == ["a device"], "a query passes"
    with pytest.raises(guard.HardwareRefused, match=r"refused sounddevice\.RawInputStream in \S*test_hardware_guard"):
        real.RawInputStream(samplerate=16000)
    guard.guard_sounddevice(real)
    assert real.RawInputStream.__wrapped__() == "opened", "wrapped once"


def test_each_suite_loads_the_guard_the_same_way():
    blocks = []
    for name in CONFTESTS:
        text = (REPO / name).read_text(encoding="utf-8")
        start = text.index("def _hardware_guard():")
        blocks.append(text[start:])
        assert text.rstrip().endswith("hardware_guard = _hardware_guard().hardware_guard"), name
    assert len(set(blocks)) == 1, "the five blocks differ"


# ------------------------------------------------- in a child's pytest


def child_tree(tmp_path: Path, tests: dict[str, str], *, package: str = "emet-sdk", with_guard: bool = True,
               extra: dict[str, str] | None = None) -> Path:
    """A tree with the guard, one suite's conftest and these test files."""
    root = tmp_path / "emet"
    folder = root / package / "tests"
    folder.mkdir(parents=True)
    (folder / "conftest.py").write_text((REPO / "emet-sdk" / "tests" / "conftest.py").read_text(encoding="utf-8"),
                                        encoding="utf-8")
    if with_guard:
        (root / "tools").mkdir()
        (root / "tools" / "hardware_guard.py").write_text(GUARD_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    for name, text in {**tests, **(extra or {})}.items():
        path = root / package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / package


def child_pytest(folder: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["PYTHONPATH"] = str(folder)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", *args],
                          cwd=folder, env=env, capture_output=True, text=True, timeout=120)


CONNECTS = f'''\
import asyncio
import socket


def test_a_socket_off_this_machine():
    socket.create_connection(("{OFF_MACHINE}", 9), timeout=1)


def test_asyncio_on_the_default_loop():
    async def go():
        await asyncio.wait_for(asyncio.open_connection("{OFF_MACHINE}", 9), 2)
    asyncio.run(go())


def test_an_error_swallowed_still_fails():
    try:
        socket.create_connection(("{OFF_MACHINE}", 9), timeout=1)
    except OSError:
        pass


def test_loopback_passes():
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = server.accept()
    for s in (client, conn, server):
        s.close()


def test_a_name_other_than_localhost_is_refused_unlooked_up():
    socket.create_connection(("api.example.com", 443), timeout=1)
'''


def test_a_childs_tests_that_connect_off_this_machine_fail_and_loopback_passes(tmp_path):
    done = child_pytest(child_tree(tmp_path, {"tests/test_net.py": CONNECTS}))
    out = done.stdout
    assert done.returncode == 1, out
    for name in ("test_a_socket_off_this_machine", "test_asyncio_on_the_default_loop",
                 "test_an_error_swallowed_still_fails", "test_a_name_other_than_localhost_is_refused_unlooked_up"):
        assert f"ERROR tests/test_net.py::{name}" in out, out
    assert "PASSED tests/test_net.py::test_loopback_passes" in out
    assert (f"the hardware guard refused a connection to {OFF_MACHINE}:9 in "
            "tests/test_net.py::test_an_error_swallowed_still_fails") in out
    assert "the hardware guard refused a name lookup of api.example.com" in out


SOUNDDEVICE = '''\
"""A stand-in that looks like the real module: it holds a `_lib`."""
_lib = object()


def RawOutputStream(*args, **kwargs):
    return "opened"


def query_devices(*args, **kwargs):
    return ["a device"]
'''

STREAMS = '''\
def test_a_stream():
    import sounddevice
    assert sounddevice.query_devices() == ["a device"]
    assert sounddevice.RawOutputStream(samplerate=22050) == "opened"
'''


def test_a_stream_is_refused_outside_the_virtual_card_file_and_opened_inside_it(tmp_path):
    folder = child_tree(tmp_path, {"tests/test_audio.py": STREAMS, "tests/test_audio_virtual.py": STREAMS},
                        package="emet-hal", extra={"sounddevice.py": SOUNDDEVICE})
    done = child_pytest(folder)
    assert done.returncode == 1, done.stdout
    assert "FAILED tests/test_audio.py::test_a_stream" in done.stdout
    assert "refused sounddevice.RawOutputStream in tests/test_audio.py::test_a_stream" in done.stdout
    assert "PASSED tests/test_audio_virtual.py::test_a_stream" in done.stdout


RELOADED = '''\
import importlib
import sys

import pytest

guard = sys.modules["emet_hardware_guard"]


def test_a_reloaded_sounddevice_is_still_guarded():
    import sounddevice
    for _ in range(3):
        with pytest.raises(guard.HardwareRefused):
            sounddevice.RawOutputStream(samplerate=22050)
        assert not guard.guarded(sounddevice.RawOutputStream.__wrapped__), "wrapped once"
        importlib.reload(sounddevice)
    guard.STATE.refused.clear()
'''


def test_a_reloaded_sounddevice_is_guarded_again(tmp_path):
    """`importlib.reload` runs the module's code again in the same module
    object, which puts the bare openers back. The guard decides opener by
    opener, so a reload is guarded as an import is (review finding
    checks-ci/code#3, 2026-11-27)."""
    folder = child_tree(tmp_path, {"tests/test_reload.py": RELOADED}, package="emet-hal",
                        extra={"sounddevice.py": SOUNDDEVICE})
    done = child_pytest(folder)
    assert done.returncode == 0, done.stdout
    assert "PASSED tests/test_reload.py::test_a_reloaded_sounddevice_is_still_guarded" in done.stdout


#: A child process that loads the guard by path, as a conftest does, with
#: `sounddevice` imported before `install()` or after it. It prints what it
#: found and opens no stream.
IMPORTED = '''\
import importlib.util
import sys


def load():
    spec = importlib.util.spec_from_file_location("emet_hardware_guard", sys.argv[1])
    guard = importlib.util.module_from_spec(spec)
    sys.modules["emet_hardware_guard"] = guard
    spec.loader.exec_module(guard)
    guard.install()
    return guard


guard = load() if sys.argv[2] == "after" else None
try:
    import sounddevice
except (ImportError, OSError) as exc:
    print("unimportable:", type(exc).__name__, exc)
    raise SystemExit(0)
guard = guard or load()
print("real:", guard.is_real_sounddevice(sounddevice))
for name in guard.STREAM_NAMES:
    if hasattr(sounddevice, name):
        print(name, "wrapped" if guard.guarded(getattr(sounddevice, name)) else "NOT wrapped")
if sys.argv[3:] == ["open"]:
    try:
        print("opened:", sounddevice.RawOutputStream(samplerate=22050))
    except guard.HardwareRefused as exc:
        print("refused:", str(exc).split(" in ", 1)[0])
'''


def imported(tmp_path: Path, when: str, *extra: str, path: str | None = None) -> list[str]:
    script = tmp_path / "imported.py"
    script.write_text(IMPORTED, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.pop("PYTHONPATH", None)
    if path:
        env["PYTHONPATH"] = path
    done = subprocess.run([sys.executable, str(script), str(GUARD_FILE), when, *extra], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.splitlines()


def test_a_sounddevice_imported_before_the_guard_is_guarded_when_it_installs(tmp_path):
    """`install()` guards a `sounddevice` already in `sys.modules`, as a
    suite that imports it before its conftest loads the guard has it."""
    folder = tmp_path / "standin"
    folder.mkdir()
    (folder / "sounddevice.py").write_text(SOUNDDEVICE + "\n\ndef InputStream(*a, **k):\n    return 'opened'\n",
                                          encoding="utf-8")
    out = imported(tmp_path, "before", "open", path=str(folder))
    assert out == ["real: True", "InputStream wrapped", "RawOutputStream wrapped",
                   "refused: the hardware guard refused sounddevice.RawOutputStream"]


@pytest.mark.parametrize("when", ["after", "before"])
def test_the_installed_sounddevice_is_known_as_real_and_every_opener_is_wrapped(tmp_path, when):
    """Every other test here guards a stand-in that holds `_lib`. This one
    imports the sounddevice the venv has, in a child process, and opens no
    stream: if a release renames the PortAudio handle the guard knows it by,
    the guard would leave every stream open, and this fails (review finding
    checks-ci/tests#3, 2026-11-27). It is skipped where sounddevice or
    PortAudio is absent, as in CI's suites."""
    out = imported(tmp_path, when)
    if out and out[0].startswith("unimportable:"):
        pytest.skip(f"sounddevice cannot be imported here ({out[0]})")
    assert out == ["real: True", *(f"{name} wrapped" for name in guard.STREAM_NAMES)]


def test_a_tree_without_the_guard_fails_the_collection_naming_it(tmp_path):
    folder = child_tree(tmp_path, {"tests/test_one.py": "def test_one():\n    pass\n"}, with_guard=False)
    done = child_pytest(folder)
    assert done.returncode != 0
    text = done.stdout + done.stderr
    assert "no tools/hardware_guard.py in " in text and "these tests do not run without it" in text
    assert "1 passed" not in text
