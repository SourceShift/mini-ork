"""Python port of bin/mini-ork-scheduler — autonomous multi-epic delivery loop.

Strangler-fig parity port. Picks the highest-effective-priority ready epic
(priority-inheritance recursive CTE, embedded verbatim for guaranteed parity),
enforces the 24h budget cap + cost-pause sentinels, dispatches via the runner,
resolves the verdict, and cascades dep resolution through the ported
``epic_graph``. Bounded by --once / --max-iters (the port never sleeps in tests).

    main(argv=None, *, db=None, root=None) -> int
        exit: 0 drained/once-clean · 1 fatal deps · 2 cost-pause/cap · 3 max-iters
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import epic_graph

_PICK_SQL = """
WITH RECURSIVE inheritors(root, node) AS (
    SELECT e.id, e.id FROM epics e
     WHERE e.status = 'not started' AND e.archived_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM epic_dependencies d
            WHERE d.to_epic_id = e.id AND d.kind='hard' AND d.resolved_at IS NULL)
    UNION
    SELECT i.root, d.to_epic_id FROM inheritors i
      JOIN epic_dependencies d ON d.from_epic_id = i.node
     WHERE d.kind='hard' AND d.resolved_at IS NULL
),
effective(root, eff) AS (
    SELECT root, COALESCE(MAX(e.priority), 0) FROM inheritors i JOIN epics e ON e.id = i.node GROUP BY root
)
SELECT e.id FROM epics e JOIN effective ef ON ef.root = e.id
 WHERE e.status='not started' AND e.archived_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM epic_dependencies d
        WHERE d.to_epic_id = e.id AND d.kind='hard' AND d.resolved_at IS NULL)
 ORDER BY ef.eff DESC, e.created_at ASC LIMIT 1
"""


def _con(db):
    import sqlite3
    c = sqlite3.connect(db); c.execute("PRAGMA busy_timeout=5000"); return c


def _ensure_priority_column(db: str) -> None:
    """Idempotent epics.priority migration (Track B5) — mirrors the bash ALTER."""
    c = _con(db)
    cols = [r[1] for r in c.execute("PRAGMA table_info('epics')").fetchall()]
    if "priority" not in cols:
        try:
            c.execute("ALTER TABLE epics ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            c.commit()
        except Exception:
            pass
    c.close()


def pick_next_epic(db: str) -> str:
    c = _con(db)
    row = c.execute(_PICK_SQL).fetchone()
    c.close()
    return row[0] if row else ""


def today_cost_usd(db: str) -> float:
    c = _con(db)
    row = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM task_runs "
                    "WHERE created_at >= strftime('%s','now','-24 hours')").fetchone()
    c.close()
    return float(row[0] or 0)


def _cost_pause_active(home: str) -> bool:
    return (os.path.isfile(os.path.join(home, "cost-pause.sentinel"))
            or os.path.isfile(os.path.join(home, "control", "cost-pause")))


def resolve_kickoff(db: str, epic_id: str, root: str, recipe: str) -> str:
    c = _con(db)
    row = c.execute("SELECT kickoff_path FROM epics WHERE id=?", (epic_id,)).fetchone()
    c.close()
    kp = row[0] if row else None
    if kp and os.path.isfile(os.path.join(root, kp)):
        return os.path.join(root, kp)
    if os.path.isfile(os.path.join(root, "kickoffs", f"{epic_id}.md")):
        return os.path.join(root, "kickoffs", f"{epic_id}.md")
    ex = os.path.join(root, "recipes", recipe, "example-kickoff.md")
    if os.path.isfile(ex):
        return ex
    return ""


def dispatch_epic(db, epic_id, root, home, recipe, dry_run) -> int:
    kickoff = resolve_kickoff(db, epic_id, root, recipe)
    c = _con(db)
    if not kickoff:
        c.execute("UPDATE epics SET status='escalated', notes=COALESCE(notes,'') || "
                  "' [scheduler: no kickoff]' WHERE id=?", (epic_id,)); c.commit(); c.close()
        return 1
    c.execute("UPDATE epics SET status='in progress' WHERE id=?", (epic_id,)); c.commit()
    if dry_run:
        sys.stdout.write(f"  [dry-run] would dispatch: {root}/bin/mini-ork run {recipe} {kickoff}\n")
        c.execute("UPDATE epics SET status='not started' WHERE id=?", (epic_id,)); c.commit(); c.close()
        return 0
    log_dir = os.path.join(home, "runs", "scheduler"); os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"dispatch-{int(time.time())}-{epic_id}.log")
    with open(log_path, "wb") as fh:
        rc = subprocess.run([os.path.join(root, "bin", "mini-ork"), "run", recipe, kickoff],
                            stdout=fh, stderr=subprocess.STDOUT).returncode
    run_id = ""
    for line in open(log_path, errors="ignore"):
        if line.startswith("run_id="):
            run_id = line.strip().split("=", 1)[1]; break
    verdict = "unknown"
    if run_id:
        for vf in ("panel-verdict.json", "verdict.json"):
            p = os.path.join(home, "runs", run_id, vf)
            if os.path.isfile(p):
                try:
                    verdict = json.load(open(p)).get("verdict", "unknown"); break
                except Exception:
                    pass
    if verdict in ("pass", "success"):
        c.execute("UPDATE epics SET status='done' WHERE id=?", (epic_id,)); c.commit(); c.close()
        epic_graph.on_done(epic_id, db=db)   # cascade dep resolution (bash: epic_graph_on_done)
    else:
        c.execute("UPDATE epics SET status='escalated' WHERE id=?", (epic_id,)); c.commit(); c.close()
    return rc


def main(argv: list[str] | None = None, *, db: str | None = None, root: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(root, ".mini-ork")
    db = db or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    recipe = os.environ.get("MO_SCHED_RECIPE", "epic-runner")
    once = 0; idle = 60; max_iters = 0; dry_run = 0
    cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50.0"))

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--once": once = 1; i += 1
        elif a == "--idle-secs": idle = int(argv[i + 1]); i += 2
        elif a == "--max-iters": max_iters = int(argv[i + 1]); i += 2
        elif a == "--budget-cap-usd": cap = float(argv[i + 1]); i += 2
        elif a == "--dry-run": dry_run = 1; i += 1
        elif a in ("--help", "-h"):
            sys.stdout.write(
                "mini-ork scheduler — autonomous multi-epic delivery loop.\n\n"
                "Pulls the next-ready epic from `epics` (status='not started' AND all hard\n"
                "deps resolved) and dispatches it via `mini-ork run epic-runner <kickoff>`.\n"
                "On verdict=success, marks epic 'done' and cascades dep resolution. On\n"
                "failure, marks epic 'escalated' (visible in `mini-ork-epics list`).\n\n"
                "Flags:\n"
                "  --once               Run a single pick→dispatch→verdict cycle then exit\n"
                "  --idle-secs N        Sleep N seconds between empty-queue polls (default 60)\n"
                "  --max-iters N        Hard stop after N dispatches (default unlimited)\n"
                "  --budget-cap-usd X   Daily cost cap; refuses to dispatch when exceeded\n"
                "                       (defaults to MO_DAILY_BUDGET_USD or 50.0)\n"
                "  --dry-run            Print what would be dispatched, do not invoke runner\n"
                "  --help\n\n"
                "Exit codes:\n"
                "  0   queue drained (no ready epics) OR --once cycle finished cleanly\n"
                "  1   fatal: missing deps (no DB, no epic-runner recipe)\n"
                "  2   cost-pause sentinel encountered\n"); return 0
        else:
            sys.stderr.write(f"scheduler: unknown flag {a}\n"); return 2

    if not os.path.isfile(db):
        sys.stderr.write(f"scheduler: state.db not found at {db}\n"); return 1
    if not os.path.isdir(os.path.join(root, "recipes", recipe)):
        sys.stderr.write(f"scheduler: recipe not found: recipes/{recipe}\n"); return 1

    _ensure_priority_column(db)
    it = 0
    while True:
        if _cost_pause_active(home):
            sys.stderr.write("scheduler: cost-pause active — exiting\n"); return 2
        spent = today_cost_usd(db)
        if spent >= cap:
            sys.stderr.write(f"scheduler: 24h spend ${spent} ≥ cap ${cap} — refusing dispatch\n"); return 2
        nxt = pick_next_epic(db)
        if not nxt:
            if once:
                sys.stdout.write("scheduler: queue empty (--once) → exit 0\n"); return 0
            sys.stdout.write(f"scheduler: queue empty; idle {idle}s\n")
            time.sleep(idle); continue
        sys.stdout.write(f"scheduler: iter {it + 1} — next={nxt} spent=${spent} / cap=${cap}\n")
        dispatch_epic(db, nxt, root, home, recipe, dry_run)
        it += 1
        if once:
            return 0
        if max_iters > 0 and it >= max_iters:
            sys.stderr.write(f"scheduler: max-iters {max_iters} reached → exit 3\n"); return 3


if __name__ == "__main__":
    raise SystemExit(main())
