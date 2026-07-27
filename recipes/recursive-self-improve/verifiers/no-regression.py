#!/usr/bin/env python3
# verifiers/no-regression.py — guard that the implemented patch did
# not regress benchmark utility scores or the bash syntax check on
# any changed shell file.
#
# Python port of no-regression.sh (bash-removal WS8). Same rc semantics,
# evidence text, and JSON output.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR
#   MINI_ORK_SELF_IMPROVE_WORKTREE
#   MINI_ORK_DB
#
# Output: JSON. Exit 0 always.

import json
import os
import re
import sqlite3
import subprocess

RUN_DIR = os.environ["MINI_ORK_RUN_DIR"]
WT = os.environ.get("MINI_ORK_SELF_IMPROVE_WORKTREE") or os.environ.get("MINI_ORK_ROOT", "")
DB = os.environ.get("MINI_ORK_DB") or os.path.join(os.environ.get("MINI_ORK_HOME", ""), "state.db")
EVIDENCE = os.path.join(RUN_DIR, "verifier-no-regression.log")
ev = open(EVIDENCE, "w")

try:
    os.chdir(WT)
except OSError:
    pass

# 1) bash -n on every changed shell file
syntax_failures = []
r = subprocess.run(["git", "-C", WT, "diff", "--name-only", "HEAD", "--", "*.sh", "bin/*"],
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
CHANGED = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l]
for f in CHANGED:
    if not os.path.isfile(os.path.join(WT, f)):
        continue
    if f.endswith(".sh") or f.startswith("bin/"):
        if subprocess.run(["bash", "-n", os.path.join(WT, f)],
                          stdout=ev, stderr=subprocess.STDOUT).returncode != 0:
            syntax_failures.append(f)
ev.write(f"changed_sh_files={len(CHANGED)} syntax_failures={len(syntax_failures)}\n")
ev.flush()

# 2) benchmark delta — fail only when enough benchmark evidence regresses.
BENCH_UTILITY_THRESHOLD = float(os.environ.get("MINI_ORK_BENCH_UTILITY_THRESHOLD", "0.5"))
BENCH_MIN_N = int(os.environ.get("MINI_ORK_BENCH_MIN_N", "3"))
bench_summary = "no-benchmarks"
if os.path.isfile(DB):
    run_id = os.environ.get("MINI_ORK_RUN_ID", "")
    try:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        try:
            where = "WHERE ran_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')"
            params = []
            if run_id:
                scoped = con.execute(
                    "SELECT AVG(utility_score) AS avg_score, COUNT(*) AS n "
                    "FROM benchmark_results WHERE CAST(run_id AS TEXT)=?",
                    (run_id,),
                ).fetchone()
                if scoped and int(scoped["n"] or 0) > 0:
                    where = "WHERE CAST(run_id AS TEXT)=?"
                    params = [run_id]
            cur = con.execute(
                "SELECT AVG(utility_score) AS avg_score, COUNT(*) AS n "
                f"FROM benchmark_results {where}", params)
            row = cur.fetchone()
            avg_score = row["avg_score"]
            n = int(row["n"] or 0)
            regression = bool(n >= BENCH_MIN_N and avg_score is not None
                              and float(avg_score) < BENCH_UTILITY_THRESHOLD)
            inconclusive = bool(n < BENCH_MIN_N)
            bench_summary = json.dumps({
                "avg_score": avg_score,
                "n": n,
                "threshold": BENCH_UTILITY_THRESHOLD,
                "min_n": BENCH_MIN_N,
                "benchmark_regression": regression,
                "benchmark_inconclusive": inconclusive,
            })
        except sqlite3.OperationalError as e:
            bench_summary = json.dumps({
                "error": str(e),
                "threshold": BENCH_UTILITY_THRESHOLD,
                "min_n": BENCH_MIN_N,
                "benchmark_regression": False,
                "benchmark_inconclusive": True,
            })
        con.close()
    except sqlite3.Error:
        bench_summary = "db-unavailable"

try:
    bench = json.loads(bench_summary)
except Exception:
    bench = None
if bench is None:
    bench_delta_ok = 2
elif bench.get("benchmark_regression"):
    bench_delta_ok = 0
elif bench.get("benchmark_inconclusive", True):
    bench_delta_ok = 2
else:
    bench_delta_ok = 1
ev.write(f"bench_summary={bench_summary}\n")

# Implementer-report gate: if the report says "refused-*" or "failed-*",
# treat as regression (the runner should also have caught it, but be safe).
report = os.path.join(RUN_DIR, "implementer-report.md")
report_outcome = "unknown"
if os.path.isfile(report):
    for line in open(report, encoding="utf-8", errors="replace"):
        m = re.match(r"^- *(success|refused-|failed-)", line)
        if m:
            report_outcome = re.sub(r"^- *", "", line.rstrip("\n"))
            break
ev.write(f"report_outcome={report_outcome}\n")
ev.flush()

passed = 1
if syntax_failures:
    passed = 0
if bench_delta_ok == 0:
    passed = 0
if report_outcome.startswith(("refused-", "failed-")):
    passed = 0

try:
    bench_obj = json.loads(bench_summary)
except Exception:
    bench_obj = {"raw": bench_summary}
benchmark_regression = bench_delta_ok == 0
benchmark_inconclusive = bench_delta_ok == 2

ev.close()
print(json.dumps({
    "verifier": "no-regression",
    "pass": passed == 1,
    "evidence_path": EVIDENCE,
    "syntax_failures": syntax_failures,
    "implementer_outcome": report_outcome,
    "benchmark_summary": bench_obj,
    "benchmark_regression": benchmark_regression,
    "benchmark_inconclusive": benchmark_inconclusive,
}))
