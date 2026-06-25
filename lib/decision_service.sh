#!/usr/bin/env bash
# lib/decision_service.sh — stateless decision surface for read-path inference.
#
# Single inference-time surface both consumers (eng-team + book-gen) call to
# get a routing / coalition / reward / recursion decision from the learned
# policy. Composes the existing brain libraries without re-implementing any
# of their logic.
#
# Public API:
#   decide <node_type> <task_class> <objective_domain> [segment]
#       Emits a JSON object on stdout:
#         {
#           "route":           "<lane name>",
#           "coalition_ok":    <bool>,
#           "reward_estimate": <float 0.0-1.0>,
#           "recursion_hint":  { ... },
#           "sample_size":     <int>,
#           "segment":         "<segment name>"
#         }
#
# Composition (pure read + compute, no per-request state):
#   - routing        : lane_router_preferred_lane <task_class> <node_type>;
#                      falls back to the agents.yaml lane for that node_type
#                      when the GRPO sample-size floor (>=3) yields no row.
#                      Cold-start safe: decide never invents a lane.
#   - coalition      : slice-aware family-diversity check over the
#                      (objective_domain, task_class, node_type) slice in
#                      execution_traces. coalition_ok=true when sample < 2
#                      (nothing to be a coalition of) or every lens in the
#                      sample maps to a distinct family. Same family-mapping
#                      logic as lib/coalition_gate.sh's
#                      mo_check_panel_coalition.
#   - reward         : mean of normalized reward_g for the slice; falls
#                      back to process_reward when reward_g is NULL or the
#                      column is absent. Empty slice emits 0.0000|0.
#   - recursion_hint : trimmed slice of mo_recursive_policy_json (max_depth,
#                      max_children_per_run, max_total_descendants,
#                      max_parallel_children, default_allow_child_spawn).
#
# Conventions:
#   - All SQLite access flows through lib/policy_store.sh
#     (mo_store_assert_sqlite + mo_store_db_path). MO_STORE_BACKEND routing
#     works the same as the rest of the brain libs.
#   - Strict mode is gated behind the direct-exec check so sourcing this
#     lib into a lenient caller shell does NOT leak `set -u`/`pipefail`
#     (matches lib/lane_router.sh / lib/process_reward.sh /
#     lib/coalition_gate.sh / lib/policy_store.sh).
#   - decide is pure: no per-request state, no side effects, no LLM dispatch.
#     It composes existing read APIs and emits JSON.

# Strict mode only when executed directly — never leak set -u/pipefail onto a
# parent that sources this lib (matches the brain libs this file composes).
[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Source the brain libs the kickoff names. Each one's strict-mode guard
# evaluates false when sourced from another lib (BASH_SOURCE[0] of the
# being-sourced file differs from the parent's $0), so no strict mode leaks
# onto decide's caller shell.
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/policy_store.sh"
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/lane_router.sh"
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/coalition_gate.sh"
# shellcheck disable=SC1091
. "${MINI_ORK_ROOT}/lib/process_reward.sh"

# decide <node_type> <task_class> <objective_domain> [segment]
#   node_type        — e.g. "implementer", "reviewer", "planner"
#   task_class       — e.g. "code-fix", "spec-author"
#   objective_domain — e.g. "eng-team", "book-gen"
#   segment          — optional free-form label echoed back in the JSON
decide() {
  local node_type="${1:?node_type required}"
  local task_class="${2:?task_class required}"
  local objective_domain="${3:-}"
  local segment="${4:-default}"

  # Gate every SQLite-direct helper behind the backend seam.
  mo_store_assert_sqlite
  local STATE_DB
  STATE_DB="$(mo_store_db_path)"

  # 1) Routing — lane_router_preferred_lane with the GRPO sample-size floor
  #    (runs_count >= 3). When the floor yields no row, fall back to the
  #    lane the agents.yaml file maps node_type to. Cold-start safe: we
  #    never invent a lane.
  local learned_route route
  learned_route=$(lane_router_preferred_lane "$task_class" "$node_type" 2>/dev/null \
    | awk -F'|' '{print $1; exit}')
  if [ -n "$learned_route" ]; then
    route="$learned_route"
  else
    route=$(decision_service_default_lane "$node_type")
  fi

  # 1a) Epsilon-greedy exploration — rlm-4b-pre-b. With probability EPSILON
  #     (default 0.10, recovered from the prior bin/mini-ork-execute argmax
  #     so decide callers get the same explore/exploit semantics decide will
  #     replace), replace route with a deterministic exploration lane
  #     picked from config/agents.yaml (excluding the current exploit
  #     route). SEED makes exploration reproducible for proof harnesses;
  #     unset ⇒ SystemRandom so live runs stay non-deterministic by default.
  #     Cold-start guard: only fires when a learned route exists
  #     (learned_route non-empty). When the GRPO floor yields no row,
  #     decide returns the configured default lane unchanged — exploration
  #     must NOT pull a cold-start run off its default lane (this is the
  #     rlm-4b regression's invariant).
  local _epsilon _seed
  _epsilon="${EPSILON:-${MO_LEARNING_EPSILON:-0.10}}"
  _seed="${SEED:-${MO_LEARNING_SEED:-}}"
  if [ -n "$learned_route" ]; then
    local _agents_yaml="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
    [ -f "$_agents_yaml" ] || _agents_yaml="$MINI_ORK_ROOT/config/agents.yaml"
    route=$(MO_DECISION_EPSILON_AGENTS_YAML="$_agents_yaml" \
      python3 - "$route" "$_epsilon" "$_seed" <<'PY'
import os, random, sys, yaml

exploit_route, epsilon_s, seed = sys.argv[1:4]
try:
    epsilon = float(epsilon_s)
except (TypeError, ValueError):
    epsilon = 0.10
if epsilon < 0.0:
    epsilon = 0.0
if epsilon > 1.0:
    epsilon = 1.0

# Seeded RNG for deterministic exploration; SystemRandom when unset so live
# runs remain non-deterministic by default. Matches bin/mini-ork-execute's
# _mo_learning_governed_lane discipline.
if seed:
    try:
        rng = random.Random(int(seed))
    except ValueError:
        rng = random.Random(seed)
else:
    rng = random.SystemRandom()

if rng.random() >= epsilon:
    # No exploration this round — exploit unchanged.
    print(exploit_route)
    sys.exit(0)

# Explore — pick a lane from agents.yaml that's not the current exploit
# route. Sorted + set so the seeded RNG produces stable selections across
# runs with the same seed (avoids hash-ordering drift on Py3 dicts).
agents_yaml = os.environ.get("MO_DECISION_EPSILON_AGENTS_YAML", "")
candidates = []
if agents_yaml and os.path.isfile(agents_yaml):
    try:
        with open(agents_yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        seen = set()
        for v in (cfg.get("lanes") or {}).values():
            if not v or v == exploit_route or v in seen:
                continue
            seen.add(v)
            candidates.append(v)
    except Exception:
        # agents.yaml malformed/unreadable — no exploration candidates;
        # fall back to exploit rather than crashing the caller.
        pass

print(rng.choice(sorted(candidates)) if candidates else exploit_route)
PY
    )
  fi

  # 2) Coalition — slice-aware family-diversity check.
  local coalition_ok
  coalition_ok=$(decision_service_coalition_ok \
    "$objective_domain" "$task_class" "$node_type" "$STATE_DB")

  # 3) Reward estimate — mean of normalized reward_g over the slice,
  #    fallback to process_reward, empty slice → 0.0000.
  local reward_summary
  reward_summary=$(decision_service_reward_summary \
    "$objective_domain" "$task_class" "$node_type" "$STATE_DB")
  local reward_mean reward_sample
  reward_mean=$(printf '%s' "$reward_summary"  | awk -F'|' '{print $1}')
  reward_sample=$(printf '%s' "$reward_summary" | awk -F'|' '{print $2}')
  if [ -z "$reward_mean" ]; then reward_mean="0.0000"; fi
  if [ -z "$reward_sample" ]; then reward_sample="0"; fi

  # 4) Recursion hint — trimmed policy snapshot.
  local recursion_hint
  recursion_hint=$(decision_service_recursion_hint)

  python3 - "$route" "$coalition_ok" "$reward_mean" "$reward_sample" \
                "$recursion_hint" "$segment" <<'PY'
import json, sys
route, coalition_ok, reward_mean, reward_sample, recursion_hint, segment = (
    sys.argv[1:7]
)
out = {
    "route":           route,
    "coalition_ok":    coalition_ok == "1",
    "reward_estimate": float(reward_mean),
    "recursion_hint":  json.loads(recursion_hint),
    "sample_size":     int(reward_sample),
    "segment":         segment,
}
print(json.dumps(out, sort_keys=True))
PY
}

# decision_service_default_lane <node_type>
#   Read agents.yaml lane for a given node_type. Uses the same precedence
#   as lib/lane-helpers.sh's mo_assert_lane_capability:
#     $MINI_ORK_HOME/config/agents.yaml → $MINI_ORK_ROOT/config/agents.yaml.
#   Emits the lane name on stdout; empty if not configured (decide emits
#   empty route and the caller decides what to do — we never invent a lane).
decision_service_default_lane() {
  local node_type="${1:?node_type required}"
  local agents_yaml="${MINI_ORK_HOME:-.mini-ork}/config/agents.yaml"
  [ -f "$agents_yaml" ] || agents_yaml="$MINI_ORK_ROOT/config/agents.yaml"
  [ -f "$agents_yaml" ] || { printf ''; return 0; }

  python3 - "$agents_yaml" "$node_type" <<'PY'
import sys, yaml
path, node_type = sys.argv[1:3]
cfg = yaml.safe_load(open(path, encoding="utf-8")) or {}
lanes = cfg.get("lanes") or {}
lane = lanes.get(node_type) or ""
print(lane)
PY
}

# decision_service_coalition_ok <objective_domain> <task_class> <node_type> <db>
#   Emits "1" on stdout when the slice is family-diverse (or too small to
#   matter), "0" otherwise. Slice key matches the GRPO slice key in
#   lib/lane_router.sh: (objective_domain, task_class, node_type). Family
#   mapping mirrors lib/coalition_gate.sh's canonical LANE_TO_FAMILY plus an
#   override layer from config/agents.yaml — so the predicate stays in
#   lockstep with mo_check_panel_coalition's family-distribution logic
#   without duplicating its panel_run_id-driven read path.
decision_service_coalition_ok() {
  local objective_domain="${1:-}"
  local task_class="${2:?task_class required}"
  local node_type="${3:?node_type required}"
  local db="${4:?db path required}"

  MO_DECISION_SERVICE_ROOT="$MINI_ORK_ROOT" \
  python3 - "$objective_domain" "$task_class" "$node_type" "$db" <<'PY'
import json, os, sqlite3, sys

od, tc, nt, db = sys.argv[1:5]

# Same family mapping as lib/coalition_gate.sh's LANE_TO_FAMILY. An override
# layer from config/agents.yaml is applied so this predicate stays in
# lockstep with mo_check_panel_coalition's family-distribution logic.
LANE_TO_FAMILY = {
    "sonnet": "anthropic", "opus": "anthropic",
    "glm": "zhipu", "glm_lens": "zhipu",
    "kimi": "moonshot", "kimi_lens": "moonshot",
    "codex": "openai", "codex_lens": "openai",
    "deepseek": "deepseek", "decomposer": "deepseek",
    "gemini": "google",
    "minimax": "minimax", "minimax_lens": "minimax",
}
mo_root = os.environ.get("MO_DECISION_SERVICE_ROOT", ".")
for agents_yaml in (
    os.path.join(os.environ.get("MINI_ORK_HOME", ".mini-ork"), "config", "agents.yaml"),
    os.path.join(mo_root, "config", "agents.yaml"),
):
    if not os.path.isfile(agents_yaml):
        continue
    try:
        import yaml  # type: ignore
        with open(agents_yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        for k, v in (cfg.get("lanes") or {}).items():
            LANE_TO_FAMILY.setdefault(str(k).lower(), str(v).lower())
        break
    except Exception:
        # YAML parser unavailable or agents.yaml malformed — fall back to
        # the canonical-name table; we never want this check to crash
        # decide's caller.
        break

def family_of(vid):
    if not vid:
        return "unknown"
    base = vid.split("-")[0].lower()
    return LANE_TO_FAMILY.get(base, base)

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row

# Detect column presence up-front so older state.db schemas (no
# objective_domain / agent_version_id) don't crash this check.
try:
    cols = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
except sqlite3.OperationalError:
    # execution_traces table absent — fall through to "indeterminate,
    # default to ok" matching mo_check_panel_coalition's fail-open design.
    print("1")
    sys.exit(0)

if "agent_version_id" not in cols:
    print("1")
    sys.exit(0)

sql = ["SELECT agent_version_id FROM execution_traces WHERE task_class = ?"]
args = [tc]
if od and "objective_domain" in cols:
    sql[0] += " AND objective_domain = ?"
    args.append(od)
sql[0] += " AND agent_version_id IS NOT NULL AND agent_version_id <> ''"

# node_type lives in verifier_output JSON in current schemas; older
# schemas may store it as a column. Try column first, fall back to JSON.
node_type_filter = ""
if "node_type" in cols:
    node_type_filter = " AND node_type = ?"
    args.append(nt)
final_sql = sql[0] + node_type_filter

try:
    rows = con.execute(final_sql, args).fetchall()
except sqlite3.OperationalError:
    print("1")
    sys.exit(0)

lanes = [r["agent_version_id"] for r in rows if r["agent_version_id"]]

# If the column-based node_type filter found nothing, try the JSON
# verifier_output path so the predicate matches current schemas.
if not rows and "node_type" not in cols:
    try:
        rows2 = con.execute(
            "SELECT agent_version_id, verifier_output FROM execution_traces "
            "WHERE task_class = ?" + (" AND objective_domain = ?" if od and "objective_domain" in cols else ""),
            args[: 2 if od and "objective_domain" in cols else 1],
        ).fetchall()
        lanes = []
        for r in rows2:
            try:
                vo = json.loads(r["verifier_output"] or "{}")
                if vo.get("node_type") == nt:
                    if r["agent_version_id"]:
                        lanes.append(r["agent_version_id"])
            except Exception:
                continue
    except sqlite3.OperationalError:
        pass

families = {family_of(l) for l in lanes}
# Same predicate shape as mo_check_panel_coalition's "ok" branch:
# single-agent / small sample → ok, otherwise distinct-families → ok.
if len(lanes) < 2 or len(families) == len(lanes):
    print("1")
else:
    print("0")
PY
}

# decision_service_reward_summary <objective_domain> <task_class> <node_type> <db>
#   Emits "<mean>|<sample_size>" on stdout. Mean is computed over the
#   slice's normalized reward_g when present; falls back to process_reward
#   when reward_g is NULL across the slice or the column is absent.
#   Empty slice emits "0.0000|0".
decision_service_reward_summary() {
  local objective_domain="${1:-}"
  local task_class="${2:?task_class required}"
  local node_type="${3:?node_type required}"
  local db="${4:?db path required}"

  python3 - "$objective_domain" "$task_class" "$node_type" "$db" <<'PY'
import json, sqlite3, sys

od, tc, nt, db = sys.argv[1:5]

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.row_factory = sqlite3.Row

try:
    cols = {row[1] for row in con.execute("PRAGMA table_info(execution_traces)").fetchall()}
except sqlite3.OperationalError:
    # execution_traces absent — empty slice semantics.
    print("0.0000|0")
    sys.exit(0)

# Prefer reward_g (normalized cross-objective, what lane_router reads).
# Fall back to process_reward when reward_g is absent or all NULL in the
# slice. Mirrors process_reward.sh's fallback discipline.
primary = "reward_g" if "reward_g" in cols else (
    "process_reward" if "process_reward" in cols else None
)
if not primary:
    print("0.0000|0")
    sys.exit(0)

# node_type filter: prefer the column; fall back to JSON verifier_output.
node_type_predicate = ""
node_type_args = []
if "node_type" in cols:
    node_type_predicate = " AND node_type = ?"
    node_type_args = [nt]

where = ["task_class = ?", f"{primary} IS NOT NULL"]
args = [tc]
if od and "objective_domain" in cols:
    where.append("objective_domain = ?")
    args.append(od)
args += node_type_args

try:
    rows = con.execute(
        f"SELECT {primary}, verifier_output FROM execution_traces "
        f"WHERE {' AND '.join(where)}{node_type_predicate}",
        args,
    ).fetchall()
except sqlite3.OperationalError:
    print("0.0000|0")
    sys.exit(0)

# If the column-based node_type filter returned nothing AND node_type
# wasn't a column to begin with, try the JSON verifier_output path so we
# match current schemas.
if not rows and not node_type_predicate and "verifier_output" in cols:
    json_args = [tc]
    if od and "objective_domain" in cols:
        json_args.append(od)
    try:
        rows = con.execute(
            f"SELECT {primary}, verifier_output FROM execution_traces "
            f"WHERE task_class = ?"
            + (" AND objective_domain = ?" if od and "objective_domain" in cols else "")
            + f" AND {primary} IS NOT NULL",
            json_args,
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

vals = []
for r in rows:
    v = r[primary]
    if v is None:
        continue
    try:
        vals.append(float(v))
    except (TypeError, ValueError):
        continue

if not vals:
    print("0.0000|0")
else:
    mean = sum(vals) / len(vals)
    print(f"{round(mean, 4)}|{len(vals)}")
PY
}

# decision_service_recursion_hint — trimmed slice of mo_recursive_policy_json
# (lib/recursive_orchestration.sh). Read-only: env knobs feed the policy,
# decide just projects the slice its callers actually consult. The full
# policy lives in mo_recursive_policy_json; this helper only emits the keys
# decide's downstream callers (eng-team + book-gen spawn gates) need.
decision_service_recursion_hint() {
  python3 - <<'PY'
import json, os
policy = {
    "max_depth": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DEPTH", "2")),
    "max_children_per_run": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_CHILDREN", "4")),
    "max_total_descendants": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DESCENDANTS", "16")),
    "max_parallel_children": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_PARALLEL", "4")),
    "default_allow_child_spawn": os.environ.get("MINI_ORK_ALLOW_CHILD_SPAWN", "0").lower() in {"1", "true", "yes", "on"},
    "default_authority_level": float(os.environ.get("MINI_ORK_CHILD_AUTHORITY", "0.3")),
}
hint = {
    "max_depth":               policy["max_depth"],
    "max_children_per_run":    policy["max_children_per_run"],
    "max_total_descendants":   policy["max_total_descendants"],
    "max_parallel_children":   policy["max_parallel_children"],
    "default_allow_child_spawn": policy["default_allow_child_spawn"],
}
print(json.dumps(hint, sort_keys=True))
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "decision_service.sh — source me and call decide <node_type> <task_class> <objective_domain> [segment]"
fi