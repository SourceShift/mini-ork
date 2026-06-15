#!/usr/bin/env bash
# examples/event-hooks/webhook.sh — POST mini-ork events to an HTTP URL.
#
# Usage:
#   export MINI_ORK_WEBHOOK_URL=https://your.dashboard/ingest
#   export MINI_ORK_ON_EVENT=$PWD/examples/event-hooks/webhook.sh
#
# Receiver gets POST {"event":"node_end","run":"...","payload":{...}}.
# Best for: chat bots, dashboards, external incident systems.
#
# Uses curl with a 4s connect + 4s total timeout so a slow / dead URL
# can't drag the dispatch loop down. Failures swallow silently.

set -uo pipefail

EVENT_TYPE="${1:-unknown}"
RUN_ID="${2:-}"
PAYLOAD="${3:-{\}}"
URL="${MINI_ORK_WEBHOOK_URL:-}"

[ -n "$URL" ] || exit 0
command -v curl >/dev/null 2>&1 || exit 0

BODY="{\"event\":\"$EVENT_TYPE\",\"run\":\"$RUN_ID\",\"payload\":$PAYLOAD}"

curl -sS -o /dev/null \
  --connect-timeout 4 \
  --max-time 4 \
  -H "Content-Type: application/json" \
  -X POST -d "$BODY" "$URL" 2>/dev/null || true
exit 0
