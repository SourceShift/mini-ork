#!/usr/bin/env bash
# safety_events.sh — write and query safety incident records.
#
# Backs docs/RSP.md § 3 (tripwires) and § 4.1 (post-incident report
# commitments). Tripwires fire from various places (lib/cost_pause.sh:47,
# lib/coalition_gate.sh:49, lib/sandbox/omnigent-bridge.sh:68); this
# library is the uniform write/query interface that keeps the records
# in state.db:safety_events with the schema in
# db/migrations/0036_safety_events.sql.
#
# Public API:
#   mo_safety_event_emit <tripwire_id> <severity> <evidence_json>
#                        [run_id] [recipe]
#   mo_safety_event_list_open [tripwire_id]
#   mo_safety_event_acknowledge <event_id> <operator_response>
#   mo_safety_event_resolve <event_id> <resolution_note>

set -uo pipefail

_mo_se_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"safety_events","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_se_db() {
  printf '%s\n' "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
}

_mo_se_table_exists() {
  local _db; _db=$(_mo_se_db)
  [ -f "$_db" ] || return 1
  sqlite3 "$_db" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='safety_events'" 2>/dev/null \
    | grep -q '^1$'
}

_mo_se_validate_severity() {
  case "$1" in
    critical|high|medium|low) return 0 ;;
    *) return 1 ;;
  esac
}

_mo_se_validate_json() {
  local _payload="$1"
  [ -z "$_payload" ] && return 0
  python3 -c "import json,sys; json.loads(sys.argv[1])" "$_payload" 2>/dev/null
}

_mo_se_new_id() {
  python3 -c "import secrets; print(secrets.token_hex(16))"
}

mo_safety_event_emit() {
  local _tripwire="${1:-}" _severity="${2:-}" _evidence="${3:-}"
  local _run_id="${4:-}" _recipe="${5:-}"

  if [ -z "$_tripwire" ] || [ -z "$_severity" ]; then
    _mo_se_log "error" "mo_safety_event_emit <tripwire_id> <severity> <evidence_json> [run_id] [recipe]"
    return 2
  fi
  if ! _mo_se_validate_severity "$_severity"; then
    _mo_se_log "error" "invalid severity: $_severity (must be critical|high|medium|low)"
    return 2
  fi
  if ! _mo_se_validate_json "$_evidence"; then
    _mo_se_log "error" "evidence_json failed JSON validation"
    return 3
  fi
  if ! _mo_se_table_exists; then
    _mo_se_log "warn" "safety_events table absent; emit is a no-op. Run migrations."
    return 0
  fi

  [ -z "$_evidence" ] && _evidence='{}'

  local _db; _db=$(_mo_se_db)

  local _existing
  if [ -n "$_run_id" ]; then
    _existing=$(sqlite3 "$_db" "
      SELECT id FROM safety_events
      WHERE tripwire_id = '$(printf '%s' "$_tripwire" | sed "s/'/''/g")'
        AND run_id = '$(printf '%s' "$_run_id" | sed "s/'/''/g")'
        AND ts >= (strftime('%s','now') - 60)
      ORDER BY ts DESC LIMIT 1" 2>/dev/null)
    if [ -n "$_existing" ]; then
      printf '%s\n' "$_existing"
      return 0
    fi
  fi

  local _id; _id=$(_mo_se_new_id)

  python3 - <<PY
import sqlite3
con = sqlite3.connect("$_db")
try:
    con.execute(
        """INSERT INTO safety_events
           (id, tripwire_id, severity, run_id, recipe, evidence_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("$_id",
         "$_tripwire",
         "$_severity",
         "$_run_id" or None,
         "$_recipe" or None,
         """$_evidence""")
    )
    con.commit()
finally:
    con.close()
PY
  printf '%s\n' "$_id"
  _mo_se_log "info" "emitted safety_event id=$_id tripwire=$_tripwire severity=$_severity run=$_run_id"
}

mo_safety_event_list_open() {
  local _filter="${1:-}"
  if ! _mo_se_table_exists; then
    _mo_se_log "warn" "safety_events table absent; nothing to list"
    return 0
  fi
  local _db; _db=$(_mo_se_db)
  local _where="status='open'"
  if [ -n "$_filter" ]; then
    _where="$_where AND tripwire_id='$(printf '%s' "$_filter" | sed "s/'/''/g")'"
  fi
  python3 - <<PY
import sqlite3, json
con = sqlite3.connect("$_db")
con.row_factory = sqlite3.Row
try:
    cur = con.execute(
        "SELECT id, ts, tripwire_id, severity, run_id, recipe, evidence_json, status "
        "FROM safety_events WHERE $_where ORDER BY ts DESC"
    )
    for row in cur:
        out = dict(row)
        try:
            out["evidence"] = json.loads(out.pop("evidence_json"))
        except Exception:
            out["evidence"] = out.pop("evidence_json")
        print(json.dumps(out))
finally:
    con.close()
PY
}

mo_safety_event_acknowledge() {
  local _id="${1:-}" _response="${2:-}"
  if [ -z "$_id" ] || [ -z "$_response" ]; then
    _mo_se_log "error" "mo_safety_event_acknowledge <event_id> <operator_response>"
    return 2
  fi
  if ! _mo_se_table_exists; then
    _mo_se_log "warn" "safety_events table absent; ack is a no-op"
    return 0
  fi
  local _db; _db=$(_mo_se_db)
  python3 - <<PY
import sqlite3
con = sqlite3.connect("$_db")
try:
    con.execute(
        "UPDATE safety_events SET status='acknowledged', operator_response=? "
        "WHERE id=? AND status='open'",
        ("""$_response""", "$_id")
    )
    con.commit()
finally:
    con.close()
PY
  _mo_se_log "info" "acknowledged safety_event id=$_id"
}

mo_safety_event_resolve() {
  local _id="${1:-}" _note="${2:-}"
  if [ -z "$_id" ] || [ -z "$_note" ]; then
    _mo_se_log "error" "mo_safety_event_resolve <event_id> <resolution_note>"
    return 2
  fi
  if ! _mo_se_table_exists; then
    _mo_se_log "warn" "safety_events table absent; resolve is a no-op"
    return 0
  fi
  local _db; _db=$(_mo_se_db)
  python3 - <<PY
import sqlite3, time
con = sqlite3.connect("$_db")
try:
    con.execute(
        "UPDATE safety_events SET status='resolved', resolution_ts=?, resolution_note=? "
        "WHERE id=? AND status IN ('open','acknowledged')",
        (int(time.time()), """$_note""", "$_id")
    )
    con.commit()
finally:
    con.close()
PY
  _mo_se_log "info" "resolved safety_event id=$_id"
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _tmp_home=$(mktemp -d)
  export MINI_ORK_HOME="$_tmp_home"
  export MINI_ORK_DB="$_tmp_home/state.db"
  _root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  sqlite3 "$MINI_ORK_DB" < "$_root/db/migrations/0036_safety_events.sql"

  echo "--- fixture 1: emit valid event ---"
  id=$(mo_safety_event_emit "TW-1" "high" '{"cost_usd":12.50}' "run-test-1" "recursive-self-improve")
  if [ -n "$id" ] && [ ${#id} -ge 16 ]; then echo "  [ok] id=$id"; else echo "  [fail]"; fi

  echo "--- fixture 2: invalid severity rejected ---"
  set +e
  mo_safety_event_emit "TW-2" "catastrophic" '{}' >/dev/null 2>&1; rc=$?
  set -e
  [ "$rc" = "2" ] && echo "  [ok]" || echo "  [fail] rc=$rc"

  echo "--- fixture 3: invalid JSON rejected ---"
  set +e
  mo_safety_event_emit "TW-3" "low" 'bad-json' >/dev/null 2>&1; rc=$?
  set -e
  [ "$rc" = "3" ] && echo "  [ok]" || echo "  [fail] rc=$rc"

  echo "--- fixture 4: idempotent emit ---"
  id1=$(mo_safety_event_emit "TW-4" "medium" '{"x":1}' "run-test-4")
  id2=$(mo_safety_event_emit "TW-4" "medium" '{"x":1}' "run-test-4")
  [ "$id1" = "$id2" ] && [ -n "$id1" ] && echo "  [ok] $id1" || echo "  [fail]"

  echo "--- fixture 5: list_open ---"
  out=$(mo_safety_event_list_open | grep -c "TW-1\|TW-4")
  [ "$out" -ge 2 ] && echo "  [ok] $out rows" || echo "  [fail] $out"

  echo "--- fixture 6: acknowledge ---"
  mo_safety_event_acknowledge "$id" "investigating"
  status=$(sqlite3 "$MINI_ORK_DB" "SELECT status FROM safety_events WHERE id='$id'")
  [ "$status" = "acknowledged" ] && echo "  [ok]" || echo "  [fail] $status"

  echo "--- fixture 7: resolve ---"
  mo_safety_event_resolve "$id" "cap raised per operator"
  status=$(sqlite3 "$MINI_ORK_DB" "SELECT status FROM safety_events WHERE id='$id'")
  [ "$status" = "resolved" ] && echo "  [ok]" || echo "  [fail] $status"

  echo "--- fixture 8: trigger blocks immutable UPDATE ---"
  set +e
  err=$(sqlite3 "$MINI_ORK_DB" "UPDATE safety_events SET tripwire_id='TW-X' WHERE id='$id'" 2>&1); rc=$?
  set -e
  [ "$rc" != "0" ] && printf '%s' "$err" | grep -q immutable && echo "  [ok]" || echo "  [fail]"

  echo "--- fixture 9: trigger blocks DELETE ---"
  set +e
  err=$(sqlite3 "$MINI_ORK_DB" "DELETE FROM safety_events WHERE id='$id'" 2>&1); rc=$?
  set -e
  [ "$rc" != "0" ] && printf '%s' "$err" | grep -q append-only && echo "  [ok]" || echo "  [fail]"

  rm -rf "$_tmp_home"
fi
