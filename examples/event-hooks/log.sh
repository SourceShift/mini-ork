#!/usr/bin/env bash
# examples/event-hooks/log.sh — append mini-ork events to a JSONL file.
#
# Usage:
#   export MINI_ORK_EVENT_LOG=$HOME/.mini-ork-events.jsonl
#   export MINI_ORK_ON_EVENT=$PWD/examples/event-hooks/log.sh
#
# Best for: offline replay, debugging, simple time-series export, multiple
# concurrent readers via `tail -F`. Atomic line-append works as long as
# each event fits in PIPE_BUF (≥512 bytes on every Unix).

set -uo pipefail

EVENT_TYPE="${1:-unknown}"
RUN_ID="${2:-}"
PAYLOAD="${3:-{\}}"
LOG="${MINI_ORK_EVENT_LOG:-$HOME/.mini-ork-events.jsonl}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

printf '{"ts":"%s","event":"%s","run":"%s","payload":%s}\n' \
  "$TS" "$EVENT_TYPE" "$RUN_ID" "$PAYLOAD" >>"$LOG" 2>/dev/null || true

exit 0
