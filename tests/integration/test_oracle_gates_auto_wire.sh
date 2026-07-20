#!/usr/bin/env bash
# tests/integration/test_oracle_gates_auto_wire.sh
#
# Regression net for the Wave 1 central-dispatcher wire-up (Phase 2 of
# the v0.3-rc1 ralph). Pins the contract the wire-up MUST satisfy:
#
#   1. With MO_ORACLE_GATES_AUTO=1 + a refactor-audit-shaped recipe
#      whose execution_traces show a same-family panel (4 anthropic
#      lenses), dispatch refuses the synthesizer node (rc != 0 OR
#      synthesizer step emits a COALITION_ABORT log line).
#
#   2. With MO_ORACLE_GATES_AUTO=1 + diverse-family panel (4 distinct
#      families), dispatch reaches the synthesizer node without abort.
#
#   3. With MO_ORACLE_GATES_AUTO=0, the existing dispatch behavior is
#      unchanged — the publisher escape hatch bypasses automatic oracle
#      gates even when panel traces would otherwise block.
#
#   4. With MO_ORACLE_GATES_AUTO=1 + a recipe whose state.db has no
#      panel-shaped traces (single-node code-fix), the coalition gate
#      fail-opens (rc=0 single_agent_run) — auto-wiring MUST NOT block
#      non-panel dispatches.
#
# Exit 0 = all 4 fixtures pass. Exit 1 = any fixture failed.
#
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT

# Flip to 1 after the wire-up lands (whichever architecture wins
# consensus pass). Until then, assertions are skipped and the test
# runner stays green.
WIRE_UP_LANDED="${WIRE_UP_LANDED:-1}"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert() {
  local name="$1"; shift
  if eval "$@" 2>/dev/null; then _ok "$name"; else _fail "$name  (expr: $*)"; fi
}

echo "══════════════════════════════════════════════════════"
echo "  Integration: oracle gates auto-wire contract"
echo "══════════════════════════════════════════════════════"

if [ "$WIRE_UP_LANDED" != "1" ]; then
  _skip "wire-up not yet landed — assertions deferred (set WIRE_UP_LANDED=1 to enable)"
  echo
  echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

# ── isolated env ──────────────────────────────────────────────────────────────
TEST_DIR="$(mktemp -d /tmp/mini-ork-int-wire-XXXXXX)"
export MINI_ORK_HOME="$TEST_DIR/home"
export MINI_ORK_DB="$TEST_DIR/home/state.db"
export MINI_ORK_DRY_RUN=0
mkdir -p "$MINI_ORK_HOME/runs"
trap 'rm -rf "$TEST_DIR"' EXIT

# Apply schema + provide a minimal runs row required by execution_traces FK.
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations >/dev/null
sqlite3 "$MINI_ORK_DB" \
  "INSERT OR IGNORE INTO runs (id, agent, final_verdict) VALUES (1, 'test', 'APPROVE');"

# Seed a same-family panel (4 anthropic lenses for run-fixture-collision).
_seed_collision_panel() {
  local prun="$1"
  python3 - "$MINI_ORK_DB" "$prun" <<'PY'
import sqlite3, sys
db, prun = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
for tid, av in [(f"tr-1-{prun}", "sonnet"), (f"tr-2-{prun}", "opus"),
                (f"tr-3-{prun}", "sonnet"), (f"tr-4-{prun}", "opus")]:
    con.execute(
        "INSERT INTO execution_traces (trace_id, agent_version_id, run_id, task_class, status, reviewer_verdict) "
        "VALUES (?,?,1,'refactor_audit','success','APPROVE')",
        (tid, av))
con.commit(); con.close()
PY
}

# Seed a diverse-family panel (4 distinct families) with VARYING verdicts.
# ρ measures pairwise verdict agreement — identical verdicts across all 4
# lenses trips ρ=1.0 regardless of family diversity (correctly: 4 voices
# agreeing perfectly IS a coalition signal even when families differ).
# A "passing" diverse panel needs split verdicts.
_seed_diverse_panel() {
  local prun="$1"
  python3 - "$MINI_ORK_DB" "$prun" <<'PY'
import sqlite3, sys
db, prun = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
for tid, av, verdict in [
    (f"tr-1-{prun}", "glm",     "APPROVE: findings cluster A"),
    (f"tr-2-{prun}", "kimi",    "REQUEST_CHANGES: missed B"),
    (f"tr-3-{prun}", "codex",   "APPROVE: focus on perf"),
    (f"tr-4-{prun}", "minimax", "ESCALATE: security gap C"),
]:
    con.execute(
        "INSERT INTO execution_traces (trace_id, agent_version_id, run_id, task_class, status, reviewer_verdict) "
        "VALUES (?,?,1,'refactor_audit','success',?)",
        (tid, av, verdict))
con.commit(); con.close()
PY
}

# ── Fixture 1: same-family panel + auto-wire ON → expect synthesizer abort ──
echo
echo "--- Fixture 1: MO_ORACLE_GATES_AUTO=1 + same-family panel → expect COALITION_ABORT ──"
_seed_collision_panel "run-fixture-collision"

# Pseudocode placeholder — actual invocation depends on wire-up architecture.
# Three candidate test invocations covered for whichever architecture wins
# the consensus pass:
#
#  (a) Per-node-type auto-gate inside execute:
#      MO_ORACLE_GATES_AUTO=1 MINI_ORK_RUN_ID=run-fixture-collision \
#        bin/mini-ork execute <synthetic-plan-with-synthesizer-node>
#      Expect rc != 0 OR stderr contains "COALITION_ABORT".
#
#  (b) Single oracle-gate pass after lens dispatch:
#      Same as above; the call happens once-per-cycle.
#
#  (c) Lib-side auto-registration:
#      MO_ORACLE_GATES_AUTO=1 sets up gate_register calls, then
#      gate_run_all "refactor_audit" "$context" rc != 0.
#
# Until the wire-up lands + the invocation shape is known, this fixture
# uses (c) as the most-portable surface (gate_run_all is testable
# without invoking the public executor route).

source "$MINI_ORK_ROOT/lib/gate_bootstrap.sh"
mo_bootstrap_oracle_gates
# Re-source registry now that bootstrap registered the 4 oracle gates.
source "$MINI_ORK_ROOT/lib/gate_registry.sh"

# gate_run_all summary JSON shape (per lib/gate_registry.sh:327-333):
#   { task_class, all_pass, any_defer, safety_violation, gate_count, gates: [...] }
context=$(printf '{"panel_run_id":"run-fixture-collision","recipe":"refactor-audit","task_class":"refactor_audit","current_round":1}')
verdict_out=$(gate_run_all "refactor_audit" "$context" 2>/dev/null)
fixture1_safety=$(echo "$verdict_out" | jq -r '.safety_violation // false' 2>/dev/null)
_assert "Fixture 1: same-family panel → safety_violation=true" \
  '[[ "$fixture1_safety" == "true" ]]'

# ── Fixture 2: diverse-family panel + auto-wire ON → expect pass ──────────────
echo
echo "--- Fixture 2: MO_ORACLE_GATES_AUTO=1 + diverse-family panel → expect pass ──"
_seed_diverse_panel "run-fixture-diverse"

context=$(printf '{"panel_run_id":"run-fixture-diverse","recipe":"refactor-audit","task_class":"refactor_audit","current_round":1}')
verdict_out=$(gate_run_all "refactor_audit" "$context" 2>/dev/null)
fixture2_safety=$(echo "$verdict_out" | jq -r '.safety_violation // false' 2>/dev/null)
_assert "Fixture 2: diverse-family panel → safety_violation=false (coalition passes)" \
  '[[ "$fixture2_safety" == "false" ]]'

# ── Fixture 3: auto-wire OFF → backward-compat (no gate fires) ──────────────
echo
echo "--- Fixture 3: MO_ORACLE_GATES_AUTO=0 → backward-compat unchanged ──"

# A collision panel would block the publisher if the auto-wire hook ran.
# Use a missing recipe so the publisher reaches the hook, then exits via
# the existing "no artifact_contract.yaml" no-op path without writing repo
# files or committing anything.
RUN_DIR="$TEST_DIR/runs/run-fixture-auto-off"
mkdir -p "$RUN_DIR"
PLAN_PATH="$RUN_DIR/plan.json"
cat > "$PLAN_PATH" <<'JSON'
{
  "objective": "Exercise publisher escape hatch",
  "task_class": "refactor_audit",
  "decomposition": [
    {"id": "publish", "description": "Publish synthetic artifact", "node_type": "publisher", "depends_on": []}
  ]
}
JSON

set +e
fixture3_out=$(
  MO_ORACLE_GATES_AUTO=0 \
  MINI_ORK_RUN_ID=run-fixture-collision \
  MINI_ORK_RECIPE=__missing_oracle_gate_test_recipe__ \
  MINI_ORK_PLAN_PATH="$PLAN_PATH" \
  "$MINI_ORK_ROOT/bin/mini-ork" execute --node-type publisher 2>&1
)
fixture3_rc=$?
set +e
fixture3_block=$(printf '%s' "$fixture3_out" | grep -c "oracle-gates: safety_violation" || true)
_assert "Fixture 3: auto-off publisher exits 0" \
  '[[ "$fixture3_rc" -eq 0 ]]'
_assert "Fixture 3: auto-off publisher does not emit oracle block" \
  '[[ "$fixture3_block" -eq 0 ]]'

# ── Fixture 4: single-node code-fix → fail-open ──────────────────────────────
echo
echo "--- Fixture 4: single-node code-fix recipe + auto-wire ON → coalition gate fail-opens ──"

context=$(printf '{"panel_run_id":"run-fixture-single-node","recipe":"code-fix","task_class":"code_fix","current_round":1}')
verdict_out=$(gate_run_all "code_fix" "$context" 2>/dev/null)
fixture4_safety=$(echo "$verdict_out" | jq -r '.safety_violation // false' 2>/dev/null)
fixture4_gate_count=$(echo "$verdict_out" | jq -r '.gate_count // 0' 2>/dev/null)
_assert "Fixture 4: single-node code-fix sees registered oracle gates" \
  '[[ "${fixture4_gate_count:-0}" -ge 5 ]]'
_assert "Fixture 4: single-node code-fix → safety_violation=false (fail-open)" \
  '[[ "$fixture4_safety" == "false" ]]'

echo
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
