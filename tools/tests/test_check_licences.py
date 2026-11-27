"""The licence gate, `tools/check_licences.py`, against fake distributions.

A fake distribution is an object with the two things the gate reads:
`metadata` (with `get` and `get_all`) and `version`. Nothing here reads
the venv the tests run in: `main` is given fake distributions, and the
other venv `--python` names is a fake `subprocess.run`. The last tests read
the real `tools/licences.json`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import check_licences as cl  # noqa: E402

E55 = {"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0", "MIT-0"}


class Meta:
    def __init__(self, fields: dict[str, list[str]]):
        self.fields = fields

    def get(self, key, default=None):
        values = self.fields.get(key)
        return values[0] if values else default

    def get_all(self, key, default=None):
        return list(self.fields[key]) if key in self.fields else default


class Dist:
    def __init__(self, name, version="1.0", *, expression=None, licence=None, classifiers=(), requires=(),
                 extras=()):
        fields: dict[str, list[str]] = {"Name": [name]}
        if expression is not None:
            fields["License-Expression"] = [expression]
        if licence is not None:
            fields["License"] = [licence]
        if classifiers:
            fields["Classifier"] = list(classifiers)
        if requires:
            fields["Requires-Dist"] = list(requires)
        if extras:
            fields["Provides-Extra"] = list(extras)
        self.metadata = Meta(fields)
        self.version = version


ENV = {
    "implementation_name": "cpython", "implementation_version": "3.13.2", "os_name": "posix",
    "platform_machine": "x86_64", "platform_release": "", "platform_system": "Linux", "platform_version": "",
    "python_full_version": "3.13.2", "platform_python_implementation": "CPython", "python_version": "3.13",
    "sys_platform": "linux",
}

BSD = "License :: OSI Approved :: BSD License"


def write_policy(tmp_path: Path, exceptions=(), tools_exceptions=(), read=()) -> cl.Policy:
    data = {
        "allowed": sorted(E55),
        "tools_allowed": ["MPL-2.0"],
        "first_party": ["emet-sdk", "emet-hal", "emet-providers", "emet-engine"],
        "read": list(read),
        "exceptions": list(exceptions),
        "tools_exceptions": list(tools_exceptions),
    }
    path = tmp_path / "licences.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return cl.load_policy(path)


CERTIFI = {
    "package": "certifi", "licence": "MPL-2.0", "via": ["emet-providers[deepgram]"],
    "reason": "unmodified, never bundled", "recorded": "CITATIONS.md, certifi",
}
PIPER = {
    "package": "piper-tts", "licence": "GPL-3.0-or-later", "via": ["emet-providers[piper]"],
    "reason": "the piper extra, never bundled", "recorded": "CITATIONS.md, Piper",
}
COLORAMA_READ = {
    "package": "colorama", "declares": "BSD", "licence": "BSD-3-Clause",
    "where": "colorama 0.4.6's LICENSE.txt, read 2026-11-26",
}


def tree(*more: Dist) -> list[Dist]:
    """The first-party packages as their metadata names them, with an extra
    that brings httpx, and httpx bringing certifi."""
    return [
        Dist("emet-sdk", "0.5.6", expression="Apache-2.0", requires=["jsonschema>=4"]),
        Dist("emet-providers", "0.5.6", expression="Apache-2.0", extras=["deepgram", "dev"],
             requires=["emet-sdk>=0.5.6", 'httpx>=0.27; extra == "deepgram"', 'pytest>=9; extra == "dev"']),
        Dist("jsonschema", "4.26.0", expression="MIT"),
        Dist("httpx", "0.28.1", licence="BSD-3-Clause", requires=["certifi"]),
        Dist("pytest", "9.2.0", expression="MIT", requires=['colorama>=0.4; sys_platform == "win32"']),
        *more,
    ]


def with_sdk_requiring(dists: list[Dist], *names: str) -> list[Dist]:
    """The tree with emet-sdk's base install requiring `names` as well."""
    dists[0] = Dist("emet-sdk", "0.5.6", expression="Apache-2.0", requires=["jsonschema>=4", *names])
    return dists


def verdicts(dists, policy, env=ENV):
    return {v.record.key: v for v in cl.judge_installed(cl.installed(dists), policy, env)}


# ------------------------------------------------------------ reading


def test_each_way_a_licence_is_declared():
    assert cl.read_licence("MIT", None, []).text == "MIT"
    assert cl.read_licence(None, "MIT License", []).text == "MIT"
    assert cl.read_licence(None, "BSD-3-Clause", []).text == "BSD-3-Clause"
    assert cl.read_licence(None, None, ["License :: OSI Approved :: Python Software Foundation License"]).text \
        == "PSF-2.0"
    family = cl.read_licence(None, "UNKNOWN", [BSD])
    assert (family.source, family.text) == ("classifiers", "BSD (classifier, no variant)")
    assert cl.read_licence(None, None, []).text == "undeclared"


def test_a_family_in_the_license_field_is_labelled_as_read_from_that_field():
    reading = cl.read_licence(None, "BSD", [])
    assert (reading.source, reading.family, reading.text) == ("License", "BSD", "BSD (License field, no variant)")


def test_the_expression_comes_first_as_pep_639_orders_them():
    reading = cl.read_licence("GPL-3.0-only", "MIT", ["License :: OSI Approved :: MIT License"])
    assert (reading.source, reading.text) == ("License-Expression", "GPL-3.0-only")


def test_an_unparseable_expression_is_unreadable_and_does_not_fall_through_to_the_field():
    reading = cl.read_licence("MIT/GPL", "MIT", ["License :: OSI Approved :: MIT License"])
    assert (reading.source, reading.node, reading.text) == ("License-Expression", None, "unreadable: MIT/GPL")


def test_several_classifiers_are_read_as_all_of_them(tmp_path):
    gpl = "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"
    reading = cl.read_licence(None, None, ["License :: OSI Approved :: MIT License", gpl])
    assert reading.text == "GPL-3.0-only AND MIT (classifiers)"
    assert not cl.passes(reading.node, E55)
    dists = with_sdk_requiring(tree(Dist("both", classifiers=["License :: OSI Approved :: MIT License", gpl])),
                               "both")
    v = verdicts(dists, write_policy(tmp_path))["both"]
    assert not v.allowed and v.why.startswith("GPL-3.0-only is outside the allowed list")


def test_the_bare_osi_classifier_beside_a_licence_is_dropped(tmp_path):
    parent = "License :: OSI Approved"
    assert cl.read_licence(None, None, [parent, "License :: OSI Approved :: MIT License"]).text == "MIT"
    alone = cl.read_licence(None, None, [parent])
    assert alone.text == "OSI Approved (classifier, no licence named)"
    dists = with_sdk_requiring(tree(Dist("mitpkg", classifiers=[parent, "License :: OSI Approved :: MIT License"]),
                                    Dist("bare", classifiers=[parent])), "mitpkg", "bare")
    found = verdicts(dists, write_policy(tmp_path))
    assert found["mitpkg"].allowed and found["mitpkg"].why == "allowed"
    assert not found["bare"].allowed


def test_spdx_spellings_are_made_canonical():
    assert cl.parse_expression("gpl-3.0").text() == "GPL-3.0-only"
    assert cl.parse_expression("GPL-2.0+").text() == "GPL-2.0-or-later"
    assert cl.parse_expression("Apache-2.0 WITH LLVM-exception").text() == "Apache-2.0"
    assert cl.parse_expression("MIT OR (Apache-2.0 AND BSD-3-Clause)").text() == "MIT OR (Apache-2.0 AND BSD-3-Clause)"
    with pytest.raises(cl.ExpressionError):
        cl.parse_expression("MIT AND")


def test_a_key_compares_and_and_or_in_any_order():
    def key(text: str) -> str:
        return cl.parse_expression(text).key()

    assert key("MPL-2.0 AND MIT") == key("MIT AND MPL-2.0")
    assert key("MIT AND (Zlib AND 0BSD)") == key("0BSD AND MIT AND Zlib")
    assert key("(MIT OR Apache-2.0) AND Zlib") == key("Zlib AND (Apache-2.0 OR MIT)")
    assert key("MIT AND Zlib") != key("MIT OR Zlib")
    assert key("MIT AND (Zlib OR 0BSD)") != key("(MIT AND Zlib) OR 0BSD")


def test_and_needs_every_side_and_or_needs_one():
    allowed = set(E55)
    assert cl.passes(cl.parse_expression("Apache-2.0 OR GPL-3.0-only"), allowed)
    assert not cl.passes(cl.parse_expression("MIT AND GPL-3.0-only"), allowed)
    assert cl.outside(cl.parse_expression("MIT AND Zlib AND 0BSD"), allowed) == ["Zlib", "0BSD"]


def test_an_or_refusal_names_its_least_refused_side():
    assert cl.outside(cl.parse_expression("GPL-3.0-only OR (Zlib AND 0BSD)"), set(E55)) == ["GPL-3.0-only"]


# ------------------------------------------------------------ markers


def on(full: str) -> dict[str, str]:
    return {**ENV, "python_full_version": full, "python_version": ".".join(full.split(".")[:2])}


def test_a_wildcard_version_is_pep_440s_prefix_match():
    """`== X.Y.*` holds when the release starts X.Y, and `!=` is its reverse
    (B2-1: 3.13.2 was read against 3.13.0)."""
    assert cl.evaluate_marker("python_full_version == '3.13.*'", on("3.13.2")) is True
    assert cl.evaluate_marker("python_full_version == '3.13.*'", on("3.12.9")) is False
    assert cl.evaluate_marker("python_full_version != '3.13.*'", on("3.13.2")) is False
    assert cl.evaluate_marker("python_full_version != '3.13.*'", on("3.12.9")) is True
    # Segments compare as numbers, the release is zero padded, and what
    # follows the release segment is ignored.
    assert cl.evaluate_marker("python_full_version == '3.1.*'", on("3.13.2")) is False
    assert cl.evaluate_marker("python_version == '3.13.0.*'", on("3.13.2")) is True
    assert cl.evaluate_marker("python_full_version == '3.14.*'", on("3.14.0rc1")) is True
    # A plain version is still an exact match.
    assert cl.evaluate_marker("python_full_version == '3.13'", on("3.13.2")) is False
    # A prefix with more than a release segment is left unread.
    assert cl.evaluate_marker("python_full_version == '3.13a1.*'", on("3.13.2")) is None


def test_a_compatible_release_is_pep_440s():
    """`~= 3.13` is `>= 3.13, == 3.*`; `~= 3.13.0` is `>= 3.13.0, == 3.13.*`."""
    assert cl.evaluate_marker("python_full_version ~= '3.13'", on("3.14.0")) is True
    assert cl.evaluate_marker("python_full_version ~= '3.13'", on("3.12.9")) is False
    assert cl.evaluate_marker("python_full_version ~= '3.13'", on("4.0.0")) is False
    assert cl.evaluate_marker("python_full_version ~= '3.13.0'", on("3.14.0")) is False
    assert cl.evaluate_marker("python_full_version ~= '3.13.0'", on("3.13.2")) is True
    assert cl.evaluate_marker("python_full_version ~= '3'", on("3.13.2")) is None


# ------------------------------------------------------------ verdicts


def test_an_allowed_licence_passes_and_first_party_is_skipped(tmp_path):
    found = verdicts(tree(), write_policy(tmp_path))
    assert "emet-sdk" not in found and "emet-providers" not in found
    assert found["jsonschema"].allowed and found["jsonschema"].why == "allowed"
    assert found["jsonschema"].brought == ["emet-sdk"]


def test_a_refused_licence_names_the_distribution_its_licence_and_what_brings_it(tmp_path):
    dists = with_sdk_requiring(tree(Dist("gplthing", "2.0", expression="GPL-3.0-or-later")), "gplthing")
    v = verdicts(dists, write_policy(tmp_path))["gplthing"]
    assert not v.allowed
    assert v.brought == ["emet-sdk"]
    lines: list[str] = []
    assert cl.report([v], show_table=False, out=lines.append) == 1
    assert lines == [
        "  x gplthing 2.0, GPL-3.0-or-later: it is outside the allowed list, and no exception in "
        "tools/licences.json names gplthing. Brought by: emet-sdk > gplthing",
        f"  fix: {cl.FIX_OUTSIDE}",
        "licences: 1 refused of 1",
    ]


def test_requires_dist_is_walked_back_to_a_first_party_extra(tmp_path):
    v = verdicts(tree(Dist("certifi", "2026.10.18", licence="MPL-2.0")), write_policy(tmp_path))["certifi"]
    assert not v.allowed
    assert v.brought == ["emet-providers[deepgram]"]
    assert v.chains == ["emet-providers[deepgram] > httpx > certifi"]
    assert cl.brought_cell(v) == "emet-providers[deepgram] (through httpx)"


def test_a_requirement_true_with_and_without_an_extra_belongs_to_the_base(tmp_path):
    dists = tree(Dist("thing", expression="MIT"))
    dists[0] = Dist("emet-sdk", "0.5.6", expression="Apache-2.0", extras=["dev"],
                    requires=["jsonschema>=4", 'thing; extra == "dev" or python_version >= "3.0"'])
    assert verdicts(dists, write_policy(tmp_path))["thing"].brought == ["emet-sdk"]


def test_only_a_first_party_extra_seeds_the_walk(tmp_path):
    """pytest's own `dev` extra is asked for by nobody, so what it requires
    is not brought, and the chain does not name pytest."""
    dists = tree(Dist("gplthing", expression="GPL-3.0-only"))
    dists[4] = Dist("pytest", "9.2.0", expression="MIT", extras=["dev"], requires=['gplthing; extra == "dev"'])
    v = verdicts(dists, write_policy(tmp_path))["gplthing"]
    assert v.brought == [] and v.chains == ["nothing installed requires it"]


def piper_tree(*engine_requires: str, wrapper_requires: tuple[str, ...] = ()) -> list[Dist]:
    """emet-providers with a piper extra bringing piper-tts, and emet-engine
    requiring `engine_requires`, with a third-party `wrapper` beside them."""
    return [
        Dist("emet-sdk", "0.5.6", expression="Apache-2.0"),
        Dist("emet-providers", "0.5.6", expression="Apache-2.0", extras=["piper"],
             requires=["emet-sdk>=0.5.6", 'piper-tts>=1.8; extra == "piper"']),
        Dist("emet-engine", "0.5.6", expression="Apache-2.0", requires=["emet-sdk>=0.5.6", *engine_requires]),
        Dist("wrapper", "1.0", expression="MIT", requires=list(wrapper_requires)),
        Dist("piper-tts", "1.8.0", licence="GPL-3.0-or-later"),
    ]


def test_an_exception_for_an_extra_holds_when_only_the_extra_brings_it(tmp_path):
    v = verdicts(piper_tree(), write_policy(tmp_path, [PIPER]))["piper-tts"]
    assert v.allowed and v.brought == ["emet-providers[piper]"]


def test_the_walk_goes_on_past_a_first_party_extra_that_a_base_install_asks_for(tmp_path):
    """emet-engine's base install brings a wrapper that asks for
    emet-providers[piper], so every engine install brings piper-tts."""
    policy = write_policy(tmp_path, [PIPER])
    v = verdicts(piper_tree("wrapper", wrapper_requires=("emet-providers[piper]>=0.5.6",)), policy)["piper-tts"]
    assert not v.allowed
    assert v.brought == ["emet-engine", "emet-providers[piper]"]
    assert v.chains[0] == "emet-engine > wrapper > emet-providers[piper] > piper-tts"
    assert v.why.startswith("brought by emet-engine, which the exception does not name")
    v = verdicts(piper_tree("emet-providers[piper]>=0.5.6"), policy)["piper-tts"]
    assert not v.allowed and v.brought == ["emet-engine", "emet-providers[piper]"]


def test_a_marker_false_here_brings_nothing(tmp_path):
    """pytest brings colorama on Windows only; on this fake Linux nothing does."""
    colorama = Dist("colorama", "0.4.6", classifiers=[BSD])
    v = verdicts(tree(colorama), write_policy(tmp_path, read=[COLORAMA_READ]))["colorama"]
    assert v.chains == ["nothing installed requires it"]
    windows = {**ENV, "sys_platform": "win32"}
    v = verdicts(tree(colorama), write_policy(tmp_path, read=[COLORAMA_READ]), windows)["colorama"]
    assert v.brought == ["emet-providers[dev]"] and v.allowed


def test_a_family_passes_only_through_the_licence_a_person_read(tmp_path):
    """A bare `BSD License` classifier could be BSD-4-Clause, and a bare
    `Apache Software License` Apache-1.1: neither is on E55's list."""
    dists = with_sdk_requiring(tree(Dist("colorama", "0.4.6", classifiers=[BSD]),
                                    Dist("apachething", licence="Apache Software License")),
                               "colorama", "apachething")
    found = verdicts(dists, write_policy(tmp_path))
    for key, text in (("colorama", "BSD (classifier, no variant)"), ("apachething", "Apache (License field, no version)")):
        assert not found[key].allowed, key
        assert found[key].record.reading.text == text
        assert found[key].why == ("it says no more than a family, and no `read` entry in tools/licences.json "
                                  "gives the licence a person read")
        assert found[key].fix == cl.FIX_READ
    v = verdicts(dists, write_policy(tmp_path, read=[COLORAMA_READ]))["colorama"]
    assert v.allowed and v.why == "allowed: BSD-3-Clause as read in colorama 0.4.6's LICENSE.txt, read 2026-11-26"
    assert v.record.reading.text == "BSD-3-Clause (read by hand; its metadata: BSD (classifier, no variant))"
    lines: list[str] = []
    assert cl.report([v], show_table=False, out=lines.append) == 0
    assert lines[0] == ("  read by hand: colorama 0.4.6, BSD-3-Clause, as read in colorama 0.4.6's LICENSE.txt, "
                        "read 2026-11-26")
    assert lines[-1] == "licences: ok, 1 distribution(s), 0 by exception, 1 read by hand"


def test_a_licence_read_by_hand_is_judged_like_a_declared_one(tmp_path):
    read = {**COLORAMA_READ, "licence": "BSD-4-Clause"}
    dists = with_sdk_requiring(tree(Dist("colorama", "0.4.6", classifiers=[BSD])), "colorama")
    v = verdicts(dists, write_policy(tmp_path, read=[read]))["colorama"]
    assert not v.allowed and v.why.startswith("it is outside the allowed list")


def test_a_read_entry_holds_only_while_the_metadata_says_the_same(tmp_path):
    dists = with_sdk_requiring(tree(Dist("colorama", "0.5.0", expression="GPL-3.0-only")), "colorama")
    v = verdicts(dists, write_policy(tmp_path, read=[COLORAMA_READ]))["colorama"]
    assert not v.allowed
    assert v.why == ("its `read` entry in tools/licences.json was written when its metadata said BSD (no variant), "
                     "and this version declares GPL-3.0-only. The entry is stale")
    assert v.fix == cl.FIX_STALE


def test_an_exception_that_matches_allows_it(tmp_path):
    v = verdicts(tree(Dist("certifi", "2026.10.18", licence="MPL-2.0")), write_policy(tmp_path, [CERTIFI]))["certifi"]
    assert v.allowed and v.why == "exception: unmodified, never bundled"


def test_an_exception_matches_its_licence_with_the_parts_in_another_order(tmp_path):
    exc = {**CERTIFI, "licence": "MIT AND MPL-2.0"}
    v = verdicts(tree(Dist("certifi", "1", expression="MPL-2.0 AND MIT")), write_policy(tmp_path, [exc]))["certifi"]
    assert v.allowed, v.why


def test_an_exception_whose_licence_no_longer_matches_is_stale(tmp_path):
    v = verdicts(tree(Dist("certifi", "2027.1.1", expression="MPL-2.0 AND GPL-2.0-only")),
                 write_policy(tmp_path, [CERTIFI]))["certifi"]
    assert not v.allowed
    assert v.why == ("the exception was written for MPL-2.0, and this version declares MPL-2.0 AND GPL-2.0-only. "
                     "The exception is stale")


def test_an_exception_whose_package_now_declares_an_allowed_licence_is_stale(tmp_path):
    v = verdicts(tree(Dist("certifi", "2027.1.1", expression="MIT")), write_policy(tmp_path, [CERTIFI]))["certifi"]
    assert not v.allowed
    assert v.why == "the exception was written for MPL-2.0, and this version declares MIT. The exception is stale"
    assert v.fix == cl.FIX_STALE


@pytest.mark.parametrize("dist", [
    Dist("certifi", "2027.1.1"),
    Dist("certifi", "2027.1.1", licence="UNKNOWN"),
    Dist("certifi", "2027.1.1", expression="MPL-2.0 AND GPL-3.0-only WITH"),
    Dist("certifi", "2027.1.1", licence="MPL 2.0 or GPL 3"),
    Dist("certifi", "2027.1.1", licence="GNU GPL v3"),
], ids=["nothing", "unknown", "unparseable", "free-text", "gpl-free-text"])
def test_an_exception_written_for_a_declared_licence_refuses_one_that_cannot_be_read(tmp_path, dist):
    v = verdicts(tree(dist), write_policy(tmp_path, [CERTIFI]))["certifi"]
    assert not v.allowed
    assert v.why.startswith("the exception was written for MPL-2.0 as declared, and this version declares ")
    assert v.why.endswith(": no licence this script can read. The exception is stale")
    raw = dist.metadata.get("License-Expression") or dist.metadata.get("License")
    said = "nothing" if raw in (None, "UNKNOWN") else f"unreadable text ({raw})"
    assert f"this version declares {said}:" in v.why


def test_an_exception_holds_only_for_the_paths_it_names(tmp_path):
    dists = with_sdk_requiring(tree(Dist("certifi", "2026.10.18", licence="MPL-2.0")), "certifi")
    v = verdicts(dists, write_policy(tmp_path, [CERTIFI]))["certifi"]
    assert not v.allowed
    assert v.why.startswith("brought by emet-sdk, which the exception does not name")


def test_an_exception_does_not_hold_when_nothing_first_party_brings_it(tmp_path):
    """The piper extra dropped from emet-providers, with piper-tts still installed."""
    dists = [Dist("emet-sdk", "0.5.6", expression="Apache-2.0"), Dist("piper-tts", "1.8.0", licence="GPL-3.0-or-later")]
    v = verdicts(dists, write_policy(tmp_path, [PIPER]))["piper-tts"]
    assert not v.allowed and v.brought == []
    assert v.why == "nothing first-party brings it, so no path the exception names applies (it names emet-providers[piper])"


def test_an_undeclared_licence_is_refused_unless_a_read_entry_gives_what_a_person_read(tmp_path):
    dists = with_sdk_requiring(tree(Dist("mystery", "0.1")), "mystery")
    v = verdicts(dists, write_policy(tmp_path))["mystery"]
    assert not v.allowed and v.why.startswith("no licence can be read from its metadata")
    read = {"package": "mystery", "declares": "nothing", "licence": "MIT",
            "where": "the sdist's LICENSE, read 2026-11-26"}
    v = verdicts(dists, write_policy(tmp_path, read=[read]))["mystery"]
    assert v.allowed and v.why == "allowed: MIT as read in the sdist's LICENSE, read 2026-11-26"
    # An entry for nothing does not cover text the metadata gained since.
    dists[-1] = Dist("mystery", "0.2", licence="All rights reserved")
    assert not verdicts(dists, write_policy(tmp_path, read=[read]))["mystery"].allowed


def test_a_hand_read_licence_outside_the_list_still_needs_an_exception(tmp_path):
    dists = with_sdk_requiring(tree(Dist("mystery", "0.1")), "mystery")
    read = {"package": "mystery", "declares": "nothing", "licence": "MPL-2.0", "where": "its LICENSE, read 2026-11-26"}
    exc = {"package": "mystery", "licence": "MPL-2.0", "via": ["emet-sdk"], "reason": "unmodified",
           "recorded": "CITATIONS.md"}
    assert not verdicts(dists, write_policy(tmp_path, read=[read]))["mystery"].allowed
    v = verdicts(dists, write_policy(tmp_path, [exc], read=[read]))["mystery"]
    assert v.allowed and v.why == "exception: unmodified; MPL-2.0 as read in its LICENSE, read 2026-11-26"


# ------------------------------------------------------------ main


def test_main_judges_what_is_installed_and_names_a_refusal(tmp_path, monkeypatch, capsys):
    """The line CI's test jobs run: no arguments."""
    dists = with_sdk_requiring(tree(Dist("gplthing", "2.0", expression="GPL-3.0-or-later")), "gplthing")
    monkeypatch.setattr(cl.importlib.metadata, "distributions", lambda: iter(dists))
    write_policy(tmp_path)
    assert cl.main(["--policy", str(tmp_path / "licences.json")]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("licences: 4 distribution(s) installed for ")
    assert out[0].endswith("first party, skipped: emet-providers, emet-sdk")
    assert "  x gplthing 2.0, GPL-3.0-or-later: it is outside the allowed list" in "\n".join(out)
    assert out[-1] == "licences: 1 refused of 4"


def test_main_on_the_real_policy_allows_certifi_by_its_exception(monkeypatch, capsys):
    monkeypatch.setattr(cl.importlib.metadata, "distributions",
                        lambda: iter(tree(Dist("certifi", "2026.11.20", licence="MPL-2.0"))))
    assert cl.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "  note: the exception for piper-tts is not used here: piper-tts is not installed" in out
    assert out[-1] == "licences: ok, 4 distribution(s), 1 by exception, 0 read by hand"


def test_main_with_a_table_prints_each_row(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cl.importlib.metadata, "distributions", lambda: iter(tree()))
    write_policy(tmp_path)
    assert cl.main(["--table", "--policy", str(tmp_path / "licences.json")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "| Package | Version | Licence | Brought by | Verdict |"
    assert "| jsonschema | 4.26.0 | MIT | emet-sdk | allowed |" in out


# ------------------------------------------------------- a tools venv


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pins.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_requirements_list_with_no_venv_is_not_run(tmp_path, capsys):
    reqs = tmp_path / "requirements.txt"
    reqs.write_text("# a comment\ntqdm==4.70.1   # MPL-2.0 AND MIT\nnumpy==2.5.4\n", encoding="utf-8")
    assert cl.main(["--requirements", str(reqs)]) == 2
    out = capsys.readouterr().out
    assert "| tqdm | 4.70.1 | not read |  | not run |" in out
    assert out.rstrip().splitlines()[-1].startswith("licences: not run: 2 pin(s)")


def test_a_via_comment_under_a_pin_is_what_brings_it(tmp_path):
    pins = cl.read_requirements(_write(tmp_path, (
        "numpy==2.5.4\n    # via faster-whisper\n"
        "tqdm==4.70.1 \\\n    --hash=sha256:00\n    # via\n    #   faster-whisper\n    #   huggingface-hub\n")))
    assert [(p.name, p.version, p.via) for p in pins] == [
        ("numpy", "2.5.4", ("faster-whisper",)), ("tqdm", "4.70.1", ("faster-whisper", "huggingface-hub"))]


def test_a_tools_venv_allows_mpl_by_d9_and_still_refuses_what_is_outside(tmp_path):
    pins = cl.read_requirements(_write(tmp_path, "tqdm==4.70.1\nnumpy==2.5.4\n"))
    dump = {"environment": ENV, "distributions": [
        {"name": "tqdm", "version": "4.70.1", "License-Expression": "MPL-2.0 AND MIT"},
        {"name": "numpy", "version": "2.5.4", "License-Expression": "BSD-3-Clause AND Zlib"},
    ]}
    found, _notes = cl.judge_requirements(pins, dump, write_policy(tmp_path))
    by_name = {v.record.name: v for v in found}
    assert by_name["tqdm"].allowed
    assert not by_name["numpy"].allowed and by_name["numpy"].why.startswith("Zlib is outside the allowed list")
    # The same tqdm in the base install is refused: MPL-2.0 needs a named exception there.
    base = verdicts(tree(Dist("tqdm", "4.70.1", expression="MPL-2.0 AND MIT")), write_policy(tmp_path))
    assert not base["tqdm"].allowed


def test_a_pin_missing_from_the_venv_or_at_another_version_is_refused(tmp_path):
    pins = cl.read_requirements(_write(tmp_path, "tqdm==4.70.1\nnumpy==2.5.4\n    # via faster-whisper\n"))
    dump = {"environment": ENV, "distributions": [
        {"name": "tqdm", "version": "4.70.0", "License-Expression": "MPL-2.0 AND MIT"}]}
    found, _notes = cl.judge_requirements(pins, dump, write_policy(tmp_path))
    by_name = {v.record.name: v for v in found}
    tqdm, numpy = by_name["tqdm"], by_name["numpy"]
    assert not tqdm.allowed and tqdm.why == "pinned at 4.70.1 on line 1, and that venv holds 4.70.0"
    assert tqdm.fix == "install the list into that venv again" and tqdm.brought == ["the list"]
    assert not numpy.allowed and numpy.why == "pinned at line 2 and not installed in that venv"
    assert (numpy.record.version, numpy.record.reading.text, numpy.brought) == ("2.5.4", "not read", ["faster-whisper"])
    lines: list[str] = []
    assert cl.report(found, show_table=False, out=lines.append) == 1
    assert "  x numpy 2.5.4, not read: pinned at line 2 and not installed in that venv. Brought by: faster-whisper" \
        in lines
    assert "  fix: install the list into that venv first" in lines


def test_a_distribution_in_the_venv_that_the_list_does_not_pin_is_judged_too(tmp_path):
    pins = cl.read_requirements(_write(tmp_path, "tqdm==4.70.1\n"))
    dump = {"environment": ENV, "distributions": [
        {"name": "tqdm", "version": "4.70.1", "License-Expression": "MPL-2.0 AND MIT", "Requires-Dist": ["gplthing"]},
        {"name": "gplthing", "version": "1.0", "License-Expression": "GPL-3.0-only"},
        {"name": "pip", "version": "25.3", "License-Expression": "MIT"},
    ]}
    found, notes = cl.judge_requirements(pins, dump, write_policy(tmp_path))
    by_name = {v.record.name: v for v in found}
    assert not by_name["gplthing"].allowed and by_name["gplthing"].brought == ["tqdm"]
    assert by_name["pip"].allowed
    assert notes == ["installed in that venv and not in the list, judged all the same: gplthing"]
    assert cl.report(found, show_table=False, out=lambda s: None) == 1


def test_a_tools_exception_holds_for_a_nested_pin_and_for_a_pin_of_the_list(tmp_path):
    pins = cl.read_requirements(_write(tmp_path, "faster-whisper==1.2.1\nnumpy==2.5.4\nzthing==1.0\n"))
    dump = {"environment": ENV, "distributions": [
        {"name": "faster-whisper", "version": "1.2.1", "License-Expression": "MIT", "Requires-Dist": ["numpy"]},
        {"name": "numpy", "version": "2.5.4", "License-Expression": "BSD-3-Clause AND Zlib"},
        {"name": "zthing", "version": "1.0", "License-Expression": "Zlib"},
    ]}
    tools = [{"package": "numpy", "licence": "BSD-3-Clause AND Zlib", "via": ["faster-whisper"], "reason": "r",
              "recorded": "here"},
             {"package": "zthing", "licence": "Zlib", "via": ["zthing"], "reason": "r", "recorded": "here"}]
    policy = write_policy(tmp_path, tools_exceptions=tools)
    found, _notes = cl.judge_requirements(pins, dump, policy)
    by_name = {v.record.name: v for v in found}
    assert by_name["numpy"].allowed and by_name["numpy"].brought == ["faster-whisper"]
    assert by_name["zthing"].allowed and by_name["zthing"].brought == ["the list"]
    # A tools exception never reaches the base install.
    base = verdicts(with_sdk_requiring(tree(Dist("zthing", expression="Zlib")), "zthing"), policy)
    assert not base["zthing"].allowed


def test_python_reads_the_other_venvs_metadata_and_judges_it(tmp_path, monkeypatch, capsys):
    pins = _write(tmp_path, "tqdm==4.70.1\n")
    dump = {"python": "other-python", "environment": ENV,
            "distributions": [{"name": "tqdm", "version": "4.70.1", "License-Expression": "MPL-2.0 AND MIT"}]}
    ran: list[list[str]] = []

    def fake_run(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(dump), stderr="")

    monkeypatch.setattr(cl.subprocess, "run", fake_run)
    write_policy(tmp_path)
    assert cl.main(["--requirements", str(pins), "--python", "other-python", "--table",
                    "--policy", str(tmp_path / "licences.json")]) == 0
    assert ran == [["other-python", "-I", str(TOOLS / "check_licences.py"), "--metadata-json"]]
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("licences: 1 pin(s) in ") and "read from other-python" in out[0]
    assert "| tqdm | 4.70.1 | MPL-2.0 AND MIT | the list | allowed |" in out
    assert out[-1] == "licences: ok, 1 distribution(s), 0 by exception, 0 read by hand"


def test_python_that_cannot_read_its_metadata_is_not_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cl.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such module"))
    assert cl.main(["--requirements", str(_write(tmp_path, "tqdm==4.70.1\n")), "--python", "broken"]) == 2
    assert "broken could not read its metadata: no such module" in capsys.readouterr().err


def test_a_lock_split_on_a_wildcard_marker_judges_the_pin_for_this_python(tmp_path):
    """`tools/ci/requirements.txt` pins PyYAML off 3.13 and pyyaml-ft on it,
    in the form uv writes (B2-1: PyYAML was refused on 3.13.2)."""
    pins = cl.read_requirements(_write(tmp_path, (
        "pyyaml==6.0.4 ; python_full_version != '3.13.*' \\\n"
        "    --hash=sha256:070c\n"
        "    # via -r requirements.in\n"
        "pyyaml-ft==8.0.0 ; python_full_version == '3.13.*' \\\n"
        "    --hash=sha256:0525\n"
        "    # via libcst\n")))
    policy = write_policy(tmp_path)
    versions = {"pyyaml": "6.0.4", "pyyaml-ft": "8.0.0"}
    for full, kept, left_out in (("3.13.2", "pyyaml-ft", "pyyaml"), ("3.12.9", "pyyaml", "pyyaml-ft")):
        env = {**ENV, "python_full_version": full, "python_version": full[:4]}
        dump = {"environment": env, "distributions": [{"name": kept, "version": versions[kept], "License": "MIT"}]}
        found, notes = cl.judge_requirements(pins, dump, policy)
        assert [(v.record.key, v.allowed) for v in found] == [(kept, True)], full
        marker = next(p.marker for p in pins if p.key == left_out)
        assert notes == [f"{left_out}: not for this interpreter ({marker}), so not judged here"], full


# ------------------------------------------------- the policy file


def test_the_real_policy_holds_e55s_list_and_names_its_exceptions():
    policy = cl.load_policy()
    assert set(policy.allowed) == E55
    assert set(policy.tools_allowed) == {"MPL-2.0"}
    assert set(policy.exceptions) == {"certifi", "piper-tts"}
    assert policy.exceptions["certifi"].licence == "MPL-2.0"
    assert set(policy.exceptions["certifi"].via) == {
        "emet-providers[deepgram]", "emet-providers[anthropic]", "emet-providers[openai]"}
    assert policy.exceptions["piper-tts"].via == ("emet-providers[piper]",)
    assert policy.tools_exceptions == {}
    assert {k: (e.declares, e.licence) for k, e in policy.read.items()} == {
        "pocketsphinx": ("BSD", "BSD-2-Clause AND BSD-3-Clause AND MIT"), "colorama": ("BSD", "BSD-3-Clause"),
        "pip-api": ("Apache", "Apache-2.0"), "pip-audit": ("Apache", "Apache-2.0")}


def bad_policy(tmp_path: Path, **change) -> Path:
    data = {"allowed": sorted(E55), "tools_allowed": ["MPL-2.0"], "first_party": [], "read": [], "exceptions": [],
            "tools_exceptions": [], **change}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("change, message", [
    ({"allowed": ["Apache 2.0"]}, "'Apache 2.0' in allowed is not an SPDX identifier"),
    ({"families": {"BSD": "x"}}, "names families, which no longer pass"),
    ({"exceptions": [{**CERTIFI, "reason": ""}]}, "exceptions[0] has no reason"),
    ({"exceptions": [{**CERTIFI, "licence": "MIT"}]}, "MIT passes the allowed list on its own"),
    ({"tools_exceptions": [{**CERTIFI}]}, "MPL-2.0 passes the allowed list on its own"),
    ({"exceptions": [CERTIFI, CERTIFI]}, "exceptions names certifi twice"),
    ({"read": [{**COLORAMA_READ, "licence": "BSD"}]}, "read[0].licence names a family"),
    ({"read": [{**COLORAMA_READ, "where": ""}]}, "read[0] has no where"),
])
def test_a_policy_with_a_wrong_field_is_refused(tmp_path, change, message):
    with pytest.raises(cl.PolicyError, match=message.replace("[", r"\[").replace("]", r"\]").replace(".", r"\.")):
        cl.load_policy(bad_policy(tmp_path, **change))
