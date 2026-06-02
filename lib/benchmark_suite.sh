#!/usr/bin/env bash
# benchmark_suite.sh — BenchmarkSuite runner for workflow candidates.
#
# Public API:
#   benchmark_add     <task_json>
#   benchmark_list    [--task-class X]
#   benchmark_run     <workflow_candidate_id>  → emits results JSON on stdout
#   benchmark_results <candidate_id>
#
# Benchmark task schema:
#   { id, task_class, input, expected_artifact_hash_or_criteria,
#     success_verifiers[], baseline_utility_score, source }

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_bench_ensure_tables() {
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_BENCH_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.executescript("""
    CREATE TABLE IF NOT EXISTS benchmark_tasks (
        id                              TEXT PRIMARY KEY,
        task_class                      TEXT NOT NULL,
        input                           TEXT NOT NULL DEFAULT '{}',
        expected_artifact_hash_or_criteria TEXT,
        success_verifiers               TEXT NOT NULL DEFAULT '[]',
        baseline_utility_score          REAL NOT NULL DEFAULT 0.0,
        source                          TEXT,
        created_at                      INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS benchmark_results (
        result_id           TEXT PRIMARY KEY,
        candidate_id        TEXT NOT NULL,
        benchmark_task_id   TEXT NOT NULL,
        run_output          TEXT,
        passed              INTEGER NOT NULL DEFAULT 0,
        utility_score       REAL NOT NULL DEFAULT 0.0,
        error_message       TEXT,
        ran_at              INTEGER NOT NULL,
        UNIQUE(candidate_id, benchmark_task_id)
    );
""")
con.commit()
con.close()
PY
  _MO_BENCH_SCHEMA_INIT=1
  export _MO_BENCH_SCHEMA_INIT
}

# desc: Add a benchmark task to the suite. payload must contain: id, task_class.
#       Returns task id on stdout.
benchmark_add() {
  local task_json="${1:?task_json required}"
  _bench_ensure_tables
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$task_json" <<'PY'
import sqlite3, json, sys, time
db = sys.argv[1]
try:
    t = json.loads(sys.argv[2])
except json.JSONDecodeError as e:
    print(f"benchmark_add: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)
# v0.2-pt35 (Phase E gap closure, 2026-06-02): real schema columns are
# benchmark_id, task_class, input_payload, expected_artifact_hash,
# expected_criteria, success_verifiers, baseline_utility_score, source,
# created_at (TEXT default-now). Previous code wrote to `id` + `input` +
# `expected_artifact_hash_or_criteria` which drifted from migration 0011.
bench_id = t.get("benchmark_id") or t.get("id")
if not bench_id or not t.get("task_class"):
    print("benchmark_add: benchmark_id (or id) + task_class required", file=sys.stderr)
    sys.exit(1)
con = sqlite3.connect(db)
con.execute("""
    INSERT INTO benchmark_tasks
        (benchmark_id, task_class, input_payload,
         expected_artifact_hash, expected_criteria,
         success_verifiers, baseline_utility_score, source)
    VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(benchmark_id) DO UPDATE SET
        task_class=excluded.task_class,
        input_payload=excluded.input_payload,
        expected_artifact_hash=excluded.expected_artifact_hash,
        expected_criteria=excluded.expected_criteria,
        success_verifiers=excluded.success_verifiers,
        baseline_utility_score=excluded.baseline_utility_score
""", (
    bench_id,
    t["task_class"],
    json.dumps(t.get("input_payload") or t.get("input") or {}),
    t.get("expected_artifact_hash") or t.get("expected_artifact_hash_or_criteria") or "",
    json.dumps(t.get("expected_criteria") or {}),
    json.dumps(t.get("success_verifiers", [])),
    float(t.get("baseline_utility_score", 0.0)),
    t.get("source") or "human",
))
con.commit()
con.close()
print(bench_id)
PY
}

# desc: List benchmark tasks, optionally filtered by task class.
#       Emits JSON array on stdout.
benchmark_list() {
  local task_class=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task-class) task_class="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  _bench_ensure_tables
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$task_class" <<'PY'
import sqlite3, json, sys
db, tc = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
if tc:
    rows = con.execute(
        "SELECT * FROM benchmark_tasks WHERE task_class=? ORDER BY benchmark_id", (tc,)
    ).fetchall()
else:
    rows = con.execute("SELECT * FROM benchmark_tasks ORDER BY task_class, benchmark_id").fetchall()
con.close()
print(json.dumps([dict(r) for r in rows]))
PY
}

# desc: Run all benchmark tasks against a workflow candidate. For each task,
#       calls MINI_ORK_WORKFLOW_RUNNER_FN (if set) or a no-op stub. Stores
#       results in benchmark_results and emits summary JSON on stdout.
benchmark_run() {
  local candidate_id="${1:?workflow_candidate_id required}"
  _bench_ensure_tables

  # shellcheck source=lib/utility_function.sh
  source "${MINI_ORK_ROOT}/lib/utility_function.sh" 2>/dev/null || true

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$candidate_id" \
    "${MINI_ORK_WORKFLOW_RUNNER_FN:-}" <<'PYEOF'
import sqlite3, json, sys, time, uuid, subprocess, os

db, cid, runner_fn = sys.argv[1], sys.argv[2], sys.argv[3]
now = int(time.time())

# v0.2-pt36 (Phase E live-dispatch fix, 2026-06-02): real schema columns
# differ from this code's previous draft. Real benchmark_results:
#   result_id (PK), benchmark_id (FK), candidate_id, run_id (NOT NULL FK),
#   pass (0/1), utility_score, evidence_path, ran_at.
# Previous code wrote benchmark_task_id / run_output / passed / error_message
# — none of which exist. Also no ON CONFLICT composite. Patched.
# Synthesize a runs.id row for the FK; mark agent='benchmark'.
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
tasks = con.execute("SELECT * FROM benchmark_tasks").fetchall()

# Create a runs row to satisfy benchmark_results.run_id NOT NULL FK.
# Bootstrap path: epics → runs → benchmark_results. The benchmark epic is
# a synthetic placeholder (one per candidate) that exists only to satisfy
# the FK chain. v0.2-pt36.
BENCHMARK_EPIC_ID = f"benchmark-{cid[:16]}"
con.execute("""
    INSERT INTO epics (id, title, status, notes)
    VALUES (?, ?, 'in progress', 'synthetic — benchmark FK bootstrap')
    ON CONFLICT(id) DO NOTHING
""", (BENCHMARK_EPIC_ID, f"Benchmark run for candidate {cid}"))
run_id = con.execute("""
    INSERT INTO runs (epic_id, run_dir, branch, baseline_sha, agent, final_verdict)
    VALUES (?, ?, ?, ?, ?, NULL)
""", (
    BENCHMARK_EPIC_ID,
    f"benchmark/{cid}/{uuid.uuid4().hex[:8]}",
    "main",
    "benchmark-synthetic",
    "mini-ork",
)).lastrowid

results = []
for task_row in tasks:
    t = dict(task_row)
    tid       = t["benchmark_id"]
    result_id = f"br-{cid[:8]}-{tid[:8]}-{uuid.uuid4().hex[:6]}"
    run_out, passed, util_score, err_msg = None, False, 0.0, None

    if runner_fn:
        # External runner: call as shell function with task JSON on stdin
        try:
            proc = subprocess.run(
                ["bash", "-c", f"source {os.environ.get('MINI_ORK_ROOT','.')}/lib/utility_function.sh 2>/dev/null; {runner_fn}"],
                input=json.dumps(t),
                capture_output=True, text=True, timeout=300,
            )
            run_out = proc.stdout.strip()
            passed = proc.returncode == 0
            # Attempt to parse utility score from output
            try:
                data = json.loads(run_out)
                util_score = float(data.get("utility_score", 0.0))
                passed = bool(data.get("passed", passed))
                run_out = json.dumps(data)
            except Exception:
                util_score = 1.0 if passed else 0.0
        except subprocess.TimeoutExpired:
            err_msg = "timeout"
            passed = False
        except Exception as e:
            err_msg = str(e)
            passed = False
    else:
        # No runner configured — mark as skipped with neutral score
        run_out = json.dumps({"skipped": True, "reason": "no MINI_ORK_WORKFLOW_RUNNER_FN set"})
        util_score = float(t.get("baseline_utility_score", 0.0) or 0.0)
        passed = False
        err_msg = "runner not configured"

    # evidence_path: where the runner output landed. For now stash run_out
    # there (it's TEXT). When a real runner writes to a file path, the
    # caller can put that path here instead.
    evidence_path = run_out or ""
    con.execute("""
        INSERT INTO benchmark_results
            (result_id, benchmark_id, candidate_id, run_id,
             pass, utility_score, evidence_path)
        VALUES (?,?,?,?,?,?,?)
    """, (result_id, tid, cid, run_id, int(bool(passed)), util_score, evidence_path))

    results.append({
        "benchmark_id": tid,
        "task_class": t["task_class"],
        "passed": passed,
        "utility_score": util_score,
        "error": err_msg,
    })

con.commit()
con.close()

passed_count  = sum(1 for r in results if r["passed"])
total         = len(results)
avg_util      = (sum(r["utility_score"] for r in results) / total) if total else 0.0

print(json.dumps({
    "candidate_id": cid,
    "ran_at": now,
    "total_tasks": total,
    "passed": passed_count,
    "failed": total - passed_count,
    "avg_utility_score": round(avg_util, 4),
    "all_pass": passed_count == total,
    "results": results,
}))
PYEOF
}

# desc: Retrieve stored benchmark results for a candidate_id. Emits JSON array.
benchmark_results() {
  local candidate_id="${1:?candidate_id required}"
  _bench_ensure_tables
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$candidate_id" <<'PY'
import sqlite3, json, sys
db, cid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT * FROM benchmark_results WHERE candidate_id=? ORDER BY ran_at DESC",
    (cid,)
).fetchall()
con.close()
print(json.dumps([dict(r) for r in rows]))
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "benchmark_suite.sh — source me and call benchmark_add / benchmark_list / benchmark_run / benchmark_results"
fi
