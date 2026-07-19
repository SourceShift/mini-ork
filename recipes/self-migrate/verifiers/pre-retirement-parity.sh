#!/usr/bin/env bash
# verifiers/pre-retirement-parity.sh — capture the Bash oracle before migration.
#
# This verifier is ordered before the migrator. Its evidence survives deletion
# of the legacy entrypoint in the proposed worktree diff, proving parity was
# established while both runtimes still existed.

set -uo pipefail
RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
REPO_ROOT="${MO_TARGET_CWD:-${MINI_ORK_ROOT:-$(pwd)}}"
FORK="${MO_FORK:-}"
FORK_TEST="$REPO_ROOT/tests/unit/test_mini_ork_${FORK}_py.py"
HARNESS="$REPO_ROOT/scripts/runtime-parity-harness.sh"
EVIDENCE="$RUN_DIR/pre-retirement-parity-evidence.log"
STATE="$RUN_DIR/pre-retirement-parity.json"

pass=true
reasons=()

# A later recovery or verifier partition may revisit this node after the Bash
# entrypoint has been removed in the worktree. Reuse only a passing state from
# this unique run directory and only while its evidence file still exists.
if [ -s "$STATE" ] && [ -s "$EVIDENCE" ] && python3 - "$STATE" <<'PY'
import json
import sys

try:
    state = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if state.get("pass") is True else 1)
PY
then
  cat "$STATE"
  exit 0
fi

if [ -n "$FORK" ] && [ -f "$FORK_TEST" ]; then
  if (
    cd "$REPO_ROOT"
    env -u MINI_ORK_RUN_DIR -u MINI_ORK_RECIPE -u MINI_ORK_RUN_ID \
      -u MINI_ORK_PLAN_PATH -u MINI_ORK_TASK_CLASS \
      python3 -m pytest "$FORK_TEST" -q -p no:cacheprovider
  ) >"$EVIDENCE" 2>&1; then
    pass=true
  else
    pass=false
    reasons+=("pre-retirement fork parity failed: $FORK_TEST")
  fi
elif [ -f "$HARNESS" ]; then
  if bash "$HARNESS" >"$EVIDENCE" 2>&1; then
    pass=true
  else
    pass=false
    reasons+=("pre-retirement runtime parity harness failed")
  fi
else
  pass=false
  reasons+=("no pre-retirement parity oracle found")
fi

python3 - "$pass" "$EVIDENCE" "$FORK" "$STATE" "${reasons[@]:-}" <<'PY'
import json
import sys

pass_str, evidence, fork, state_path = sys.argv[1:5]
reasons = [reason for reason in sys.argv[5:] if reason]
result = {
    "name": "pre-retirement-parity",
    "fork": fork,
    "pass": pass_str == "true",
    "evidence": evidence,
    "reasons": reasons,
}
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle)
    handle.write("\n")
print(json.dumps(result))
PY

[ "$pass" = true ]
