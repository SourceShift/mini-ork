#!/usr/bin/env bash
# healer.sh — self-healing loop. Reads a failed run's logs, classifies
# the failure, and dispatches the matching recovery action.
#
# Usage:
#   healer.sh <epic_id> <run_dir>
#
# Workflow:
#   1. Read worker.log + reviewer-verdict.json + iter logs from <run_dir>
#   2. Try fingerprint match against existing lessons_bank
#   3. If miss: call llm gateway to classify into a NEW lesson;
#      memory-store.sh saves it
#   4. Print recovery_action + args to stdout (one JSON line); orchestrator
#      reads it and dispatches the action
#
# Outputs (stdout):
#   {"lesson_id":N,"failure_class":"...","recovery_action":"...","recovery_args":{...},"matched":true|false}
#
# Exit codes:
#   0  ok (recovery decided)
#   2  usage error
#   3  no run_dir or empty logs (caller should escalate)

set -euo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

EPIC_ID="${1:-}"
RUN_DIR="${2:-}"

if [ -z "$EPIC_ID" ] || [ -z "$RUN_DIR" ]; then
  echo "Usage: $0 <epic_id> <run_dir>" >&2
  exit 2
fi
if [ ! -d "$RUN_DIR" ]; then
  echo "[healer] run_dir not found: $RUN_DIR" >&2
  exit 3
fi

MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
AGENTFLOW_DIR="${AGENTFLOW_DIR:-${MINI_ORK_HOME}}"
DB="${MINI_ORK_DB:-$AGENTFLOW_DIR/state.db}"
RETRIEVE="$MINI_ORK_ROOT/lib/memory-retrieve.sh"
STORE="$MINI_ORK_ROOT/lib/memory-store.sh"

# ── Step 1: collect failure context ───────────────────────────────────
WORKER_LOG=$(find "$RUN_DIR" -name 'worker.log' -size +0c 2>/dev/null | head -1)
REVIEWER_VERDICT=$(find "$RUN_DIR" -name 'verdict.json' -size +0c 2>/dev/null | head -1)
GAUNTLET_LOG=$(find "$RUN_DIR" -name 'gauntlet*.log' -size +0c 2>/dev/null | head -1)

# Extract last ~50 lines of error-shaped output for pattern matching.
ERROR_LINES=""
for log in "$WORKER_LOG" "$GAUNTLET_LOG"; do
  [ -z "$log" ] && continue
  ERROR_LINES+=$(grep -iE 'error|fail|exception|cannot|invalid|exit code|429|401|403|503|TS[0-9]+|Cannot find module' "$log" 2>/dev/null | tail -50)$'\n'
done
ERROR_LINES=$(printf '%s' "$ERROR_LINES" | head -c 4000)

# Reviewer verdict gives us the structural failure type (REQUEST_CHANGES /
# ESCALATE / CRASH).
REVIEWER_VERDICT_TYPE=""
if [ -n "$REVIEWER_VERDICT" ]; then
  REVIEWER_VERDICT_TYPE=$(jq -r '.verdict // .final_verdict // ""' "$REVIEWER_VERDICT" 2>/dev/null)
fi

# ── Step 2: try fingerprint / regex match against lessons_bank ────────
echo "[healer] $EPIC_ID — searching lessons_bank for matching diagnosis..." >&2

LESSON_JSON=""
if [ -n "$ERROR_LINES" ] && [ -f "$RETRIEVE" ]; then
  LESSON_JSON=$("$RETRIEVE" match "$ERROR_LINES" 2>/dev/null || true)
fi

MATCHED=false
if [ -n "$LESSON_JSON" ]; then
  MATCHED=true
  echo "[healer] match found: $(echo "$LESSON_JSON" | jq -r '.failure_class + " → " + .recovery_action')" >&2
else
  # ── Step 3: miss — call LLM to classify ─────────────────────────────
  echo "[healer] no match in lessons_bank; calling LLM to classify..." >&2

  LLM_HELPERS="$MINI_ORK_ROOT/lib/agentflow-llm-helpers.sh"
  if [ ! -f "$LLM_HELPERS" ]; then
    echo "[healer] agentflow-llm-helpers.sh missing — emitting escalate-human" >&2
    printf '{"lesson_id":null,"failure_class":"unknown","recovery_action":"escalate-human","recovery_args":{},"matched":false}\n'
    exit 0
  fi
  # shellcheck disable=SC1091
  source "$LLM_HELPERS"
  if ! af_llm_ensure_key; then
    echo "[healer] no provider key — emitting escalate-human" >&2
    printf '{"lesson_id":null,"failure_class":"unknown","recovery_action":"escalate-human","recovery_args":{},"matched":false}\n'
    exit 0
  fi

  # Use generate-object for schema-coerced JSON output — eliminates the
  # regex JSON extraction + fence-stripping that bit us in early dev.
  SCHEMA_FILE="$MINI_ORK_ROOT/lib/schemas/failure-classification.json"
  prompt=$(cat <<EOF
Classify this mini-ork worker failure. Be specific in error_pattern —
it must be a regex that captures THIS class of failure without matching
unrelated errors (no bare "error" or "fail"; include distinguishing
tokens). Be conservative in recovery_action: prefer cleaner-on-main /
rebase-and-retry for code-state issues, escalate-human only when truly
no automated recovery makes sense.

Few-shot examples:
  - TS2307 'Cannot find module' → failure_class=baseline_rot,
    error_pattern="TS2307|Cannot find module",
    recovery_action=cleaner-on-main
  - 429 'rate_limit' → failure_class=rate_limit,
    error_pattern="429|rate.*limit",
    recovery_action=wait-and-retry,
    recovery_args={"wait_s":30}
  - 'Cannot find tool Task' (GLM lacks subagents) → failure_class=subagent_unsupported,
    error_pattern="Cannot find tool.*Task",
    recovery_action=switch-agent,
    recovery_args={"agent":"kimi"}

Reviewer verdict: $REVIEWER_VERDICT_TYPE
Epic ID: $EPIC_ID

Recent error lines:
$ERROR_LINES
EOF
)

  classification=$(printf '%s' "$prompt" | af_llm_call_json \
    --feature "mini-ork:healer-classify" \
    --tier smart \
    --epic-id "$EPIC_ID" \
    --actor mo-healer \
    --system 'You are a code-failure classifier. Match the supplied schema strictly.' \
    --schema-file "$SCHEMA_FILE" \
    --max-tokens 800 \
    --temperature 0.1 2>/dev/null)

  # generate-object returns {object: {...}, text: "...", cost_usd, ...}
  # Extract the parsed object (schema-validated).
  if ! echo "$classification" | jq -e '.object.failure_class' >/dev/null 2>&1; then
    echo "[healer] LLM classification missing schema fields — emitting escalate-human" >&2
    printf '{"lesson_id":null,"failure_class":"unknown","recovery_action":"escalate-human","recovery_args":{},"matched":false}\n'
    exit 0
  fi

  fc=$(echo "$classification" | jq -r '.object.failure_class')
  ep=$(echo "$classification" | jq -r '.object.error_pattern // ""')
  diag=$(echo "$classification" | jq -r '.object.diagnosis')
  ra=$(echo "$classification" | jq -r '.object.recovery_action')
  rargs=$(echo "$classification" | jq -c '.object.recovery_args // {}')

  # Persist as a new lesson.
  export MEMORY_STORE_SOURCE=llm
  export MEMORY_STORE_LLM_PROVIDER="kimi"
  export MEMORY_STORE_LLM_MODEL="kimi-k2.6"
  export MO_EVENT_EPIC="$EPIC_ID"
  lesson_id=$("$STORE" "$fc" "$ep" "$diag" "$ra" "$rargs" 2>/dev/null || echo "")

  if [ -z "$lesson_id" ]; then
    echo "[healer] memory-store failed; emitting LLM classification directly" >&2
    LESSON_JSON=$(printf '{"id":null,"failure_class":"%s","error_pattern":"%s","diagnosis":"%s","recovery_action":"%s","recovery_args_json":%s,"success_count":0,"failure_count":0,"source":"llm"}' \
      "$fc" "$ep" "$diag" "$ra" "$rargs")
  else
    LESSON_JSON=$("$RETRIEVE" fingerprint "$fc" "$ep")
    [ -z "$LESSON_JSON" ] && LESSON_JSON=$(sqlite3 "$DB" "SELECT json_object(
      'id', id,
      'failure_class', failure_class,
      'error_pattern', error_pattern,
      'diagnosis', diagnosis,
      'recovery_action', recovery_action,
      'recovery_args_json', json(recovery_args_json),
      'success_count', success_count,
      'failure_count', failure_count,
      'source', source
    ) FROM lessons_bank WHERE id=$lesson_id;")
  fi
fi

# ── Step 4: emit decision ─────────────────────────────────────────────
LESSON_ID=$(echo "$LESSON_JSON" | jq -r '.id // null')
FAILURE_CLASS=$(echo "$LESSON_JSON" | jq -r '.failure_class')
RECOVERY_ACTION=$(echo "$LESSON_JSON" | jq -r '.recovery_action')
RECOVERY_ARGS=$(echo "$LESSON_JSON" | jq -c '.recovery_args_json // {}')

# Emit mo_event if available
MO_EVENT_SH="$MINI_ORK_ROOT/lib/mo-event.sh"
if [ -f "$MO_EVENT_SH" ]; then
  # shellcheck disable=SC1091
  . "$MO_EVENT_SH"
  if command -v mo_emit >/dev/null 2>&1; then
    payload=$(printf '{"lesson_id":%s,"failure_class":"%s","recovery_action":"%s","matched":%s}' \
      "${LESSON_ID:-null}" "$FAILURE_CLASS" "$RECOVERY_ACTION" "$MATCHED")
    MO_EVENT_EPIC="$EPIC_ID" \
      mo_emit "note" "mo-healer" "$payload" "ok" "" "" >/dev/null 2>&1 || true
  fi
fi

printf '{"lesson_id":%s,"failure_class":"%s","recovery_action":"%s","recovery_args":%s,"matched":%s}\n' \
  "${LESSON_ID:-null}" "$FAILURE_CLASS" "$RECOVERY_ACTION" "$RECOVERY_ARGS" "$MATCHED"
