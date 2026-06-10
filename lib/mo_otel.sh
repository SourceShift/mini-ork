#!/usr/bin/env bash
# lib/mo_otel.sh — live OTel span buffering for a mini-ork run.
#
# Implements the "span emitter" from docs/architecture/otel-langfuse.md:
# lifecycle events are appended as JSONL to $MINI_ORK_RUN_DIR/.otel-spans.jsonl
# during the run, then flushed once at run end via mini_ork.otel_export
# (--from-jsonl), which assembles the span tree (task_run → agent →
# llm_call, the llm_call layer joined in from state.db.llm_calls) and POSTs
# OTLP/JSON to Langfuse.
#
# Gating:
#   MO_OTEL=1                  master switch — without it every function no-ops
#   LANGFUSE_PUBLIC_KEY/SECRET required for the flush to leave the host;
#                              without creds the JSONL buffer survives on disk
#                              for a later manual flush (resync)
#   MO_OTEL_DRY_RUN=1          flush prints the OTLP payload instead of POSTing
#
# Failure mode: best-effort everywhere. Observability must never break
# execution — every entry point returns 0.

set -uo pipefail

mo_otel_enabled() {
  [ "${MO_OTEL:-0}" = "1" ] && [ -n "${MINI_ORK_RUN_DIR:-}" ]
}

mo_otel_buf() {
  echo "${MINI_ORK_RUN_DIR:-}/.otel-spans.jsonl"
}

# Portable ms timestamp (same rationale as lib/mo_node_events.sh:_mo_now_ms:
# BSD date emits literal "N" for %3N).
_mo_otel_now_ms() {
  if command -v gdate >/dev/null 2>&1; then
    gdate +%s%3N
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import time; print(int(time.time() * 1000))'
  else
    echo "$(($(date +%s) * 1000))"
  fi
}

# desc: Append one raw JSON event line to the buffer. No-op when disabled.
mo_otel_emit() {
  mo_otel_enabled || return 0
  printf '%s\n' "${1:?json required}" >> "$(mo_otel_buf)" 2>/dev/null || true
}

# desc: Record run start. Call once, after MINI_ORK_RUN_DIR is exported.
# Usage: mo_otel_root_begin <task_run_id>
mo_otel_root_begin() {
  mo_otel_enabled || return 0
  local run_id="${1:?task_run_id required}"
  mo_otel_emit "{\"type\":\"root_begin\",\"task_run_id\":\"${run_id}\",\"start_ms\":$(_mo_otel_now_ms)}"
}

# desc: Record run end. Maps a shell rc to span status.
# Usage: mo_otel_root_end <rc>
mo_otel_root_end() {
  mo_otel_enabled || return 0
  local rc="${1:-0}" status="success"
  [ "$rc" = "0" ] || status="failure"
  mo_otel_emit "{\"type\":\"root_end\",\"end_ms\":$(_mo_otel_now_ms),\"status\":\"${status}\"}"
}

# desc: Record one agent (workflow node) span.
# Usage: mo_otel_agent <node_id> <node_type> <start_ms> <end_ms> [<verdict>]
mo_otel_agent() {
  mo_otel_enabled || return 0
  local node_id="${1:?}" node_type="${2:-}" start_ms="${3:-0}" end_ms="${4:-0}" verdict="${5:-}"
  mo_otel_emit "{\"type\":\"agent\",\"node_id\":\"${node_id}\",\"node_type\":\"${node_type}\",\"start_ms\":${start_ms},\"end_ms\":${end_ms},\"verdict\":\"${verdict}\"}"
}

# desc: Flush the buffer through mini_ork.otel_export. On a successful live
#       POST the exporter renames the buffer to *.sent; on any failure the
#       JSONL stays in place for resync. Always returns 0.
mo_otel_flush() {
  mo_otel_enabled || return 0
  local buf
  buf="$(mo_otel_buf)"
  [ -s "$buf" ] || return 0
  local root="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  local -a flags=(--from-jsonl "$buf")
  [ -n "${MINI_ORK_DB:-}" ] && [ -f "${MINI_ORK_DB:-}" ] && flags+=(--db "$MINI_ORK_DB")
  [ "${MO_OTEL_DRY_RUN:-0}" = "1" ] && flags+=(--dry-run)
  (cd "$root" && python3 -m mini_ork.otel_export "${flags[@]}") || true
  return 0
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "mo_otel.sh — source me; MO_OTEL=1 enables span buffering"
fi
