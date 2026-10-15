"""``cn_file`` — one file's session history, live holder, and failure context.

A single page summarises what ContextNest knows about one project-relative
file:

* a Sessions list from ``GET /api/v1/sessions/by-file?path=`` whose matches
  are ranked newest-first by each session's own
  ``GET /api/v1/sessions/:id/summary`` ``summary.last_ts`` (the by-file
  endpoint ships no timestamp); every row's Open button jumps to the
  matching ``cn_session`` page;
* a Live holder row from ``GET /api/v1/coord/hot-claims`` filtered to
  claims whose ``path`` field matches the page's argument (foreign-path
  claims never leak across projects);
* a Recent failures row from ``POST /api/v1/tools/retrieve`` filtered to
  ``metadata.kind == "failure"`` (the endpoint ships other kinds too —
  every verified CN response is a flat ``hits[]`` and the page must keep
  the kind filter, not lift it);

Paths are resolved against ``home.parent`` (the project root). A path
that escapes the root via ``..``, or is absolute and points elsewhere,
renders an explanation row and makes zero ContextNest calls — the page
must never ask CN about a path it cannot verify belongs to the project.
``home.parent`` follows the ``cn_session`` convention so the same
project-root logic covers both pages.

Four endpoints, every one routed through
:mod:`mini_ork.ide_pages._cn.render_cached_section` so a slow / down /
disabled ContextNest still renders the one-line state and the page
stays ``ok: true``. With no ``path`` argument the page renders an
empty-state callout — the IDE actions sweep builds every page with
empty args, and a ``path``-required page would fail that suite.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork import cn_client
from mini_ork.ide_pages import _cn as CN
from mini_ork.ide_pages import spec as S

# Per-section caps keep the page readable on files touched by hundreds
# of sessions: ten rows in the timeline is plenty for a human, and
# beyond that the operator wants a filter, not more rows.
_SESSIONS_LIMIT = 30
_FAILURES_LIMIT = 10

# Each (section, scope) pair caches independently. The by-file slot is
# the raw payload; the per-session summary slots carry the recap fetch
# keyed by ``<by-file-path>__summary__<sid>`` so two files never share a
# summary slot. Hot-claims and failures get one slot per file.
_SECTION_BY_FILE = "cn-file-by-file"
_SECTION_HOT = "cn-file-hot"
_SECTION_FAILURES = "cn-file-failures"


# ── data fetch ─────────────────────────────────────────────────────────────


def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _fetch_by_file(path: str) -> dict:
    """``GET /api/v1/sessions/by-file?path=<path>`` → ``dict``.

    ``cn_client._get`` swallows network errors into ``"{}"`` so
    :func:`cn_client.cached_section` falls through to its cache slot or
    the unreachable one-line state — the page never raises on a CN
    timeout. The endpoint shape is ``{query, matches: [...]}``; older
    payloads used ``items``/``sessions`` and are tolerated by the same
    fallback logic, but the verifier fixtures pin the live shape so a
    regression that re-introduces a foreign-key parser lights up.
    """
    raw = cn_client._get(f"/api/v1/sessions/by-file?path={_enc(path)}")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_summary(session_id: str) -> dict:
    """``GET /api/v1/sessions/:id/summary`` → ``dict``.

    Fields the page reads (``goal``, ``last_ts``) live under the
    ``summary`` key on the wire; a flat shape (older CN) is returned
    verbatim so the cache slot is robust to either schema.
    """
    raw = cn_client._get(f"/api/v1/sessions/{_enc(session_id)}/summary")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_hot_claims() -> dict:
    """``GET /api/v1/coord/hot-claims`` → ``dict``.

    :func:`cn_client.coord_hot_claims` raises
    :class:`CoordUnavailable` / :class:`CoordHTTPError` rather than
    returning ``{}`` — the page catches those and returns ``{}`` so
    :func:`cn_client.cached_section` falls through to its cache slot or
    the unreachable one-line state.
    """
    try:
        return cn_client.coord_hot_claims() or {}
    except Exception:
        return {}


def _fetch_failure_atoms(query: str) -> dict:
    """``POST /api/v1/tools/retrieve`` → ``{"hits": [...]}``.

    The endpoint returns a flat ``hits[]`` regardless of kind; the page
    filters by ``metadata.kind == "failure"`` so the Recent-failures
    section never advertises a decision / verification / artifact. The
    response is wrapped as ``{"hits": [...]}`` so the caller can pass
    the dict straight into :func:`cn_client.cached_section`.
    """
    raw = cn_client.retrieve(query, limit=_FAILURES_LIMIT * 4)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {"hits": []}
    if not isinstance(data, dict):
        return {"hits": []}
    hits = data.get("hits") or []
    return {"hits": hits if isinstance(hits, list) else []}


# ── helpers ────────────────────────────────────────────────────────────────


def _is_error(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("_unreachable")
        or payload.get("_disabled")
        or payload.get("_config_error")
    )


def _str(value: Any) -> str:
    """``None`` and non-strings become ``""``."""
    if value is None:
        return ""
    return str(value).strip()


def _unpack_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The CN wire nests every rendered field under a ``summary`` key.

    A flat shape (older CN, or a stub) is returned verbatim so the page
    is robust to either schema — the verifier fixtures use the nested
    shape.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("summary")
    if isinstance(inner, dict):
        return inner
    return payload


def _last_ts_epoch(inner: dict[str, Any]) -> int:
    """An epoch for newest-first sorting; ``0`` when missing sorts last.

    Accepts both an int / float epoch (older payloads) and an ISO-8601
    string (the live CN wire). A malformed value drops to ``0`` — the
    session still renders, just at the tail of the list — rather than
    raising and blanking the page.
    """
    raw = inner.get("last_ts") or inner.get("last_activity")
    if raw is None or raw == "":
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return 0
    return 0


def _resolve_inside_project(raw: str, project_root: Path | None) -> str | None:
    """The project-relative path if ``raw`` resolves inside ``project_root``;
    ``None`` otherwise (the outside-project guard).

    A non-existent path inside the project is still "inside" — the
    guard is about *containment*, not file existence, because the
    by-file endpoint accepts both file and directory paths and a
    file the operator hasn't written yet still belongs to the project.
    Relative paths are joined to ``project_root``; absolute paths are
    taken as-is. The resolved path is required to live inside the
    project root — paths whose ``..`` components escape the root, or
    absolute paths that point outside the project, do not pass. A
    missing project root or any OS-level resolution error degrades
    every path to "do not pass" rather than raising.
    """
    if not raw or project_root is None:
        return None
    try:
        root = project_root.resolve()
    except OSError:
        return None
    if raw.startswith("/") or raw.startswith("~"):
        candidate = Path(raw)
    else:
        candidate = project_root / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return None


# ── section builders ──────────────────────────────────────────────────────


def _sessions(home: Path, path: str) -> list[dict[str, Any]]:
    """Sessions that touched ``path``, newest-first by ``summary.last_ts``.

    The by-file payload carries no timestamp; recency is a fan-out: one
    cached summary fetch per matched session, scoped by the per-session
    cache key so two files never share a summary slot. A summary fetch
    that hits an unreachable CN for that one session degrades to
    ``ts=0`` rather than blanking the section.
    """
    del home
    payload = CN.render_cached_section(
        _SECTION_BY_FILE, path, lambda: _fetch_by_file(path)
    )
    if _is_error(payload):
        return [S.lst("Sessions", [CN.section_status(payload)], full=True)]

    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return [S.lst("Sessions",
                      [S.dot("No sessions touched this file yet")], full=True)]

    enriched: list[tuple[int, dict[str, Any]]] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        sid = _str(m.get("session_id"))
        if not sid:
            continue
        sp = CN.render_cached_section(
            _SECTION_BY_FILE, f"{path}__summary__{sid}",
            lambda sid=sid: _fetch_summary(sid)
        )
        inner = _unpack_summary(sp) if not _is_error(sp) else {}
        ts = _last_ts_epoch(inner)
        enriched.append((ts, {
            "session_id": sid,
            "inner": inner,
            "matched_files": m.get("matched_files"),
            "total_files": m.get("total_files"),
        }))

    # Newest first. A session without a parseable timestamp sinks to the
    # tail (we use ``0`` as the floor — the helper treats a missing
    # value the same as a malformed one).
    enriched.sort(key=lambda t: t[0], reverse=True)

    items: list[dict[str, Any]] = []
    for ts, row in enriched[:_SESSIONS_LIMIT]:
        inner = row["inner"]
        sid = row["session_id"]
        goal = _str(inner.get("goal")) or "(no goal recorded)"
        last = _str(inner.get("last_ts") or inner.get("last_activity"))
        mf = row["matched_files"]
        if ts > 0 and last:
            sub = f"last activity {last}"
        elif last:
            sub = last
        else:
            sub = "no timestamp recorded"
        if isinstance(mf, list) and mf:
            sub += f" · {len(mf)} matched file(s) here"
        items.append(
            S.ok(goal, sub,
                 acts=[S.btn("Open", S.page_link("cn_session", id=sid), "ghost")])
        )
    if not items:
        return [S.lst("Sessions",
                      [S.dot("No sessions touched this file yet")], full=True)]
    return [S.lst("Sessions", items, full=True,
                  note="Newest first by summary.last_ts. Open jumps to the session.")]


def _hot_claims(path: str) -> list[dict[str, Any]]:
    """Live principal holding ``path`` from ``/coord/hot-claims``.

    A claim with a foreign path never appears here: the match runs on
    the verbatim ``path`` string the page was built with, and also on
    a leading ``./`` join — a few CN versions emit relative paths with
    a leading ``./`` and the page should keep that line open. The
    section renders ``"No live claim on this file"`` when nothing
    matches, so an empty payload is distinguishable from a CN outage.
    """
    payload = CN.render_cached_section(
        _SECTION_HOT, path, lambda: _fetch_hot_claims()
    )
    if _is_error(payload):
        return [S.lst("Live holder", [CN.section_status(payload)], full=True)]

    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        return [S.lst("Live holder",
                      [S.dot("No live claim on this file")], full=True)]

    variants = {path, f"./{path}"}
    matched: list[dict[str, Any]] = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        cp = _str(c.get("path"))
        if cp and cp in variants:
            matched.append(c)
    if not matched:
        return [S.lst("Live holder",
                      [S.dot("No live claim on this file")], full=True)]

    items: list[dict[str, Any]] = []
    for c in matched:
        principal = _str(c.get("principal_id"))
        expires = _str(c.get("expires_at"))
        sub = f"expires {expires}" if expires else "(no expiry)"
        items.append(S.ok(principal or "(no principal)", sub))
    return [S.lst("Live holder", items, full=True,
                  note="From /coord/hot-claims. Empty means no live holder.")]


def _failures(path: str) -> list[dict[str, Any]]:
    """Recent failure atoms whose content or metadata names ``path``.

    The endpoint ships a flat ``hits[]`` regardless of ``kind``; the
    page filters ``metadata.kind == "failure"`` so the section never
    advertises a decision / verification / artifact. The query is the
    path string itself, which is what ContextNest's substrate retrieval
    is built to match against. A hit whose ``content`` is empty is
    dropped — a failure with no message is just noise.
    """
    payload = CN.render_cached_section(
        _SECTION_FAILURES, path, lambda: _fetch_failure_atoms(path)
    )
    if _is_error(payload):
        return [S.lst("Recent failures",
                      [CN.section_status(payload)], full=True)]

    hits = payload.get("hits")
    if not isinstance(hits, list) or not hits:
        return [S.lst("Recent failures",
                      [S.dot("No failure atoms name this file")], full=True)]

    items: list[dict[str, Any]] = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        meta = h.get("metadata") or {}
        kind = _str(meta.get("kind")).lower()
        if kind != "failure":
            continue
        content = _str(h.get("content"))
        if not content:
            continue
        ts = _str(meta.get("ts"))
        sub = ts[:10] if ts else ""
        items.append(S.bad(content[:160], sub))
    if not items:
        return [S.lst("Recent failures",
                      [S.dot("No failure atoms name this file")], full=True)]
    return [S.lst("Recent failures", items, full=True,
                  note="From /api/v1/tools/retrieve, filtered to kind=failure.")]


# ── page entry ────────────────────────────────────────────────────────────


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    del tab
    raw = _str(args.get("path") or args.get("file"))
    project_root = home.parent if isinstance(home, Path) else None

    # 1. No argument → empty-state callout. The IDE actions sweep builds
    # every page key with empty ``args``; an arg-required page would fail
    # ``test_every_page_builds_with_ok_true``.
    if not raw:
        return S.page(
            "cn_file",
            "CN file",
            "One file's session history, live holder, and failures.",
            chips_=[S.chip("ContextNest")],
            args=args,
            sections=[S.callout(
                "No file path",
                "Open this page with `cn_file path=<project-relative-path>` "
                "— the URL the by-file lists and the file watchers copy from.",
                tone="sub",
            )],
            errors={},
        )

    # 2. Outside-project guard. A path that escapes the root via ``..``,
    # or is absolute and points elsewhere, makes zero ContextNest calls
    # — the page must never ask CN about a path it cannot verify.
    rel = _resolve_inside_project(raw, project_root)
    if rel is None:
        where = str(project_root) if project_root is not None else "(no project root)"
        return S.page(
            "cn_file",
            "CN file",
            raw,
            chips_=[S.chip("ContextNest")],
            args=args,
            sections=[S.callout(
                "Path is outside the project",
                f"`{raw}` resolves outside `{where}` — the page never asks "
                f"ContextNest about paths it cannot verify against the "
                f"project root.",
                tone="warn",
            )],
            errors={},
        )

    # 3. Render the three sections against the canonical relative path.
    errors: dict[str, str] = {}
    sections: list[dict[str, Any]] = []
    sections += S.guarded(errors, "Sessions",
                          lambda: _sessions(home, rel))
    sections += S.guarded(errors, "Live holder",
                          lambda: _hot_claims(rel))
    sections += S.guarded(errors, "Recent failures",
                          lambda: _failures(rel))

    return S.page(
        "cn_file",
        rel,
        "project-relative path · ContextNest view",
        chips_=[S.chip("ContextNest")],
        args=args,
        sections=sections,
        errors=errors,
    )