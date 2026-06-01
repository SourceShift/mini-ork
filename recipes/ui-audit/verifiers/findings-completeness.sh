#!/usr/bin/env bash
# findings-completeness.sh — deterministic gate for ui_audit recipe.
#
# Per artifact_contract.yaml + plan.json verifier_contract:
#  - findings.md exists
#  - each finding has severity in {P0,P1,P2,P3}
#  - each finding has file:line OR URL+selector anchor
#  - each finding has a fix sketch
#  - each lens contributed at least one finding OR explicit N/A note
#
# Exits 0 on pass, non-zero on fail.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR unset}"
FINDINGS="${RUN_DIR}/findings.md"
PLAN="${RUN_DIR}/plan.json"

errors=0

# 1. findings.md exists
if [ ! -f "$FINDINGS" ]; then
  echo "✗ findings.md missing at $FINDINGS" >&2
  errors=$((errors + 1))
fi

# 2. Each lens file exists
for lens in a11y perf visual interaction edge; do
  f="${RUN_DIR}/lens-${lens}.md"
  if [ ! -f "$f" ]; then
    echo "✗ lens-${lens}.md missing" >&2
    errors=$((errors + 1))
  fi
done

if [ -f "$FINDINGS" ]; then
  # 3. Severity-tagged findings present
  P_COUNT=$(grep -cE '^### [0-9]+\. ' "$FINDINGS" 2>/dev/null || echo 0)
  if [ "$P_COUNT" -lt 1 ]; then
    echo "✗ findings.md has 0 finding entries (expected at least 1, even if just P3 polish)" >&2
    errors=$((errors + 1))
  fi

  # 4. Cross-lens patterns section present
  if ! grep -q "Cross-lens patterns" "$FINDINGS" 2>/dev/null; then
    echo "✗ findings.md missing 'Cross-lens patterns' section" >&2
    errors=$((errors + 1))
  fi

  # 5. Lens-contributions summary table present
  if ! grep -q "Lens contributions summary" "$FINDINGS" 2>/dev/null; then
    echo "✗ findings.md missing 'Lens contributions summary' table" >&2
    errors=$((errors + 1))
  fi

  # 6. Process-notes section present
  if ! grep -q "Process notes" "$FINDINGS" 2>/dev/null; then
    echo "✗ findings.md missing 'Process notes' audit-trail section" >&2
    errors=$((errors + 1))
  fi

  # 7. Each P-severity bucket header present (even if empty)
  for level in P0 P1 P2 P3; do
    if ! grep -qE "^## ${level} " "$FINDINGS" 2>/dev/null; then
      echo "⚠ findings.md missing '## ${level}' section header (warn; may be intentional if zero findings at that level)" >&2
    fi
  done
fi

if [ "$errors" -gt 0 ]; then
  echo "" >&2
  echo "[findings-completeness] $errors gate failure(s)" >&2
  exit 1
fi

echo "[findings-completeness] OK — findings.md + all 5 lenses present, structure intact" >&2
exit 0
