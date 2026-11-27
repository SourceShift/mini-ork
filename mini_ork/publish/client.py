"""Upload a public run bundle to the mini-ork control plane.

The wire contract is intentionally dull: one JSON POST, bearer-style API key in
``x-api-key``, and a response that names the public slug. The consumer
(``POST /api/v1/public-runs``) re-validates the bundle, re-scans it for
secrets, and refuses rather than silently trusting the producer.

Two transports exist because a self-hosted console may expose the API directly
while the hosted one only exposes the Next.js app:

* ``api_url`` pointing at the backend (``https://api.example/api/v1``) posts
  straight to ``/public-runs``;
* the default (``https://console.miniork.dev``) posts to
  ``/api/public/runs``, a thin BFF route that forwards the key server-side.

Both carry the same body; only the path differs.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

__all__ = ["PublishError", "resolve_publish_url", "publish"]

DEFAULT_BASE = "https://console.miniork.dev"
_TIMEOUT_S = 120


class PublishError(RuntimeError):
    """A publish that did not produce a slug. The message is safe to print."""

    def __init__(self, message: str, *, status: int | None = None,
                 body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def resolve_publish_url(base: str) -> str:
    """Turn a user-supplied base URL into the ingest endpoint."""
    base = (base or DEFAULT_BASE).rstrip("/")
    if base.endswith("/api/v1"):
        return base + "/public-runs"
    if base.endswith("/api/v1/public-runs"):
        return base
    return base + "/api/public/runs"


def publish(
    bundle: dict[str, Any],
    transcripts: dict[str, list[dict[str, Any]]],
    *,
    api_url: str = DEFAULT_BASE,
    api_key: str,
    slug: str | None = None,
    timeout_s: int = _TIMEOUT_S,
) -> dict[str, Any]:
    """POST the bundle; return the server's ``{slug, url, sha256, …}``."""
    if not api_key:
        raise PublishError(
            "no API key: pass --api-key-env VAR (default ATTESTIA_API_KEY) "
            "or --api-key-stdin; the key is never read from a flag argument"
        )
    payload = json.dumps(
        {"bundle": bundle, "transcripts": transcripts, **({"slug": slug} if slug else {})},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        resolve_publish_url(api_url),
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "x-api-key": api_key,
            "user-agent": "mini-ork-publish",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as err:  # 4xx/5xx carry the reason we need
        raw = err.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw.decode("utf-8", "replace")[:500]
        reason = parsed.get("error") if isinstance(parsed, dict) else None
        detail = parsed.get("message") if isinstance(parsed, dict) else None
        message = "publish rejected: " + " ".join(
            str(part) for part in (reason, detail) if part
        )
        raise PublishError(message or f"HTTP {err.code}", status=err.code,
                           body=parsed) from None
    except urllib.error.URLError as err:
        raise PublishError(f"cannot reach {resolve_publish_url(api_url)}: {err.reason}") from None

    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise PublishError("the API returned a non-JSON body", status=status) from None
    if not isinstance(data, dict) or not data.get("slug"):
        raise PublishError("the API returned no slug", status=status, body=data)
    return data
