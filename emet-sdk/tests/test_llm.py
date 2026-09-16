"""The language model seam.

The second provider seam, built like the first: a contract in the SDK, a
mock behind it, then the vendors. Two vendors ship in the same batch, because
a seam with one implementation is untested as a seam.

**Note what this file imports.** Only `emet_sdk`. Every plugin it exercises
lives in `emet_providers` and arrives by discovery, which is the constraint
the engine works under.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from emet_sdk.discovery import GROUP_LLM, PluginRegistry
from emet_sdk.models import STAGES, chat_selection, model_selection, stt_selection
from emet_sdk.plugin import LanguageModelPlugin
from emet_sdk.types import (
    LanguageModelDescriptor,
    Message,
    Prompt,
    ReplyDone,
    TextDelta,
    ToolCall,
    ToolSpec,
)
from emet_sdk.validate import MissingPluginError, load_yaml, validate_manifest, validate_soul

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run(coro):
    return asyncio.run(coro)


def codes(report) -> set[str]:
    return {f.code for f in report.errors}


def manifest(**models: Any) -> dict:
    doc = {"audio": {"input": {"device": "x"}, "output": {"device": "y"}}}
    if models:
        doc["models"] = models
    return doc


def soul(**models: Any) -> dict:
    doc = {"identity": {"name": "Emet", "wake_word": "hey emet"}}
    if models:
        doc["models"] = models
    return doc


async def collect(events: AsyncIterator) -> list:
    return [e async for e in events]


class _FakeBatchModel(LanguageModelPlugin):
    """Defined here rather than imported from emet_providers: the SDK's own
    tests must not depend on the layer above it. Sends the whole text as one
    delta, the way a provider that does not stream would."""

    provider = "fake"

    def describe(self) -> LanguageModelDescriptor:
        return LanguageModelDescriptor(provider=self.provider, model=self.model, streaming=False, tools=False)

    async def reply(self, prompt: Prompt):
        last = prompt.messages[-1].content if prompt.messages else ""
        yield TextDelta(f"You said {last}.")
        yield ReplyDone(text=f"You said {last}.", stop_reason="end", model="fake-1", output_tokens=3)


# ---------------------------------------------------------------- discovery


def test_llm_is_a_real_entry_point_group():
    registry = PluginRegistry.discover()
    assert GROUP_LLM == "emet.llm"
    assert {"mock", "anthropic", "openai"} <= set(registry.llm_names), (
        "emet-providers registers three language models; if this fails the "
        "package needs installing so its entry points are picked up"
    )
    assert registry.has_llm("mock")


def test_llm_appears_in_the_registry_listing():
    assert not PluginRegistry(llm={})
    listed = {group for group, _ in PluginRegistry.discover()}
    assert "llm" in listed


def test_an_uninstalled_provider_is_a_missing_plugin_not_a_schema_error():
    registry = PluginRegistry.discover()
    with pytest.raises(MissingPluginError) as exc:
        registry.load_llm("gemini")
    assert "gemini" in str(exc.value.report)
    assert "language model provider" in str(exc.value.report)


def test_every_shipped_provider_arrives_without_being_imported():
    registry = PluginRegistry.discover()
    for name in ("mock", "anthropic", "openai"):
        cls = registry.load_llm(name)
        assert cls.__module__.startswith("emet_providers"), cls.__module__
        assert issubclass(cls, LanguageModelPlugin)
        assert cls.provider == name


def test_the_engine_can_build_and_drive_a_model_it_never_imported():
    """A name from the documents, a class from the registry, `cls(config)`,
    a prompt in, a streamed reply out."""
    selection = chat_selection(manifest(), soul(chat={"provider": "mock"}))
    assert selection is not None
    llm = PluginRegistry.discover().load_llm(selection["provider"])(selection)
    prompt = Prompt(system="Be brief.", messages=(Message(role="user", content="what time is it"),))

    async def scenario():
        await llm.start()
        assert llm.describe().healthy
        events = await collect(llm.reply(prompt))
        await llm.shutdown()
        return events

    events = run(scenario())
    assert isinstance(events[-1], ReplyDone)
    assert events[-1].stop_reason == "end"
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert deltas and "".join(d.text for d in deltas) == events[-1].text
    assert "what time is it" in events[-1].text


# ----------------------------------------------------------------- contract


def test_a_batch_model_satisfies_the_same_contract():
    llm = _FakeBatchModel({"provider": "fake", "model": "fake-1"})
    prompt = Prompt(system="", messages=(Message(role="user", content="hello"),))
    events = run(collect(llm.reply(prompt)))
    assert [type(e) for e in events] == [TextDelta, ReplyDone]
    assert events[-1].text == "You said hello."
    assert not llm.describe().streaming and not llm.describe().tools
    assert llm.model == "fake-1"


def test_the_config_is_unpacked_the_way_the_engine_hands_it_over():
    llm = _FakeBatchModel({"provider": "fake", "model": "m", "key_env": "K", "params": {"x": 1}})
    assert (llm.model, llm.key_env, llm.params) == ("m", "K", {"x": 1})
    assert "key" not in {k.lower() for k in llm.config}


def test_reply_done_only_speaks_the_seams_stop_reasons():
    for reason in ("end", "tool", "length", "refusal", "error"):
        ReplyDone(text="", stop_reason=reason)
    with pytest.raises(ValueError):
        ReplyDone(text="", stop_reason="max_tokens")


def test_messages_carry_roles_the_providers_can_map():
    Message(role="user", content="hi")
    Message(role="assistant", content="", tool_calls=(ToolCall(id="c1", name="remember"),))
    Message(role="tool", content="ok", tool_call_id="c1")
    with pytest.raises(ValueError):
        Message(role="system", content="no: the system prompt lives on the Prompt")
    with pytest.raises(ValueError):
        Message(role="tool", content="ok")
    with pytest.raises(ValueError):
        Message(role="user", content="x", tool_calls=(ToolCall(id="c", name="n"),))


def test_a_prompt_bounds_its_reply_and_a_tool_spec_defaults_to_no_parameters():
    prompt = Prompt(system="s", messages=(), tools=(ToolSpec(name="remember", description="keep a fact"),))
    assert prompt.max_tokens == 1024
    assert prompt.tools[0].parameters == {"type": "object", "properties": {}}
    with pytest.raises(ValueError):
        Prompt(system="s", messages=(), max_tokens=0)


def test_a_language_model_is_a_plugin_with_the_shared_lifecycle():
    from emet_sdk.plugin import Plugin

    llm = _FakeBatchModel({"provider": "fake"})
    assert isinstance(llm, Plugin)
    assert llm.health().ok
    run(llm.start())
    run(llm.shutdown())


# ---------------------------------------------------------------- selection


def test_one_rule_for_every_stage():
    assert STAGES == {"stt", "chat", "tts", "micro"}
    soul_doc = soul(chat={"provider": "openai", "model": "m", "key_env": "EMET_OPENAI_KEY"})
    assert chat_selection(manifest(), soul_doc) == {
        "provider": "openai",
        "model": "m",
        "key_env": "EMET_OPENAI_KEY",
        "params": {},
    }
    assert model_selection(manifest(), soul_doc, "chat") == chat_selection(manifest(), soul_doc)
    assert stt_selection(manifest(), soul_doc) is None


def test_the_body_takes_a_stage_over_whole_or_not_at_all():
    chosen = chat_selection(
        manifest(chat={"provider": "mock", "params": {"reply": "hi"}}),
        soul(chat={"provider": "openai", "model": "m", "key_env": "EMET_OPENAI_KEY"}),
    )
    assert chosen == {"provider": "mock", "model": None, "key_env": None, "params": {"reply": "hi"}}


def test_the_body_tunes_the_souls_choice_of_model():
    chosen = chat_selection(
        manifest(chat={"params": {"effort": "low"}}), soul(chat={"provider": "anthropic"})
    )
    assert chosen is not None
    assert chosen["provider"] == "anthropic" and chosen["params"] == {"effort": "low"}


def test_stages_are_independent():
    """Taking speech recognition over says nothing about the language model."""
    chosen = chat_selection(manifest(stt={"provider": "mock"}), soul(chat={"provider": "anthropic"}))
    assert chosen is not None and chosen["provider"] == "anthropic"
    assert chat_selection(manifest(stt={"provider": "mock"}), soul()) is None


def test_an_unknown_stage_is_a_programming_error():
    with pytest.raises(ValueError, match="stage"):
        model_selection(manifest(), soul(), "vision")


def test_the_reference_soul_names_a_language_model_that_ships():
    """The reference soul is what newcomers copy, so it names a provider that
    is installed, with the model that plugin would use unasked."""
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    chosen = chat_selection(load_yaml(EXAMPLES / "bodiless.yaml"), doc)
    assert chosen is not None and chosen["provider"] == "openai"
    assert chosen["key_env"] == "EMET_OPENAI_KEY"
    cls = PluginRegistry.discover().load_llm("openai")
    assert chosen["model"] == getattr(sys.modules[cls.__module__], "DEFAULT_MODEL")


# --------------------------------------------------------------- validation


def test_a_body_naming_an_uninstalled_language_model_is_an_error():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"chat": {"provider": "abacus"}}
    report = validate_manifest(doc)
    assert not report.ok
    assert "missing_plugin" in codes(report)
    assert any(f.path == "/models/chat/provider" for f in report.errors)


def test_the_invalid_fixture_says_the_same():
    report = validate_manifest(load_yaml(EXAMPLES / "invalid" / "unknown-llm-provider.yaml"))
    assert not report.ok and "missing_plugin" in codes(report)


def test_a_body_naming_shipped_providers_validates_clean():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {
        "stt": {"provider": "mock"},
        "chat": {"provider": "anthropic", "params": {"effort": "low"}},
    }
    assert validate_manifest(doc).ok


def test_the_reserved_stage_is_accepted_and_not_checked():
    """`micro` resolves against nothing yet, so a body may name anything
    there and the schema still accepts the shape. `tts` used to be here and
    is checked since the voice seam arrived."""
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"micro": {"provider": "nobody_yet", "params": {"x": 1}}}
    assert validate_manifest(doc).ok


def test_an_unknown_key_under_a_stage_is_a_schema_error():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"chat": {"provider": "mock", "api_key": "never-in-a-manifest"}}
    assert "schema" in codes(validate_manifest(doc))


def test_an_unknown_stage_is_a_schema_error():
    doc = load_yaml(EXAMPLES / "bodiless.yaml")
    doc["models"] = {"vision": {"provider": "mock"}}
    assert "schema" in codes(validate_manifest(doc))


def test_a_soul_naming_an_uninstalled_language_model_still_validates():
    doc = load_yaml(EXAMPLES / "emet-soul.yaml")
    doc["models"]["chat"] = {"provider": "abacus", "key_env": "X"}
    assert validate_soul(doc).ok


def test_the_mocked_example_body_takes_both_stages_over():
    doc = load_yaml(EXAMPLES / "mock-scout.yaml")
    assert doc["models"]["stt"]["provider"] == "mock"
    assert doc["models"]["chat"]["provider"] == "mock"
    assert validate_manifest(doc).ok
