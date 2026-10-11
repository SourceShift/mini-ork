"""Hermetic tests for ``mini_ork.pm.http`` — the one call the PM adapters make.

``urllib.request.urlopen`` is replaced, so nothing here touches the network and
every failure mode a remote service can produce is produced locally. What is
pinned:

* an ``https``-only helper, refused before the socket opens
* every transport failure becomes a ``ProviderError`` naming the source and the
  operation — never a raw ``URLError`` escaping into a page
* **the credential never appears in that message**, including when the failing
  service echoes the token back in its own error body. This is the assertion the
  helper exists for: a leak here would depend on a remote service's formatting,
  which is not something a caller can control.
* a bounded snippet, an empty body reading as ``None``, a non-JSON body named
  as such
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.pm import http as pm_http  # noqa: E402
from mini_ork.pm.provider import ProviderError  # noqa: E402

URL = "https://api.github.com/repos/acme/app/issues"
TOKEN = "ghp_notarealtoken"


class _Response:
    """What ``urlopen`` returns: a context manager over a body."""

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def read(self) -> bytes:
        return self._body.read()


class _URLopen:
    """A recording stand-in. ``answers`` is a queue of bodies or exceptions."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.answers:
            raise AssertionError(f"nothing queued for {request.full_url}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)

    @property
    def sent_headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.requests[-1].header_items()}


@pytest.fixture
def transport(monkeypatch) -> _URLopen:
    opener = _URLopen()
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    return opener


def _http_error(code: int, body: bytes, url: str = URL) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "reason", {}, io.BytesIO(body))


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def test_a_json_body_is_parsed_and_the_headers_are_sent(transport):
    transport.answers.append(json.dumps([{"number": 1}]).encode("utf-8"))
    assert pm_http.request_json(URL, headers=_headers(), source="github", operation="list") == [
        {"number": 1}
    ]
    assert transport.sent_headers["authorization"] == f"Bearer {TOKEN}"
    assert transport.timeouts == [pm_http.DEFAULT_TIMEOUT]


def test_an_empty_body_reads_as_none(transport):
    transport.answers.append(b"")
    assert pm_http.request_json(URL, headers={}) is None


def test_a_post_serialises_the_body_and_sets_the_content_type(transport):
    transport.answers.append(b"{}")
    pm_http.request_json(URL, method="post", headers={}, body={"a": 1})
    request = transport.requests[-1]
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"a": 1}
    assert transport.sent_headers["content-type"] == "application/json"


def test_a_non_json_body_is_named_as_such(transport):
    transport.answers.append(b"<html>not json</html>")
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json(URL, headers={}, source="github", operation="list")
    assert "github list failed" in str(caught.value)
    assert "not JSON" in str(caught.value)


def test_an_error_status_names_the_source_the_operation_and_the_code(transport):
    transport.answers.append(_http_error(500, b'{"message": "server exploded"}'))
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json(URL, headers={}, source="github", operation="list issues")
    message = str(caught.value)
    assert "github list issues failed" in message
    assert "HTTP 500" in message
    assert "server exploded" in message


def test_a_401_does_not_leak_the_token(transport):
    """The failing service echoes the credential back; the message must not.

    A scrubbed value is not merely absent — ``***`` is where it was, so the
    message still reads as a message.
    """
    body = json.dumps(
        {
            "message": "Bad credentials",
            "documentation_url": "https://docs.github.com/rest",
            "request_headers": {"Authorization": f"Bearer {TOKEN}"},
            "token": TOKEN,
        }
    ).encode("utf-8")
    transport.answers.append(_http_error(401, body, url=f"{URL}?token={TOKEN}"))
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json(
            f"{URL}?token={TOKEN}", headers=_headers(), source="github", operation="list"
        )
    message = str(caught.value)
    assert TOKEN not in message
    assert "***" in message
    assert "Bad credentials" in message


def test_a_short_header_value_is_not_scrubbed_into_noise(transport):
    """The floor exists so structural values survive: "GET" is not a secret."""
    transport.answers.append(_http_error(400, b"GET is not supported here"))
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json(URL, headers={"Accept": "application/json", "X-Method": "GET"})
    assert "GET is not supported here" in str(caught.value)


def test_a_connection_failure_is_a_provider_error(transport):
    transport.answers.append(urllib.error.URLError(ConnectionRefusedError("refused")))
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json(URL, headers={}, source="github", operation="list")
    assert "cannot reach" in str(caught.value)
    assert "github list failed" in str(caught.value)


def test_a_timed_out_socket_is_a_provider_error(transport):
    transport.answers.append(TimeoutError("timed out"))
    with pytest.raises(ProviderError):
        pm_http.request_json(URL, headers={}, source="github", operation="list")


def test_the_error_snippet_is_bounded(transport):
    transport.answers.append(_http_error(500, b"x" * 200_000))
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json(URL, headers={}, source="github", operation="list")
    assert len(str(caught.value)) < pm_http.MAX_SNIPPET + 200


def test_plaintext_http_is_refused_before_the_socket_opens(transport):
    with pytest.raises(ProviderError) as caught:
        pm_http.request_json("http://api.example.com/issues", headers=_headers())
    assert "https" in str(caught.value)
    assert transport.requests == []


def test_a_relative_url_is_refused_too(transport):
    with pytest.raises(ProviderError):
        pm_http.request_json("/issues", headers={})
    assert transport.requests == []
