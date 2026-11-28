"""What the HTTP providers share: the error line, the fault kind, where a host is.

No network. `fault_kind` and `place_of` are read by the OpenAI and the
Anthropic plugins, and `fault_kind` by both Deepgram plugins, so each rule
is held here once, with a table of the cases each plugin relies on.
"""

from __future__ import annotations

import json
import socket

import pytest

from emet_providers._http import (
    LOCAL_NETWORK,
    THIS_MACHINE,
    api_error,
    endpoint_of,
    error_code,
    fault_kind,
    place_of,
    url_problem,
    who,
)


def body(**error) -> bytes:
    return json.dumps({"error": error}).encode()


# ----------------------------------------------------------------- api_error


@pytest.mark.parametrize(
    ("status", "given", "line"),
    [
        (404, body(type="invalid_request_error", code="model_not_found", message="The model `x` does not exist"),
         "HTTP 404 invalid_request_error, model_not_found: The model `x` does not exist"),
        (500, body(type="server_error", code="server_error", message="boom"), "HTTP 500 server_error: boom"),
        (529, body(type="overloaded_error", message="Overloaded"), "HTTP 529 overloaded_error: Overloaded"),
        (429, body(code="rate_limit_exceeded", message="slow"), "HTTP 429 rate_limit_exceeded: slow"),
        (400, body(message="no kind"), "HTTP 400 error: no kind"),
        (401, body(type="authentication_error", code=None, message=""), "HTTP 401 authentication_error"),
        (402, json.dumps({"err_code": "PAYMENT_REQUIRED", "err_msg": "Insufficient credits"}).encode(),
         "HTTP 402 PAYMENT_REQUIRED: Insufficient credits"),
        (502, b"<html>Bad Gateway</html>", "HTTP 502: <html>Bad Gateway</html>"),
        (500, b'{"detail": "no error key"}', 'HTTP 500: {"detail": "no error key"}'),
        (503, "  plain text  ", "HTTP 503: plain text"),
    ],
    ids=["type-and-code", "equal", "type-only", "code-only", "neither", "no-message", "deepgram", "html", "other-json", "str"],
)
def test_api_error_names_the_type_then_a_different_code(status, given, line):
    assert api_error(status, given) == line


def test_error_code_prefers_the_code_and_falls_back_to_the_type():
    assert error_code(body(type="insufficient_quota", code="credit_balance_exhausted")) == "credit_balance_exhausted"
    assert error_code(body(type="insufficient_quota", code=None)) == "insufficient_quota"
    assert error_code(json.dumps({"err_code": "INSUFFICIENT_CREDITS"})) == "INSUFFICIENT_CREDITS"
    assert error_code(b"not json") is None


# ---------------------------------------------------------------- fault_kind


CODES = {
    (429, "insufficient_quota"): "out_of_credit",
    (429, "credit_balance_exhausted"): "out_of_credit",
    (404, "model_not_found"): "model_unavailable",
    (402, None): "out_of_credit",
}


@pytest.mark.parametrize(
    ("status", "given", "kind"),
    [
        (429, body(type="insufficient_quota", code="credit_balance_exhausted"), "out_of_credit"),
        (429, body(type="insufficient_quota", code="something_new"), "out_of_credit"),
        (429, body(type="requests", code="rate_limit_exceeded"), None),
        (404, body(type="invalid_request_error", code="model_not_found"), "model_unavailable"),
        (404, body(type="invalid_request_error", message="Not found"), None),
        (404, b"404 page not found", None),
        (402, b"", "out_of_credit"),
        (402, json.dumps({"err_code": "ANYTHING"}).encode(), "out_of_credit"),
        (500, body(type="insufficient_quota"), None),
        (429, body(type=7, code=["credit_balance_exhausted"]), None),
    ],
    ids=["code", "type-when-the-code-has-no-row", "rate-limit", "model-code", "bare-404", "not-json",
         "bare-status-row", "deepgram-code-falls-to-status", "wrong-status", "not-strings"],
)
def test_fault_kind_reads_the_code_then_the_type_then_the_bare_status(status, given, kind):
    assert fault_kind(status, given, CODES) == kind


def test_the_code_is_read_before_the_type():
    codes = {(429, "credit_balance_exhausted"): "out_of_credit", (429, "insufficient_quota"): "x_quota"}
    assert fault_kind(429, body(type="insufficient_quota", code="credit_balance_exhausted"), codes) == "out_of_credit"


def test_the_kind_never_comes_from_the_message():
    given = body(type="invalid_request_error", message="You exceeded your current quota and the model does not exist")
    assert fault_kind(429, given, CODES) is None and fault_kind(404, given, CODES) is None


# ------------------------------------------------------------------ place_of


@pytest.mark.parametrize(
    ("host", "place"),
    [
        ("127.0.0.1:9380", THIS_MACHINE),
        ("127.8.9.1", THIS_MACHINE),
        ("localhost", THIS_MACHINE),
        ("LOCALHOST:11434", THIS_MACHINE),
        ("[::1]:8080", THIS_MACHINE),
        ("::1", THIS_MACHINE),
        ("[::ffff:127.0.0.1]:80", THIS_MACHINE),
        ("0.0.0.0:8080", THIS_MACHINE),
        ("192.168.1.20:11434", LOCAL_NETWORK),
        ("10.0.0.2", LOCAL_NETWORK),
        ("172.16.5.4:80", LOCAL_NETWORK),
        ("169.254.1.1", LOCAL_NETWORK),
        ("[fe80::1]:8080", LOCAL_NETWORK),
        ("[fd12:3456::1]", LOCAL_NETWORK),
        ("emet-pi.local", LOCAL_NETWORK),
        ("Emet-Pi.Local.:8080", LOCAL_NETWORK),
        ("api.openai.com", None),
        ("api.anthropic.com:443", None),
        ("8.8.8.8", None),
        ("[2606:4700::1111]:443", None),
        ("100.64.0.1", None),
        ("local", None),
        ("notlocal.example", None),
        ("", None),
    ],
)
def test_place_of_says_where_a_host_is(host, place):
    assert place_of(host) == place


def test_place_of_looks_up_no_name(monkeypatch):
    def refuse(*_args, **_kw):
        raise AssertionError("place_of looked a name up")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, refuse)
    assert place_of("my-server.example:8080") is None
    assert place_of("emet-pi.local") == LOCAL_NETWORK


# ---------------------------------------------------------- endpoint_of, who


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("http://me:secret@127.0.0.1:9380/v1?k=secret#top", "http://127.0.0.1:9380/v1"),
        ("http://[::1]:8080/v1", "http://[::1]:8080/v1"),
        ("localhost:11434/v1?x=1", "localhost:11434/v1"),
        # Review findings providers/tests#5 and providers/code#4: a URL with no scheme loses its credentials too.
        ("me:hunter2@127.0.0.1:9380/v1", "127.0.0.1:9380/v1"),
        ("me@localhost:11434", "localhost:11434"),
        # Review finding sdk/code#1: a URL urlsplit cannot read loses its credentials and raises nothing.
        ("http://me:hunter2@[::1/v1?key=sk-secret#top", "http://[::1/v1"),
    ],
)
def test_endpoint_of_drops_credentials_query_and_fragment(url, endpoint):
    assert endpoint_of(url) == endpoint


@pytest.mark.parametrize("url", ["http://me:hunter2@[::1/v1", "http://127.0.0.1:port/v1"])
def test_a_url_that_cannot_be_read_is_one_refusal_naming_its_field_and_no_password(url):
    problem = url_problem(url, "models.chat.params.url")
    assert problem is not None and problem.startswith("models.chat.params.url cannot be read as a URL (")
    assert "hunter2" not in problem and problem.endswith(". Correct it in the manifest.")
    assert url_problem("http://127.0.0.1:9380/v1", "models.chat.params.url") is None


def test_who_names_the_vendor_at_its_own_url_and_the_server_elsewhere():
    assert who("https://api.openai.com/v1/", default_url="https://api.openai.com/v1", vendor="OpenAI", host="api.openai.com") == "OpenAI"
    assert who("https://api.openai.com/v1", default_url="https://api.openai.com/v1/", vendor="OpenAI", host="api.openai.com") == "OpenAI"
    assert who("http://127.0.0.1:9380/v1", default_url="https://api.openai.com/v1", vendor="OpenAI", host="127.0.0.1:9380") == (
        "the server at 127.0.0.1:9380"
    )
    assert who("https://gateway.example/v1", default_url="https://api.openai.com/v1", vendor="OpenAI", host=None) == (
        "the server at https://gateway.example/v1"
    )
    assert who("http://me:pw@/v1?k=pw", default_url="https://api.openai.com/v1", vendor="OpenAI", host=None) == (
        "the server at http:///v1"
    ), "with no host the URL is named without its credentials or query"
