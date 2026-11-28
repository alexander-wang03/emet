"""Where each provider sends what it is given, and which key pays for it.

The three provider descriptors carry `endpoint` and `key_env`, so the boot
lines can say where the words, the prompt and the text to be spoken go, and
`--stats` can count what left the body for each host. Both default to None,
which a provider that runs on the body keeps. `host` is what gets printed:
the host and port alone, so a user name or a password written into a URL is
never printed with it.
"""

from __future__ import annotations

import dataclasses

import pytest

from emet_sdk.types import LanguageModelDescriptor, TranscriberDescriptor, VoiceDescriptor

DESCRIPTORS = (TranscriberDescriptor, LanguageModelDescriptor, VoiceDescriptor)


@pytest.mark.parametrize("kind", DESCRIPTORS)
def test_a_provider_that_says_nothing_sends_nothing(kind):
    described = kind("local")
    assert described.endpoint is None and described.key_env is None and described.host is None


@pytest.mark.parametrize("kind", DESCRIPTORS)
def test_the_two_fields_come_last_so_a_positional_descriptor_keeps_its_meaning(kind):
    names = [f.name for f in dataclasses.fields(kind)]
    assert names[-3:] == ["healthy", "endpoint", "key_env"]


def test_a_descriptor_built_by_position_before_the_fields_existed_is_unchanged():
    assert LanguageModelDescriptor("openai", "gpt-5.6-terra", True, True, False).healthy is False
    assert TranscriberDescriptor("deepgram", "nova-3", True, 16000, True).endpoint is None
    assert VoiceDescriptor("piper", "en_US-ljspeech-medium", 22050, False, True).key_env is None


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        ("https://api.openai.com/v1", "api.openai.com"),
        ("https://api.anthropic.com", "api.anthropic.com"),
        ("wss://api.deepgram.com/v1/listen", "api.deepgram.com"),
        ("https://api.deepgram.com/v1/speak", "api.deepgram.com"),
        ("http://127.0.0.1:9380/v1", "127.0.0.1:9380"),
        ("http://localhost:11434/v1", "localhost:11434"),
        ("http://[::1]:8080/v1", "[::1]:8080"),
        ("HTTPS://API.Example.COM/v1", "api.example.com"),
        ("http://pi.local:8080", "pi.local:8080"),
        ("localhost:11434", "localhost:11434"),
        ("localhost:11434/v1", "localhost:11434"),
        ("http://server:notaport/v1", "server"),
    ],
)
def test_the_host_is_the_host_and_port_alone(endpoint, host):
    for kind in DESCRIPTORS:
        assert kind("p", endpoint=endpoint).host == host


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        ("localhost:11434?api_key=sk-secret", "localhost:11434"),
        ("example.com#token=sk-secret", "example.com"),
        ("LOCALHOST:11434/v1", "localhost:11434"),
        ("owner:pa/ss@localhost:11434/v1", "localhost:11434"),
    ],
)
def test_an_endpoint_with_no_scheme_is_cut_and_lower_cased_as_a_url_is(endpoint, host):
    """A query can carry a key, and the boot line, the `--stats` footer and
    the events file print `host`. The `@` is found before the `/`, so a
    password holding a `/` is not cut into the host."""
    for kind in DESCRIPTORS:
        assert kind("p", endpoint=endpoint).host == host


@pytest.mark.parametrize(
    ("endpoint", "host"),
    [
        ("http://[::1/v1", "[::1"),
        ("http://[::1:8080/v1", "[::1:8080"),
        ("http://[abc]/v1", "[abc]"),
        ("http://owner:hunter2@[::1/v1?key=sk-secret", "[::1"),
        ("http://ex\uff03ample.com/v1", "ex\uff03ample.com"),
    ],
)
def test_a_url_urlsplit_cannot_read_still_has_a_host(endpoint, host):
    """An open bracket, a bracket around no address, and a host that changes
    under NFKC each make `urlsplit` raise. `host` names what was written
    instead, with no credentials, so the line that prints it never raises."""
    for kind in DESCRIPTORS:
        assert kind("p", endpoint=endpoint).host == host


@pytest.mark.parametrize(
    "endpoint",
    ["https://owner:hunter2@example.com/v1", "http://hunter2@10.0.0.5:8080/v1", "owner:hunter2@example.com"],
)
def test_credentials_written_into_a_url_never_reach_the_host(endpoint):
    host = LanguageModelDescriptor("openai", endpoint=endpoint).host
    assert host is not None and "hunter2" not in host and "owner" not in host


def test_the_key_is_named_by_its_variable():
    described = TranscriberDescriptor("deepgram", "nova-3", endpoint="wss://api.deepgram.com/v1/listen",
                                      key_env="EMET_DEEPGRAM_KEY")
    assert described.key_env == "EMET_DEEPGRAM_KEY"
    assert dataclasses.replace(described, key_env=None).key_env is None
