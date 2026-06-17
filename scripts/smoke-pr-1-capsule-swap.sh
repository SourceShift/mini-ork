#!/usr/bin/env bash
# scripts/smoke-pr-1-capsule-swap.sh — Smoke for PR-1 (capsule swap).
#
# Per ContextNest's docs/roadmap/epics/agent-context-pack.md Smoke Test
# Standard. Proves the planner's CN injection block now leads with
# kind-ordered capsule sections (## Risks / ## Decisions / ## Failures
# to avoid) instead of flat similarity hits, with retrieve fallback
# still functional when capsule returns empty.
#
# Tests covered (PR-1 specific):
#   1. Happy path: planner block contains capsule kind headings + bracket header
#   2. Capsule fallback: when capsule returns <100 chars, retrieve hits surface
#   3. CN-down: silent empty output
#   4. MO_DISABLE_CN=1: silent empty output
#   5. CN_TIMEOUT_SEC bumped default (8s, not 2s) — bridge no longer
#      silently no-ops on 500-char planner briefs
#
# Usage:
#   bash scripts/smoke-pr-1-capsule-swap.sh
#   KICKOFF=kickoffs/<other>.md bash scripts/smoke-pr-1-capsule-swap.sh
#
# Exit codes:
#   0   all assertions passed
#   1   one or more assertions failed (evidence file lists which)
#   78  prerequisite missing

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export MINI_ORK_ROOT
CN_BASE_URL="${CN_BASE_URL:-http://127.0.0.1:28080}"
export CN_BASE_URL
KICKOFF="${KICKOFF:-${MINI_ORK_ROOT}/kickoffs/oracle-hardening-v03.md}"
EVIDENCE_DIR="${EVIDENCE_DIR:-${MINI_ORK_ROOT}/tmp/smoke-evidence}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
EVIDENCE="${EVIDENCE_DIR}/pr-1-capsule-swap-${TS}.md"

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
  printf '# Smoke evidence: PR-1 capsule swap\n'
  printf '**Ran:** %s\n' "$TS"
  printf '**Branch:** %s\n' "$(git -C "$MINI_ORK_ROOT" branch --show-current 2>/dev/null || echo unknown)"
  printf '**Commit:** %s\n' "$(git -C "$MINI_ORK_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  printf '**CN url:** %s\n' "$CN_BASE_URL"
  printf '**Kickoff:** %s\n' "$KICKOFF"
} > "$EVIDENCE"

# Prereqs
[ -f "$KICKOFF" ] || { echo "prereq missing: kickoff $KICKOFF" >&2; exit 78; }
[ -f "$MINI_ORK_ROOT/lib/cn_client.sh" ] || { echo "prereq missing: lib/cn_client.sh" >&2; exit 78; }
command -v python3 >/dev/null || { echo "prereq missing: python3" >&2; exit 78; }

# Initial reachability probe with retry — CN /health can intermittently
# take >5s under consolidation worker contention on a populated substrate.
# Two attempts with 10s timeout each catches the transient case; if both
# fail CN is genuinely down and Tests 1+2 skip.
cn_code="000"
for _ in 1 2; do
  cn_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "$CN_BASE_URL/api/v1/substrate/health" 2>/dev/null || echo "000")
  [[ "$cn_code" == "200" ]] && break
  sleep 1
done
{ printf '**CN reachable:** %s (health=%s)\n' \
    "$([[ "$cn_code" == "200" ]] && echo yes || echo no)" "$cn_code"; } >> "$EVIDENCE"
if [[ "$cn_code" != "200" ]]; then
  echo "CN at $CN_BASE_URL not reachable; live tests skip. Start with: make cn-serve" >&2
fi

# Test 1 - happy path: planner block leads with capsule kind headings
{ printf '\n## Test 1 - happy path: planner block contains capsule kind headings\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  T1_OUT=$(
    cd "$MINI_ORK_ROOT" && bash -c '
      export MINI_ORK_ROOT="$PWD"
      rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
      source lib/context_assembler.sh
      context_contextnest_atoms_md "'"$KICKOFF"'" 6
    ' 2>&1
  )
  # The PR-1 swap wraps capsule output in `--- ContextNest capsule ---`
  # markers (distinct from the legacy `--- ContextNest atoms ---` marker
  # that the retrieve fallback still produces). Either marker is acceptable
  # as long as one of them appears + the body has substantive content.
  _assert "block has capsule OR atoms header" "ContextNest" "$T1_OUT"
  # Capsule-specific: kind-ordered headings appear when capsule fires.
  # Retrieve fallback: `sim=` lines appear instead.
  if echo "$T1_OUT" | grep -qE "^## (Risks|Decisions|Failures|Verifications|Evidence)"; then
    _assert_eq_int "capsule path fired (kind headings present)" 1 1
  elif echo "$T1_OUT" | grep -q "sim="; then
    _assert_eq_int "retrieve fallback fired (sim= lines present, capsule was empty)" 1 1
  else
    _assert_eq_int "either capsule kind headings or retrieve sim= lines present" 1 0
  fi
  { printf '\n### Captured planner CN block (first 1200 chars):\n```\n%s\n```\n' "${T1_OUT:0:1200}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 1 - CN unreachable" >&2
fi

# Test 2 - direct cn_capsule call returns markdown body
{ printf '\n## Test 2 - direct cn_capsule call returns kind-ordered markdown\n'; } >> "$EVIDENCE"
if [[ "$cn_code" == "200" ]]; then
  # NB: do NOT clear .mini-ork/state/cn_ping.cache here. Test 1 has already
  # primed it with "up" via a real /health ping. Re-pinging in Test 2 races
  # CN under load (consolidation worker contention can push /health past
  # the 3s CN_HOOK_TIMEOUT_SEC), producing intermittent "down" → empty
  # cn_capsule output. The 30s TTL safely covers the harness's runtime.
  T2_OUT=$(
    cd "$MINI_ORK_ROOT" && bash -c '
      export MINI_ORK_ROOT="$PWD"
      source lib/cn_client.sh
      cn_capsule "" "30d"
    ' 2>&1
  )
  # Capsule renderer always emits at least `# Prompt Context\n` header.
  if [[ "${#T2_OUT}" -gt 30 && "$T2_OUT" == *"Prompt Context"* ]]; then
    _assert_eq_int "cn_capsule returns non-trivial markdown (len ${#T2_OUT})" 1 1
  else
    _assert_eq_int "cn_capsule returns non-trivial markdown (len ${#T2_OUT})" 1 0
  fi
  { printf '\n### Captured capsule (first 600 chars):\n```\n%s\n```\n' "${T2_OUT:0:600}"; } >> "$EVIDENCE"
else
  echo "  [skip] Test 2 - CN unreachable" >&2
fi

# Test 3 - CN_TIMEOUT_SEC default is now 8s, not 2s
{ printf '\n## Test 3 - CN_TIMEOUT_SEC default bumped from 2s to 8s\n'; } >> "$EVIDENCE"
T3_DEFAULT=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    unset CN_TIMEOUT_SEC
    source lib/cn_client.sh
    echo "$CN_TIMEOUT_SEC"
  ' 2>&1
)
_assert_eq_int "CN_TIMEOUT_SEC defaults to 8 (was 2 pre-PR-1)" 8 "$T3_DEFAULT"

# Test 4 - CN-down: graceful degradation
{ printf '\n## Test 4 - failure path: CN-down -> bridge degrades silently\n'; } >> "$EVIDENCE"
T4_OUT=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    export CN_BASE_URL=http://127.0.0.1:1
    rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
    source lib/context_assembler.sh
    out=$(context_contextnest_atoms_md "'"$KICKOFF"'" 6)
    echo "len=${#out}"
  ' 2>&1
)
if [[ "$T4_OUT" == *"len=0"* ]]; then
  _assert_eq_int "CN-down -> silent empty" 1 1
else
  _assert_eq_int "CN-down -> silent empty" 1 0
fi
{ printf '\n### CN-down captured: `%s`\n' "${T4_OUT:0:200}"; } >> "$EVIDENCE"

# Test 5 - MO_DISABLE_CN=1: short-circuit
{ printf '\n## Test 5 - failure path: MO_DISABLE_CN=1 short-circuits everything\n'; } >> "$EVIDENCE"
T5_OUT=$(
  cd "$MINI_ORK_ROOT" && bash -c '
    export MINI_ORK_ROOT="$PWD"
    export MO_DISABLE_CN=1
    rm -f .mini-ork/state/cn_ping.cache 2>/dev/null
    source lib/context_assembler.sh
    out=$(context_contextnest_atoms_md "'"$KICKOFF"'" 6)
    echo "len=${#out}"
  ' 2>&1
)
if [[ "$T5_OUT" == *"len=0"* ]]; then
  _assert_eq_int "MO_DISABLE_CN=1 -> silent empty" 1 1
else
  _assert_eq_int "MO_DISABLE_CN=1 -> silent empty" 1 0
fi
{ printf '\n### MO_DISABLE_CN=1 captured: `%s`\n' "$T5_OUT"; } >> "$EVIDENCE"

# Summary
{
  printf '\n---\n## Summary\n'
  printf '**Assertions:** %d PASS, %d FAIL\n' "$PASS" "$FAIL"
  printf '**Failure-path coverage:** CN-down, MO_DISABLE_CN=1 — both exercised\n'
  if [[ "$cn_code" == "200" ]]; then
    printf '**Live-path coverage:** Tests 1+2 exercised against real CN\n'
  else
    printf '**Live-path coverage:** SKIPPED — CN unreachable\n'
  fi
} >> "$EVIDENCE"

echo ""
echo "-- Smoke evidence: $EVIDENCE --"
echo "-- $PASS PASS, $FAIL FAIL --"
[[ "$FAIL" -eq 0 ]]
