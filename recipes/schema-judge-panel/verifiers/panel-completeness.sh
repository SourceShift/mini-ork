#!/usr/bin/env bash
set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-panel-completeness.log"
exec 3>"$EVIDENCE"

missing=()
reports=(
  judge-opus-scalability.md
  judge-opus-llm-safety.md
  judge-kimi-correctness.md
  judge-codex-codebase.md
  judge-minimax-performance.md
)

for report in "${reports[@]}"; do
  f="$RUN_DIR/$report"
  if [ ! -f "$f" ]; then
    echo "MISSING: $report" >&3
    missing+=("$report")
    continue
  fi
  lines=$(wc -l < "$f" | tr -d ' ')
  evidence_hits=$(grep -ciE 'Discovery Evidence|information_schema|pg_stat|rg |server/|docs/|SELECT|file:line|:[0-9]+' "$f" || true)
  plan_hits=$(grep -ciE '^#+ +Migration Plan|phased|phase|gate|rollback|backfill' "$f" || true)
  echo "$report: lines=$lines evidence_hits=$evidence_hits plan_hits=$plan_hits" >&3
  [ "$lines" -lt 20 ] && missing+=("$report too_short")
  [ "$evidence_hits" -lt 2 ] && missing+=("$report missing_discovery_evidence")
  [ "$plan_hits" -lt 1 ] && missing+=("$report missing_plan")
done

synth="$RUN_DIR/synthesis.md"
if [ ! -f "$synth" ]; then
  missing+=("synthesis.md")
else
  for needle in "opus-scalability" "opus-llm" "kimi" "codex" "minimax"; do
    if ! grep -qi "$needle" "$synth"; then
      missing+=("synthesis.md missing_$needle")
    fi
  done
fi

if [ "${#missing[@]}" -eq 0 ]; then
  python3 -c "import json; print(json.dumps({'verifier':'panel-completeness','pass':True,'evidence_path':'$EVIDENCE','missing':[]}))"
else
  python3 - "$EVIDENCE" "${missing[@]}" <<'PY'
import json, sys
print(json.dumps({
    "verifier": "panel-completeness",
    "pass": False,
    "evidence_path": sys.argv[1],
    "missing": sys.argv[2:],
}))
PY
fi

exit 0
