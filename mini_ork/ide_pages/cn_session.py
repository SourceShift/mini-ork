"""``cn_session`` — one ContextNest session, rendered for the IDE.

A single page summarises one ContextNest session:

* a header with goal / project / last activity and a Run button when a
  trajectory record's ``metadata.project_cwd`` lies under
  ``.mini-ork/runs/<run-id>`` with a canonical ``run-<digits>-<short>``
  shape (so the operator jumps from the session to its run tab in one
  click);
* an Open tasks list from ``summary.summary.tasks`` (completed / deleted
  rows dropped — those are history, not work for the operator);
* a markdown summary block from ``GET /api/v1/sessions/:id/summary``
  (``summary.summary.goal`` / ``current_state`` / ``progress`` /
  ``domain`` / ``last_ts``);
* the trajectory from ``GET /api/v1/sessions/:id/trajectory`` — a
  flat ``records[]`` with ``kind`` values ``decision_made`` /
  ``assumption`` / ``failure`` / ``verification`` / ``risk_flag`` /
  ``artifact``, grouped by kind, newest first, capped at 10 per group
  with a "showing N of M" header;
* delivered features from ``GET /api/v1/features``, scoped to a
  ``since=<days>`` window computed from ``summary.summary.started_at``
  (the endpoint takes no server-side session filter, so the page
  filters ``session_id == id`` client-side); each feature carries a
  Re-test button that seeds a ``threads send`` message and never
  executes ``how_to_test``;
* a files-touched list that is the deduplicated union of ``files`` on
  each feature and ``metadata.path`` on ``artifact``-kind records,
  with in-project entries resolved against ``summary.summary.domain`` /
  the project root and opened via ``S.open_path``.

Three endpoints only. The raw ``/api/v1/sessions/:id`` dump takes 12–20 s
on large sessions (kickoff c:0), so this page never asks for it. The
new ``/api/v1/features`` call has its OWN cache section so a slow CN
cannot poison the summary or trajectory slots. Each endpoint goes
through ``mini_ork.ide_pages._cn.render_cached_section`` so a down /
disabled / mis-configured ContextNest still renders a one-line state
and the page stays ``ok: true``. With no ``id`` argument the page
renders an empty-state callout — the IDE actions sweep builds every
page with empty args, and an id-required page would fail it.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork import cn_client
from mini_ork.ide_pages import _cn as CN
from mini_ork.ide_pages import spec as S

# Cap per-group counts so a long-running session stays a page a person reads.
_TRAJ_GROUP_LIMIT = 10
_TRAJ_CONTENT_LIMIT = 300
_FEATURE_LIMIT = 30
_FILES_LIMIT = 30

# Each (section, scope) pair caches independently so a slow trajectory call
# never poisons the summary slot and vice-versa.
_SECTION_SUMMARY = "cn-session-summary"
_SECTION_TRAJECTORY = "cn-session-trajectory"
_SECTION_FEATURES = "cn-session-features"

# ``run-<digits>-<short>`` only — see ``cn_client.extract_run_id``. The page
# extracts from a trajectory record's ``metadata.project_cwd`` lying under
# ``.mini-ork/runs/<id>``, so the path must also match that convention.
_RUN_ID_RE = re.compile(r"^run-\d{6,}-[A-Za-z0-9]{4,}$")
_RUN_PATH_RE = re.compile(
    r"(?:^|/)" + r"\.mini-ork/runs/" + r"(run-\d{6,}-[A-Za-z0-9]{4,})/?$"
)

# Trajectory ``kind`` → display label. The order is the order the page shows
# the groups (decision activity first; the artifacts the run produced last).
_TRAJ_KINDS: tuple[tuple[str, str], ...] = (
    ("decision_made", "Decisions"),
    ("assumption", "Assumptions"),
    ("failure", "Failures"),
    ("verification", "Verifications"),
    ("risk_flag", "Risk flags"),
    ("artifact", "Artifacts"),
)

# Tasks in these statuses are open work, not history. The CN wires these
# names through; the surrounding strings are matched case-insensitively.
_OPEN_TASK_STATUSES = frozenset({
    "open", "todo", "pending", "in_progress", "in-progress", "running",
    "blocked", "needs_you", "needs-review", "needs_review",
})


# ── data fetch ─────────────────────────────────────────────────────────────


def _enc(session_id: str) -> str:
    return urllib.parse.quote(session_id, safe="")


def _fetch_summary(session_id: str) -> dict:
    """``GET /api/v1/sessions/:id/summary`` → ``dict``.

    The endpoint is small (header facts only); the raw ``/sessions/:id``
    dump is the slow one this page deliberately avoids (c:0). ``cn_client._get``
    is the existing fail-soft helper — on any network error it returns
    ``"{}"`` so :func:`cn_client.cached_section` falls through to the cache
    or the unreachable one-line state.
    """
    path = f"/api/v1/sessions/{_enc(session_id)}/summary"
    raw = cn_client._get(path)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_trajectory(session_id: str) -> dict:
    """``GET /api/v1/sessions/:id/trajectory`` → ``dict``."""
    path = f"/api/v1/sessions/{_enc(session_id)}/trajectory"
    raw = cn_client._get(path)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_features(session_id: str, since: str) -> dict:
    """``GET /api/v1/features?since=<since>`` → ``dict``.

    The endpoint has no server-side session filter — every page in this repo
    that lists features goes through it. The page then filters
    ``session_id == id`` client-side, so this stub only needs to honour the
    ``since`` window. No project/layer/format — those widen the window, and
    ``project`` in particular would defeat a different session's project
    boundary. The wider the window, the more other sessions leak in; the
    client-side filter keeps only this session's rows.
    """
    del session_id
    path = f"/api/v1/features?since={urllib.parse.quote(since, safe='')}"
    raw = cn_client._get(path)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


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

    A flat shape (older CN, or a stub) is returned verbatim so the page is
    robust to either schema — the verifier fixtures use the flat shape.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("summary")
    if isinstance(inner, dict):
        return inner
    return payload


def _started_at_epoch(inner: dict[str, Any]) -> int | None:
    """``started_at`` epoch, or ``None`` when the field is missing/malformed.

    The kickoff asks the page to "cover started_at -> now". When
    ``started_at`` is missing we deliberately return ``None`` rather
    than fall back to ``last_ts`` / ``last_activity`` — a recent
    activity timestamp would yield a tiny ``since`` window that
    silently drops this session's older features. The caller widens
    to the 365-day cap on ``None`` instead. A malformed value is
    treated the same as missing.
    """
    raw = inner.get("started_at")
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return int(raw)
        if isinstance(raw, str):
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
    except (ValueError, TypeError):
        return None
    return None


def _since_days(inner: dict[str, Any]) -> str:
    """``"Nd"`` for the ``since`` query — whole days from started_at to now
    plus one, capped at 365 so a session that started years ago cannot
    blow the window to absurd sizes.

    When started_at is missing or malformed, return the cap (``"365d"``)
    so the page never silently drops features on sessions whose start
    date the server did not record. A too-large window is harmless
    (the client-side session filter is the gate); a too-small one
    loses rows.
    """
    started = _started_at_epoch(inner)
    if started is None:
        return "365d"
    days = max(1, (int(time.time()) - started) // 86400 + 1)
    return f"{min(int(days), 365)}d"


def _extract_run_id_from_records(records: list[dict[str, Any]]) -> str:
    """A canonical ``run-<digits>-<short>``, or ``""``.

    The run id is no longer in the summary — it surfaces as the
    ``.mini-ork/runs/<run-id>`` tail of a trajectory record's
    ``metadata.project_cwd``. The page never asks for the raw session dump
    (c:0) so this is the only source of the run id; an Artifact record is
    the most reliable carrier because every run writes one or more of those.
    """
    for record in records:
        if not isinstance(record, dict):
            continue
        meta = record.get("metadata")
        if not isinstance(meta, dict):
            continue
        cwd = meta.get("project_cwd")
        if not isinstance(cwd, str):
            continue
        m = _RUN_PATH_RE.search(cwd)
        if m:
            rid = m.group(1)
            if _RUN_ID_RE.match(rid):
                return rid
    return ""


def _record_content(record: dict[str, Any]) -> str:
    """The visible text of one trajectory record.

    CN carries the body on ``content``; older payloads used ``summary`` /
    ``text``. The cap matches the per-group cap so the rendered payload
    stays small on long sessions.
    """
    for key in ("content", "summary", "text", "title"):
        val = record.get(key)
        if isinstance(val, str):
            text = val.strip().replace("\n", " ")
            if text:
                return text[:_TRAJ_CONTENT_LIMIT]
    return ""


def _record_ts(record: dict[str, Any]) -> int:
    """A timestamp for newest-first sorting; ``0`` when missing sorts last."""
    for key in ("ts", "occurred_at", "created_at", "phase_idx"):
        val = record.get(key)
        if isinstance(val, (int, float)):
            return int(val)
    return 0


def _resolve_path(raw: str, project: str) -> str:
    """Make a path absolute against ``project`` when it isn't already."""
    if not raw:
        return ""
    if raw.startswith("/") or raw.startswith("~"):
        return raw
    if project:
        return str(Path(project) / raw)
    return raw


def _session_features(payload: dict[str, Any], session_id: str, *,
                     limit: int | None = None) -> list[dict[str, Any]]:
    """Features from ``payload`` whose ``session_id`` matches this session.

    The ``/api/v1/features`` endpoint has no server-side session filter,
    so the page filters ``session_id == id`` client-side. The filter
    runs BEFORE any slice: the response is not guaranteed to be ordered
    by session (a 365-day ``since`` window can put hundreds of other
    sessions' rows ahead of this one's), and slicing the raw response
    first would silently drop this session's delivered features.

    Both the "Delivered features" renderer and the "Files touched"
    path collector go through this helper, so the two sections see
    the same set of this-session features and the slice bug cannot
    recur in one place without the other.
    """
    if not isinstance(payload, dict) or _is_error(payload):
        return []
    features = (
        payload.get("features")
        or payload.get("items")
        or payload.get("records")
        or []
    )
    if not isinstance(features, list):
        return []
    matched = [
        f for f in features
        if isinstance(f, dict) and _str(f.get("session_id")) == session_id
    ]
    if limit is None:
        return matched
    return matched[:limit]


# ── section builders ──────────────────────────────────────────────────────


def _summary_md(inner: dict[str, Any]) -> dict[str, Any]:
    """The summary block: goal / current state / progress / domain as markdown."""
    goal = _str(inner.get("goal")) or "(no goal recorded)"
    current_state = _str(inner.get("current_state"))
    progress = _str(inner.get("progress"))
    domain = _str(inner.get("domain"))
    last = _str(inner.get("last_ts") or inner.get("last_activity"))
    lines = [f"**Goal** — {goal}"]
    if current_state:
        lines.append(f"**Current state** — {current_state}")
    if progress:
        lines.append(f"**Progress** — {progress}")
    if domain:
        lines.append(f"**Domain** — `{domain}`")
    if last:
        lines.append(f"**Last activity** — {last}")
    return S.markdown("Summary", "\n\n".join(lines) + "\n")


def _open_tasks(inner: dict[str, Any]) -> list[dict[str, Any]]:
    """Open tasks — completed / deleted rows are dropped, not shown as 0.

    CN ships tasks as ``[{id, status, subject}]``; older payloads may
    flat-out omit ``status``. A missing status is treated as open because
    the absence of a closed state is the canonical "still in flight".
    """
    tasks = inner.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return [S.lst("Open tasks",
                      [S.dot("No open tasks recorded")], full=True)]

    items: list[dict[str, Any]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        status = _str(t.get("status")).lower()
        if status and status not in _OPEN_TASK_STATUSES:
            continue
        subject = _str(t.get("subject") or t.get("title") or t.get("name"))
        tid = _str(t.get("id"))
        sub = tid if tid else ""
        if not subject:
            subject = tid or "(unnamed task)"
        items.append(S.ok(subject, sub))
    if not items:
        return [S.lst("Open tasks",
                      [S.dot("No open tasks recorded")], full=True)]
    return [S.lst("Open tasks", items, full=True,
                  note="Completed and deleted tasks are dropped.")]


def _trajectory(session_id: str) -> list[dict[str, Any]]:
    """Trajectory groups: one row per group, cap 10, "showing N of M" header."""
    payload = CN.render_cached_section(
        _SECTION_TRAJECTORY, session_id, lambda: _fetch_trajectory(session_id)
    )
    if _is_error(payload):
        return [S.lst("Trajectory", [CN.section_status(payload)], full=True)]

    records = payload.get("records")
    if not isinstance(records, list):
        records = []

    rows_by_kind: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = _str(record.get("kind")).lower()
        if not kind:
            continue
        rows_by_kind.setdefault(kind, []).append(record)

    items: list[dict[str, Any]] = []
    for kind, label in _TRAJ_KINDS:
        rows = sorted(rows_by_kind.get(kind, []),
                      key=_record_ts, reverse=True)
        total = len(rows)
        if total == 0:
            items.append(S.dot(f"{label} · 0", "no entries"))
            continue
        shown = rows[:_TRAJ_GROUP_LIMIT]
        first = _record_content(shown[0]) or "(no content)"
        header = f"{label} · showing {len(shown)} of {total}"
        items.append(S.ok(header, first))
    return [S.lst("Trajectory", items, full=True,
                  note="Newest first. Each group is capped to keep the page readable.")]


def _features(session_id: str, inner: dict[str, Any]) -> list[dict[str, Any]]:
    """Delivered features: client-side session-id filter + Re-test seed.

    The Re-test button seeds a ``threads send`` message and never
    executes ``how_to_test`` itself (rule 4). ``how_to_test`` rides the
    message body so a human can review it before the run starts. A
    feature without ``how_to_test`` still gets the Re-test button — the
    seed text just stays shorter.
    """
    since = _since_days(inner)
    payload = CN.render_cached_section(
        _SECTION_FEATURES, f"{session_id}__since={since}",
        lambda: _fetch_features(session_id, since)
    )
    if _is_error(payload):
        return [S.lst("Delivered features", [CN.section_status(payload)], full=True)]

    feats = _session_features(payload, session_id, limit=_FEATURE_LIMIT)
    if not feats:
        return [S.lst("Delivered features",
                      [S.dot("No features delivered yet for this session")], full=True)]

    items: list[dict[str, Any]] = []
    for f in feats:
        name = _str(f.get("name") or f.get("feature") or f.get("label"))
        if not name:
            continue
        how = _str(f.get("how_to_test"))
        seed = (
            f"Re-test the **{name}** feature.\n\n"
            f"How to test:\n\n{how}\n"
            if how
            else f"Re-test the **{name}** feature."
        )
        items.append(
            S.ok(name, how or "(no how_to_test recorded)",
                 acts=[S.btn("Re-test", S.cli(
                     "threads", "send", session_id,
                     "--text", seed,
                     "--recipe", "code-fix",
                 ), "default")])
        )
    if not items:
        return [S.lst("Delivered features",
                      [S.dot("No features delivered yet for this session")], full=True)]
    return [S.lst("Delivered features", items, full=True,
                  note="Re-test seeds a thread with the how_to_test text — the test "
                       "is never run from this page.")]


def _files(project: str, feature_paths: list[str],
           artifact_paths: list[str]) -> list[dict[str, Any]]:
    """Files-touched: deduplicated union of feature files and artifact paths.

    Features carry ``files`` (whatever the run touched); ``artifact``-kind
    trajectory records carry ``metadata.path``. The page surfaces both,
    deduplicated by absolute path so the same file doesn't render twice
    across the two sources.
    """
    files: list[str] = []
    seen: set[str] = set()
    for raw in (*feature_paths, *artifact_paths):
        if not raw:
            continue
        full = _resolve_path(raw, project)
        if full and full not in seen:
            seen.add(full)
            files.append(full)

    if not files:
        return [S.lst("Files touched",
                      [S.dot("No files recorded")], full=True)]

    items: list[dict[str, Any]] = []
    for full in files[:_FILES_LIMIT]:
        items.append(S.dot(full, "", acts=[S.btn("Open", S.open_path(full), "ghost")]))
    return [S.lst("Files touched", items, full=True,
                  note="Relative paths resolve against the session's project.")]


# ── page entry ────────────────────────────────────────────────────────────


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    del home, tab
    session_id = _str(args.get("id") or args.get("session_id"))
    if not session_id:
        # No id → empty state. The IDE actions sweep builds every page with
        # ``{}`` for a key other than ``run``; an id-required page would fail
        # ``test_every_page_builds_with_ok_true``. This state carries no
        # ``cli`` actions so the parser walk also passes.
        return S.page(
            "cn_session",
            "CN session",
            "One ContextNest session, end-to-end.",
            chips_=[S.chip("ContextNest")],
            args=args,
            sections=[S.callout(
                "No session id",
                "Open this page with `cn_session id=<session-id>` — the URL the "
                "inbox and the by-file lists copy from.",
                tone="sub",
            )],
            errors={},
        )

    errors: dict[str, str] = {}

    # Summary first: the header reads goal / project / run-id from it, and
    # ``render_cached_section`` caches under (section, scope), so the per-
    # section call below hits the cache rather than re-hitting CN.
    summary_payload = CN.render_cached_section(
        _SECTION_SUMMARY, session_id, lambda: _fetch_summary(session_id)
    )

    if _is_error(summary_payload):
        # CN is unreachable / disabled / mis-configured: the whole page is
        # the one-line state. The page must still be ``ok: true`` (kickoff
        # acceptance), so we wrap the status as a single list section rather
        # than returning ``ok: false``.
        return S.page(
            "cn_session",
            "CN session",
            session_id,
            chips_=[S.chip("ContextNest", "sub")],
            args=args,
            sections=[S.lst("ContextNest", [CN.section_status(summary_payload)],
                            full=True)],
            errors={},
        )

    inner = _unpack_summary(summary_payload)
    goal = _str(inner.get("goal"))
    domain = _str(inner.get("domain"))
    last = _str(inner.get("last_ts") or inner.get("last_activity"))
    project = domain  # ``domain`` is the project root for path resolution

    sub_parts = [p for p in (project, f"last activity {last}" if last else "") if p]
    sub = " · ".join(sub_parts) if sub_parts else session_id

    # Run id comes from trajectory records — not the summary. Fetch it
    # AFTER the summary, so a slow trajectory fetch still shows the
    # summary card first.
    trajectory_payload = CN.render_cached_section(
        _SECTION_TRAJECTORY, session_id, lambda: _fetch_trajectory(session_id)
    )
    records: list[dict[str, Any]] = []
    if isinstance(trajectory_payload, dict) and not _is_error(trajectory_payload):
        rec = trajectory_payload.get("records")
        if isinstance(rec, list):
            records = [r for r in rec if isinstance(r, dict)]
    run_id = _extract_run_id_from_records(records)

    # ``artifact``-kind records carry a single ``metadata.path`` each;
    # collect every one into the files list (dedup happens in :func:`_files`).
    artifact_paths: list[str] = []
    for record in records:
        if _str(record.get("kind")).lower() != "artifact":
            continue
        meta = record.get("metadata")
        if not isinstance(meta, dict):
            continue
        path = _str(meta.get("path") or meta.get("file"))
        if path:
            artifact_paths.append(path)

    actions: list[dict[str, Any]] = []
    if run_id:
        actions.append(S.btn("Run", S.open_run(run_id, goal or session_id), "primary"))

    # Files-touched pulls the artifact record paths AND the feature files;
    # the feature fetch happens in :func:`_features`, so we re-read the
    # path list here from the cache slot — no second HTTP call. The
    # session-id filter is the same one ``_features`` uses, so the
    # "Files touched" list can never contain a path for a feature the
    # "Delivered features" section drops (and vice versa).
    since = _since_days(inner)
    feature_payload = CN.render_cached_section(
        _SECTION_FEATURES, f"{session_id}__since={since}",
        lambda: _fetch_features(session_id, since)
    )
    feature_paths: list[str] = []
    for f in _session_features(feature_payload, session_id):
        files_field = f.get("files")
        if isinstance(files_field, list):
            for entry in files_field:
                if isinstance(entry, str):
                    feature_paths.append(entry)
                elif isinstance(entry, dict):
                    feature_paths.extend(
                        str(v) for v in entry.values() if isinstance(v, str)
                    )

    sections: list[dict[str, Any]] = []
    sections += S.guarded(errors, "Summary", lambda: _summary_md(inner))
    sections += S.guarded(errors, "Open tasks", lambda: _open_tasks(inner))
    sections += S.guarded(errors, "Trajectory", lambda: _trajectory(session_id))
    sections += S.guarded(errors, "Delivered features",
                          lambda: _features(session_id, inner))
    sections += S.guarded(errors, "Files touched",
                          lambda: _files(project, feature_paths, artifact_paths))

    chips: list[dict[str, Any]] = [S.chip("ContextNest")]
    if run_id:
        chips.append(S.chip(f"run {run_id}", "green"))

    return S.page(
        "cn_session",
        goal or session_id,
        sub,
        chips_=chips,
        actions=actions,
        args=args,
        sections=sections,
        errors=errors,
    )
