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

# 1) syntax-check every changed file — routed by type/shebang because the
# bash runtime is gone: bin/mini-ork is Python, so feeding it to `bash -n`
# false-fails, and a Python syntax error in a changed .py file used to pass
# unseen. `*.sh` keeps the original bash -n arm; `bin/*` is routed by its
# shebang (python -> compile, shell -> bash -n).

_PY_SHEBANG = re.compile(r"^#!\s*(?:/\S*/)?(?:env\s+)?python[0-9.]*\b")
_SHELL_SHEBANG = re.compile(r"^#!\s*(?:/\S*/)?(?:env\s+)?(?:sh|bash|zsh|dash)\b")


def _first_line(path):
    try:
        with open(path, "rb") as fh:
            return fh.readline().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _is_python_shebang(path):
    return bool(_PY_SHEBANG.match(_first_line(path)))


def _is_shell_shebang(path):
    return bool(_SHELL_SHEBANG.match(_first_line(path)))


syntax_failures = []
r = subprocess.run(["git", "-C", WT, "diff", "--name-only", "HEAD", "--", "*.sh", "bin/*"],
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
CHANGED = [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l]
for f in CHANGED:
    path = os.path.join(WT, f)
    if not os.path.isfile(path):
        continue
    if f.endswith(".sh") or (f.startswith("bin/") and _is_shell_shebang(path)):
        if subprocess.run(["bash", "-n", path],
                          stdout=ev, stderr=subprocess.STDOUT).returncode != 0:
            syntax_failures.append(f)
ev.write(f"changed_sh_files={len(CHANGED)} syntax_failures={len(syntax_failures)}\n")

# Python arm: union (de-duplicated, diff-then-untracked order) of tracked
# modifications and new untracked files, plus python-shebang bin/ files from
# the shell-arm list. compile() catches SyntaxError and ValueError without
# writing a .pyc.
py_files = []
_seen_py = set()


def _add_py(rel):
    if rel and rel not in _seen_py:
        _seen_py.add(rel)
        py_files.append(rel)


r = subprocess.run(["git", "-C", WT, "diff", "--name-only", "HEAD", "--", "*.py"],
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
for f in r.stdout.decode("utf-8", "replace").splitlines():
    _add_py(f)
r = subprocess.run(["git", "-C", WT, "ls-files", "--others", "--exclude-standard", "--", "*.py"],
                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
for f in r.stdout.decode("utf-8", "replace").splitlines():
    _add_py(f)
for f in CHANGED:
    if f.startswith("bin/") and _is_python_shebang(os.path.join(WT, f)):
        _add_py(f)

py_syntax_failures = []
for f in py_files:
    path = os.path.join(WT, f)
    if not os.path.isfile(path):
        continue
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        compile(source, f, "exec")
    except (SyntaxError, ValueError):
        py_syntax_failures.append(f)
ev.write(f"changed_py_files={len(py_files)} py_syntax_failures={len(py_syntax_failures)}\n")
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
if py_syntax_failures:
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
result = {
    "verifier": "no-regression",
    "pass": passed == 1,
    "evidence_path": EVIDENCE,
    "syntax_failures": syntax_failures,
    "py_syntax_failures": py_syntax_failures,
    "implementer_outcome": report_outcome,
    "benchmark_summary": bench_obj,
    "benchmark_regression": benchmark_regression,
    "benchmark_inconclusive": benchmark_inconclusive,
}
print(json.dumps(result))
# Canonical sidecar the outer runner reads (self_improve.read_verifier looks for
# verifier-result-<name>.json). Dropped in the bash→Python port; without it the
# runner never sees a regression pass, so the triple was structurally dead.
try:
    with open(os.path.join(RUN_DIR, "verifier-result-no-regression.json"), "w") as _sc:
        json.dump(result, _sc)
except OSError:
    pass
