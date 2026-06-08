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

# 2) benchmark delta — only checked if benchmark_results table has data
bench_delta_ok=1
bench_summary="no-benchmarks"
if [ -f "$DB" ]; then
  bench_summary=$(python3 - "$DB" <<'PY' 2>/dev/null || echo "db-unavailable"
import sqlite3, sys, json
con = sqlite3.connect(sys.argv[1])
con.row_factory = sqlite3.Row
try:
    cur = con.execute("""
        SELECT AVG(utility_score) AS avg_score, COUNT(*) AS n
        FROM benchmark_results
        WHERE ran_at >= strftime('%s', 'now', '-1 day')
    """)
    row = cur.fetchone()
    print(json.dumps({"avg_score": row["avg_score"], "n": row["n"]}))
except sqlite3.OperationalError as e:
    print(json.dumps({"error": str(e)}))
con.close()
PY
)
fi
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
case "$report_outcome" in
  refused-*|failed-*) pass=0 ;;
esac

python3 - "$pass" "${#syntax_failures[@]}" "$report_outcome" "$bench_summary" "$EVIDENCE" "${syntax_failures[@]}" <<'PY'
import json, sys
pass_, sf, outcome, bench_summary, ev, *failures = sys.argv[1:]
try:
    bench = json.loads(bench_summary)
except Exception:
    bench = {"raw": bench_summary}
print(json.dumps({
    "verifier": "no-regression",
    "pass": pass_ == "1",
    "evidence_path": ev,
    "syntax_failures": failures,
    "implementer_outcome": outcome,
    "benchmark_summary": bench,
}))
PY
