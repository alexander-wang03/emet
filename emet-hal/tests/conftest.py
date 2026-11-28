"""What every HAL test may assume about its surroundings.

**No device and no network.** The HAL is where the microphone and the
speaker live, so its tests are where a real stream is easiest to open by
mistake. The hardware guard every suite loads (0.5.7-D10 (a)) fails a test
that opens a sound device stream, save in `test_audio_virtual.py`, which
plays into the virtual card in the `sound` job alone, and a test that
connects off this machine. Device queries, such as listing what PortAudio
sees, pass.
"""

from __future__ import annotations


# --------------------------------------------------------- the hardware guard


def _hardware_guard():
    """Load `tools/hardware_guard.py` once by path, the first one found
    walking up from this folder, and put its wrappers in place (0.5.7-D10
    (a)). A tree without one fails the collection, naming the file."""
    import importlib.util
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for folder in (here, *here.parents):
        path = folder / "tools" / "hardware_guard.py"
        if path.is_file():
            module = sys.modules.get("emet_hardware_guard")
            if module is None:
                spec = importlib.util.spec_from_file_location("emet_hardware_guard", path)
                module = importlib.util.module_from_spec(spec)
                sys.modules["emet_hardware_guard"] = module
                spec.loader.exec_module(module)
            module.install()
            return module
    raise RuntimeError(f"no tools/hardware_guard.py in {here} or any folder above it: every suite loads the "
                       f"hardware guard (0.5.7-D10 (a)), and these tests do not run without it")


#: Autouse: no test opens a sound device stream or connects off this machine.
hardware_guard = _hardware_guard().hardware_guard
