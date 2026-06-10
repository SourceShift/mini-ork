#!/usr/bin/env bash
# context_assembler.sh — Bounded ContextPack builder.
#
# Public API:
#   context_assemble <task_brief_path> <workflow_node_name>
#       → emits ContextPack JSON on stdout
#
# ContextPack fields:
#   task_brief, relevant_files[], prior_similar_runs[],
#   known_failure_modes[], user_preferences, verifier_contract,
#   constraints[], forbidden_fallbacks[]
#
# Token budget: MINI_ORK_CTX_BUDGET_TOKENS (default 64000). Prefers
# recent/high-confidence items; truncates with summary marker.
# Every included item carries a cite: <source> field.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# desc: Build a bounded ContextPack for task_brief_path at workflow_node_name.
#       Queries task_memory and failure_memory tables in MINI_ORK_DB.
#       Emits JSON ContextPack on stdout.
context_assemble() {
  local task_brief_path="${1:?task_brief_path required}"
  local workflow_node="${2:?workflow_node_name required}"

  if [[ ! -f "$task_brief_path" ]]; then
    echo "context_assemble: task_brief_path not found: $task_brief_path" >&2
    return 1
  fi

  local brief_content
  brief_content="$(< "$task_brief_path")"
  local budget="${MINI_ORK_CTX_BUDGET_TOKENS:-64000}"

  # Load artifact contract if available
  local verifier_contract="{}"
  if declare -f artifact_contract_load > /dev/null 2>&1; then
    local task_class
    task_class="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('task_class',''))
except Exception:
    print('')
" "$brief_content" 2>/dev/null || echo "")"
    if [[ -n "$task_class" ]]; then
      verifier_contract="$(artifact_contract_load "$task_class" 2>/dev/null || echo '{}')"
    fi
  fi

  python3 - \
    "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$brief_content" \
    "$workflow_node" \
    "$budget" \
    "$verifier_contract" \
    <<'PY'
import sqlite3, json, sys, re, time

db          = sys.argv[1]
brief_raw   = sys.argv[2]
node_name   = sys.argv[3]
budget      = int(sys.argv[4])
vc_raw      = sys.argv[5]

def approx_tokens(s):
    """Rough estimate: 1 token ~ 4 chars."""
    return max(1, len(s) // 4)

try:
    brief = json.loads(brief_raw)
except Exception:
    brief = {"raw": brief_raw}

task_class = brief.get("task_class", "")
try:
    verifier_contract = json.loads(vc_raw)
except Exception:
    verifier_contract = {}

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# --- Prior similar runs from execution_traces ---------------------------
prior_runs = []
try:
    rows = con.execute("""
        SELECT trace_id, task_class, status, cost_usd, duration_ms, created_at
        FROM execution_traces
        WHERE task_class = ?
        ORDER BY created_at DESC LIMIT 10
    """, (task_class,)).fetchall()
    for r in rows:
        prior_runs.append({
            "cite": f"execution_traces/{r['trace_id']}",
            "trace_id": r["trace_id"],
            "status": r["status"],
            "cost_usd": r["cost_usd"],
            "duration_ms": r["duration_ms"],
            "created_at": r["created_at"],
        })
except Exception:
    pass

# --- Known failure modes from gradient_records -------------------------
# task_class column is the primary join (populated by gradient_store);
# the legacy target-substring match stays as fallback for rows stored
# before the column existed.
failure_modes = []
try:
    rows = con.execute("""
        SELECT target, signal, suggested_change, confidence
        FROM gradient_records
        WHERE (task_class = ? OR target LIKE ?) AND confidence >= 0.6
        ORDER BY confidence DESC LIMIT 10
    """, (task_class, f"%{task_class}%")).fetchall()
    for r in rows:
        failure_modes.append({
            "cite": f"gradient_records/{r['target']}",
            "target": r["target"],
            "signal": r["signal"],
            "suggested_change": r["suggested_change"],
            "confidence": r["confidence"],
        })
except Exception:
    pass

# --- User preferences (from config if present) -------------------------
user_prefs = {}
try:
    import os
    cfg_path = os.environ.get("MINI_ORK_HOME", ".mini-ork") + "/config/user_preferences.json"
    with open(cfg_path) as f:
        user_prefs = json.load(f)
    user_prefs["cite"] = cfg_path
except Exception:
    pass

# --- Constraints and forbidden fallbacks from config -------------------
constraints = []
forbidden_fallbacks = []
try:
    import os
    cfg_path = os.environ.get("MINI_ORK_HOME", ".mini-ork") + "/config/constraints.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    constraints = cfg.get("constraints", [])
    forbidden_fallbacks = cfg.get("forbidden_fallbacks", [])
except Exception:
    pass

con.close()

# --- Token budget enforcement ------------------------------------------
pack = {
    "task_brief": {"content": brief, "cite": "task_brief_path"},
    "workflow_node": node_name,
    "verifier_contract": {"content": verifier_contract, "cite": "artifact_contract"},
    "prior_similar_runs": prior_runs,
    "known_failure_modes": failure_modes,
    "user_preferences": user_prefs,
    "constraints": constraints,
    "forbidden_fallbacks": forbidden_fallbacks,
    "assembled_at": int(time.time()),
    "budget_tokens": budget,
}

serialized = json.dumps(pack)
tokens_used = approx_tokens(serialized)

if tokens_used > budget:
    # Trim prior_runs first, then failure_modes
    while tokens_used > budget and pack["prior_similar_runs"]:
        pack["prior_similar_runs"].pop()
        pack["_truncated"] = True
        tokens_used = approx_tokens(json.dumps(pack))

    while tokens_used > budget and pack["known_failure_modes"]:
        pack["known_failure_modes"].pop()
        pack["_truncated"] = True
        tokens_used = approx_tokens(json.dumps(pack))

    pack["_truncation_summary"] = (
        f"Context truncated to fit {budget} token budget; "
        f"oldest prior_runs and low-confidence failure_modes removed."
    )

pack["tokens_estimated"] = approx_tokens(json.dumps(pack))
print(json.dumps(pack))
PY
}

# desc: Emit learned failure modes for task_class as a compact markdown block
#       suitable for direct prompt injection. Prints NOTHING when no learnings
#       exist (callers can append output unconditionally). Confidence floor 0.6.
context_failure_modes_md() {
  local task_class="${1:?task_class required}"
  local limit="${2:-5}"
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0
  python3 - "$MINI_ORK_DB" "$task_class" "$limit" <<'PY'
import sqlite3, sys

db, task_class, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    rows = con.execute("""
        SELECT target, signal, suggested_change
        FROM gradient_records
        WHERE (task_class = ? OR target LIKE ?) AND confidence >= 0.6
        ORDER BY confidence DESC, created_at DESC LIMIT ?
    """, (task_class, f"%{task_class}%", limit)).fetchall()
except sqlite3.OperationalError:
    rows = []
finally:
    con.close()

if rows:
    print("--- Learned failure modes (from prior runs of this task class) ---")
    print("Avoid repeating these known issues:")
    for target, signal, change in rows:
        print(f"- [{target}] {signal.strip()}")
        print(f"  Fix applied going forward: {change.strip()}")
    print("--- /learned failure modes ---")
PY
}

# desc: Emit prior same-task_class run outcomes as a compact markdown block
#       suitable for direct prompt injection (the prior_similar_runs slice of
#       the ContextPack, without the full context_assemble JSON envelope).
#       Excludes the CURRENT run's own traces via $MINI_ORK_RUN_ID — without
#       that, the classify trace written moments earlier shows up as its own
#       "prior" memory. Prints NOTHING when no prior runs exist.
context_prior_runs_md() {
  local task_class="${1:?task_class required}"
  local limit="${2:-5}"
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] || return 0
  python3 - "$MINI_ORK_DB" "$task_class" "$limit" "${MINI_ORK_RUN_ID:-}" <<'PY'
import sqlite3, sys

db, task_class, limit, cur_run = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
try:
    rows = con.execute("""
        SELECT trace_id, status, cost_usd, duration_ms, created_at
        FROM execution_traces
        WHERE task_class = ?
          AND (? = '' OR run_id IS NULL OR run_id != ?)
        ORDER BY created_at DESC LIMIT ?
    """, (task_class, cur_run, cur_run, limit)).fetchall()
except sqlite3.OperationalError:
    rows = []
finally:
    con.close()

if rows:
    n_ok = sum(1 for r in rows if (r[1] or "") == "success")
    print("--- Prior runs of this task class (memory) ---")
    print(f"{len(rows)} most recent: {n_ok} success / {len(rows) - n_ok} other. "
          "Calibrate plan scope and verifier strictness against these outcomes:")
    for trace_id, status, cost, dur_ms, created in rows:
        cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
        dur_s = f"{dur_ms // 1000}s" if isinstance(dur_ms, int) else "?"
        print(f"- {trace_id}: {status or 'unknown'} (cost {cost_s}, {dur_s})")
    print("--- /prior runs ---")
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "context_assembler.sh — source me and call context_assemble <task_brief_path> <node_name>"
fi
