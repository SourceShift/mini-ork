"""``cn_session`` — one ContextNest session, rendered for the IDE.

A single page summarises one ContextNest session:

* a header with goal / project / last-activity and a Run button when the
  session carries a ``run-<digits>-<short>`` id (so the operator jumps from
  the session to its run tab in one click);
* a markdown summary block from ``/api/v1/sessions/:id/summary``;
* the trajectory from ``/api/v1/sessions/:id/trajectory`` grouped into
  decisions / assumptions / failures / verifications / open user actions;
* delivered features, with ``how_to_test`` shown as **text only** — never
  executed — plus a Re-test button that seeds a ``threads send`` message;
* a files-touched list that opens in-project files via ``S.open_path``.

Two endpoints only. The raw ``/api/v1/sessions/:id`` dump takes 12–20 s on
large sessions (kickoff c:0), so this page never asks for it. Both
endpoints go through ``mini_ork.ide_pages._cn.render_cached_section`` so a
down / disabled / mis-configured ContextNest still renders a one-line
state and the page stays ``ok: true``. With no ``id`` argument the page
renders an empty-state callout — the IDE actions sweep builds every page
with empty args, and an id-required page would fail it.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from mini_ork import cn_client
from mini_ork.ide_pages import _cn as CN
from mini_ork.ide_pages import spec as S

# Cap per-group counts so a long-running session stays a page a person reads.
_TRAJ_GROUP_LIMIT = 20
_FEATURE_LIMIT = 30
_FILES_LIMIT = 30

# Each (section, scope) pair caches independently so a slow trajectory call
# never poisons the summary slot and vice-versa.
_SECTION_SUMMARY = "cn-session-summary"
_SECTION_TRAJECTORY = "cn-session-trajectory"

# ``run-<digits>-<short>`` only — see ``cn_client.extract_run_id``.
_RUN_ID_RE = re.compile(r"^run-\d{6,}-[A-Za-z0-9]{4,}$")

# Trajectory group keys the page knows about, paired with the label shown in
# the section. ``open_user_actions`` is the last one because it always reads
# as "what you need to do next".
_TRAJ_GROUPS: tuple[tuple[str, str], ...] = (
    ("decisions", "Decisions"),
    ("assumptions", "Assumptions"),
    ("failures", "Failures"),
    ("verifications", "Verifications"),
    ("open_user_actions", "Open user actions"),
)


# ── data fetch ─────────────────────────────────────────────────────────────


def _fetch_summary(session_id: str) -> dict:
    """``GET /api/v1/sessions/:id/summary`` → ``dict``.

    The endpoint is small (header facts only); the raw ``/sessions/:id`` dump
    is the slow one this page deliberately avoids (c:0). ``cn_client._get``
    is the existing fail-soft helper — on any network error it returns
    ``"{}"`` so :func:`cn_client.cached_section` falls through to the cache
    or the unreachable one-line state.
    """
    path = f"/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/summary"
    raw = cn_client._get(path)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_trajectory(session_id: str) -> dict:
    """``GET /api/v1/sessions/:id/trajectory`` → ``dict``."""
    path = f"/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}/trajectory"
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


def _extract_run_id(summary: dict[str, Any]) -> str:
    """A canonical ``run-<digits>-<short>``, or ``""``.

    Accepts only the four keys :func:`mini_ork.cn_client.extract_run_id`
    recognises — anything else is dropped so the Run button never points at
    a non-existent run.
    """
    for key in ("run_id", "run", "task_run_id", "trace_id"):
        val = summary.get(key)
        if isinstance(val, str) and _RUN_ID_RE.match(val):
            return val
    return ""


def _first_text(row: Any) -> str:
    """The first non-empty summary/text field of a trajectory row."""
    if not isinstance(row, dict):
        return ""
    for key in ("summary", "text", "title", "description", "action"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:160]
    return ""


def _resolve_path(raw: str, project: str) -> str:
    """Make a path absolute against ``project`` when it isn't already."""
    if not raw:
        return ""
    if raw.startswith("/") or raw.startswith("~"):
        return raw
    if project:
        return str(Path(project) / raw)
    return raw


# ── section builders ──────────────────────────────────────────────────────


def _summary_md(summary: dict[str, Any]) -> dict[str, Any]:
    """The summary block: goal / project / last activity as markdown."""
    goal = _str(summary.get("goal")) or "(no goal recorded)"
    project = _str(summary.get("project"))
    last = _str(
        summary.get("last_activity")
        or summary.get("last_seen")
        or summary.get("updated_at")
    )
    lines = [f"**Goal** — {goal}"]
    if project:
        lines.append(f"**Project** — `{project}`")
    if last:
        lines.append(f"**Last activity** — {last}")
    return S.markdown("Summary", "\n\n".join(lines) + "\n")


def _trajectory(session_id: str) -> list[dict[str, Any]]:
    """Trajectory groups: one row per group with count + first item."""
    payload = CN.render_cached_section(
        _SECTION_TRAJECTORY, session_id, lambda: _fetch_trajectory(session_id)
    )
    if _is_error(payload):
        return [S.lst("Trajectory", [CN.section_status(payload)], full=True)]
    items: list[dict[str, Any]] = []
    for key, label in _TRAJ_GROUPS:
        rows = payload.get(key)
        if not isinstance(rows, list):
            alt = payload.get(f"{key}_items")
            rows = alt if isinstance(alt, list) else []
        rows = [r for r in rows if isinstance(r, dict)]
        rows = rows[:_TRAJ_GROUP_LIMIT]
        if not rows:
            items.append(S.dot(f"{label} · 0", "no entries"))
            continue
        first = _first_text(rows[0]) or "(no summary)"
        extra = f" · +{len(rows) - 1} more" if len(rows) > 1 else ""
        items.append(S.ok(f"{label} · {len(rows)}", first + extra))
    return [S.lst("Trajectory", items, full=True,
                  note="Counts only — the full timeline lives in ContextNest.")]


def _features(session_id: str, summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Delivered features: ``how_to_test`` as plain text + Re-test thread seed.

    The Re-test button seeds a ``threads send`` message and never executes
    ``how_to_test`` itself (rule 4). ``how_to_test`` rides the message body
    so a human can review it before the run starts.
    """
    features = summary.get("delivered_features") or summary.get("features") or []
    if not isinstance(features, list) or not features:
        return [S.lst("Delivered features",
                      [S.dot("No features delivered yet")], full=True)]

    items: list[dict[str, Any]] = []
    for f in features[:_FEATURE_LIMIT]:
        if not isinstance(f, dict):
            continue
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
    return [S.lst("Delivered features", items, full=True,
                  note="Re-test seeds a thread with the how_to_test text — the test "
                       "is never run from this page.")]


def _files(summary: dict[str, Any], project: str) -> list[dict[str, Any]]:
    """Files-touched: one row per path with an ``S.open_path`` button."""
    raw_files = summary.get("files_touched") or summary.get("files") or []
    if not isinstance(raw_files, list) or not raw_files:
        return [S.lst("Files touched",
                      [S.dot("No files recorded")], full=True)]

    items: list[dict[str, Any]] = []
    for entry in raw_files[:_FILES_LIMIT]:
        if isinstance(entry, dict):
            path = _str(entry.get("path") or entry.get("file"))
        else:
            path = _str(entry)
        if not path:
            continue
        full = _resolve_path(path, project)
        items.append(S.dot(path, "", acts=[S.btn("Open", S.open_path(full), "ghost")]))
    return [S.lst("Files touched", items, full=True,
                  note="Relative paths resolve against the session's project.")]


# ── page entry ────────────────────────────────────────────────────────────


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    del home
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

    summary = summary_payload
    goal = _str(summary.get("goal"))
    project = _str(summary.get("project"))
    last = _str(
        summary.get("last_activity")
        or summary.get("last_seen")
        or summary.get("updated_at")
    )
    run_id = _extract_run_id(summary)

    sub_parts = [p for p in (project, f"last activity {last}" if last else "") if p]
    sub = " · ".join(sub_parts) if sub_parts else session_id

    actions: list[dict[str, Any]] = []
    if run_id:
        actions.append(S.btn("Run", S.open_run(run_id, goal or session_id), "primary"))

    sections: list[dict[str, Any]] = []
    sections += S.guarded(errors, "Summary", lambda: _summary_md(summary))
    sections += S.guarded(errors, "Trajectory", lambda: _trajectory(session_id))
    sections += S.guarded(errors, "Delivered features",
                          lambda: _features(session_id, summary))
    sections += S.guarded(errors, "Files touched",
                          lambda: _files(summary, project))

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