#!/usr/bin/env bash
# lib/mid_node_injector.sh — mid-node operator-steering injector.
#
# Closes the surface that docs/OPERATOR-STEERING.md does not: while a
# node's LLM call is in flight, a sidecar polls operator_steering and
# pushes any new rows targeted at this run_id+role into the worker
# CLI's stdin as user-shaped JSON messages. The CLI consumes them at
# its next turn boundary (between tool calls) — in-flight tokens are
# preserved.
#
# Two wire surfaces, one per CLI family:
#
#   claude  --input-format stream-json + stdin fifo
#           supervisor writes:
#           {"type":"user","message":{"role":"user","content":[...]}}\n
#           claude reads at next turn boundary; no SIGTERM needed
#
#   codex   no realtime stdin; supervisor SIGTERMs codex and then runs
#           `codex fork <session_id>` with the steering as the next
#           user turn. In-flight LLM response is discarded.
#
# Public API:
#
#   mid_node_injector_start <kind> <fifo_in> <run_id> <role> [poll_secs]
#
#     kind        "claude" | "codex"
#     fifo_in     Path to the fifo the CLI is reading from (claude) or
#                 the path that holds the codex session_id (codex)
#     run_id      MINI_ORK_RUN_ID — used as the operator_steering query
#     role        "implementer" | "reviewer" | "researcher" — used as
#                 the role_target filter
#     poll_secs   How often to scan operator_steering (default 5)
#
#     Spawns a background sidecar process. Writes the PID to
#     ${MINI_ORK_RUN_DIR}/.mid-node-injector.pid for stop().
#
#   mid_node_injector_stop
#
#     Reads the PID file and kills the sidecar.

set -uo pipefail

_mid_injector_pid_file() {
  echo "${MINI_ORK_RUN_DIR:-/tmp}/.mid-node-injector.pid"
}

# Build a claude-shaped user message from a steering row.
# Returns one JSON line ready to push into a stream-json fifo.
_mid_injector_format_claude_user_msg() {
  local message="$1"
  local severity="${2:-info}"
  local source="${3:-operator}"
  local body
  body="$(printf 'OPERATOR STEERING [%s] (from %s): %s' \
    "$severity" "$source" "$message")"
  jq -nc --arg t "$body" \
    '{type:"user",message:{role:"user",content:[{type:"text",text:$t}]}}'
}

# Build the codex-shaped continuation prompt from a steering row.
# Returns a plain text string to pass as `codex fork <id> "..."`.
_mid_injector_format_codex_prompt() {
  local message="$1"
  local severity="${2:-info}"
  local source="${3:-operator}"
  printf 'OPERATOR STEERING [%s] (from %s): %s\nContinue your task with this guidance.' \
    "$severity" "$source" "$message"
}

# Internal sidecar loop body for claude lane.
_mid_injector_claude_loop() {
  local fifo_in="$1"
  local run_id="$2"
  local role="$3"
  local poll_secs="$4"
  local parent_pid="$5"

  local lib_dir
  lib_dir="$(dirname "${BASH_SOURCE[0]}")"
  # shellcheck source=lib/operator_steering.sh
  . "$lib_dir/operator_steering.sh" 2>/dev/null || return 1

  while kill -0 "$parent_pid" 2>/dev/null; do
    sleep "$poll_secs"
    kill -0 "$parent_pid" 2>/dev/null || break
    local rows
    rows="$(operator_steering_fetch_for "$run_id" "$role" 2>/dev/null)"
    [ -n "$rows" ] || continue
    while IFS= read -r r; do
      [ -n "$r" ] || continue
      local msg sev src line
      msg="$(jq -r '.message' <<<"$r" 2>/dev/null)" || continue
      sev="$(jq -r '.severity' <<<"$r" 2>/dev/null)"
      src="$(jq -r '.source // "operator"' <<<"$r" 2>/dev/null)"
      line="$(_mid_injector_format_claude_user_msg "$msg" "$sev" "$src")"
      # Best-effort push to the fifo. If the reader (claude) has
      # already closed, we silently drop the message.
      printf '%s\n' "$line" >"$fifo_in" 2>/dev/null || true
    done <<<"$rows"
  done
}

# Internal sidecar loop body for codex lane.
_mid_injector_codex_loop() {
  local session_id_file="$1"
  local run_id="$2"
  local role="$3"
  local poll_secs="$4"
  local parent_pid="$5"
  local fork_out_dir="${MINI_ORK_RUN_DIR:?MINI_ORK_RUN_DIR required for codex injector}"

  local lib_dir
  lib_dir="$(dirname "${BASH_SOURCE[0]}")"
  # shellcheck source=lib/operator_steering.sh
  . "$lib_dir/operator_steering.sh" 2>/dev/null || return 1

  while kill -0 "$parent_pid" 2>/dev/null; do
    sleep "$poll_secs"
    kill -0 "$parent_pid" 2>/dev/null || break
    local rows
    rows="$(operator_steering_fetch_for "$run_id" "$role" 2>/dev/null)"
    [ -n "$rows" ] || continue

    # Need a known session_id to fork. If the parent codex hasn't
    # written it yet, push the steering back via a re-emit so the next
    # poll picks it up (the fetch already marked it consumed, so this
    # is permanent loss — log and continue).
    local session_id=""
    [ -f "$session_id_file" ] && session_id="$(cat "$session_id_file" 2>/dev/null)"
    if [ -z "$session_id" ]; then
      echo "mid_injector_codex: dropped steering — session_id not yet captured" >&2
      continue
    fi

    # SIGTERM the parent codex so the in-flight response is cut. The
    # outer wrapper script will detect the exit + replay via fork.
    kill -TERM "$parent_pid" 2>/dev/null || true

    while IFS= read -r r; do
      [ -n "$r" ] || continue
      local msg sev src
      msg="$(jq -r '.message' <<<"$r" 2>/dev/null)" || continue
      sev="$(jq -r '.severity' <<<"$r" 2>/dev/null)"
      src="$(jq -r '.source // "operator"' <<<"$r" 2>/dev/null)"
      local prompt
      prompt="$(_mid_injector_format_codex_prompt "$msg" "$sev" "$src")"
      # Replay via codex fork; output appended to the run's out file.
      ( codex fork "$session_id" "$prompt" \
          --output-format text \
          >> "$fork_out_dir/codex-fork.out" 2>>"$fork_out_dir/codex-fork.err" ) || true
    done <<<"$rows"

    # After the first SIGTERM + fork, exit the loop — the outer
    # wrapper handles the next dispatch cycle.
    break
  done
}

mid_node_injector_start() {
  local kind="${1:?kind required (claude|codex)}"
  local fifo_or_session_id_file="${2:?fifo_in or session_id_file required}"
  local run_id="${3:?run_id required}"
  local role="${4:?role required}"
  local poll_secs="${5:-5}"

  case "$kind" in
    claude)
      _mid_injector_claude_loop "$fifo_or_session_id_file" "$run_id" "$role" "$poll_secs" "$$" &
      ;;
    codex)
      _mid_injector_codex_loop "$fifo_or_session_id_file" "$run_id" "$role" "$poll_secs" "$$" &
      ;;
    *)
      echo "mid_node_injector_start: unknown kind: $kind" >&2
      return 2
      ;;
  esac
  local sidecar_pid=$!
  printf '%s\n' "$sidecar_pid" > "$(_mid_injector_pid_file)"
  echo "$sidecar_pid"
}

mid_node_injector_stop() {
  local pid_file
  pid_file="$(_mid_injector_pid_file)"
  [ -f "$pid_file" ] || return 0
  local pid
  pid="$(cat "$pid_file" 2>/dev/null)"
  [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
  rm -f "$pid_file"
}
