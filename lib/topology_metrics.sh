#!/usr/bin/env bash
# topology_metrics.sh — E-MO-01: 3-axis panel-topology measurement.
#
# Computes realised (ρ, C, I) per panel run + classifies into one of 8
# quadrants from the framework doc:
# docs/_meta/research/20260602-2030-context-formation-diversity-framework-multi-agent-panels.md
#
# Public API (positional args; sourced from a bash 4+ shell):
#   measure_rho      <panel_run_id>           → float on stdout
#   measure_C        <panel_run_id>           → float on stdout
#   measure_I        <panel_run_id>           → float on stdout
#   measure_topology <panel_run_id> <recipe>  → writes 1 row to panel_topology_telemetry
#                                                + emits the telemetry_id on stdout
#
# Requires:
#   - MINI_ORK_DB env var (path to state.db)
#   - MINI_ORK_ROOT env var (for config/agents.yaml lookup)
#   - python3 + sqlite3 + pyyaml
#
# All functions are SAFE to call on panel runs with < 2 traces — they
# emit 0.0 for each metric (single agent → no pairwise distance defined).

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# desc: ensure the panel_topology_telemetry table exists (idempotent).
_topology_ensure_table() {
  [ "${_MO_TOPOLOGY_SCHEMA_INIT:-0}" = "1" ] && return 0
  local mig="$MINI_ORK_ROOT/db/migrations/0015_panel_topology_telemetry.sql"
  if [ ! -f "$mig" ]; then
    return 0
  fi
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$mig" <<'PY'
import sqlite3, sys
db, mig = sys.argv[1], sys.argv[2]
with open(mig) as f:
    sql = f.read()
con = sqlite3.connect(db)
con.executescript(sql)
con.commit()
con.close()
PY
  _MO_TOPOLOGY_SCHEMA_INIT=1
  export _MO_TOPOLOGY_SCHEMA_INIT
}

# desc: Measure ρ — output correlation across the panel run's traces.
#       Proxy: Krippendorff-α-like agreement over reviewer_verdict strings.
#       Returns: float on stdout in [-1.0, 1.0]
measure_rho() {
  local panel_run_id="${1:?panel_run_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$panel_run_id" <<'PY'
import sqlite3, sys, statistics
db, panel_run_id = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# Pull verdicts for this panel run. panel_run_id maps to multiple traces
# via mini_ork_run_id (encoded in trace_id prefix as 'tr-<role>-<ts>-<panel_run_id>')
# OR via the run_id column when available. For now use a substring match
# on trace_id since trace_id encodes the panel run.
rows = con.execute("""
    SELECT trace_id, agent_version_id, reviewer_verdict, verifier_output
    FROM execution_traces
    WHERE trace_id LIKE ? OR trace_id LIKE ?
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchall()
con.close()

verdicts = [r["reviewer_verdict"] or r["verifier_output"] or "" for r in rows
            if (r["reviewer_verdict"] or r["verifier_output"])]
if len(verdicts) < 2:
    print(0.0)
    sys.exit(0)

# Agreement proxy: fraction of pairwise verdicts whose first 50 chars match.
# Crude but cheap; a proper embeddings-based ρ comes in E-MO-06 (Krippendorff).
def head(v, n=50):
    return (v or "").strip().lower()[:n]

pairs = 0
agreeing = 0
for i in range(len(verdicts)):
    for j in range(i+1, len(verdicts)):
        pairs += 1
        if head(verdicts[i]) and head(verdicts[i]) == head(verdicts[j]):
            agreeing += 1
        elif head(verdicts[i]) and head(verdicts[j]):
            # Token-level Jaccard as soft-agreement signal
            ti = set(head(verdicts[i], 200).split())
            tj = set(head(verdicts[j], 200).split())
            if ti and tj:
                jacc = len(ti & tj) / len(ti | tj)
                if jacc >= 0.5:
                    agreeing += 1

rho = (agreeing / pairs) if pairs > 0 else 0.0
print(round(rho, 4))
PY
}

# desc: Measure C — context formation distance across the panel run's traces.
#       Mean pairwise Jaccard distance over (files_read ∪ tool_call signatures).
#       Returns: float on stdout in [0.0, 1.0]
measure_C() {
  local panel_run_id="${1:?panel_run_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$panel_run_id" <<'PY'
import sqlite3, sys, json
db, panel_run_id = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT trace_id, files_read, tool_calls
    FROM execution_traces
    WHERE trace_id LIKE ? OR trace_id LIKE ?
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchall()
con.close()

contexts = []
for r in rows:
    try:
        files = json.loads(r["files_read"] or "[]")
    except Exception:
        files = []
    try:
        tools = json.loads(r["tool_calls"] or "[]")
    except Exception:
        tools = []
    # Tool-call signature: tool name + first input key's value (stable hash)
    tool_sigs = []
    for tc in tools:
        if isinstance(tc, dict):
            name = tc.get("tool", "?")
            inp = tc.get("input", {}) or {}
            # Sign by tool name + first input key (concrete enough to dedup, not so concrete it never matches)
            first_key = next(iter(inp.keys()), "")
            first_val = str(inp.get(first_key, ""))[:80]
            tool_sigs.append(f"{name}:{first_key}={first_val}")
    ctx = frozenset(list(files) + tool_sigs)
    if ctx:
        contexts.append(ctx)

if len(contexts) < 2:
    print(0.0)
    sys.exit(0)

# Mean pairwise Jaccard distance
def jaccard_dist(a, b):
    union = a | b
    if not union:
        return 0.0
    return 1.0 - (len(a & b) / len(union))

total = 0.0
pairs = 0
for i in range(len(contexts)):
    for j in range(i+1, len(contexts)):
        total += jaccard_dist(contexts[i], contexts[j])
        pairs += 1
mean_C = total / pairs if pairs > 0 else 0.0
print(round(mean_C, 4))
PY
}

# desc: Measure I — inductive prior distance across the panel run's traces.
#       Looks up each trace's agent_version_id family via config/agents.yaml.
#       Returns: float on stdout in [0.0, 1.0]
measure_I() {
  local panel_run_id="${1:?panel_run_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$panel_run_id" "$MINI_ORK_ROOT" <<'PY'
import sqlite3, sys, os, re
try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False
db, panel_run_id, root = sys.argv[1], sys.argv[2], sys.argv[3]

# Build lane → family map from config/agents.yaml
lane_to_family = {}
agents_yaml = os.path.join(root, "config", "agents.yaml")
if HAVE_YAML and os.path.isfile(agents_yaml):
    try:
        with open(agents_yaml) as f:
            data = yaml.safe_load(f) or {}
        for lane, family in (data.get("lanes") or {}).items():
            lane_to_family[lane] = str(family).strip()
    except Exception:
        pass

# Family canonicalisation — these are the distinct training lineages
FAMILY_CANON = {
    "opus": "anthropic", "sonnet": "anthropic", "haiku": "anthropic",
    "opus_lens": "anthropic", "spec_reviewer": "anthropic", "reviewer": "anthropic",
    "brain": "anthropic", "spec_author": "anthropic", "planner": "anthropic",
    "researcher": "anthropic", "implementer": "anthropic", "worker": "anthropic",
    "verifier": "anthropic", "reflector": "anthropic", "publisher": "anthropic",
    "rollback": "anthropic", "bdd_runner": "anthropic", "healer": "anthropic",
    "worker_default": "anthropic", "reviewer_default": "anthropic",
    "glm": "zhipu", "glm_lens": "zhipu",
    "kimi": "moonshot", "kimi_lens": "moonshot",
    "codex": "openai", "codex_lens": "openai",
    "deepseek": "deepseek", "decomposer": "deepseek",
    "gemini": "google",
    "minimax": "minimax", "minimax_lens": "minimax",
}
def family_of(version_id):
    """Map agent_version_id (e.g. 'glm_lens-v3' or 'sonnet') to canonical family."""
    if not version_id:
        return "unknown"
    base = version_id.split("-")[0].lower()
    # First check direct lane_to_family lookup
    if base in lane_to_family:
        target = lane_to_family[base]
        return FAMILY_CANON.get(target, target)
    # Then check canon
    return FAMILY_CANON.get(base, base)

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT trace_id, agent_version_id
    FROM execution_traces
    WHERE trace_id LIKE ? OR trace_id LIKE ?
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchall()
con.close()

families = [family_of(r["agent_version_id"]) for r in rows if r["agent_version_id"]]
if len(families) < 2:
    print(0.0)
    sys.exit(0)

# Pairwise distance: 1 if different family, 0 if same
total = 0.0
pairs = 0
for i in range(len(families)):
    for j in range(i+1, len(families)):
        total += (0.0 if families[i] == families[j] else 1.0)
        pairs += 1
mean_I = total / pairs if pairs > 0 else 0.0
print(round(mean_I, 4))
PY
}

# desc: Classify (ρ, C, I) into one of 8 quadrants from the framework doc.
#       Thresholds: rho >= 0.5 = HIGH; C >= 0.3 = HIGH; I >= 0.5 = HIGH.
_topology_quadrant() {
  local rho="$1" C="$2" I="$3"
  python3 -c "
rho, C, I = float('$rho'), float('$C'), float('$I')
rh = 'high' if rho >= 0.5 else 'low'
ch = 'high' if C   >= 0.3 else 'low'
ih = 'high' if I   >= 0.5 else 'low'

key = (rh, ch, ih)
quadrants = {
    ('high','low','low'):   'coalition',
    ('low','low','low'):    'noise',
    ('high','high','low'):  'convergent_corroboration',
    ('low','high','low'):   'genuine_perspective_split',
    ('high','low','high'):  'forced_consensus_shared_evidence',
    ('low','low','high'):   'prior_driven_disagreement',
    ('high','high','high'): 'submodular_gain_target',
    ('low','high','high'):  'high_variance_discovery',
}
print(quadrants.get(key, 'unclassified'))
"
}

# desc: Measure all three axes + classify + persist. The canonical
#       post-cycle hook.
#       Args: <panel_run_id> <recipe>
#       Emits: telemetry_id on stdout
measure_topology() {
  local panel_run_id="${1:?panel_run_id required}"
  local recipe="${2:?recipe required}"
  _topology_ensure_table

  local rho C I quadrant
  rho=$(measure_rho "$panel_run_id")
  C=$(measure_C   "$panel_run_id")
  I=$(measure_I   "$panel_run_id")
  quadrant=$(_topology_quadrant "$rho" "$C" "$I")

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
                "$panel_run_id" "$recipe" "$rho" "$C" "$I" "$quadrant" <<'PY'
import sqlite3, sys, uuid
(db, panel_run_id, recipe, rho, C, I, quadrant) = sys.argv[1:]
telemetry_id = f"pt-{panel_run_id[:16]}-{uuid.uuid4().hex[:6]}"

con = sqlite3.connect(db)

# Count contributing traces
n_traces = con.execute("""
    SELECT COUNT(*) FROM execution_traces
    WHERE trace_id LIKE ? OR trace_id LIKE ?
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchone()[0]
agent_count = con.execute("""
    SELECT COUNT(DISTINCT agent_version_id) FROM execution_traces
    WHERE (trace_id LIKE ? OR trace_id LIKE ?) AND agent_version_id != ''
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchone()[0]

con.execute("""
    INSERT INTO panel_topology_telemetry
        (telemetry_id, panel_run_id, recipe, rho, context_distance,
         inductive_distance, agent_count, n_traces, quadrant)
    VALUES (?,?,?,?,?,?,?,?,?)
""", (telemetry_id, panel_run_id, recipe,
      float(rho), float(C), float(I), agent_count, n_traces, quadrant))
con.commit()
con.close()
print(telemetry_id)
PY
}

# Self-test entry point.
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "topology_metrics.sh — source me and call measure_topology <panel_run_id> <recipe>" >&2
fi
