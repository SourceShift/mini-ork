#!/usr/bin/env bash
# verifiers/lens_outputs_complete.sh — assert all 10 lens verdicts +
# synthesizer panel-verdict + publisher report were written.
#
# Exit 0 = pass; non-zero = fail (per mini-ork verifier contract).
# Emits an evidence log at $MINI_ORK_VERIFIER_EVIDENCE for the
# reviewer + reflect cycle.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="${MINI_ORK_VERIFIER_EVIDENCE:-$RUN_DIR/verifier-lens-outputs-complete.log}"

: > "$EVIDENCE"

missing=0
for n in 01 02 03 04 05 06 07 08 09 10; do
  f="$RUN_DIR/lens-${n}-verdict.json"
  if [ -s "$f" ]; then
    if jq -e '.verdict and .score_0_to_10 != null' "$f" >/dev/null 2>&1; then
      echo "ok lens-${n}-verdict.json (verdict=$(jq -r .verdict "$f"), score=$(jq -r .score_0_to_10 "$f"))" >> "$EVIDENCE"
    else
      echo "fail lens-${n}-verdict.json present but schema-incomplete" >> "$EVIDENCE"
      missing=$((missing + 1))
    fi
  else
    echo "fail lens-${n}-verdict.json missing or empty" >> "$EVIDENCE"
    missing=$((missing + 1))
  fi
done

# Synthesizer output
if [ -s "$RUN_DIR/panel-verdict.json" ]; then
  if jq -e '.overall_verdict and .weighted_score != null' "$RUN_DIR/panel-verdict.json" >/dev/null 2>&1; then
    echo "ok panel-verdict.json (overall=$(jq -r .overall_verdict "$RUN_DIR/panel-verdict.json"), score=$(jq -r .weighted_score "$RUN_DIR/panel-verdict.json"))" >> "$EVIDENCE"
  else
    echo "fail panel-verdict.json present but schema-incomplete" >> "$EVIDENCE"
    missing=$((missing + 1))
  fi
else
  echo "fail panel-verdict.json missing — synthesizer did not produce final verdict" >> "$EVIDENCE"
  missing=$((missing + 1))
fi

# Publisher output (markdown report) — soft check; non-fatal
if [ -s "$RUN_DIR/chapter-validation-report.md" ]; then
  echo "ok chapter-validation-report.md present ($(wc -l < "$RUN_DIR/chapter-validation-report.md") lines)" >> "$EVIDENCE"
else
  echo "warn chapter-validation-report.md missing — publisher did not produce human-readable report" >> "$EVIDENCE"
fi

echo "" >> "$EVIDENCE"
echo "summary: ${missing} required artifact(s) missing of 11 (10 lenses + 1 synthesizer)" >> "$EVIDENCE"

exit "$missing"
