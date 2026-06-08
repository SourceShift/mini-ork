#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

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

exit "$failures"
