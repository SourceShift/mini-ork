#!/usr/bin/env bash
set -euo pipefail

PLAN_PATH="${MINI_ORK_PLAN_PATH:-}"
RUN_DIR="${MINI_ORK_RUN_DIR:-}"
PROFILE_PATH="${MINI_ORK_PROFILE_PATH:-}"

if [ -z "$RUN_DIR" ] && [ -n "$PLAN_PATH" ]; then
  RUN_DIR="$(dirname "$PLAN_PATH")"
fi

SELECTED_FILE="${RUN_DIR:-.}/selected-option.md"
OPTIONS_FILE="${RUN_DIR:-.}/options.md"

preselected="false"
kickoff_path=""
if [ -n "$PROFILE_PATH" ] && [ -f "$PROFILE_PATH" ]; then
  kickoff_path="$(python3 - "$PROFILE_PATH" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print((json.load(f).get("kickoff_path") or "").strip())
except Exception:
    print("")
PY
)"
fi

if [ -n "$kickoff_path" ] && [ -f "$kickoff_path" ]; then
  if grep -Eqi '^(selected|preselected)[ _-]*option\s*:' "$kickoff_path"; then
    preselected="true"
  fi
fi

missing=()
if [ ! -f "$OPTIONS_FILE" ]; then
  missing+=("options.md")
fi

if [ ! -s "$SELECTED_FILE" ] && [ "$preselected" != "true" ]; then
  missing+=("selected-option.md or kickoff Selected Option:")
fi

python3 - "$SELECTED_FILE" "$OPTIONS_FILE" "$preselected" "${missing[@]}" <<'PY'
import json
import sys

selected_file, options_file, preselected = sys.argv[1:4]
missing = sys.argv[4:]
print(json.dumps({
    "verifier": "selected-option-gate",
    "pass": not missing,
    "selected_file": selected_file,
    "options_file": options_file,
    "preselected_by_kickoff": preselected == "true",
    "missing": missing,
}))
PY

[ "${#missing[@]}" -eq 0 ]
