#!/usr/bin/env python3
# Regression coverage for recursive-self-improve no-regression benchmark gating.
# Python port of test_no_regression_bench.sh (bash-removal WS8) — runs the
# ported no-regression.py verifier against seeded benchmark_results fixtures.

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
VERIFIER = os.path.join(ROOT, "recipes", "recursive-self-improve", "verifiers", "no-regression.py")

PASS = 0
FAIL = 0


def _ok(msg):
    global PASS
    print(f"  [OK]   {msg}")
    PASS += 1


def _fail(msg):
    global FAIL
    print(f"  [FAIL] {msg}")
    FAIL += 1


TMPDIR = tempfile.mkdtemp()
RUN_DIR = os.path.join(TMPDIR, "run")
DB = os.path.join(TMPDIR, "state.db")
os.makedirs(RUN_DIR, exist_ok=True)

ENV = {
    **os.environ,
    "MINI_ORK_ROOT": ROOT,
    "MINI_ORK_SELF_IMPROVE_WORKTREE": ROOT,
    "MINI_ORK_RUN_DIR": RUN_DIR,
    "MINI_ORK_DB": DB,
    "MINI_ORK_RUN_ID": "1",
}


def seed_scores(*scores):
    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS benchmark_results")
    con.execute("CREATE TABLE benchmark_results (result_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, "
                "utility_score REAL NOT NULL DEFAULT 0.0, ran_at TEXT NOT NULL)")
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    for idx, score in enumerate(scores):
        con.execute("INSERT INTO benchmark_results (result_id, run_id, utility_score, ran_at) "
                    "VALUES (?,?,?,?)", (f"br-{idx}", 1, float(score), now))
    con.commit()
    con.close()


def read_flag(out, key):
    value = json.loads(out).get(key)
    return "true" if value is True else "false" if value is False else value


def assert_flags(label, want_pass, want_regression, want_inconclusive):
    out = subprocess.run([sys.executable, VERIFIER], env=ENV,
                         stdout=subprocess.PIPE).stdout.decode()
    passed = read_flag(out, "pass")
    regression = read_flag(out, "benchmark_regression")
    inconclusive = read_flag(out, "benchmark_inconclusive")
    if passed == want_pass and regression == want_regression and inconclusive == want_inconclusive:
        _ok(label)
    else:
        _fail(f"{label}: expected pass={want_pass} benchmark_regression={want_regression} "
              f"benchmark_inconclusive={want_inconclusive}, got pass={passed} "
              f"benchmark_regression={regression} benchmark_inconclusive={inconclusive}")


try:
    print("── verifier: no-regression benchmark gate ──")

    seed_scores(0.1, 0.1, 0.1, 0.1, 0.1)
    assert_flags("benchmark regression must be caught", "false", "true", "false")

    seed_scores(0.1, 0.1)
    assert_flags("low-n benchmark must be inconclusive not failing", "true", "false", "true")

    seed_scores(0.9, 0.9, 0.9, 0.9, 0.9)
    assert_flags("healthy benchmark must pass cleanly", "true", "false", "false")

    print("")
    print(f"── Results: {PASS} OK  {FAIL} FAIL ──")
finally:
    shutil.rmtree(TMPDIR, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)
