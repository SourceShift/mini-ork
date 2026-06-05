#!/usr/bin/env bash
# gates/synthesis-promote.sh — recipe-level synthesis-class promotion gate.
#
# Wraps lib/promotion_gate.sh::mo_promote_synthesis_gate. Enforces the
# conjunction-of-three contract for synthesis-class task classes
# (research_synthesis, refactor_audit, blog-post, ui-audit, ops-runbook):
#   1. panel_score >= MO_PROMOTE_SCORE_THRESHOLD (default 80)
#   2. CW-POR <= MO_CW_POR_THRESHOLD (default 0.3) — no authority capture
#   3. >= 1 structural quality signal (citations / file coverage / cardinality)
#
# Deterministic-oracle classes (code_fix, db_migration) bypass this gate
# entirely with early-return reason='deterministic_class'.
#
# Context JSON contract:
#   { "verdict_file": "<path-to-panel-verdict-with-structural.json>",
#     "task_class":   "<task-class-name>" }
#
# Env knobs (forwarded to the lib):
#   MO_PROMOTE_SCORE_THRESHOLD     default 80
#   MO_CW_POR_THRESHOLD            default 0.3
#   MO_MIN_CITATION_DENSITY        default 3
#   MO_MIN_FINDING_CARDINALITY     default 5
#   MO_DETERMINISTIC_TASK_CLASSES  default "code_fix db_migration"
#
# rc:
#   0 → approved (auto-promote-eligible)
#   1 → rejected (one of the 3 conditions failed — see .reason field)
#   2 → defer (malformed input / lib unloadable)

set +e
MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

context="${1:-}"
if [ -z "$context" ]; then
  printf '{"decision":"defer","reason":"no context json"}\n'
  exit 2
fi

verdict_file=$(printf '%s' "$context" | jq -r '.verdict_file // empty' 2>/dev/null)
task_class=$(printf '%s' "$context" | jq -r '.task_class // empty' 2>/dev/null)

if [ -z "$verdict_file" ] || [ ! -f "$verdict_file" ] || [ -z "$task_class" ]; then
  printf '{"decision":"defer","reason":"context missing verdict_file or task_class"}\n'
  exit 2
fi

# shellcheck source=../lib/promotion_gate.sh
source "$MINI_ORK_ROOT/lib/promotion_gate.sh" 2>/dev/null || {
  printf '{"decision":"defer","reason":"promotion_gate lib not loadable"}\n'
  exit 2
}

result=$(mo_promote_synthesis_gate "$verdict_file" "$task_class" 2>/dev/null)
decision=$(printf '%s' "$result" | jq -r '.decision // "defer"' 2>/dev/null)
printf '%s\n' "$result"

case "$decision" in
  approved)  exit 0 ;;
  rejected)  exit 1 ;;
  *)         exit 2 ;;
esac
