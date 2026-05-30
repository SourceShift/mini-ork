#!/usr/bin/env bash
# reflection_pipeline.sh — Background reflection orchestrator.
#
# Each step is independently callable so users can override any one.
#
# Public API:
#   reflection_extract_gradients  <since_ts>
#   reflection_deduplicate        <gradients_table>
#   reflection_link_failures      <failure_table>
#   reflection_detect_stale       <memory_table>
#   reflection_summarize_patterns <cluster_id>
#   reflection_suggest_promotions <patterns_table>
#   reflection_run                <since_ts>   ← orchestrates all 6

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_rfl_now() { date +%s; }

# desc: Extract gradients from all execution_traces created after since_ts
#       (unix epoch). Emits gradient JSON objects on stdout via gradient_extractor.
reflection_extract_gradients() {
  local since_ts="${1:-0}"
  # shellcheck source=lib/gradient_extractor.sh
  source "${MINI_ORK_ROOT}/lib/gradient_extractor.sh" 2>/dev/null || true
  # shellcheck source=lib/trace_store.sh
  source "${MINI_ORK_ROOT}/lib/trace_store.sh" 2>/dev/null || true

  local trace_ids
  trace_ids="$(python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$since_ts" <<'PY'
import sqlite3, json, sys
con = sqlite3.connect(sys.argv[1])
rows = con.execute(
    "SELECT trace_id FROM execution_traces WHERE created_at >= ? ORDER BY created_at",
    (int(sys.argv[2]),)
).fetchall()
con.close()
for r in rows:
    print(r[0])
PY
)"

  local extracted=0
  while IFS= read -r tid; do
    [[ -z "$tid" ]] && continue
    while IFS= read -r gradient; do
      [[ -z "$gradient" ]] && continue
      gradient_store "$gradient" >/dev/null 2>&1 || true
      echo "$gradient"
      (( extracted++ )) || true
    done < <(gradient_extract "$tid" 2>/dev/null || true)
  done <<< "$trace_ids"

  echo "reflection_extract_gradients: extracted ${extracted} gradients since ${since_ts}" >&2
}

# desc: Deduplicate gradient_records table (merge identical target+signal pairs,
#       keeping highest confidence). gradients_table defaults to "gradient_records".
reflection_deduplicate() {
  local gradients_table="${1:-gradient_records}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$gradients_table" <<'PY'
import sqlite3, json, sys
db, tbl = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
# Find duplicate (target, signal) groups — keep highest confidence row.
rows = con.execute(f"""
    SELECT gradient_id, target, signal, suggested_change, confidence, evidence
    FROM {tbl}
    ORDER BY target, signal, confidence DESC
""").fetchall()

seen = {}
to_delete = []
for gid, tgt, sig, _, conf, _ in rows:
    key = (tgt, sig)
    if key in seen:
        to_delete.append(gid)   # lower-confidence duplicate
    else:
        seen[key] = gid

if to_delete:
    placeholders = ",".join("?" * len(to_delete))
    con.execute(f"DELETE FROM {tbl} WHERE gradient_id IN ({placeholders})", to_delete)
    con.commit()
    print(f"reflection_deduplicate: removed {len(to_delete)} duplicates", file=sys.stderr)
else:
    print("reflection_deduplicate: no duplicates found", file=sys.stderr)
con.close()
PY
}

# desc: Correlate failure-status traces with gradient targets; update a
#       failure_links table. failure_table defaults to "execution_traces".
reflection_link_failures() {
  local failure_table="${1:-execution_traces}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$failure_table" <<'PY'
import sqlite3, json, sys, time
db, ftbl = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("""
    CREATE TABLE IF NOT EXISTS failure_links (
        link_id      TEXT PRIMARY KEY,
        trace_id     TEXT NOT NULL,
        gradient_id  TEXT,
        task_class   TEXT,
        linked_at    INTEGER NOT NULL
    )
""")
now = int(time.time())
failures = con.execute(
    f"SELECT trace_id, task_class FROM {ftbl} WHERE status='failure'"
).fetchall()
inserted = 0
for tid, tc in failures:
    gradients = con.execute(
        "SELECT gradient_id FROM gradient_records WHERE evidence=?", (tid,)
    ).fetchall()
    for (gid,) in gradients:
        link_id = f"fl-{tid[:8]}-{gid[:8]}"
        con.execute("""
            INSERT OR IGNORE INTO failure_links (link_id, trace_id, gradient_id, task_class, linked_at)
            VALUES (?,?,?,?,?)
        """, (link_id, tid, gid, tc, now))
        inserted += 1
con.commit()
con.close()
print(f"reflection_link_failures: {inserted} links created/verified", file=sys.stderr)
PY
}

# desc: Detect stale memory entries (not updated within MINI_ORK_STALE_DAYS,
#       default 14). memory_table is the table name to inspect.
reflection_detect_stale() {
  local memory_table="${1:?memory_table required}"
  local stale_days="${MINI_ORK_STALE_DAYS:-14}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$memory_table" "$stale_days" <<'PY'
import sqlite3, json, sys, time
db, tbl, days = sys.argv[1], sys.argv[2], int(sys.argv[3])
cutoff = int(time.time()) - days * 86400
con = sqlite3.connect(db)
# Attempt to find a timestamp column — try common names in order.
cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
ts_col = next((c for c in ["updated_at","created_at","reflection_last_check","last_seen"] if c in cols), None)
if ts_col is None:
    print(f"reflection_detect_stale: no timestamp column found in {tbl}", file=sys.stderr)
    con.close()
    sys.exit(0)

# Find a primary-key-like column
pk_col = next((c for c in ["id","gradient_id","pattern_id","trace_id","adr_id"] if c in cols), cols[0])
rows = con.execute(f"SELECT {pk_col} FROM {tbl} WHERE {ts_col} < ?", (cutoff,)).fetchall()
con.close()

stale = [r[0] for r in rows]
print(json.dumps({"table": tbl, "stale_ids": stale, "stale_before_epoch": cutoff}))
print(f"reflection_detect_stale: {len(stale)} stale entries in {tbl}", file=sys.stderr)
PY
}

# desc: Summarize all pattern_records belonging to a cluster and emit a
#       consolidated summary JSON on stdout.
reflection_summarize_patterns() {
  local cluster_id="${1:?cluster_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$cluster_id" <<'PY'
import sqlite3, json, sys
db, cid = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
rows = con.execute("""
    SELECT pattern_id, description, frequency, output_type, first_seen, last_seen
    FROM pattern_records
    WHERE cluster_id = ?
    ORDER BY frequency DESC
""", (cid,)).fetchall() if cid else []
con.close()
summary = {
    "cluster_id": cid,
    "pattern_count": len(rows),
    "patterns": [
        {"pattern_id": r[0], "description": r[1], "frequency": r[2],
         "output_type": r[3], "first_seen": r[4], "last_seen": r[5]}
        for r in rows
    ],
    "dominant_output_type": rows[0][3] if rows else None,
    "total_frequency": sum(r[2] for r in rows),
}
print(json.dumps(summary))
PY
}

# desc: Suggest promotion candidates from patterns_table where frequency >= threshold.
#       Emits JSON array of promotion suggestions on stdout.
reflection_suggest_promotions() {
  local patterns_table="${1:-pattern_records}"
  local min_freq="${MINI_ORK_PROMOTION_MIN_FREQ:-3}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$patterns_table" "$min_freq" <<'PY'
import sqlite3, json, sys
db, tbl, mf = sys.argv[1], sys.argv[2], int(sys.argv[3])
con = sqlite3.connect(db)
rows = con.execute(f"""
    SELECT pattern_id, description, frequency, output_type, evidence_trace_ids
    FROM {tbl}
    WHERE frequency >= ?
    ORDER BY frequency DESC
""", (mf,)).fetchall()
con.close()
suggestions = []
for r in rows:
    suggestions.append({
        "pattern_id": r[0],
        "description": r[1],
        "frequency": r[2],
        "suggested_promotion_type": r[3],
        "evidence_trace_ids": json.loads(r[4]) if r[4] else [],
        "rationale": f"Pattern observed {r[2]} times — meets promotion threshold of {mf}",
    })
print(json.dumps(suggestions))
PY
}

# desc: Orchestrate all 6 reflection steps sequentially. since_ts is unix epoch;
#       defaults to 24 hours ago.
reflection_run() {
  local since_ts="${1:-$(( $(_rfl_now) - 86400 ))}"
  echo "reflection_run: starting pipeline since=${since_ts}" >&2

  echo "  [1/6] extract_gradients" >&2
  reflection_extract_gradients "$since_ts" >/dev/null

  echo "  [2/6] deduplicate" >&2
  reflection_deduplicate "gradient_records"

  echo "  [3/6] link_failures" >&2
  reflection_link_failures "execution_traces"

  echo "  [4/6] detect_stale(gradient_records)" >&2
  reflection_detect_stale "gradient_records" >/dev/null

  echo "  [5/6] summarize_patterns (all clusters)" >&2
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY' | while IFS= read -r cid; do
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
rows = con.execute("SELECT DISTINCT cluster_id FROM pattern_records WHERE cluster_id IS NOT NULL").fetchall() 2>/dev/null or []
con.close()
for r in rows:
    print(r[0])
PY
    reflection_summarize_patterns "$cid" >/dev/null
  done || true

  echo "  [6/6] suggest_promotions" >&2
  local suggestions
  suggestions="$(reflection_suggest_promotions "pattern_records" 2>/dev/null || echo '[]')"
  local count
  count="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$suggestions" 2>/dev/null || echo 0)"
  echo "reflection_run: ${count} promotion suggestions generated" >&2
  echo "$suggestions"
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "reflection_pipeline.sh — source me and call reflection_run / individual step functions"
fi
