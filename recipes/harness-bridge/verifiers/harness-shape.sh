#!/usr/bin/env bash
# harness-shape.sh — verifier for harness-bridge recipe.
#
# Checks that the harness wrapper emitted the expected artifacts:
#   1. harness-verdict.json is well-formed JSON
#   2. it names a known harness
#   3. if a diff was produced, it parses as a unified diff
#
# Per mini-ork verifier contract: emit JSON on stdout, exit 0
# regardless of pass/fail (the JSON result.pass is the signal).

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
EVIDENCE="$RUN_DIR/harness-verifier-evidence.log"
exec 3>"$EVIDENCE"

VERDICT_FILE="$RUN_DIR/harness-verdict.json"
DIFF_FILE="$RUN_DIR/harness.diff"

artifact_verdict_exists=false
verdict_parses=false
harness_known=false
diff_shape_ok=true  # default true when no diff produced

if [ -f "$VERDICT_FILE" ]; then
  artifact_verdict_exists=true
  echo "[ok] harness-verdict.json exists" >&3
  if python3 -c "import json,sys; json.load(open('$VERDICT_FILE'))" 2>/dev/null; then
    verdict_parses=true
    echo "[ok] harness-verdict.json parses as JSON" >&3
    harness_name=$(python3 -c "import json; print(json.load(open('$VERDICT_FILE')).get('harness',''))" 2>/dev/null)
    case "$harness_name" in
      claude-code|codex-cli|gemini-cli) harness_known=true ;;
    esac
    if [ "$harness_known" = "true" ]; then
      echo "[ok] harness recognized: $harness_name" >&3
    else
      echo "[fail] unknown harness in verdict: $harness_name" >&3
    fi
  fi
fi

if [ -s "$DIFF_FILE" ]; then
  if grep -q '^diff --git' "$DIFF_FILE"; then
    diff_shape_ok=true
    echo "[ok] diff shape valid" >&3
  else
    diff_shape_ok=false
    echo "[fail] diff present but lacks 'diff --git' anchor" >&3
  fi
fi

if [ "$artifact_verdict_exists" = "true" ] && [ "$verdict_parses" = "true" ] \
   && [ "$harness_known" = "true" ] && [ "$diff_shape_ok" = "true" ]; then
  pass=true
else
  pass=false
fi

# Translate bash booleans (true/false) to Python booleans (True/False)
# at the heredoc boundary so json.dumps receives valid identifiers.
_py_bool() { [ "$1" = "true" ] && printf 'True' || printf 'False'; }

python3 -c "
import json
print(json.dumps({
  'verifier': 'harness-shape',
  'pass': $(_py_bool "$pass"),
  'evidence_path': '$EVIDENCE',
  'artifact_verdict_exists': $(_py_bool "$artifact_verdict_exists"),
  'verdict_parses': $(_py_bool "$verdict_parses"),
  'harness_known': $(_py_bool "$harness_known"),
  'diff_shape_ok': $(_py_bool "$diff_shape_ok"),
}))
"
exit 0
