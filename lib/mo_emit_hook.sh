#!/usr/bin/env bash
# lib/mo_emit_hook.sh — outbound event-callback hook.
#
# Purpose: turn mini-ork's DB-only event emission into a push channel for
# external observers (dashboards, CI bridges, agent supervisors, chat bots,
# anything that wants to react in real-time instead of polling
# run_events / mo_events).
#
# The hook is a single shell command supplied via the MINI_ORK_ON_EVENT
# env var. Mini-ork calls it best-effort after every DB write with three
# positional args:
#
#   $1  event_type   e.g. "node_start" | "node_end" | "child.completed"
#   $2  run_id       e.g. "run-1781509524-39638"
#   $3  payload_json e.g. {"node_id":"planner","node_type":"planner"}
#
# Failure mode: any non-zero exit, timeout, or missing command is logged
# to stderr and SWALLOWED. Observability must never break dispatch.
#
# Example use:
#   export MINI_ORK_ON_EVENT="$HOME/.config/mini-ork/event-hook.sh"
#   bin/mini-ork run code-fix kickoffs/my-fix.md
#
# Reference handlers live under examples/event-hooks/:
#   - fifo.sh    — append to a named pipe; reader does `tail -F` or `cat`
#   - webhook.sh — POST JSON to a URL; great for chat / dashboards
#   - log.sh     — append to a JSONL file for offline replay
#
# See docs/EVENT-HOOKS.md for the full contract.

# Internal: invoke the user's hook script with a hard timeout so a runaway
# handler can't wedge a dispatch. Silent no-op when MINI_ORK_ON_EVENT
# is unset.
_mo_emit_hook() {
  local hook="${MINI_ORK_ON_EVENT:-}"
  [ -n "$hook" ] || return 0

  local event_type="${1:-unknown}"
  local run_id="${2:-}"
  local payload_json="${3:-{\}}"

  # Resolve to absolute path if it looks like a relative file ref.
  if [[ "$hook" =~ ^[^/].*\.sh$ ]] && [ -f "$hook" ]; then
    hook="$(cd "$(dirname "$hook")" && pwd)/$(basename "$hook")"
  fi

  # Hard-cap the hook at 5s — anything slower belongs in a daemon.
  # gtimeout (GNU coreutils on macOS) preferred; fall back to timeout.
  local timeout_bin=""
  if command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="gtimeout"
  elif command -v timeout >/dev/null 2>&1; then
    timeout_bin="timeout"
  fi

  if [ -n "$timeout_bin" ]; then
    "$timeout_bin" 5 "$hook" "$event_type" "$run_id" "$payload_json" \
      >/dev/null 2>&1 || true
  else
    "$hook" "$event_type" "$run_id" "$payload_json" \
      >/dev/null 2>&1 || true
  fi
}
