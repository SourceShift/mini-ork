#!/usr/bin/env bash
# coalition_gate.sh — Pre-synthesis family-diversity + ρ hard-block gate.
#
# Implements the ρ threshold + family-diversity precondition for any
# panel-based synthesis path. The gate is designed to fire AFTER the
# lens nodes have written their execution_traces but BEFORE the
# synthesizer/publisher consumes them, so the framework refuses to
# blend a structurally-compromised panel into a "consensus" synthesis.
#
# Why this exists:
#   Bertalanič 2026 ("The Cost of Consensus in Homogeneous Multi-Agent
#   Debate", arxiv:2605.00914) demonstrates empirically that homogeneous
#   N-agent debates are WORSE than single-agent self-correction on the
#   same compute budget — the family-diversity precondition is
#   load-bearing for the multi-agent claim, not decorative. Rajan 2025
#   ("CodeX-Verify", arxiv:2511.16708) proves a multi-agent submodular
#   gain holds IFF pairwise output correlation ρ stays below ~0.25.
#   Until today the framework MEASURED ρ post-cycle for telemetry only.
#   This gate converts measurement into enforcement.
#
# Public API:
#   mo_check_panel_coalition <panel_run_id> <recipe>
#       → emits structured JSON on stdout:
#         { "verdict": "panel_diverse" | "COALITION_ABORT",
#           "reason":  "ok" | "high_rho" | "family_collision" | "both",
#           "rho":     <float>,
#           "rho_threshold": <float>,
#           "family_distribution": { "<family>": <count>, ... },
#           "lens_count":   <int>,
#           "family_count": <int>,
#           "rationale":    "<one sentence>" }
#       rc=0 when both checks pass (or topology library unavailable —
#         fail-open by design; the gate cannot block when it cannot
#         measure, but emits verdict=indeterminate so callers can log).
#       rc=1 when COALITION_ABORT triggers.
#
# Env knobs:
#   MO_RHO_THRESHOLD            ρ ceiling. Default 0.25 (Rajan 2025).
#   MO_FAMILY_DIVERSITY_GATE    "strict" | "advisory". Default "strict".
#                               strict   → family_count < lens_count fails.
#                               advisory → emits warning but rc=0.
#
# Requires: bash 4+, python3, sqlite3 (via lib/topology_metrics.sh).

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

mo_check_panel_coalition() {
  local panel_run_id="${1:?panel_run_id required}"
  local recipe="${2:?recipe required}"
  local rho_threshold="${MO_RHO_THRESHOLD:-0.25}"
  local gate_mode="${MO_FAMILY_DIVERSITY_GATE:-strict}"

  # Source topology_metrics.sh for measure_rho. Without it we can't
  # compute the diversity signals → fail-open with verdict=indeterminate
  # so the gate doesn't block the framework on every run when topology
  # measurement is absent.
  if [ ! -f "${MINI_ORK_ROOT}/lib/topology_metrics.sh" ]; then
    printf '{"verdict":"indeterminate","reason":"topology_lib_missing","rationale":"lib/topology_metrics.sh not found; gate cannot measure ρ — fail-open"}\n'
    return 0
  fi
  # shellcheck source=lib/topology_metrics.sh
  source "${MINI_ORK_ROOT}/lib/topology_metrics.sh" 2>/dev/null || true
  if ! declare -f measure_rho > /dev/null 2>&1; then
    printf '{"verdict":"indeterminate","reason":"measure_rho_unavailable","rationale":"lib/topology_metrics.sh sourced but measure_rho function missing — fail-open"}\n'
    return 0
  fi

  # Measure ρ. measure_rho returns 0.0 when fewer than 2 traces exist
  # (single-agent run — nothing to be coalition-of).
  local rho
  rho=$(measure_rho "$panel_run_id" 2>/dev/null || echo "0.0")

  # Compute family-distribution from execution_traces for this panel run.
  local family_json
  family_json=$(python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$panel_run_id" "${MINI_ORK_ROOT}/config/agents.yaml" <<'PY'
import json, os, sqlite3, sys

db, panel_run_id, agents_yaml = sys.argv[1], sys.argv[2], sys.argv[3]

# Best-effort agents.yaml parse — supports plain YAML lane: family
# mapping under a `lanes:` key. If pyyaml is absent we fall back to a
# regex parser; if agents.yaml is missing we use the canonical-name
# table baked into topology_metrics.sh.
LANE_TO_FAMILY = {
    "sonnet": "anthropic", "opus": "anthropic",
    "glm": "zhipu", "glm_lens": "zhipu",
    "kimi": "moonshot", "kimi_lens": "moonshot",
    "codex": "openai", "codex_lens": "openai",
    "deepseek": "deepseek", "decomposer": "deepseek",
    "gemini": "google",
    "minimax": "minimax", "minimax_lens": "minimax",
}
if os.path.exists(agents_yaml):
    try:
        import yaml  # type: ignore
        with open(agents_yaml) as f:
            cfg = yaml.safe_load(f) or {}
        lanes = cfg.get("lanes", {}) or {}
        for lane, fam in lanes.items():
            LANE_TO_FAMILY.setdefault(lane.lower(), str(fam).lower())
    except ImportError:
        import re
        with open(agents_yaml) as f:
            in_lanes = False
            for line in f:
                line = line.rstrip()
                if re.match(r"^lanes:\s*$", line):
                    in_lanes = True
                    continue
                if in_lanes:
                    if line and not line.startswith((" ", "\t")):
                        in_lanes = False
                        continue
                    m = re.match(r"^\s+([\w_-]+):\s*([\w_-]+)\s*$", line)
                    if m:
                        LANE_TO_FAMILY.setdefault(m.group(1).lower(),
                                                   m.group(2).lower())

def family_of(version_id):
    if not version_id:
        return "unknown"
    base = version_id.split("-")[0].lower()
    return LANE_TO_FAMILY.get(base, base)

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT trace_id, agent_version_id
    FROM execution_traces
    WHERE trace_id LIKE ? OR trace_id LIKE ?
""", (f"%{panel_run_id}%", f"tr-%-{panel_run_id}%")).fetchall()
con.close()

lenses  = [r["agent_version_id"] for r in rows if r["agent_version_id"]]
distrib = {}
for v in lenses:
    f = family_of(v)
    distrib[f] = distrib.get(f, 0) + 1

print(json.dumps({
    "lens_count":   len(lenses),
    "family_count": len(distrib),
    "family_distribution": distrib,
}))
PY
)

  local lens_count family_count
  lens_count=$(echo "$family_json"   | jq -r '.lens_count   // 0')
  family_count=$(echo "$family_json" | jq -r '.family_count // 0')

  python3 - "$rho" "$rho_threshold" "$lens_count" "$family_count" "$gate_mode" "$family_json" <<'PY'
import json, sys

rho_s, rho_thr_s, lc_s, fc_s, mode, fam_json = sys.argv[1:7]
rho           = float(rho_s)
rho_threshold = float(rho_thr_s)
lens_count    = int(lc_s)
family_count  = int(fc_s)
distrib       = json.loads(fam_json).get("family_distribution", {})

# Single-agent / no-trace runs cannot be a coalition of anyone.
if lens_count < 2:
    print(json.dumps({
        "verdict": "panel_diverse",
        "reason": "single_agent_run",
        "rho": rho,
        "rho_threshold": rho_threshold,
        "lens_count": lens_count,
        "family_count": family_count,
        "family_distribution": distrib,
        "rationale": ("fewer than 2 lens traces present — coalition check "
                      "not applicable")
    }))
    sys.exit(0)

high_rho   = rho >= rho_threshold
collision  = family_count < lens_count

if not high_rho and not collision:
    print(json.dumps({
        "verdict": "panel_diverse",
        "reason": "ok",
        "rho": rho,
        "rho_threshold": rho_threshold,
        "lens_count": lens_count,
        "family_count": family_count,
        "family_distribution": distrib,
        "rationale": (f"ρ={rho:.3f} < {rho_threshold:.3f}, "
                      f"family_count={family_count} == lens_count="
                      f"{lens_count} — panel is family-diverse")
    }))
    sys.exit(0)

reason = "both" if (high_rho and collision) else \
         ("high_rho" if high_rho else "family_collision")
parts = []
if high_rho:
    parts.append(f"ρ={rho:.3f} >= {rho_threshold:.3f} (Rajan 2025 submodularity ceiling)")
if collision:
    parts.append(f"family_count={family_count} < lens_count={lens_count} (Bertalanič 2026 family-diversity precondition violated)")

# Advisory mode emits the verdict but never blocks dispatch.
if mode.lower() == "advisory":
    print(json.dumps({
        "verdict": "panel_diverse",
        "reason": f"advisory_{reason}",
        "rho": rho,
        "rho_threshold": rho_threshold,
        "lens_count": lens_count,
        "family_count": family_count,
        "family_distribution": distrib,
        "advisory_note": "; ".join(parts),
        "rationale": ("MO_FAMILY_DIVERSITY_GATE=advisory — emitting warning "
                      "but proceeding with synthesis")
    }))
    sys.exit(0)

print(json.dumps({
    "verdict": "COALITION_ABORT",
    "reason": reason,
    "rho": rho,
    "rho_threshold": rho_threshold,
    "lens_count": lens_count,
    "family_count": family_count,
    "family_distribution": distrib,
    "rationale": "; ".join(parts),
    "remediation": ("widen family diversity in config/agents.yaml (one "
                    "lane per family), OR accept the coalition-flagged "
                    "lens reports without synthesis, OR set "
                    "MO_FAMILY_DIVERSITY_GATE=advisory to bypass")
}))
sys.exit(1)
PY
}

# Self-test entry point. Constructs a fake state.db with two execution_traces
# and probes the gate in strict + advisory modes.
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  set -Eeuo pipefail
  _td=$(mktemp -d)
  trap 'rm -rf "$_td"' EXIT
  export MINI_ORK_DB="$_td/state.db"
  export MINI_ORK_ROOT
  # Build a minimal execution_traces schema sufficient for measure_rho
  # + the family-distribution query.
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id           TEXT PRIMARY KEY,
  agent_version_id   TEXT,
  reviewer_verdict   TEXT,
  verifier_output    TEXT
);
CREATE TABLE IF NOT EXISTS panel_topology_telemetry (
  telemetry_id        TEXT PRIMARY KEY,
  panel_run_id        TEXT,
  recipe              TEXT,
  rho                 REAL,
  context_distance    REAL,
  inductive_distance  REAL,
  agent_count         INTEGER,
  n_traces            INTEGER,
  quadrant            TEXT
);
""")
prun = "run-fixture-12345"
# Fixture: 4 lenses, all same family (anthropic) — family_collision = true.
for v in [("tr-glm-1-"+prun,  "sonnet",  "APPROVE: looks good"),
          ("tr-kimi-1-"+prun, "opus",    "APPROVE: looks good"),
          ("tr-cdx-1-"+prun,  "sonnet",  "APPROVE: looks good"),
          ("tr-mxi-1-"+prun,  "opus",    "APPROVE: looks good")]:
    con.execute("INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES (?,?,?)", v)
con.commit()
con.close()
PY

  echo "── fixture A (4-lens single-family panel, strict mode) ──"
  out_a=$(mo_check_panel_coalition "run-fixture-12345" "refactor-audit" || true)
  echo "$out_a" | jq -c .
  [ "$(echo "$out_a" | jq -r .verdict)" = "COALITION_ABORT" ] && \
    [ "$(echo "$out_a" | jq -r .reason)" != "ok" ] || {
    echo "FIXTURE A FAILED (expected COALITION_ABORT)" >&2; exit 1; }

  echo "── fixture B (same panel, advisory mode) ──"
  out_b=$(MO_FAMILY_DIVERSITY_GATE=advisory mo_check_panel_coalition "run-fixture-12345" "refactor-audit")
  echo "$out_b" | jq -c .
  [ "$(echo "$out_b" | jq -r .verdict)" = "panel_diverse" ] && \
    [[ "$(echo "$out_b" | jq -r .reason)" =~ ^advisory_ ]] || {
    echo "FIXTURE B FAILED (expected advisory_*)" >&2; exit 1; }

  echo "── fixture C (heterogeneous panel — same panel_run_id, replace lenses) ──"
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM execution_traces")
prun = "run-fixture-diverse"
# 4 different families.
for v in [("tr-1-"+prun, "glm",     "APPROVE A"),
          ("tr-2-"+prun, "kimi",    "REJECT B"),
          ("tr-3-"+prun, "codex",   "APPROVE C"),
          ("tr-4-"+prun, "minimax", "APPROVE D")]:
    con.execute("INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES (?,?,?)", v)
con.commit()
con.close()
PY
  out_c=$(mo_check_panel_coalition "run-fixture-diverse" "refactor-audit")
  echo "$out_c" | jq -c .
  [ "$(echo "$out_c" | jq -r .verdict)" = "panel_diverse" ] && \
    [ "$(echo "$out_c" | jq -r .reason)" = "ok" ] || {
    echo "FIXTURE C FAILED (expected panel_diverse / reason=ok)" >&2; exit 1; }

  echo "── fixture D (single-agent run — not a coalition) ──"
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM execution_traces")
con.execute("INSERT INTO execution_traces (trace_id, agent_version_id, reviewer_verdict) VALUES (?,?,?)",
            ("tr-1-run-fixture-single", "sonnet", "APPROVE"))
con.commit()
con.close()
PY
  out_d=$(mo_check_panel_coalition "run-fixture-single" "code-fix")
  echo "$out_d" | jq -c .
  [ "$(echo "$out_d" | jq -r .verdict)" = "panel_diverse" ] && \
    [ "$(echo "$out_d" | jq -r .reason)" = "single_agent_run" ] || {
    echo "FIXTURE D FAILED" >&2; exit 1; }

  echo "all four self-test fixtures passed."
fi
