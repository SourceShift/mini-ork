#!/usr/bin/env bash
# Regression coverage for recursive-self-improve no-regression benchmark gating.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
VERIFIER="$ROOT/recipes/recursive-self-improve/verifiers/no-regression.sh"

PASS=0
FAIL=0
_ok() { echo "  [OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

export MINI_ORK_ROOT="$ROOT"
export MINI_ORK_SELF_IMPROVE_WORKTREE="$ROOT"
export MINI_ORK_RUN_DIR="$TMPDIR/run"
export MINI_ORK_DB="$TMPDIR/state.db"
export MINI_ORK_RUN_ID="1"
mkdir -p "$MINI_ORK_RUN_DIR"

seed_scores() {
  python3 - "$MINI_ORK_DB" "$@" <<'PY'
import sqlite3, sys, time
db, *scores = sys.argv[1:]
con = sqlite3.connect(db)
con.execute("DROP TABLE IF EXISTS benchmark_results")
con.execute("CREATE TABLE benchmark_results (result_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, utility_score REAL NOT NULL DEFAULT 0.0, ran_at TEXT NOT NULL)")
now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
for idx, score in enumerate(scores):
    con.execute("INSERT INTO benchmark_results (result_id, run_id, utility_score, ran_at) VALUES (?,?,?,?)", (f"br-{idx}", 1, float(score), now))
con.commit()
con.close()
PY
}

read_flag() {
  python3 - "$1" "$2" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
value = data.get(sys.argv[2])
print("true" if value is True else "false" if value is False else value)
PY
}

assert_flags() {
  local label="$1" want_pass="$2" want_regression="$3" want_inconclusive="$4"
  local out pass regression inconclusive
  out="$(bash "$VERIFIER")"
  pass="$(read_flag "$out" pass)"
  regression="$(read_flag "$out" benchmark_regression)"
  inconclusive="$(read_flag "$out" benchmark_inconclusive)"
  if [[ "$pass" == "$want_pass" && "$regression" == "$want_regression" && "$inconclusive" == "$want_inconclusive" ]]; then
    _ok "$label"
  else
    _fail "$label: expected pass=$want_pass benchmark_regression=$want_regression benchmark_inconclusive=$want_inconclusive, got pass=$pass benchmark_regression=$regression benchmark_inconclusive=$inconclusive"
  fi
}

echo "── verifier: no-regression benchmark gate ──"

seed_scores 0.1 0.1 0.1 0.1 0.1
assert_flags "benchmark regression must be caught" false true false

seed_scores 0.1 0.1
assert_flags "low-n benchmark must be inconclusive not failing" true false true

seed_scores 0.9 0.9 0.9 0.9 0.9
assert_flags "healthy benchmark must pass cleanly" true false false

echo ""
echo "── Results: ${PASS} OK  ${FAIL} FAIL ──"
[[ "$FAIL" -eq 0 ]]
