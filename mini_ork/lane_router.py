"""GRPO relative-advantage lane routing — Python port of lib/lane_router.sh (Tier A).

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


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB") or os.environ.get("MO_STORE_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def recompute_advantages(since: int = 0, db: str | None = None) -> int:
    since_iso = datetime.datetime.utcfromtimestamp(int(since)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")

    SHRINK_K = int(os.environ.get("MO_LEARNING_SHRINKAGE_K", "5"))
    DECAY_ALPHA = float(os.environ.get("MO_LEARNING_DECAY_ALPHA", "0.30"))
    HALFLIFE = float(os.environ.get("MO_LEARNING_HALFLIFE_DAYS", "14"))
    TIEBREAK = int(os.environ.get("MO_LEARNING_TIEBREAK", "1"))

    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row

    cols = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
    code_region_expr = "code_region" if "code_region" in cols else "NULL AS code_region"
    ts_expr = "created_at" if "created_at" in cols else "NULL AS created_at"
    cost_expr = "cost_usd" if "cost_usd" in cols else "0.0 AS cost_usd"

    prior_apm, prior_domain, prior_region = {}, {}, {}
    try:
        for row in con.execute(
                "SELECT agent_version_id, task_class, relative_advantage "
                "FROM agent_performance_memory").fetchall():
            prior_apm[(row[0], row[1])] = row[2]
    except Exception:
        pass

    con.execute("""CREATE TABLE IF NOT EXISTS lane_domain_advantage (
          agent_version_id TEXT NOT NULL, task_class TEXT NOT NULL,
          node_type TEXT NOT NULL DEFAULT '', objective_domain TEXT NOT NULL DEFAULT '',
          relative_advantage REAL NOT NULL DEFAULT 0.0, runs_count INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0,
          last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain))""")
    try:
        for row in con.execute(
                "SELECT agent_version_id, task_class, node_type, objective_domain, "
                "relative_advantage FROM lane_domain_advantage").fetchall():
            prior_domain[(row[0], row[1], row[2], row[3])] = row[4]
    except Exception:
        pass

    con.execute("""CREATE TABLE IF NOT EXISTS lane_region_advantage (
          agent_version_id TEXT NOT NULL, task_class TEXT NOT NULL,
          node_type TEXT NOT NULL DEFAULT '', objective_domain TEXT NOT NULL DEFAULT '',
          code_region TEXT NOT NULL DEFAULT '', relative_advantage REAL NOT NULL DEFAULT 0.0,
          runs_count INTEGER NOT NULL DEFAULT 0, success_count INTEGER NOT NULL DEFAULT 0,
          last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain, code_region))""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_lane_region_adv ON lane_region_advantage(
          task_class, node_type, objective_domain, code_region, relative_advantage DESC)""")
    try:
        for row in con.execute(
                "SELECT agent_version_id, task_class, node_type, objective_domain, "
                "code_region, relative_advantage FROM lane_region_advantage").fetchall():
            prior_region[(row[0], row[1], row[2], row[3], row[4])] = row[5]
    except Exception:
        pass

    rows = con.execute(f"""
        SELECT objective_domain, task_class, agent_version_id, verifier_output,
               reward_g, {code_region_expr}, {ts_expr}, {cost_expr}
          FROM execution_traces
         WHERE created_at >= ? AND task_class IS NOT NULL AND task_class <> ''
           AND agent_version_id IS NOT NULL AND agent_version_id <> ''
           AND objective_domain IS NOT NULL AND objective_domain <> ''
           AND reward_g IS NOT NULL""", (since_iso,)).fetchall()

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
            w = math.exp(-math.log(2) * age_days / HALFLIFE)
        else:
            w = 1.0
        groups[(r["objective_domain"], r["task_class"], _node_type(r), code_region)].append(
            {"lane": r["agent_version_id"], "score": float(r["reward_g"]),
             "task_class": r["task_class"], "cost": cost, "weight": w})

    acc = defaultdict(lambda: {"shr_sum": 0.0, "groups": 0, "wins": 0,
                               "node_types": defaultdict(int),
                               "objective_domains": defaultdict(int)})
    acc_domain = defaultdict(lambda: {"shr_sum": 0.0, "groups": 0, "wins": 0})
    acc_region = defaultdict(lambda: {"shr_sum": 0.0, "groups": 0, "wins": 0})
    for (_od, _tc, _nt, _cr), members in groups.items():
        if len(members) < 2:
            continue
        sum_w = sum(m["weight"] for m in members)
        if sum_w <= 0:
            continue
        wmean = sum(m["weight"] * m["score"] for m in members) / sum_w
        lane_bonus = {}
        scores = [m["score"] for m in members]
        if TIEBREAK != 0 and min(scores) == max(scores):
            costs = [m["cost"] for m in members]
            lo, hi = min(costs), max(costs)
            if lo != hi:
                for m in members:
                    lane_bonus[m["lane"]] = 0.1 - 0.2 * (m["cost"] - lo) / (hi - lo)
        by_lane = defaultdict(lambda: {"ws": 0.0, "w": 0.0, "n": 0})
        for m in members:
            b = by_lane[m["lane"]]
            b["ws"] += m["weight"] * m["score"]
            b["w"] += m["weight"]
            b["n"] += 1
        for lane, b in by_lane.items():
            lane_mean = b["ws"] / b["w"] if b["w"] > 0 else 0.0
            lane_adv = lane_mean - wmean + lane_bonus.get(lane, 0.0)
            n_in_group = b["n"]
            shrink_factor = n_in_group / (n_in_group + SHRINK_K) if SHRINK_K > 0 else 1.0
            shrunken = lane_adv * shrink_factor
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
            if _cr:
                rr = acc_region[(lane, _tc, _nt, _od, _cr)]
                rr["shr_sum"] += shrunken
                rr["groups"] += 1
                rr["wins"] += wins

    _penalty_by_key = defaultdict(float)
    try:
        has_defect = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='defect_attributions' LIMIT 1").fetchone()
    except Exception:
        has_defect = None
    if has_defect:
        _now_utc = datetime.datetime.utcnow()
        for pr in con.execute(
                "SELECT lane, code_region, task_class, penalty, decay_halflife_days, ts "
                "FROM defect_attributions WHERE penalty IS NOT NULL AND penalty <> 0").fetchall():
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
        if prior is None or DECAY_ALPHA >= 1.0:
            return batch
        if DECAY_ALPHA <= 0.0:
            return prior
        try:
            p = float(prior)
        except (TypeError, ValueError):
            return batch
        return DECAY_ALPHA * batch + (1.0 - DECAY_ALPHA) * p

    upserted = 0
    for (lane, tc), stats in acc.items():
        if stats["groups"] <= 0:
            continue
        new_rel_adv = _ema(prior_apm.get((lane, tc)), stats["shr_sum"] / stats["groups"])
        top_node = (max(stats["node_types"].items(), key=lambda kv: kv[1])[0]
                    if stats["node_types"] else None)
        con.execute("""INSERT INTO agent_performance_memory
                (agent_version_id, role, model, task_class, runs_count, success_count,
                 relative_advantage, last_updated)
                VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(agent_version_id, task_class) DO UPDATE SET
                role=excluded.role, model=excluded.model, runs_count=excluded.runs_count,
                success_count=excluded.success_count,
                relative_advantage=excluded.relative_advantage, last_updated=excluded.last_updated""",
                    (lane, top_node or lane, lane, tc, stats["groups"], stats["wins"],
                     round(new_rel_adv, 4)))
        upserted += 1

    for (lane, tc, nt, od), stats in acc_domain.items():
        if stats["groups"] <= 0:
            continue
        new_rel_adv = _ema(prior_domain.get((lane, tc, nt or "", od or "")),
                           stats["shr_sum"] / stats["groups"])
        con.execute("""INSERT INTO lane_domain_advantage
                (agent_version_id, task_class, node_type, objective_domain,
                 relative_advantage, runs_count, success_count, last_updated)
                VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(agent_version_id, task_class, node_type, objective_domain)
                DO UPDATE SET relative_advantage=excluded.relative_advantage,
                runs_count=excluded.runs_count, success_count=excluded.success_count,
                last_updated=excluded.last_updated""",
                    (lane, tc, nt or "", od or "", round(new_rel_adv, 4),
                     stats["groups"], stats["wins"]))

    for (lane, tc, nt, od, cr), stats in acc_region.items():
        if stats["groups"] <= 0:
            continue
        new_rel_adv = _ema(prior_region.get((lane, tc, nt or "", od or "", cr or "")),
                           stats["shr_sum"] / stats["groups"])
        if cr:
            new_rel_adv += _penalty_by_key.get((lane, cr, tc), 0.0)
        con.execute("""INSERT INTO lane_region_advantage
                (agent_version_id, task_class, node_type, objective_domain, code_region,
                 relative_advantage, runs_count, success_count, last_updated)
                VALUES (?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(agent_version_id, task_class, node_type, objective_domain, code_region)
                DO UPDATE SET relative_advantage=excluded.relative_advantage,
                runs_count=excluded.runs_count, success_count=excluded.success_count,
                last_updated=excluded.last_updated""",
                    (lane, tc, nt or "", od or "", cr or "", round(new_rel_adv, 4),
                     stats["groups"], stats["wins"]))

    con.commit()
    con.close()
    return upserted


def preferred_lane(task_class: str, node_type: str = "", objective_domain: str = "",
                   code_region: str = "", db: str | None = None) -> str:
    """Highest-advantage lane for the slice (sample floor MO_LEARNING_MIN_SAMPLES,
    default 3). Region → domain → global, matching bash. Returns
    'lane|adv|runs' (bash's pipe format) or '' when no slice clears the floor."""
    min_samples = int(os.environ.get("MO_LEARNING_MIN_SAMPLES", "3"))
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    try:
        if objective_domain and code_region:
            where = ("task_class=? AND objective_domain=? AND code_region=? AND runs_count>=?")
            params = [task_class, objective_domain, code_region, min_samples]
            if node_type:
                where += " AND node_type=?"
                params.append(node_type)
            row = con.execute(
                f"SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count "
                f"FROM lane_region_advantage WHERE {where} "
                f"ORDER BY relative_advantage DESC, runs_count DESC LIMIT 1", params).fetchone()
            if row:
                return f"{row[0]}|{row[1]}|{row[2]}"
        if objective_domain:
            where = "task_class=? AND objective_domain=? AND runs_count>=?"
            params = [task_class, objective_domain, min_samples]
            if node_type:
                where += " AND node_type=?"
                params.append(node_type)
            row = con.execute(
                f"SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count "
                f"FROM lane_domain_advantage WHERE {where} "
                f"ORDER BY relative_advantage DESC, runs_count DESC LIMIT 1", params).fetchone()
            if row:
                return f"{row[0]}|{row[1]}|{row[2]}"
        where = "task_class=? AND runs_count>=?"
        params = [task_class, min_samples]
        if node_type:
            where += " AND (role=? OR model=?)"
            params += [node_type, node_type]
        row = con.execute(
            f"SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count "
            f"FROM agent_performance_memory WHERE {where} "
            f"ORDER BY relative_advantage DESC, runs_count DESC LIMIT 1", params).fetchone()
        return f"{row[0]}|{row[1]}|{row[2]}" if row else ""
    finally:
        con.close()
