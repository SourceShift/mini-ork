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

from mini_ork.orchestration import epic_graph


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
  4   a pre-dispatch hook deferred (exit 75): a precondition is down; nothing consumed

Retry loop (all opt-in; defaults keep the historic one-shot behaviour):
  MO_SCHED_MAX_ATTEMPTS=N       failed epics return to 'not started' until N
                                attempts are used (per-epic epics.max_attempts
                                overrides); then 'escalated'. Default 1.
  MO_SCHED_CARRY_OVER=1         a retry's kickoff = original + "Previous attempts"
                                (verdict, failing verifiers, reflection excerpt).
  MO_SCHED_REQUIRED_VERIFIERS   comma list; 'done' also needs
                                runs/<run>/verifier_<name>.json to pass.
  MO_SCHED_PRE_DISPATCH_HOOK    executable; exit 0 = go, 75 = defer (requeue,
                                no attempt used, stop admitting), other = failed attempt.
  MO_SCHED_POST_VERDICT_HOOK    executable; exit 0 = accept the outcome,
                                10 = hold (status 'blocked', never auto-retried),
                                other = failed attempt.
  epics.recipe                  per-epic recipe; overrides MO_SCHED_RECIPE.
Hooks get MO_EPIC_ID, MO_EPIC_ATTEMPT, MO_EPIC_KICKOFF, MO_EPIC_RECIPE and,
post-verdict, MO_RUN_ID, MO_RUN_DIR, MO_VERDICT, MO_OUTCOME, MO_OUTCOME_REASON.
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


_RETRY_COLUMNS = (
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("max_attempts", "INTEGER"),
    ("recipe", "TEXT"),
    ("held_reason", "TEXT"),
    ("last_run_id", "TEXT"),
)


def ensure_retry_schema(db: str | None = None) -> None:
    """Idempotent runtime migration for the retry loop — same pattern as
    ensure_priority_column: new epics columns + an epic_attempts history table.
    No-op when the epics table does not exist yet."""
    con = _conn(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(epics)").fetchall()}
        if not cols:
            return
        for name, decl in _RETRY_COLUMNS:
            if name not in cols:
                con.execute(f"ALTER TABLE epics ADD COLUMN {name} {decl}")
        con.execute("""
            CREATE TABLE IF NOT EXISTS epic_attempts (
              epic_id      TEXT NOT NULL,
              attempt      INTEGER NOT NULL,
              run_id       TEXT,
              recipe       TEXT,
              kickoff_path TEXT,
              verdict      TEXT,
              outcome      TEXT,
              reason       TEXT,
              started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              finished_at  TEXT,
              PRIMARY KEY (epic_id, attempt)
            )""")
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
    the whole list. Within one priority tier the LEAST-attempted epic goes first,
    so a retrying epic can never starve the others (a first-failed-first retry
    rule spun one step nine times while four other failures waited)."""
    try:
        ensure_retry_schema(db)
    except sqlite3.OperationalError:
        return []
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
             ORDER BY ef.eff DESC, COALESCE(e.attempts, 0) ASC, e.created_at ASC
        """).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def today_cost_usd(db: str | None = None) -> float:
    """Rolling-24h spend, in dollars, as the daily budget guard sees it.

    Sums ``llm_calls`` — the per-dispatch ledger every provider call writes,
    node or stage — rather than ``task_runs.cost_usd``. task_runs only carries
    what a node handler explicitly charged, so stage spend (reflect /
    gradient-extract, the jury, the lens panel) and any child that never
    reached a charge call were invisible to the meter: it read $0.00 against
    $3.13 of real dispatch spend, and a budget circuit that cannot see spend
    cannot stop it.

    Falls back to the task_runs sum when llm_calls is missing, so a DB behind
    the migrator degrades to the old estimate rather than to a blind zero."""
    con = _conn(db)
    try:
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls "
                "WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S','now','-24 hours')"
            ).fetchone()
            return float(row[0] or 0)
        except sqlite3.OperationalError:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM task_runs "
                "WHERE created_at >= strftime('%s','now','-24 hours')").fetchone()
            return float(row[0] or 0)
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


def _run_id_from_log(log_path: str) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("run_id="):
                    return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return ""


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


_PASS_WORDS = {"pass", "passed", "success", "proven", "ok", "true"}
_DEFER_RC = 75   # EX_TEMPFAIL — a pre-dispatch precondition is down
_HOLD_RC = 10    # post-verdict hook: hold for a human


def _epic_row(db: str | None, epic_id: str) -> dict:
    con = _conn(db)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM epics WHERE id=?", (epic_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _max_attempts(epic: dict) -> int:
    per_epic = epic.get("max_attempts")
    if per_epic:
        return max(1, int(per_epic))
    return max(1, int(os.environ.get("MO_SCHED_MAX_ATTEMPTS", "1") or 1))


def _verifier_passes(payload: dict) -> bool:
    if payload.get("pass") is True:
        return True
    return str(payload.get("status") or payload.get("verdict") or "").lower() in _PASS_WORDS


def check_required_verifiers(run_dir: str) -> tuple[bool, str]:
    """MO_SCHED_REQUIRED_VERIFIERS contract: every named verifier must have
    written runs/<run>/verifier_<name>.json and it must pass. Missing counts
    as failing — a gate that never ran cannot vouch for anything."""
    names = [n.strip() for n in os.environ.get("MO_SCHED_REQUIRED_VERIFIERS", "").split(",") if n.strip()]
    for name in names:
        path = os.path.join(run_dir, f"verifier_{name}.json")
        try:
            payload = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return False, f"required verifier {name}: no result at {path}"
        if not _verifier_passes(payload):
            why = payload.get("reason") or payload.get("status") or "failed"
            return False, f"required verifier {name}: {why}"
    return True, ""


def _failing_verifiers(run_dir: str) -> list[str]:
    out = []
    try:
        names = sorted(f for f in os.listdir(run_dir) if f.startswith("verifier_") and f.endswith(".json"))
    except OSError:
        return out
    for f in names:
        try:
            payload = json.load(open(os.path.join(run_dir, f), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not _verifier_passes(payload):
            why = payload.get("reason") or payload.get("status") or "failed"
            out.append(f"- `{f[len('verifier_'):-5]}`: {str(why)[:300]}")
    return out


def _excerpt(run_dir: str, limit: int = 4000) -> str:
    for name in ("reflection.md", "cycle-report.md"):
        path = os.path.join(run_dir, name)
        if os.path.isfile(path):
            text = open(path, encoding="utf-8", errors="replace").read().strip()
            return f"From `{name}`:\n\n" + (text[:limit] + ("\n…(truncated)" if len(text) > limit else ""))
    return ""


def build_carry_over_kickoff(epic_id: str, base_kickoff: str, attempt: int,
                             home: str, db: str | None = None, keep: int = 2) -> str:
    """Attempt N's kickoff = the original kickoff + what attempts N-1.. measured.
    The recursion of an RSI loop: the next try plans from the last failure."""
    con = _conn(db)
    try:
        prior = con.execute(
            "SELECT attempt, run_id, verdict, outcome, reason FROM epic_attempts "
            "WHERE epic_id=? AND attempt<? ORDER BY attempt DESC LIMIT ?",
            (epic_id, attempt, keep)).fetchall()
    finally:
        con.close()
    if not prior:
        return base_kickoff
    lines = [open(base_kickoff, encoding="utf-8", errors="replace").read().rstrip(), "",
             f"## Previous attempts (this is attempt {attempt})", "",
             "These were measured and rejected — change the approach, not the wording.", ""]
    for n, run_id, verdict, outcome, reason in prior:
        lines += [f"### Attempt {n} — {outcome or verdict or 'unknown'}", "",
                  f"- verdict: `{verdict}`", f"- reason: {reason or '(none recorded)'}"]
        run_dir = os.path.join(home, "runs", run_id) if run_id else ""
        if run_dir and os.path.isdir(run_dir):
            lines.append(f"- run dir: `{run_dir}`")
            failing = _failing_verifiers(run_dir)
            if failing:
                lines += ["", "Failing verifiers:"] + failing
            ex = _excerpt(run_dir)
            if ex:
                lines += ["", ex]
        lines.append("")
    out_dir = os.path.join(home, "runs", "scheduler", "kickoffs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{epic_id}-attempt-{attempt}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _run_hook(var: str, env_extra: dict, log_path: str) -> tuple[int, str] | None:
    hook = os.environ.get(var, "").strip()
    if not hook:
        return None
    env = {**os.environ, **{k: str(v) for k, v in env_extra.items()}}
    proc = subprocess.run([hook], env=env, capture_output=True, text=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"[{var}] rc={proc.returncode}\n{proc.stdout}{proc.stderr}\n")
    lines = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
    return proc.returncode, (lines[-1] if lines else "")


def _record_attempt(db: str | None, epic_id: str, attempt: int, **fields) -> None:
    con = _conn(db)
    try:
        con.execute(
            "INSERT OR REPLACE INTO epic_attempts (epic_id, attempt, run_id, recipe, "
            "kickoff_path, verdict, outcome, reason, finished_at) VALUES "
            "(?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (epic_id, attempt, fields.get("run_id"), fields.get("recipe"),
             fields.get("kickoff_path"), fields.get("verdict"), fields.get("outcome"),
             fields.get("reason")))
        con.execute("UPDATE epics SET attempts=?, last_run_id=COALESCE(?, last_run_id) WHERE id=?",
                    (attempt, fields.get("run_id"), epic_id))
        con.commit()
    finally:
        con.close()


def _settle(db: str | None, epic_id: str, epic: dict, attempt: int, outcome: str, reason: str) -> str:
    """Map an attempt's outcome to the epic's next status."""
    if outcome == "done":
        _set_status(db, epic_id, "done")
        epic_graph.on_done(epic_id, db=db)
        return "done"
    if outcome == "held":
        con = _conn(db)
        try:
            con.execute("UPDATE epics SET status='blocked', held_reason=? WHERE id=?", (reason, epic_id))
            con.commit()
        finally:
            con.close()
        return "blocked"
    if attempt < _max_attempts(epic):
        _set_status(db, epic_id, "not started", f" [scheduler: attempt {attempt} failed — retrying]")
        return "not started"
    _set_status(db, epic_id, "escalated", f" [scheduler: {attempt} attempt(s) failed]")
    return "escalated"


def dispatch_epic(epic_id: str, root: str, home: str, recipe: str,
                  db: str | None = None, dry_run: bool = False,
                  runner_cmd: list[str] | None = None) -> tuple[str, int]:
    """Mark in-progress, run the recipe, resolve verdict, update status +
    cascade. Returns (verdict, rc). `runner_cmd` overrides the runner argv
    (test seam); default is `<root>/bin/mini-ork run <recipe> <kickoff>`.

    Retry loop (opt-in, see _USAGE): per-epic recipe, carry-over kickoff,
    required verifiers, pre/post hooks, attempts with a cap."""
    ensure_retry_schema(db)
    epic = _epic_row(db, epic_id)
    recipe = (epic.get("recipe") or "").strip() or recipe
    kickoff = resolve_kickoff(epic_id, root, recipe, db)
    if not kickoff:
        _set_status(db, epic_id, "escalated", " [scheduler: no kickoff]")
        return "no_kickoff", 1

    attempt = int(epic.get("attempts") or 0) + 1
    log_dir = os.path.join(home, "runs", "scheduler")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"dispatch-{int(time.time())}-{epic_id}-a{attempt}.log")

    if dry_run:
        sys.stdout.write(
            f"  [dry-run] would dispatch: {root}/bin/mini-ork run {recipe} {kickoff}\n"
        )
        return "dry_run", 0

    if attempt > 1 and os.environ.get("MO_SCHED_CARRY_OVER", "1") != "0":
        kickoff = build_carry_over_kickoff(epic_id, kickoff, attempt, home, db)

    hook_env = {"MO_EPIC_ID": epic_id, "MO_EPIC_ATTEMPT": attempt,
                "MO_EPIC_KICKOFF": kickoff, "MO_EPIC_RECIPE": recipe}
    pre = _run_hook("MO_SCHED_PRE_DISPATCH_HOOK", hook_env, log_path)
    if pre is not None and pre[0] == _DEFER_RC:
        _set_status(db, epic_id, "not started", " [scheduler: deferred by pre-dispatch hook]")
        return "deferred", _DEFER_RC
    if pre is not None and pre[0] != 0:
        reason = f"pre-dispatch hook rc={pre[0]}: {pre[1]}"
        _record_attempt(db, epic_id, attempt, recipe=recipe, kickoff_path=kickoff,
                        verdict="not_run", outcome="failed", reason=reason)
        _settle(db, epic_id, epic, attempt, "failed", reason)
        return "hook_failed", pre[0]

    _set_status(db, epic_id, "in progress")
    cmd = runner_cmd or [os.path.join(root, "bin", "mini-ork"), "run", recipe, kickoff]
    with open(log_path, "a", encoding="utf-8") as log:
        rc = subprocess.run(cmd + ([kickoff] if runner_cmd else []),
                            stdout=log, stderr=subprocess.STDOUT).returncode

    verdict = _verdict_from_log(log_path, home)
    run_id = _run_id_from_log(log_path)
    run_dir = os.path.join(home, "runs", run_id) if run_id else ""
    if verdict in ("pass", "success"):
        ok, why = check_required_verifiers(run_dir) if run_dir else (
            not os.environ.get("MO_SCHED_REQUIRED_VERIFIERS", "").strip(), "no run dir")
        outcome, reason = ("done", "") if ok else ("failed", why)
    else:
        outcome, reason = "failed", f"verdict={verdict} rc={rc}"

    post = _run_hook("MO_SCHED_POST_VERDICT_HOOK", {
        **hook_env, "MO_RUN_ID": run_id, "MO_RUN_DIR": run_dir, "MO_VERDICT": verdict,
        "MO_OUTCOME": outcome, "MO_OUTCOME_REASON": reason}, log_path)
    if post is not None:
        if post[0] == _HOLD_RC:
            outcome, reason = "held", post[1] or "held by post-verdict hook"
        elif post[0] != 0:
            outcome, reason = "failed", f"post-verdict hook rc={post[0]}: {post[1]}"

    _record_attempt(db, epic_id, attempt, run_id=run_id or None, recipe=recipe,
                    kickoff_path=kickoff, verdict=verdict, outcome=outcome, reason=reason)
    _settle(db, epic_id, epic, attempt, outcome, reason)
    return verdict, rc


def run_pool(root: str, home: str, recipe: str = "epic-runner",
             db: str | None = None, max_parallel: int | None = None,
             max_iters: int = 0, budget_cap: float | None = None,
             dry_run: bool = False, runner_cmd: list[str] | None = None,
             stats: dict | None = None) -> int:
    """WIN #1 — bounded concurrent pool over the whole ready-set. Drains the
    queue: dispatches up to `max_parallel` epics at once, and as each finishes
    (cascading its deps), newly-ready epics join. Returns count dispatched.
    Budget/cost-pause are re-checked before every admission, like the bash loop."""
    if max_parallel is None:
        max_parallel = int(os.environ.get("MO_SCHED_MAX_PARALLEL", "3"))
    if budget_cap is None:
        budget_cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50.0"))
    ensure_priority_column(db)
    ensure_retry_schema(db)

    dispatched = 0
    deferred = False
    in_flight: dict = {}
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while True:
            if cost_pause_active(home) or today_cost_usd(db) >= budget_cap:
                break
            ready = [] if deferred else [e for e in pick_ready(db) if e not in
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
                try:
                    if fut.result()[0] == "deferred":
                        deferred = True   # a precondition is down: stop admitting
                except Exception:  # noqa: BLE001 — a crashed dispatch is not a deferral
                    pass
            if max_iters > 0 and dispatched >= max_iters and not in_flight:
                break
    if stats is not None:
        stats["deferred"] = deferred
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
    ensure_retry_schema(db)
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
        stats: dict = {}
        dispatched = run_pool(
            root,
            home,
            recipe,
            db=db,
            max_iters=remaining,
            budget_cap=budget_cap,
            dry_run=dry_run,
            runner_cmd=runner_cmd,
            stats=stats,
        )
        dispatched_total += dispatched
        if stats.get("deferred"):
            sys.stderr.write("scheduler: a pre-dispatch hook deferred (rc 75) → exit 4\n")
            return 4

        if once:
            return 0
        if max_iters > 0 and dispatched_total >= max_iters:
            sys.stderr.write(f"scheduler: max-iters {max_iters} reached → exit 3\n")
            return 3


if __name__ == "__main__":
    raise SystemExit(main())
