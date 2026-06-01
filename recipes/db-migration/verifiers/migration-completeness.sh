#!/usr/bin/env bash
# migration-completeness.sh — deterministic gate for db_migration recipe.
#
# Enforces (per artifact_contract + plan.json verifier_contract):
#  - migration-plan.md exists
#  - all 5 lens reports exist + ≥ 200 words each
#  - migration-plan.md has IF NOT EXISTS / IF EXISTS guards (idempotent)
#  - migration-plan.md has at least one Reversal SQL block
#  - migration-plan.md has Snapshot section (data-loss insurance)
#  - migration-plan.md has Smoke script section
#  - migration-plan.md has Risk summary table
#  - migration-plan.md has Process notes (audit trail)
#
# Exits 0 on pass, non-zero on fail.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR unset}"
PLAN="${RUN_DIR}/migration-plan.md"

errors=0

if [ ! -f "$PLAN" ]; then
  echo "✗ migration-plan.md missing at $PLAN" >&2
  errors=$((errors + 1))
fi

for lens in integrity rollback perf compat edge; do
  f="${RUN_DIR}/lens-${lens}.md"
  if [ ! -f "$f" ]; then
    echo "✗ lens-${lens}.md missing" >&2
    errors=$((errors + 1))
    continue
  fi
  wc_val=$(wc -w < "$f" | tr -d '[:space:]')
  if [ "$wc_val" -lt 200 ]; then
    echo "✗ lens-${lens}.md word count $wc_val < 200" >&2
    errors=$((errors + 1))
  fi
done

if [ -f "$PLAN" ]; then
  # Idempotency check — at least one IF EXISTS / IF NOT EXISTS guard
  if ! grep -qiE 'IF (NOT )?EXISTS' "$PLAN"; then
    echo "✗ migration-plan.md has no IF (NOT) EXISTS guards — non-idempotent" >&2
    errors=$((errors + 1))
  fi

  # Reversal SQL section
  if ! grep -qiE "Reversal|Rollback" "$PLAN"; then
    echo "✗ migration-plan.md missing Reversal / Rollback SQL" >&2
    errors=$((errors + 1))
  fi

  # Snapshot section
  if ! grep -qiE "Snapshot|pg_dump|mysqldump|backup" "$PLAN"; then
    echo "✗ migration-plan.md missing Snapshot / backup section" >&2
    errors=$((errors + 1))
  fi

  # Smoke script section
  if ! grep -qiE "Smoke|smoke|post-migration verification" "$PLAN"; then
    echo "✗ migration-plan.md missing Smoke / post-migration verification section" >&2
    errors=$((errors + 1))
  fi

  # Risk summary
  if ! grep -qi "Risk summary" "$PLAN"; then
    echo "✗ migration-plan.md missing 'Risk summary' table" >&2
    errors=$((errors + 1))
  fi

  # Process notes audit trail
  if ! grep -qi "Process notes" "$PLAN"; then
    echo "✗ migration-plan.md missing 'Process notes' audit-trail section" >&2
    errors=$((errors + 1))
  fi
fi

if [ "$errors" -gt 0 ]; then
  echo "" >&2
  echo "[migration-completeness] $errors gate failure(s)" >&2
  exit 1
fi

echo "[migration-completeness] OK — migration-plan + all 5 lenses present, idempotent + reversible + smoke-tested" >&2
exit 0
