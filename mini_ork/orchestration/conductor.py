"""Python port of bin/mini-ork-conductor — meta-orchestrator.

Strangler-fig parity port. Per tick: pick the next ready epic (via ported
epic_graph), decide topology + lane hints + prompt/plasticity gates from the
learning tables (Mass win-rate + InfraMind budget-bias + GRPO lane advantage),
log to conductor_decisions, then dispatch via the scheduler. The decision block
is transcribed verbatim from the bash's embedded python.

    decide_for_epic(db, epic_id, spent, cap, plasticity) -> dict
    main(argv=None, *, db=None, root=None) -> int
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

from mini_ork.orchestration import epic_graph


def _today_cost(db):
    # bash: `sqlite3 … 2>/dev/null || echo 0` — tolerate a missing task_runs table /
    # empty or absent db (a fresh .mini-ork), returning 0 instead of crashing the loop.
    try:
        c = sqlite3.connect(db)
        r = c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM task_runs "
                      "WHERE created_at >= strftime('%s','now','-24 hours')").fetchone()
        c.close()
        return float(r[0] or 0)
    except sqlite3.Error:
        return 0.0


def _adaptive_lane_gain(con, base=0.3):
    """Fit the lane-advantage gain from conductor_decisions.realized_score.

    Mirror of the embedded-python helper in bin/mini-ork-conductor. Reads back
    the realized_score history (written post-run by the learning loop) and
    calibrates the gain via an EMA of past outcomes: ema==0.5 → gain==base,
    good track record widens it, poor track record damps it. Cold-safe: fewer
    than MO_CONDUCTOR_GAIN_MIN_SAMPLES resolved decisions keeps the historical
    default (0.3). Opt-out MO_CONDUCTOR_ADAPTIVE_GAIN=0. Bounded to [0.1, 0.6].
    """
    if os.environ.get("MO_CONDUCTOR_ADAPTIVE_GAIN", "1") != "1":
        return base
    try:
        min_samples = int(os.environ.get("MO_CONDUCTOR_GAIN_MIN_SAMPLES", "3"))
    except ValueError:
        min_samples = 3
    try:
        rows = con.execute(
            "SELECT realized_score FROM conductor_decisions "
            "WHERE realized_score IS NOT NULL ORDER BY decided_at ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return base
    scores = []
    for r in rows:
        try:
            scores.append(float(r[0]))
        except (TypeError, ValueError):
            continue
    if len(scores) < min_samples:
        return base
    alpha = 0.3
    ema = scores[0]
    for s in scores[1:]:
        ema = alpha * s + (1.0 - alpha) * ema
    gain = base * (2.0 * ema)
    return max(0.1, min(0.6, gain))


def decide_for_epic(db, epic_id, spent, cap, plast_budget) -> dict:
    budget_pct = (spent / cap) if cap > 0 else 0.0
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000"); con.row_factory = sqlite3.Row
    epic = con.execute("SELECT id, title, kickoff_path FROM epics WHERE id=?", (epic_id,)).fetchone()
    if not epic:
        con.close(); return {"error": "epic_not_found", "epic_id": epic_id}

    task_class = "framework_edit"
    try:
        tc = con.execute("SELECT task_class FROM task_runs WHERE kickoff_path=? "
                         "ORDER BY created_at DESC LIMIT 1", (epic["kickoff_path"] or "",)).fetchone()
        if tc and tc["task_class"]:
            task_class = tc["task_class"]
    except sqlite3.OperationalError:
        pass

    topology, topology_rationale = None, ""
    try:
        if budget_pct > 0.70:
            rows = con.execute("""
                SELECT t.topology_id, t.workflow_name, t.win_rate, t.sample_size, t.avg_cost_usd,
                       COALESCE(wm.status,'unknown') AS wf_status
                  FROM topology_win_rates t LEFT JOIN workflow_memory wm ON wm.yaml_hash = t.topology_id
                 WHERE t.task_class=? AND t.sample_size >= 3
                   AND (wm.status IS NULL OR wm.status IN ('promoted','shadow'))
                 ORDER BY t.avg_cost_usd ASC, t.win_rate DESC LIMIT 1""", (task_class,)).fetchone()
            topology_rationale = f"budget pressure {budget_pct:.0%} > 70%; biasing toward lowest avg_cost (InfraMind 2606.11440)."
        else:
            rows = con.execute("""
                SELECT t.topology_id, t.workflow_name, t.win_rate, t.sample_size, t.avg_cost_usd,
                       COALESCE(wm.status,'unknown') AS wf_status
                  FROM topology_win_rates t LEFT JOIN workflow_memory wm ON wm.yaml_hash = t.topology_id
                 WHERE t.task_class=? AND t.sample_size >= 3
                   AND (wm.status IS NULL OR wm.status IN ('promoted','shadow'))
                 ORDER BY t.win_rate DESC, t.sample_size DESC LIMIT 1""", (task_class,)).fetchone()
            topology_rationale = "highest win_rate with sample_size >= 3, stability-anchored (Mass 2502.02533 + workflow_memory.status filter)."
        if rows:
            topology = {"topology_id": rows["topology_id"],
                        "workflow_name": rows["workflow_name"] or task_class.replace("_", "-"),
                        "win_rate": rows["win_rate"], "sample_size": rows["sample_size"]}
    except sqlite3.OperationalError:
        pass

    lane_hints = {}
    try:
        for node_type in ("planner", "researcher", "reviewer", "implementer", "verifier"):
            r = con.execute("""
                SELECT agent_version_id, relative_advantage FROM agent_performance_memory
                 WHERE task_class=? AND runs_count >= 3 AND (role=? OR model=?)
                 ORDER BY relative_advantage DESC, runs_count DESC LIMIT 1""",
                            (task_class, node_type, node_type)).fetchone()
            if r and r["relative_advantage"] > 0:
                lane_hints[node_type] = r["agent_version_id"]
    except sqlite3.OperationalError:
        pass

    op = con.execute("SELECT COUNT(*) AS n FROM role_evolver_log WHERE status='open' "
                     "AND (target_recipe=? OR target_recipe='*')",
                     ((topology or {}).get("workflow_name") or "*",)).fetchone()
    open_proposals_n = op["n"] if op else 0
    mut_today = con.execute("SELECT COUNT(*) AS n FROM role_evolver_log "
                            "WHERE applied_at >= strftime('%s','now','-24 hours')").fetchone()["n"]
    plasticity_remaining = max(0, plast_budget - mut_today)

    lane_gain = _adaptive_lane_gain(con, 0.3)
    predicted = (topology or {}).get("win_rate", 0.5) or 0.5
    lane_adv_sum = 0.0
    try:
        if lane_hints:
            placeholders = ",".join(["?"] * len(lane_hints))
            for r in con.execute(f"""SELECT agent_version_id, relative_advantage
                    FROM agent_performance_memory WHERE task_class=? AND agent_version_id IN ({placeholders})""",
                                 (task_class, *lane_hints.values())).fetchall():
                if r["relative_advantage"] > 0:
                    lane_adv_sum += r["relative_advantage"]
    except sqlite3.OperationalError:
        pass
    predicted = min(1.0, predicted * (1.0 + lane_adv_sum * lane_gain))
    con.close()
    return {"epic_id": epic_id, "task_class": task_class, "topology": topology,
            "lane_hints": lane_hints, "open_role_proposals": open_proposals_n,
            "plasticity_remaining": plasticity_remaining, "predicted_score": round(predicted, 4),
            "budget_pct_used": round(budget_pct, 4), "topology_rationale": topology_rationale}


def log_decision(db, dec):
    con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
    con.execute("""INSERT INTO conductor_decisions
        (decided_at, epic_id, task_class, chosen_topology, chosen_recipe, chosen_lane_hints,
         predicted_score, budget_pct_used, rationale, outcome)
        VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
        (int(time.time()), dec.get("epic_id"), dec.get("task_class"),
         (dec.get("topology") or {}).get("topology_id"),
         (dec.get("topology") or {}).get("workflow_name"),
         json.dumps(dec.get("lane_hints") or {}),
         float(dec.get("predicted_score") or 0.0), float(dec.get("budget_pct_used") or 0.0),
         dec.get("topology_rationale", "")[:600]))
    con.commit(); con.close()


def main(argv: list[str] | None = None, *, db: str | None = None, root: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = root or os.environ.get("MINI_ORK_ROOT") or os.getcwd()
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(root, ".mini-ork")
    db = db or os.environ.get("MINI_ORK_DB") or os.path.join(home, "state.db")
    cap = float(os.environ.get("MO_DAILY_BUDGET_USD", "50.0"))
    plast = int(os.environ.get("MO_PLASTICITY_BUDGET", "5"))
    once = max_iters = idle = dry_run = explain = 0
    idle = 60
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--once": once = 1; i += 1
        elif a == "--max-iters": max_iters = int(argv[i + 1]); i += 2
        elif a == "--idle-secs": idle = int(argv[i + 1]); i += 2
        elif a == "--dry-run": dry_run = 1; i += 1
        elif a == "--explain": explain = 1; i += 1
        elif a in ("--help", "-h"):
            sys.stdout.write(
                "mini-ork conductor — meta-orchestrator (mini-ork-of-mini-orks).\n\n"
                "Phase 2 of the design grounded in Shepherd (arXiv:2605.10913),\n"
                "TaskWeave (2606.01199), TacoMAS (2605.09539), Mass (2502.02533),\n"
                "InfraMind (2606.11440) and TCP-MCP (2605.27850).\n\n"
                "Per tick:\n"
                "  1. Read state of: epic queue, prompt_win_rates, agent_performance_memory,\n"
                "     topology_win_rates, role_evolver_log proposals, 24h cost vs cap.\n"
                "  2. Pick the next epic from epic_graph_ready_now.\n"
                "  3. Decide:\n"
                "       - Recipe / topology  (highest win_rate per task_class, ties broken\n"
                "         by lower avg_cost; falls back to the epic's kickoff hint)\n"
                "       - Lane hints         (highest relative_advantage per (lane, task_class))\n"
                "       - Prompt seeds       (top-quartile prompt_version_hash for the role)\n"
                "     Under high budget pressure (>70% spend), prefer cheaper topologies\n"
                "     with sample_size >= 3 over higher-win-rate but expensive ones\n"
                "     (InfraMind-style budget bias).\n"
                "  4. Write the decision to conductor_decisions.\n"
                "  5. Stage the lane hints + prompt seeds into env vars and call\n"
                "     bin/mini-ork-scheduler --once.\n\n"
                "Modes:\n"
                "  --once               One decision + dispatch then exit\n"
                "  --max-iters N        Bound iterations (default unbounded)\n"
                "  --idle-secs N        Poll interval when queue empty (default 60)\n"
                "  --dry-run            Decide + log but skip dispatch\n"
                "  --explain            Emit a verbose rationale per decision\n"
                "  --help\n\n"
                "All decisions logged to conductor_decisions. Re-runnable.\n"); return 0
        else:
            sys.stderr.write(f"conductor: unknown flag {a}\n"); return 2

    it = 0
    while True:
        spent = _today_cost(db)
        ready = epic_graph.ready_now(db) if hasattr(epic_graph, "ready_now") else []
        nxt = (ready or [None])[0]
        if not nxt:
            # bash: _tick returns 2 on empty queue; sourced-lib errexit aborts the
            # whole script with 2 before the --once/idle checks can run.
            sys.stdout.write("conductor: queue empty\n"); return 2
        dec = decide_for_epic(db, nxt, spent, cap, plast)
        if explain or dry_run:
            sys.stdout.write(f"conductor: decision for {nxt}:\n")
            sys.stdout.write("  " + json.dumps(dec, indent=4).replace("\n", "\n  ") + "\n")
        else:
            recipe = (dec.get("topology") or {}).get("workflow_name", "") or ""
            sys.stdout.write(f"conductor: chose recipe={recipe} for {nxt} spent=${spent}\n")
        log_decision(db, dec)
        if not dry_run:
            recipe = (dec.get("topology") or {}).get("workflow_name", "") or ""
            env = {**os.environ}
            if recipe:
                env["MO_SCHED_RECIPE"] = recipe
                env["MO_CONDUCTOR_LANE_HINTS"] = json.dumps(dec.get("lane_hints", {}))
            subprocess.run([os.path.join(root, "bin", "mini-ork-scheduler"),
                            "--once", "--max-iters", "1"], env=env)
        it += 1
        if once:
            return 0
        if max_iters > 0 and it >= max_iters:
            sys.stdout.write(f"conductor: max-iters {max_iters} reached\n"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
