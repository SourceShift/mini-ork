#!/usr/bin/env bash
# lib/operator_steering.sh — emit + consume operator steering messages.
#
# Public API:
#
#   operator_steering_emit \
#       --message "<text>" \
#       [--run-id <run-id>] \
#       [--role-target planner|implementer|reviewer|verifier|any] \
#       [--severity info|warn|critical] \
#       [--source <free-form>] \
#       [--confidence 0.0-1.0] \
#       [--ttl-secs <int>]   # default 3600 (1h)
#
#     Returns the new row id on stdout.
#
#   operator_steering_fetch_for <run-id> <role>
#
#     Prints up to 10 unconsumed, unexpired steering rows targeted at the
#     given run + role (or "any") as JSONL. Marks them consumed so the
#     next call returns nothing — agents should not see the same steering
#     twice in a run.
#
# Failure mode: any DB error logs to stderr and returns non-zero. Callers
# decide what to do; the bin wrapper exits with the same code.

set -uo pipefail

_operator_steering_db() {
  echo "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
}

_operator_steering_now_ms() {
  if command -v gdate >/dev/null 2>&1; then
    gdate +%s%3N
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import time; print(int(time.time() * 1000))'
  else
    echo $(($(date +%s) * 1000))
  fi
}

operator_steering_emit() {
  local run_id=""
  local role_target="any"
  local severity="info"
  local source=""
  local confidence="0.8"
  local ttl_secs="${MINI_ORK_STEERING_DEFAULT_TTL:-3600}"
  local message=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --run-id)       run_id="$2"; shift 2 ;;
      --role-target)  role_target="$2"; shift 2 ;;
      --severity)     severity="$2"; shift 2 ;;
      --source)       source="$2"; shift 2 ;;
      --confidence)   confidence="$2"; shift 2 ;;
      --ttl-secs)     ttl_secs="$2"; shift 2 ;;
      --message)      message="$2"; shift 2 ;;
      *) echo "operator_steering_emit: unknown flag: $1" >&2; return 2 ;;
    esac
  done

  [ -n "$message" ] || { echo "operator_steering_emit: --message required" >&2; return 2; }

  local db
  db="$(_operator_steering_db)"
  [ -f "$db" ] || { echo "operator_steering_emit: state.db not found: $db" >&2; return 1; }

  local now_ms expires_ms
  now_ms="$(_operator_steering_now_ms)"
  expires_ms=$((now_ms + ttl_secs * 1000))

  python3 - "$db" "$run_id" "$role_target" "$severity" "$message" \
                   "$source" "$confidence" "$now_ms" "$expires_ms" <<'PY' || return 1
import sqlite3, sys
db, run_id, role_target, severity, message, source, confidence, created_at, expires_at = sys.argv[1:10]
con = sqlite3.connect(db, timeout=5.0)
con.execute("PRAGMA busy_timeout = 5000")
try:
    cur = con.execute(
        """INSERT INTO operator_steering
             (run_id, role_target, severity, message, source, confidence, created_at, expires_at)
           VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?)""",
        (run_id, role_target, severity, message, source, float(confidence), int(created_at), int(expires_at))
    )
    con.commit()
    print(cur.lastrowid)
finally:
    con.close()
PY
}

operator_steering_fetch_for() {
  local run_id="${1:-}"
  local role="${2:-any}"

  local db
  db="$(_operator_steering_db)"
  [ -f "$db" ] || return 0  # silent no-op when no DB

  local now_ms
  now_ms="$(_operator_steering_now_ms)"

  python3 - "$db" "$run_id" "$role" "$now_ms" <<'PY' 2>/dev/null
import json, sqlite3, sys, time
db, run_id, role, now_ms = sys.argv[1:5]
now_ms = int(now_ms)
con = sqlite3.connect(db, timeout=5.0)
con.execute("PRAGMA busy_timeout = 5000")
try:
    cur = con.execute(
        """SELECT id, run_id, role_target, severity, message, source,
                  confidence, created_at, expires_at
             FROM operator_steering
            WHERE consumed_at IS NULL
              AND expires_at > ?
              AND (run_id = ? OR run_id IS NULL)
              AND (role_target = ? OR role_target = 'any')
            ORDER BY
              CASE severity WHEN 'critical' THEN 3 WHEN 'warn' THEN 2 ELSE 1 END DESC,
              confidence DESC,
              created_at DESC
            LIMIT 10""",
        (now_ms, run_id or '', role)
    )
    rows = cur.fetchall()
    ids = []
    for r in rows:
        ids.append(r[0])
        print(json.dumps({
            "id": r[0],
            "run_id": r[1],
            "role_target": r[2],
            "severity": r[3],
            "message": r[4],
            "source": r[5],
            "confidence": r[6],
            "created_at": r[7],
            "expires_at": r[8],
        }))
    if ids:
        # Mark consumed in one statement so a second call returns nothing.
        placeholders = ",".join("?" for _ in ids)
        con.execute(
            f"UPDATE operator_steering SET consumed_at = ? WHERE id IN ({placeholders})",
            [now_ms, *ids],
        )
        con.commit()
finally:
    con.close()
PY
}
