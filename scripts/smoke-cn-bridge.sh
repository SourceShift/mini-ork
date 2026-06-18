#!/usr/bin/env bash
# scripts/smoke-cn-bridge.sh — reference smoke harness for the v1 CN bridge.
#
# Reference TEMPLATE every ContextNest <-> mini-ork PR must follow per
# the Smoke Test Standard in ContextNest's
# docs/roadmap/epics/agent-context-pack.md. It exercises the real system
# end-to-end against a live ContextNest server and produces a human-
# readable evidence file at tmp/smoke-evidence/cn-bridge-<timestamp>.md.
#
# Tests covered:
#   1. Happy path: planner injection block contains real CN atoms
#   2. Worker prefetch: file written + content non-empty
#   3. CN-down: graceful degradation (planner finishes, empty CN block)
#   4. MO_DISABLE_CN=1: zero CN calls
#   5. CN-slow: silent no-op without crash
#
# Usage:
#   bash scripts/smoke-cn-bridge.sh                       # default kickoff
#   KICKOFF=kickoffs/<other>.md bash scripts/smoke-cn-bridge.sh
#   CN_BASE_URL=http://localhost:28080 bash scripts/smoke-cn-bridge.sh
#
# Exit codes:
#   0   all assertions passed
#   1   one or more assertions failed (evidence file lists which)
#   78  prerequisite missing (e.g. kickoff path, lib/cn_client.sh)
#
# Bash-only on purpose so any reviewer can read it without Rust/Python
# tooling beyond what mini-ork already requires.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT
CN_BASE_URL="${CN_BASE_URL:-http://127.0.0.1:28080}"
export CN_BASE_URL
KICKOFF="${KICKOFF:-${MINI_ORK_ROOT}/kickoffs/oracle-hardening-v03.md}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${MINI_ORK_ROOT}/tmp/smoke-evidence}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE="${EVIDENCE_DIR}/cn-bridge-${TS}.md"

PASS=0
FAIL=0
mkdir -p "$EVIDENCE_DIR"

_assert() {
  local name="$1" expected="$2" actual="$3" verdict
  if [[ "$actual" == *"$expected"* ]]; then
    verdict="PASS"
    PASS=$((PASS+1))
  else
    verdict="FAIL - expected substring not found"
    FAIL=$((FAIL+1))
  fi
  {
    printf '\n## Assertion: %s\n' "$name"
    printf '**Expected substring:** `%s`\n' "$expected"
    printf '**Actual (first 240 chars):** `%s`\n' "${actual:0:240}"
    printf '**Verdict:** %s\n' "$verdict"
  } >> "$EVIDENCE"
}

_assert_either() {
  # Passes when EITHER expected substring is present. Needed because
  # context_contextnest_atoms_md emits two valid shapes since the PR-1
  # capsule swap: the kind-ordered capsule (preferred) or the legacy
  # flat retrieve hit list (fallback on thin/legacy substrates).
  local name="$1" exp_a="$2" exp_b="$3" actual="$4" verdict
  if [[ "$actual" == *"$exp_a"* || "$actual" == *"$exp_b"* ]]; then
    verdict="PASS"
    PASS=$((PASS+1))
  else
    verdict="FAIL - neither expected substring found"
    FAIL=$((FAIL+1))
  fi
  {
    printf '\n## Assertion: %s\n' "$name"
    printf '**Expected substring (either):** `%s` OR `%s`\n' "$exp_a" "$exp_b"
    printf '**Actual (first 240 chars):** `%s`\n' "${actual:0:240}"
    printf '**Verdict:** %s\n' "$verdict"
  } >> "$EVIDENCE"
}

_assert_eq_int() {
  local name="$1" expected="$2" actual="$3" verdict
  if [[ "$actual" -eq "$expected" ]]; then
    verdict="PASS"
    PASS=$((PASS+1))
  else
    verdict="FAIL - expected $expected, got $actual"
    FAIL=$((FAIL+1))
  fi
  {
    printf '\n## Assertion: %s\n' "$name"
    printf '**Expected:** %s\n' "$expected"
    printf '**Actual:** %s\n' "$actual"
    printf '**Verdict:** %s\n' "$verdict"
  } >> "$EVIDENCE"
}

# Evidence header
{
  printf '# Smoke evidence: ContextNest <-> mini-ork v1 bridge\n'
  printf '**Ran:** %s\n' "$TS"
  printf '**Branch:** %s\n' "$(git -C "$MINI_ORK_ROOT" branch --show-current 2>/dev/null || echo unknown)"
  printf '**Commit:** %s\n' "$(git -C "$MINI_ORK_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '**CN url:** %s\n' "$CN_BASE_URL"
  printf '**Kickoff:** %s\n' "$KICKOFF"
} > "$EVIDENCE"

# Prereq checks
if [[ ! -f "$KICKOFF" ]]; then
  echo "prereq missing: kickoff $KICKOFF" >&2; exit 78
fi
if ! command -v python3 >/dev/null && ! command -v jq >/dev/null; then
  echo "prereq missing: jq or python3" >&2; exit 78
fi
if [[ ! -f "$MINI_ORK_ROOT/lib/cn_client.sh" ]]; then
  echo "prereq missing: lib/cn_client.sh (v1 bridge not installed)" >&2; exit 78
fi

# CN reachability probe. Bumped timeout so the harness itself
# doesn't trip on the 2s default that motivated this harness.
cn_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  "$CN_BASE_URL/api/v1/substrate/health" 2>/dev/null || echo "000")
{
  printf '**CN reachable:** %s (health code=%s)\n' \
    "$([[ "$cn_code" == "200" ]] && echo yes || echo no)" "$cn_code"
} >> "$EVIDENCE"

if [[ "$cn_code" != "200" ]]; then
  echo "CN at $CN_BASE_URL not reachable; live-path tests will skip. Start with: make cn-serve" >&2
fi

# Test 1 - happy path: planner CN injection block populated
{ printf '\n## Test 1 - happy path: planner injection block contains CN atoms\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T1_OUT=$(
    cd "$MINI_ORK_ROOT" && bash -c '
      export MINI_ORK_ROOT="$PWD"
      export CN_TIMEOUT_SEC=10
      rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
      source lib/context_assembler.sh
      context_contextnest_atoms_md "'"$KICKOFF"'" 6
    ' 2>&1
  )
  # Accept either output shape: capsule (kind-ordered digest) or the
  # legacy flat retrieve hit list. PR-1 made capsule the default path.
  _assert_either "CN block has header (capsule or atoms)" \
    "--- ContextNest capsule" "--- ContextNest atoms" "$T1_OUT"
  _assert_either "CN block carries substrate content" \
    "# Prompt Context" "sim=" "$T1_OUT"
  { printf '\n### Captured planner CN block (1000 chars):\n```\n%s\n```\n' "${T1_OUT:0:1000}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 1 - CN unreachable" >&2
fi

# Test 2 - worker prefetch: file written + content non-empty
{ printf '\n## Test 2 - worker prefetch hook writes populated context file\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T2_RUN="smoke-cn-bridge-${TS}"
  T2_DIR=$(mktemp -d "/tmp/smoke-cn-bridge-XXXXXX")
  mkdir -p "$T2_DIR/runs"
  T2_PREFETCH="$T2_DIR/runs/$T2_RUN/cn_prefetch/test-sid.md"
  T2_PAYLOAD='{"session_id":"test-sid","prompt":"chapter anchor schema audit and consolidation backoff design","cwd":"'"$MINI_ORK_ROOT"'"}'
  echo "$T2_PAYLOAD" | \
    MINI_ORK_ROOT="$MINI_ORK_ROOT" \
    MINI_ORK_RUN_ID="$T2_RUN" \
    MINI_ORK_RUN_DIR="$T2_DIR/runs" \
    CN_TIMEOUT_SEC=10 \
    CN_PREFETCH_REFRESH_SEC=0 \
    bash "$MINI_ORK_ROOT/hooks/subagent-prefetch.sh" >/dev/null 2>&1
  if [[ -f "$T2_PREFETCH" ]]; then
    T2_SIZE=$(wc -c < "$T2_PREFETCH" | tr -d ' ')
    _assert_eq_int "prefetch file exists" 1 1
    _assert_eq_int "prefetch file > 100 bytes (got $T2_SIZE)" 1 "$([[ "$T2_SIZE" -gt 100 ]] && echo 1 || echo 0)"
    _assert "prefetch markdown has session header" "ContextNest prefetch for session test-sid" "$(cat "$T2_PREFETCH" 2>/dev/null)"
    { printf '\n### Captured prefetch file (first 50 lines):\n```\n%s\n```\n' "$(head -50 "$T2_PREFETCH" 2>/dev/null)"; } >> "$EVIDENCE"
  else
    _assert_eq_int "prefetch file exists" 1 0
  fi
  rm -rf "$T2_DIR"
else
  echo "  [skip] Test 2 - CN unreachable" >&2
fi

# Test 3 - CN-down: graceful degradation
{ printf '\n## Test 3 - failure path: CN-down -> bridge degrades silently\n'; } >> "$EVIDENCE"
T3_OUT=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    export CN_BASE_URL=http://127.0.0.1:1
    rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
    source lib/context_assembler.sh
    out=$(context_contextnest_atoms_md "'"$KICKOFF"'" 6)
    echo "len=${#out}"
  ' 2>&1
)
if [[ "$T3_OUT" == *"len=0"* ]]; then
  _assert_eq_int "CN-down -> silent empty output" 1 1
else
  _assert_eq_int "CN-down -> silent empty output" 1 0
fi
{ printf '\n### CN-down captured output:\n```\n%s\n```\n' "${T3_OUT:0:500}"; } >> "$EVIDENCE"

# Test 4 - MO_DISABLE_CN=1: short-circuit
{ printf '\n## Test 4 - failure path: MO_DISABLE_CN=1 short-circuits everything\n'; } >> "$EVIDENCE"
T4_OUT=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    export MO_DISABLE_CN=1
    rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
    source lib/context_assembler.sh
    out=$(context_contextnest_atoms_md "'"$KICKOFF"'" 6)
    echo "len=${#out}"
  ' 2>&1
)
if [[ "$T4_OUT" == *"len=0"* ]]; then
  _assert_eq_int "MO_DISABLE_CN=1 -> empty output" 1 1
else
  _assert_eq_int "MO_DISABLE_CN=1 -> empty output" 1 0
fi
{ printf '\n### MO_DISABLE_CN=1 captured output:\n```\n%s\n```\n' "$T4_OUT"; } >> "$EVIDENCE"

# Test 5 - CN-slow: silent no-op when timeout exceeded
{ printf '\n## Test 5 - failure path: CN-slow (forced timeout) -> silent no-op\n'; } >> "$EVIDENCE"
T5_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()' 2>/dev/null || echo "")
if [[ -z "$T5_PORT" ]]; then
  echo "  [skip] Test 5 - cannot allocate port" >&2
else
  T5_PID_FILE=$(mktemp /tmp/smoke-slow-pid-XXXXXX)
  python3 - "$T5_PORT" "$T5_PID_FILE" <<'PY' &
import os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
port, pid_file = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw): pass
    def do_GET(self): time.sleep(5); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    def do_POST(self): time.sleep(5); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
with open(pid_file, 'w') as f: f.write(str(os.getpid()))
HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
  sleep 0.4
  T5_STUB_PID=$(cat "$T5_PID_FILE" 2>/dev/null || echo "")
  T5_START=$(date +%s)
  T5_OUT=$(
    cd "$MINI_ORK_ROOT" && bash -c '
      export MINI_ORK_ROOT="$PWD"
      export CN_BASE_URL=http://127.0.0.1:'"$T5_PORT"'
      export CN_TIMEOUT_SEC=1
      export CN_HOOK_TIMEOUT_SEC=1
      rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
      source lib/context_assembler.sh
      out=$(context_contextnest_atoms_md "'"$KICKOFF"'" 3)
      echo "len=${#out}"
    ' 2>&1
  )
  T5_DUR=$(( $(date +%s) - T5_START ))
  [[ -n "$T5_STUB_PID" ]] && kill "$T5_STUB_PID" 2>/dev/null || true
  rm -f "$T5_PID_FILE"
  if [[ "$T5_OUT" == *"len=0"* && "$T5_DUR" -lt 10 ]]; then
    _assert_eq_int "CN-slow -> empty + finishes <10s (took ${T5_DUR}s)" 1 1
  else
    _assert_eq_int "CN-slow -> empty + finishes <10s (took ${T5_DUR}s)" 1 0
  fi
  { printf '\n### CN-slow captured output (took %ss):\n```\n%s\n```\n' "$T5_DUR" "$T5_OUT"; } >> "$EVIDENCE"
fi

# Summary
{
  printf '\n---\n## Summary\n'
  printf '**Assertions:** %d PASS, %d FAIL\n' "$PASS" "$FAIL"
  printf '**Failure-path coverage:** CN-down, MO_DISABLE_CN=1, CN-slow - all exercised\n'
  if [[ "$cn_code" == "200" ]]; then
    printf '**Live-path coverage:** Tests 1+2 exercised against real CN\n'
  else
    printf '**Live-path coverage:** SKIPPED - CN was not reachable (start it with `make cn-serve` and re-run)\n'
  fi
} >> "$EVIDENCE"

echo ""
echo "-- Smoke evidence written to: $EVIDENCE --"
echo "-- $PASS PASS, $FAIL FAIL --"
[[ "$FAIL" -eq 0 ]]
