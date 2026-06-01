#!/usr/bin/env bash
# draft-completeness.sh — deterministic gate for blog_post recipe.
#
# Per artifact_contract.yaml: draft.md exists + ≥ 0.8 × target_word_count +
# every lens-*.md exists at >= 200 words + synthesizer process-notes
# section present + no fabricated-looking citations.
#
# Exits 0 on pass, non-zero on fail. Errors to stderr.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR unset}"
PLAN="${RUN_DIR}/plan.json"
DRAFT="${RUN_DIR}/draft.md"

errors=0

# 1. Plan exists
if [ ! -f "$PLAN" ]; then
  echo "✗ plan.json missing at $PLAN" >&2
  errors=$((errors + 1))
  TARGET_WC=1200
else
  TARGET_WC=$(python3 -c "
import json
try:
    p = json.load(open('$PLAN'))
    print(int(p.get('target_word_count', 1200)))
except Exception:
    print(1200)
")
fi

# 2. Draft exists and meets word-count floor
if [ ! -f "$DRAFT" ]; then
  echo "✗ draft.md missing at $DRAFT" >&2
  errors=$((errors + 1))
else
  DRAFT_WC=$(wc -w < "$DRAFT" | tr -d '[:space:]')
  FLOOR=$(python3 -c "print(int(0.8 * $TARGET_WC))")
  if [ "$DRAFT_WC" -lt "$FLOOR" ]; then
    echo "✗ draft.md word count $DRAFT_WC < floor $FLOOR (0.8 × target $TARGET_WC)" >&2
    errors=$((errors + 1))
  fi
fi

# 3. Each lens-*.md exists at ≥ 200 words
for lens in editor researcher narrative audience counter; do
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

# 4. Synthesizer process-notes block present
if [ -f "$DRAFT" ] && ! grep -q "Process notes" "$DRAFT" 2>/dev/null; then
  echo "✗ draft.md missing 'Process notes' audit-trail section" >&2
  errors=$((errors + 1))
fi

# 5. Citation-fabrication smell-check: every URL must be plausibly real.
# Heuristic — flag URLs with obviously-fake path components.
if [ -f "$DRAFT" ]; then
  bad=$(grep -oE 'https?://[^[:space:])]+' "$DRAFT" 2>/dev/null \
    | grep -ciE '/fake/|/example/|placeholder|/lorem/' || true)
  if [ "$bad" -gt 0 ]; then
    echo "✗ draft.md contains $bad URL(s) matching fabrication smell-test" >&2
    errors=$((errors + 1))
  fi
fi

if [ "$errors" -gt 0 ]; then
  echo "" >&2
  echo "[draft-completeness] $errors gate failure(s)" >&2
  exit 1
fi

echo "[draft-completeness] OK — draft + all 5 lenses + process-notes present" >&2
exit 0
