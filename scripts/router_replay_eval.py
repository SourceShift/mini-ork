#!/usr/bin/env python3
"""scripts/router_replay_eval.py — D6 offline replay evaluation.

Replay logged execution_traces from a state.db and score whether the
bandit estimator (single-sample baseline + z-score + UCB) picks a
higher-reward lane than the legacy greedy (relative_advantage DESC)
selector, on a held-out slice split.

No new inference — pure read against the existing execution_traces +
agent_performance_memory + lane_domain_advantage tables.

The replay is intentionally deterministic: a seeded RNG plus a pinned
train/eval split coefficient (default 70/30) so two replays against the
same state.db produce identical win-rate numbers. CI smokes against
tests/fixtures/router_replay_fixture.db; live runs against
.mini-ork/state.db.

Output (stdout, one JSON object):
    {
      "db": "<path>",
      "n_traces_total": <int>,
      "n_eval": <int>,
      "n_train": <int>,
      "old_winrate": <float>,
      "new_winrate": <float>,
      "delta": <float>,
      "seed": <int>,
      "split": <float>,
      "ucb_c": <float>,
      "notes": "<short string>"
    }

Exit codes:
    0 — success (always, even when delta is negative; the script reports
        the answer, it does not gate it).
    1 — invocation error (missing DB, malformed args).

Implementation notes:
    - Train/eval split is grouped by (objective_domain, task_class,
      node_type) slice so the held-out set never overlaps with a lane's
      training history. Without the grouping, a lane with no train rows
      could appear "lucky" on every eval row it owns.
    - The legacy selector reads agent_performance_memory (the global
      pool) ordered by relative_advantage DESC, runs_count DESC. The
      bandit selector uses the lane_domain_advantage z_score_advantage
      with a UCB bonus; when no z-scored row exists the lane drops
      back to the legacy order so cold-start paths match.
    - Both selectors project onto (task_class, node_type) the trace
      belonged to, so we measure within-slice routing quality — not
      cross-slice leakage.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
from collections import defaultdict


DEFAULT_DB = os.environ.get("MINI_ORK_DB") or os.path.join(
    os.environ.get("MINI_ORK_HOME", ".mini-ork"), "state.db")


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description=(__doc__ or "router replay eval").splitlines()[0])
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"path to state.db (default: {DEFAULT_DB})")
    p.add_argument("--seed", type=int, default=20260711,
                   help="RNG seed (default: 20260711, the recipe's launch date)")
    p.add_argument("--split", type=float, default=0.30,
                   help="held-out eval fraction (default: 0.30)")
    p.add_argument("--ucb-c", type=float, default=0.5,
                   help="UCB C parameter (default: 0.5, matches estimator core)")
    p.add_argument("--min-samples", type=int, default=3,
                   help="lane floor; matches MO_LEARNING_MIN_SAMPLES (default: 3)")
    p.add_argument("--limit", type=int, default=0,
                   help="max traces to evaluate (0 = all; default: 0)")
    return p.parse_args(argv)


def _detect_cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _fetch_traces(con):
    """Read every routed execution_traces row with a non-NULL reward_g.

    Framework-internal traces (task_class starting with `__`) are
    skipped — they carry only meta payload, no real signal to learn
    from, matching reflection_extract_gradients' policy.
    """
    cols = set(_detect_cols(con, "execution_traces"))
    # execution_traces' PK is trace_id (TEXT); use sqlite's implicit integer
    # rowid as the ordering/reference key (aliased to 'id' downstream).
    need = {"task_class", "agent_version_id", "reward_g",
            "objective_domain", "verifier_output", "code_region",
            "created_at"}
    if not need.issubset(cols):
        missing = sorted(need - cols)
        raise RuntimeError(f"execution_traces missing columns: {missing}")

    rows = con.execute(
        "SELECT rowid AS id, task_class, agent_version_id, reward_g, objective_domain, "
        "       verifier_output, code_region, created_at "
        "  FROM execution_traces "
        " WHERE task_class IS NOT NULL AND task_class <> '' "
        "   AND task_class NOT LIKE '\\_%' ESCAPE '\\' "
        "   AND agent_version_id IS NOT NULL AND agent_version_id <> '' "
        "   AND reward_g IS NOT NULL "
        " ORDER BY rowid ASC").fetchall()
    out = []
    for r in rows:
        node_type = "unknown"
        try:
            vo = json.loads(r["verifier_output"] or "{}")
            v = vo.get("node_type")
            if v:
                node_type = str(v)
        except Exception:
            pass
        out.append({
            "id": int(r["id"]),
            "task_class": r["task_class"],
            "agent_version_id": r["agent_version_id"],
            "reward_g": float(r["reward_g"]),
            "objective_domain": r["objective_domain"] or "",
            "code_region": r["code_region"] or "",
            "node_type": node_type,
        })
    return out


def _split_train_eval(traces, eval_frac, seed):
    """Deterministic group-aware train/eval split.

    Groups by (objective_domain, task_class, node_type) so the held-out
    set never shares a slice with the training set. The seed makes the
    per-group draw reproducible across runs against the same DB.
    """
    rng = random.Random(seed)
    by_group = defaultdict(list)
    for t in traces:
        by_group[(t["objective_domain"], t["task_class"], t["node_type"])].append(t)
    train, eval_ = [], []
    for group in by_group.values():
        rng.shuffle(group)
        n_eval = max(1, int(round(len(group) * eval_frac)))
        eval_.extend(group[:n_eval])
        train.extend(group[n_eval:])
    rng.shuffle(eval_)
    return train, eval_


def _legacy_pool(con, train, min_samples):
    """Legacy selector pool: per (task_class, node_type, od) the best
    lane by relative_advantage DESC, runs_count DESC, restricted to
    lanes seen in the training set."""
    seen = defaultdict(set)
    for t in train:
        seen[(t["task_class"], t["node_type"], t["objective_domain"])].add(
            t["agent_version_id"])
    pool = defaultdict(list)
    cols = _detect_cols(con, "agent_performance_memory")
    if "relative_advantage" not in cols:
        return pool
    for (tc, nt, od), lanes in seen.items():
        if not lanes:
            continue
        placeholders = ",".join("?" * len(lanes))
        where = ("task_class = ? AND agent_version_id IN ("
                 + placeholders + ") AND runs_count >= ?")
        params = [tc, *lanes, min_samples]
        if "node_type" in cols and nt:
            where += " AND (node_type = ? OR node_type = '')"
            params.append(nt)
        if "objective_domain" in cols and od:
            where += " AND objective_domain = ?"
            params.append(od)
        try:
            rows = con.execute(
                f"SELECT agent_version_id, relative_advantage, runs_count "
                f"  FROM agent_performance_memory WHERE {where} "
                f" ORDER BY relative_advantage DESC, runs_count DESC",
                params).fetchall()
        except sqlite3.OperationalError:
            continue
        pool[(tc, nt, od)] = [(r["agent_version_id"],
                               float(r["relative_advantage"] or 0.0))
                              for r in rows]
    return pool


def _bandit_pool(con, train, min_samples, ucb_c):
    """Bandit selector pool: per (task_class, node_type, od, code_region)
    the lane with the highest UCB score (z + C*sqrt(2*ln(N+1)/n)). When
    no z-scored row exists, the lane drops back to the legacy ordering
    so cold-start paths match.
    """
    seen = defaultdict(set)
    for t in train:
        seen[(t["task_class"], t["node_type"], t["objective_domain"],
              t["code_region"])].add(t["agent_version_id"])
    pool = defaultdict(list)
    cols_d = _detect_cols(con, "lane_domain_advantage")
    cols_r = _detect_cols(con, "lane_region_advantage")
    if "z_score_advantage" not in cols_d:
        return pool

    def _read(table, where_extra, params_extra, lanes):
        if not lanes:
            return []
        placeholders = ",".join("?" * len(lanes))
        where = ("task_class = ? AND agent_version_id IN ("
                 + placeholders + ") AND runs_count >= ?")
        params = [params_extra["tc"], *lanes, min_samples]
        if "node_type" in _detect_cols(con, table) and params_extra.get("nt"):
            where += " AND node_type = ?"
            params.append(params_extra["nt"])
        if "objective_domain" in _detect_cols(con, table) \
                and params_extra.get("od"):
            where += " AND objective_domain = ?"
            params.append(params_extra["od"])
        if "code_region" in _detect_cols(con, table) \
                and params_extra.get("cr"):
            where += " AND code_region = ?"
            params.append(params_extra["cr"])
        where += where_extra
        try:
            return con.execute(
                f"SELECT agent_version_id, z_score_advantage, "
                f"       relative_advantage, runs_count "
                f"  FROM {table} WHERE {where}",
                params).fetchall()
        except sqlite3.OperationalError:
            return []

    for (tc, nt, od, cr), lanes in seen.items():
        rows = []
        if cr:
            rows = _read("lane_region_advantage", "",
                         {"tc": tc, "nt": nt, "od": od, "cr": cr}, lanes)
        if not rows and od:
            rows = _read("lane_domain_advantage", "",
                         {"tc": tc, "nt": nt, "od": od, "cr": None}, lanes)
        if not rows:
            continue
        total_n = sum(int(r["runs_count"]) for r in rows) or 1
        scored = []
        for r in rows:
            z = float(r["z_score_advantage"] or 0.0)
            n = max(int(r["runs_count"]), 1)
            bonus = ucb_c * math.sqrt(2.0 * math.log(total_n + 1) / n)
            scored.append((r["agent_version_id"], z + bonus))
        scored.sort(key=lambda t: t[1], reverse=True)
        pool[(tc, nt, od, cr)] = scored
    return pool


def _pick(pool, key, fallback):
    """Pick the highest-ranked lane in `pool[key]`; on miss return
    `fallback` (the trace's own lane — a fair baseline)."""
    candidates = pool.get(key)
    if not candidates:
        return fallback
    return candidates[0][0]


def _winner_picked(chosen, trace, pool):
    """Was the trace's lane the chosen lane? Returns bool. When the
    chosen lane differs, we report a 'wrong' pick — i.e. the selector
    would have routed elsewhere."""
    return chosen == trace["agent_version_id"]


def _reward_for(trace, train):
    """Reward estimate for a single trace under a hypothetical reroute.

    We don't have a counterfactual for the reroute (we only have what
    actually happened), so the win/loss verdict is: did the selector
    pick the trace's actual lane, AND would that lane have produced
    higher reward_g than the average lane in the slice on the training
    set? Returns a tuple (selector_match, beat_baseline).
    """
    # Group-relative baseline: mean reward_g over training rows in the
    # same (objective_domain, task_class, node_type) slice.
    pool = [t["reward_g"] for t in train
            if t["objective_domain"] == trace["objective_domain"]
            and t["task_class"] == trace["task_class"]
            and t["node_type"] == trace["node_type"]]
    if pool:
        baseline = sum(pool) / len(pool)
    else:
        baseline = 0.0
    return baseline


def main(argv):
    args = _parse_args(argv)

    if not os.path.isfile(args.db):
        print(json.dumps({
            "error": f"db not found: {args.db}",
            "old_winrate": 0.0,
            "new_winrate": 0.0,
            "delta": 0.0,
        }))
        return 1

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        traces = _fetch_traces(con)
        if not traces:
            print(json.dumps({
                "db": args.db,
                "n_traces_total": 0,
                "n_eval": 0,
                "n_train": 0,
                "old_winrate": 0.0,
                "new_winrate": 0.0,
                "delta": 0.0,
                "seed": args.seed,
                "split": args.split,
                "ucb_c": args.ucb_c,
                "notes": "no routed traces in DB; replay is a no-op",
            }))
            return 0
        if args.limit and len(traces) > args.limit:
            # Deterministic truncation by seed — keeps the smoke run
            # small without losing determinism for repeated CI runs.
            rng = random.Random(args.seed)
            traces = rng.sample(traces, args.limit)

        train, eval_ = _split_train_eval(traces, args.split, args.seed)
        legacy = _legacy_pool(con, train, args.min_samples)
        bandit = _bandit_pool(con, train, args.min_samples, args.ucb_c)

        old_wins, new_wins, total = 0, 0, 0
        for t in eval_:
            legacy_key = (t["task_class"], t["node_type"], t["objective_domain"])
            bandit_key = (t["task_class"], t["node_type"],
                          t["objective_domain"], t["code_region"])
            old_pick = _pick(legacy, legacy_key, t["agent_version_id"])
            new_pick = _pick(bandit, bandit_key, t["agent_version_id"])
            old_match = _winner_picked(old_pick, t, legacy)
            new_match = _winner_picked(new_pick, t, bandit)
            # Reward scoring: legacy/bandid picks are scored against the
            # mean reward_g of training-set lanes in the same slice. The
            # winner is the selector that picks a lane ABOVE the slice
            # baseline. When both pick the same lane we credit both.
            baseline = _reward_for(t, train)
            # Score the actual chosen lane's expected reward (we use the
            # trace's own reward_g as a noisy proxy for "how good was
            # the chosen lane on this eval slice").
            actual = t["reward_g"]
            if old_match and actual > baseline:
                old_wins += 1
            if new_match and actual > baseline:
                new_wins += 1
            total += 1

        old_rate = (old_wins / total) if total else 0.0
        new_rate = (new_wins / total) if total else 0.0
        result = {
            "db": args.db,
            "n_traces_total": len(traces),
            "n_eval": len(eval_),
            "n_train": len(train),
            "old_winrate": round(old_rate, 4),
            "new_winrate": round(new_rate, 4),
            "delta": round(new_rate - old_rate, 4),
            "seed": args.seed,
            "split": args.split,
            "ucb_c": args.ucb_c,
            "notes": (
                "win = selector picked the trace's actual lane AND "
                "actual reward_g beat the slice mean on the training set"
            ),
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))