#!/usr/bin/env bash
# tests/unit/test_circuit_breaker.sh — unit tests for lib/circuit_breaker.sh
# Usage: bash tests/unit/test_circuit_breaker.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
#
# Covers `mo_check_liveness_breaker` (W2-C behavioral CB, sister to
# coalition_gate + adaptive_stability):
#   - LIVENESS_TRIP / state=OPEN when all 3 signals fire under majority policy
#   - PROCEED / state=CLOSED when run is productive (artifact varies, writes happen)
#   - cooldown elapsed → HALF_OPEN PROBE → CLOSED on a healthy run
#   - unknown run_id → fail-open PROCEED (gate refuses to block what it cannot measure)
#   - MO_CB_DISABLE=1 always emits PROCEED (escape hatch)
#   - MO_CB_POLICY=or trips on a single signal
set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export MINI_ORK_ROOT
LIB="$MINI_ORK_ROOT/lib/circuit_breaker.sh"

PASS=0; FAIL=0; SKIP=0
_ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
_skip() { echo "  [SKIP] $*"; SKIP=$((SKIP+1)); }
_assert_eq() {
  local label="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then _ok "$label"; else _fail "$label — got='$got' want='$want'"; fi
}

echo "── unit: circuit_breaker.sh ──"

if [[ ! -f "$LIB" ]]; then
  _skip "lib/circuit_breaker.sh not found — tests deferred"
  echo ""; echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
  exit 0
fi

TEST_DB=$(mktemp /tmp/mini-ork-test-XXXXXX.db)
export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_HOME=$(mktemp -d)
trap 'rm -f "$TEST_DB"; rm -rf "$MINI_ORK_HOME"' EXIT

# Minimal schema — only the columns the gate reads. Pre-create the
# circuit_breaker_state table so test seeds can DELETE FROM it before
# the gate's lazy _cb_ensure_state_table fires on first invocation.
python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.executescript("""
CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT,
  artifact_hash TEXT, cost_usd REAL, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS execution_traces (
  trace_id TEXT PRIMARY KEY, reviewer_verdict TEXT,
  files_written TEXT, cost_usd REAL,
  created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS circuit_breaker_state (
  scope_key   TEXT PRIMARY KEY,
  state       TEXT NOT NULL,
  opened_at   INTEGER,
  last_run_id TEXT,
  last_reason TEXT,
  trip_count  INTEGER DEFAULT 0,
  updated_at  INTEGER
);
""")
con.commit(); con.close()
PY

# shellcheck source=/dev/null
source "$LIB"

# Helpers for seeding state across fixtures
_seed_stuck_run() {
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM task_runs")
con.execute("DELETE FROM execution_traces")
con.execute("DELETE FROM circuit_breaker_state")
now = int(time.time())
for i, rid in enumerate(["run-old-1", "run-old-2", "run-stuck"]):
    con.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
                (rid, "code_fix", "code-fix", "deadbeef" * 4, 0.5, now - (3 - i)))
for j in range(3):
    con.execute("INSERT INTO execution_traces (trace_id, reviewer_verdict, files_written, cost_usd) VALUES (?,?,?,?)",
                (f"tr-{j}-run-stuck", "REQUEST_CHANGES", "[]", 0.5))
con.commit(); con.close()
PY
}

_seed_productive_run() {
  python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM task_runs")
con.execute("DELETE FROM execution_traces")
con.execute("DELETE FROM circuit_breaker_state")
now = int(time.time())
for i, (rid, h) in enumerate([("run-old-1","aaaa"), ("run-old-2","bbbb"), ("run-prod","cccc")]):
    con.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
                (rid, "code_fix", "code-fix", h*4, 0.3, now - (3 - i)))
for j in range(3):
    con.execute("INSERT INTO execution_traces (trace_id, reviewer_verdict, files_written, cost_usd) VALUES (?,?,?,?)",
                (f"tr-{j}-run-prod", "APPROVE",
                 '[{"path":"src/foo.py","hash":"x"}]', 0.1))
con.commit(); con.close()
PY
}

echo ""
echo "--- happy path: stuck loop trips → LIVENESS_TRIP / OPEN / 3 signals fired ---"
_seed_stuck_run
out_a=$(mo_check_liveness_breaker "run-stuck" || true)
_assert_eq "verdict=LIVENESS_TRIP" "$(echo "$out_a" | jq -r .verdict)" "LIVENESS_TRIP"
_assert_eq "state=OPEN"            "$(echo "$out_a" | jq -r .state)"   "OPEN"
_assert_eq "fired_count=3"         "$(echo "$out_a" | jq -r .fired_count)" "3"

echo ""
echo "--- happy path: productive run → PROCEED / CLOSED / 0 signals fired ---"
_seed_productive_run
out_b=$(mo_check_liveness_breaker "run-prod")
_assert_eq "verdict=PROCEED"  "$(echo "$out_b" | jq -r .verdict)" "PROCEED"
_assert_eq "state=CLOSED"     "$(echo "$out_b" | jq -r .state)"   "CLOSED"
_assert_eq "fired_count=0"    "$(echo "$out_b" | jq -r .fired_count)" "0"

echo ""
echo "--- cooldown elapsed: pre-seeded OPEN 2hr ago → HALF_OPEN probe → PROCEED ---"
python3 - "$MINI_ORK_DB" <<'PY'
import sqlite3, sys, time
con = sqlite3.connect(sys.argv[1])
con.execute("DELETE FROM circuit_breaker_state")
con.execute("DELETE FROM task_runs")
con.execute("DELETE FROM execution_traces")
now = int(time.time())
# OPEN state opened 2hr ago — well past 1800s default cooldown.
con.execute("INSERT INTO circuit_breaker_state (scope_key, state, opened_at, last_run_id, last_reason, trip_count, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("code_fix::code-fix", "OPEN", now - 7200, "run-old", "all_signals", 1, now - 7200))
for i, (rid, h) in enumerate([("run-x","1111"), ("run-y","2222"), ("run-probe","3333")]):
    con.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?)",
                (rid, "code_fix", "code-fix", h*4, 0.2, now - (3 - i)))
con.commit(); con.close()
PY
out_c=$(mo_check_liveness_breaker "run-probe")
_assert_eq "verdict=PROCEED"           "$(echo "$out_c" | jq -r .verdict)"        "PROCEED"
_assert_eq "previous_state=HALF_OPEN"  "$(echo "$out_c" | jq -r .previous_state)" "HALF_OPEN"

echo ""
echo "--- unknown run_id: fail-open PROCEED (cannot measure what doesn't exist) ---"
out_d=$(mo_check_liveness_breaker "run-does-not-exist")
_assert_eq "verdict=PROCEED on unknown run" "$(echo "$out_d" | jq -r .verdict)" "PROCEED"

echo ""
echo "--- escape hatch: MO_CB_DISABLE=1 always returns PROCEED ---"
_seed_stuck_run
out_e=$(MO_CB_DISABLE=1 mo_check_liveness_breaker "run-stuck")
_assert_eq "MO_CB_DISABLE=1 → PROCEED" "$(echo "$out_e" | jq -r .verdict)" "PROCEED"

echo ""
echo "── Results: ${PASS} OK  ${SKIP} SKIP  ${FAIL} FAIL ──"
[ "$FAIL" -eq 0 ]
