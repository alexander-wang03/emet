"""What the package's README tells an owner and a plugin author, held to the code.

Each test is a review finding of 0.5.7's Stage 3 that the README once got
wrong: a sentence that, followed as written, breaks a robot, misreports
what leaves the body, or leaves a local server open to a browser page.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
README = (PACKAGE / "README.md").read_text(encoding="utf-8")

def section(title: str) -> str:
    """The README's text under one `## ` heading, up to the next."""
    found = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README, re.M | re.S)
    assert found, f"the README has no section {title!r}"
    return found.group(1)


def toml_blocks(text: str) -> list[str]:
    return re.findall(r"```toml\n(.*?)```", text, re.S)


def sh_lines(text: str) -> list[str]:
    return [line for block in re.findall(r"```sh\n(.*?)```", text, re.S) for line in block.splitlines()]


def test_the_example_entry_points_offer_no_name_this_package_offers():
    """Review finding batch/docs-claims#1: the example registered `deepgram`
    in `emet.stt`, which 0.5.7's clash refusal turns into a refused boot."""
    ours = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["entry-points"]
    (block,) = toml_blocks(section("Writing one"))
    example = tomllib.loads(block)["project"]["entry-points"]
    assert example, "the example registers entry points"
    for group, names in example.items():
        assert group.startswith("emet."), group
        clash = set(names) & set(ours.get(group, {}))
        assert not clash, f"the README's example offers {sorted(clash)} in {group}, which emet-providers offers too"
    assert "DuplicatePluginError" in section("Writing one")


def test_writing_one_says_describe_reports_where_it_sends_and_which_key():
    """Review finding batch/docs-claims#2: a plugin that leaves `endpoint`
    None is printed `on this body`, and `--stats` counts nothing sent."""
    text = section("Writing one")
    for words in ("`endpoint`", "`key_env`", "on this body"):
        assert words in text, words


def test_the_llama_cpp_recipe_checks_a_key_and_limits_cors():
    """Review finding providers/claims-scope#1, ledger L52: a server bound to
    127.0.0.1 still answers a browser page on the same machine."""
    text = section("A local language model")
    (command,) = [line for line in sh_lines(text) if line.startswith("llama-server ")]
    assert "--host 127.0.0.1" in command
    assert '--api-key "$EMET_LOCAL_KEY"' in command
    assert "--cors-origins localhost" in command
    assert "browser" in text and "checks no key" in text


def test_the_fallback_is_described_as_the_plugins_take_it():
    """Review finding batch/docs-claims#3: at another server every answer but
    the model and a rejected key falls back to the list, so an unknown id
    boots there, and only a vendor's own address names a model unavailable at
    boot (`test_openai.py` and `test_anthropic.py` hold the code)."""
    text = section("A local language model")
    assert "any answer but the model or a rejected key" in text
    assert "when the server does not serve that path" not in README
    assert "a chat model OpenAI or Anthropic has\nretired" in README
