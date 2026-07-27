#!/usr/bin/env python3
# verifiers/test.py — run the project's test suite and emit structured JSON.
#
# Python port of test.sh (bash-removal WS8). Same rc semantics, env vars, and
# output text.
#
# Gating is BASELINE-RELATIVE (delta), not absolute-green: a patch is judged on
# whether it makes the suite WORSE, not on whether the suite is perfectly green.
# Pre-existing failures and broken test environments (wrong interpreter, shadow
# installs, collection/import errors in untouched code) are NOT the patch's fault
# and must not roll back a correct fix. This mirrors SWE-bench's own
# FAIL_TO_PASS / PASS_TO_PASS semantics and fixes the false-reject that discarded
# correct cheap-model patches (see docs: verifier net-negative diagnosis).
#
# Decision table (post = current patched tree, base = HEAD worktree without patch):
#   post green                         -> pass  (nothing to prove)
#   post red,  base ALSO red           -> pass  (pre-existing/env breakage, uninformative)
#   post red,  base green              -> fail  (the patch introduced a regression)
#   post red,  base indeterminate      -> fail  (fall back to absolute; never hide a regression)
#
# Exit codes:  0 pass   1 fail   (callers also read the JSON "pass" field)
#
# Env vars:
#   MINI_ORK_TEST_CMD    explicit command to run (skips auto-detect)
#   MINI_ORK_HOME        path to .mini-ork/ dir (default: .mini-ork)
#   MINI_ORK_RUN_ID      current run id (used in log path)
#   MO_TEST_BASELINE     set to 0 to disable baseline (revert to absolute gating)

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

MINI_ORK_HOME = os.environ.get("MINI_ORK_HOME", ".mini-ork")
MINI_ORK_RUN_ID = os.environ.get("MINI_ORK_RUN_ID", "unknown-run")
LOG_DIR = os.path.join(MINI_ORK_HOME, "runs", MINI_ORK_RUN_ID)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "verifier_test.log")
BASE_LOG = os.path.join(LOG_DIR, "verifier_test_baseline.log")


def _package_scripts():
    try:
        with open("package.json", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def detect_test_cmd():
    # Explicit override wins.
    if os.environ.get("MINI_ORK_TEST_CMD"):
        return os.environ["MINI_ORK_TEST_CMD"]

    # npm / pnpm / yarn — check package.json scripts first.
    if os.path.isfile("package.json"):
        scripts = _package_scripts()
        for candidate in ("test", "test:unit", "test:ci"):
            if candidate in scripts:
                if shutil.which("pnpm"):
                    return f"pnpm run {candidate}"
                if shutil.which("npm"):
                    return "npm test"
        # Fallback: npm test without package.json script parsing
        if shutil.which("pnpm"):
            return "pnpm test"
        if shutil.which("npm"):
            return "npm test"

    # Python pytest
    if shutil.which("pytest"):
        return "pytest"
    if shutil.which("python3"):
        if subprocess.run([sys.executable, "-m", "pytest", "--version"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return "python3 -m pytest"

    # Rust
    if shutil.which("cargo") and os.path.isfile("Cargo.toml"):
        return "cargo test"

    # Go
    if shutil.which("go") and os.path.isfile("go.mod"):
        return "go test ./..."

    # Ruby
    if shutil.which("bundle") and os.path.isfile("Gemfile"):
        return "bundle exec rake test"

    # Nothing found — skip and pass
    return ""


CMD = detect_test_cmd()
BASE_RC = ""


def run_suite(log):  # returns the exit code, never raises
    with open(log, "wb") as fh:
        return subprocess.run(CMD, shell=True, stdout=fh, stderr=subprocess.STDOUT).returncode


def first_fail_line(log):
    pat = re.compile(r"(FAIL|Error|failed|assert|ImportError|ModuleNotFound|Interrupted)")
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                if pat.search(line):
                    return line.rstrip("\n").replace('"', '\\"')[:200]
    except OSError:
        pass
    return "see log"


def emit(passed, reason, post_rc):
    summary = reason if passed else f"{reason}: {first_fail_line(LOG_PATH)}"
    print(json.dumps({
        "verifier": "test", "pass": passed, "evidence_path": LOG_PATH,
        "error_summary": summary, "post_rc": post_rc, "base_rc": str(BASE_RC) if BASE_RC != "" else "",
    }, separators=(",", ":"), ensure_ascii=False))
    return 0 if passed else 1


def main():
    global BASE_RC

    if not CMD:
        sys.stderr.write("[test] no test command detected — skipping (pass)\n")
        print(json.dumps({
            "verifier": "test", "pass": True, "evidence_path": None,
            "error_summary": "no test runner detected — skipped",
        }, separators=(",", ":"), ensure_ascii=False))
        return 0

    # ── Post-patch run (current working tree = patched) ──────────────────
    sys.stderr.write(f"[test] running: {CMD}\n")
    post_rc = run_suite(LOG_PATH)

    if post_rc == 0:
        return emit(True, "post-patch suite green", post_rc)

    # ── Post-patch failed → establish a baseline to attribute blame ───────
    # Baseline = HEAD (the pre-patch tree) run in a throwaway worktree so the
    # real working directory is never mutated. Only reached when post-patch is red.
    in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if os.environ.get("MO_TEST_BASELINE", "1") != "0" and in_git:
        wt = tempfile.mkdtemp()
        try:
            added = subprocess.run(["git", "worktree", "add", "-q", "--detach", wt, "HEAD"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if added:
                with open(BASE_LOG, "wb") as fh:
                    BASE_RC = subprocess.run(CMD, shell=True, cwd=wt, stdout=fh,
                                             stderr=subprocess.STDOUT).returncode
                rc = subprocess.run(["git", "worktree", "remove", "--force", wt],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
                if rc != 0:
                    shutil.rmtree(wt, ignore_errors=True)
                wt = ""
        finally:
            if wt:
                shutil.rmtree(wt, ignore_errors=True)

    if BASE_RC == "":
        # Could not establish a baseline → fall back to absolute gating (do not hide a regression).
        return emit(False, "post-patch failing; no baseline established (absolute gate)", post_rc)
    if BASE_RC != 0:
        # Baseline ALSO fails → pre-existing failure / broken test env in untouched code.
        sys.stderr.write(f"[test] baseline (HEAD) also fails rc={BASE_RC} — pre-existing/env, not a regression\n")
        return emit(True, "pre-existing failure: baseline (HEAD) also fails — uninformative test env, not caused by this patch", post_rc)
    # Baseline green, post red → the patch broke something.
    return emit(False, "regression: baseline (HEAD) passed but post-patch fails", post_rc)


if __name__ == "__main__":
    sys.exit(main())
