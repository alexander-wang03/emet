"""Which model provider a body and a soul agree on.

The soul's `models` block names the providers it was written for. The keys
are the owner's (BYOK, bring your own keys) and they travel with the soul, so
a provider is a soul field without breaching principle 1: a cloud account is
not hardware. The body has a say too, and the two documents have to be read
together to know what runs.

Pure functions over contract data, which is why this lives in the SDK rather
than in the engine (`DESIGN.md` section 3): the engine and the validator must
agree on what an absent field means, and two copies of that rule would drift
onto somebody else's robot before anyone noticed.

One rule for every stage. Speech recognition (`stt`), the language model
(`chat`), speech synthesis (`tts`) and the backchannel micro-model (`micro`)
are chosen the same way, from the same two blocks, so a manifest author learns
it once.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["STAGES", "model_selection", "stt_selection", "chat_selection", "tts_selection"]

#: The keys of a `models` block, on the soul and on the body alike. Each names
#: one stage of the conversation and resolves against one entry-point group:
#: `stt` against `emet.stt`, `chat` against `emet.llm`, `tts` against
#: `emet.tts`. `micro` is reserved in the schema and resolves against nothing
#: yet.
STAGES: frozenset[str] = frozenset({"stt", "chat", "tts", "micro"})


def model_selection(
    manifest: Mapping[str, Any],
    soul: Mapping[str, Any],
    stage: str,
) -> dict[str, Any] | None:
    """Resolve which provider runs one stage, and with what.

    The rule, in order:

    1. If the body's `models.<stage>.provider` is set, the body has taken the
       stage over. Its `provider`, `model` and `key_env` are used and the
       soul's are ignored, all three together. A provider name from one
       document with a model name and a key from another is a broken
       reference, so the override is whole or nothing.
    2. Otherwise the soul's `models.<stage>` chooses: `provider`, `model`,
       `key_env`.
    3. Either way the body's `models.<stage>.params` ride along untouched.
       They are tuning for this deployment (an endpoint, a timeout) and
       belong to whichever provider ends up running.

    Returns None when neither document names a provider. There is no default:
    every stage is a cloud account nobody can be assumed to have, and quietly
    choosing one would spend somebody's credits without asking.

    The returned mapping is what the stage's plugin is constructed from:
    `provider`, `model`, `key_env`, `params`. The key itself is never in it.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown model stage {stage!r}; expected one of {sorted(STAGES)}")

    body = (manifest.get("models") or {}).get(stage) or {}
    soul_ref = (soul.get("models") or {}).get(stage) or {}

    chosen = body if body.get("provider") else soul_ref
    provider = chosen.get("provider")
    if not isinstance(provider, str) or not provider:
        return None

    return {
        "provider": provider,
        "model": chosen.get("model"),
        "key_env": chosen.get("key_env"),
        "params": dict(body.get("params") or {}),
    }


def stt_selection(manifest: Mapping[str, Any], soul: Mapping[str, Any]) -> dict[str, Any] | None:
    """Which speech recognition provider runs. `model_selection(..., "stt")`."""
    return model_selection(manifest, soul, "stt")


def chat_selection(manifest: Mapping[str, Any], soul: Mapping[str, Any]) -> dict[str, Any] | None:
    """Which language model runs. `model_selection(..., "chat")`."""
    return model_selection(manifest, soul, "chat")


def tts_selection(manifest: Mapping[str, Any], soul: Mapping[str, Any]) -> dict[str, Any] | None:
    """Which voice speaks. `model_selection(..., "tts")`.

    The same rule as the other stages, and the same absence of a default,
    though the reason differs: the shipped local voice costs nothing per
    word, and there is still no default because a voice model is a file
    somebody has to have downloaded, and a robot that chose one quietly would
    fail at boot naming a file nobody asked for.
    """
    return model_selection(manifest, soul, "tts")
