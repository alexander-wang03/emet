"""What every SDK test may assume about its surroundings.

**No device and no network.** The SDK opens neither, and its tests hold
that by the hardware guard every suite loads (0.5.7-D10 (a)): a sound
device stream or a connection off this machine fails the test that made it.
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
