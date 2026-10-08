"""Inbox — what needs the operator first, across every run of the project.

No tabs. Data: ``mini_ork.acp.fleet.fleet_rows`` (the same read ``runs.py``
uses) supplies the candidate rows; ``outcome.resolve`` turns each into one
decision; ``task_state.landed_report`` and ``repair.json`` drop the runs that
are already done or owned by auto-repair.

One row per run — a full-width ``callout`` carrying the outcome's tone, text
and actions plus an "Open run" button. A row that cannot be read costs its own
row only (``spec.guarded``), never the page.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

# "Runs from the last 7 days" — the inbox's candidate window.
_WINDOW_DAYS = 7
# At most this many rows; the rest collapse into one "N more…" link.
_ROW_CAP = 25
# Only the newest rows get a full outcome card: each one loads the run and
# resolves its outcome (~0.65 s CPU on a large home), and the page must stay
# well inside the host's 30 s per-process budget. The rest are compact rows.
_DETAILED_ROWS = 8
# ``fleet_rows`` clamps ``limit`` to its own ``MAX_LIMIT = 50``.
_FLEET_LIMIT = 50


def _run_mod():
    """The sibling ``run`` page — reached lazily so importing this module stays light."""
    from mini_ork.ide_pages import run as run_mod

    return run_mod


def _outcome_mod():
    from mini_ork.ide_pages import outcome

    return outcome


def _fleet(home: Path, state: str):
    from mini_ork.acp.fleet import fleet_rows

    return fleet_rows(home, state=state, limit=_FLEET_LIMIT)


def _run_dir(home: Path, run_id: str) -> Path:
    return Path(home) / "runs" / run_id


def _landed(home: Path, run_id: str) -> bool:
    """True when the change landed elsewhere (``landed.json``) — the run is done."""
    try:
        from mini_ork.acp.task_state import landed_report

        return landed_report(_run_dir(home, run_id)) is not None
    except Exception:  # noqa: BLE001 — task_state optional: keep the row
        return False


def _repairing(home: Path, run_id: str) -> bool:
    """True when auto-repair owns the run (``repair.json`` state is ``repairing``)."""
    try:
        data = json.loads((_run_dir(home, run_id) / "repair.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and str(data.get("state") or "") == "repairing"


def _in_window(row: Any, cutoff: int) -> bool:
    """A row with a known start inside the window; an unknown start is kept."""
    return row.started_at is None or row.started_at >= cutoff


def _newest_first(rows: list[Any]) -> list[Any]:
    return sorted(rows, key=lambda r: r.started_at or 0, reverse=True)


def _row(home: Path, run_id: str) -> dict[str, Any]:
    """One inbox row: the run's outcome as a full-width callout."""
    run = _run_mod()._load(home, run_id)
    if run is None:
        raise RuntimeError(f"no run {run_id}")
    out = _outcome_mod().resolve(run)
    title = str(run.card.get("title") or run_id)
    text = str(out.get("text") or "")
    detail = str(out.get("detail") or "").strip()
    text_md = f"{text}\n\n{detail}" if detail else text
    actions = list(out.get("actions") or [])
    actions.append(S.btn("Open run", S.open_run(run.id, title), "ghost"))
    return S.callout(title, text_md, tone=str(out.get("tone") or "muted"),
                     actions=actions, full=True)


def _inbox(home: Path, now: int, errors: dict[str, str], ctx: dict[str, Any]
           ) -> list[dict[str, Any]]:
    cutoff = now - _WINDOW_DAYS * 86400
    try:
        needs, _ = _fleet(home, "needs_you")
    except Exception as exc:  # noqa: BLE001 — one fleet read must not blank the page
        errors["inbox"] = f"{type(exc).__name__}: {exc}"
        needs = []
    try:
        failed, _ = _fleet(home, "failed")
    except Exception as exc:  # noqa: BLE001
        errors["inbox failed"] = f"{type(exc).__name__}: {exc}"
        failed = []

    needs = _newest_first(
        [r for r in needs if _in_window(r, cutoff) and not _landed(home, r.run_id)])
    # Auto-repair owns a run in either bucket: it is not waiting on the human.
    repairing = _newest_first(
        [r for r in needs + failed if _in_window(r, cutoff) and _repairing(home, r.run_id)])
    needs = [r for r in needs if not _repairing(home, r.run_id)]
    failed = _newest_first(
        [r for r in failed
         if _in_window(r, cutoff) and not _landed(home, r.run_id) and not _repairing(home, r.run_id)])

    ctx["needs_you"] = len(needs)
    ctx["failed"] = len(failed)
    ctx["repairing"] = len(repairing)

    ordered = needs + failed
    shown = ordered[:_ROW_CAP]
    sections: list[dict[str, Any]] = []
    for row in shown[:_DETAILED_ROWS]:
        sections += S.guarded(errors, row.run_id, lambda rid=row.run_id: [_row(home, rid)])
    compact = shown[_DETAILED_ROWS:]
    if compact:
        sections.append(S.lst("Also waiting", [
            S.dot(r.title or r.run_id,
                  f"{r.recipe} · {'needs you' if r.state == 'needs_you' else 'failed'}",
                  acts=[S.btn("Open", S.open_run(r.run_id, r.title or r.run_id), "ghost")])
            for r in compact], full=True))
    if not shown:
        sections.append(S.callout(
            "Nothing needs you",
            "Runs that wait on a decision, a fix, or a cost cap land here.",
            tone="green", full=True))
    if repairing:
        items = [
            S.dot(r.title or r.run_id,
                  f"{r.recipe} · being repaired" if r.recipe else "being repaired",
                  acts=[S.btn("Open", S.open_run(r.run_id, r.title or r.run_id), "ghost")])
            for r in repairing
        ]
        sections.append(S.lst("Being repaired", items, full=True,
                              note="Auto-repair owns these runs; they are not waiting on you."))
    if len(ordered) > _ROW_CAP:
        more = len(ordered) - _ROW_CAP
        sections.append(S.lst(
            "More",
            [S.dot(f"{more} more…", "Older runs that still need you",
                   acts=[S.btn("Open the runs page", S.page_link("runs"), "ghost")])],
            full=True))
    return sections


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    errors: dict[str, str] = {}
    ctx: dict[str, Any] = {}
    sections = S.guarded(errors, "Inbox", lambda: _inbox(home, now, errors, ctx))
    chips = [S.chip(f"✋ needs you {ctx.get('needs_you', 0)}", "yellow"),
             S.chip(f"✗ failed {ctx.get('failed', 0)}", "red")]
    if ctx.get("repairing"):
        chips.append(S.chip(f"⟳ being repaired {ctx['repairing']}", "blue"))
    return S.page("inbox", "Inbox", "What needs you first, then what failed.",
                  chips_=chips, sections=sections, errors=errors, args=args)
