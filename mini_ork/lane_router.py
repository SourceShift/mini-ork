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


def _equirouter_enabled() -> bool:
    """EquiRouter ranking is ON by default; ``MO_EQUIROUTER=0`` restores the
    legacy selector. Default-ON with an opt-out follows the MO_APPLY_SCORER
    precedent: the env var exists to go back, not to switch a capability on."""
    return os.environ.get("MO_EQUIROUTER", "1").strip().lower() not in ("0", "false", "no", "")


def _entrorouter_enabled() -> bool:
    """EntroRouter entropy regulation is ON by default; ``MO_ENTROROUTER=0``
    restores the constant-C selector. It engages only when a per-lane offline
    capability estimate exists, so a database with no
    ``agent_performance_memory`` rows routes exactly as it does today."""
    return os.environ.get("MO_ENTROROUTER", "1").strip().lower() not in ("0", "false", "no", "")


def _recovery_floor(capability: float, n: int, anchor: float, tau: float) -> float:
    """Soft-anchored recovery floor for a lane whose observed estimate sank.

    EntroRouter (2606.29424) names the failure it prevents *Trust Region
    Collapse*: a capable lane gets one unlucky sample early, its estimate sinks,
    and it is never selected again — so it never gets the chance to recover, and
    the router settles on a lane that is merely acceptable. UCB does not save it,
    because the competitor's score stays above the sunk lane's even after the
    bonus shrinks on both sides.

    Two stages, matching the paper:

      * **Soft supervision** — the floor starts high and decays as the lane
        accumulates observations (``tau / (tau + n)``). The prior keeps
        exploration honest while evidence is thin; once the lane has been
        sampled enough times its own record decides, not the prior.
      * **Soft anchor** — the floor is scaled by the lane's *offline* capability
        estimate, so it rescues lanes known to be strong and leaves weak lanes
        where they are. A lane with no positive offline estimate gets no floor
        at all, which is what keeps this a no-op on a cold database.

    Returned as a floor rather than an additive bonus: adding it would inflate
    the incumbent that is already winning, which is not a lane that needs
    rescuing. ``max`` only ever lifts a sunk lane up to a fighting chance.

    *When it engages*: the floor decays as ``1/n`` while the UCB bonus decays as
    ``1/sqrt(n)``, so the floor is relatively strongest at small ``n`` and the
    bonus overtakes it as the slice fills. In a warm slice the bonus already
    gives every lane a fighting chance, so the floor changes nothing; it decides
    in the cold slice the paper is about — both lanes' ``z`` negative, few runs
    each — which is exactly where the collapse happens.
    """
    if capability <= 0.0:
        return 0.0
    entropy = tau / (tau + n) if tau > 0 else 1.0
    return anchor * capability * entropy


def _slice_rankings(store, task_class: str, node_type: str,
                    objective_domain: str, code_region: str,
                    min_samples: int) -> dict:
    """``(objective_domain, code_region) -> [(lane, relative_advantage), …]``.

    One entry per slice that has at least two lanes clearing ``min_samples``,
    each lane list ordered best-first. Slices with a single lane carry no
    preference information (nothing to prefer it *over*), so they are dropped
    — ``recompute_advantages`` already skips their advantage entirely.

    ``code_region`` is deliberately NOT filtered: the region axis is the slice
    dimension EquiRouter aggregates over. ``objective_domain`` is filtered when
    given, because lanes observed in a different objective domain are evidence
    about a different task and must not vote here.
    """
    where = "task_class=? AND node_type=? AND runs_count>=?"
    params: list = [task_class, node_type, min_samples]
    if objective_domain:
        where += " AND objective_domain=?"
        params.append(objective_domain)
    rows = store.con.execute(
        f"SELECT agent_version_id, objective_domain, code_region, "
        f"relative_advantage, runs_count FROM lane_region_advantage "
        f"WHERE {where} ORDER BY objective_domain, code_region, "
        f"relative_advantage DESC, runs_count DESC, agent_version_id",
        params).fetchall()
    slices: dict = defaultdict(list)
    for lane, od, cr, adv, _runs in rows:
        slices[(od, cr)].append((lane, float(adv or 0.0)))
    return {k: v for k, v in slices.items() if len(v) >= 2}


def rank_lanes(node_type: str, task_class: str, objective_domain: str = "",
               code_region: str = "", *, db: str | None = None,
               min_samples: int | None = None) -> list[tuple[str, float]]:
    """Rank lanes by Borda count aggregated across slices, best first.

    EquiRouter (2602.03478): a lane's correct object of learning is its
    *ordering* against the alternatives, not its absolute score. Training a
    scalar invites collapse toward whichever lane happens to carry the largest
    magnitude (caution C8 in the paper); a Borda count is scale-free — it uses
    only each slice's ordinal preference, so a lane winning a slice by +5.0 and
    one winning by +0.1 contribute identical points.

    Each slice ``(objective_domain, code_region)`` ranks its lanes by
    ``relative_advantage`` DESC and awards ``len(slice) - position`` points.
    Points are summed across slices. Ties break on mean advantage, then lane
    name, so the result is deterministic.

    Returns ``[]`` when fewer than two slices carry a preference — a single
    slice has nothing to aggregate, and its own ordering is already what the
    legacy selector consumes.
    """
    if min_samples is None:
        min_samples = int(os.environ.get("MO_LEARNING_MIN_SAMPLES", "3"))
    store = AdvantageStore(db).open()
    try:
        slices = _slice_rankings(store, task_class, node_type,
                                 objective_domain, code_region, min_samples)
    finally:
        store.close()
    if len(slices) < 2:
        return []
    return _borda(slices)


def _borda(slices: dict) -> list[tuple[str, float]]:
    """Borda-count the per-slice lane orderings into one ranking.

    Shared by ``rank_lanes`` and ``preferred_lane``'s override so the two can
    never drift into disagreeing about what "ranked first" means.
    """
    points: dict = defaultdict(float)
    adv_sum: dict = defaultdict(float)
    adv_n: dict = defaultdict(int)
    # sorted() so float accumulation order is stable run to run.
    for key in sorted(slices):
        lanes = slices[key]
        size = len(lanes)
        for pos, (lane, adv) in enumerate(lanes):
            points[lane] += size - pos
            adv_sum[lane] += adv
            adv_n[lane] += 1
    return sorted(
        ((lane, points[lane]) for lane in points),
        key=lambda t: (-t[1], -(adv_sum[t[0]] / adv_n[t[0]]), t[0]),
    )


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
    extra per-task model calls (D7).

    EquiRouter (default ON, ``MO_EQUIROUTER=0`` restores the above verbatim)
    sits on top of whichever branch fired: when two or more slices carry a lane
    preference, the pick is re-ordered by the cross-slice Borda ranking from
    ``rank_lanes``, restricted to lanes that already cleared the floor here.
    With a single slice it is a no-op, because that slice's own ordering is
    already what ``_select_best_lane`` consumed."""
    return _pick_lane(task_class, node_type, objective_domain, code_region,
                      db)["pick"]


def preferred_lane_detail(task_class: str, node_type: str = "",
                          objective_domain: str = "", code_region: str = "",
                          db: str | None = None) -> dict:
    """``preferred_lane``'s pick plus how confidently it was made.

    ``{"pick", "lane", "adv", "runs", "margin", "source"}``, where ``source``
    names the slice that produced the pick (``region`` / ``domain`` / ``global``
    / ``""``). ``margin`` is None when the comparison does not exist — see
    ``_select_best_lane_scored``. Callers that only need the lane string keep
    using ``preferred_lane``; this exists so the uncertainty signal survives
    the decision instead of being recomputed by whoever wants it.
    """
    return _pick_lane(task_class, node_type, objective_domain, code_region, db)


def _pick_lane(task_class: str, node_type: str, objective_domain: str,
               code_region: str, db: str | None) -> dict:
    """One slice-walk shared by ``preferred_lane`` and ``preferred_lane_detail``
    so the string contract and the provenance view can never disagree."""
    min_samples = int(os.environ.get("MO_LEARNING_MIN_SAMPLES", "3"))
    ucb_c = float(os.environ.get("MO_ROUTER_UCB_C", "0.5"))
    contextual = int(os.environ.get("MO_ROUTER_CONTEXTUAL", "0"))
    bandit_on = ucb_c > 0.0

    store = AdvantageStore(db).open()  # Row factory: _select_best_lane reads by column name
    try:
        # EntroRouter's offline capability table. Fetched once per pick — the
        # ranking function itself stays DB-free. An absent table yields {} and
        # EntroRouter degrades to the constant-C selector.
        caps: dict = {}
        if _entrorouter_enabled():
            caps = store.fetch_lane_capabilities(task_class)
        for source, candidates in (
            ("region", store.fetch_region_candidates(
                task_class, objective_domain, code_region, node_type, min_samples)
                if (objective_domain and code_region) else []),
            ("domain", store.fetch_domain_candidates(
                task_class, objective_domain, node_type, min_samples)
                if objective_domain else []),
        ):
            pick, margin = _select_best_lane_scored(
                candidates, bandit_on, ucb_c, contextual,
                task_class, node_type, objective_domain, code_region, caps)
            if not pick:
                continue
            overridden = _equirouter_pick(store, candidates, pick, task_class,
                                          node_type, objective_domain, code_region,
                                          min_samples)
            if overridden != pick:
                # The Borda pick is an aggregate of other slices' orderings, not
                # a margin comparison against this slice's runner-up, so the
                # margin no longer describes the lane that was chosen.
                return _detail(overridden, None, source)
            return _detail(pick, margin, source)
        row = store.fetch_global_best(task_class, node_type, min_samples)
        # The global table carries no z-score, so bandit ordering degrades to
        # relative_advantage DESC and there is no runner-up to measure against.
        return _detail(f"{row[0]}|{row[1]}|{row[2]}" if row else "", None, "global")
    finally:
        store.close()


def _detail(pick: str, margin: float | None, source: str) -> dict:
    parts = (pick or "").split("|")
    return {
        "pick": pick or "",
        "lane": parts[0] if parts and parts[0] else "",
        "adv": parts[1] if len(parts) > 1 else "",
        "runs": parts[2] if len(parts) > 2 else "",
        "margin": margin,
        "source": source if pick else "",
    }


def _equirouter_pick(store, candidates: list, legacy: str, task_class: str,
                     node_type: str, objective_domain: str,
                     code_region: str, min_samples: int) -> str:
    """Re-order a slice's pick by the cross-slice Borda ranking.

    Only engages when at least two slices carry a preference — with one slice
    there is nothing to aggregate, and that slice's own ordering is exactly
    what ``_select_best_lane`` already consumed. Keeping the single-slice path
    untouched is what lets the UCB exploration bonus keep working where it is
    the only signal available.

    The winning lane must already be in ``candidates`` (the lanes that cleared
    the sample floor for the requested slice), so this can reorder qualified
    lanes but can never invent one — the cold-start invariant that routing
    never routes to a lane with no evidence is preserved.

    The returned string keeps the bash ``lane|adv|runs`` shape, carrying the
    chosen lane's own ``relative_advantage`` so callers that diff the string
    against the bash port still see a value from that lane.
    """
    if not legacy or not _equirouter_enabled() or not objective_domain:
        return legacy
    slices = _slice_rankings(store, task_class, node_type,
                             objective_domain, code_region, min_samples)
    if len(slices) < 2:
        return legacy

    allowed = {r[0] for r in candidates}
    ranked = [lane for lane, _ in _borda(slices) if lane in allowed]
    if not ranked or ranked[0] == legacy.split("|")[0]:
        return legacy
    for r in candidates:
        if r[0] == ranked[0]:
            return f"{r[0]}|{r[1]}|{r[2]}"
    return legacy


def _select_best_lane_scored(candidates: list,
                             bandit_on: bool, ucb_c: float, contextual: int,
                             task_class: str, node_type: str,
                             objective_domain: str, code_region: str,
                             capabilities: dict | None = None):
    """The lane picker, returning ``(pick, margin)``.

    ``margin`` is the winning UCB score minus the runner-up's — how confidently
    the router preferred this lane over its nearest alternative. It is computed
    and discarded today, which is why no escalation rule can be calibrated: the
    router's own uncertainty signal dies at the return statement. UCCI consumes
    it as the raw input to an isotonic error map, so a thin margin reads as a
    high predicted error rate.

    ``None`` (not ``0.0``) when the margin is *unknown* rather than zero: bandit
    off, fewer than two candidates, or a later override (EquiRouter / NeuralUCB)
    that moved the pick to something this comparison does not describe. Keeping
    unknown distinct from certain is load-bearing — a calibration fitted on
    zeros would read every unknown as maximal disagreement.

    ``capabilities`` maps lane -> offline relative advantage and is optional so
    the pure ranking math stays DB-free; the caller fetches it. EntroRouter is a
    no-op when it is absent or empty.
    """
    if not candidates:
        return "", None
    if not bandit_on:
        # Legacy face: take the first row (already sorted by relative_advantage DESC).
        first = candidates[0]
        return f"{first[0]}|{first[1]}|{first[2]}", None
    entro = None
    if capabilities and _entrorouter_enabled():
        try:
            anchor = float(os.environ.get("MO_ROUTER_ANCHOR", "0.5"))
            tau = float(os.environ.get("MO_ROUTER_ENTROPY_TAU", "3"))
        except (TypeError, ValueError):
            anchor, tau = 0.5, 3.0
        if anchor > 0.0:
            entro = (capabilities, anchor, tau)
    total_runs = sum(int(r["runs_count"]) for r in candidates) or 1
    scored = []
    for r in candidates:
        z = float(r["z_score_advantage"] or 0.0)
        n = max(int(r["runs_count"]), 1)
        # UCB1 with slice-wide N_total — favours lanes with high z and low n.
        bonus = ucb_c * math.sqrt(2.0 * math.log(total_runs + 1) / n)
        score = z + bonus
        if entro:
            caps, anchor, tau = entro
            floor = _recovery_floor(float(caps.get(r[0], 0.0) or 0.0), n, anchor, tau)
            if floor > score:
                score = floor
        scored.append((score, r))
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
    margin = scored[0][0] - scored[1][0] if len(scored) >= 2 else None
    return f"{best[0]}|{best[1]}|{best[2]}", margin


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
    tie-break signal); this function is ranking math only — no SQL. The
    margin half of ``_select_best_lane_scored`` is dropped here so callers
    that only want the lane keep their existing signature.
    """
    return _select_best_lane_scored(
        candidates, bandit_on, ucb_c, contextual,
        task_class, node_type, objective_domain, code_region)[0]



