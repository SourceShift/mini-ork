#!/usr/bin/env bash
# gates_common.sh — shared helpers for gate libraries.
#
# Provides mo_grounded_rejection, the canonical (concern, evidence,
# suggestion) emitter required on every gate fail/needs_revision verdict
# per HarnessBridge Technique 4 (arxiv:2606.12882) and the 2026-06-15
# audit Theme C finding (synthesis at
# .mini-ork/runs/run-1781523329-71306/synthesis.md gitignored).
#
# Stored to state.db:grounded_rejections per migration
# db/migrations/0037_grounded_rejection.sql so the reflector can read
# structured rejection rows instead of re-extracting tuples from
# free-text rationale.
#
# Public API:
#   mo_grounded_rejection <gate_name> <verdict> <concern> \
#                         <evidence_summary> <suggestion> \
#                         [evidence_trace_ids_json] [run_id]
#       Writes a grounded_rejections row and prints its id on stdout.
#       verdict must be one of: fail, needs_revision, indeterminate.
#       evidence_trace_ids_json is a JSON array (default '[]').
#       Returns 2 on argument error, 3 on JSON validation error,
#       0 on success.
#
#   mo_grounded_rejection_tuple_json <concern> <evidence_summary> \
#                                    <suggestion>
#       Prints the canonical JSON tuple form to stdout for callers
#       that want the tuple structure without DB persistence.

set -uo pipefail

_mo_gr_log() {
  local _level="$1"; shift
  printf '{"level":"%s","subsystem":"gates_common","ts":"%s","msg":"%s"}\n' \
    "$_level" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

_mo_gr_db() {
  printf '%s\n' "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
}

_mo_gr_table_exists() {
  local _db; _db=$(_mo_gr_db)
  [ -f "$_db" ] || return 1
  sqlite3 "$_db" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='grounded_rejections'" 2>/dev/null \
    | grep -q '^1$'
}

_mo_gr_validate_verdict() {
  case "$1" in
    fail|needs_revision|indeterminate) return 0 ;;
    *) return 1 ;;
  esac
}

_mo_gr_validate_json_array() {
  local _payload="$1"
  [ -z "$_payload" ] && return 0
  python3 -c "import json,sys; v=json.loads(sys.argv[1]); assert isinstance(v,list)" "$_payload" 2>/dev/null
}

mo_grounded_rejection_tuple_json() {
  local _concern="${1:-}" _evidence="${2:-}" _suggestion="${3:-}"
  if [ -z "$_concern" ] || [ -z "$_evidence" ] || [ -z "$_suggestion" ]; then
    _mo_gr_log "error" "mo_grounded_rejection_tuple_json <concern> <evidence> <suggestion>"
    return 2
  fi
  python3 - <<PY
import json
print(json.dumps({
    "concern": """$_concern""",
    "evidence": """$_evidence""",
    "suggestion": """$_suggestion""",
}, ensure_ascii=False))
PY
}

mo_grounded_rejection() {
  local _gate="${1:-}" _verdict="${2:-}" _concern="${3:-}"
  local _evidence_summary="${4:-}" _suggestion="${5:-}"
  local _evidence_ids="${6:-[]}" _run_id="${7:-}"

  if [ -z "$_gate" ] || [ -z "$_verdict" ] || [ -z "$_concern" ] || \
     [ -z "$_evidence_summary" ] || [ -z "$_suggestion" ]; then
    _mo_gr_log "error" "mo_grounded_rejection <gate> <verdict> <concern> <evidence_summary> <suggestion> [trace_ids_json] [run_id]"
    return 2
  fi
  if ! _mo_gr_validate_verdict "$_verdict"; then
    _mo_gr_log "error" "invalid verdict: $_verdict (must be fail|needs_revision|indeterminate)"
    return 2
  fi
  if ! _mo_gr_validate_json_array "$_evidence_ids"; then
    _mo_gr_log "error" "evidence_trace_ids must be a JSON array"
    return 3
  fi
  if ! _mo_gr_table_exists; then
    _mo_gr_log "warn" "grounded_rejections table absent; emit is a no-op. Run migrations."
    return 0
  fi

  local _db; _db=$(_mo_gr_db)
  local _id
  _id=$(python3 -c "import secrets; print(secrets.token_hex(16))")

  python3 - <<PY
import sqlite3
con = sqlite3.connect("$_db")
try:
    con.execute(
        """INSERT INTO grounded_rejections
           (id, gate_name, verdict, concern, evidence_summary, suggestion,
            evidence_trace_ids, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("$_id",
         "$_gate",
         "$_verdict",
         """$_concern""",
         """$_evidence_summary""",
         """$_suggestion""",
         """$_evidence_ids""",
         "$_run_id" or None)
    )
    con.commit()
finally:
    con.close()
PY
  printf '%s\n' "$_id"
  _mo_gr_log "info" "emitted grounded_rejection id=$_id gate=$_gate verdict=$_verdict run=$_run_id"
}

# Self-test fixtures.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  _tmp_home=$(mktemp -d)
  export MINI_ORK_HOME="$_tmp_home"
  export MINI_ORK_DB="$_tmp_home/state.db"
  _root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  sqlite3 "$MINI_ORK_DB" < "$_root/db/migrations/0037_grounded_rejection.sql"

  echo "--- fixture 1: tuple_json shape ---"
  out=$(mo_grounded_rejection_tuple_json "missing input" "evidence span" "wait for upstream")
  if printf '%s' "$out" | python3 -c "import sys,json; t=json.load(sys.stdin); assert set(t.keys())=={'concern','evidence','suggestion'}; print('ok')" 2>/dev/null | grep -q ok; then
    echo "  [ok] tuple_json keys = concern+evidence+suggestion"
  else
    echo "  [fail] tuple_json shape wrong: $out"
  fi

  echo "--- fixture 2: emit valid rejection ---"
  id=$(mo_grounded_rejection "coalition" "fail" \
       "panel missing third lens family" \
       "trace tr-7d2a1 shows only 2 of 3 families voted" \
       "wait for kimi-lens retry or escalate to operator" \
       '["tr-7d2a1","tr-7d2a2"]' \
       "run-test-1")
  if [ -n "$id" ] && [ ${#id} -ge 16 ]; then
    echo "  [ok] emit returned id=$id"
  else
    echo "  [fail] emit did not return valid id"
  fi

  echo "--- fixture 3: invalid verdict rejected ---"
  set +e
  mo_grounded_rejection "coalition" "catastrophic" "x" "y" "z" '[]' >/dev/null 2>&1
  rc=$?
  set -e
  [ "$rc" = "2" ] && echo "  [ok] invalid verdict rejected" || echo "  [fail] rc=$rc"

  echo "--- fixture 4: invalid evidence_trace_ids JSON rejected ---"
  set +e
  mo_grounded_rejection "coalition" "fail" "x" "y" "z" 'not-json' >/dev/null 2>&1
  rc=$?
  set -e
  [ "$rc" = "3" ] && echo "  [ok] non-array JSON rejected" || echo "  [fail] rc=$rc"

  echo "--- fixture 5: append-only trigger blocks UPDATE of provenance ---"
  set +e
  err=$(sqlite3 "$MINI_ORK_DB" "UPDATE grounded_rejections SET concern='hacked' WHERE id='$id'" 2>&1)
  rc=$?
  set -e
  if [ "$rc" != "0" ] && printf '%s' "$err" | grep -q immutable; then
    echo "  [ok] append-only trigger blocked concern UPDATE"
  else
    echo "  [fail] trigger leaked: rc=$rc err=$err"
  fi

  echo "--- fixture 6: consumed_by_reflector_ts is updatable ---"
  set +e
  err=$(sqlite3 "$MINI_ORK_DB" "UPDATE grounded_rejections SET consumed_by_reflector_ts=strftime('%s','now') WHERE id='$id'" 2>&1)
  rc=$?
  set -e
  if [ "$rc" = "0" ]; then
    echo "  [ok] consumed_by_reflector_ts UPDATE allowed"
  else
    echo "  [fail] consumed_by_reflector_ts UPDATE blocked: $err"
  fi

  rm -rf "$_tmp_home"
fi
