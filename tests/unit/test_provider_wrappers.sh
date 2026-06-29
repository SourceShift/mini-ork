#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export MINI_ORK_ROOT="$ROOT"

failures=0

check_wrapper() {
  local provider="$1"
  local key_name="$2"
  local fake_key="$3"
  local wrapper="$ROOT/lib/providers/cl_${provider}.sh"

  if (
    set +u
    export "$key_name=$fake_key"
    # shellcheck source=/dev/null
    source "$wrapper"
    [ "${ANTHROPIC_AUTH_TOKEN:-}" = "$fake_key" ]
  ); then
    echo "ok - $provider wrapper preserves $key_name exactly"
  else
    echo "not ok - $provider wrapper corrupts $key_name" >&2
    failures=$((failures + 1))
  fi
}

check_wrapper "glm" "GLM_API_KEY" "glm_fake_token_123"
check_wrapper "minimax" "MINIMAX_API_KEY" "minimax_fake_token_123"
check_wrapper "kimi" "KIMI_API_KEY" "kimi_fake_token_123"
check_wrapper "deepseek" "DEEPSEEK_API_KEY" "deepseek_fake_token_123"

# GLM fair-usage throttling is retryable; auth/config/request failures are not.
# shellcheck source=/dev/null
source "$ROOT/lib/llm-dispatch.sh"

if _mo_llm_glm_fair_usage_retryable "glm" "[1313][Your account's current usage pattern does not comply with the Fair Usage Policy]" 1 3; then
  echo "ok - glm fair-usage 1313 is retryable before max attempts"
else
  echo "not ok - glm fair-usage 1313 should be retryable" >&2
  failures=$((failures + 1))
fi

if _mo_llm_glm_fair_usage_retryable "glm" "401 invalid api key" 1 3; then
  echo "not ok - glm auth errors should not be fair-usage retryable" >&2
  failures=$((failures + 1))
else
  echo "ok - glm auth errors are not fair-usage retryable"
fi

if _mo_llm_glm_fair_usage_retryable "glm" "429 Fair Usage Policy" 3 3; then
  echo "not ok - glm fair-usage should stop at max attempts" >&2
  failures=$((failures + 1))
else
  echo "ok - glm fair-usage stops at max attempts"
fi

exit "$failures"
