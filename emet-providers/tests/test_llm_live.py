"""One real reply from each vendor, when a key is present and asked for.

Skipped in CI and on any machine without `EMET_LIVE_TESTS=1` and the vendor's
key in the environment. Each asks for a single word and spends a few tokens.
What it proves is the half the stand-ins cannot: that the headers, the body
and the stream are what the live service accepts today. Run by hand before a
release, and whenever a vendor announces a change.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from emet_sdk.types import Message, Prompt, ReplyDone, TextDelta

LIVE = os.environ.get("EMET_LIVE_TESTS") == "1"


def _round_trip(llm):
    prompt = Prompt(
        system="You are a test. Reply with the single word: ready.",
        messages=(Message(role="user", content="Are you there?"),),
        max_tokens=64,
    )

    async def scenario():
        await llm.start()
        assert llm.describe().healthy, llm.health().detail
        events = [e async for e in llm.reply(prompt)]
        await llm.shutdown()
        return events

    events = asyncio.run(scenario())
    done = events[-1]
    assert isinstance(done, ReplyDone)
    assert done.stop_reason == "end", done
    assert done.text.strip(), done
    assert any(isinstance(e, TextDelta) for e in events)
    assert done.output_tokens is None or done.output_tokens > 0
    assert llm.last_error is None, llm.last_error
    return done


@pytest.mark.skipif(
    not (LIVE and os.environ.get("EMET_ANTHROPIC_KEY")),
    reason="set EMET_ANTHROPIC_KEY and EMET_LIVE_TESTS=1 to call Anthropic for real",
)
def test_anthropic_answers():
    pytest.importorskip("httpx", reason="emet-providers[anthropic] is not installed")
    from emet_providers.anthropic import AnthropicLanguageModel

    done = _round_trip(AnthropicLanguageModel({"provider": "anthropic"}))
    assert done.model


@pytest.mark.skipif(
    not (LIVE and os.environ.get("EMET_OPENAI_KEY")),
    reason="set EMET_OPENAI_KEY and EMET_LIVE_TESTS=1 to call OpenAI for real",
)
def test_openai_answers():
    pytest.importorskip("httpx", reason="emet-providers[openai] is not installed")
    from emet_providers.openai import OpenAILanguageModel

    done = _round_trip(OpenAILanguageModel({"provider": "openai"}))
    assert done.model
