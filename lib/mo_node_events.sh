#!/usr/bin/env bash
# lib/mo_node_events.sh — emit node-level lifecycle events to run_events.
#
# Purpose: power the observability UI's per-node DAG status. Until this,
# `mini-ork serve` could show *which* family ran each lens, but not whether
# the node had run, was running, or had completed. With these events the
# UI can color React Flow nodes accordingly.
#
# Why run_events, not mo_events:
#   - run_events.event_type has NO CHECK constraint (free-form string).
#   - run_events.run_id is TEXT — accepts task_run id directly.
#   - mo_events.epic_id is NOT NULL — top-level task_runs without an
#     attached epic can't write to mo_events without lying about epic_id.
#
# Public API:
#   mo_node_emit <run_id> <node_id> <node_type> <event_type> [extra_json]
#
# Args:
#   run_id      e.g. "self-improve-iter-32-20260609110333" (matches task_runs.id)
#   node_id     workflow node name (e.g. "opus_synthesizer")
#   node_type   "researcher" | "reviewer" | "implementer" | "verifier" | ...
#   event_type  "node_start" | "node_end" | "node_heartbeat"
#   extra_json  optional JSON object merged into payload (e.g. model_lane, duration_ms)
#
# Failure mode: best-effort. Any DB error is logged to stderr but does
# NOT fail the dispatch — observability must never break execution.

set -uo pipefail

# Portable millisecond timestamp. BSD `date` (macOS default) does NOT
# honor `%3N` — it silently emits the literal "N" instead of failing,
# which poisons arithmetic. Prefer GNU `gdate` (coreutils), fall back
# to python3. Same pattern as lib/llm-dispatch.sh:_mo_llm_now_ms.
_mo_now_ms() {
  if command -v gdate >/dev/null 2>&1; then
    gdate +%s%3N
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import time; print(int(time.time() * 1000))'
  else
    # Last-resort: seconds × 1000, accepting 1s resolution.
    echo "$(($(date +%s) * 1000))"
  fi
}

mo_node_emit() {
  local run_id="${1:-}"
  local node_id="${2:-}"
  local node_type="${3:-}"
  local event_type="${4:-}"
  local extra_json="${5:-{\}}"

  [ -n "$run_id" ]     || { echo "mo_node_emit: run_id required" >&2; return 0; }
  [ -n "$node_id" ]    || { echo "mo_node_emit: node_id required" >&2; return 0; }
  [ -n "$event_type" ] || { echo "mo_node_emit: event_type required" >&2; return 0; }

  local db="${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
  [ -f "$db" ] || return 0  # silent no-op if state.db missing (e.g. uninitialized test)

  local event_id="evt-${event_type}-${node_id}-$(date +%s%N 2>/dev/null || date +%s)-$$"

  python3 - "$db" "$event_id" "$run_id" "$node_id" "$node_type" "$event_type" "$extra_json" <<'PY' 2>/dev/null || true
import sqlite3, sys, json, time

db, event_id, run_id, node_id, node_type, event_type, extra_json = sys.argv[1:8]

# Merge mandatory fields into payload
try:
    extra = json.loads(extra_json) if extra_json else {}
    if not isinstance(extra, dict):
        extra = {"_raw": str(extra)}
except json.JSONDecodeError:
    extra = {"_raw": extra_json}

payload = {
    "node_id":   node_id,
    "node_type": node_type,
    **extra,
}

con = sqlite3.connect(db, timeout=2.0)
con.execute("PRAGMA busy_timeout = 2000")
try:
    cols = {r[1] for r in con.execute("PRAGMA table_info(run_events)").fetchall()}
    now_s = int(time.time())
    heartbeat_ms = int(time.time() * 1000)
    insert_cols = ["event_id", "run_id", "event_type", "payload_json", "created_at"]
    values = [event_id, run_id, event_type, json.dumps(payload), now_s]
    if "finish_reason" in cols:
        insert_cols.append("finish_reason")
        values.append(payload.get("finish_reason"))
    if "last_heartbeat_at" in cols and event_type in ("node_start", "node_heartbeat"):
        insert_cols.append("last_heartbeat_at")
        values.append(heartbeat_ms)
    placeholders = ",".join("?" for _ in insert_cols)
    con.execute(
        f"INSERT INTO run_events({', '.join(insert_cols)}) VALUES ({placeholders})",
        values,
    )
    con.commit()
finally:
    con.close()
PY
}

# Convenience: emit node_start with model_lane payload.
# Usage: mo_node_start <run_id> <node_id> <node_type> [<model_lane>]
mo_node_start() {
  local run_id="${1:?}" node_id="${2:?}" node_type="${3:?}" model_lane="${4:-}"
  local extra='{}'
  if [ -n "$model_lane" ]; then
    extra="{\"model_lane\":\"$model_lane\"}"
  fi
  mo_node_emit "$run_id" "$node_id" "$node_type" "node_start" "$extra"
}

# Convenience: emit node_heartbeat for the currently running node.
# Usage: mo_emit_node_heartbeat <node_id> <run_id>
mo_emit_node_heartbeat() {
  local node_id="${1:?}" run_id="${2:?}"
  local node_type="${MO_NODE_TYPE:-heartbeat}"
  mo_node_emit "$run_id" "$node_id" "$node_type" "node_heartbeat" "{}"
}

# RETURN-trap callback. Reads variables from the caller's scope so it can
# emit a final node_end regardless of which case branch returned the
# function (early `return 1` from researcher/implementer/reviewer/verifier
# failures all need a node_end too — not just clean fall-through).
#
# Caller must set: _mo_run_id, _mo_node_start_ms, node_id, node_type
# Optional: VERDICT, CONTEXT_FILE, IMPL_LOG, REVIEW_FILE
mo_node_emit_end_trap() {
  local _rc=$?
  local _end _dur _artifact _finish_reason
  [ -n "${_mo_run_id:-}" ] || return 0
  [ -n "${node_id:-}" ]    || return 0
  [ -n "${node_type:-}" ]  || return 0
  _end=$(_mo_now_ms)
  _dur=$(( _end - ${_mo_node_start_ms:-0} ))
  _artifact="${CONTEXT_FILE:-${IMPL_LOG:-${REVIEW_FILE:-}}}"
  _finish_reason="${MO_NODE_FINISH_REASON:-}"
  if [ -z "$_finish_reason" ]; then
    if [ "$_rc" -eq 0 ]; then
      _finish_reason="done"
    else
      _finish_reason="error"
    fi
  fi
  mo_node_end "$_mo_run_id" "$node_id" "$node_type" "$_dur" "${VERDICT:-}" "$_artifact" "$_finish_reason" || true
  # Piggyback an OTel agent span when lib/mo_otel.sh is loaded (MO_OTEL=1).
  if declare -f mo_otel_agent >/dev/null 2>&1; then
    mo_otel_agent "$node_id" "$node_type" "${_mo_node_start_ms:-0}" "$_end" "${VERDICT:-}" || true
  fi
}

# Convenience: emit node_end with duration + verdict payload.
# Usage: mo_node_end <run_id> <node_id> <node_type> <duration_ms> [<verdict>] [<artifact_path>] [<finish_reason>]
mo_node_end() {
  local run_id="${1:?}" node_id="${2:?}" node_type="${3:?}" duration_ms="${4:-0}"
  local verdict="${5:-}" artifact_path="${6:-}" finish_reason="${7:-}"
  local extra
  extra=$(python3 - "$duration_ms" "$verdict" "$artifact_path" "$finish_reason" <<'PY'
import json, sys
duration_ms, verdict, artifact_path, finish_reason = sys.argv[1:5]
out = {"duration_ms": int(duration_ms or 0)}
if verdict:       out["verdict"] = verdict
if artifact_path: out["artifact_path"] = artifact_path
if finish_reason: out["finish_reason"] = finish_reason
print(json.dumps(out))
PY
  )
  mo_node_emit "$run_id" "$node_id" "$node_type" "node_end" "$extra"
}
