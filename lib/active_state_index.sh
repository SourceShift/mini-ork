#!/usr/bin/env bash
# active_state_index.sh — surface live-state index for planner prompts.
#
# HarnessBridge Technique 1 (arxiv:2606.12882):
# Long-horizon agents waste context reconstructing "what state am I in"
# from chronological history. The Active-State Index records
# unresolved errors, open constraints, established facts, pending goals,
# and remaining decision variables in a compact block placed BEFORE
# the projected chronological history.
#
# Mini-ork wiring: invoked from mini_ork.cli.plan via MO_INJECT_LEARNINGS
# block, immediately after the ContextNest atoms wiring restored by
# PR #19 in the Python planner. Adds a top-of-prompt JSON + markdown
# block sourced from live state.db rows.
#
# Public API:
#   mo_active_state_block [task_class] [days_window]
#       Prints a markdown block with embedded JSON to stdout.
#       Returns 0 when at least one section has content; 0 with empty
#       block when nothing to surface.
#
# Env knobs:
#   MINI_ORK_DB    Path to state.db. Default
#                  ${MINI_ORK_HOME:-./.mini-ork}/state.db.
#   MO_DISABLE_ACTIVE_STATE
#                  When set to 1, mo_active_state_block prints nothing
#                  and returns 0. For escape valve.
#   MO_ACTIVE_STATE_MAX_PER_SECTION
#                  Default 5. Cap per section to keep token usage bounded.

set -uo pipefail

_mo_asi_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"active_state_index","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_asi_db() {
  printf '%s\n' "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
}

_mo_asi_table_exists() {
  local _name="$1"
  local _db; _db=$(_mo_asi_db)
  [ -f "$_db" ] || return 1
  sqlite3 "$_db" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$_name'" 2>/dev/null \
    | grep -q '^1$'
}

# Internal helpers: each emits JSON arrays for one section.
# Tolerant of missing tables — a fresh install where migration 0009
# is the first or last applied still produces a coherent block with
# empty sections.

_mo_asi_unresolved_errors() {
  local _max="$1" _days="$2"
  if ! _mo_asi_table_exists failure_memory; then
    printf '[]\n'
    return 0
  fi
  local _db; _db=$(_mo_asi_db)
  python3 - <<PY
import sqlite3, json
con = sqlite3.connect("$_db")
con.row_factory = sqlite3.Row
try:
    cur = con.execute(
        """SELECT failure_id, workflow_stage, failure_category, error_message, occurred_at
           FROM failure_memory
           WHERE occurred_at >= datetime('now', '-$_days days')
           ORDER BY occurred_at DESC LIMIT $_max"""
    )
    out = [{
        "failure_id": r["failure_id"],
        "workflow_stage": r["workflow_stage"],
        "failure_category": r["failure_category"],
        "error_message": (r["error_message"] or "")[:200],
        "occurred_at": r["occurred_at"],
    } for r in cur]
    print(json.dumps(out))
finally:
    con.close()
PY
}

_mo_asi_open_constraints() {
  local _max="$1" _days="$2"
  if ! _mo_asi_table_exists policy_decisions; then
    printf '[]\n'
    return 0
  fi
  local _db; _db=$(_mo_asi_db)
  python3 - <<PY
import sqlite3, json
con = sqlite3.connect("$_db")
con.row_factory = sqlite3.Row
try:
    cur = con.execute(
        """SELECT decision_id, run_id, event_type, policy_name, result, reason, evaluated_at
           FROM policy_decisions
           WHERE result IN ('DENY','REQUIRE_APPROVAL')
             AND evaluated_at >= (strftime('%s','now') - $_days * 86400)
           ORDER BY evaluated_at DESC LIMIT $_max"""
    )
    out = [{
        "decision_id": r["decision_id"],
        "run_id": r["run_id"],
        "policy_name": r["policy_name"],
        "result": r["result"],
        "reason": (r["reason"] or "")[:200],
        "evaluated_at": r["evaluated_at"],
    } for r in cur]
    print(json.dumps(out))
finally:
    con.close()
PY
}

_mo_asi_established_facts() {
  local _max="$1" _task_class="$2" _days="$3"
  if ! _mo_asi_table_exists task_runs; then
    printf '[]\n'
    return 0
  fi
  local _db; _db=$(_mo_asi_db)
  python3 - <<PY
import sqlite3, json
con = sqlite3.connect("$_db")
con.row_factory = sqlite3.Row
try:
    cur = con.execute(
        """SELECT id, task_class, recipe, verdict, cost_usd, duration_ms, ended_at, notes
           FROM task_runs
           WHERE verdict = 'APPROVE'
             AND (?='__any__' OR task_class = ?)
             AND COALESCE(ended_at, updated_at) >= (strftime('%s','now') - ? * 86400)
           ORDER BY ended_at DESC NULLS LAST LIMIT ?""",
        ("$_task_class", "$_task_class", $_days, $_max)
    )
    out = [{
        "run_id": r["id"],
        "task_class": r["task_class"],
        "recipe": r["recipe"],
        "cost_usd": r["cost_usd"],
        "duration_ms": r["duration_ms"],
        "notes": (r["notes"] or "")[:200],
    } for r in cur]
    print(json.dumps(out))
finally:
    con.close()
PY
}

_mo_asi_pending_goals() {
  local _max="$1" _task_class="$2"
  if ! _mo_asi_table_exists task_runs; then
    printf '[]\n'
    return 0
  fi
  local _db; _db=$(_mo_asi_db)
  python3 - <<PY
import sqlite3, json
con = sqlite3.connect("$_db")
con.row_factory = sqlite3.Row
try:
    cur = con.execute(
        """SELECT id, task_class, recipe, status, kickoff_path, created_at
           FROM task_runs
           WHERE status NOT IN ('published','rolled_back','failed')
             AND (?='__any__' OR task_class = ?)
           ORDER BY created_at DESC LIMIT ?""",
        ("$_task_class", "$_task_class", $_max)
    )
    out = [{
        "run_id": r["id"],
        "task_class": r["task_class"],
        "recipe": r["recipe"],
        "status": r["status"],
        "kickoff_path": r["kickoff_path"],
    } for r in cur]
    print(json.dumps(out))
finally:
    con.close()
PY
}

_mo_asi_decision_variables() {
  # Surface a tiny, stable set of operator-tunable knobs from the
  # active config. Not pulled live from agents.yaml because that would
  # add a YAML parse to the hot path; instead, names known to the
  # framework are listed so the planner can reason about which it can
  # touch in its plan.
  cat <<'JSON'
[
  {"knob":"MO_DAILY_BUDGET_USD","kind":"cost-cap","scope":"global"},
  {"knob":"MO_TIER4_QUORUM","kind":"panel-quorum","scope":"per-recipe"},
  {"knob":"MO_DISABLE_CN","kind":"context-source","scope":"per-run"},
  {"knob":"MO_INJECT_LEARNINGS","kind":"context-injection","scope":"per-run"},
  {"knob":"MO_DISABLE_ACTIVE_STATE","kind":"context-injection","scope":"per-run"},
  {"knob":"MO_REFUSE_UNSANDBOXED","kind":"safety-threshold","scope":"per-recipe"}
]
JSON
}

mo_active_state_block() {
  if [ "${MO_DISABLE_ACTIVE_STATE:-0}" = "1" ]; then
    return 0
  fi
  local _task_class="${1:-__any__}"
  local _days="${2:-30}"
  local _max="${MO_ACTIVE_STATE_MAX_PER_SECTION:-5}"

  local _u _o _e _p _d
  _u=$(_mo_asi_unresolved_errors "$_max" 7)
  _o=$(_mo_asi_open_constraints "$_max" 7)
  _e=$(_mo_asi_established_facts "$_max" "$_task_class" "$_days")
  _p=$(_mo_asi_pending_goals "$_max" "$_task_class")
  _d=$(_mo_asi_decision_variables)

  python3 - "$_u" "$_o" "$_e" "$_p" "$_d" <<'PY'
import sys, json
u, o, e, p, d = (json.loads(x) for x in sys.argv[1:6])
total = len(u) + len(o) + len(e) + len(p)
if total == 0 and len(d) == 0:
    sys.exit(0)
block = {
    "schema": "mini-ork.active-state-index/v1",
    "source": "state.db",
    "unresolved_errors":   u,
    "open_constraints":    o,
    "established_facts":   e,
    "pending_goals":       p,
    "decision_variables":  d,
}
print("--- ACTIVE STATE INDEX (HarnessBridge T1) ---")
print()
print("```json")
print(json.dumps(block, indent=2, ensure_ascii=False))
print("```")
print()
counts = []
if u: counts.append(f"{len(u)} unresolved error{'s' if len(u) != 1 else ''}")
if o: counts.append(f"{len(o)} open constraint{'s' if len(o) != 1 else ''}")
if e: counts.append(f"{len(e)} established fact{'s' if len(e) != 1 else ''}")
if p: counts.append(f"{len(p)} pending goal{'s' if len(p) != 1 else ''}")
if counts:
    print("**Summary:** " + ", ".join(counts) + ".")
print("--- /ACTIVE STATE INDEX ---")
PY
}

# Self-test fixtures.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _tmp_home=$(mktemp -d)
  export MINI_ORK_HOME="$_tmp_home"
  export MINI_ORK_DB="$_tmp_home/state.db"
  _root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

  # Apply the migrations we need (idempotent CREATE TABLE IF NOT EXISTS).
  for _m in db/migrations/0009_memory_namespaces.sql \
            db/migrations/0013_task_runs.sql \
            db/migrations/0026_policy_state.sql; do
    [ -f "$_root/$_m" ] && sqlite3 "$MINI_ORK_DB" < "$_root/$_m" 2>/dev/null || true
  done

  echo "--- fixture 1: empty DB returns empty (just decision_variables) ---"
  out=$(mo_active_state_block "code_fix" 30)
  if [ -z "$out" ] || printf '%s' "$out" | grep -q '"decision_variables"'; then
    echo "  [ok] empty DB produced a block with at least decision_variables"
  else
    echo "  [fail] unexpected output: $out"
  fi

  echo "--- fixture 2: seeded failure_memory surfaces unresolved_errors ---"
  # failure_memory requires a runs row by FK; add minimal stub.
  sqlite3 "$MINI_ORK_DB" "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY);"
  sqlite3 "$MINI_ORK_DB" "INSERT INTO runs (id) VALUES (1);"
  sqlite3 "$MINI_ORK_DB" "INSERT INTO failure_memory (failure_id, run_id, workflow_stage, failure_category, error_message) VALUES ('fm-test-1', 1, 'verifier', 'verifier_fail', 'lens-glm.md missing');"
  out=$(mo_active_state_block "__any__" 30)
  if printf '%s' "$out" | grep -q 'fm-test-1\|unresolved error'; then
    echo "  [ok] failure_memory row surfaced"
  else
    echo "  [fail] failure_memory not in block"
    printf '%s' "$out" | head -20
  fi

  echo "--- fixture 3: seeded policy_decisions surfaces open_constraints ---"
  sqlite3 "$MINI_ORK_DB" "INSERT INTO policy_decisions (decision_id, run_id, event_type, policy_name, result, reason) VALUES ('pd-test-1', 'run-x', 'constraint_safety', 'no_unsandboxed_dispatch', 'DENY', 'sandbox CLI absent');"
  out=$(mo_active_state_block "__any__" 30)
  if printf '%s' "$out" | grep -q 'no_unsandboxed_dispatch\|open constraint'; then
    echo "  [ok] policy_decisions row surfaced"
  else
    echo "  [fail] policy_decisions not in block"
  fi

  echo "--- fixture 4: seeded task_runs APPROVE surfaces established_facts ---"
  sqlite3 "$MINI_ORK_DB" "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, verdict, created_at, updated_at, ended_at) VALUES ('run-est-1', 'code_fix', 'code-fix', '/tmp/k.md', 'published', 'APPROVE', strftime('%s','now')-3600, strftime('%s','now')-3000, strftime('%s','now')-3000);"
  out=$(mo_active_state_block "code_fix" 30)
  if printf '%s' "$out" | grep -q 'run-est-1\|established fact'; then
    echo "  [ok] task_runs APPROVE surfaced"
  else
    echo "  [fail] task_runs not in block"
  fi

  echo "--- fixture 5: seeded task_runs in-flight surfaces pending_goals ---"
  sqlite3 "$MINI_ORK_DB" "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, created_at, updated_at) VALUES ('run-pend-1', 'code_fix', 'code-fix', '/tmp/p.md', 'executing', strftime('%s','now'), strftime('%s','now'));"
  out=$(mo_active_state_block "code_fix" 30)
  if printf '%s' "$out" | grep -q 'run-pend-1\|pending goal'; then
    echo "  [ok] task_runs executing surfaced as pending"
  else
    echo "  [fail] pending goal not in block"
  fi

  echo "--- fixture 6: MO_DISABLE_ACTIVE_STATE=1 short-circuits ---"
  out=$(MO_DISABLE_ACTIVE_STATE=1 mo_active_state_block "code_fix" 30)
  if [ -z "$out" ]; then
    echo "  [ok] disabled flag produced no output"
  else
    echo "  [fail] disabled flag still produced output: ${out:0:80}"
  fi

  rm -rf "$_tmp_home"
fi
