#!/usr/bin/env python3
# recipes/framework-edit/verifiers/_verdict_merge.py
#
# Python port of _verdict_merge.sh. Shared helper imported by static-check.py
# and test.py to maintain $RUN_DIR/verdict.json with the schema declared in
# artifact_contract.yaml:
#
#   {
#     "files_changed": <int>,
#     "tests_pass":    <bool>,
#     "static_pass":   <bool>,
#     "pass":          <bool>     # tests_pass && static_pass (missing -> false)
#   }
#
# Usage (from a verifier):
#
#   from _verdict_merge import write_verdict
#   write_verdict(static_pass="<bool>", files_changed="<int>")   # static-check
#   write_verdict(tests_pass="<bool>")                           # test.py
#
# The helper:
#   * tolerates either verifier running first (reads existing or {}),
#   * merges only the keys it's been told about (never wipes peer keys),
#   * writes atomically via mkstemp + os.replace (POSIX-atomic on same FS),
#   * recomputes pass = bool(tests_pass) && bool(static_pass) with missing
#     keys defaulted to false (defensive default keeps schema stable).

import json
import os
import tempfile


def _merge_bool(cur, name, raw):
    if raw == "" or raw is None:
        # Caller didn't pass this key. If the existing verdict.json already
        # has it (peer verifier wrote earlier), keep that value; otherwise
        # default to false (defensive — schema contract guarantees all four
        # keys present).
        cur.setdefault(name, False)
        return
    if isinstance(raw, str):
        cur[name] = raw.strip().lower() in ("1", "true", "yes", "y", "t")
    else:
        cur[name] = bool(raw)


def _merge_int(cur, name, raw):
    if raw == "" or raw is None:
        cur.setdefault(name, 0)
        return
    try:
        cur[name] = int(raw)
    except (TypeError, ValueError):
        cur.setdefault(name, 0)


def write_verdict(static_pass="", files_changed="", tests_pass=""):
    run_dir = os.environ["MINI_ORK_RUN_DIR"]
    verdict_path = os.path.join(run_dir, "verdict.json")

    # Pre-create with empty object so the merge always has input.
    if not os.path.isfile(verdict_path):
        with open(verdict_path, "w") as f:
            f.write("{}")

    try:
        with open(verdict_path) as f:
            cur = json.load(f)
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}

    _merge_bool(cur, "static_pass", static_pass)
    _merge_int(cur, "files_changed", files_changed)
    _merge_bool(cur, "tests_pass", tests_pass)

    # Recompute pass — both required keys are now guaranteed present.
    cur["pass"] = bool(cur["static_pass"]) and bool(cur["tests_pass"])

    # Enforce schema: files_changed:int, others:bool.
    cur["files_changed"] = int(cur["files_changed"])
    cur["static_pass"] = bool(cur["static_pass"])
    cur["tests_pass"] = bool(cur["tests_pass"])
    cur["pass"] = bool(cur["pass"])

    fd, tmp = tempfile.mkstemp(prefix=".verdict.json.", dir=run_dir)
    with os.fdopen(fd, "w") as f:
        json.dump(cur, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, verdict_path)
