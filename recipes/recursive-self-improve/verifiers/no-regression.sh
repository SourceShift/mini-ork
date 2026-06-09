#!/usr/bin/env bash
# verifiers/no-regression.sh — guard that the implemented patch did
# not regress benchmark utility scores or the bash syntax check on
# any changed shell file.
#
# Inputs (via env):
#   MINI_ORK_RUN_DIR
#   MINI_ORK_SELF_IMPROVE_WORKTREE
#   MINI_ORK_DB
#
# Output: JSON. Exit 0 always.

set -uo pipefail

RUN_DIR="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required}"
WT="${MINI_ORK_SELF_IMPROVE_WORKTREE:-$MINI_ORK_ROOT}"
DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
EVIDENCE="$RUN_DIR/verifier-no-regression.log"
exec 3>"$EVIDENCE"

cd "$WT" || true

# 1) bash -n on every changed shell file
syntax_failures=()
mapfile -t CHANGED < <(git -C "$WT" diff --name-only HEAD -- '*.sh' 'bin/*' 2>/dev/null || true)
for f in "${CHANGED[@]}"; do
  [ -f "$WT/$f" ] || continue
  case "$f" in
    *.sh|bin/*)
      if ! bash -n "$WT/$f" 2>>"$EVIDENCE"; then
        syntax_failures+=("$f")
      fi
      ;;
  esac
done
echo "changed_sh_files=${#CHANGED[@]} syntax_failures=${#syntax_failures[@]}" >&3

# 2) benchmark delta — fail only when enough benchmark evidence regresses.
BENCH_UTILITY_THRESHOLD="${MINI_ORK_BENCH_UTILITY_THRESHOLD:-0.5}"
BENCH_MIN_N="${MINI_ORK_BENCH_MIN_N:-3}"
bench_delta_ok=2
bench_summary="no-benchmarks"
if [ -f "$DB" ]; then
  bench_summary=$(python3 - "$DB" "${MINI_ORK_RUN_ID:-}" "$BENCH_UTILITY_THRESHOLD" "$BENCH_MIN_N" <<'PY' 2>/dev/null || echo "db-unavailable"
import sqlite3, sys, json
db, run_id, threshold_s, min_n_s = sys.argv[1:5]
threshold = float(threshold_s)
min_n = int(min_n_s)
con = sqlite3.connect(db)
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
    cur = con.execute("""
        SELECT AVG(utility_score) AS avg_score, COUNT(*) AS n
        FROM benchmark_results
        {where}
    """.format(where=where), params)
    row = cur.fetchone()
    avg_score = row["avg_score"]
    n = int(row["n"] or 0)
    regression = bool(n >= min_n and avg_score is not None and float(avg_score) < threshold)
    inconclusive = bool(n < min_n)
    print(json.dumps({
        "avg_score": avg_score,
        "n": n,
        "threshold": threshold,
        "min_n": min_n,
        "benchmark_regression": regression,
        "benchmark_inconclusive": inconclusive,
    }))
except sqlite3.OperationalError as e:
    print(json.dumps({
        "error": str(e),
        "threshold": threshold,
        "min_n": min_n,
        "benchmark_regression": False,
        "benchmark_inconclusive": True,
    }))
con.close()
PY
)
fi
bench_delta_ok=$(python3 - "$bench_summary" <<'PY' 2>/dev/null || echo 2
import json, sys
try:
    bench = json.loads(sys.argv[1])
except Exception:
    print(2)
else:
    if bench.get("benchmark_regression"):
        print(0)
    elif bench.get("benchmark_inconclusive", True):
        print(2)
    else:
        print(1)
PY
)
echo "bench_summary=$bench_summary" >&3

# Implementer-report gate: if the report says "refused-*" or "failed-*",
# treat as regression (the runner should also have caught it, but be safe).
report="$RUN_DIR/implementer-report.md"
report_outcome="unknown"
if [ -f "$report" ]; then
  report_outcome=$(grep -E '^- *success|^- *refused-|^- *failed-' "$report" \
                   | head -1 | sed -E 's/^- *//' || true)
fi
echo "report_outcome=$report_outcome" >&3

pass=1
[ "${#syntax_failures[@]}" -gt 0 ] && pass=0
[ "$bench_delta_ok" = "0" ] && pass=0
case "$report_outcome" in
  refused-*|failed-*) pass=0 ;;
esac

python3 - "$pass" "${#syntax_failures[@]}" "$report_outcome" "$bench_summary" "$bench_delta_ok" "$EVIDENCE" "${syntax_failures[@]}" <<'PY'
import json, sys
pass_, sf, outcome, bench_summary, bench_delta_ok, ev, *failures = sys.argv[1:]
try:
    bench = json.loads(bench_summary)
except Exception:
    bench = {"raw": bench_summary}
benchmark_regression = bench_delta_ok == "0"
benchmark_inconclusive = bench_delta_ok == "2"
print(json.dumps({
    "verifier": "no-regression",
    "pass": pass_ == "1",
    "evidence_path": ev,
    "syntax_failures": failures,
    "implementer_outcome": outcome,
    "benchmark_summary": bench,
    "benchmark_regression": benchmark_regression,
    "benchmark_inconclusive": benchmark_inconclusive,
}))
PY
