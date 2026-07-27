#!/usr/bin/env python3
# Verifier for obs-smoke: checks the researcher wrote lens-tiny.md and the
# reviewer's JSON verdict exists. Deterministic — no LLM cost.
#
# Python port of lens-exists.sh (bash-removal WS8). Identical logic (the .sh
# was a thin wrapper around an embedded Python program).
#
# Beyond existence, asserts:
#   - lens-tiny.md content SHAPE: first non-blank line is a markdown header,
#     >=4 non-blank lines, and no chat-transcript markers (<z-insight>) —
#     catches the harness overwriting the agent-written artifact with the
#     agent's reply text (gradient 0.95).
#   - review-tiny_reviewer.json is valid JSON with verdict in {pass,fail}.
#   - telemetry: this run's researcher/reviewer traces exist in
#     execution_traces with non-null run_id and evidence of work
#     (tool_calls or files_written non-empty). Skipped (not failed) when
#     MINI_ORK_DB / run id are not in scope (ad-hoc invocation).
#
# Emits JSON to stdout (consumed by mini_ork/cli/execute.py) + writes the
# canonical verifier-result-lens-exists.json sidecar to the run dir.

import json
import os
import sqlite3
import sys

RUN_DIR = os.environ.get("MINI_ORK_RUN_DIR", ".")
lens_path = os.path.join(RUN_DIR, "lens-tiny.md")
review_path = os.path.join(RUN_DIR, "review-tiny_reviewer.json")

reasons = []
checks = {}

# --- lens-tiny.md: existence + content shape ----------------------------
if not os.path.isfile(lens_path):
    reasons.append(f"lens-tiny.md missing at {lens_path}")
    checks["lens_exists"] = False
else:
    checks["lens_exists"] = True
    with open(lens_path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    if len(content) < 30:
        reasons.append("lens-tiny.md too small (<30 bytes — researcher likely no-op'd)")
    lines = [l for l in content.splitlines() if l.strip()]
    shape_ok = True
    if not lines or not lines[0].lstrip().startswith("#"):
        shape_ok = False
        reasons.append("lens-tiny.md first non-blank line is not a markdown header — "
                       "content is not the lens the researcher was asked to write")
    if len(lines) < 4:
        shape_ok = False
        reasons.append(f"lens-tiny.md has {len(lines)} non-blank lines (<4) — incomplete lens")
    if "<z-insight>" in content:
        shape_ok = False
        reasons.append("lens-tiny.md contains <z-insight> chat-transcript marker — "
                       "harness overwrote the agent-written artifact with reply text")
    checks["lens_shape"] = shape_ok

# --- review-tiny_reviewer.json: existence + valid verdict ---------------
if not os.path.isfile(review_path):
    reasons.append(f"review-tiny_reviewer.json missing at {review_path}")
    checks["review_exists"] = False
else:
    checks["review_exists"] = True
    with open(review_path, encoding="utf-8", errors="replace") as f:
        review_text = f.read()
    try:
        review = json.loads(review_text)
        checks["review_json_strict"] = True
    except (json.JSONDecodeError, ValueError):
        # Reviewers emit preamble prose around the JSON (D-011/D-016 class).
        # Tolerant fallback via lib/extract_verdict.py — strict failure here
        # cascaded a passing run into rollback (run-1781105320-64712).
        checks["review_json_strict"] = False
        review = None
        lib_dir = os.path.join(os.environ.get("MINI_ORK_ROOT", ""), "lib")
        if os.path.isdir(lib_dir):
            sys.path.insert(0, lib_dir)
            try:
                from extract_verdict import extract_review
                review = extract_review(review_text)
            except ImportError:
                pass
    if not isinstance(review, dict):
        reasons.append("review-tiny_reviewer.json contains no JSON object with a verdict")
        checks["review_verdict"] = False
    else:
        verdict = review.get("verdict")
        if verdict not in ("pass", "fail"):
            reasons.append(f"review verdict {verdict!r} not in {{pass,fail}}")
            checks["review_verdict"] = False
        else:
            checks["review_verdict"] = True

# --- telemetry: this run's LLM-node traces -------------------------------
db = os.environ.get("MINI_ORK_DB", "")
run_id = (os.environ.get("MINI_ORK_TASK_RUN_ID")
          or os.environ.get("MINI_ORK_RUN_ID") or "")
if db and os.path.isfile(db) and run_id:
    try:
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=5000")
        rows = con.execute(
            "SELECT trace_id, run_id, tool_calls, files_written "
            "FROM execution_traces WHERE run_id = ? AND "
            "(trace_id LIKE 'tr-researcher-%' OR trace_id LIKE 'tr-reviewer-%')",
            (run_id,)).fetchall()
        con.close()
        if not rows:
            reasons.append(f"telemetry: no researcher/reviewer traces for run {run_id} "
                           "in execution_traces")
            checks["telemetry_traces"] = False
        else:
            checks["telemetry_traces"] = True
            for trace_id, trow_run, tool_calls, files_written in rows:
                if not trow_run:
                    reasons.append(f"telemetry: {trace_id} has NULL run_id")
                    checks["telemetry_run_id"] = False

                def nonempty(v):
                    try:
                        x = json.loads(v) if isinstance(v, str) else v
                        if isinstance(x, str):  # double-encoded
                            x = json.loads(x)
                        return bool(x)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        return bool(v and v not in ("[]", '"[]"'))
                if not (nonempty(tool_calls) or nonempty(files_written)):
                    reasons.append(f"telemetry: {trace_id} has empty tool_calls AND "
                                   "files_written — no evidence of work recorded")
                    checks["telemetry_work_evidence"] = False
            checks.setdefault("telemetry_run_id", True)
            checks.setdefault("telemetry_work_evidence", True)
    except sqlite3.Error as e:
        reasons.append(f"telemetry: db query failed: {e}")
        checks["telemetry_traces"] = False
else:
    checks["telemetry"] = "skipped (no MINI_ORK_DB or run id in scope)"

passed = not reasons
result = json.dumps({
    "verifier": "lens-exists",
    "pass": passed,
    "reasons": reasons,
    "checks": checks,
    "lens_path": lens_path,
    "review_path": review_path,
})
print(result)
# Persist the sidecar for the obs UI's Why? panel to consume
try:
    with open(os.path.join(RUN_DIR, "verifier-result-lens-exists.json"), "w") as f:
        f.write(result + "\n")
except OSError:
    pass
# Exit reflects the verdict so the executor's verifier gate sees failures.
sys.exit(0 if passed else 1)
