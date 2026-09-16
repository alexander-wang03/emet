"""Emet providers: the plugins that reach a service.

Hardware lives in `emet-hal`. This package is its counterpart for the things
a robot borrows from a computer somewhere else: speech recognition today,
language models and speech synthesis as releases arrive. They are plugins in
exactly the sense drivers are: they satisfy a contract in `emet_sdk.plugin`,
advertise themselves through entry points, and reach the engine by name.

Why a package of its own. `emet-hal` is named for hardware, and a client for
a speech service does not belong under that heading. Provider client
libraries are also heavy and networked, and a body that only ever wakes should
not install them. Keeping them here keeps the HAL a HAL.

Layering, enforced in CI: this package imports `emet_sdk` and nothing else.

Shipped, speech recognition (`emet.stt`):

    mock        a transcriber that reads words out of the bytes it is given
    deepgram    streaming recognition through Deepgram      (extra: deepgram)

Shipped, language models (`emet.llm`):

    mock        repeats what it heard, or says a scripted line, a word a delta
    anthropic   Claude through the Messages API              (extra: anthropic)
    openai      GPT, or any Chat Completions server          (extra: openai)

In each group the mock came first, so that the real providers were written
against the contract rather than the contract against a provider, and the
language models came as a pair, because a seam with one implementation is
untested as a seam.
"""

from importlib import metadata as _metadata

#: Read from the installed distribution rather than written here, so that
#: `pyproject.toml` is the single place this number appears. Two declarations
#: drift silently, and did once: the metadata said one version and the
#: source said another, and nothing compared them.
try:
    __version__ = _metadata.version("emet-providers")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    # Imported from a tree that was never installed. Say so rather than
    # inventing a number that would later be reported as fact.
    __version__ = "0+unknown"
