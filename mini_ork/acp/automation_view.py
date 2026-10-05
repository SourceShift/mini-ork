"""Per-automation rows + automation-card markdown for the ACP surface (Zed S6b-1).

``/automations`` answers "what is scheduled, when does it next fire, what did
the last run do?"; ``/automation <id>`` answers "give me the card for one
schedule". ``automation_rows`` / ``automation_card`` are the read-model
projections; ``render_automations`` / ``render_automation_card`` are the
markdown formatters.

Pure functions, no async, no ACP types, no module-level state — same shape
as ``mini_ork.acp.recipe_view``. ``automation_card`` returns plain dicts so
the caller can shape, filter, and order without coupling to a dataclass.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any


# ── markdown helpers ────────────────────────────────────────────────────────


_WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Card recent-runs table is wider than the table view; we cap at 10 per the
# kickoff spec (the run list view caps at 5 elsewhere — see recipe_view).
RECENT_RUNS_LIMIT = 10


def _format_age(created_at: Any, now_epoch: int) -> str:
    """Compact ``"3h"`` / ``"5d"`` / ``"now"`` age label.

    Same shape as ``mini_ork.acp.recipe_view._format_age``. Duplicated here
    because ``recipe_view._format_age`` is module-private and the two views
    will drift independently if one is extended (e.g. week / month labels).
    """
    try:
        ts = int(created_at)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    if now_epoch <= 0:
        return "—"
    delta = max(0, int(now_epoch) - ts)
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86_400:
        return f"{delta // 3600}h"
    return f"{delta // 86_400}d"


def _ago(epoch: int, now_epoch: int) -> str:
    """``"3h ago"`` / ``"just now"`` / ``"—"``."""
    age = _format_age(epoch, now_epoch)
    if age == "—":
        return age
    return "just now" if age == "now" else f"{age} ago"


def _format_next_fire(when: _dt.datetime, now: _dt.datetime) -> str:
    """Short local-time label: ``"Mon 12 Oct 09:00"`` / ``"today 14:30"`` /
    ``"tomorrow 09:00"``."""
    today = now.date()
    target = when.date()
    time_text = when.strftime("%H:%M")
    if target == today:
        return f"today {time_text}"
    if target == today + _dt.timedelta(days=1):
        return f"tomorrow {time_text}"
    # Within the next 7 days: weekday + time. Otherwise: weekday + day + month.
    delta_days = (target - today).days
    if 0 < delta_days < 7:
        return f"{_WEEKDAY_SHORT[target.weekday()]} {time_text}"
    return f"{_WEEKDAY_SHORT[target.weekday()]} {target.day} {target.strftime('%b')} {time_text}"


def _table_mark(
    *,
    enabled: bool,
    last_run_id: str | None,
    last_error: str | None,
    run_status: str | None,
    run_dir: Path | None,
) -> str:
    """Mark glyph for the table mark column.

    Order: paused (⏸) → never fired (·) → failed to launch (✗) → delegate
    to ``task_state.run_mark(status, run_dir)`` for the actual run. The
    delegate owns the "needs you" / done / failed glyphs from the canonical
    run-mark seam.
    """
    if not enabled:
        return "⏸"
    if last_error:
        return "✗"
    if not last_run_id:
        return "·"
    from mini_ork.acp import task_state as _task_state

    return _task_state.run_mark(run_status, run_dir)


# ── rows projection ─────────────────────────────────────────────────────────


def _run_mark(home: Path, run_id: str, status: str | None) -> str:
    """The canonical run mark (✋ for a run waiting on review, ✓, ✗, ●)."""
    from mini_ork.acp import task_state as _task_state

    return _task_state.run_mark(status, home / "runs" / run_id)


def automation_rows(home: Path | None, *, now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    """One row per automation for the ``/automations`` table (enabled first).

    Each row: ``id``, ``name``, ``recipe``, ``schedule``, ``when``,
    ``enabled``, ``workspace``, ``next`` (short local time or ``"paused"``),
    ``last_run`` (the table cell), ``last_run_id``, ``last_error``, ``mark``.
    One history read for the whole table.
    """
    now = now or _dt.datetime.now()
    now_epoch = int(now.timestamp())

    from mini_ork import automations as _auto

    items = _auto.load(home) if home is not None else []
    statuses = _auto.run_statuses(home) if (home is not None and items) else {}

    rows: list[dict[str, Any]] = []
    for a in items:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        schedule = str(a.get("schedule") or "")
        enabled = bool(a.get("enabled", True))
        rid = a.get("last_run_id") if isinstance(a.get("last_run_id"), str) else None
        error = a.get("last_error") if isinstance(a.get("last_error"), str) else None
        status = statuses.get(rid) if rid else None

        if not enabled:
            next_text = "paused"
        else:
            fires = _auto.next_fires(schedule, n=1, after=now)
            next_text = _format_next_fire(fires[0], now) if fires else "—"
        if error:
            last_run = f"not started: {error}"
        elif rid:
            ago = _ago(_parse_epoch(a.get("last_fired_at")), now_epoch)
            last_run = f"`{rid}` · {ago} · {status or 'starting'}"
        else:
            last_run = "never"
        mark = _table_mark(enabled=enabled, last_run_id=rid, last_error=error,
                           run_status=status,
                           run_dir=(home / "runs" / rid) if (home is not None and rid) else None)
        rows.append({
            "id": aid,
            "name": a.get("name") or aid,
            "recipe": a.get("recipe") or "",
            "schedule": schedule,
            "when": _auto.describe(schedule),
            "enabled": enabled,
            "workspace": a.get("workspace") or "worktree",
            "next": next_text,
            "last_run": last_run,
            "last_run_id": rid,
            "last_error": error,
            "mark": mark,
        })
    rows.sort(key=lambda r: (0 if r["enabled"] else 1, r["id"]))
    return rows


# ── render table ────────────────────────────────────────────────────────────


def render_automations(
    home: Path | None,
    *,
    now: _dt.datetime | None = None,
    scheduler: dict[str, Any] | None = None,
) -> str:
    """Markdown for the ``/automations`` table + scheduler footer.

    Empty → kickoff copy. Footer line depends on scheduler state: on → ``last
    tick <age> ago.``; off and any enabled automation exists → off-and-fire
    warning; off and no enabled automation → no footer.
    """
    rows = automation_rows(home, now=now)
    if not rows:
        return (
            "No automations yet. In a mini-ork thread, ask to run a recipe "
            'on a schedule ("run changelog-entry every weekday at 9"), or '
            "use mini-ork automations add."
        )
    lines: list[str] = [
        "| | automation | recipe | when | next | last run |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['mark']} | {r['name']} (`{r['id']}`) | `{r['recipe']}` | {r['when']} | "
            f"{r['next']} | {r['last_run']} |"
        )
    lines.append("")
    footer = _render_scheduler_footer(rows, home=home, scheduler=scheduler,
                                      now_epoch=int((now or _dt.datetime.now()).timestamp()))
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _render_scheduler_footer(
    rows: list[dict[str, Any]],
    *,
    home: Path | None,
    scheduler: dict[str, Any] | None,
    now_epoch: int,
) -> str:
    """Pick the right footer line based on scheduler + enabled rows."""
    if scheduler is None and home is not None:
        from mini_ork import automations as _auto
        try:
            scheduler = _auto.scheduler_status(home)
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(scheduler, dict):
        return ""
    installed = bool(scheduler.get("installed"))
    last_tick = scheduler.get("last_tick")
    age = "—"
    if last_tick:
        try:
            tick_dt = _dt.datetime.fromisoformat(str(last_tick))
            age = _format_age(int(tick_dt.timestamp()), now_epoch)
        except (TypeError, ValueError):
            age = "—"
    if installed:
        if age == "now":
            return "Scheduler on — last tick just now."
        if age and age != "—":
            return f"Scheduler on — last tick {age} ago."
        return "Scheduler on — no tick yet."
    any_enabled = any(r["enabled"] for r in rows)
    if not any_enabled:
        return ""
    return (
        "Scheduler off — scheduled automations will not fire. "
        "`/automation scheduler on` turns it on."
    )


# ── card projection ────────────────────────────────────────────────────────


def automation_card(
    home: Path | None,
    automation_id: str,
    *,
    now: _dt.datetime | None = None,
    scheduler: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The full card payload for ``automation_id`` (or ``None`` when absent).

    Combines the store record, the proposal draft (if any), the run-history
    rows for ``Recent runs``, the scheduler status, and the kickoff excerpt.
    Every read is soft — partial / broken YAML or a missing DB degrades to
    empty dicts, never raises.
    """
    if home is None:
        return None
    from mini_ork import automations as _auto

    try:
        items = _auto.load(home)
    except Exception:  # noqa: BLE001
        items = []
    record = next((a for a in items if a.get("id") == automation_id), None)
    if record is None:
        return None
    try:
        proposal = _auto.get_proposal(home, automation_id)
    except Exception:  # noqa: BLE001
        proposal = None

    now = now or _dt.datetime.now()
    fires = _auto.next_fires(str(record.get("schedule") or ""), n=3, after=now)
    statuses = _auto.run_statuses(home)
    last_run = _auto.last_run_status(home, record, statuses=statuses)
    rid = record.get("last_run_id") if isinstance(record.get("last_run_id"), str) else None
    error = record.get("last_error") if isinstance(record.get("last_error"), str) else None
    enabled = bool(record.get("enabled", True))
    mark = _table_mark(enabled=enabled, last_run_id=rid, last_error=error,
                       run_status=statuses.get(rid) if rid else None,
                       run_dir=(home / "runs" / rid) if rid else None)
    recent = _recent_runs(home, statuses, list(record.get("runs") or []))

    if scheduler is None:
        try:
            scheduler = _auto.scheduler_status(home)
        except Exception:  # noqa: BLE001
            scheduler = {}

    return {
        "id": automation_id,
        "name": record.get("name") or automation_id,
        "recipe": record.get("recipe") or "",
        "schedule": str(record.get("schedule") or ""),
        "when": _auto.describe(str(record.get("schedule") or "")),
        "workspace": record.get("workspace") or "worktree",
        "enabled": enabled,
        "mark": mark,
        "next_fires": [f.isoformat() for f in fires],
        "last_run": last_run,
        "last_run_id": rid,
        "last_error": record.get("last_error"),
        "last_fired_at": record.get("last_fired_at"),
        "kickoff": record.get("kickoff") or "",
        "runs": recent,
        "proposal": proposal,
        "scheduler": scheduler,
    }


def _recent_runs(
    home: Path,
    statuses: dict[str, str],
    run_ids: list[str],
    *,
    limit: int = RECENT_RUNS_LIMIT,
) -> list[dict[str, Any]]:
    """Each run id (newest first) with its status and mark; ``—`` for runs the
    history no longer holds."""
    out: list[dict[str, Any]] = []
    for rid in run_ids[:limit]:
        status = statuses.get(rid)
        if status is None:
            out.append({"run_id": rid, "status": "unknown", "mark": "—"})
            continue
        out.append({"run_id": rid, "status": status or "starting",
                    "mark": _run_mark(home, rid, status)})
    return out


def _run_started(run_id: str, now_epoch: int) -> str:
    """``"3h ago"`` from the epoch in a ``run-<epoch>-<hex>`` id, else ``—``."""
    parts = run_id.split("-")
    if len(parts) >= 3 and parts[1].isdigit():
        return _ago(int(parts[1]), now_epoch)
    return "—"


def _parse_epoch(value: Any) -> int:
    """Coerce a ``created_at`` value (epoch int OR ISO datetime string) to int.

    ``history.list_runs`` returns ``created_at`` as ISO datetime strings in
    some code paths and as epoch ints in others (the test fixture seeds
    ints; live ``task_runs`` rows hold ISO strings). The card age label
    needs a numeric offset.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            return 0
    return 0


# ── render card ────────────────────────────────────────────────────────────


def render_automation_card(card: dict[str, Any], *, now: _dt.datetime | None = None) -> str:
    """Markdown for one automation card.

    Heading ``### <mark> <name>``, the metadata line, ``Next: <3 times>``
    (or ``Paused.``), the kickoff fenced block (first 40 lines, ``…`` when
    longer), the ``Recent runs`` table, the last-firing error (when any),
    and the commands footer.
    """
    now = now or _dt.datetime.now()
    enabled = bool(card.get("enabled", True))
    mark = card.get("mark") or ("⏸" if not enabled else "·")
    kickoff = str(card.get("kickoff") or "").rstrip()
    kickoff_lines = kickoff.splitlines()
    if len(kickoff_lines) > 40:
        kickoff_text = "\n".join(kickoff_lines[:40]) + "\n…"
    else:
        kickoff_text = kickoff
    workspace = card.get("workspace") or "worktree"
    workspace_text = "in a new worktree" if workspace == "worktree" else "in place"
    enabled_text = "enabled" if enabled else "paused"

    lines: list[str] = [
        f"### {mark} {card.get('name') or card.get('id')}",
        "",
        f"`{card.get('id')}` · `{card.get('recipe')}` · {card.get('when')} "
        f"(`{card.get('schedule')}`) · {workspace_text} · {enabled_text}",
        "",
    ]

    nexts = list(card.get("next_fires") or [])
    if enabled:
        if nexts:
            pretty = []
            for raw in nexts:
                try:
                    dt = _dt.datetime.fromisoformat(str(raw))
                except ValueError:
                    pretty.append(str(raw))
                    continue
                pretty.append(_format_next_fire(dt, now))
            lines.append(f"Next: {' · '.join(pretty)}")
        else:
            lines.append("Next: never (schedule has no future firing in the next 5 years)")
    else:
        lines.append("Paused.")
    lines.append("")

    if kickoff_text.strip():
        lines.append("```markdown")
        lines.append(kickoff_text)
        lines.append("```")
        lines.append("")

    proposal = card.get("proposal")
    if isinstance(proposal, dict) and proposal:
        lines.append("_Draft pending your approval:_")
        lines.append(
            f"- name: `{proposal.get('name')}` · schedule: `{proposal.get('schedule')}` "
            f"· workspace: `{proposal.get('workspace')}`"
        )
        lines.append("")

    runs = list(card.get("runs") or [])
    if runs:
        lines.append("Recent runs:")
        lines.append("")
        lines.append("| | run | started | status |")
        lines.append("| --- | --- | --- | --- |")
        for r in runs:
            started = _run_started(str(r.get("run_id") or ""), int(now.timestamp()))
            lines.append(
                f"| {r.get('mark') or '·'} | `{r.get('run_id')}` | "
                f"{started} | {r.get('status') or 'unknown'} |"
            )
        lines.append("")

    last_error = card.get("last_error")
    if last_error:
        lines.append(f"Last firing failed: {last_error}")
        lines.append("")

    lines.append(
        f"`/automation run {card.get('id')}` · "
        f"`/automation {'pause' if enabled else 'resume'} {card.get('id')}` · "
        f"`/automation delete {card.get('id')}`"
    )
    return "\n".join(lines)


__all__ = [
    "automation_rows",
    "render_automations",
    "automation_card",
    "render_automation_card",
]