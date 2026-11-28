"""Tests open no sound device stream and reach no address off this machine.

CLAUDE.md's rule 7 says a test never touches audio hardware, and the suites
held it by their helpers' defaults: `sink: "null"`, the wav source, a fake
transport. The keys guard of 0.4 and the voice guard of 0.5 were each added
after a test reached a real file. This one is 0.5.7-D10 (a), added before
a test opens a real device or a vendor.

Every suite's `conftest.py` (sdk, hal, providers, engine, tools) walks up
from its own folder to the first `tools/hardware_guard.py`, loads it by
path, calls `install()` and takes its `hardware_guard` fixture, which is
autouse. A conftest that finds no guard fails the collection and names the
file. `tools/ci.py`'s `mutmut_folder` puts the guard in each mutmut copy,
since mutmut runs a package's tests from `<copy>/<package>/mutants/tests/`.

**What is refused**, in any test outside `emet-hal/tests/test_audio_virtual.py`
(streams) and `emet-providers/tests/*_live.py` (connections):

- a sound device stream: `sounddevice`'s `InputStream`, `OutputStream`,
  `Stream`, their `Raw` forms, `play`, `rec` and `playrec`. Device queries
  (`query_devices`, `check_input_settings`, `default`) pass. The real
  `sounddevice` is wrapped when it is imported or reloaded, and only it:
  a module without the PortAudio handle `_lib`, such as a test's fake put
  into `sys.modules`, is left alone.
- a connection to an address off this machine: `socket.socket.connect` and
  `connect_ex`, and, on Windows, asyncio's proactor connect. Loopback
  (127.0.0.0/8, `::1`, the name `localhost`) and `AF_UNIX` pass.
- a name lookup (`socket.getaddrinfo`) of any name but `localhost`, since
  `socket.create_connection` and asyncio look a name up before they connect
  and the lookup itself reaches the network. A numeric address passes, as
  it is looked up nowhere.

**asyncio on Windows.** The default event loop there is the proactor, whose
`IocpProactor.connect` hands the socket to `ConnectEx` and never calls
`socket.connect` (CPython 3.13's `asyncio/windows_events.py`, read
2026-11-27 on the laptop, Python 3.13.2). So the guard wraps that method as
well. The selector loop, the default elsewhere, calls `sock.connect` and is
covered by the socket wrapper. A connection made in another process, such as
a console script a test starts, is outside this process's guard.

A refusal raises `HardwareRefused`, an `OSError`, so the code under test
takes it as it takes a device or a network that is not there. Since code
can swallow an `OSError`, each refusal is also kept, and the fixture fails
the test at its teardown with the guard's message.

Standard library and pytest only.
"""

from __future__ import annotations

import importlib.abc
import ipaddress
import socket
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

#: The one file that may open a stream, and the files that may connect.
STREAM_FILE = ("emet-hal", "test_audio_virtual.py")
LIVE_PACKAGE, LIVE_SUFFIX = "emet-providers", "_live.py"

#: What `sounddevice` opens a device with. Its queries are not listed.
STREAM_NAMES = ("InputStream", "OutputStream", "Stream", "RawInputStream", "RawOutputStream", "RawStream",
                "play", "rec", "playrec")

MESSAGE = ('the hardware guard refused {what} in {nodeid}. Tests open no sound device and reach no network: '
           'declare sink "null" and source "wav", and use a fake transport. Only test_audio_virtual.py may '
           'open a stream, and only a *_live.py file may connect.')


class HardwareRefused(OSError):
    """A device stream or a connection the guard would not open."""


class _State:
    nodeid: str = "(outside a test)"
    path: Path | None = None
    refused: list[str] = []
    installed: bool = False


STATE = _State()


def may_open_streams(path: Path | None) -> bool:
    """Whether the test file at `path` may open a sound device stream."""
    return path is not None and path.name == STREAM_FILE[1] and STREAM_FILE[0] in path.parts


def may_connect(path: Path | None) -> bool:
    """Whether the test file at `path` may connect off this machine."""
    return path is not None and path.name.endswith(LIVE_SUFFIX) and LIVE_PACKAGE in path.parts


def on_this_machine(address: Any) -> bool:
    """Whether a socket address stays on this machine. A path (`AF_UNIX`)
    does; a host and port does when the host is `localhost` or a loopback
    address. Nothing is looked up."""
    if isinstance(address, (str, bytes)):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def refuse(what: str) -> HardwareRefused:
    """Keep the refusal for the test's teardown and return the error to raise."""
    text = MESSAGE.format(what=what, nodeid=STATE.nodeid)
    STATE.refused.append(text)
    return HardwareRefused(text)


def _shown(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"a connection to {address[0]}:{address[1]}"
    return f"a connection to {address!r}"


def _guard_connect(original: Callable[..., Any]) -> Callable[..., Any]:
    def connect(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not on_this_machine(address) and not may_connect(STATE.path):
            raise refuse(_shown(address))
        return original(sock, address, *args, **kwargs)

    connect.__wrapped__ = original  # type: ignore[attr-defined]
    return connect


def _numeric(host: Any) -> bool:
    try:
        ipaddress.ip_address(str(host).strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _guard_lookup(original: Callable[..., Any]) -> Callable[..., Any]:
    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if (name is not None and not _numeric(name) and str(name).lower() != "localhost"
                and not may_connect(STATE.path)):
            raise refuse(f"a name lookup of {name}")
        return original(host, *args, **kwargs)

    getaddrinfo.__wrapped__ = original  # type: ignore[attr-defined]
    return getaddrinfo


def _guard_proactor(original: Callable[..., Any]) -> Callable[..., Any]:
    def connect(proactor: Any, conn: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        if not on_this_machine(address) and not may_connect(STATE.path):
            raise refuse(_shown(address))
        return original(proactor, conn, address, *args, **kwargs)

    connect.__wrapped__ = original  # type: ignore[attr-defined]
    return connect


def _guard_stream(name: str, original: Any) -> Callable[..., Any]:
    def opened(*args: Any, **kwargs: Any) -> Any:
        if not may_open_streams(STATE.path):
            raise refuse(f"sounddevice.{name}")
        return original(*args, **kwargs)

    opened.__wrapped__ = original  # type: ignore[attr-defined]
    opened.__name__ = name
    opened._emet_hardware_guard = True  # type: ignore[attr-defined]
    return opened


def is_real_sounddevice(module: Any) -> bool:
    """The real module holds the PortAudio library it loaded; a fake does not."""
    return getattr(module, "__name__", None) == "sounddevice" and hasattr(module, "_lib")


def guarded(opener: Any) -> bool:
    """Whether a stream opener is this guard's wrapper. The mark is the
    wrapper's own, so a real opener that carries `__wrapped__` for another
    reason is still wrapped."""
    return getattr(opener, "_emet_hardware_guard", False) is True


def guard_sounddevice(module: Any) -> None:
    """Wrap each of the real module's stream openers that is not wrapped
    yet; leave anything else alone. It decides opener by opener and keeps
    no mark on the module: `importlib.reload` runs the module's code again
    in the same module object, which puts the bare openers back, and a mark
    left on the module once let them through (review finding
    checks-ci/code#3, 2026-11-27)."""
    if not is_real_sounddevice(module):
        return
    for name in STREAM_NAMES:
        opener = getattr(module, name, None)
        if opener is not None and not guarded(opener):
            setattr(module, name, _guard_stream(name, opener))


class _GuardedLoader(importlib.abc.Loader):
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def create_module(self, spec: Any) -> Any:
        return self.inner.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self.inner.exec_module(module)
        guard_sounddevice(module)


class _SounddeviceFinder(importlib.abc.MetaPathFinder):
    """Finds `sounddevice` as the other finders would, and wraps its loader
    so the module is guarded as soon as it has run."""

    def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
        if name != "sounddevice":
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _GuardedLoader(spec.loader)
                return spec
        return None


def install() -> None:
    """Put the wrappers in place for the rest of the process. Calling it
    again changes nothing."""
    if STATE.installed:
        return
    for name in ("connect", "connect_ex"):
        setattr(socket.socket, name, _guard_connect(getattr(socket.socket, name)))
    socket.getaddrinfo = _guard_lookup(socket.getaddrinfo)
    if sys.platform == "win32":
        from asyncio import windows_events

        windows_events.IocpProactor.connect = _guard_proactor(windows_events.IocpProactor.connect)
    sys.meta_path.insert(0, _SounddeviceFinder())
    if "sounddevice" in sys.modules:
        guard_sounddevice(sys.modules["sounddevice"])
    STATE.installed = True


@pytest.fixture(autouse=True)
def hardware_guard(request: pytest.FixtureRequest):
    """Name the running test for the wrappers, and fail it at teardown when
    it reached for a device or the network, even if its code swallowed the
    error."""
    install()
    STATE.nodeid, STATE.path, STATE.refused = request.node.nodeid, Path(str(request.node.path)), []
    yield
    refused, STATE.refused = STATE.refused, []
    STATE.nodeid, STATE.path = "(outside a test)", None
    if refused:
        pytest.fail(refused[0] + (f" ({len(refused) - 1} more)" if len(refused) > 1 else ""), pytrace=False)
