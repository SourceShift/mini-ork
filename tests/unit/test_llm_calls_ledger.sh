#!/usr/bin/env bash
# tests/unit/test_llm_calls_ledger.sh — unit test for llm_calls telemetry writes.
set -Eeuo pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT

TEST_DB="$(mktemp /tmp/mini-ork-llm-calls-XXXXXX.db)"
TEST_RUN_DIR="$(mktemp -d /tmp/mini-ork-llm-calls-run-XXXXXX)"
trap 'rm -f "$TEST_DB"; rm -rf "$TEST_RUN_DIR"' EXIT

export MINI_ORK_DB="$TEST_DB"
export MINI_ORK_RUN_DIR="$TEST_RUN_DIR"
export MINI_ORK_RUN_ID="34"
export MO_RECURSIVE_ITER="34"

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/tests/lib/setup_state_db.sh"
test_apply_migrations

# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/llm-dispatch.sh"

_mo_llm_write_llm_calls_row \
  "anthropic" "sonnet" "default" "mini-ork:test" "researcher" \
  "success" "1234" "0.0021" ""

COUNT="$(
  sqlite3 "$MINI_ORK_DB" \
    "SELECT COUNT(*) FROM llm_calls WHERE status='success' AND duration_ms=1234 AND actor='researcher' AND cost_usd=0.0021;"
)"

if [ "$COUNT" != "1" ]; then
  echo "FAIL: expected exactly 1 row in llm_calls with status='success' AND duration_ms=1234 after successful dispatch, got $COUNT"
  exit 1
fi

echo "PASS: llm_calls ledger writes success row"
