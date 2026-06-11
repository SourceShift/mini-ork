#!/usr/bin/env bash
# verifiers/schema.sh — verify chapter-review.json shape, syntax, and keys.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "schema", "pass": bool, "evidence_path": "...",
#     "findings": [...] }
#
# Exit codes: always 0 (caller reads .pass from JSON).
set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-schema.log"
exec 3>"$EVIDENCE"

TARGET="$RUN_DIR/chapter-review.json"
findings=()
pass=true

_fail() { findings+=("$1"); pass=false; echo "FAIL: $1" >&3; }
_ok()   { echo "OK:   $1" >&3; }

# 1. File exists + valid JSON
if [ ! -f "$TARGET" ]; then
  _fail "chapter-review.json not found at $TARGET"
elif ! jq -e . "$TARGET" >/dev/null 2>&1; then
  _fail "chapter-review.json is not valid JSON"
else
  _ok "chapter-review.json exists and is valid JSON"
fi

if [ "$pass" = true ]; then
  # 2. schema_version exactly "1.0.0"
  sv=$(jq -r '.schema_version // ""' "$TARGET")
  [ "$sv" = "1.0.0" ] && _ok "schema_version=1.0.0" \
    || _fail "schema_version must be \"1.0.0\" (got \"$sv\")"

  # 3. All 9 axis keys present + per-axis shape
  for ax in C1_structure_flow C2_clarity_conciseness C3_style_voice \
            C4_engagement_pacing C5_factuality_citations C6_technical_accuracy \
            C7_audience_fit C8_narrative_coherence C9_originality_insight; do
    if ! jq -e ".axes[\"$ax\"]" "$TARGET" >/dev/null 2>&1; then
      _fail "axes.$ax missing"
      continue
    fi
    sc=$(jq -r ".axes[\"$ax\"].score // empty" "$TARGET")
    [[ "$sc" =~ ^[0-9]+$ ]] && [ "$sc" -ge 1 ] && [ "$sc" -le 10 ] \
      || _fail "axes.$ax.score must be int 1-10 (got \"$sc\")"
    rt=$(jq -r ".axes[\"$ax\"].rationale // empty" "$TARGET")
    [ -n "$rt" ] || _fail "axes.$ax.rationale must be non-empty string"
    conf=$(jq -r ".axes[\"$ax\"].confidence // empty" "$TARGET")
    awk -v c="$conf" 'BEGIN { exit !(c != "" && c+0 >= 0 && c+0 <= 1) }' \
      || _fail "axes.$ax.confidence must be number in [0,1] (got \"$conf\")"
    src_t=$(jq -r ".axes[\"$ax\"].sources | type" "$TARGET" 2>/dev/null || echo "")
    [ "$src_t" = "array" ] || _fail "axes.$ax.sources must be array (got $src_t)"
  done

  # 4. fragment_suggestions array
  fs_t=$(jq -r '.fragment_suggestions | type' "$TARGET" 2>/dev/null || echo "")
  [ "$fs_t" = "array" ] || _fail "fragment_suggestions must be array (got $fs_t)"

  # 5. overall_verdict enum
  ov=$(jq -r '.overall_verdict // ""' "$TARGET")
  case "$ov" in
    ACCEPT|MINOR_REVISION|MAJOR_REVISION|REJECT) _ok "overall_verdict=$ov" ;;
    *) _fail "overall_verdict must be ACCEPT|MINOR_REVISION|MAJOR_REVISION|REJECT (got \"$ov\")" ;;
  esac

  # 6. summary non-empty
  sm=$(jq -r '.summary // ""' "$TARGET")
  [ -n "$sm" ] || _fail "summary must be non-empty string"

  # 7. panel_disagreement_score in [0,1]
  pds=$(jq -r '.panel_disagreement_score // empty' "$TARGET")
  awk -v p="$pds" 'BEGIN { exit !(p != "" && p+0 >= 0 && p+0 <= 1) }' \
    || _fail "panel_disagreement_score must be number in [0,1] (got \"$pds\")"

  # 8. panel sub-object references all 4 lenses
  for lens in glm kimi codex opus; do
    jq -e ".panel[\"$lens\"]" "$TARGET" >/dev/null 2>&1 \
      || _fail "panel.$lens missing"
  done
fi

# Emit verdict JSON
fjson=$(printf '%s\n' "${findings[@]+"${findings[@]}"}" | jq -R . | jq -s .)
jq -nc \
  --argjson pass "$pass" \
  --arg evidence "$EVIDENCE" \
  --argjson findings "${fjson:-[]}" \
  '{verifier:"schema", pass:$pass, evidence_path:$evidence, findings:$findings}'
