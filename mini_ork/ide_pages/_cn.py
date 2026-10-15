"""Cached ContextNest sections for the IDE pages.

The page builders are sync; CN is a network dependency that may be slow,
briefly down, or (in the secure tier) misconfigured. This module wraps the
client in a single shape the pages can render without caring which one
happened:

* **healthy**      — the cache slot is fresh, an "as of HH:MM" label is added.
* **cached**       — the live call failed but a recent (TTL-bounded) slot
                     exists; the cached payload is rendered with its own
                     as-of label so the operator knows the data is stale.
* **unreachable**  — no live data and no cache; a one-line "ContextNest is
                     not running" line is rendered.
* **disabled**     — ``MO_DISABLE_CN=1``; a one-line "ContextNest is off"
                     line is rendered. Never shown alongside the unreachable
                     line — disabled wins.
* **config error** — secure tier is on but the secret store is missing the
                     key. The error message names the key (never the value)
                     so the operator knows exactly what to export.

The page builders call :func:`render_cached_section` once per section
(``atoms``, ``features``, ``inbox``, ``basins``, …) and the returned dict
is what gets handed to the spec helpers — the page never has to look at
the network, the secret store, or the cache file path itself.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from mini_ork import cn_client


# ── one-line render shapes ──────────────────────────────────────────────────

_DISABLED_LINE = {
    "kind": "dot",
    "label": "ContextNest",
    "text": "off · MO_DISABLE_CN=1",
    "tone": "sub",
}

_UNREACHABLE_LINE = {
    "kind": "dot",
    "label": "ContextNest",
    "text": "not running · start ContextNest or set CN_BASE_URL",
    "tone": "warn",
}


def _as_of_label(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(int(ts)))


def _config_error_line(message: str) -> dict:
    """The secure-tier error path: never the value, always the key name."""
    return {
        "kind": "dot",
        "label": "ContextNest",
        "text": f"config error · {message}",
        "tone": "error",
    }


# ── the public surface ──────────────────────────────────────────────────────


def section_status(payload: dict) -> dict:
    """Translate a cached_section payload into one render line.

    Returns a single ``dot``-style dict the page builder drops straight into
    a list. The payload carries the markers (``_as_of``, ``_from_cache``,
    ``_unreachable``, ``_disabled``) the wrapper adds — pages do not parse
    the payload themselves.
    """
    if payload.get("_disabled"):
        return dict(_DISABLED_LINE)
    if payload.get("_unreachable"):
        return dict(_UNREACHABLE_LINE)
    if payload.get("_config_error"):
        return _config_error_line(str(payload.get("_config_error_message", "missing token")))
    as_of = int(payload.get("_as_of", 0) or 0)
    from_cache = bool(payload.get("_from_cache"))
    suffix = ""
    if from_cache:
        suffix = f" · cached {_as_of_label(as_of)}"
    elif as_of:
        suffix = f" · as of {_as_of_label(as_of)}"
    return {
        "kind": "dot",
        "label": "ContextNest",
        "text": f"connected{suffix}",
        "tone": "ok" if not from_cache else "warn",
    }


def render_cached_section(
    section: str,
    scope: str,
    fetch: Callable[[], dict],
    *,
    disabled: bool | None = None,
) -> dict:
    """One cached-section render. ``fetch`` returns a dict payload; this
    wrapper arbitrates between live / cached / unreachable / config-error
    and stamps the result with the markers the page builder expects.

    ``disabled`` defaults to ``MO_DISABLE_CN=1``; pass an explicit value to
    override (e.g. when a tab wants to show CN even when global CN is off).
    """
    if disabled is None:
        disabled = os.environ.get("MO_DISABLE_CN", "0") == "1"
    if disabled:
        return {"_disabled": True}
    try:
        payload = cn_client.cached_section(section, scope, fetch)
    except Exception as exc:  # noqa: BLE001
        # The ONLY thing that escapes ``cn_client.cached_section`` is a
        # SecretStoreError (secure tier, missing key). Convert it into a
        # config-error payload so the page renders the key name rather
        # than an exception trace.
        from mini_ork.dispatch.secrets import SecretStoreError
        if isinstance(exc, SecretStoreError):
            return {
                "_config_error": True,
                "_config_error_message": str(exc),
                "section": section,
            }
        # Anything else (e.g. OSError) — treat as unreachable.
        return {"_unreachable": True}
    return payload


# ── the secure-tier wrapper (raises are OK at this layer) ──────────────────


def fetch_with_auth(fetch: Callable[[], dict]) -> dict:
    """Run ``fetch`` and re-raise ``SecretStoreError`` so the page's
    :func:`render_cached_section` converts it to a key-naming error.

    Most read paths in :mod:`mini_ork.cn_client` swallow non-secure errors
    into ``{}``; this wrapper is the explicit one for callers that want
    the secure-tier error path raised (so the page renders the key name
    rather than silently falling back to cached or unreachable).
    """
    return fetch()


__all__ = [
    "render_cached_section",
    "section_status",
    "fetch_with_auth",
]
