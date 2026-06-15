#!/usr/bin/env bash
# examples/event-hooks/fifo.sh — push mini-ork events into a named pipe.
#
# Usage:
#   mkfifo /tmp/mini-ork.fifo
#   export MINI_ORK_EVENT_FIFO=/tmp/mini-ork.fifo
#   export MINI_ORK_ON_EVENT=$PWD/examples/event-hooks/fifo.sh
#
# In another shell (or supervisor process):
#   tail -F /tmp/mini-ork.fifo | jq -c
#
# Each event lands as one line:
#   {"ts":"2026-06-15T09:47:00Z","event":"node_end","run":"run-...","payload":{...}}
#
# Best for: single supervisor consuming events live. Non-blocking write —
# if no reader is attached the event is dropped (FIFO semantics).

set -uo pipefail

EVENT_TYPE="${1:-unknown}"
RUN_ID="${2:-}"
PAYLOAD="${3:-{\}}"
FIFO="${MINI_ORK_EVENT_FIFO:-/tmp/mini-ork.fifo}"

[ -p "$FIFO" ] || exit 0  # silent skip when no FIFO exists

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LINE="{\"ts\":\"$TS\",\"event\":\"$EVENT_TYPE\",\"run\":\"$RUN_ID\",\"payload\":$PAYLOAD}"

# Non-blocking write — if no reader, drop the event rather than wedge.
printf '%s\n' "$LINE" >"$FIFO" 2>/dev/null &
disown 2>/dev/null || true
exit 0
