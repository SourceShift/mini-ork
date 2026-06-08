#!/usr/bin/env bash
set -euo pipefail

PLAN_PATH="${MINI_ORK_PLAN_PATH:-}"
RUN_DIR="${MINI_ORK_RUN_DIR:-}"

if [ -z "$RUN_DIR" ] && [ -n "$PLAN_PATH" ]; then
  RUN_DIR="$(dirname "$PLAN_PATH")"
fi

OPTIONS_FILE="${ARTIFACT_PATH:-}"
if [ -z "$OPTIONS_FILE" ] || [ "$OPTIONS_FILE" = "." ]; then
  OPTIONS_FILE="${RUN_DIR:-.}/options.md"
fi

missing=()
[ -f "$OPTIONS_FILE" ] || missing+=("options.md")

if [ -f "$OPTIONS_FILE" ]; then
  grep -qi "Option" "$OPTIONS_FILE" || missing+=("Option")
  grep -Eqi "Recommend(ed|ation)?" "$OPTIONS_FILE" || missing+=("Recommendation")
  grep -qi "Tradeoff" "$OPTIONS_FILE" || missing+=("Tradeoff")
  grep -qi "Risk" "$OPTIONS_FILE" || missing+=("Risk")
  grep -qi "Validation" "$OPTIONS_FILE" || missing+=("Validation")
  grep -qi "Decision" "$OPTIONS_FILE" || missing+=("Decision")
fi

python3 - "$OPTIONS_FILE" "${missing[@]}" <<'PY'
import json
import sys

options_file = sys.argv[1]
missing = sys.argv[2:]
print(json.dumps({
    "verifier": "options-completeness",
    "pass": not missing,
    "options_file": options_file,
    "missing": missing,
}))
PY

[ "${#missing[@]}" -eq 0 ]
