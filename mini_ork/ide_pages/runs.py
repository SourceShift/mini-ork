"""Runs & live execution — every run, what is in flight, and the event hooks.

Tabs: ``runs`` (All runs, filterable), ``active`` (Active & control),
``hooks`` (Event hooks). Data: ``acp.fleet.fleet_rows`` for the run table,
``run_events`` node lifecycle rows for heartbeats and the event tail.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("runs", "All runs"), ("active", "Active & control"), ("hooks", "Event hooks")]
FILTERS = ("all", "working", "needs_you", "failed", "done")
_RUN_LIMIT = 40
_ACTIVE_LIMIT = 20
_EVENT_TAIL = 12
# A heartbeat older than this reads as stale (yellow).
_STALE_SECONDS = 90


def _db(home: Path):
    from mini_ork.web.deps import db_for

    return db_for(home)


def _change(added: int, removed: int) -> str:
    return f"+{added} −{removed}" if (added or removed) else "—"


def _fleet(home: Path, state: str, limit: int):
    from mini_ork.acp.fleet import fleet_rows

    return fleet_rows(home, state=state, limit=limit)


# ── All runs ───────────────────────────────────────────────────────────────

def _total_runs(home: Path) -> int:
    db = _db(home)
    if not db.has_table("task_runs"):
        return 0
    rows = db.rows("SELECT COUNT(*) AS n FROM task_runs")
    return int(rows[0].get("n") or 0) if rows else 0


def _all_runs(home: Path, args: dict[str, str], now: int,
              ctx: dict[str, Any]) -> list[dict[str, Any]]:
    current = (args.get("filter") or "all").replace("-", "_")
    if current not in FILTERS:
        current = "all"
    rows, counts = _fleet(home, current, _RUN_LIMIT)
    ctx["counts"] = counts
    total = sum(counts.get(k, 0) for k in ("working", "needs_you", "failed", "done"))
    labels = {"all": f"All {total}",
              "working": f"● working {counts.get('working', 0)}",
              "needs_you": f"✋ needs you {counts.get('needs_you', 0)}",
              "failed": f"✗ failed {counts.get('failed', 0)}",
              "done": f"✓ done {counts.get('done', 0)}"}
    chips = S.chips("", [{"t": labels[k], "on": k == current, "do": S.set_args(filter=k)}
                         for k in FILTERS], full=True)
    table_rows = []
    for r in rows:
        mark, colour = S.STATE_MARK.get(r.state, S.STATE_MARK["working"])
        step_colour = "red" if r.state == "failed" else "yellow" if r.state == "needs_you" else "muted"
        title = r.title or r.run_id
        table_rows.append({
            "cells": [S.cell(mark, colour), S.cell(title, "text"), S.muted(r.recipe),
                      S.cell(r.step or "—", step_colour), S.muted(S.age(r.started_at, now)),
                      S.mono(S.money(r.cost_usd)), S.mono(_change(r.added, r.removed))],
            "do": S.open_run(r.run_id, title),
        })
    if not table_rows:
        table_rows = [[S.muted(""), S.muted("No runs here yet"), "", "", "", "", ""]]
    shown = f"Showing the latest {len(rows)}. " if len(rows) >= _RUN_LIMIT else ""
    project_total = _total_runs(home)
    if project_total > total:
        shown += f"Counts cover the latest {total} of {project_total} runs. "
    table = S.table(
        "", [S.col(18), S.col(fr=1, min=180), S.col(110), S.col(fr=0.6, min=120), S.col(46),
             S.col(56), S.col(64)],
        ["", "run", "recipe", "step", "age", "cost", "change"], table_rows, full=True,
        note=f"{shown}Filter like /runs needs-you or /runs failed recipe:code-fix. "
             "Click a run to open its card.")
    return [chips, table]


# ── Active & control ───────────────────────────────────────────────────────

def _live_node(home: Path, run_id: str) -> dict[str, Any]:
    """The run's open node (started, not ended), its lane and last heartbeat."""
    db = _db(home)
    if not db.has_table("run_events"):
        return {}
    events = db.rows(
        "SELECT event_type, payload_json, created_at, last_heartbeat_at FROM run_events "
        "WHERE run_id = ? AND event_type IN ('node_start','node_heartbeat','node_end') "
        "ORDER BY created_at ASC",
        (run_id,),
    )
    return _live_node_from_events(events)


def _live_nodes_bulk(home: Path, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One ``IN``-query for the active tab's open-node state across many runs.

    The ``active`` tab calls ``_live_node`` once per row, which fans out as N
    SQLite queries — the dominant per-render cost on a busy home. This batched
    variant keeps the rendering logic identical to ``_live_node`` while doing
    a single SELECT for the whole visible set.
    """
    db = _db(home)
    if not run_ids or not db.has_table("run_events"):
        return {rid: {} for rid in run_ids}
    placeholders = ",".join("?" for _ in run_ids)
    rows = db.rows(
        "SELECT run_id, event_type, payload_json, created_at, last_heartbeat_at "
        f"FROM run_events WHERE run_id IN ({placeholders}) "
        "AND event_type IN ('node_start','node_heartbeat','node_end') "
        "ORDER BY created_at ASC",
        tuple(run_ids),
    )
    events_by_run: dict[str, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
    for row in rows or []:
        rid = str(row.get("run_id") or "")
        if rid in events_by_run:
            events_by_run[rid].append(row)
    return {rid: _live_node_from_events(events_by_run[rid]) for rid in run_ids}


def _live_node_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a single run's event list to its open node + last heartbeat.

    Pulled out of ``_live_node`` so ``_live_nodes_bulk`` can reuse it without
    paying for a per-run ``IN`` query.
    """
    open_nodes: dict[str, dict[str, Any]] = {}
    last_beat = 0
    for e in events:
        try:
            payload = json.loads(e.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        node = str(payload.get("node_id") or "")
        beat = int(e.get("created_at") or 0)
        hb_ms = e.get("last_heartbeat_at")
        if hb_ms:
            beat = max(beat, int(hb_ms) // 1000)
        last_beat = max(last_beat, beat)
        if e.get("event_type") == "node_start" and node:
            open_nodes[node] = {"node": node, "lane": str(payload.get("model_lane") or "")}
        elif e.get("event_type") == "node_end" and node:
            open_nodes.pop(node, None)
    current = list(open_nodes.values())[-1] if open_nodes else {}
    return {**current, "beat": last_beat}


def _host(home: Path, run_id: str) -> str:
    sync = home / "runs" / run_id / ".remote-sync-state.json"
    if not sync.is_file():
        return "local"
    try:
        data = json.loads(sync.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "remote"
    return str(data.get("node") or data.get("node_name") or "remote") if isinstance(data, dict) else "remote"


def _cost_paused(home: Path, run_id: str) -> bool:
    return (home / "runs" / run_id / ".cost-pause").exists()


def _active(home: Path, now: int) -> list[dict[str, Any]]:
    rows, _ = _fleet(home, "working", _ACTIVE_LIMIT)
    # A cost-paused run reads as "needs you" in the fleet but is still in flight.
    waiting, _ = _fleet(home, "needs_you", _ACTIVE_LIMIT)
    rows = list(rows) + [r for r in waiting if _cost_paused(home, r.run_id)]
    # One IN-query for the open-node state of every active row, then map back.
    live_by_run = _live_nodes_bulk(home, [r.run_id for r in rows])
    table_rows = []
    for r in rows:
        live = live_by_run.get(r.run_id) or _live_node(home, r.run_id)
        beat = live.get("beat") or 0
        if _cost_paused(home, r.run_id):
            heartbeat = S.cell("paused · cost", "yellow")
        elif beat:
            stale = now - beat > _STALE_SECONDS
            heartbeat = S.cell(f"{S.age(beat, now)} ago" if now - beat >= 60 else f"{max(0, now - beat)}s ago",
                               "yellow" if stale else "green")
        else:
            heartbeat = S.muted("—")
        lane = live.get("lane") or ""
        table_rows.append({
            "cells": [S.mono(r.run_id), S.cell(live.get("node") or r.step or "—", "body"),
                      S.cell(lane or "—", f"fam:{lane}" if lane else "sub"), S.muted(_host(home, r.run_id)),
                      heartbeat, S.mono(S.money(r.cost_usd))],
            "do": S.open_run(r.run_id, r.title or r.run_id),
        })
    if not table_rows:
        table_rows = [[S.muted("Nothing in flight"), "", "", "", "", ""]]
    table = S.table(
        "Active dispatches · heartbeat",
        [S.col(fr=1, min=160), S.col(110), S.col(70), S.col(110), S.col(80), S.col(56)],
        ["run", "node", "lane", "host", "heartbeat", "cost"], table_rows, full=True,
        note="A stale heartbeat is the first hint a dispatcher hung.")

    controls: list[dict[str, Any]] = []
    for r in rows:
        title = r.title or r.run_id
        if _cost_paused(home, r.run_id):
            controls.append(S.item(f"{r.run_id} · paused on cost",
                                   f"{title} · resume it with `mini-ork resume {r.run_id}`",
                                   m="⏸", mc="yellow",
                                   acts=[S.btn("Resume", S.cli("board", "resume", r.run_id), "primary"),
                                         S.btn("Open", S.open_run(r.run_id, title), "ghost")]))
            continue
        controls.append(S.item(
            f"{r.run_id} · {r.recipe}" if r.recipe else r.run_id,
            f"{title} · Stop lets the current node finish; no new nodes start.",
            m="●", mc="blue",
            acts=[S.btn("Stop", S.cli("board", "stop", r.run_id,
                                      confirm=f"Stop {r.run_id}? The current node finishes."), "warn"),
                  S.btn("Kill", S.cli("board", "kill", r.run_id,
                                      confirm=f"Kill {r.run_id}? SIGTERM, then SIGKILL after 2 s."),
                        "danger"),
                  S.btn("Open", S.open_run(r.run_id, title), "ghost")]))
    failed, _ = _fleet(home, "failed", 3)
    for r in failed:
        title = r.title or r.run_id
        controls.append(S.item(
            f"{r.run_id} · failed at {r.step or 'unknown step'}",
            f"{title} · recover from its last checkpoint with `mini-ork recover {r.run_id}`",
            m="✗", mc="red", acts=[S.btn("Open", S.open_run(r.run_id, title), "ghost")]))
    controls.append(S.item("Roll back a promotion",
                           "version_registry keeps rollback pointers for every promoted change",
                           m="↺", mc="purple",
                           acts=[S.btn("Roll back…", S.page_link("verify", "autonomy"))]))
    return [table, S.lst("Controls", controls, full=True)]


# ── Event hooks ────────────────────────────────────────────────────────────

def _hook_sources(home: Path) -> list[tuple[str, str]]:
    """``(where, command head)`` for every place MINI_ORK_ON_EVENT is set.

    Only the command's first word is shown — the rest may carry a token.
    """
    found: list[tuple[str, str]] = []
    env = os.environ.get("MINI_ORK_ON_EVENT", "").strip()
    if env:
        found.append(("environment", Path(env.split()[0]).name))
    candidates = [home / "config" / "secrets.local.sh", home / "config" / "env.sh",
                  home.parent / "config" / "secrets.local.sh"]
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "MINI_ORK_ON_EVENT=" not in stripped:
                continue
            value = stripped.split("MINI_ORK_ON_EVENT=", 1)[1].strip().strip("'\"")
            if value:
                found.append((str(path), Path(value.split()[0]).name))
    return found


def _hooks(home: Path, now: int) -> list[dict[str, Any]]:
    sources = _hook_sources(home)
    docs = Path(__file__).resolve().parents[2] / "docs" / "EVENT-HOOKS.md"
    doc_acts = [S.btn("Read EVENT-HOOKS.md", S.open_path(str(docs)), "ghost")] if docs.is_file() else []
    if sources:
        items = [S.ok(f"{head}", f"MINI_ORK_ON_EVENT · {where}") for where, head in sources]
    else:
        items = [S.dot("No handler configured",
                       "Set MINI_ORK_ON_EVENT to a command; it is called with event_type, run_id, "
                       "payload_json after every event (5 s cap, failures ignored).", doc_acts)]
    handlers = S.lst("MINI_ORK_ON_EVENT handlers", items, full=True,
                     note="Node lifecycle events pushed to external observers. Pair a watcher "
                          "with steering to close the supervisor loop.")

    db = _db(home)
    lines: list[tuple[str, str]] = []
    if db.has_table("run_events"):
        for e in db.rows(
            "SELECT run_id, event_type, payload_json, created_at FROM run_events "
            "ORDER BY created_at DESC LIMIT ?", (_EVENT_TAIL,),
        ):
            try:
                payload = json.loads(e.get("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            payload = payload if isinstance(payload, dict) else {}
            kind = str(e.get("event_type") or "")
            parts = [f"{S.age(e.get('created_at'), now):>4}", f"{kind:<14}", str(e.get("run_id") or "")]
            if payload.get("node_id"):
                parts.append(str(payload["node_id"]))
            if payload.get("model_lane"):
                parts.append(f"lane={payload['model_lane']}")
            if payload.get("finish_reason"):
                parts.append(str(payload["finish_reason"]))
            failed = payload.get("finish_reason") in ("error", "timeout", "verdict_fail")
            colour = "red" if failed else "yellow" if ("cost" in kind or "steer" in kind) else "muted"
            lines.append(("  ".join(parts), colour))
    if not lines:
        lines = [("No events recorded yet.", "dim")]
    return [handlers, S.code("Last events", list(reversed(lines)), full=True)]


def _web_ui() -> str | None:
    """The local web UI's address when ``mini-ork serve`` is listening (loopback only)."""
    import socket

    port = int(os.environ.get("MO_SERVE_PORT", "7090") or 7090)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.05):
            return f"http://127.0.0.1:{port}"
    except OSError:
        return None


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in {k for k, _ in TABS} else "runs"
    now = int(time.time())
    errors: dict[str, str] = {}
    if tab == "active":
        sections = S.guarded(errors, "Active dispatches", lambda: _active(home, now))
    elif tab == "hooks":
        sections = S.guarded(errors, "Event hooks", lambda: _hooks(home, now))
    else:
        sections = S.guarded(errors, "Runs", lambda: _all_runs(home, args, now, {}))
    chips = []
    try:
        chips = [S.chip(f"{_total_runs(home)} runs")]
    except Exception as exc:  # noqa: BLE001
        errors["counts"] = f"{type(exc).__name__}: {exc}"
    web = _web_ui()
    actions = [S.btn("Open web UI", S.url(web), "ghost")] if web else []
    return S.page(
        "runs", "Runs & live execution",
        "Every run of the project, including runs started from the CLI and automations.",
        chips_=chips, actions=actions, tabs=TABS, tab=tab, args=args, sections=sections,
        errors=errors)
