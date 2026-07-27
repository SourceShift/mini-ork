#!/usr/bin/env python3
# bdd-first-delivery — Playwright BDD verifier
#
# Python port of playwright_runner.sh (bash-removal WS8). Same rc semantics,
# env vars, and JSON output (jq queries reimplemented with the json module).
#
# Runs the Playwright spec for one sub-epic from within the implementer's
# working directory. Emits a JSON verdict to stdout and writes it to
# the path specified by MINI_ORK_VERDICT_DIR (if set).
#
# Required env:
#   SUB_EPIC_ID     — e.g. "USER-SETTINGS-V2-B"
#   WORKDIR         — absolute path to the implementer's worktree
#
# Optional env:
#   MINI_ORK_PLAYWRIGHT_CMD   — override the playwright invocation
#                               (default: npx playwright test)
#   MINI_ORK_VERDICT_DIR      — directory to write bdd-verdict.json into
#   MINI_ORK_BDD_TIMEOUT_SEC  — wall-clock timeout for the playwright run
#                               (default: 300)
#
# Exit codes:
#   0  — verdict emitted (check .pass field for actual pass/fail)
#   1  — setup failure (missing env, workdir not found, etc.)

import glob
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

SUB_EPIC_ID = os.environ.get("SUB_EPIC_ID", "")
WORKDIR = os.environ.get("WORKDIR", "")

if not SUB_EPIC_ID:
    print('{"verifier":"playwright","error":"SUB_EPIC_ID not set","pass":false}')
    sys.exit(1)

if not WORKDIR:
    print('{"verifier":"playwright","error":"WORKDIR not set","pass":false}')
    sys.exit(1)

if not os.path.isdir(WORKDIR):
    print(json.dumps({"verifier": "playwright",
                      "error": f"WORKDIR does not exist: {WORKDIR}", "pass": False},
                     separators=(",", ":"), ensure_ascii=False))
    sys.exit(1)


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Spec discovery ──────────────────────────────────────────────────────
# Spec must live at e2e/<SUB_EPIC_ID>_*.spec.ts in the worktree.

spec_file = ""
candidates = sorted(glob.glob(os.path.join(WORKDIR, "e2e", f"{SUB_EPIC_ID}_*.spec.ts")))
if candidates:
    spec_file = candidates[0]

if not spec_file:
    # No spec found: this is a legitimate BE-only skip, not a failure.
    print(json.dumps({
        "verifier": "playwright", "sub_epic_id": SUB_EPIC_ID, "pass": True, "skipped": True,
        "reason": f"no spec found at e2e/{SUB_EPIC_ID}_*.spec.ts (BE-only sub-epic or spec not yet written)",
        "scenarios_run": 0, "scenarios_passed": 0, "scenarios_failed": 0,
        "duration_ms": 0, "ran_at": _utc_now(),
    }, separators=(",", ":"), ensure_ascii=False))
    sys.exit(0)

# ─── Configuration ───────────────────────────────────────────────────────

playwright_cmd = os.environ.get("MINI_ORK_PLAYWRIGHT_CMD", "npx playwright test")
timeout_sec = float(os.environ.get("MINI_ORK_BDD_TIMEOUT_SEC", "300"))
verdict_dir = os.environ.get("MINI_ORK_VERDICT_DIR", "")
json_results_file = ""

if verdict_dir:
    os.makedirs(verdict_dir, exist_ok=True)
    json_results_file = os.path.join(verdict_dir, "playwright-results.json")

# ─── Run ─────────────────────────────────────────────────────────────────

started_ms = int(time.time()) * 1000
exit_code = 0
log_file = os.path.join(verdict_dir, "playwright-runner.log") if verdict_dir else ""

reporter_args = ["--reporter=json"] if json_results_file else []
env = dict(os.environ)
if json_results_file:
    env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = json_results_file

argv = shlex.split(playwright_cmd) + [spec_file] + reporter_args
try:
    if log_file:
        with open(log_file, "wb") as lf:
            proc = subprocess.run(argv, cwd=WORKDIR, env=env, stdout=lf,
                                  stderr=subprocess.STDOUT, timeout=timeout_sec)
    else:
        proc = subprocess.run(argv, cwd=WORKDIR, env=env, timeout=timeout_sec)
    exit_code = proc.returncode
except subprocess.TimeoutExpired:
    exit_code = 124  # coreutils `timeout` rc on expiry
except FileNotFoundError as exc:
    exit_code = 127
    sys.stderr.write(f"{exc}\n")

ended_ms = int(time.time()) * 1000
duration_ms = ended_ms - started_ms

# ─── Parse results ───────────────────────────────────────────────────────

total = 0
passed_count = 0
failed = 0
skipped_count = 0
results = None

if json_results_file and os.path.isfile(json_results_file):
    try:
        results = json.load(open(json_results_file, encoding="utf-8"))
    except Exception:
        results = None
if isinstance(results, dict):
    stats = results.get("stats") or {}
    total = (stats.get("expected", 0) + stats.get("unexpected", 0)
             + stats.get("flaky", 0) + stats.get("skipped", 0))
    passed_count = stats.get("expected", 0)
    failed = stats.get("unexpected", 0) + stats.get("flaky", 0)
    skipped_count = stats.get("skipped", 0)

# Determine pass/fail.
verdict_pass = exit_code == 0 and failed == 0

# Build failure summary array (top 5).
failure_summary = []
if results is not None and failed > 0:
    for suite in (results.get("suites") or []):
        for inner in (suite.get("suites") or []):
            for spec in (inner.get("specs") or []):
                tests = spec.get("tests") or []
                has_failure = any(
                    (r.get("status") != "passed")
                    for t in tests for r in (t.get("results") or []))
                if not has_failure:
                    continue
                error = "no error message"
                try:
                    error = tests[0]["results"][0]["error"]["message"] or error
                except (IndexError, KeyError, TypeError):
                    pass
                failure_summary.append({
                    "spec": spec.get("file"),
                    "title": spec.get("title"),
                    "error": error,
                })
    failure_summary = failure_summary[:5]

# ─── Emit verdict JSON ───────────────────────────────────────────────────

evidence_path = json_results_file or ""
if not evidence_path and log_file:
    evidence_path = log_file

verdict_json = json.dumps({
    "verifier": "playwright",
    "sub_epic_id": SUB_EPIC_ID,
    "pass": verdict_pass,
    "skipped": False,
    "spec_file": spec_file,
    "exit_code": exit_code,
    "scenarios_run": total,
    "scenarios_passed": passed_count,
    "scenarios_failed": failed,
    "scenarios_skipped": skipped_count,
    "duration_ms": duration_ms,
    "failure_summary": failure_summary,
    "evidence_path": evidence_path,
    "ran_at": _utc_now(),
}, ensure_ascii=False)

# Write to verdict dir if set.
if verdict_dir:
    with open(os.path.join(verdict_dir, "bdd-verdict.json"), "w") as f:
        f.write(verdict_json + "\n")

# Always emit to stdout (orchestrator reads this).
print(verdict_json)

# Exit 0 so the orchestrator can inspect the .pass field itself.
# The orchestrator decides whether a FAIL verdict blocks the pipeline.
sys.exit(0)
