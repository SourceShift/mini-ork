#!/usr/bin/env bash
# gate_registry.sh — 6 built-in gate types + register/evaluate API.
#
# Built-in gate types:
#   deterministic_verifier | reviewer_gate | human_gate |
#   budget_gate | scope_gate | deployment_gate
#
# Public API:
#   gate_register  <gate_type> <condition_fn_or_path> <task_class_filter>
#                  [--safety]
#   gate_evaluate  <gate_id> <context_json>   → pass|fail|defer
#   gate_list      [--task-class X]
#   gate_run_all   <task_class> <context_json> → emits per-gate result JSON

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_VALID_GATE_TYPES=(
  deterministic_verifier
  reviewer_gate
  human_gate
  budget_gate
  scope_gate
  deployment_gate
  custom
)

_gate_ensure_table() {
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_GATE_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.execute("""
    CREATE TABLE IF NOT EXISTS gate_registry (
        gate_id             TEXT PRIMARY KEY,
        gate_type           TEXT NOT NULL,
        condition           TEXT NOT NULL,
        task_class_filter   TEXT,
        safety              INTEGER NOT NULL DEFAULT 0,
        active              INTEGER NOT NULL DEFAULT 1,
        registered_at       INTEGER NOT NULL
    )
""")
con.commit()
con.close()
PY
  _MO_GATE_SCHEMA_INIT=1
  export _MO_GATE_SCHEMA_INIT
}

# desc: Register a gate. condition_fn_or_path is either a bash function name
#       (loaded at eval time) or a path to a script that exits 0=pass, 1=fail,
#       2=defer. task_class_filter='' means applies to all task classes.
#       Pass --safety to mark gate as safety-critical (cannot be removed).
gate_register() {
  local gate_type="${1:?gate_type required}"
  local condition="${2:?condition_fn_or_path required}"
  local task_class_filter="${3:-}"
  local safety=0
  shift 3 2>/dev/null || shift $#
  while [[ $# -gt 0 ]]; do
    case "$1" in --safety) safety=1 ;; esac
    shift
  done

  # Validate gate_type
  local valid=0
  for t in "${_VALID_GATE_TYPES[@]}"; do
    [[ "$gate_type" == "$t" ]] && valid=1 && break
  done
  if [[ $valid -eq 0 ]]; then
    echo "gate_register: unknown gate_type '${gate_type}'. Valid: ${_VALID_GATE_TYPES[*]}" >&2
    return 1
  fi

  _gate_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
    "$gate_type" "$condition" "$task_class_filter" "$safety" <<'PY'
import sqlite3, json, sys, time, uuid
db, gtype, cond, tcf, safety = sys.argv[1:6]
now  = int(time.time())
gid  = f"gate-{gtype[:6]}-{uuid.uuid4().hex[:8]}"
tcf_ = tcf if tcf else None
con  = sqlite3.connect(db)
con.execute("""
    INSERT INTO gate_registry
        (gate_id, gate_type, condition, task_class_filter, safety, active, registered_at)
    VALUES (?,?,?,?,?,1,?)
    ON CONFLICT(gate_id) DO NOTHING
""", (gid, gtype, cond, tcf_, int(safety), now))
con.commit()
con.close()
print(gid)
PY
}

# desc: Evaluate a single gate against context_json.
#       Returns "pass", "fail", or "defer" on stdout.
gate_evaluate() {
  local gate_id="${1:?gate_id required}"
  local context="${2:?context_json required}"
  _gate_ensure_table

  local gate_row
  gate_row="$(python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$gate_id" <<'PY'
import sqlite3, json, sys
db, gid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute("SELECT * FROM gate_registry WHERE gate_id=? AND active=1", (gid,)).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
)"

  if [[ "$gate_row" == "null" ]]; then
    echo "gate_evaluate: gate_id '${gate_id}' not found or inactive" >&2
    echo "fail"
    return 1
  fi

  local gate_type condition
  gate_type="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['gate_type'])" "$gate_row")"
  condition="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['condition'])" "$gate_row")"

  # Built-in gate evaluation logic
  case "$gate_type" in
    budget_gate)
      # condition is a max_cost_usd float or path to budget config
      python3 - "$context" "$condition" <<'PY'
import json, sys
try:
    ctx = json.loads(sys.argv[1])
    limit = float(sys.argv[2])
    cost  = float(ctx.get("cost_usd", 0.0))
    print("pass" if cost <= limit else "fail")
except Exception:
    print("defer")
PY
      ;;
    human_gate)
      # Always defer — requires human resolution
      echo "defer"
      ;;
    scope_gate)
      # condition is a JSON array of allowed task_classes
      python3 - "$context" "$condition" <<'PY'
import json, sys
try:
    ctx       = json.loads(sys.argv[1])
    allowed   = json.loads(sys.argv[2])
    task_cls  = ctx.get("task_class", "")
    print("pass" if task_cls in allowed else "fail")
except Exception:
    print("defer")
PY
      ;;
    deployment_gate|reviewer_gate|deterministic_verifier|custom)
      # condition is a bash function name or script path
      if declare -f "$condition" > /dev/null 2>&1; then
        # It's a registered function — call it
        local rc=0
        "$condition" "$context" >/dev/null 2>&1 || rc=$?
        case $rc in
          0) echo "pass" ;;
          2) echo "defer" ;;
          *) echo "fail" ;;
        esac
      elif [[ -x "$condition" ]]; then
        local rc=0
        "$condition" "$context" >/dev/null 2>&1 || rc=$?
        case $rc in
          0) echo "pass" ;;
          2) echo "defer" ;;
          *) echo "fail" ;;
        esac
      else
        echo "gate_evaluate: condition '${condition}' not a callable function or executable" >&2
        echo "defer"
      fi
      ;;
    *)
      echo "gate_evaluate: unhandled gate_type '${gate_type}'" >&2
      echo "defer"
      ;;
  esac
}

# desc: List all registered gates, optionally filtered by task_class.
#       Emits JSON array on stdout.
gate_list() {
  local task_class=""
  while [[ $# -gt 0 ]]; do
    case "$1" in --task-class) task_class="$2"; shift 2 ;; *) shift ;; esac
  done
  _gate_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$task_class" <<'PY'
import sqlite3, json, sys
db, tc = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
if tc:
    rows = con.execute("""
        SELECT * FROM gate_registry
        WHERE active=1 AND (task_class_filter IS NULL OR task_class_filter=?)
        ORDER BY gate_type
    """, (tc,)).fetchall()
else:
    rows = con.execute(
        "SELECT * FROM gate_registry WHERE active=1 ORDER BY gate_type"
    ).fetchall()
con.close()
print(json.dumps([dict(r) for r in rows]))
PY
}

# desc: Run all applicable gates for task_class against context_json.
#       Emits a JSON object with per-gate verdicts and overall summary.
gate_run_all() {
  local task_class="${1:?task_class required}"
  local context="${2:?context_json required}"

  local gates_json
  gates_json="$(gate_list --task-class "$task_class")"

  python3 - "$gates_json" "$context" <<'PYEOF'
import json, sys, subprocess, os

gates   = json.loads(sys.argv[1])
context = sys.argv[2]

results = []
all_pass  = True
any_defer = False
safety_fail = False

for g in gates:
    gid   = g["gate_id"]
    gtype = g["gate_type"]
    cond  = g["condition"]
    safety = bool(g["safety"])

    # Re-invoke gate_evaluate via sourced shell (simpler: python logic inline)
    verdict = "defer"

    if gtype == "budget_gate":
        try:
            ctx   = json.loads(context)
            limit = float(cond)
            cost  = float(ctx.get("cost_usd", 0.0))
            verdict = "pass" if cost <= limit else "fail"
        except Exception:
            verdict = "defer"
    elif gtype == "human_gate":
        verdict = "defer"
    elif gtype == "scope_gate":
        try:
            ctx     = json.loads(context)
            allowed = json.loads(cond)
            verdict = "pass" if ctx.get("task_class", "") in allowed else "fail"
        except Exception:
            verdict = "defer"
    else:
        # External callable — attempt via subprocess
        mini_ork_root = os.environ.get("MINI_ORK_ROOT", ".")
        try:
            src = f"source {mini_ork_root}/lib/gate_registry.sh 2>/dev/null; gate_evaluate '{gid}' '{context}'"
            proc = subprocess.run(
                ["bash", "-c", src],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "MINI_ORK_DB": os.environ.get("MINI_ORK_DB", "")}
            )
            out = proc.stdout.strip()
            verdict = out if out in ("pass", "fail", "defer") else "defer"
        except Exception:
            verdict = "defer"

    if verdict != "pass":
        all_pass = False
    if verdict == "defer":
        any_defer = True
    if verdict == "fail" and safety:
        safety_fail = True

    results.append({
        "gate_id": gid,
        "gate_type": gtype,
        "safety": safety,
        "verdict": verdict,
    })

summary = {
    "task_class": json.loads(context).get("task_class", "") if context else "",
    "all_pass": all_pass,
    "any_defer": any_defer,
    "safety_violation": safety_fail,
    "gate_count": len(results),
    "gates": results,
}
print(json.dumps(summary))
PYEOF
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "gate_registry.sh — source me and call gate_register / gate_evaluate / gate_list / gate_run_all"
fi
