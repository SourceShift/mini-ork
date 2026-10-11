"""The one HTTP call the PM adapters make, over the standard library.

Why stdlib rather than ``requests``/``httpx``: this is the third place in
mini-ork that talks to an external service (``dispatch/vault.py``,
``dispatch/cn_client.py``, the MCP registry), all three use ``urllib.request``,
and the runtime dependency list is deliberately two entries long — ``PyYAML``
and ``jsonschema``. A board that adds a dependency to show a list of issues is
not a trade worth making.

Two properties matter more than the plumbing:

* **A failure is a :class:`~mini_ork.pm.provider.ProviderError` naming the
  source and the operation**, because the message is shown to a person. The
  status, the URL without its query and a bounded slice of the body are what
  that person needs; a traceback is not.

* **The credential never appears in that message, however the remote side
  behaves.** A failing API can echo the request back — some do it in the error
  body, some in a ``WWW-Authenticate`` hint — so every value this helper *sent*
  as a header is scrubbed out of whatever text it is about to raise, including
  the longer words inside a ``Bearer <token>`` value. Scrubbing is a guard
  against a leak that would otherwise depend on a remote service's error
  formatting, which is not something a caller can control.

``https`` only. The helper exists to carry credentials, and there is no PM
source in this project reachable over plaintext; a ``http://`` URL is refused
before the socket opens rather than being sent and hoped about.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from mini_ork.pm.provider import ProviderError

#: One request's timeout. Long enough for a slow API, short enough that a hung
#: socket does not hold a board open.
DEFAULT_TIMEOUT = 30.0

#: How much of a failing response's body goes into the error message. Enough to
#: read the API's own words (GitHub's ``Bad credentials`` is 40 bytes), short
#: enough that a 4 KiB HTML error page cannot fill a terminal.
MAX_SNIPPET = 4096

#: A header value — or a word inside one — at least this long is treated as a
#: credential and scrubbed from error text. The floor keeps short, structural
#: values ("GET", "application/json") from being replaced by ``***``.
MIN_SECRET = 8

_WORD_BREAK = re.compile(r"[\s,;]+")


def _credential_shaped(headers: Mapping[str, str]) -> list[str]:
    """Every header value worth scrubbing, longest first.

    Both the whole value and the words inside it, because ``Bearer ghp_x``
    leaks as ``ghp_x`` the moment a service echoes only the token. Longest
    first so replacing a short secret cannot leave a longer one half-visible.
    """
    found: set[str] = set()
    for value in headers.values():
        text = str(value).strip()
        if len(text) >= MIN_SECRET:
            found.add(text)
        for word in _WORD_BREAK.split(text):
            if len(word) >= MIN_SECRET:
                found.add(word)
    return sorted(found, key=len, reverse=True)


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _failure(source: str, operation: str, detail: str, secrets: list[str]) -> ProviderError:
    where = " ".join(part for part in (source, operation) if part) or "pm request"
    return ProviderError(f"{where} failed: {_scrub(detail, secrets)}")


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    source: str = "",
    operation: str = "",
) -> Any:
    """One request, parsed as JSON. ``None`` for an empty body (HTTP 204).

    ``body`` is any JSON-serialisable value and is only sent when it is not
    ``None``, so a GET carries no entity. Raises
    :class:`~mini_ork.pm.provider.ProviderError` on a transport failure, a
    non-2xx status, or a body that is not JSON.
    """
    sent = {str(key): str(value) for key, value in (headers or {}).items()}
    secrets = _credential_shaped(sent)

    scheme = urllib.parse.urlsplit(url).scheme
    if scheme != "https":
        raise _failure(
            source, operation,
            f"refusing to send a credentialed request to a {scheme or 'relative'} URL "
            f"({_scrub(url, secrets)}); pm sources are https",
            secrets,
        )

    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        sent.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, method=method.upper(), headers=sent)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        # HTTPError is itself the response, so the API's explanation is still
        # readable here — and it is the most useful thing in the message.
        try:
            snippet = exc.read(MAX_SNIPPET).decode("utf-8", "replace").strip()
        except OSError:
            snippet = ""
        raise _failure(
            source, operation,
            f"HTTP {exc.code} from {_scrub(url, secrets)}: {snippet or '(no body)'}",
            secrets,
        ) from exc
    except urllib.error.URLError as exc:
        raise _failure(
            source, operation, f"cannot reach {_scrub(url, secrets)}: {exc.reason}", secrets
        ) from exc
    except OSError as exc:
        # TimeoutError is an OSError subclass, so a hung socket lands here too.
        raise _failure(source, operation, f"{_scrub(url, secrets)}: {exc}", secrets) from exc

    text = payload.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _failure(
            source, operation,
            f"{_scrub(url, secrets)} returned a body that is not JSON: {exc}",
            secrets,
        ) from exc
