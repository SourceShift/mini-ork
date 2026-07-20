"""Canonical autonomous multi-epic scheduler.

Faithful port of the pick/dispatch/verdict/cascade mechanics PLUS the win #1
concurrency seam: the bash scheduler computed the full ready-set and then threw
all but the first away (`_pick_next_epic | head -1`) and blocked on one epic at
a time — cross-epic parallelism was 1. This port dispatches a bounded worker
pool (MO_SCHED_MAX_PARALLEL, default 3) over the whole priority-ordered
ready-set; as each epic completes, its deps cascade and newly-unblocked epics
join the pool. Priority-inheritance (Track B5) ordering is preserved exactly.

Public semantics: budget cap over rolling-24h task_runs spend,
cost-pause sentinels, kickoff resolution order, verdict resolution from
{panel-verdict,verdict}.json, done->cascade / fail->escalated.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from mini_ork.ported import epic_graph


_USAGE = """mini-ork scheduler — autonomous multi-epic delivery loop.

Pulls the next-ready epic from `epics` (status='not started' AND all hard
deps resolved) and dispatches it via `mini-ork run epic-runner <kickoff>`.
On verdict=success, marks epic 'done' and cascades dep resolution. On
failure, marks epic 'escalated' (visible in `mini-ork-epics list`).

Flags:
  --once               Run a single pick→dispatch→verdict cycle then exit
  --idle-secs N        Sleep N seconds between empty-queue polls (default 60)
  --max-iters N        Hard stop after N dispatches (default unlimited)
  --budget-cap-usd X   Daily cost cap; refuses to dispatch when exceeded
                       (defaults to MO_DAILY_BUDGET_USD or 50.0)
  --dry-run            Print what would be dispatched, do not invoke runner
  --help

Exit codes:
  0   queue drained (no ready epics) OR --once cycle finished cleanly
  1   fatal: missing deps (no DB, no epic-runner recipe)
  2   cost-pause sentinel encountered or budget cap reached
  3   max-iters reached
"""


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
        sys.stdout.write(
            f"  [dry-run] would dispatch: {root}/bin/mini-ork run {recipe} {kickoff}\n"
        )
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


def main(
    argv: list[str] | None = None,
    *,
    db: str | None = None,
    root: str | None = None,
    home: str | None = None,
    runner_cmd: list[str] | None = None,
) -> int:
    """Run the scheduler CLI over the canonical concurrent scheduler core.

    ``--once`` admits exactly one epic. Normal operation drains ready work with
    ``MO_SCHED_MAX_PARALLEL`` workers, then resumes the historic idle loop.
    ``runner_cmd`` is an acceptance-test seam; production uses ``bin/mini-ork``.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    home = home or os.environ.get("MINI_ORK_HOME") or os.path.join(root, ".mini-ork")
    db = db or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    recipe = os.environ.get("MO_SCHED_RECIPE", "epic-runner")
    once = False
    idle_secs = 60
    max_iters = 0
    dry_run = False
    budget_cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50.0"))

    def value_after(index: int, flag: str) -> str | None:
        if index + 1 < len(args):
            return args[index + 1]
        sys.stderr.write(f"scheduler: {flag} requires a value\n")
        return None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--once":
            once = True
            i += 1
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg in {"--help", "-h"}:
            sys.stdout.write(_USAGE)
            return 0
        elif arg in {"--idle-secs", "--max-iters", "--budget-cap-usd"}:
            value = value_after(i, arg)
            if value is None:
                return 2
            try:
                if arg == "--idle-secs":
                    idle_secs = int(value)
                elif arg == "--max-iters":
                    max_iters = int(value)
                else:
                    budget_cap = float(value)
            except ValueError:
                sys.stderr.write(f"scheduler: invalid value for {arg}: {value}\n")
                return 2
            i += 2
        else:
            sys.stderr.write(f"scheduler: unknown flag {arg}\n")
            return 2

    if not os.path.isfile(db):
        sys.stderr.write(f"scheduler: state.db not found at {db}\n")
        return 1
    if not os.path.isdir(os.path.join(root, "recipes", recipe)):
        sys.stderr.write(f"scheduler: recipe not found: recipes/{recipe}\n")
        return 1

    ensure_priority_column(db)
    dispatched_total = 0
    while True:
        if cost_pause_active(home):
            sys.stderr.write("scheduler: cost-pause active — exiting\n")
            return 2
        spent = today_cost_usd(db)
        if spent >= budget_cap:
            sys.stderr.write(
                f"scheduler: 24h spend ${spent} ≥ cap ${budget_cap} — refusing dispatch\n"
            )
            return 2

        ready = pick_ready(db)
        if not ready:
            if once:
                sys.stdout.write("scheduler: queue empty (--once) → exit 0\n")
                return 0
            sys.stdout.write(f"scheduler: queue empty; idle {idle_secs}s\n")
            time.sleep(idle_secs)
            continue

        sys.stdout.write(
            f"scheduler: iter {dispatched_total + 1} — next={ready[0]} "
            f"spent=${spent} / cap=${budget_cap}\n"
        )
        remaining = 1 if once else (
            max_iters - dispatched_total if max_iters > 0 else 0
        )
        dispatched = run_pool(
            root,
            home,
            recipe,
            db=db,
            max_iters=remaining,
            budget_cap=budget_cap,
            dry_run=dry_run,
            runner_cmd=runner_cmd,
        )
        dispatched_total += dispatched

        if once:
            return 0
        if max_iters > 0 and dispatched_total >= max_iters:
            sys.stderr.write(f"scheduler: max-iters {max_iters} reached → exit 3\n")
            return 3


if __name__ == "__main__":
    raise SystemExit(main())
