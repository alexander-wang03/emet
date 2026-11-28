"""Which entry point a name loads, read the way every program reads it.

`emet-listen`, `emet-talk`, `emet-say` and `emet validate` build their
registry with `PluginRegistry.discover()`, so the refusal of a name two
distributions offer is held here through that call, with the distributions
written into a folder put first on `sys.path`. The rest of the file holds
the rules `from_entry_points` keeps for a name found more than once: the
first entry point is the one loaded, a driver offered as an actuator and
as a sensor loads the actuator, and a distribution's name is compared as
pip compares it.
"""

from __future__ import annotations

from importlib import metadata
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from emet_sdk.discovery import GROUP_ACTUATOR, GROUP_LLM, GROUP_SENSOR, PluginRegistry
from emet_sdk.types import Health
from emet_sdk.validate import DuplicatePluginError

#: Two entry point targets that load, told apart by what they return.
FIRST = "emet_sdk.types:Health"
SECOND = "emet_sdk.types:Reading"


def distribution(root: Path, name: str, version: str, entries: dict[str, dict[str, str]]) -> Path:
    """Write a distribution's metadata the way an installer leaves it."""
    info = root / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    lines: list[str] = []
    for group, names in entries.items():
        lines.append(f"[{group}]")
        lines += [f"{key} = {value}" for key, value in names.items()]
        lines.append("")
    (info / "entry_points.txt").write_text("\n".join(lines), encoding="utf-8")
    return root


def installed(*roots: Path) -> list[EntryPoint]:
    """Every entry point of the distributions under these folders, in order."""
    return [ep for dist in metadata.distributions(path=[str(r) for r in roots]) for ep in dist.entry_points]


def discovered() -> PluginRegistry:
    """What `PluginRegistry.discover()` builds, without its cache.

    The cached registry is shared by every test in the run, so it is left
    alone, and a registry built from this test's `sys.path` is not kept.
    """
    return PluginRegistry.__dict__["discover"].__func__.__wrapped__(PluginRegistry)


def test_discovery_refuses_a_name_two_installed_distributions_offer(tmp_path, monkeypatch):
    """The path every program boots on. A registry built as master built it,
    one dict a group, keeps the last of the two and loads it without a word,
    which would hand that provider's key to a package the owner did not
    choose."""
    distribution(tmp_path, "emet-clash-one", "1.0", {GROUP_LLM: {"emet_clash_probe": FIRST}})
    distribution(tmp_path, "emet-clash-two", "2.0", {GROUP_LLM: {"emet_clash_probe": SECOND}})
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = discovered()
    assert registry.has_llm("emet_clash_probe")
    with pytest.raises(DuplicatePluginError) as caught:
        registry.load_llm("emet_clash_probe")
    message = str(caught.value)
    assert "offer the language model provider 'emet_clash_probe' in the group emet.llm" in message
    assert "emet-clash-one 1.0 (emet_sdk.types:Health) and emet-clash-two 2.0 (emet_sdk.types:Reading)" in message


def test_the_first_entry_point_of_a_name_is_the_one_loaded(tmp_path):
    """One distribution found twice, as a stale dist-info left on the path
    does, is one claimant, and the one found first is the one loaded."""
    newer = distribution(tmp_path / "a", "emet-providers", "0.5.7", {GROUP_LLM: {"openai": FIRST}})
    stale = distribution(tmp_path / "b", "emet-providers", "0.5.6", {GROUP_LLM: {"openai": SECOND}})
    registry = PluginRegistry.from_entry_points(installed(newer, stale))
    assert registry.origin(GROUP_LLM, "openai").label == "emet-providers 0.5.7"
    assert registry.load_llm("openai") is Health


def test_a_driver_offered_as_an_actuator_and_as_a_sensor_loads_the_actuator(tmp_path):
    """One name in two groups is no clash, and `load_driver` looks in
    `emet.actuators` first."""
    root = distribution(tmp_path, "emet-servo", "1.0", {GROUP_ACTUATOR: {"servo": FIRST}, GROUP_SENSOR: {"servo": SECOND}})
    registry = PluginRegistry.from_entry_points(installed(root))
    assert registry.load_driver("servo") is Health
    assert registry.origin(GROUP_SENSOR, "servo").value == SECOND


@pytest.mark.parametrize("spelled", ["Emet_Providers", "emet.providers", "EMET--providers"])
def test_every_spelling_pip_takes_for_one_project_is_one_claimant(tmp_path, spelled):
    """PEP 503 folds runs of `-`, `_` and `.` to one `-` and ignores case."""
    first = distribution(tmp_path / "a", "emet-providers", "0.5.7", {GROUP_LLM: {"openai": FIRST}})
    second = distribution(tmp_path / "b", spelled, "0.5.7", {GROUP_LLM: {"openai": SECOND}})
    assert PluginRegistry.from_entry_points(installed(first, second)).load_llm("openai") is Health
