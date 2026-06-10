#!/usr/bin/env bash
# tests/test_providers_live.sh — live smoke test for every provider lane.
#
# Dispatches a one-line prompt through each provider via mo_llm_dispatch
# and asserts a sane reply came back. Costs real money (fractions of a
# cent per provider) and needs keys in secrets.local.sh + ambient
# claude / codex logins.
#
# Usage:
#   bash tests/test_providers_live.sh                    # default set
#   MO_LIVE_MODELS="kimi glm" bash tests/test_providers_live.sh
#   MO_LIVE_TIMEOUT=240 bash tests/test_providers_live.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MINI_ORK_ROOT="$REPO_ROOT"
export MINI_ORK_HOME="${MINI_ORK_HOME:-$REPO_ROOT/.mini-ork}"

MODELS="${MO_LIVE_MODELS:-sonnet opus codex glm kimi minimax}"
TIMEOUT="${MO_LIVE_TIMEOUT:-180}"
PROMPT='Reply with exactly one word: PONG'

# shellcheck disable=SC1091
source "$REPO_ROOT/lib/llm-dispatch.sh"
set +e

WORK=$(mktemp -d -t mini-ork-live-smoke.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
for model in $MODELS; do
  out="$WORK/$model.txt"
  start=$(date +%s)
  mo_llm_dispatch "$model" "$PROMPT" "$out" "$TIMEOUT" 5
  rc=$?
  dur=$(( $(date +%s) - start ))
  reply="$(tr -d '\n' < "$out" 2>/dev/null | head -c 120)"
  if [ "$rc" -eq 0 ] && grep -qi 'PONG' "$out" 2>/dev/null; then
    echo "ok   - $model (${dur}s): $reply"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $model (rc=$rc, ${dur}s): ${reply:-<empty>}"
    [ -s "$out.err.log" ] && sed 's/^/       err: /' "$out.err.log" | tail -5
    FAIL=$((FAIL + 1))
  fi
done

echo
echo "passed: $PASS, failed: $FAIL"
[ "$FAIL" -eq 0 ]
