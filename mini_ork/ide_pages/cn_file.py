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
* a Recent failures row from ``GET /api/v1/prompt-context/atoms`` filtered
  client-side: the request carries ``kind=failure``, ``since=30d``,
  ``limit=500``, and the project root so CN's atoms endpoint ships a
  bounded window of failure-shaped atoms; the page then keeps the atoms
  whose ``project`` equals the resolved root and whose ``text`` names the
  page's file (the project-relative path, the basename, or the basename
  stem when the stem is at least 6 characters), sorted newest ``ts`` first,
  capped at ten rows with a "Showing N of M" note when capped.

Paths are resolved against ``home.parent`` (the project root). A path
that escapes the root via ``..``, or is absolute and points elsewhere,
renders an explanation row and makes zero ContextNest calls — the page
must never ask CN about a path it cannot verify belongs to the project.
``home.parent`` follows the ``cn_session`` convention so the same
project-root logic covers both pages.

Three endpoints, every one routed through
:mod:`mini_ork.ide_pages._cn.render_cached_section` so a slow / down /
disabled ContextNest still renders the one-line state and the page
stays ``ok: true``. With no ``path`` argument the page renders an
empty-state callout — the IDE actions sweep builds every page with
empty args, and a ``path``-required page would fail that suite.

The atoms endpoint takes ``kind=`` (one value) and ``q=`` is ignored
server-side, so file matching runs entirely on the client: the server's
project filter is treated as an index hint, not a containment
guarantee, and ``limit=500`` may truncate — the page therefore re-checks
``atom.project`` against the resolved root before deciding to render a
row.
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
# The atoms endpoint takes ``kind=`` (one value) and a project filter
# hint; 500 is the smallest round number that comfortably fits a 30-day
# window of failures for one project without truncation on the busy
# files we have observed in production traces.
_ATOMS_REQUEST_LIMIT = 500
_ATOMS_SINCE = "30d"

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


def _fetch_failure_atoms(project_root: str) -> dict:
    """``GET /api/v1/prompt-context/atoms`` → ``dict``.

    The request URL pins ``kind=failure``, ``since=30d``, ``limit=500``
    and the resolved project root — CN's atoms endpoint takes ``kind=``
    as a single value and ignores ``q=``, so file matching runs entirely
    on the client (the request payload is kept narrow on purpose).

    Returns ``{}`` when the wire payload is the ``{}`` fallback
    ``cn_client._get`` writes on a down CN — :func:`cn_client.cached_section`
    treats an empty dict as a miss and falls through to the unreachable
    marker, which is what the section renders as the "not running"
    one-liner. A healthy CN that genuinely answers ``{"atoms": []}``
    keeps that payload verbatim so the page renders the no-match line
    rather than papering the outage over.
    """
    qs = (f"since={_ATOMS_SINCE}&kind=failure"
          f"&project={_enc(project_root)}&limit={_ATOMS_REQUEST_LIMIT}")
    raw = cn_client._get(f"/api/v1/prompt-context/atoms?{qs}")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(data, dict) or not data:
        # Either a non-dict payload or the empty ``{}`` CN writes on a
        # down service. Either way, fall through to the unreachable
        # one-liner rather than paper over it with a no-match row.
        return {}
    return data


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


def _failures(home: Path, path: str) -> list[dict[str, Any]]:
    """Recent failure atoms whose ``text`` names ``path``.

    The atoms request pins ``kind=failure``, ``since=30d``, ``limit=500``
    and the resolved project root; CN's project filter is treated as an
    index hint (the wire payload can include atoms from a sibling
    project when the filter mis-spells or a load spills over), so the
    page re-checks ``atom.project`` against the resolved root and drops
    every foreign-project atom before deciding what to render. File
    matching runs entirely client-side: an atom whose ``text`` contains
    the project-relative path, the basename, or the basename stem (when
    the stem is at least 6 characters) is attributed to this file.
    A short stem like ``init`` would match unrelated atom text and is
    deliberately excluded.

    Atoms are sorted newest ``ts`` first (a malformed timestamp sinks
    to the tail), the list is capped at :data:`_FAILURES_LIMIT` rows,
    and the section note reads "Showing N of M" when the cap truncates.
    Each row carries a ``cn_session`` page link for the atom's
    ``session_id`` so an operator can jump straight to the source
    session.
    """
    project_root_path = home.parent if isinstance(home, Path) else None
    try:
        project_root_str = (project_root_path.resolve().as_posix()
                            if project_root_path is not None else "")
    except OSError:
        project_root_str = ""

    payload = CN.render_cached_section(
        _SECTION_FAILURES, path,
        lambda: _fetch_failure_atoms(project_root_str),
    )
    if _is_error(payload):
        return [S.lst("Recent failures",
                      [CN.section_status(payload)], full=True)]

    atoms = payload.get("atoms")
    if not isinstance(atoms, list) or not atoms:
        return [S.lst("Recent failures",
                      [S.dot("No failures in the last 30 days name this file")],
                      full=True)]

    basename = Path(path).name
    stem = Path(path).stem
    matched: list[dict[str, Any]] = []
    for a in atoms:
        if not isinstance(a, dict):
            continue
        # Re-check the project filter: CN's server-side project filter
        # is an index hint, not a containment guarantee (``limit=500``
        # may also spill into a sibling project). Equal-to-the-root is
        # what counts — the resolved project root is the only string
        # the page trusts.
        if not project_root_str or _str(a.get("project")) != project_root_str:
            continue
        kind = _str(a.get("kind")).lower()
        if kind and kind != "failure":
            continue
        text = _str(a.get("text"))
        if not text:
            continue
        if not _atom_names_file(text, path, basename, stem):
            continue
        matched.append({
            "text": text,
            "ts": _str(a.get("ts")),
            "session_id": _str(a.get("session_id")),
        })
    if not matched:
        return [S.lst("Recent failures",
                      [S.dot("No failures in the last 30 days name this file")],
                      full=True)]

    matched.sort(key=lambda row: _atom_ts_epoch(row["ts"]), reverse=True)
    cap = _FAILURES_LIMIT
    head = matched[:cap]
    items: list[dict[str, Any]] = []
    for row in head:
        ts = row["ts"]
        sub = ts[:10] if ts else ""
        acts = []
        if row["session_id"]:
            acts = [S.btn("Open", S.page_link("cn_session",
                                               id=row["session_id"]),
                          "ghost")]
        items.append(S.bad(row["text"][:160], sub, acts=acts))
    note = ""
    if len(matched) > cap:
        note = f"Showing {cap} of {len(matched)} matching atoms."
    return [S.lst("Recent failures", items, full=True, note=note)]


def _atom_names_file(text: str, rel_path: str, basename: str,
                     stem: str) -> bool:
    """Whether ``text`` is plausibly about ``rel_path``.

    The match runs on verbatim substring containment so atom text is
    displayed exactly as CN stored it — there is no ``eval`` or pattern
    language here, just a substring scan over the project's three name
    shapes. The basename stem is only counted when the stem is at least
    six characters long, so a short stem like ``init`` never silently
    attributes unrelated failures to ``src/init.py``.
    """
    if not text:
        return False
    if rel_path and rel_path in text:
        return True
    if basename and basename in text:
        return True
    if stem and len(stem) >= 6 and stem in text:
        return True
    return False


def _atom_ts_epoch(ts: str) -> int:
    """An epoch for newest-first sorting; ``0`` when missing sorts last.

    Accepts the ISO-8601 string the atoms wire shape carries (``ts``
    under :data:`_ATOMS_SINCE`); a malformed value drops to ``0`` —
    the atom still renders, just at the tail of the list — rather than
    raising and blanking the section.
    """
    if not ts:
        return 0
    try:
        iso = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


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
                          lambda: _failures(home, rel))

    return S.page(
        "cn_file",
        rel,
        "project-relative path · ContextNest view",
        chips_=[S.chip("ContextNest")],
        args=args,
        sections=sections,
        errors=errors,
    )