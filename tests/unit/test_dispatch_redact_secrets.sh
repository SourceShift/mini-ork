#!/usr/bin/env bash
# tests/unit/test_dispatch_redact_secrets.sh
#
# Defence-in-depth for the llm_calls.error_message column. mo_llm_dispatch
# captures the last 200 bytes of provider stderr on failure and writes them
# to llm_calls.error_message, which the read-only web API surfaces via
# /api/runs/<id>/llm-calls (mini_ork/web/agents.py:565).
#
# If a provider echoes an API key in its 401 ("Invalid x-api-key: sk-ant-…"),
# that key fragment would land in the DB and the web response. The redactor
# strips known key shapes before the row is written.
#
# Coverage: the prefix family every cl_*.sh provider emits today, plus
# Bearer-token and env-dump shapes that show up in verbose curl logs.

set -uo pipefail
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

OK=0
FAIL=0
_ok()   { OK=$((OK + 1));   echo "  [OK]   $1"; }
_fail() { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }

# Source-only mode: pulls function defs without entering dispatch logic.
MINI_ORK_LLM_SOURCE_ONLY=1 source "$MINI_ORK_ROOT/lib/llm-dispatch.sh" 2>/dev/null || true

_assert_redacts() {
  local label="$1" input="$2" must_redact="$3" must_keep="${4:-}"
  local out
  out=$(_mo_llm_redact_secrets "$input")
  if echo "$out" | grep -qF "$must_redact"; then
    _fail "$label: secret survived: $out"
    return
  fi
  if [ -n "$must_keep" ] && ! echo "$out" | grep -qF "$must_keep"; then
    _fail "$label: context dropped, got: $out"
    return
  fi
  _ok "$label"
}

echo "── _mo_llm_redact_secrets ──"

_assert_redacts "Anthropic sk-ant-* key" \
  "401 Unauthorized: Invalid x-api-key: sk-ant-abc1234567890XYZdef" \
  "sk-ant-abc1234567890XYZdef" "401 Unauthorized"

_assert_redacts "OpenRouter sk-or-* key" \
  "error: sk-or-v1-deadbeefdeadbeefdeadbeef invalid" \
  "sk-or-v1-deadbeefdeadbeefdeadbeef" "error"

_assert_redacts "MiniMax sk-cp-* key" \
  "MiniMax 401: sk-cp-1234567890abcdefghij" \
  "sk-cp-1234567890abcdefghij" "MiniMax 401"

_assert_redacts "Bearer token" \
  "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.foo.bar1234567" \
  "eyJhbGciOiJIUzI1NiJ9.foo.bar1234567" "Authorization"

_assert_redacts "ANTHROPIC_AUTH_TOKEN env dump" \
  "env: ANTHROPIC_AUTH_TOKEN=sk-ant-secretvalue123 PATH=/usr/bin" \
  "sk-ant-secretvalue123" "PATH=/usr/bin"

_assert_redacts "GLM hex-style key" \
  "GLM auth fail: 406c14eba1d8f9f72b4e9a0c1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f" \
  "406c14eba1d8f9f72b4e9a0c1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f" "GLM auth fail"

# Negative case: harmless strings must NOT be mangled.
out=$(_mo_llm_redact_secrets "harmless: file not found at /tmp/foo")
if [ "$out" = "harmless: file not found at /tmp/foo" ]; then
  _ok "harmless string passes through unchanged"
else
  _fail "harmless string mangled: $out"
fi

# Empty input must return empty (not crash, not echo placeholder).
out=$(_mo_llm_redact_secrets "")
if [ -z "$out" ]; then
  _ok "empty input returns empty"
else
  _fail "empty input returned: $out"
fi

echo ""
echo "── Results: $OK OK  $FAIL FAIL ──"
[ "$FAIL" -eq 0 ]
