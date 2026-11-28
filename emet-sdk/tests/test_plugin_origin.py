"""Where each plugin comes from, and a name two distributions offer.

The boot lines say which distribution and version each provider came from,
so `PluginRegistry` keeps the distribution behind every entry point. A name
two distributions offer in one group is refused when it is loaded, naming
both: which one Python finds first depends on where each is installed and
on the order the file system lists a folder in, which nobody chose,
and a package that takes a provider's name would be handed that provider's
key. The distributions here are written into a temporary folder and read
back through `importlib.metadata`, the way an installed package is read.
"""

from __future__ import annotations

from importlib import metadata
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from emet_sdk.types import Health
from emet_sdk.discovery import (
    GROUP_ACTUATOR,
    GROUP_LLM,
    GROUP_SENSOR,
    GROUP_TTS,
    PluginOrigin,
    PluginRegistry,
)
from emet_sdk.validate import DuplicatePluginError, MissingPluginError, ValidationError

#: An entry point target that loads: any importable attribute will do.
LOADS = "emet_sdk.types:Health"


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


def registry_of(*roots: Path) -> PluginRegistry:
    return PluginRegistry.from_entry_points(installed(*roots))


# ------------------------------------------------------------------ origin


def test_the_origin_names_the_distribution_and_its_version(tmp_path):
    registry = registry_of(distribution(tmp_path, "emet-fake", "1.2.3", {GROUP_LLM: {"fake": LOADS}}))
    origin = registry.origin(GROUP_LLM, "fake")
    assert origin == PluginOrigin(GROUP_LLM, "fake", LOADS, "emet-fake", "1.2.3")
    assert origin.label == "emet-fake 1.2.3"
    assert registry.load_llm("fake") is Health


def test_a_name_nothing_installed_has_no_origin(tmp_path):
    registry = registry_of(distribution(tmp_path, "emet-fake", "1.2.3", {GROUP_LLM: {"fake": LOADS}}))
    assert registry.origin(GROUP_LLM, "other") is None
    assert registry.origin(GROUP_TTS, "fake") is None


def test_an_entry_point_no_distribution_declared_has_an_origin_with_no_label():
    registry = PluginRegistry(llm={"made": EntryPoint("made", LOADS, GROUP_LLM)})
    origin = registry.origin(GROUP_LLM, "made")
    assert origin == PluginOrigin(GROUP_LLM, "made", LOADS)
    assert origin.distribution is None and origin.version is None and origin.label is None


def test_a_group_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match=r"no entry-point group 'emet\.voice'\. The groups are emet\.actuators, "):
        PluginRegistry().origin("emet.voice", "x")


def test_the_installed_providers_name_their_own_distribution():
    origin = PluginRegistry.discover().origin(GROUP_LLM, "mock")
    assert origin is not None
    assert origin.distribution == "emet-providers"
    assert origin.version == metadata.version("emet-providers")
    assert origin.value == "emet_providers.mock:MockLanguageModel"


def test_a_distribution_with_no_metadata_file_counts_as_one_no_distribution_declared(tmp_path):
    """An interrupted install can leave a dist-info folder with its entry
    points and no METADATA. Reading `dist.name` there warns that it will
    raise, an error in this suite, and discovery runs at the start of every
    program, so the name is read in a way that does neither."""
    info = tmp_path / "emet_broken-1.0.dist-info"
    info.mkdir()
    (info / "entry_points.txt").write_text(f"[{GROUP_LLM}]\nbroken = {LOADS}\n", encoding="utf-8")
    registry = registry_of(tmp_path)
    assert registry.origin(GROUP_LLM, "broken") == PluginOrigin(GROUP_LLM, "broken", LOADS)
    assert registry.load_llm("broken") is Health


def test_each_distribution_is_read_once_however_many_names_it_offers(tmp_path, monkeypatch):
    """`dist.name` and `dist.version` each parse the whole METADATA file, a
    README included, and the boot lines ask `origin` of every provider."""
    root = distribution(tmp_path, "emet-fake", "1.2.3", {GROUP_LLM: {"fake": LOADS, "other": LOADS}, GROUP_TTS: {"fake": LOADS}})
    reads: list[str] = []
    real = metadata.PathDistribution.read_text

    def counted(self, filename):
        if filename == "METADATA":
            reads.append(filename)
        return real(self, filename)

    monkeypatch.setattr(metadata.PathDistribution, "read_text", counted)
    registry = registry_of(root)
    assert len(reads) == 1
    assert registry.origin(GROUP_LLM, "other").label == "emet-fake 1.2.3"
    assert registry.origin(GROUP_TTS, "fake").label == "emet-fake 1.2.3"
    assert len(reads) == 1


def test_entry_points_outside_the_nine_groups_are_left_out(tmp_path):
    registry = registry_of(distribution(tmp_path, "emet-fake", "1.0", {"console_scripts": {"fake": LOADS}}))
    assert not registry


# -------------------------------------------------------------- the clash


def two_claimants(tmp_path: Path, group: str = GROUP_LLM, name: str = "openai") -> PluginRegistry:
    first = distribution(tmp_path / "a", "emet-providers", "0.5.7", {group: {name: "emet_providers.openai:OpenAILanguageModel"}})
    second = distribution(tmp_path / "b", "emet-openai-fork", "0.1.0", {group: {name: "emet_openai_fork:Model"}})
    return registry_of(first, second)


EXPECTED = (
    "[error] duplicate_plugin: two installed distributions offer the language model provider 'openai' in the "
    "group emet.llm: emet-openai-fork 0.1.0 (emet_openai_fork:Model) and emet-providers 0.5.7 "
    "(emet_providers.openai:OpenAILanguageModel). Emet will not choose one for you, since which one Python "
    "finds first depends on where each is installed and on the order the file system lists a folder in, "
    "which nobody chose. Uninstall the one you did not mean to install, "
    "then start again: pip uninstall emet-openai-fork, or pip uninstall emet-providers."
)


def test_a_name_two_distributions_offer_is_refused_when_it_is_loaded(tmp_path):
    registry = two_claimants(tmp_path)
    with pytest.raises(DuplicatePluginError) as caught:
        registry.load_llm("openai")
    assert str(caught.value) == EXPECTED
    assert [f.code for f in caught.value.report.errors] == ["duplicate_plugin"]


def test_the_refusal_is_a_validation_error_the_programs_already_print():
    """`emet-listen` and `emet-talk` print a `ValidationError` and exit 1,
    so the refusal reaches the person with no change to either program."""
    assert issubclass(DuplicatePluginError, ValidationError)
    assert not issubclass(DuplicatePluginError, MissingPluginError)


def test_asking_where_a_clashing_name_comes_from_is_refused_the_same_way(tmp_path):
    with pytest.raises(DuplicatePluginError, match="emet-openai-fork 0.1.0"):
        two_claimants(tmp_path).origin(GROUP_LLM, "openai")


def test_a_clash_is_still_a_name_that_is_installed(tmp_path):
    """Validation passes a manifest naming it, and the boot refuses it."""
    registry = two_claimants(tmp_path)
    assert registry.has_llm("openai")
    assert registry.llm_names == ["openai"]


def test_a_clash_leaves_every_other_name_alone(tmp_path):
    first = distribution(tmp_path / "a", "emet-providers", "0.5.7", {GROUP_LLM: {"openai": LOADS}, GROUP_TTS: {"piper": LOADS}})
    second = distribution(tmp_path / "b", "emet-openai-fork", "0.1.0", {GROUP_LLM: {"openai": LOADS}})
    registry = registry_of(first, second)
    assert registry.load_tts("piper") is not None
    assert registry.origin(GROUP_TTS, "piper").label == "emet-providers 0.5.7"


def test_one_distribution_found_twice_is_one_claimant(tmp_path):
    root = distribution(tmp_path, "emet-providers", "0.5.7", {GROUP_LLM: {"openai": LOADS}})
    eps = installed(root) + installed(root)
    assert len(eps) == 2
    registry = PluginRegistry.from_entry_points(eps)
    assert registry.origin(GROUP_LLM, "openai").label == "emet-providers 0.5.7"


def test_a_distribution_name_is_compared_as_pip_compares_it(tmp_path):
    """`Emet_Providers` and `emet-providers` are one project to pip (PEP 503)."""
    first = distribution(tmp_path / "a", "emet-providers", "0.5.7", {GROUP_LLM: {"openai": LOADS}})
    second = distribution(tmp_path / "b", "Emet_Providers", "0.5.7", {GROUP_LLM: {"openai": LOADS}})
    assert registry_of(first, second).load_llm("openai") is not None


def test_one_name_in_two_groups_is_no_clash(tmp_path):
    first = distribution(tmp_path / "a", "emet-providers", "0.5.7", {GROUP_LLM: {"mock": LOADS}})
    second = distribution(tmp_path / "b", "emet-other", "1.0", {GROUP_TTS: {"mock": LOADS}})
    registry = registry_of(first, second)
    assert registry.load_llm("mock") is not None and registry.load_tts("mock") is not None


def test_a_driver_two_distributions_offer_is_refused(tmp_path):
    with pytest.raises(DuplicatePluginError, match="offer the driver plugin 'pca9685' in the group emet.actuators"):
        two_claimants(tmp_path, GROUP_ACTUATOR, "pca9685").load_driver("pca9685")


def test_a_sensor_clash_is_refused_as_a_driver(tmp_path):
    with pytest.raises(DuplicatePluginError, match="in the group emet.sensors"):
        two_claimants(tmp_path, GROUP_SENSOR, "imu").load_driver("imu")


def test_a_view_at_another_strictness_keeps_the_refusal(tmp_path):
    view = two_claimants(tmp_path).with_verification(True)
    assert view.verify_drivers
    with pytest.raises(DuplicatePluginError):
        view.load_llm("openai")


def test_an_entry_point_no_distribution_declared_is_named_by_its_target():
    eps = [EntryPoint("made", "one:A", GROUP_LLM), EntryPoint("made", "two:B", GROUP_LLM)]
    with pytest.raises(DuplicatePluginError) as caught:
        PluginRegistry.from_entry_points(eps).load_llm("made")
    message = str(caught.value)
    assert "an entry point no distribution declared (one:A) and an entry point no distribution declared (two:B)" in message
    assert "pip uninstall" not in message


def test_a_missing_name_is_still_a_missing_plugin(tmp_path):
    registry = two_claimants(tmp_path)
    with pytest.raises(MissingPluginError, match="no language model provider provides 'anthropic'. Installed: openai."):
        registry.load_llm("anthropic")
