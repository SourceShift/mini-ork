#!/usr/bin/env bash
# subagent-stop.sh — Claude `SubagentStop` hook.
#
# Wire-up:
#   "hooks": {
#     "SubagentStop": [
#       { "command": "<repo>/.mini-ork/hooks/subagent-stop.sh" }
#     ]
#   }
#
# Closes the most recent matching subagent_runs row by parent session +
# subagent_type and stamps result_excerpt + child_claude_session_id when
# Claude provides them in the stop payload.
#
# Output: emit `{"continue": true}` so Claude proceeds normally.

set -uo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export PYTHONPATH="$MINI_ORK_ROOT${PYTHONPATH:+:$PYTHONPATH}"
DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

payload=""
if [ ! -t 0 ]; then
  payload=$(cat 2>/dev/null || true)
fi

emit_continue() { printf '{"continue": true}\n'; }
[ -f "$DB" ] || { emit_continue; exit 0; }

parent_session="${MINI_ORK_PARENT_CLAUDE_SESSION:-${CLAUDE_SESSION_ID:-}}"
[ -z "$parent_session" ] && { emit_continue; exit 0; }

extract_field() {
  local field="$1"
  if [ -z "$payload" ]; then return; fi
  if command -v jq >/dev/null 2>&1; then
    echo "$payload" | jq -r ".tool_input.${field} // .tool_response.${field} // .params.${field} // .result.${field} // empty" 2>/dev/null
  else
    echo "$payload" | sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -1
  fi
}

subagent_type=$(extract_field "subagent_type")
child_session=$(extract_field "session_id")
result=$(extract_field "result")
[ -z "$result" ] && result=$(extract_field "summary")
status_raw=$(extract_field "status")

case "$status_raw" in
  fail|failed|error)        new_status="failed" ;;
  cancel|cancelled|aborted) new_status="cancelled" ;;
  *)                        new_status="completed" ;;
esac

result_excerpt="${result:0:480}"
esc_sql() { printf '%s' "${1:-}" | sed "s/'/''/g"; }

match_clause="parent_claude_session_id = '$(esc_sql "$parent_session")' AND status = 'spawned'"
if [ -n "$subagent_type" ]; then
  match_clause="$match_clause AND subagent_type = '$(esc_sql "$subagent_type")'"
fi

child_set_sql=""
if [ -n "$child_session" ]; then
  child_set_sql=", child_claude_session_id = '$(esc_sql "$child_session")'"
fi

sqlite3 "$DB" "
  UPDATE subagent_runs SET
    status        = '$new_status',
    ended_at      = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
    result_excerpt = '$(esc_sql "$result_excerpt")',
    duration_ms   = CAST(
      (strftime('%s','now') - strftime('%s', started_at)) * 1000 +
      (strftime('%f','now') - strftime('%f', started_at)) * 1000 AS INTEGER
    )
    $child_set_sql
  WHERE id = (
    SELECT id FROM subagent_runs
     WHERE $match_clause
     ORDER BY started_at DESC
     LIMIT 1
  );
" 2>/dev/null || true

# Mirror to ContextNest so the child subagent's transcript is ingested into
# the substrate. CN's /api/v1/cc/hook/subagent_stop tails the JSONL from the
# last-known offset and extracts memories/features. Fire-and-forget.
# Restored 2026-06-16 after PR #18 silently dropped this hook augmentation
# (regression caught by scripts/smoke-cn-bridge.sh).
if [ "${MO_DISABLE_CN:-0}" != "1" ]; then
  transcript_path=$(extract_field "transcript_path")
  [ -z "$transcript_path" ] && transcript_path=$(extract_field "transcript")
  target_session="${child_session:-$parent_session}"
  python3 -m mini_ork.cli.cn_hook post "subagent_stop" "$target_session" \
    --cwd "$PWD" --transcript "$transcript_path" >/dev/null 2>&1 || true

  # PR-6 outcome feedback (2026-06-17). If the worker consumed atoms via
  # the prefetch hook (file at $MO_CN_PREFETCH_DIR/<sid>.md), extract the
  # CN atom ids it referenced and POST outcome to CN's /agent/outcome
  # endpoint. CN bumps last_accessed on each + adjusts a confidence
  # signal capped at ±0.05 per call. Fire-and-forget; never blocks.
  #
  # Heuristic for atom_id extraction: scan the prefetch file for any
  # `cn-<32 hex chars>` token (CN's canonical atom id shape). The
  # prefetch file content (rendered by cn_render_atoms_md +
  # cn_render_inbox_md + cn_render_features_md) embeds these inline in
  # the "sess=<8 chars>" / "[id]" markup, but only `cn-` atom ids
  # uniquely identify substrate atoms.
  #
  # Outcome classification: subagent_runs.status (set above by the
  # UPDATE) → "success" | "failure". status comes from extract_field
  # earlier in this hook; default to "neutral" when unknown.
  if [ -n "${MO_CN_PREFETCH_DIR:-}" ]; then
    _outcome_session="${child_session:-$parent_session}"
    _prefetch_file="${MO_CN_PREFETCH_DIR}/${_outcome_session}.md"
    if [ -f "$_prefetch_file" ]; then
      # Extract cn-<hex>... atom ids. grep -oE keeps full matches; uniq
      # dedups; tr+paste collapses to CSV.
      _atom_ids_csv=$(grep -oE 'cn-[a-f0-9]{8,40}' "$_prefetch_file" 2>/dev/null \
        | sort -u \
        | head -20 \
        | paste -sd, -)
      if [ -n "$_atom_ids_csv" ]; then
        # Map subagent status → outcome. "completed" / "ok" / etc. → success.
        # "failed" / "cancelled" / etc. → failure. Anything else → neutral.
        _outcome_str="neutral"
        case "$new_status" in
          completed|ok|success) _outcome_str="success" ;;
          failed|cancelled|error|aborted) _outcome_str="failure" ;;
        esac
        # Evidence: first 240 chars of the worker's result_excerpt (already
        # captured upstream in this hook). Keeps the outcome trace
        # debuggable without paying the 480-char CN cap.
        _evidence="${result_excerpt:0:240}"
        python3 -m mini_ork.cli.cn_hook outcome "$_outcome_str" "$_atom_ids_csv" \
          --evidence "$_evidence" --session "$_outcome_session" >/dev/null 2>&1 || true
      fi
    fi
  fi
fi

emit_continue
