#!/usr/bin/env bash
# version_registry.sh — VersionRegistry + rollback for workflows and agents.
#
# Public API:
#   version_register      <kind> <version_payload>   → version_id
#   version_get           <kind> <version_id>         → JSON or "null"
#   version_current       <kind> <name>               → JSON of current stable
#   version_rollback      <kind> <name>               → previous_stable JSON
#   version_quarantine    <kind> <version_id> <reason>
#   version_can_promote   <kind> <version_id>         → true|false
#   version_clear_quarantine <version_id> <approver>

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_ver_ensure_table() {
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""
    CREATE TABLE IF NOT EXISTS version_registry (
        version_id               TEXT PRIMARY KEY,
        kind                     TEXT NOT NULL CHECK(kind IN ('workflow','agent')),
        name                     TEXT NOT NULL,
        status                   TEXT NOT NULL DEFAULT 'candidate'
                                     CHECK(status IN ('candidate','stable','quarantined','retired')),
        payload                  TEXT NOT NULL DEFAULT '{}',
        previous_stable_version  TEXT,
        quarantine_reason        TEXT,
        quarantine_cleared_by    TEXT,
        utility_score            REAL DEFAULT 0.0,
        promoted_at              INTEGER,
        quarantined_at           INTEGER,
        created_at               INTEGER NOT NULL
    )
""")
con.commit()
con.close()
PY
}

# desc: Register a new version record. payload must include: name, version string.
#       kind must be "workflow" or "agent". Returns version_id on stdout.
version_register() {
  local kind="${1:?kind required (workflow|agent)}"
  local payload="${2:?version_payload required}"
  _ver_ensure_table

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$kind" "$payload" <<'PY'
import sqlite3, json, sys, time, uuid
db, kind = sys.argv[1], sys.argv[2]
try:
    p = json.loads(sys.argv[3])
except json.JSONDecodeError as e:
    print(f"version_register: invalid JSON: {e}", file=sys.stderr)
    sys.exit(1)

name = p.get("name", "")
if not name:
    print("version_register: payload must include 'name'", file=sys.stderr)
    sys.exit(1)

vid  = p.get("version_id") or f"v-{kind[:3]}-{uuid.uuid4().hex[:12]}"
now  = int(time.time())

con = sqlite3.connect(db)
# Find current stable to record as previous
prev = con.execute(
    "SELECT version_id FROM version_registry WHERE kind=? AND name=? AND status='stable' ORDER BY promoted_at DESC LIMIT 1",
    (kind, name)
).fetchone()
prev_vid = prev[0] if prev else None

con.execute("""
    INSERT INTO version_registry
        (version_id, kind, name, status, payload, previous_stable_version,
         utility_score, created_at)
    VALUES (?,?,?,?,?,?,?,?)
    ON CONFLICT(version_id) DO UPDATE SET
        payload=excluded.payload,
        status=CASE WHEN status='quarantined' THEN 'quarantined' ELSE excluded.status END
""", (
    vid, kind, name, p.get("status", "candidate"),
    json.dumps(p), prev_vid,
    float(p.get("utility_score", 0.0)), now,
))
con.commit()
con.close()
print(vid)
PY
}

# desc: Retrieve a version record by version_id. Emits JSON or "null".
version_get() {
  local kind="${1:?kind required}"
  local version_id="${2:?version_id required}"
  _ver_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$kind" "$version_id" <<'PY'
import sqlite3, json, sys
db, kind, vid = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT * FROM version_registry WHERE kind=? AND version_id=?", (kind, vid)
).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# desc: Get the current stable version for a named workflow/agent. Emits JSON.
version_current() {
  local kind="${1:?kind required}"
  local name="${2:?name required}"
  _ver_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$kind" "$name" <<'PY'
import sqlite3, json, sys
db, kind, name = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute("""
    SELECT * FROM version_registry
    WHERE kind=? AND name=? AND status='stable'
    ORDER BY promoted_at DESC LIMIT 1
""", (kind, name)).fetchone()
con.close()
print(json.dumps(dict(row)) if row else "null")
PY
}

# desc: Roll back a named workflow/agent to its previous stable version.
#       Retires the current stable and promotes the previous one.
#       Emits the now-current version JSON on stdout.
version_rollback() {
  local kind="${1:?kind required}"
  local name="${2:?name required}"
  _ver_ensure_table

  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$kind" "$name" <<'PY'
import sqlite3, json, sys, time
db, kind, name = sys.argv[1], sys.argv[2], sys.argv[3]
now = int(time.time())
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

current = con.execute("""
    SELECT * FROM version_registry
    WHERE kind=? AND name=? AND status='stable'
    ORDER BY promoted_at DESC LIMIT 1
""", (kind, name)).fetchone()

if not current:
    print(f"version_rollback: no stable version found for {kind}/{name}", file=sys.stderr)
    sys.exit(1)

prev_vid = current["previous_stable_version"]
if not prev_vid:
    print(f"version_rollback: no previous stable version recorded for {current['version_id']}", file=sys.stderr)
    sys.exit(1)

# Retire current
con.execute(
    "UPDATE version_registry SET status='retired' WHERE version_id=?",
    (current["version_id"],)
)
# Promote previous
con.execute(
    "UPDATE version_registry SET status='stable', promoted_at=? WHERE version_id=?",
    (now, prev_vid)
)
con.commit()

new_current = con.execute(
    "SELECT * FROM version_registry WHERE version_id=?", (prev_vid,)
).fetchone()
con.close()
print(json.dumps(dict(new_current)) if new_current else "null")
PY
}

# desc: Quarantine a version so it cannot be re-promoted without explicit clearance.
version_quarantine() {
  local kind="${1:?kind required}"
  local version_id="${2:?version_id required}"
  local reason="${3:?reason required}"
  _ver_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$kind" "$version_id" "$reason" <<'PY'
import sqlite3, sys, time
db, kind, vid, reason = sys.argv[1:5]
now = int(time.time())
con = sqlite3.connect(db)
con.execute("""
    UPDATE version_registry
    SET status='quarantined', quarantine_reason=?, quarantined_at=?
    WHERE version_id=? AND kind=?
""", (reason, now, vid, kind))
con.commit()
con.close()
print(f"version_quarantine: {vid} quarantined", file=sys.stderr)
PY
}

# desc: Returns "true" if a version can be promoted (not quarantined); else "false".
version_can_promote() {
  local kind="${1:?kind required}"
  local version_id="${2:?version_id required}"
  _ver_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$kind" "$version_id" <<'PY'
import sqlite3, sys
db, kind, vid = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
row = con.execute(
    "SELECT status FROM version_registry WHERE kind=? AND version_id=?", (kind, vid)
).fetchone()
con.close()
if row is None:
    print("false")   # unknown version — cannot promote
elif row[0] == "quarantined":
    print("false")
else:
    print("true")
PY
}

# desc: Clear quarantine on a version, allowing future promotion.
#       Requires approver identity for audit trail.
version_clear_quarantine() {
  local version_id="${1:?version_id required}"
  local approver="${2:?approver required}"
  _ver_ensure_table
  python3 - "${MINI_ORK_DB:?MINI_ORK_DB unset}" "$version_id" "$approver" <<'PY'
import sqlite3, sys, time
db, vid, approver = sys.argv[1:4]
now = int(time.time())
con = sqlite3.connect(db)
updated = con.execute("""
    UPDATE version_registry
    SET status='candidate', quarantine_cleared_by=?, quarantine_reason=NULL
    WHERE version_id=? AND status='quarantined'
""", (approver, vid)).rowcount
con.commit()
con.close()
if updated == 0:
    print(f"version_clear_quarantine: {vid} not found or not quarantined", file=sys.stderr)
    sys.exit(1)
print(f"version_clear_quarantine: {vid} cleared by {approver}", file=sys.stderr)
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "version_registry.sh — source me and call version_register / version_get / version_current / version_rollback / etc."
fi
