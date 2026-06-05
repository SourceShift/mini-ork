#!/usr/bin/env bash
# verifiers/grep-assert.sh — grep-pattern assertion runner for `docs` recipe.
#
# Reads verifier_contract.checks[] from the plan JSON and runs each grep
# assertion against the named file. An assertion is:
#
#   { "kind": "grep", "file": "<path>", "pattern": "<extended-regex>", "min_count": <int> }
#
# Each assertion passes when `grep -cE "<pattern>" <file>` returns ≥ min_count.
# rc=0 when ALL grep assertions pass. rc=1 on ANY failure.
#
# Env:
#   MINI_ORK_PLAN_PATH    path to the plan JSON (default:
#                         $MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/plan.json)
#   MINI_ORK_HOME         project home (default: $(pwd)/.mini-ork)
#   MINI_ORK_RUN_ID       current run id (used in log path)
#
# Output: human-readable per-assertion status + a JSON summary on the final
# line for the run logger to parse.

set -Eeuo pipefail

MINI_ORK_HOME="${MINI_ORK_HOME:-$(pwd)/.mini-ork}"
MINI_ORK_RUN_ID="${MINI_ORK_RUN_ID:-unknown-run}"
PLAN_PATH="${MINI_ORK_PLAN_PATH:-$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/plan.json}"
LOG_DIR="$MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID"
mkdir -p "$LOG_DIR"
LOG_PATH="$LOG_DIR/verifier_grep_assert.log"

if [ ! -f "$PLAN_PATH" ]; then
  printf '{"verifier":"grep-assert","status":"skipped","reason":"plan not found: %s"}\n' "$PLAN_PATH"
  exit 0
fi

# Extract every grep assertion as one JSON object per line.
mapfile -t assertions < <(jq -c '.verifier_contract.checks[]? | select(.kind == "grep")' "$PLAN_PATH" 2>/dev/null || true)

if [ "${#assertions[@]}" -eq 0 ]; then
  printf '{"verifier":"grep-assert","status":"skipped","reason":"no grep assertions in plan"}\n' | tee -a "$LOG_PATH"
  exit 0
fi

n_total=${#assertions[@]}
n_passed=0
n_failed=0
failed_details=()

for a in "${assertions[@]}"; do
  file=$(echo "$a"      | jq -r '.file // ""')
  pattern=$(echo "$a"   | jq -r '.pattern // ""')
  min_count=$(echo "$a" | jq -r '.min_count // 1')

  if [ -z "$file" ] || [ -z "$pattern" ]; then
    n_failed=$((n_failed + 1))
    failed_details+=("malformed assertion (missing file or pattern): $a")
    echo "  [FAIL] malformed: $a" | tee -a "$LOG_PATH"
    continue
  fi

  if [ ! -f "$file" ]; then
    n_failed=$((n_failed + 1))
    failed_details+=("file not found: $file (pattern was: $pattern)")
    echo "  [FAIL] file not found: $file" | tee -a "$LOG_PATH"
    continue
  fi

  # grep -c always prints a number; rc=1 just means "0 matches".
  # `|| echo 0` would APPEND a second 0 (double-newline output), so use
  # `|| count=0` instead — the assignment short-circuits.
  count=$(grep -cE "$pattern" "$file" 2>/dev/null) || count=0

  if [ "$count" -ge "$min_count" ]; then
    n_passed=$((n_passed + 1))
    echo "  [PASS] $file ~ /$pattern/  count=$count (>= $min_count)" | tee -a "$LOG_PATH"
  else
    n_failed=$((n_failed + 1))
    failed_details+=("count=$count below min=$min_count for /$pattern/ in $file")
    echo "  [FAIL] $file ~ /$pattern/  count=$count (< $min_count)" | tee -a "$LOG_PATH"
  fi
done

# JSON summary on final line for log parsers.
if [ "$n_failed" -eq 0 ]; then
  printf '{"verifier":"grep-assert","status":"pass","passed":%d,"failed":0,"total":%d}\n' "$n_passed" "$n_total" | tee -a "$LOG_PATH"
  exit 0
else
  failed_arr=$(printf '%s\n' "${failed_details[@]}" | jq -R . | jq -s -c .)
  printf '{"verifier":"grep-assert","status":"fail","passed":%d,"failed":%d,"total":%d,"failures":%s}\n' \
    "$n_passed" "$n_failed" "$n_total" "$failed_arr" | tee -a "$LOG_PATH"
  exit 1
fi
