#!/usr/bin/env bash
# verifiers/tier4-panel-quorum.sh — fail-fast quorum gate for tier-4 panel.
#
# Counts tier4-{glm,kimi,codex,minimax}.md files that exist AND are
# non-empty (size > 100 bytes). Passes if count >= MO_TIER4_QUORUM
# (default 3 of 4). Emits JSON to stdout per the mini-ork verifier
# contract; exit code is always 0.
#
# Stops the recipe-level stall observed in run-1781333552-17796-identity-and-rbac
# and run-1781377882-65036-intervention-policies-gate where 2 of 4
# tier-4 lens reports were missing and tier4_synth hung forever.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR — run directory written by the native execute runtime
#   MO_TIER4_QUORUM  — minimum non-empty lens reports (default 3)
#
# Output: JSON to stdout with shape:
#   {"verifier":"tier4-panel-quorum","pass":true|false,"verdict":"pass|fail",
#    "evidence_path":"...","quorum_required":N,"quorum_met":M,
#    "present":["codex","minimax"],"missing":["kimi","glm"]}

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
QUORUM="${MO_TIER4_QUORUM:-3}"
MIN_SIZE_BYTES="${MO_TIER4_LENS_MIN_BYTES:-100}"
NAME="tier4-panel-quorum"
EVIDENCE="$RUN_DIR/verifier-$NAME.log"
: >"$EVIDENCE"

LENSES=(glm kimi codex minimax)
present=()
missing=()

for lens in "${LENSES[@]}"; do
  f="$RUN_DIR/tier4-${lens}.md"
  if [ -f "$f" ]; then
    size=$(wc -c <"$f" 2>/dev/null | tr -d ' ')
    if [ -n "$size" ] && [ "$size" -gt "$MIN_SIZE_BYTES" ]; then
      present+=("$lens")
      echo "[present] tier4-${lens}.md size=${size}" >>"$EVIDENCE"
      continue
    fi
    echo "[missing] tier4-${lens}.md exists but size=${size:-0} <= MIN ($MIN_SIZE_BYTES)" >>"$EVIDENCE"
  else
    echo "[missing] tier4-${lens}.md does not exist" >>"$EVIDENCE"
  fi
  missing+=("$lens")
done

met=${#present[@]}
pass=$([ "$met" -ge "$QUORUM" ] && echo true || echo false)
verdict=$([ "$pass" = "true" ] && echo pass || echo fail)

python3 - "$NAME" "$EVIDENCE" "$pass" "$verdict" "$QUORUM" "$met" "${present[*]}" "${missing[*]}" <<'PY'
import json, sys
name, evidence, pass_s, verdict, quorum, met, present_s, missing_s = sys.argv[1:9]
present = [x for x in present_s.split() if x]
missing = [x for x in missing_s.split() if x]
print(json.dumps({
    "verifier": name,
    "pass": pass_s == "true",
    "verdict": verdict,
    "evidence_path": evidence,
    "quorum_required": int(quorum),
    "quorum_met": int(met),
    "present": present,
    "missing": missing,
    "reasons": [] if pass_s == "true" else [f"tier-4 quorum {met}/{quorum} — missing lenses: {','.join(missing)}"],
}))
PY
exit 0
