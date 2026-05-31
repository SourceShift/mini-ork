#!/usr/bin/env bash
# agent_registry.sh — AgentVersionRecord storage and performance stats.
#
# Public API:
#   agent_register   <role> <version_payload>    → version_id
#   agent_get        <role> <version_id>          → JSON or "null"
#   agent_current    <role>                       → JSON of active version
#   agent_performance <role>                      → stats JSON
#
# Schema fields: role, model, provider, tools (json), prompt_hash,
#   task_classes (json), cost_profile, context_window, success_rate,
#   known_failure_modes (json)

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_agent_ensure_tables() {
  # v0.2-pt10 G-003 DDL session guard
  [ "${_MO_AGENT_SCHEMA_INIT:-0}" = "1" ] && return 0
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
con.executescript("""
    CREATE TABLE IF NOT EXISTS agent_registry (
        version_id           TEXT PRIMARY KEY,
        role                 TEXT NOT NULL,
        model                TEXT NOT NULL,
        provider             TEXT NOT NULL DEFAULT 'anthropic',
        tools                TEXT NOT NULL DEFAULT '[]',
        prompt_hash          TEXT,
        task_classes         TEXT NOT NULL DEFAULT '[]',
        cost_profile         TEXT DEFAULT '{}',
        context_window       INTEGER DEFAULT 200000,
        success_rate         REAL DEFAULT 0.0,
        known_failure_modes  TEXT NOT NULL DEFAULT '[]',
        status               TEXT NOT NULL DEFAULT 'active'
                                 CHECK(status IN ('active','retired','candidate')),
        registered_at        INTEGER NOT NULL
    );
""")
con.commit()
con.close()
PY
  _MO_AGENT_SCHEMA_INIT=1
  export _MO_AGENT_SCHEMA_INIT
}

# desc: Register a new agent version record for role. Returns version_id on stdout.
#       payload fields: model (required), provider, tools, prompt_hash,
#       task_classes, cost_profile, context_window, known_failure_modes.
agent_register() {
  local role="${1:?role required}"
  local payload="${2:?version_payload required}"
  _agent_ensure_tables

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$role" "$payload" <<'PY'
import sqlite3, json, sys, time, uuid

db, role = sys.argv[1], sys.argv[2]
try:
    p = json.loads(sys.argv[3])
except json.JSONDecodeError as e:
    print(f"agent_register: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

if not p.get("model"):
    print("agent_register: 'model' required in payload", file=sys.stderr)
    sys.exit(1)

vid = p.get("version_id") or f"av-{role[:6]}-{uuid.uuid4().hex[:10]}"
now = int(time.time())

tools = p.get("tools", [])
if isinstance(tools, str):
    try:
        tools = json.loads(tools)
    except Exception:
        tools = []

task_classes = p.get("task_classes", [])
if isinstance(task_classes, str):
    try:
        task_classes = json.loads(task_classes)
    except Exception:
        task_classes = []

known_failures = p.get("known_failure_modes", [])
if isinstance(known_failures, str):
    try:
        known_failures = json.loads(known_failures)
    except Exception:
        known_failures = []

cost_profile = p.get("cost_profile", {})
if isinstance(cost_profile, str):
    try:
        cost_profile = json.loads(cost_profile)
    except Exception:
        cost_profile = {}

con = sqlite3.connect(db)
# Retire previous active version for same role
con.execute(
    "UPDATE agent_registry SET status='retired' WHERE role=? AND status='active'",
    (role,)
)
con.execute("""
    INSERT INTO agent_registry (
        version_id, role, model, provider, tools, prompt_hash,
        task_classes, cost_profile, context_window, success_rate,
        known_failure_modes, status, registered_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(version_id) DO UPDATE SET
        model=excluded.model,
        provider=excluded.provider,
        tools=excluded.tools,
        prompt_hash=excluded.prompt_hash,
        task_classes=excluded.task_classes,
        cost_profile=excluded.cost_profile,
        context_window=excluded.context_window,
        known_failure_modes=excluded.known_failure_modes,
        status='active'
""", (
    vid, role, p["model"],
    p.get("provider", "anthropic"),
    json.dumps(tools),
    p.get("prompt_hash"),
    json.dumps(task_classes),
    json.dumps(cost_profile),
    int(p.get("context_window", 200000)),
    float(p.get("success_rate", 0.0)),
    json.dumps(known_failures),
    p.get("status", "active"),
    now,
))
con.commit()
con.close()
print(vid)
PY
}

# desc: Retrieve a specific agent version by role + version_id. Emits JSON or "null".
agent_get() {
  local role="${1:?role required}"
  local version_id="${2:?version_id required}"
  _agent_ensure_tables
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$role" "$version_id" <<'PY'
import sqlite3, json, sys
db, role, vid = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT * FROM agent_registry WHERE role=? AND version_id=?", (role, vid)
).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# desc: Get the currently active agent version for role. Emits JSON or "null".
agent_current() {
  local role="${1:?role required}"
  _agent_ensure_tables
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$role" <<'PY'
import sqlite3, json, sys
db, role = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT * FROM agent_registry WHERE role=? AND status='active' ORDER BY registered_at DESC LIMIT 1",
    (role,)
).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# desc: Emit performance statistics for all versions of role. Stats are derived
#       from execution_traces joined on agent_version_id.
agent_performance() {
  local role="${1:?role required}"
  _agent_ensure_tables
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$role" <<'PY'
import sqlite3, json, sys
db, role = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# All agent versions for role
versions = con.execute(
    "SELECT version_id, model, status, registered_at FROM agent_registry WHERE role=?",
    (role,)
).fetchall()

stats = []
for v in versions:
    vid = v["version_id"]
    # Attempt to join with execution_traces
    try:
        rows = con.execute("""
            SELECT
                COUNT(*) as total_runs,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successes,
                AVG(cost_usd) as avg_cost,
                AVG(duration_ms) as avg_duration_ms
            FROM execution_traces
            WHERE agent_version_id=?
        """, (vid,)).fetchone()
        total   = rows[0] or 0
        success = rows[1] or 0
        succ_rate = (success / total) if total > 0 else float(v.get("success_rate", 0.0))
    except Exception:
        total, success, succ_rate = 0, 0, 0.0
        rows = None

    stats.append({
        "version_id":    vid,
        "model":         v["model"],
        "status":        v["status"],
        "registered_at": v["registered_at"],
        "total_runs":    total,
        "success_runs":  success,
        "success_rate":  round(succ_rate, 4),
        "avg_cost_usd":  round(rows[2] or 0.0, 6) if rows else 0.0,
        "avg_duration_ms": round(rows[3] or 0.0, 1) if rows else 0.0,
    })

con.close()
print(json.dumps({
    "role": role,
    "version_count": len(stats),
    "versions": stats,
}))
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "agent_registry.sh — source me and call agent_register / agent_get / agent_current / agent_performance"
fi
