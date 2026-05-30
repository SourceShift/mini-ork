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
import sqlite3, json, sys, uuid, time

db = sys.argv[1]
try:
    p = json.loads(sys.argv[2])
except json.JSONDecodeError as e:
    print(f"trace_write: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

trace_id = p.get("trace_id") or f"tr-{uuid.uuid4().hex[:16]}"
now = int(time.time())

con = sqlite3.connect(db)
con.execute("""
    CREATE TABLE IF NOT EXISTS execution_traces (
        trace_id            TEXT PRIMARY KEY,
        task_class          TEXT NOT NULL DEFAULT '',
        prompt_version      TEXT,
        context_bundle_hash TEXT,
        tool_calls          TEXT DEFAULT '[]',
        files_read          TEXT DEFAULT '[]',
        files_written       TEXT DEFAULT '[]',
        verifier_output     TEXT DEFAULT '{}',
        reviewer_verdict    TEXT,
        cost_usd            REAL DEFAULT 0.0,
        duration_ms         INTEGER DEFAULT 0,
        final_artifact_ref  TEXT,
        status              TEXT NOT NULL DEFAULT 'success'
                                CHECK(status IN ('success','failure','pending')),
        workflow_version_id TEXT,
        agent_version_id    TEXT,
        created_at          INTEGER NOT NULL
    )
""")
con.execute("""
    INSERT INTO execution_traces (
        trace_id, task_class, prompt_version, context_bundle_hash,
        tool_calls, files_read, files_written, verifier_output,
        reviewer_verdict, cost_usd, duration_ms, final_artifact_ref,
        status, workflow_version_id, agent_version_id, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trace_id) DO UPDATE SET
        status=excluded.status,
        verifier_output=excluded.verifier_output,
        reviewer_verdict=excluded.reviewer_verdict,
        cost_usd=excluded.cost_usd,
        duration_ms=excluded.duration_ms,
        final_artifact_ref=excluded.final_artifact_ref
""", (
    trace_id,
    p.get("task_class", ""),
    p.get("prompt_version"),
    p.get("context_bundle_hash"),
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
    p.get("agent_version_id"),
    now,
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
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --task-class) task_class="$2"; shift 2 ;;
      --status)     status="$2";     shift 2 ;;
      --since)      since="$2";      shift 2 ;;
      *) shift ;;
    esac
  done
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" \
            "$task_class" "$status" "$since" <<'PY'
import sqlite3, json, sys
db, task_class, status, since = sys.argv[1:5]
clauses, params = ["created_at >= ?"], [int(since)]
if task_class:
    clauses.append("task_class = ?"); params.append(task_class)
if status:
    clauses.append("status = ?");     params.append(status)
sql = "SELECT * FROM execution_traces WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC"
con = sqlite3.connect(db)
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
