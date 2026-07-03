"""Autonomous multi-epic scheduler — Python port of bin/mini-ork-scheduler.

Faithful port of the pick/dispatch/verdict/cascade mechanics PLUS the win #1
concurrency seam: the bash scheduler computed the full ready-set and then threw
all but the first away (`_pick_next_epic | head -1`) and blocked on one epic at
a time — cross-epic parallelism was 1. This port dispatches a bounded worker
pool (MO_SCHED_MAX_PARALLEL, default 3) over the whole priority-ordered
ready-set; as each epic completes, its deps cascade and newly-unblocked epics
join the pool. Priority-inheritance (Track B5) ordering is preserved exactly.

Semantics kept from bash: budget cap over rolling-24h task_runs spend,
cost-pause sentinels, kickoff resolution order, verdict resolution from
{panel-verdict,verdict}.json, done->cascade / fail->escalated.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from mini_ork.ported import epic_graph


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def _conn(db: str | None) -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(db), timeout=30)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def ensure_priority_column(db: str | None = None) -> None:
    """Idempotent epics.priority migration (Track B5)."""
    con = _conn(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(epics)").fetchall()}
        if "priority" not in cols:
            con.execute("ALTER TABLE epics ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            con.commit()
    finally:
        con.close()


def effective_priority(epic_id: str, db: str | None = None) -> int:
    """eff(E) = max(base(E), max(base(W) for W transitively blocked on E)) —
    identical recursive CTE to the bash _epic_effective_priority."""
    con = _conn(db)
    try:
        row = con.execute("""
            WITH RECURSIVE inheritors(node) AS (
                SELECT id FROM epics WHERE id = ?
                UNION
                SELECT d.to_epic_id
                  FROM inheritors i
                  JOIN epic_dependencies d ON d.from_epic_id = i.node
                 WHERE d.kind = 'hard' AND d.resolved_at IS NULL
            )
            SELECT COALESCE(MAX(e.priority), 0)
              FROM inheritors i JOIN epics e ON e.id = i.node
        """, (epic_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def pick_ready(db: str | None = None) -> list[str]:
    """Priority-ordered ready-set (Track B5 inheritance; ties oldest-first).
    Same query as bash _pick_next_epic WITHOUT the LIMIT 1 — the pool consumes
    the whole list. Element 0 == what bash would have picked."""
    con = _conn(db)
    try:
        rows = con.execute("""
            WITH RECURSIVE inheritors(root, node) AS (
                SELECT e.id, e.id FROM epics e
                 WHERE e.status = 'not started' AND e.archived_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM epic_dependencies d
                        WHERE d.to_epic_id = e.id AND d.kind = 'hard'
                          AND d.resolved_at IS NULL)
                UNION
                SELECT i.root, d.to_epic_id
                  FROM inheritors i
                  JOIN epic_dependencies d ON d.from_epic_id = i.node
                 WHERE d.kind = 'hard' AND d.resolved_at IS NULL
            ),
            effective(root, eff) AS (
                SELECT root, COALESCE(MAX(e.priority), 0)
                  FROM inheritors i JOIN epics e ON e.id = i.node
                 GROUP BY root
            )
            SELECT e.id
              FROM epics e JOIN effective ef ON ef.root = e.id
             WHERE e.status = 'not started' AND e.archived_at IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM epic_dependencies d
                    WHERE d.to_epic_id = e.id AND d.kind = 'hard'
                      AND d.resolved_at IS NULL)
             ORDER BY ef.eff DESC, e.created_at ASC
        """).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def today_cost_usd(db: str | None = None) -> float:
    con = _conn(db)
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM task_runs "
            "WHERE created_at >= strftime('%s','now','-24 hours')").fetchone()
        return float(row[0] or 0)
    except sqlite3.OperationalError:
        return 0.0
    finally:
        con.close()


def cost_pause_active(home: str | None = None) -> bool:
    home = home or os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return (os.path.isfile(os.path.join(home, "cost-pause.sentinel"))
            or os.path.isfile(os.path.join(home, "control", "cost-pause")))


def resolve_kickoff(epic_id: str, root: str, recipe: str,
                    db: str | None = None) -> str | None:
    """kickoff_path column -> kickoffs/<id>.md -> recipe example (bash order)."""
    con = _conn(db)
    try:
        row = con.execute("SELECT kickoff_path FROM epics WHERE id=?",
                          (epic_id,)).fetchone()
    finally:
        con.close()
    kp = row[0] if row and row[0] else ""
    if kp and os.path.isfile(os.path.join(root, kp)):
        return os.path.join(root, kp)
    cand = os.path.join(root, "kickoffs", f"{epic_id}.md")
    if os.path.isfile(cand):
        return cand
    cand = os.path.join(root, "recipes", recipe, "example-kickoff.md")
    if os.path.isfile(cand):
        return cand
    return None


def _set_status(db: str | None, epic_id: str, status: str, note: str = "") -> None:
    con = _conn(db)
    try:
        if note:
            con.execute("UPDATE epics SET status=?, notes=COALESCE(notes,'') || ? "
                        "WHERE id=?", (status, note, epic_id))
        else:
            con.execute("UPDATE epics SET status=? WHERE id=?", (status, epic_id))
        con.commit()
    finally:
        con.close()


def _verdict_from_log(log_path: str, home: str) -> str:
    """run_id= line -> runs/<run_id>/{panel-verdict,verdict}.json -> verdict."""
    run_id = ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("run_id="):
                    run_id = line.strip().split("=", 1)[1]
                    break
    except OSError:
        return "unknown"
    if not run_id:
        return "unknown"
    for vfile in ("panel-verdict.json", "verdict.json"):
        p = os.path.join(home, "runs", run_id, vfile)
        if os.path.isfile(p):
            try:
                v = json.load(open(p, encoding="utf-8")).get("verdict", "")
                if v:
                    return v
            except (OSError, ValueError):
                continue
    return "unknown"


def dispatch_epic(epic_id: str, root: str, home: str, recipe: str,
                  db: str | None = None, dry_run: bool = False,
                  runner_cmd: list[str] | None = None) -> tuple[str, int]:
    """Mark in-progress, run the recipe, resolve verdict, update status +
    cascade. Returns (verdict, rc). `runner_cmd` overrides the runner argv
    (test seam); default is `<root>/bin/mini-ork run <recipe> <kickoff>`."""
    kickoff = resolve_kickoff(epic_id, root, recipe, db)
    if not kickoff:
        _set_status(db, epic_id, "escalated", " [scheduler: no kickoff]")
        return "no_kickoff", 1

    _set_status(db, epic_id, "in progress")
    log_dir = os.path.join(home, "runs", "scheduler")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"dispatch-{int(time.time())}-{epic_id}.log")

    if dry_run:
        _set_status(db, epic_id, "not started")
        return "dry_run", 0

    cmd = runner_cmd or [os.path.join(root, "bin", "mini-ork"), "run", recipe, kickoff]
    with open(log_path, "w", encoding="utf-8") as log:
        rc = subprocess.run(cmd + ([kickoff] if runner_cmd else []),
                            stdout=log, stderr=subprocess.STDOUT).returncode

    verdict = _verdict_from_log(log_path, home)
    if verdict in ("pass", "success"):
        _set_status(db, epic_id, "done")
        epic_graph.on_done(epic_id, db=db)
    else:
        _set_status(db, epic_id, "escalated")
    return verdict, rc


def run_pool(root: str, home: str, recipe: str = "epic-runner",
             db: str | None = None, max_parallel: int | None = None,
             max_iters: int = 0, budget_cap: float | None = None,
             dry_run: bool = False, runner_cmd: list[str] | None = None) -> int:
    """WIN #1 — bounded concurrent pool over the whole ready-set. Drains the
    queue: dispatches up to `max_parallel` epics at once, and as each finishes
    (cascading its deps), newly-ready epics join. Returns count dispatched.
    Budget/cost-pause are re-checked before every admission, like the bash loop."""
    if max_parallel is None:
        max_parallel = int(os.environ.get("MO_SCHED_MAX_PARALLEL", "3"))
    if budget_cap is None:
        budget_cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50.0"))
    ensure_priority_column(db)

    dispatched = 0
    in_flight: dict = {}
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while True:
            if cost_pause_active(home) or today_cost_usd(db) >= budget_cap:
                break
            ready = [e for e in pick_ready(db) if e not in
                     {v for v in in_flight.values()}]
            while ready and len(in_flight) < max_parallel and (
                    max_iters <= 0 or dispatched < max_iters):
                epic = ready.pop(0)
                fut = pool.submit(dispatch_epic, epic, root, home, recipe,
                                  db, dry_run, runner_cmd)
                in_flight[fut] = epic
                dispatched += 1
            if not in_flight:
                break  # queue drained
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                in_flight.pop(fut, None)
            if max_iters > 0 and dispatched >= max_iters and not in_flight:
                break
    return dispatched
