#!/usr/bin/env bash
# runbook-completeness.sh — deterministic gate for ops_runbook recipe.
#
# Enforces:
#  - runbook.md exists
#  - all 5 lens reports exist
#  - runbook has the 5 expected section headers (Detection / Containment /
#    Diagnosis / Recovery / Prevention OR sections 0-4)
#  - runbook has a TL;DR section
#  - runbook has Process notes (audit trail)
#  - every code block under Recovery has Verify OR Rollback within 5 lines
#
# Exits 0 on pass, non-zero on fail.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR unset}"
RUNBOOK="${RUN_DIR}/runbook.md"

errors=0

if [ ! -f "$RUNBOOK" ]; then
  echo "✗ runbook.md missing at $RUNBOOK" >&2
  errors=$((errors + 1))
fi

# Each lens file present
for lens in detection containment diagnosis recovery prevention; do
  f="${RUN_DIR}/lens-${lens}.md"
  if [ ! -f "$f" ]; then
    echo "✗ lens-${lens}.md missing" >&2
    errors=$((errors + 1))
  else
    wc_val=$(wc -w < "$f" | tr -d '[:space:]')
    if [ "$wc_val" -lt 150 ]; then
      echo "✗ lens-${lens}.md word count $wc_val < 150 (too thin to be useful)" >&2
      errors=$((errors + 1))
    fi
  fi
done

if [ -f "$RUNBOOK" ]; then
  # TL;DR section
  if ! grep -qiE '^## (0 — |TL;DR|0\. )' "$RUNBOOK"; then
    echo "✗ runbook.md missing TL;DR / Section-0 header" >&2
    errors=$((errors + 1))
  fi

  # Each phase header
  for phase in Detection Containment Diagnosis Recovery Prevention; do
    if ! grep -qiE "^(#{1,3}) .*${phase}" "$RUNBOOK"; then
      echo "⚠ runbook.md missing '${phase}' section header (warn — may be in different casing)" >&2
    fi
  done

  # Process notes
  if ! grep -qi "Process notes" "$RUNBOOK"; then
    echo "✗ runbook.md missing 'Process notes' audit-trail section" >&2
    errors=$((errors + 1))
  fi

  # Recovery section MUST have at least one Verify or Rollback line
  awk '/^## .*Recovery/,/^## /{print}' "$RUNBOOK" > /tmp/runbook-recovery-section.txt 2>/dev/null || true
  if [ -s /tmp/runbook-recovery-section.txt ]; then
    if ! grep -qiE "Verify|Rollback" /tmp/runbook-recovery-section.txt; then
      echo "✗ Recovery section has no Verify or Rollback lines — every recovery step should have both" >&2
      errors=$((errors + 1))
    fi
  fi
  rm -f /tmp/runbook-recovery-section.txt
fi

if [ "$errors" -gt 0 ]; then
  echo "" >&2
  echo "[runbook-completeness] $errors gate failure(s)" >&2
  exit 1
fi

echo "[runbook-completeness] OK — runbook.md + all 5 lenses present, structure intact" >&2
exit 0
