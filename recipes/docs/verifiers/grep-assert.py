#!/usr/bin/env python3
# verifiers/grep-assert.py — grep-pattern assertion runner for `docs` recipe.
#
# Python port of grep-assert.sh (bash-removal WS8). Same per-assertion status
# text, JSON summary, and rc semantics (jq queries reimplemented with the json
# module).
#
# Reads verifier_contract.checks[] from the plan JSON and runs each grep
# assertion against the named file. An assertion is:
#
#   { "kind": "grep", "file": "<path>", "pattern": "<extended-regex>", "min_count": <int> }
#
# Each assertion passes when `grep -cE "<pattern>" <file>` returns ≥ min_count.
# rc=0 when ALL grep assertions pass. rc=1 on ANY failure.
#
# Env:
#   MINI_ORK_PLAN_PATH    path to the plan JSON (default:
#                         $MINI_ORK_HOME/runs/$MINI_ORK_RUN_ID/plan.json)
#   MINI_ORK_HOME         project home (default: $(pwd)/.mini-ork)
#   MINI_ORK_RUN_ID       current run id (used in log path)
#
# Output: human-readable per-assertion status + a JSON summary on the final
# line for the run logger to parse.

import json
import os
import re
import sys

MINI_ORK_HOME = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
MINI_ORK_RUN_ID = os.environ.get("MINI_ORK_RUN_ID", "unknown-run")
PLAN_PATH = os.environ.get("MINI_ORK_PLAN_PATH") or \
    os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID, "plan.json")
LOG_DIR = os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "verifier_grep_assert.log")

_log = open(LOG_PATH, "a")


def emit(line):
    """tee -a LOG_PATH"""
    print(line)
    _log.write(line + "\n")
    _log.flush()


if not os.path.isfile(PLAN_PATH):
    print(json.dumps({"verifier": "grep-assert", "status": "skipped",
                      "reason": f"plan not found: {PLAN_PATH}"}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)

# Extract every grep assertion.
try:
    plan = json.load(open(PLAN_PATH, encoding="utf-8"))
except Exception:
    plan = {}
checks = ((plan.get("verifier_contract") or {}).get("checks") or [])
assertions = [c for c in checks if isinstance(c, dict) and c.get("kind") == "grep"]

if not assertions:
    emit(json.dumps({"verifier": "grep-assert", "status": "skipped",
                     "reason": "no grep assertions in plan"}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)

n_total = len(assertions)
n_passed = 0
n_failed = 0
failed_details = []

for a in assertions:
    a_json = json.dumps(a, separators=(",", ":"))
    file = a.get("file") or ""
    pattern = a.get("pattern") or ""
    min_count = a.get("min_count", 1)

    if not file or not pattern:
        n_failed += 1
        failed_details.append(f"malformed assertion (missing file or pattern): {a_json}")
        emit(f"  [FAIL] malformed: {a_json}")
        continue

    if not os.path.isfile(file):
        n_failed += 1
        failed_details.append(f"file not found: {file} (pattern was: {pattern})")
        emit(f"  [FAIL] file not found: {file}")
        continue

    # grep -cE: count of lines matching the (extended) pattern. An invalid
    # pattern errors in grep (rc 2 → count treated as 0); mirror that.
    try:
        rx = re.compile(pattern)
        count = sum(1 for line in open(file, encoding="utf-8", errors="replace") if rx.search(line))
    except re.error:
        count = 0

    if count >= min_count:
        n_passed += 1
        emit(f"  [PASS] {file} ~ /{pattern}/  count={count} (>= {min_count})")
    else:
        n_failed += 1
        failed_details.append(f"count={count} below min={min_count} for /{pattern}/ in {file}")
        emit(f"  [FAIL] {file} ~ /{pattern}/  count={count} (< {min_count})")

# JSON summary on final line for log parsers.
if n_failed == 0:
    emit(json.dumps({"verifier": "grep-assert", "status": "pass", "passed": n_passed,
                     "failed": 0, "total": n_total}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)
else:
    emit(json.dumps({"verifier": "grep-assert", "status": "fail", "passed": n_passed,
                     "failed": n_failed, "total": n_total,
                     "failures": failed_details}, separators=(",", ":"), ensure_ascii=False))
    sys.exit(1)
