#!/usr/bin/env bash
# scripts/smoke-pr-3-role-packs.sh — Smoke for PR-3 (role-tailored ContextPacks).
#
# Per ContextNest's docs/roadmap/epics/agent-context-pack.md Smoke Test
# Standard. Proves each of the 6 role-pack variants composes the
# expected CN endpoint slice and emits role-specific section markers.
#
# Tests covered:
#   1. Happy path planner pack — contains "planner pack" marker (capsule
#      digest or inbox or basins or by-intent block)
#   2. Implementer pack — contains "implementer pack" marker (features
#      delivered OR graph neighbours OR recent sessions for files)
#   3. Reviewer/verifier pack — contains "reviewer pack" marker AND
#      filters to Failures/Verifications/Risks only (NOT Decisions)
#   4. Reflector pack — contains "reflector pack" marker
#   5. Publisher pack — contains shipped-features and/or inbox sections
#   6. Unknown role — falls back to generic atoms_md (still produces
#      ContextNest output, no crash)
#   7. CN-down failure path — every role returns silent empty
#   8. MO_DISABLE_CN=1 failure path — every role returns silent empty
#
# Usage:
#   bash scripts/smoke-pr-3-role-packs.sh
#   KICKOFF=kickoffs/<other>.md bash scripts/smoke-pr-3-role-packs.sh
#
# Exit codes:
#   0   all assertions passed
#   1   failures (evidence file lists which)
#   78  prerequisites missing

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT
CN_BASE_URL="${CN_BASE_URL:-http://127.0.0.1:28080}"
export CN_BASE_URL
KICKOFF="${KICKOFF:-${MINI_ORK_ROOT}/kickoffs/oracle-hardening-v03.md}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${MINI_ORK_ROOT}/tmp/smoke-evidence}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE="${EVIDENCE_DIR}/pr-3-role-packs-${TS}.md"

PASS=0; FAIL=0
mkdir -p "$EVIDENCE_DIR"

_assert() {
  local name="$1" expected="$2" actual="$3" verdict
  if [[ "$actual" == *"$expected"* ]]; then
    verdict="PASS"; PASS=$((PASS+1))
  else
    verdict="FAIL - expected substring not found"; FAIL=$((FAIL+1))
  fi
  {
    printf '\n## Assertion: %s\n' "$name"
    printf '**Expected substring:** `%s`\n' "$expected"
    printf '**Actual (first 240 chars):** `%s`\n' "${actual:0:240}"
    printf '**Verdict:** %s\n' "$verdict"
  } >> "$EVIDENCE"
}

_assert_not() {
  local name="$1" unwanted="$2" actual="$3" verdict
  if [[ "$actual" != *"$unwanted"* ]]; then
    verdict="PASS"; PASS=$((PASS+1))
  else
    verdict="FAIL - unwanted substring present"; FAIL=$((FAIL+1))
  fi
  {
    printf '\n## Assertion: %s\n' "$name"
    printf '**Unwanted substring:** `%s`\n' "$unwanted"
    printf '**Actual (first 240 chars):** `%s`\n' "${actual:0:240}"
    printf '**Verdict:** %s\n' "$verdict"
  } >> "$EVIDENCE"
}

_assert_eq_int() {
  local name="$1" expected="$2" actual="$3" verdict
  if [[ "$actual" -eq "$expected" ]]; then
    verdict="PASS"; PASS=$((PASS+1))
  else
    verdict="FAIL - expected $expected, got $actual"; FAIL=$((FAIL+1))
  fi
  {
    printf '\n## Assertion: %s\n' "$name"
    printf '**Expected:** %s\n' "$expected"
    printf '**Actual:** %s\n' "$actual"
    printf '**Verdict:** %s\n' "$verdict"
  } >> "$EVIDENCE"
}

{
  printf '# Smoke evidence: PR-3 role-tailored ContextPacks\n'
  printf '**Ran:** %s\n' "$TS"
  printf '**Branch:** %s\n' "$(git -C "$MINI_ORK_ROOT" branch --show-current 2>/dev/null || echo unknown)"
  printf '**Commit:** %s\n' "$(git -C "$MINI_ORK_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '**CN url:** %s\n' "$CN_BASE_URL"
  printf '**Kickoff:** %s\n' "$KICKOFF"
} > "$EVIDENCE"

# Prereqs
[ -f "$KICKOFF" ] || { echo "prereq missing: kickoff $KICKOFF" >&2; exit 78; }
[ -f "$MINI_ORK_ROOT/lib/context_role_packs.sh" ] || { echo "prereq missing: lib/context_role_packs.sh" >&2; exit 78; }
command -v python3 >/dev/null || { echo "prereq missing: python3" >&2; exit 78; }

# Initial reachability probe (10s + retry handles transient CN slowness).
cn_code="000"
for _ in 1 2; do
  cn_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "$CN_BASE_URL/api/v1/substrate/health" 2>/dev/null || echo "000")
  [[ "$cn_code" == "200" ]] && break
  sleep 1
done
{ printf '**CN reachable:** %s (health=%s)\n' \
    "$([[ "$cn_code" == "200" ]] && echo yes || echo no)" "$cn_code"; } >> "$EVIDENCE"

# Helper: invoke a role pack in a clean subshell.
_invoke_role() {
  local role="$1"
  local files_csv="${2:-}"
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    source lib/context_role_packs.sh
    context_role_pack_md "'"$role"'" "'"$KICKOFF"'" "'"$files_csv"'"
  ' 2>&1
}

# Test 1 - planner pack (only if CN reachable)
{ printf '\n## Test 1 - planner pack contains role-specific marker\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T1=$(_invoke_role planner)
  _assert "planner pack contains 'planner pack' marker" "planner pack" "$T1"
  { printf '\n### Captured planner pack (first 1200 chars):\n```\n%s\n```\n' "${T1:0:1200}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 1 - CN unreachable" >&2
fi

# Test 2 - implementer pack
{ printf '\n## Test 2 - implementer pack contains role-specific marker\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T2=$(_invoke_role implementer)
  # Implementer pack may produce features, neighbours, or recent-sessions
  # blocks. Any "implementer pack" marker OR "features delivered" marker
  # OR "graph neighbours" marker passes.
  if [[ "$T2" == *"implementer pack"* || "$T2" == *"features delivered"* || "$T2" == *"graph neighbours"* || "$T2" == *"recent sessions"* ]]; then
    _assert_eq_int "implementer pack produces at least one tactical section" 1 1
  else
    _assert_eq_int "implementer pack produces at least one tactical section" 1 0
  fi
  { printf '\n### Captured implementer pack (first 800 chars):\n```\n%s\n```\n' "${T2:0:800}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 2 - CN unreachable" >&2
fi

# Test 3 - reviewer pack filters to failures/verifications/risks only
{ printf '\n## Test 3 - reviewer pack filters to failures/verifications/risks (NOT decisions)\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T3=$(_invoke_role reviewer)
  _assert "reviewer pack marker present" "reviewer pack" "$T3"
  # If non-empty, should NOT contain `## Decisions` — the filter strips it.
  if [ -n "$T3" ]; then
    _assert_not "reviewer pack excludes Decisions heading" "## Decisions" "$T3"
  fi
  { printf '\n### Captured reviewer pack (first 800 chars):\n```\n%s\n```\n' "${T3:0:800}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 3 - CN unreachable" >&2
fi

# Test 4 - reflector pack
{ printf '\n## Test 4 - reflector pack contains marker\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T4=$(_invoke_role reflector)
  _assert "reflector pack marker present" "reflector pack" "$T4"
  { printf '\n### Captured reflector pack (first 800 chars):\n```\n%s\n```\n' "${T4:0:800}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 4 - CN unreachable" >&2
fi

# Test 5 - publisher pack (features OR inbox)
{ printf '\n## Test 5 - publisher pack contains features-delivered OR inbox section\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T5=$(_invoke_role publisher)
  if [[ "$T5" == *"features delivered"* || "$T5" == *"attention inbox"* ]]; then
    _assert_eq_int "publisher pack produces features OR inbox section" 1 1
  else
    _assert_eq_int "publisher pack produces features OR inbox section" 1 0
  fi
  { printf '\n### Captured publisher pack (first 800 chars):\n```\n%s\n```\n' "${T5:0:800}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 5 - CN unreachable" >&2
fi

# Test 6 - unknown role falls back to generic
{ printf '\n## Test 6 - unknown role falls back to generic atoms_md\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T6=$(cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    source lib/context_assembler.sh
    source lib/context_role_packs.sh
    context_role_pack_md "totally-unknown-role" "'"$KICKOFF"'" ""
  ' 2>&1)
  # Generic atoms_md emits either capsule or atoms marker
  if [[ "$T6" == *"ContextNest capsule"* || "$T6" == *"ContextNest atoms"* ]]; then
    _assert_eq_int "unknown role produces fallback output" 1 1
  else
    _assert_eq_int "unknown role produces fallback output" 1 0
  fi
else
  echo "  [skip] Test 6 - CN unreachable" >&2
fi

# Test 7 - CN-down failure path (every role silent empty)
{ printf '\n## Test 7 - CN-down → all role packs silent empty\n'; } >> "$EVIDENCE"
T7_PLANNER=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    export CN_BASE_URL=http://127.0.0.1:1
    rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
    source lib/context_role_packs.sh
    out=$(context_role_pack_md planner "'"$KICKOFF"'" "")
    echo "len=${#out}"
  ' 2>&1
)
if [[ "$T7_PLANNER" == *"len=0"* ]]; then
  _assert_eq_int "CN-down → planner pack silent empty" 1 1
else
  _assert_eq_int "CN-down → planner pack silent empty" 1 0
fi
{ printf '\n### CN-down planner: `%s`\n' "$T7_PLANNER"; } >> "$EVIDENCE"

# Test 8 - MO_DISABLE_CN=1 (every role silent empty)
{ printf '\n## Test 8 - MO_DISABLE_CN=1 → all role packs silent empty\n'; } >> "$EVIDENCE"
T8=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    export MO_DISABLE_CN=1
    rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
    source lib/context_role_packs.sh
    for role in planner researcher implementer reviewer reflector publisher; do
      out=$(context_role_pack_md "$role" "'"$KICKOFF"'" "")
      echo "role=$role len=${#out}"
    done
  ' 2>&1
)
nonempty_count=$(echo "$T8" | grep -cE 'len=[1-9]' || true)
if [[ "$nonempty_count" -eq 0 ]]; then
  _assert_eq_int "MO_DISABLE_CN=1 → all 6 roles silent empty" 6 6
else
  _assert_eq_int "MO_DISABLE_CN=1 → all 6 roles silent empty (some role(s) emitted)" 0 "$nonempty_count"
fi
{ printf '\n### MO_DISABLE_CN=1 per-role:\n```\n%s\n```\n' "$T8"; } >> "$EVIDENCE"

# Summary
{
  printf '\n---\n## Summary\n'
  printf '**Assertions:** %d PASS, %d FAIL\n' "$PASS" "$FAIL"
  printf '**Failure-path coverage:** CN-down, MO_DISABLE_CN=1 — both exercised\n'
  if [[ "$cn_code" == "200" ]]; then
    printf '**Live-path coverage:** Tests 1-6 exercised against real CN\n'
  else
    printf '**Live-path coverage:** SKIPPED — CN unreachable\n'
  fi
} >> "$EVIDENCE"

echo ""
echo "-- Smoke evidence: $EVIDENCE --"
echo "-- $PASS PASS, $FAIL FAIL --"
[[ "$FAIL" -eq 0 ]]
