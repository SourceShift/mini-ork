#!/usr/bin/env bash
# verifiers/panel-completeness.sh — verify all 4 lens artifacts exist
# pre-synthesis and that panel_disagreement_score is derivable.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR     run directory (set by mini-ork-execute)
#
# Output: JSON to stdout
#   { "verifier": "panel-completeness", "pass": bool,
#     "evidence_path": "...", "missing": [...],
#     "lens_scores_present": { "glm": bool, "kimi": bool,
#                               "codex": bool, "opus": bool } }
#
# Exit codes: always 0 (caller reads .pass from JSON).
set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/verifier-panel-completeness.log"
exec 3>"$EVIDENCE"

missing=()
declare -A score_ok=()

# Per-lens assigned-axis list (matches prompts/lens-*.md responsibility split)
glm_axes="C1_structure_flow C2_clarity_conciseness C7_audience_fit"
kimi_axes="C3_style_voice C4_engagement_pacing C8_narrative_coherence"
codex_axes="C5_factuality_citations C6_technical_accuracy"
opus_axes="C9_originality_insight"

_check_lens() {
  local lens="$1" axes_var="${1}_axes"
  local axes="${!axes_var}"
  local f="$RUN_DIR/lens-${lens}.json"
  if [ ! -f "$f" ]; then
    echo "MISSING: $f" >&3
    missing+=("lens-${lens}.json")
    score_ok[$lens]=false
    return
  fi
  if ! jq -e . "$f" >/dev/null 2>&1; then
    echo "INVALID-JSON: $f" >&3
    missing+=("lens-${lens}.json (invalid JSON)")
    score_ok[$lens]=false
    return
  fi
  local all_present=true
  for ax in $axes; do
    local sc; sc=$(jq -r ".axes[\"$ax\"].score // empty" "$f" 2>/dev/null)
    if ! [[ "$sc" =~ ^[0-9]+$ ]] || [ "$sc" -lt 1 ] || [ "$sc" -gt 10 ]; then
      echo "MISSING-OR-BAD-SCORE: $f axes.$ax (got \"$sc\")" >&3
      all_present=false
    fi
  done
  if [ "$all_present" = true ]; then
    score_ok[$lens]=true
    echo "OK: lens-${lens}.json has all assigned axes scored" >&3
  else
    score_ok[$lens]=false
    missing+=("lens-${lens}.json (assigned axes missing/invalid)")
  fi
}

for lens in glm kimi codex opus; do _check_lens "$lens"; done

# Synthesis presence + panel sub-object cross-reference
synth="$RUN_DIR/chapter-review.json"
if [ ! -f "$synth" ]; then
  missing+=("chapter-review.json")
  echo "MISSING: $synth" >&3
else
  for lens in glm kimi codex opus; do
    jq -e ".panel[\"$lens\"]" "$synth" >/dev/null 2>&1 \
      || missing+=("chapter-review.json: panel.$lens missing")
  done
fi

# panel_disagreement_score sanity: when all 4 lenses present, must be in [0,1]
if [ -f "$synth" ] && [ "${#missing[@]}" -eq 0 ]; then
  pds=$(jq -r '.panel_disagreement_score // empty' "$synth")
  awk -v p="$pds" 'BEGIN { exit !(p != "" && p+0 >= 0 && p+0 <= 1) }' \
    || missing+=("panel_disagreement_score not in [0,1] (got \"$pds\")")
fi

# pass = no missing entries
if [ "${#missing[@]}" -eq 0 ]; then pass=true; else pass=false; fi

lp=$(jq -nc \
  --argjson g "${score_ok[glm]:-false}" \
  --argjson k "${score_ok[kimi]:-false}" \
  --argjson c "${score_ok[codex]:-false}" \
  --argjson o "${score_ok[opus]:-false}" \
  '{glm:$g, kimi:$k, codex:$c, opus:$o}')

mjson=$(printf '%s\n' "${missing[@]+"${missing[@]}"}" | jq -R . | jq -s .)
jq -nc \
  --argjson pass "$pass" \
  --arg evidence "$EVIDENCE" \
  --argjson missing "${mjson:-[]}" \
  --argjson lens_scores_present "$lp" \
  '{verifier:"panel-completeness", pass:$pass, evidence_path:$evidence,
    missing:$missing, lens_scores_present:$lens_scores_present}'
