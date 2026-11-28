"""Every default model Emet ships, with the date its vendor may retire it.

A soul that names only `provider: anthropic` gets the plugin's
`DEFAULT_MODEL`, and the example soul names a model for each cloud stage, so
an engine left unupgraded meets its default's retirement the day the vendor
turns it off. This table holds what each vendor has said about each default,
and `tools/release_check.py` holds a release to 180 days of runway from it:
a `retires` date fewer than 180 days away is a problem, a `floor` fewer than
180 days away is a warning, and a row read more than 45 days ago is a
warning to read its source again. The monthly canary asks each vendor's
models endpoint about each row, which costs no tokens.

The fields of a row:

- `vendor`: `openai`, `anthropic` or `deepgram`, the plugin family.
- `model`: the id exactly as the plugin sends it.
- `used_by`: where Emet names it, a plugin's `DEFAULT_MODEL` or a stage of
  the example soul.
- `retires`: the retirement date the vendor has announced, `YYYY-MM-DD`, or
  None while none is announced.
- `floor`: the earliest date the vendor's own policy allows a retirement,
  or None when it publishes none for the model. Anthropic's model
  deprecations page gives one per model, "Not sooner than"; its Models API
  does not, since `retires_at` is null while a model is active (Get a Model,
  verified 2026-11-27), so a floor is read from the page by hand.
- `pinned`: True when the vendor says the id names one fixed snapshot,
  False when it says the model behind the id changes, None while unverified.
- `source`: the page the row was read from.
- `read`: the day it was read there, `YYYY-MM-DD`.

Each row is written with literal values only, so `ast` reads the table
without importing this module or anything it imports. Standard library only.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["DefaultModel", "DEFAULTS"]


class DefaultModel(NamedTuple):
    """One default model, with what its vendor has said about retiring it."""

    vendor: str
    model: str
    used_by: tuple[str, ...]
    retires: str | None
    floor: str | None
    pinned: bool | None
    source: str
    read: str


DEFAULTS: tuple[DefaultModel, ...] = (
    # OpenAI's deprecations page names gpt-5.6-terra only as the replacement
    # of models it retires, and states "Generally available models: At least
    # 6 months" of notice (verified 2026-11-27), which is a notice period and
    # no floor date. Ledger L42, both facts read 2026-11-27: OpenAI's models
    # page lists gpt-6.1-terra and no longer lists gpt-5.6-terra; and whether
    # gpt-5.6-terra is a pinned snapshot or an alias is unverified, since its
    # model page lists it as its own "Default snapshot" with no dated id.
    DefaultModel(
        vendor="openai",
        model="gpt-5.6-terra",
        used_by=("emet_providers/openai.py DEFAULT_MODEL", "emet-sdk/examples/emet-soul.yaml models.chat"),
        retires=None,
        floor=None,
        pinned=None,
        source="https://developers.openai.com/api/docs/deprecations",
        read="2026-11-27",
    ),
    # Anthropic's model deprecations page: claude-opus-5 "Active", "N/A",
    # "Not sooner than July 24, 2027". Its models overview: "Every Claude
    # model ID is a pinned snapshot, including the dateless IDs used from the
    # 4.6 generation on." Both verified 2026-11-27.
    DefaultModel(
        vendor="anthropic",
        model="claude-opus-5",
        used_by=("emet_providers/anthropic.py DEFAULT_MODEL",),
        retires=None,
        floor="2027-07-24",
        pinned=True,
        source="https://platform.claude.com/docs/en/about-claude/model-deprecations",
        read="2026-11-27",
    ),
    # Deepgram lists nova-3 as a current general model and states no
    # retirement for it, and publishes no notice policy (models and languages
    # overview, verified 2026-11-27). Whether the name always serves one
    # version is not stated there, so pinned is unverified.
    DefaultModel(
        vendor="deepgram",
        model="nova-3",
        used_by=("emet_providers/deepgram.py DEFAULT_MODEL", "emet-sdk/examples/emet-soul.yaml models.stt"),
        retires=None,
        floor=None,
        pinned=None,
        source="https://developers.deepgram.com/docs/models-languages-overview",
        read="2026-11-27",
    ),
    # Deepgram's voice models page lists aura-2-thalia-en among the Aura-2
    # English voices, with no retirement stated (verified 2026-11-27). The
    # example soul names it only in a comment, as the one-line cloud voice.
    DefaultModel(
        vendor="deepgram",
        model="aura-2-thalia-en",
        used_by=("emet_providers/deepgram_voice.py DEFAULT_MODEL",),
        retires=None,
        floor=None,
        pinned=None,
        source="https://developers.deepgram.com/docs/tts-models",
        read="2026-11-27",
    ),
)
