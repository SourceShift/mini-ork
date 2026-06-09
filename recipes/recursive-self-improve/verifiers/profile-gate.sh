#!/usr/bin/env bash
# verifiers/profile-gate.sh — regression guard for planner profile readiness.
#
# Output: JSON to stdout. Exit 0 on pass, 1 on fail.

set -uo pipefail

WT="${MINI_ORK_SELF_IMPROVE_WORKTREE:-${MINI_ORK_ROOT:-$(pwd)}}"
TMPROOT=$(mktemp -d /tmp/mini-ork-profile-gate-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

pass=1
notes=()

mkdir -p "$TMPROOT/root/lib" "$TMPROOT/home/runs/profile-gate-needs" "$TMPROOT/home/runs/profile-gate-ready"
TEST_DB="$TMPROOT/home/state.db"

python3 - "$TEST_DB" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE node_runs (run_id TEXT, node_id TEXT, node_type TEXT, lane TEXT)")
con.commit()
con.close()
PY

cat > "$TMPROOT/root/lib/trace_store.sh" <<'SH'
trace_write() { return 0; }
SH

cat > "$TMPROOT/root/lib/llm-dispatch.sh" <<'SH'
llm_dispatch() {
  printf 'called\n' >> "${PROFILE_GATE_DISPATCH_MARKER:?PROFILE_GATE_DISPATCH_MARKER required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB required}" "${MINI_ORK_RUN_ID:-}" <<'PY'
import sqlite3
import sys

db, run_id = sys.argv[1:3]
con = sqlite3.connect(db)
con.execute(
    "INSERT INTO node_runs (run_id, node_id, node_type, lane) VALUES (?, 'planner', 'planner', 'codex')",
    (run_id,),
)
con.commit()
con.close()
PY
  cat <<'JSON'
{
  "objective": "ready profile dispatch fixture",
  "assumptions": [],
  "decomposition": [],
  "dependencies": [],
  "risk_notes": [],
  "artifact_contract": { "outputs": [], "success_verifiers": [] },
  "verifier_contract": { "checks": [{ "id": "ready", "description": "ready profile dispatch happened" }] }
}
JSON
}
SH

cat > "$TMPROOT/kickoff.md" <<'EOF'
# Profile gate regression
## Definition of Done
- The planner skips dispatch while the run profile needs answers.
EOF

READY_PROFILE="$TMPROOT/run_profile-ready.json"
cat > "$READY_PROFILE" <<'JSON'
{
  "profile_status": "ready",
  "confidence": 0.9,
  "human_questions": [],
  "success_criteria": ["plan verifier_contract exists"]
}
JSON

NEEDS_OUT="$TMPROOT/home/runs/profile-gate-needs/plan.json"
NEEDS_MARKER="$TMPROOT/needs-dispatch.marker"
NEEDS_STDOUT=$(
  MINI_ORK_ROOT="$TMPROOT/root" \
  MINI_ORK_HOME="$TMPROOT/home" \
  MINI_ORK_DB="$TEST_DB" \
  MINI_ORK_RUN_ID="profile-gate-needs" \
  MINI_ORK_PROFILE_GATE=1 \
  MINI_ORK_PROFILE_PATH="$WT/tests/fixtures/run_profile-needs-answers.json" \
  PROFILE_GATE_DISPATCH_MARKER="$NEEDS_MARKER" \
  "$WT/bin/mini-ork-plan" --out "$NEEDS_OUT" "$TMPROOT/kickoff.md" 2>"$TMPROOT/needs.err"
)
needs_rc=$?

if [ "$needs_rc" -ne 0 ]; then
  pass=0
  notes+=("needs_answers invocation exited $needs_rc")
fi

if [ -s "$NEEDS_MARKER" ]; then
  pass=0
  notes+=("needs_answers profile called llm_dispatch")
fi

needs_node_rows=$(python3 - "$TEST_DB" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
row = con.execute(
    "SELECT COUNT(*) FROM node_runs WHERE run_id='profile-gate-needs' AND node_type='planner' AND lane IN ('opus','sonnet','codex','haiku','anthropic')"
).fetchone()
print(row[0])
con.close()
PY
)
if [ "$needs_node_rows" -ne 0 ]; then
  pass=0
  notes+=("needs_answers profile wrote planner node_runs rows")
fi

if ! python3 - "$NEEDS_OUT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    plan = json.load(f)

assert plan["plan_status"] == "needs_answers"
assert plan["blocked_by"] == "run_profile"
assert plan["decomposition"] == []
assert plan["verifier_contract"]["checks"]
PY
then
  pass=0
  notes+=("needs_answers plan.json missing blocked shape")
fi

if ! printf '%s\n' "$NEEDS_STDOUT" | grep -q '"plan_status":"needs_answers"'; then
  pass=0
  notes+=("needs_answers stdout missing plan_status marker")
fi

READY_OUT="$TMPROOT/home/runs/profile-gate-ready/plan.json"
READY_MARKER="$TMPROOT/ready-dispatch.marker"
MINI_ORK_ROOT="$TMPROOT/root" \
MINI_ORK_HOME="$TMPROOT/home" \
MINI_ORK_DB="$TEST_DB" \
MINI_ORK_RUN_ID="profile-gate-ready" \
MINI_ORK_PROFILE_GATE=1 \
MINI_ORK_PROFILE_PATH="$READY_PROFILE" \
PROFILE_GATE_DISPATCH_MARKER="$READY_MARKER" \
"$WT/bin/mini-ork-plan" --out "$READY_OUT" "$TMPROOT/kickoff.md" >/dev/null 2>"$TMPROOT/ready.err"
ready_rc=$?

if [ "$ready_rc" -ne 0 ]; then
  pass=0
  notes+=("ready invocation exited $ready_rc")
fi

if [ ! -s "$READY_MARKER" ]; then
  pass=0
  notes+=("ready profile did not call llm_dispatch")
fi

ready_node_rows=$(python3 - "$TEST_DB" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
row = con.execute(
    "SELECT COUNT(*) FROM node_runs WHERE run_id='profile-gate-ready' AND node_type='planner' AND lane='codex'"
).fetchone()
print(row[0])
con.close()
PY
)
if [ "$ready_node_rows" -lt 1 ]; then
  pass=0
  notes+=("ready profile did not write planner node_runs dispatch row")
fi

python3 - "$pass" "$NEEDS_OUT" "$READY_OUT" "$TEST_DB" "$TMPROOT/needs.err" "$TMPROOT/ready.err" "${notes[@]}" <<'PY'
import json
import sys

pass_flag, needs_out, ready_out, db, needs_err, ready_err, *notes = sys.argv[1:]
print(json.dumps({
    "verifier": "profile-gate",
    "pass": pass_flag == "1",
    "needs_answers_plan": needs_out,
    "ready_plan": ready_out,
    "node_runs_db": db,
    "needs_stderr": needs_err,
    "ready_stderr": ready_err,
    "notes": notes,
}))
PY

[ "$pass" -eq 1 ]
