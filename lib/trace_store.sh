#!/usr/bin/env bash
# trace_store.sh — TraceStore CRUD on execution_traces table.
#
# Public API:
#   trace_write   <json_payload>
#   trace_get     <trace_id>
#   trace_query   [--task-class X] [--status Y] [--since DATE]
#   trace_attach_artifact <trace_id> <artifact_path> <artifact_hash>
#
# Schema fields: trace_id, prompt_version, context_bundle_hash,
#   tool_calls (json), files_read (json), files_written (json),
#   verifier_output (json), reviewer_verdict, cost_usd, duration_ms,
#   final_artifact_ref, status (success|failure), workflow_version_id,
#   agent_version_id, task_class, created_at

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_trace_now() { date +%s; }

# desc: Write a new execution trace. json_payload must include task_class; all
#       other fields are optional and default to empty/null. Returns trace_id on
#       stdout.
trace_write() {
  local payload="${1:?json_payload required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$payload" <<'PY'
import sqlite3, json, sys, uuid, time, os

db = sys.argv[1]
try:
    p = json.loads(sys.argv[2])
except json.JSONDecodeError as e:
    print(f"trace_write: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

trace_id = p.get("trace_id") or f"tr-{uuid.uuid4().hex[:16]}"
now = int(time.time())
# run_id stamps the trace with its parent task_runs.id so the obs UI can
# attribute node traces (and the gradients they evidence) to a run.
# Payload wins; env fallback covers every lifecycle caller for free —
# bin/mini-ork exports MINI_ORK_RUN_ID for the whole run, execute exports
# MINI_ORK_TASK_RUN_ID. Was always NULL before — 3 separate auto-minted
# gradients flagged it.
run_id = (p.get("run_id")
          or os.environ.get("MINI_ORK_TASK_RUN_ID")
          or os.environ.get("MINI_ORK_RUN_ID"))

con = sqlite3.connect(db)
# v0.2-pt7 (F-11/R1): per-connection busy_timeout — handle SQLITE_BUSY
# under concurrent worker access instead of silent data loss.
con.execute("PRAGMA busy_timeout=5000")
# v0.2-pt11 (D-039): the inline CREATE TABLE IF NOT EXISTS was a BUG —
# its column names + types didn't match migration 0010 (prompt_version
# vs prompt_version_hash, created_at INTEGER vs TEXT) AND the INSERT
# below targeted columns that don't exist in the real schema. The CREATE
# IF NOT EXISTS no-op'd because the table already existed from 0010,
# but every INSERT silently failed (caller uses 2>/dev/null || true).
# Result: execution_traces stayed EMPTY across 10+ DF cycles.
# Migration 0010 + 0014 own the schema authoritatively now.
con.execute("""
    INSERT INTO execution_traces (
        trace_id, run_id, task_class, prompt_version_hash, context_bundle_hash,
        tool_calls, files_read, files_written, verifier_output,
        reviewer_verdict, cost_usd, duration_ms, final_artifact_ref,
        status, workflow_version_id, agent_version_id
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trace_id) DO UPDATE SET
        status=excluded.status,
        run_id=COALESCE(excluded.run_id, run_id),
        verifier_output=excluded.verifier_output,
        reviewer_verdict=excluded.reviewer_verdict,
        cost_usd=excluded.cost_usd,
        duration_ms=excluded.duration_ms,
        final_artifact_ref=excluded.final_artifact_ref
""", (
    trace_id,
    run_id,
    p.get("task_class", ""),
    p.get("prompt_version", "") or "",
    p.get("context_bundle_hash", "") or "",
    json.dumps(p.get("tool_calls", [])),
    json.dumps(p.get("files_read", [])),
    json.dumps(p.get("files_written", [])),
    json.dumps(p.get("verifier_output", {})),
    p.get("reviewer_verdict"),
    float(p.get("cost_usd", 0.0)),
    int(p.get("duration_ms", 0)),
    p.get("final_artifact_ref"),
    p.get("status", "success"),
    p.get("workflow_version_id"),
    p.get("agent_version_id", "") or "",
))
con.commit()
con.close()
print(trace_id)
PY
}

# desc: Retrieve a single execution trace by trace_id. Emits JSON on stdout or
#       "null" when not found.
trace_get() {
  local trace_id="${1:?trace_id required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$trace_id" <<'PY'
import sqlite3, json, sys
db, trace_id = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")  # v0.2-pt7 F-11
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT * FROM execution_traces WHERE trace_id=?", (trace_id,)
).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# desc: Query execution traces by optional filters. Emits JSON array on stdout.
#       Flags: --task-class X, --status Y, --since EPOCH_SECS
trace_query() {
  local task_class="" status="" since="0"
  local limit="${MO_TRACE_QUERY_LIMIT:-1000}"  # v0.2-pt7 R6/F-17
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task-class) task_class="$2"; shift 2 ;;
      --status)     status="$2";     shift 2 ;;
      --since)      since="$2";      shift 2 ;;
      --limit)      limit="$2";      shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
            "$task_class" "$status" "$since" "$limit" <<'PY'
import sqlite3, json, sys, datetime
db, task_class, status, since = sys.argv[1:5]
# v0.2-pt37 (2026-06-05): created_at is TEXT ISO ('2026-06-05T...') per
# migration 0010, not integer epoch. Lexicographic comparison of an
# integer epoch (e.g. 1780...) against ISO ('2026...') fails because
# '1' < '2'. Convert epoch → ISO before comparing.
try:
    since_int = int(since)
    since_iso = datetime.datetime.utcfromtimestamp(since_int).strftime('%Y-%m-%dT%H:%M:%S.000Z')
except (ValueError, TypeError):
    since_iso = since
clauses, params = ["created_at >= ?"], [since_iso]
if task_class:
    clauses.append("task_class = ?"); params.append(task_class)
if status:
    clauses.append("status = ?");     params.append(status)
# v0.2-pt7 (R6/F-17): cap fetchall to MO_TRACE_QUERY_LIMIT rows (default
# 1000). Unbounded fetchall on 10M-row execution_traces OOMs the
# process — was a pinned 10M/day failure in Opus §3.
limit = int(sys.argv[5]) if len(sys.argv) > 5 else 1000
sql = "SELECT * FROM execution_traces WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT ?"
params.append(limit)
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")  # v0.2-pt7 F-11
con.row_factory = sqlite3.Row
rows = con.execute(sql, params).fetchall()
con.close()
print(json.dumps([dict(r) for r in rows]))
PY
}

# desc: Attach an artifact reference (path + sha256 hash) to an existing trace.
trace_attach_artifact() {
  local trace_id="${1:?trace_id required}"
  local artifact_path="${2:?artifact_path required}"
  local artifact_hash="${3:?artifact_hash required}"
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
            "$trace_id" "$artifact_path" "$artifact_hash" <<'PY'
import sqlite3, json, sys
db, trace_id, path, ahash = sys.argv[1:5]
ref = json.dumps({"path": path, "sha256": ahash})
con = sqlite3.connect(db)
con.execute(
    "UPDATE execution_traces SET final_artifact_ref=? WHERE trace_id=?",
    (ref, trace_id)
)
if con.execute("SELECT changes()").fetchone()[0] == 0:
    print(f"trace_attach_artifact: trace_id {trace_id} not found", file=sys.stderr)
    sys.exit(1)
con.commit()
con.close()
print(f"artifact attached to {trace_id}", file=sys.stderr)
PY
}

# When invoked directly, emit usage.
if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "trace_store.sh — source me and call trace_write / trace_get / trace_query / trace_attach_artifact"
fi
