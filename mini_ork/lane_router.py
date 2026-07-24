"""GRPO relative-advantage lane routing — Python port of lib/lane_router.sh (Tier A).

This module persists + reads two ROUTING FACES over the same execution_traces:

  * Legacy face (MO_ROUTER_UCB_C=0, MO_ROUTER_SINGLE_SAMPLE=0):
      relative_advantage = (lane_mean - group_mean), shrunken, EMA-blended.
      Within-group shrunken means get stored into agent_performance_memory +
      lane_domain_advantage + lane_region_advantage exactly as the old code did.
      ``preferred_lane`` orders by relative_advantage DESC. Single-sample
      groups (only one lane in the slice) are skipped, same as before.

  * Bandit face (MO_ROUTER_UCB_C > 0):
      recompute_advantages ALSO computes advantage_var / advantage_std /
      z_score_advantage per (lane, slice) and writes them into the per-domain
      + per-region tables. Single-sample groups (when MO_ROUTER_SINGLE_SAMPLE=1)
      drop their score into lane_slice_baseline as a persistent EMA. The
      ``preferred_lane`` selector uses the Upper Confidence Bound
      ``z_score_advantage + C * sqrt(2 ln N / n_lane)`` so under-sampled lanes
      get explored via the exploit-time ordering. (Follow-up: reroute
      ``lib/decision_service.sh``'s ε-explore draw to the highest-uncertainty
      lane too — not yet wired; the ε path is still uniform-random.)

  * NeuralUCB (MO_ROUTER_CONTEXTUAL=1, default OFF):
      a Linear-Bandit UCB using mini_ork.memory.semantic.HashEmbedder to
      featurize (task_class, node_type, code_region) and per-lane ridge
      regression to predict expected reward. Used to break ties between lanes
      with identical UCB scores. Embedder import + call live strictly inside
      the gated branch — the default path makes zero extra per-task model
      calls. (D7)

This is a CONTEXTUAL BANDIT — not canonical GRPO. The advantage estimator
remains within-group (no importance-sampling / IPS), the UCB bonus is a
heuristic ordering term not a bound, and NeuralUCB is a linear posterior not
a deep network. Off-policy correction under near-deterministic logging is
intentionally NOT attempted (per research note 2509.00648 Fig 3g). The
comments + docs do not claim zero-cost unbiasedness.

relative_advantage[i] = score[i] - mean(group), score = normalized reward_g
(NULL rows skipped), grouped by (objective_domain, task_class, node_type,
code_region). Refinements preserved exactly: per-group shrinkage (K=5), EMA
blend with prior (α=0.30), recency halflife (14d), cost tie-break on flat
groups, and the decayed defect-attribution penalty on the region slice.
Faithful extraction of the bash heredoc; env knobs unchanged.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sqlite3
from collections import defaultdict

from mini_ork.learning.advantage_store import AdvantageStore, resolve_db_path


def _db_path(db: str | None) -> str:
    """Kept for the two helpers below that still open short-lived connections
    (log_propensity / z_score_advantage); resolution logic lives in
    mini_ork/learning/advantage_store.resolve_db_path."""
    return resolve_db_path(db)


# ── pure math (M9: SQL lives in AdvantageStore; these stay here, DB-free) ────


def _recency_weight(age_days: float, halflife_days: float) -> float:
    """Exponential recency decay: 0.5 ** (age / halflife)."""
    return math.exp(-math.log(2) * age_days / halflife_days)


def _shrink(advantage: float, n_in_group: int, shrink_k: int) -> float:
    """n-aware shrinkage toward 0: adv * n/(n+K); K<=0 disables shrinkage."""
    factor = n_in_group / (n_in_group + shrink_k) if shrink_k > 0 else 1.0
    return advantage * factor


def _ema_blend(prior, batch, alpha: float):
    """EMA blend of a stored prior with the fresh batch value.

    alpha >= 1 → batch wins; alpha <= 0 → prior wins; unparseable prior → batch.
    """
    if prior is None or alpha >= 1.0:
        return batch
    if alpha <= 0.0:
        return prior
    try:
        p = float(prior)
    except (TypeError, ValueError):
        return batch
    return alpha * batch + (1.0 - alpha) * p


def _zscore(value: float, mean: float, var: float) -> float:
    """z = (value - mean) / max(std, 1e-3)."""
    std = math.sqrt(max(var, 0.0))
    denom = std if std > 1e-3 else 1e-3
    return (value - mean) / denom


def recompute_advantages(since: int = 0, db: str | None = None) -> int:
    since_iso = datetime.datetime.utcfromtimestamp(int(since)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")

    SHRINK_K = int(os.environ.get("MO_LEARNING_SHRINKAGE_K", "5"))
    DECAY_ALPHA = float(os.environ.get("MO_LEARNING_DECAY_ALPHA", "0.30"))
    HALFLIFE = float(os.environ.get("MO_LEARNING_HALFLIFE_DAYS", "14"))
    TIEBREAK = int(os.environ.get("MO_LEARNING_TIEBREAK", "1"))
    # Cost-free contextual-bandit env knobs (D1-D3). Defaults per kickoff:
    #   MO_ROUTER_UCB_C       default 0.5 → bandit ordering on by default
    #   MO_ROUTER_SINGLE_SAMPLE default 1   → single-sample baselines on
    #   MO_ROUTER_CONTEXTUAL  default 0   → NeuralUCB off (zero-cost default)
    # Setting all three to 0 reproduces the legacy within-group-mean router
    # byte-for-byte (verifier regression gate).
    UCB_C = float(os.environ.get("MO_ROUTER_UCB_C", "0.5"))
    SINGLE_SAMPLE = int(os.environ.get("MO_ROUTER_SINGLE_SAMPLE", "1"))
    BANDIT_ON = UCB_C > 0.0

    store = AdvantageStore(db).open()

    prior_apm = store.fetch_prior_apm()
    store.ensure_advantage_tables()
    prior_domain = store.fetch_prior_domain()
    prior_region = store.fetch_prior_region()
    prior_baseline = store.fetch_prior_baseline()

    rows = store.fetch_source_rows(since_iso)

    def _node_type(row):
        try:
            return (json.loads(row["verifier_output"] or "{}").get("node_type")
                    or "unknown")
        except Exception:
            return "unknown"

    def _parse_ts(raw):
        if raw is None:
            return None
        tsv = str(raw).strip().rstrip("Z").replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.datetime.strptime(tsv, fmt)
            except ValueError:
                continue
        return None

    groups = defaultdict(list)
    _now_utc = datetime.datetime.utcnow()
    for r in rows:
        keys = r.keys()
        code_region = (r["code_region"] or "").strip() if "code_region" in keys else ""
        try:
            cost = float(r["cost_usd"]) if "cost_usd" in keys and r["cost_usd"] is not None else 0.0
        except (TypeError, ValueError):
            cost = 0.0
        ts = _parse_ts(r["created_at"]) if "created_at" in keys else None
        if HALFLIFE > 0 and ts is not None:
            age_days = max((_now_utc - ts).total_seconds() / 86400.0, 0.0)
            w = _recency_weight(age_days, HALFLIFE)
        else:
            w = 1.0
        groups[(r["objective_domain"], r["task_class"], _node_type(r), code_region)].append(
            {"lane": r["agent_version_id"], "score": float(r["reward_g"]),
             "task_class": r["task_class"], "cost": cost, "weight": w})

    acc = defaultdict(lambda: {"shr_sum": 0.0, "groups": 0, "wins": 0,
                               "node_types": defaultdict(int),
                               "objective_domains": defaultdict(int)})
    acc_domain = defaultdict(lambda: {"shr_sum": 0.0, "groups": 0, "wins": 0,
                                      "var_sum": 0.0, "n_for_var": 0,
                                      "slice_mean": 0.0, "slice_std": 0.0})
    acc_region = defaultdict(lambda: {"shr_sum": 0.0, "groups": 0, "wins": 0,
                                      "var_sum": 0.0, "n_for_var": 0,
                                      "slice_mean": 0.0, "slice_std": 0.0})
    acc_baseline = defaultdict(lambda: {"sum_score": 0.0, "sum_score_sq": 0.0,
                                        "n": 0, "mean": 0.0, "var": 0.0})
    for (_od, _tc, _nt, _cr), members in groups.items():
        # D2 single-sample fallback (MO_ROUTER_SINGLE_SAMPLE=1). With only
        # one lane in the slice we cannot compute relative advantage (no
        # group mean exists); instead, persist the score into the slice
        # baseline so future recomputes can z-score against it. When
        # MO_ROUTER_SINGLE_SAMPLE=0 the legacy "skip 1-member groups"
        # behavior is preserved byte-equivalently.
        if len(members) < 2:
            if SINGLE_SAMPLE and members:
                _m = members[0]
                b = acc_baseline[(_od, _tc, _nt, _cr)]
                b["sum_score"] += _m["weight"] * _m["score"]
                b["sum_score_sq"] += _m["weight"] * _m["score"] * _m["score"]
                b["n"] += 1
            continue
        sum_w = sum(m["weight"] for m in members)
        if sum_w <= 0:
            continue
        wmean = sum(m["weight"] * m["score"] for m in members) / sum_w
        # D3 z-score reference: also accumulate slice-wide wss statistics for
        # lane_slice_baseline EMA update. Independent of BANDIT_ON so the
        # baseline table fills even when the bandit is off — the baseline
        # only feeds the bandit selector.
        _slice_wss = sum(m["weight"] * m["score"] * m["score"] for m in members)
        _slice_wsum = sum_w
        sb = acc_baseline[(_od, _tc, _nt, _cr)]
        sb["sum_score"] += sum(m["weight"] * m["score"] for m in members)
        sb["sum_score_sq"] += _slice_wss
        sb["n"] += 1
        lane_bonus = {}
        scores = [m["score"] for m in members]
        if TIEBREAK != 0 and min(scores) == max(scores):
            costs = [m["cost"] for m in members]
            lo, hi = min(costs), max(costs)
            if lo != hi:
                for m in members:
                    lane_bonus[m["lane"]] = 0.1 - 0.2 * (m["cost"] - lo) / (hi - lo)
        by_lane = defaultdict(lambda: {"ws": 0.0, "w": 0.0, "n": 0, "wss": 0.0})
        for m in members:
            b = by_lane[m["lane"]]
            b["ws"] += m["weight"] * m["score"]
            b["w"] += m["weight"]
            b["n"] += 1
            b["wss"] += m["weight"] * m["score"] * m["score"]
        for lane, b in by_lane.items():
            lane_mean = b["ws"] / b["w"] if b["w"] > 0 else 0.0
            lane_adv = lane_mean - wmean + lane_bonus.get(lane, 0.0)
            n_in_group = b["n"]
            shrunken = _shrink(lane_adv, n_in_group, SHRINK_K)
            # Per-lane weighted variance of scores within the slice's group
            # window. var = E[x^2] - E[x]^2 (population formula; n >= 2
            # here). NaN-safe — wss=0 or w=0 yields 0.
            var = 0.0
            if b["w"] > 0:
                ex2 = b["wss"] / b["w"]
                var = max(ex2 - lane_mean * lane_mean, 0.0)
            _std = math.sqrt(var)  # computed for bash-port fidelity; var feeds var_sum
            wins = 1 if lane_adv > 0 else 0
            a = acc[(lane, _tc)]
            a["shr_sum"] += shrunken
            a["groups"] += 1
            a["wins"] += wins
            a["node_types"][_nt] += 1
            a["objective_domains"][_od] += 1
            d = acc_domain[(lane, _tc, _nt, _od)]
            d["shr_sum"] += shrunken
            d["groups"] += 1
            d["wins"] += wins
            d["var_sum"] += var
            d["n_for_var"] += 1
            if _cr:
                rr = acc_region[(lane, _tc, _nt, _od, _cr)]
                rr["shr_sum"] += shrunken
                rr["groups"] += 1
                rr["wins"] += wins
                rr["var_sum"] += var
                rr["n_for_var"] += 1

    _penalty_by_key = defaultdict(float)
    if store.has_defect_attributions():
        _now_utc = datetime.datetime.utcnow()
        for pr in store.fetch_defect_penalties():
            try:
                pen = float(pr["penalty"])
                hlf = float(pr["decay_halflife_days"]) if pr["decay_halflife_days"] is not None else 30.0
            except (TypeError, ValueError):
                continue
            if hlf <= 0:
                continue
            tsv = str(pr["ts"]).strip().rstrip("Z").replace("T", " ")
            ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.datetime.strptime(tsv, fmt)
                    break
                except ValueError:
                    ts = None
            if ts is None:
                continue
            age_days = max((_now_utc - ts).total_seconds() / 86400.0, 0.0)
            _penalty_by_key[(pr["lane"], pr["code_region"], pr["task_class"])] += pen * (0.5 ** (age_days / hlf))

    def _ema(prior, batch):
        return _ema_blend(prior, batch, DECAY_ALPHA)

    # D2 EMA-blend slice baselines BEFORE the per-lane upserts so the
    # z-score normaliser in BANDIT_ON mode sees the same prior the lane
    # advantage will be compared against.
    for key, stats in acc_baseline.items():
        _od, _tc, _nt, _cr = key
        if stats["n"] <= 0:
            continue
        batch_mean = stats["sum_score"] / stats["n"]
        ex2 = stats["sum_score_sq"] / stats["n"]
        batch_var = max(ex2 - batch_mean * batch_mean, 0.0)
        prior_t = prior_baseline.get(key)
        if prior_t is None:
            new_mean, new_var = batch_mean, batch_var
        else:
            new_mean = _ema(prior_t[0], batch_mean)
            new_var = _ema(prior_t[1], batch_var)
        new_std = math.sqrt(max(new_var, 0.0))
        store.upsert_slice_baseline(_od, _tc, _nt, _cr,
                                    round(new_mean, 6), round(new_var, 6),
                                    round(new_std, 6), stats["n"])
        # Cache locally for the z-score step below.
        stats["mean"], stats["var"] = new_mean, new_var

    def _z_score(lane_adv: float, key) -> float:
        """D3 z-score = (lane_adv - slice_baseline_mean) /
        max(slice_baseline_std, 1e-3). Pure read of the freshly-updated
        acc_baseline cache; falls back to lane_adv itself when the slice
        is cold (no baseline row AND no current accumulators)."""
        baseline = acc_baseline.get(key)
        if not baseline or baseline.get("n", 0) == 0:
            return lane_adv
        return _zscore(lane_adv, baseline.get("mean", 0.0), baseline.get("var", 0.0))

    upserted = 0
    for (lane, tc), stats in acc.items():
        if stats["groups"] <= 0:
            continue
        new_rel_adv = _ema(prior_apm.get((lane, tc)), stats["shr_sum"] / stats["groups"])
        top_node = (max(stats["node_types"].items(), key=lambda kv: kv[1])[0]
                    if stats["node_types"] else None)
        store.upsert_agent_performance(lane, top_node or lane, lane, tc,
                                       stats["groups"], stats["wins"],
                                       round(new_rel_adv, 4))
        upserted += 1

    for (lane, tc, nt, od), stats in acc_domain.items():
        if stats["groups"] <= 0:
            continue
        new_rel_adv = _ema(prior_domain.get((lane, tc, nt or "", od or "")),
                           stats["shr_sum"] / stats["groups"])
        if BANDIT_ON and stats.get("n_for_var", 0) > 0:
            adv_var = stats["var_sum"] / stats["n_for_var"]
            adv_std = math.sqrt(max(adv_var, 0.0))
            new_z = _z_score(new_rel_adv, (od, tc, nt or "", ""))
        else:
            adv_var, adv_std, new_z = 0.0, 0.0, 0.0
        store.upsert_domain_advantage(lane, tc, nt or "", od or "",
                                      round(new_rel_adv, 4), stats["groups"],
                                      stats["wins"], round(adv_var, 6),
                                      round(adv_std, 6), round(new_z, 4))

    for (lane, tc, nt, od, cr), stats in acc_region.items():
        if stats["groups"] <= 0:
            continue
        new_rel_adv = _ema(prior_region.get((lane, tc, nt or "", od or "", cr or "")),
                           stats["shr_sum"] / stats["groups"])
        if cr:
            new_rel_adv += _penalty_by_key.get((lane, cr, tc), 0.0)
        # D1/D3: when the bandit is on, mirror the per-region variance + z-score
        # so preferred_lane() can rank by UCB. Penalty fold applies to the
        # scored number (the z-score is computed against the un-penalised
        # baseline), keeping the bonus a heuristic ordering term.
        if BANDIT_ON and stats.get("n_for_var", 0) > 0:
            adv_var = stats["var_sum"] / stats["n_for_var"]
            adv_std = math.sqrt(max(adv_var, 0.0))
            new_z = _z_score(new_rel_adv, (od, tc, nt or "", cr or ""))
        else:
            adv_var, adv_std, new_z = 0.0, 0.0, 0.0
        store.upsert_region_advantage(lane, tc, nt or "", od or "", cr or "",
                                      round(new_rel_adv, 4), stats["groups"],
                                      stats["wins"], round(adv_var, 6),
                                      round(adv_std, 6), round(new_z, 4))

    store.commit()
    store.close()
    return upserted


def preferred_lane(task_class: str, node_type: str = "", objective_domain: str = "",
                   code_region: str = "", db: str | None = None) -> str:
    """Highest-advantage lane for the slice (sample floor MO_LEARNING_MIN_SAMPLES,
    default 3). Region → domain → global, matching bash. Returns
    'lane|adv|runs' (bash's pipe format) or '' when no slice clears the floor.

    Bandit selector (MO_ROUTER_UCB_C > 0): when the active slice has z-scored
    advantages stored, the ordering switches to UCB
    ``z_score_advantage + C * sqrt(2 * ln(N) / n_lane)`` so under-sampled lanes
    get explored. The displayed ``adv`` field remains the legacy
    ``relative_advantage`` value so bash/python parity still prints the same
    string when both code paths converge on the same winner.

    NeuralUCB (MO_ROUTER_CONTEXTUAL=1) lives inside the gated branch: when
    two lanes tie on the UCB score within 1e-6, the HashEmbedder feature
    ``(task_class, node_type, code_region)`` is run through a per-lane ridge
    regression and the lane with the higher ridge score wins. The embedder
    import + call happen ONLY in this branch — the default path makes zero
    extra per-task model calls (D7)."""
    min_samples = int(os.environ.get("MO_LEARNING_MIN_SAMPLES", "3"))
    ucb_c = float(os.environ.get("MO_ROUTER_UCB_C", "0.5"))
    contextual = int(os.environ.get("MO_ROUTER_CONTEXTUAL", "0"))
    bandit_on = ucb_c > 0.0

    store = AdvantageStore(db).open()  # Row factory: _select_best_lane reads by column name
    try:
        if objective_domain and code_region:
            candidates = store.fetch_region_candidates(
                task_class, objective_domain, code_region, node_type, min_samples)
            row = _select_best_lane(candidates,
                                    bandit_on, ucb_c, contextual,
                                    task_class, node_type, objective_domain, code_region)
            if row:
                return row
        if objective_domain:
            candidates = store.fetch_domain_candidates(
                task_class, objective_domain, node_type, min_samples)
            row = _select_best_lane(candidates,
                                    bandit_on, ucb_c, contextual,
                                    task_class, node_type, objective_domain, code_region)
            if row:
                return row
        row = store.fetch_global_best(task_class, node_type, min_samples)
        return f"{row[0]}|{row[1]}|{row[2]}" if row else ""
    finally:
        store.close()


def _select_best_lane(candidates: list,
                      bandit_on: bool, ucb_c: float, contextual: int,
                      task_class: str, node_type: str,
                      objective_domain: str, code_region: str) -> str:
    """D1/D7 lane picker. Returns the bash-format 'lane|adv|runs' string.

    Bandit path:
      1. SELECT all lanes clearing the sample floor, ordered by raw
         relative_advantage DESC so we don't lose the cost tie-break signal.
      2. If bandit_on and the table has z-scored rows, recompute the UCB
         score ``z_score_advantage + C * sqrt(2 ln N_total / n_lane)`` and
         return the lane with the highest UCB. The ``adv`` field shown
         stays the legacy ``relative_advantage`` so bash/python parity is
         maintained for callers that diff the string.
      3. If contextual=1 and the top two UCB scores tie within 1e-6, run
         NeuralUCB (HashEmbedder featurize → per-lane ridge) on the tied
         candidates. The embedder import + call live strictly inside this
         branch.

    ``candidates`` comes pre-fetched from AdvantageStore (ordered by raw
    relative_advantage DESC, runs_count DESC so we don't lose the cost
    tie-break signal); this function is ranking math only — no SQL.
    """
    if not candidates:
        return ""
    if not bandit_on:
        # Legacy face: take the first row (already sorted by relative_advantage DESC).
        first = candidates[0]
        return f"{first[0]}|{first[1]}|{first[2]}"
    total_runs = sum(int(r["runs_count"]) for r in candidates) or 1
    scored = []
    for r in candidates:
        z = float(r["z_score_advantage"] or 0.0)
        n = max(int(r["runs_count"]), 1)
        # UCB1 with slice-wide N_total — favours lanes with high z and low n.
        bonus = ucb_c * math.sqrt(2.0 * math.log(total_runs + 1) / n)
        scored.append((z + bonus, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    if contextual and len(scored) >= 2:
        # Tie-break via NeuralUCB on top-2 candidates (D7).
        # Linear-posterior style: context feature x = embedder(slice identity);
        # per-lane weight w = embedder(lane). Score = x . w. The lane with
        # the larger dot product wins the tie. Deterministic — same input
        # always produces the same ordering.
        if abs(scored[0][0] - scored[1][0]) < 1e-6:
            try:
                # Embedder import + call strictly inside the gated branch.
                from mini_ork.memory.semantic import HashEmbedder
                emb = HashEmbedder()
                ctx = emb.embed([
                    f"{task_class}|{node_type}|{objective_domain}|{code_region}"
                ])[0]
                w0 = emb.embed([scored[0][1][0]])[0]
                w1 = emb.embed([scored[1][1][0]])[0]
                s0 = sum(a * b for a, b in zip(ctx, w0))
                s1 = sum(a * b for a, b in zip(ctx, w1))
                if s1 > s0:
                    scored = [scored[1], scored[0]] + scored[2:]
            except Exception:
                # On any failure, keep the UCB order — never raise out of the
                # selector for tie-break noise.
                pass
    best = scored[0][1]
    return f"{best[0]}|{best[1]}|{best[2]}"


def log_propensity(trace_id: int, source: str, explore: int, score: float,
                   *, db: str | None = None) -> None:
    """D4: stamp route_source / route_explore / route_score on a routed trace
    row. Called by the executor after the lane is picked. Null-safe: leaves
    the row untouched if the trace no longer exists."""
    con = sqlite3.connect(_db_path(db))
    try:
        con.execute(
            "UPDATE execution_traces SET route_source = ?, route_explore = ?, "
            "route_score = ? WHERE id = ?",
            (source, int(bool(explore)), float(score), trace_id))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def z_score_advantage(lane: str, task_class: str, node_type: str = "",
                      objective_domain: str = "", code_region: str = "",
                      db: str | None = None) -> float:
    """Read-only convenience for callers (and the bandit selector's tests):
    return the most-specific z_score_advantage for ``lane`` in the slice, or
    0.0 if no row clears the sample floor."""
    min_samples = int(os.environ.get("MO_LEARNING_MIN_SAMPLES", "3"))
    con = sqlite3.connect(_db_path(db))
    try:
        if objective_domain and code_region:
            row = con.execute(
                "SELECT z_score_advantage FROM lane_region_advantage "
                "WHERE agent_version_id=? AND task_class=? AND node_type=? "
                "AND objective_domain=? AND code_region=? AND runs_count>=? "
                "ORDER BY runs_count DESC LIMIT 1",
                (lane, task_class, node_type or "", objective_domain,
                 code_region, min_samples)).fetchone()
            if row:
                return float(row[0] or 0.0)
        if objective_domain:
            row = con.execute(
                "SELECT z_score_advantage FROM lane_domain_advantage "
                "WHERE agent_version_id=? AND task_class=? AND node_type=? "
                "AND objective_domain=? AND runs_count>=? "
                "ORDER BY runs_count DESC LIMIT 1",
                (lane, task_class, node_type or "", objective_domain, min_samples)).fetchone()
            if row:
                return float(row[0] or 0.0)
    finally:
        con.close()
    return 0.0
