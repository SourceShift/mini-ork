#!/usr/bin/env bash
# Unit tests for the lane-agnostic throttle retry primitive in lib/llm-dispatch.sh.
# Verifies that:
#   * Retryable categories (capacity|network|stream|provider) → retry
#   * Terminal categories (quota|auth|request|safety) → fail fast
#   * attempt-bound guard: never retry once attempt == max_attempts
#   * Backoff stays within MO_DISPATCH_RETRY_MAX_SLEEP_S cap with floor ≥ 1
#   * GLM fair-usage regression: GLM-specific regex still routes to retry
#
# Usage: bash tests/unit/test_dispatch_retry.sh
# Exit 0 = all assertions pass.  Exit 1 = any assertion failed.
set -u

PASS=0; FAIL=0
_ok()   { echo "[OK]   $*"; PASS=$((PASS+1)); }
_fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINI_ORK_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
DISPATCH_LIB="$MINI_ORK_REPO/lib/llm-dispatch.sh"

echo "=== test_dispatch_retry.sh ==="

# ── Guard: skip if the lib is missing ─────────────────────────────────────────

if [[ ! -f "$DISPATCH_LIB" ]]; then
  echo "[FAIL] lib/llm-dispatch.sh not found at $DISPATCH_LIB"
  echo ""
  echo "=== Results: 0 OK  0 SKIP  1 FAIL ==="
  exit 1
fi

# Helper: source the lib in a subshell, invoke a function with args, return
# its exit code. Sourcing in a subshell keeps the parent shell pristine and
# avoids side-effects on global state. The lib sets `set -euo pipefail` at
# load time, so we disable errexit around the call — the function's own rc
# is what we want to capture, and rc=1 is a valid test outcome (e.g. terminal
# categories must fail-fast).
_call_fn() {
  local fn="$1"; shift
  bash -c "source '$DISPATCH_LIB' >/dev/null 2>&1; set +e; $fn \"\$@\"; echo \"rc=\$?\"" "$fn" "$@"
}

# Helper: invoke _mo_llm_throttle_retryable and assert exit code equals expected
_assert_throttle() {
  local label="$1" expected_rc="$2" model="$3" message="$4" rc="$5" attempt="$6" max_attempts="$7"
  local out actual_rc
  out=$(_call_fn _mo_llm_throttle_retryable "$model" "$message" "$rc" "$attempt" "$max_attempts")
  actual_rc="${out##*rc=}"
  if [[ "$actual_rc" == "$expected_rc" ]]; then
    _ok "$label"
  else
    _fail "$label — expected rc=$expected_rc, got rc=$actual_rc (out=$out)"
  fi
}

# Helper: invoke _mo_llm_glm_fair_usage_retryable and assert exit code
_assert_glm_retry() {
  local label="$1" expected_rc="$2" model="$3" message="$4" attempt="$5" max_attempts="$6"
  local out actual_rc
  out=$(_call_fn _mo_llm_glm_fair_usage_retryable "$model" "$message" "$attempt" "$max_attempts")
  actual_rc="${out##*rc=}"
  if [[ "$actual_rc" == "$expected_rc" ]]; then
    _ok "$label"
  else
    _fail "$label — expected rc=$expected_rc, got rc=$actual_rc (out=$out)"
  fi
}

echo ""
echo "--- retryable categories retry under attempt<max ---"

# (a) kimi '503 overloaded' — classifier routes 503+overload → capacity → retryable
_assert_throttle "kimi 503 overloaded rc=1 attempt=1 max=3 → retry (rc=0)" \
  0 kimi "503 overloaded" 1 1 3
# (b) kimi '429 capacity exceeded' — classifier routes 429+capacity → capacity → retryable
_assert_throttle "kimi 429 capacity exceeded rc=1 attempt=1 max=3 → retry (rc=0)" \
  0 kimi "429 capacity exceeded" 1 1 3
# (c) kimi '502 bad gateway' — classifier routes 502 → provider → retryable
_assert_throttle "kimi 502 bad gateway rc=1 attempt=1 max=3 → retry (rc=0)" \
  0 kimi "502 bad gateway" 1 1 3
# (extra coverage) network category
_assert_throttle "kimi connection refused rc=1 attempt=1 max=3 → retry (rc=0)" \
  0 kimi "connection refused upstream" 1 1 3
# (extra coverage) stream category
_assert_throttle "kimi partial stream rc=1 attempt=1 max=3 → retry (rc=0)" \
  0 kimi "unexpected eof: partial stream" 1 1 3

echo ""
echo "--- terminal categories fail fast ---"

# (d) kimi '429 insufficient credits' — 429+billing/credits → quota → non-retryable
_assert_throttle "kimi 429 insufficient credits rc=1 attempt=1 max=3 → fail fast (rc=1)" \
  1 kimi "429 insufficient credits" 1 1 3
# (e) kimi '401 unauthorized' — 401 → auth → non-retryable
_assert_throttle "kimi 401 unauthorized rc=1 attempt=1 max=3 → fail fast (rc=1)" \
  1 kimi "401 unauthorized" 1 1 3
# (f) kimi '400 invalid request' — 400 → request → non-retryable
_assert_throttle "kimi 400 invalid request rc=1 attempt=1 max=3 → fail fast (rc=1)" \
  1 kimi "400 invalid request: bad prompt" 1 1 3
# (extra) safety category
_assert_throttle "kimi content filter rc=1 attempt=1 max=3 → fail fast (rc=1)" \
  1 kimi "content filter triggered" 1 1 3

echo ""
echo "--- attempt-bound guard ---"

# (g) attempt == max → never retry even on retryable category
_assert_throttle "kimi 503 overloaded attempt=3 max=3 → no retry (rc=1)" \
  1 kimi "503 overloaded" 1 3 3
_assert_throttle "kimi 503 overloaded attempt=5 max=3 → no retry (rc=1)" \
  1 kimi "503 overloaded" 1 5 3
# (extra) attempt=2 max=3 still retries
_assert_throttle "kimi 503 overloaded attempt=2 max=3 → retry (rc=0)" \
  0 kimi "503 overloaded" 1 2 3
# (extra) empty model → no retry (defensive)
_assert_throttle "empty model 503 overloaded attempt=1 max=3 → no retry (rc=1)" \
  1 "" "503 overloaded" 1 1 3

echo ""
echo "--- backoff cap + floor ---"

# (h) backoff for attempt=1..5 with low cap stays in range
export MO_DISPATCH_RETRY_MAX_SLEEP_S=3
export MO_DISPATCH_RETRY_BASE_S=1
for n in 1 2 3 4 5; do
  out=$(_call_fn _mo_llm_backoff_seconds "$n")
  delay="${out%%rc=*}"
  delay="${delay%$'\n'}"
  if [[ "$delay" =~ ^[0-9]+$ ]] && [ "$delay" -ge 1 ] && [ "$delay" -le 3 ]; then
    _ok "backoff attempt=$n → ${delay}s (within [1,3])"
  else
    _fail "backoff attempt=$n → ${delay}s (expected integer in [1,3])"
  fi
done
unset MO_DISPATCH_RETRY_MAX_SLEEP_S MO_DISPATCH_RETRY_BASE_S

echo ""
echo "--- GLM fair-usage regression ---"

# (i) regression: GLM 1313 'fair usage' still routes to retry
_assert_glm_retry "glm 1313 fair usage attempt=1 max=3 → retry (rc=0)" \
  0 glm "1313 fair usage policy" 1 3
# GLM with a non-fair-usage message → no retry
_assert_glm_retry "glm plain 503 attempt=1 max=3 → no retry via GLM predicate (rc=1)" \
  1 glm "503 overloaded" 1 3

echo ""
echo "=== Results: $PASS OK  $FAIL FAIL ==="
(( FAIL > 0 )) && exit 1 || exit 0
