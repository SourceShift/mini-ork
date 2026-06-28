#!/usr/bin/env bash
# lane_router.sh — GRPO-style relative-advantage lane routing.
#
# Track B item 4 (arXiv:2601.22607 verifiable-reward GRPO + 2603.02701
# Graph-GRPO for multi-agent topologies). When a recipe dispatches a
# lens panel (codex + kimi + glm + minimax in parallel), each lens
# produces a comparable trajectory. We score each by the normalized
# cross-objective reward_g (set by trace_store.sh from
# reward_value/anchor/direction) and compute:
#
#   relative_advantage[i] = score[i] - mean(scores in group)
#
# The group key is (objective_domain, task_class, node_type, code_region) when
# code_region is present, and the legacy (objective_domain, task_class,
# node_type) key when code_region is NULL/absent. score is sourced only from
# reward_g; rows with reward_g IS NULL are skipped — falling back to
# process_reward or status would reintroduce raw cross-objective reward
# contamination the normalized reward_g column exists to remove.
#
# Positive advantage = "this lens outperformed peers on this
# objective_domain + task_class + node_type". Negative = "this lens
# underperformed". Aggregated into agent_performance_memory.
# relative_advantage and used by the lane router to bias future picks
# toward consistently-winning lanes.
#
# Public API:
#   lane_router_recompute_advantages [--since EPOCH]
#       Walk recent execution_traces grouped by (objective_domain,
#       task_class, node_type). For each group, compute per-lane scores
#       from normalized reward_g plus the group mean, then UPSERT
#       agent_performance_memory rows with relative_advantage. Rows
#       with reward_g IS NULL are skipped.
#
#       frc-a5: when migration-0045's `defect_attributions` table is
#       present, the per-region (acc_region) slice also folds each
#       row's exponentially decayed `penalty` into the blamed lane's
#       `lane_region_advantage.relative_advantage`. The decay formula is
#       `decay = 0.5 ** (age_days / decay_halflife_days)` and `penalty`
#       is signed negative (range [-1, 0]), so blamed lanes drop and
#       unblamed lanes (no matching attribution) are unchanged. Global
#       (agent_performance_memory) and per-domain (lane_domain_advantage)
#       slices are intentionally untouched — penalties are region-scoped
#       by design.
#
#   lane_router_preferred_lane <task_class> <node_type> [objective_domain] [code_region]
#       Print the lane (model lane name) with highest relative_advantage
#       for the given slice, with sample_size >= 3 to avoid noise.
#       Falls back to the lane configured in agents.yaml when no data.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# v0.2-pt36 RLM-3: DB access now flows through the policy_store seam over
# lib/db_open.sh. STATE_DB is resolved per-call via $(mo_store_db_path) so
# MO_STORE_DB / MO_STORE_BACKEND=postgres can route without forking this lib.
# SQLite default (no env override) resolves to the same path as the old
# ${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db} convention.
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/policy_store.sh"

lane_router_recompute_advantages() {
  local since=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --since) since="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  mo_store_assert_sqlite
  local STATE_DB
  STATE_DB="$(mo_store_db_path)"
  python3 - "$STATE_DB" "$since" <<'PY'
import json, sqlite3, sys, datetime
from collections import defaultdict

db, since_str = sys.argv[1:3]
since_iso = datetime.datetime.utcfromtimestamp(int(since_str)).strftime(
    '%Y-%m-%dT%H:%M:%S.000Z'
)

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row

cols = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
code_region_expr = "code_region" if "code_region" in cols else "NULL AS code_region"

# Group traces by (objective_domain, task_class, node_type, code_region) when
# code_region is present. NULL/absent code_region is kept on the exact legacy
# (objective_domain, task_class, node_type) path for backward compatibility.
# "lane" derives from agent_version_id (mini-ork stores it as the model_lane
# string, e.g. "codex_lens"). score is sourced only from normalized reward_g.
rows = con.execute(f"""
    SELECT objective_domain, task_class, agent_version_id,
           verifier_output, reward_g, {code_region_expr}
      FROM execution_traces
     WHERE created_at >= ?
       AND task_class IS NOT NULL AND task_class <> ''
       AND agent_version_id IS NOT NULL AND agent_version_id <> ''
       AND objective_domain IS NOT NULL AND objective_domain <> ''
       AND reward_g IS NOT NULL
""", (since_iso,)).fetchall()

def _score(row):
    # score is the normalized cross-objective reward_g only. No fallback to
    # process_reward or status — that would reintroduce raw cross-objective
    # reward contamination the reward_g column exists to remove.
    return float(row["reward_g"])

def _node_type(row):
    # node_type is stored in verifier_output JSON as {"node_type": "..."}
    try:
        vo = json.loads(row["verifier_output"] or "{}")
        return vo.get("node_type") or "unknown"
    except Exception:
        return "unknown"

# Build groups keyed by (objective_domain, task_class, node_type, code_region).
groups = defaultdict(list)
for r in rows:
    code_region = (r["code_region"] or "").strip() if "code_region" in r.keys() else ""
    groups[(r["objective_domain"], r["task_class"], _node_type(r), code_region)].append({
        "lane":      r["agent_version_id"],
        "score":     _score(r),
        "task_class": r["task_class"],
    })

# Accumulate two slices in one pass (review-36 #193):
#   acc        — (lane, task_class) GLOBAL slice → agent_performance_memory
#                (cross-domain, backward-compatible — unchanged readers keep
#                working).
#   acc_domain — (lane, task_class, node_type, objective_domain) PER-DOMAIN
#                slice → lane_domain_advantage (migration 0043). This is the
#                durable per-objective_domain storage the router prefers; it
#                no longer collides across domains the way a single
#                agent_performance_memory row did.
#   acc_region — (lane, task_class, node_type, objective_domain, code_region)
#                REGION slice → lane_region_advantage. Populated only for
#                non-empty code_region so NULL-region behavior stays exactly
#                on the existing global/domain paths.
acc = defaultdict(lambda: {"adv_sum": 0.0, "n": 0, "wins": 0,
                          "node_types": defaultdict(int),
                          "objective_domains": defaultdict(int)})
acc_domain = defaultdict(lambda: {"adv_sum": 0.0, "n": 0, "wins": 0})
acc_region = defaultdict(lambda: {"adv_sum": 0.0, "n": 0, "wins": 0})
for (_od, _tc, _nt, _cr), members in groups.items():
    if len(members) < 2:
        continue  # need at least 2 lenses for group-relative comparison
    mean = sum(m["score"] for m in members) / len(members)
    for m in members:
        adv = m["score"] - mean
        key = (m["lane"], m["task_class"])
        acc[key]["adv_sum"] += adv
        acc[key]["n"] += 1
        if adv > 0:
            acc[key]["wins"] += 1
        acc[key]["node_types"][_nt] += 1
        acc[key]["objective_domains"][_od] += 1
        # Per-domain slice: keep node_type + objective_domain in the key so
        # domains and node types are isolated at the storage row, not folded.
        dkey = (m["lane"], m["task_class"], _nt, _od)
        acc_domain[dkey]["adv_sum"] += adv
        acc_domain[dkey]["n"] += 1
        if adv > 0:
            acc_domain[dkey]["wins"] += 1
        if _cr:
            rkey = (m["lane"], m["task_class"], _nt, _od, _cr)
            acc_region[rkey]["adv_sum"] += adv
            acc_region[rkey]["n"] += 1
            if adv > 0:
                acc_region[rkey]["wins"] += 1

# frc-a5: fold decayed defect_attributions into region-scoped advantage.
# Aggregation is per (lane, code_region, task_class) — apply the summed
# effective penalty (penalty * decay) to every acc_region key whose
# (lane, task_class, code_region) matches. acc and acc_domain stay
# untouched so no-region routing behavior is preserved.
_penalty_by_key = defaultdict(float)  # (lane, code_region, task_class) -> summed effective penalty
try:
    _has_defect_attributions = con.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='defect_attributions' LIMIT 1"
    ).fetchone()
except Exception:
    _has_defect_attributions = None

if _has_defect_attributions:
    _penalty_rows = con.execute("""
        SELECT lane, code_region, task_class,
               penalty, decay_halflife_days, ts
          FROM defect_attributions
         WHERE penalty IS NOT NULL AND penalty <> 0
    """).fetchall()
    _now_utc = datetime.datetime.utcnow()
    _penalty_by_key = defaultdict(float)
    for _pr in _penalty_rows:
        _pen = float(_pr["penalty"])
        _hlf_raw = _pr["decay_halflife_days"]
        try:
            _hlf = float(_hlf_raw) if _hlf_raw is not None else 30.0
        except (TypeError, ValueError):
            continue  # non-numeric halflife → skip; do not silently invent
        if _hlf <= 0:
            continue  # invalid halflife → skip; do not silently invent
        _ts_raw = _pr["ts"]
        # SQLite emits 'YYYY-MM-DDTHH:MM:SS.mmmZ'. Parse tolerantly (with or
        # without fractional seconds); a missing %S previously made EVERY row
        # fail and silently skip, killing the whole fold (frc-a5 critic fix).
        _tsv = str(_ts_raw).strip().rstrip('Z').replace('T', ' ')
        _ts = None
        for _fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                _ts = datetime.datetime.strptime(_tsv, _fmt)
                break
            except ValueError:
                _ts = None
        if _ts is None:
            continue  # unparseable ts → skip
        _age_days = max((_now_utc - _ts).total_seconds() / 86400.0, 0.0)
        _decay = 0.5 ** (_age_days / _hlf)
        _effective = _pen * _decay  # signed negative penalty (severity-scaled, decayed)
        _penalty_by_key[
            (_pr["lane"], _pr["code_region"], _pr["task_class"])
        ] += _effective
    # NOTE: penalty is applied AFTER the adv_sum/n average at the region upsert
    # below — NOT added to adv_sum here. Folding into adv_sum diluted the penalty
    # by run count (a -0.5 penalty became -0.5/n), so magnitude wrongly depended
    # on sample size (frc-a5 critic fix).

# Upsert agent_performance_memory.
# PRIMARY KEY is (agent_version_id, task_class) so we collapse node_types
# into the most common one per (lane, task_class).
upserted = 0
for (lane, tc), stats in acc.items():
    avg_adv = stats["adv_sum"] / max(stats["n"], 1)
    top_node = max(stats["node_types"].items(), key=lambda kv: kv[1])[0] \
               if stats["node_types"] else None
    success_rate = stats["wins"] / max(stats["n"], 1)
    con.execute("""
        INSERT INTO agent_performance_memory
            (agent_version_id, role, model, task_class,
             runs_count, success_count,
             relative_advantage,
             last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(agent_version_id, task_class) DO UPDATE SET
            role               = excluded.role,
            model              = excluded.model,
            runs_count         = excluded.runs_count,
            success_count      = excluded.success_count,
            relative_advantage = excluded.relative_advantage,
            last_updated       = excluded.last_updated
    """, (lane, top_node or lane, lane, tc,
          stats["n"], stats["wins"], round(avg_adv, 4)))
    upserted += 1

# Per-domain slice → lane_domain_advantage (migration 0043). Keyed by
# (agent_version_id, task_class, node_type, objective_domain) so objective
# domains no longer collide on a single row. Defensive CREATE keeps recompute
# from crashing on a DB where migration 0043 has not yet applied (review-38).
con.execute("""
    CREATE TABLE IF NOT EXISTS lane_domain_advantage (
      agent_version_id   TEXT    NOT NULL,
      task_class         TEXT    NOT NULL,
      node_type          TEXT    NOT NULL DEFAULT '',
      objective_domain   TEXT    NOT NULL DEFAULT '',
      relative_advantage REAL    NOT NULL DEFAULT 0.0,
      runs_count         INTEGER NOT NULL DEFAULT 0,
      success_count      INTEGER NOT NULL DEFAULT 0,
      last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain)
    )
""")
for (lane, tc, nt, od), stats in acc_domain.items():
    avg_adv = stats["adv_sum"] / max(stats["n"], 1)
    con.execute("""
        INSERT INTO lane_domain_advantage
            (agent_version_id, task_class, node_type, objective_domain,
             relative_advantage, runs_count, success_count, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(agent_version_id, task_class, node_type, objective_domain)
        DO UPDATE SET
            relative_advantage = excluded.relative_advantage,
            runs_count         = excluded.runs_count,
            success_count      = excluded.success_count,
            last_updated       = excluded.last_updated
    """, (lane, tc, nt or '', od or '', round(avg_adv, 4),
          stats["n"], stats["wins"]))

# Region slice → lane_region_advantage. This additive table keeps the existing
# lane_domain_advantage key untouched while making non-NULL code_region a real
# routing dimension.
con.execute("""
    CREATE TABLE IF NOT EXISTS lane_region_advantage (
      agent_version_id   TEXT    NOT NULL,
      task_class         TEXT    NOT NULL,
      node_type          TEXT    NOT NULL DEFAULT '',
      objective_domain   TEXT    NOT NULL DEFAULT '',
      code_region        TEXT    NOT NULL DEFAULT '',
      relative_advantage REAL    NOT NULL DEFAULT 0.0,
      runs_count         INTEGER NOT NULL DEFAULT 0,
      success_count      INTEGER NOT NULL DEFAULT 0,
      last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain, code_region)
    )
""")
con.execute("""
    CREATE INDEX IF NOT EXISTS idx_lane_region_adv
      ON lane_region_advantage(task_class, node_type, objective_domain, code_region, relative_advantage DESC)
""")
for (lane, tc, nt, od, cr), stats in acc_region.items():
    avg_adv = stats["adv_sum"] / max(stats["n"], 1)
    # Apply decayed defect penalty AFTER averaging so its magnitude does not
    # depend on run count (frc-a5 critic fix). Region-scoped only.
    if cr:
        avg_adv += _penalty_by_key.get((lane, cr, tc), 0.0)
    con.execute("""
        INSERT INTO lane_region_advantage
            (agent_version_id, task_class, node_type, objective_domain,
             code_region, relative_advantage, runs_count, success_count,
             last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(agent_version_id, task_class, node_type, objective_domain, code_region)
        DO UPDATE SET
            relative_advantage = excluded.relative_advantage,
            runs_count         = excluded.runs_count,
            success_count      = excluded.success_count,
            last_updated       = excluded.last_updated
    """, (lane, tc, nt or '', od or '', cr or '', round(avg_adv, 4),
          stats["n"], stats["wins"]))

con.commit()
con.close()
print(upserted)
PY
}

lane_router_preferred_lane() {
  local task_class="${1:?task_class required}"
  local node_type="${2:-}"
  local objective_domain="${3:-}"
  local code_region="${4:-}"
  mo_store_assert_sqlite
  local STATE_DB
  STATE_DB="$(mo_store_db_path)"
  local _min_samples="${MO_LEARNING_MIN_SAMPLES:-3}"

  # review-37/38: single-quote-escape every interpolated value (SQLite CLI
  # has no bind params here) so quotes/inputs can't break or inject SQL.
  local _tc_e=${task_class//\'/\'\'}
  local _nt_e=${node_type//\'/\'\'}
  local _od_e=${objective_domain//\'/\'\'}
  local _cr_e=${code_region//\'/\'\'}

  # Region slice: only active for non-empty code_region. When code_region is
  # absent, do not change the legacy/domain routing path at all.
  if [ -n "$objective_domain" ] && [ -n "$code_region" ]; then
    local rwhere="task_class='$_tc_e' AND objective_domain='$_od_e' AND code_region='$_cr_e' AND runs_count >= ${_min_samples}"
    [ -n "$node_type" ] && rwhere="$rwhere AND node_type='$_nt_e'"
    local _rhit
    _rhit=$(sqlite3 -separator '|' "$STATE_DB" \
      "SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count
         FROM lane_region_advantage
        WHERE $rwhere
        ORDER BY relative_advantage DESC, runs_count DESC
        LIMIT 1;" 2>/dev/null)
    if [ -n "$_rhit" ]; then
      echo "$_rhit"
      return 0
    fi
  fi

  # review-36 #193: prefer the durable per-objective_domain slice
  # (lane_domain_advantage, migration 0043) when a domain is supplied and it
  # clears the sample floor. This is what makes per-domain learning real at
  # the storage layer rather than slice-mean only.
  if [ -n "$objective_domain" ]; then
    local dwhere="task_class='$_tc_e' AND objective_domain='$_od_e' AND runs_count >= ${_min_samples}"
    [ -n "$node_type" ] && dwhere="$dwhere AND node_type='$_nt_e'"
    local _hit
    _hit=$(sqlite3 -separator '|' "$STATE_DB" \
      "SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count
         FROM lane_domain_advantage
        WHERE $dwhere
        ORDER BY relative_advantage DESC, runs_count DESC
        LIMIT 1;" 2>/dev/null)
    if [ -n "$_hit" ]; then
      echo "$_hit"
      return 0
    fi
  fi

  # Fallback: cross-domain global slice (no domain given, or the domain has
  # not yet reached the sample floor — cold-start safe).
  local where="task_class='$_tc_e' AND runs_count >= ${_min_samples}"
  [ -n "$node_type" ] && where="$where AND (role='$_nt_e' OR model='$_nt_e')"
  sqlite3 -separator '|' "$STATE_DB" \
    "SELECT agent_version_id, printf('%.3f', relative_advantage), runs_count
       FROM agent_performance_memory
      WHERE $where
      ORDER BY relative_advantage DESC, runs_count DESC
      LIMIT 1;"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "lane_router.sh — source me and call lane_router_recompute_advantages / lane_router_preferred_lane"
fi
