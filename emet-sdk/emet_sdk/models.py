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

Only speech recognition is here today. Language models and speech synthesis
take the same shape when they arrive.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["stt_selection"]


def stt_selection(
    manifest: Mapping[str, Any],
    soul: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve which speech recognition provider runs, and with what.

    The rule, in order:

    1. If the body's `audio.stt.provider` is set, the body has taken speech
       recognition over. Its `provider`, `model` and `key_env` are used and
       the soul's are ignored, all three together. A provider name from one
       document with a model name and a key from another is a broken
       reference, so the override is whole or nothing.
    2. Otherwise the soul's `models.stt` chooses: `provider`, `model`,
       `key_env`.
    3. Either way the body's `audio.stt.params` ride along untouched. They
       are tuning for this deployment (an endpoint, a timeout) and belong to
       whichever provider ends up running.

    Returns None when neither document names a provider. There is no default:
    speech recognition is a cloud account nobody can be assumed to have, and
    quietly choosing one would spend somebody's credits without asking.

    The returned mapping is what a `TranscriberPlugin` is constructed from:
    `provider`, `model`, `key_env`, `params`. The key itself is never in it.
    """
    body = (manifest.get("audio") or {}).get("stt") or {}
    soul_ref = (soul.get("models") or {}).get("stt") or {}

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
