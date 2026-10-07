"""Automations & scheduling — recipes on a schedule, and what schedules them."""
from __future__ import annotations

import datetime as _dt
import os
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TITLE = "Automations & scheduling"
SUB = ("Recipes that run on a schedule whether or not Zed is open. "
       "Each firing is an ordinary run that waits for review.")
TABS = [("automations", "Automations"), ("schedulers", "Schedulers")]

_MARK_COLOUR = {"✓": "green", "✗": "red", "✋": "yellow", "●": "blue", "⏸": "sub"}
_KICKOFF_LINES = 40


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    errors: dict[str, str] = {}
    scheduler = _scheduler(home)
    on = bool(scheduler.get("installed"))
    chips = [S.chip("scheduler on" if on else "scheduler off", "green" if on else "yellow")]
    actions = [S.btn("New automation", S.thread("/automation new "), "primary")]
    if tab == "automations":
        sections = _automations_tab(home, args, errors)
    else:
        sections = _schedulers_tab(home, scheduler, errors)
    return S.page("autos", TITLE, SUB, chips_=chips, actions=actions, tabs=TABS, tab=tab,
                  args=args, sections=sections, errors=errors)


def _scheduler(home: Path) -> dict[str, Any]:
    try:
        from mini_ork import automations

        return automations.scheduler_status(home)
    except Exception:  # noqa: BLE001 — the chip just reads "off"
        return {}


# ── Automations tab ────────────────────────────────────────────────────────

def _rows(home: Path) -> list[dict[str, Any]]:
    from mini_ork.acp.automation_view import automation_rows

    return automation_rows(home)


def _last_cell(row: dict[str, Any]) -> dict[str, Any]:
    mark = str(row.get("mark") or "")
    if row.get("last_error"):
        return S.cell("✗ not started", "red")
    if not row.get("last_run_id"):
        return S.muted("never")
    parts = [p.strip() for p in str(row.get("last_run") or "").replace("`", "").split(" · ")]
    status = parts[2] if len(parts) > 2 else ""
    ago = parts[1] if len(parts) > 1 else ""
    text = " ".join(p for p in (mark, status) if p) + (f" · {ago}" if ago else "")
    return S.cell(text, _MARK_COLOUR.get(mark, "sub"))


def _automations_tab(home: Path, args: dict[str, str], errors: dict[str, str]) -> list[dict[str, Any]]:
    try:
        rows = _rows(home)
    except Exception as exc:  # noqa: BLE001
        errors["Automations"] = f"{type(exc).__name__}: {exc}"
        return [S.lst("Automations", [S.bad("Could not read automations", f"{type(exc).__name__}: {exc}")],
                      full=True)]
    if not rows:
        return [S.table(
            "Automations",
            [S.col(fr=1, min=120), S.col(fr=1, min=120), S.col(80), S.col(110)],
            ["automation", "when", "next", "last"],
            [[S.muted("No automations yet"), "", "", ""]],
            full=True,
            note="Ask a mini-ork thread to run a recipe on a schedule, "
                 "or use mini-ork automations add.")]
    selected = next((r for r in rows if r["id"] == args.get("auto")), rows[0])
    table = S.table(
        "Automations",
        [S.col(fr=1, min=120), S.col(fr=1, min=120), S.col(80), S.col(110)],
        ["automation", "when", "next", "last"],
        [{"cells": [S.mono(r["id"]), r.get("when") or r.get("schedule") or "",
                    S.muted("paused" if not r.get("enabled") else (r.get("next") or "—")),
                    _last_cell(r)],
          "do": S.set_args(auto=r["id"]), "sel": r["id"] == selected["id"]} for r in rows],
        full=True)
    out = [table]
    out += S.guarded(errors, selected["id"], lambda: _detail(selected))
    out += S.guarded(errors, "Next three firings", lambda: _firings(selected))
    out += S.guarded(errors, "Kickoff each run receives", lambda: _kickoff(home, selected["id"]))
    return out


def _detail(row: dict[str, Any]) -> dict[str, Any]:
    aid = row["id"]
    enabled = bool(row.get("enabled"))
    actions = [
        S.btn("Run now", S.cli("automations", "run", aid,
                               confirm=f"Start a run of {row.get('recipe') or 'this automation'} now?"),
              "primary"),
        S.btn("Pause" if enabled else "Resume", S.cli("automations", "pause" if enabled else "resume", aid)),
        S.btn("Delete", S.cli("automations", "remove", aid,
                              confirm=f"Delete the automation {aid}? Its past runs stay."), "danger"),
    ]
    note = f"Last firing did not start: {row['last_error']}" if row.get("last_error") else ""
    return S.kv(aid, [("Recipe", row.get("recipe") or "—"),
                      ("When", row.get("when") or row.get("schedule") or "—"),
                      ("State", "active" if enabled else "paused", "green" if enabled else "yellow")],
                actions=actions, note=note)


def _firings(row: dict[str, Any]) -> dict[str, Any]:
    if not row.get("enabled"):
        return S.lst("Next three firings", [S.dot("Paused — nothing is scheduled", "Resume it to fire again.")])
    from mini_ork import automations

    fires = automations.next_fires(str(row.get("schedule") or ""), n=3)
    if not fires:
        return S.lst("Next three firings", [S.warn("This schedule never fires", str(row.get("schedule") or ""))])
    return S.lst("Next three firings", [S.dot(_fmt_fire(f)) for f in fires])


def _fmt_fire(when: _dt.datetime) -> str:
    return f"{when.strftime('%a %b')} {when.day} {when.strftime('%H:%M')}"


def _kickoff(home: Path, automation_id: str) -> dict[str, Any]:
    from mini_ork import automations

    record = next((a for a in automations.load(home) if a.get("id") == automation_id), {})
    kickoff = str(record.get("kickoff") or "")
    text = kickoff
    candidates = [Path(kickoff).expanduser()]
    if kickoff and not Path(kickoff).is_absolute():
        candidates += [home.parent / kickoff, home / kickoff]
    for path in candidates:
        try:
            if kickoff and len(kickoff) < 1024 and path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                break
        except OSError:
            continue
    lines = [ln for ln in text.splitlines()] or ["(empty kickoff)"]
    if len(lines) > _KICKOFF_LINES:
        lines = lines[:_KICKOFF_LINES] + [f"… {len(lines) - _KICKOFF_LINES} more lines"]
    return S.code("Kickoff each run receives", [(ln, "body") for ln in lines], full=True)


# ── Schedulers tab ─────────────────────────────────────────────────────────

def _schedulers_tab(home: Path, scheduler: dict[str, Any], errors: dict[str, str]) -> list[dict[str, Any]]:
    out = S.guarded(errors, "Project scheduler", lambda: _project_scheduler(scheduler))
    out += S.guarded(errors, "Epic scheduler", lambda: _epic_scheduler(home))
    out += S.guarded(errors, "Conductor · last decisions", lambda: _conductor(home))
    out += S.guarded(errors, "Watchdog", lambda: _watchdog(home))
    out += S.guarded(errors, "Lifetime leaderboard", lambda: _leaderboard(home))
    return out


def _project_scheduler(scheduler: dict[str, Any]) -> dict[str, Any]:
    on = bool(scheduler.get("installed"))
    platform = str(scheduler.get("platform") or "")
    if on:
        where = Path(str(scheduler.get("plist") or "")).stem or platform
        tick = scheduler.get("last_tick")
        sub = f"checks every minute · last tick {tick}" if tick else "checks every minute · no tick yet"
        items = [S.ok(f"On · {platform} {where}".strip(), sub + " · missed firings are skipped")]
    else:
        items = [S.warn("Off", "automations will not fire")]
    action = (S.btn("Turn off", S.cli("automations", "scheduler", "uninstall",
                                      confirm="Turn the project scheduler off? Automations stop firing."),
                    "danger")
              if on else S.btn("Turn on", S.cli("automations", "scheduler", "install"), "primary"))
    return S.lst("Project scheduler", items, full=True, actions=[action],
                 note="Logs: .mini-ork/automations-tick.log and automations.log.")


def _db(home: Path):
    from mini_ork.web.db import db_for

    return db_for(home)


def _epic_scheduler(home: Path) -> dict[str, Any]:
    from mini_ork import cost_ledger, scheduler

    db = _db(home)
    counts: dict[str, int] = {}
    if db.has_table("epics"):
        for r in db.rows("SELECT status, COUNT(*) AS n FROM epics WHERE archived_at IS NULL "
                         "GROUP BY status"):
            counts[str(r.get("status") or "")] = int(r.get("n") or 0)
    paused = scheduler.cost_pause_active(str(home))
    try:
        cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50") or 50)
    except ValueError:
        cap = 50.0
    state_db = home / "state.db"
    spent = cost_ledger.spent_last_24h(state_db if state_db.is_file() else None)
    return S.kv("Epic scheduler", [
        ("State", "cost pause" if paused else "no cost pause", "yellow" if paused else "text"),
        ("Not started", counts.get("not started", 0)),
        ("In progress", counts.get("in progress", 0)),
        ("Blocked", counts.get("blocked", 0), "yellow" if counts.get("blocked") else "text"),
        ("Daily cap", f"{S.money(spent)} of {S.money(cap)}",
         "red" if spent >= cap else "text", "MO_DAILY_BUDGET_USD"),
    ])


def _conductor(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT decided_at, epic_id, chosen_recipe, chosen_topology, decided_by, rationale, "
                   "outcome FROM conductor_decisions ORDER BY decided_at DESC LIMIT 3") \
        if db.has_table("conductor_decisions") else []
    if not rows:
        return S.lst("Conductor · last decisions", [S.dot("No conductor decisions yet")])
    now = int(time.time())
    items = []
    for r in rows:
        target = r.get("epic_id") or r.get("chosen_recipe") or "a run"
        recipe = r.get("chosen_recipe")
        head = f"Picked {target}" + (f" · {recipe}" if recipe and recipe not in ("?", target) else "")
        why = str(r.get("rationale") or "").strip()
        sub = " · ".join(p for p in (f"by {r.get('decided_by')}" if r.get("decided_by") else "",
                                     S.age(r.get("decided_at"), now) + " ago" if r.get("decided_at") else "",
                                     str(r.get("outcome") or ""), why[:90]) if p)
        items.append(S.dot(head, sub))
    return S.lst("Conductor · last decisions", items)


def _watchdog(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows("SELECT run_id, task_class, matched_pattern, match_score, outcome, aborted_at "
                   "FROM watchdog_aborts ORDER BY aborted_at DESC LIMIT 3") \
        if db.has_table("watchdog_aborts") else []
    if not rows:
        return S.lst("Watchdog", [S.ok("No watchdog aborts recorded")])
    return S.lst("Watchdog", [
        S.warn(f"Aborted {r.get('run_id')}",
               " · ".join(str(p) for p in (r.get("task_class"), r.get("matched_pattern"),
                                            r.get("outcome")) if p))
        for r in rows])


def _leaderboard(home: Path) -> dict[str, Any]:
    db = _db(home)
    rows = db.rows(
        "SELECT model, SUM(runs_count) AS runs, SUM(success_count) AS ok, "
        "SUM(avg_cost_usd * runs_count) AS spend FROM agent_performance_memory "
        "GROUP BY model ORDER BY runs DESC LIMIT 8") if db.has_table("agent_performance_memory") else []
    cols = [S.col(90), S.col(50), S.col(60), S.col(60)]
    head = ["lane", "runs", "success", "cost/run"]
    if not rows:
        return S.table("Lifetime leaderboard", cols, head, [[S.muted("No lane history yet"), "", "", ""]])
    out = []
    for r in rows:
        runs = int(r.get("runs") or 0)
        ok = int(r.get("ok") or 0)
        lane = str(r.get("model") or "?")
        out.append([S.cell(lane, f"fam:{lane}"), S.mono(runs),
                    S.mono(f"{round(100 * ok / runs)}%" if runs else "—"),
                    S.mono(S.money(float(r.get("spend") or 0) / runs) if runs else "—")])
    return S.table("Lifetime leaderboard", cols, head, out)
