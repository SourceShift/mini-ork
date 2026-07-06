#!/usr/bin/env bash
# mo-steer — push a steering message to a live mini-ork worker.
#
# Transport: append one JSONL line to <iter-dir>/STEER.jsonl. The worker's
# native fs.watch picks it up instantly and yields it as a new user
# message into the same SDK session.
#
# Liveness: refuses to send if <iter-dir>/HEARTBEAT is stale (>15s).
# Override with --force.
#
# Ack: after writing, optionally tail worker.log for a `steer_yielded`
# event with matching steer_id to confirm delivery (--wait-ack).
#
# Usage:
#   mo-steer <epic-id> <message>
#   mo-steer <epic-id> --from=reviewer < file.md
#   mo-steer EXPL-DLG-C "Skip the rail variant for this epic." --wait-ack
#   mo-steer EXPL-DLG-C --job=expl-dlg --from=user "stop and rebase first"
#
# Discovery: epic-id is sufficient if MO_JOB env is set or if the run dir
# can be inferred from CWD. Else pass --job=<job-id>.

set -euo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

usage() {
  cat >&2 <<USAGE
Usage:
  $0 <epic-id> [--job=<job-id>] [--iter=<N>] [--from=<source>] [--wait-ack] [--force] <message>
  $0 <epic-id> [--job=<job-id>] [--iter=<N>] [--from=<source>] [--wait-ack] [--force]   (reads message from stdin)

Examples:
  $0 EXPL-DLG-C "Skip rail variant for this epic." --wait-ack
  $0 EXPL-DLG-A --job=expl-dlg --from=reviewer < feedback.md
USAGE
  exit 2
}

[ $# -ge 1 ] || usage
EPIC="$1"; shift

JOB="${MO_JOB:-}"
ITER=""
FROM="${USER:-user}"
WAIT_ACK=0
FORCE=0
MSG=""

STEER_OVERRIDE=""
HB_OVERRIDE=""
LOG_OVERRIDE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --job=*)         JOB="${1#--job=}"; shift ;;
    --iter=*)        ITER="${1#--iter=}"; shift ;;
    --from=*)        FROM="${1#--from=}"; shift ;;
    --steer-file=*)  STEER_OVERRIDE="${1#--steer-file=}"; shift ;;
    --heartbeat=*)   HB_OVERRIDE="${1#--heartbeat=}"; shift ;;
    --log=*)         LOG_OVERRIDE="${1#--log=}"; shift ;;
    --wait-ack)      WAIT_ACK=1; shift ;;
    --force)         FORCE=1; shift ;;
    -h|--help)       usage ;;
    *)               MSG="${MSG:+$MSG }$1"; shift ;;
  esac
done

# Read from stdin if no inline message given.
if [ -z "$MSG" ]; then
  if [ -t 0 ]; then
    echo "ERROR: no message given on argv and stdin is a tty" >&2
    usage
  fi
  MSG="$(cat)"
fi

[ -n "$MSG" ] || { echo "ERROR: empty message" >&2; exit 2; }

# Resolve channel files — explicit overrides win; else derive from runs/<job>/<epic>/iter-N.
if [ -n "$STEER_OVERRIDE" ]; then
  STEER_FILE="$STEER_OVERRIDE"
  HEARTBEAT="${HB_OVERRIDE:-$(dirname "$STEER_FILE")/HEARTBEAT}"
  WORKER_LOG="${LOG_OVERRIDE:-$(dirname "$STEER_FILE")/worker.log}"
  echo "[mo-steer] using explicit steer file: $STEER_FILE" >&2
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  MINI_ORK_HOME="${MINI_ORK_HOME:-.mini-ork}"
  RUNS_DIR="$REPO_ROOT/$MINI_ORK_HOME/runs"
  if [ -z "$JOB" ]; then
    # Try to guess from epic prefix (e.g. EXPL-DLG-C → expl-dlg).
    JOB="$(echo "$EPIC" | sed -E 's/-[A-Z0-9]+$//' | tr '[:upper:]' '[:lower:]')"
    echo "[mo-steer] inferred job=$JOB from epic prefix" >&2
  fi
  EPIC_DIR="$RUNS_DIR/$JOB/$EPIC"
  [ -d "$EPIC_DIR" ] || { echo "ERROR: epic dir not found: $EPIC_DIR" >&2; exit 3; }
  if [ -z "$ITER" ]; then
    ITER_DIR=$(ls -1d "$EPIC_DIR"/iter-* 2>/dev/null | sort -V | tail -1)
  else
    ITER_DIR="$EPIC_DIR/iter-$ITER"
  fi
  [ -d "$ITER_DIR" ] || { echo "ERROR: iter dir not found: $ITER_DIR" >&2; exit 4; }
  STEER_FILE="$ITER_DIR/STEER.jsonl"
  HEARTBEAT="$ITER_DIR/HEARTBEAT"
  WORKER_LOG="$ITER_DIR/worker.log"
fi
# Ensure parent exists
mkdir -p "$(dirname "$STEER_FILE")"

# Liveness check via heartbeat.
if [ "$FORCE" -ne 1 ] && [ -f "$HEARTBEAT" ]; then
  # Heartbeat file's mtime — refuse if older than 15s.
  # GNU stat (`-c %Y`, Linux/CI) FIRST, then BSD stat (`-f %m`, macOS). The old
  # BSD-first order broke on Linux: GNU `stat -f %m` means *filesystem* status,
  # not mtime, and "succeeds" with non-numeric output that poisoned the
  # arithmetic below (`File: unbound variable` under set -u). Sanitize to 0.
  HB_MTIME=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || stat -f %m "$HEARTBEAT" 2>/dev/null || echo 0)
  case "$HB_MTIME" in ''|*[!0-9]*) HB_MTIME=0 ;; esac
  NOW=$(date +%s)
  AGE=$((NOW - HB_MTIME))
  if [ "$AGE" -gt 15 ]; then
    echo "ERROR: heartbeat stale (${AGE}s old) — worker likely dead" >&2
    echo "  $HEARTBEAT" >&2
    echo "  Override with --force if you really mean it." >&2
    exit 5
  fi
  HB_STATE=$(cat "$HEARTBEAT" 2>/dev/null | tail -1 | grep -oE '"state":"[^"]+"' | cut -d'"' -f4 || echo unknown)
  if [ "$HB_STATE" = "done" ] || [ "$HB_STATE" = "aborting" ]; then
    echo "ERROR: heartbeat says state=$HB_STATE — worker not accepting steer" >&2
    echo "  Override with --force." >&2
    exit 5
  fi
fi

# Generate steer message envelope.
ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "$(date +%s)-$$-$RANDOM")
AT=$(date -u +%FT%TZ)

# Atomic append — POSIX guarantees atomicity for writes < PIPE_BUF (~4KB).
MSG_LEN=${#MSG}
JSON=$(jq -nc \
  --arg id "$ID" \
  --arg at "$AT" \
  --arg from "$FROM" \
  --arg body "$MSG" \
  '{id:$id, at:$at, from:$from, body:$body}')

printf '%s\n' "$JSON" >> "$STEER_FILE"
echo "[mo-steer] wrote $MSG_LEN-byte steer (id=$ID) -> $STEER_FILE" >&2

if [ "$WAIT_ACK" -eq 1 ]; then
  echo "[mo-steer] waiting for steer_yielded ack (timeout 30s)..." >&2
  for i in $(seq 1 30); do
    if grep -q "\"event\":\"steer_yielded\".*\"steer_id\":\"$ID\"" "$WORKER_LOG" 2>/dev/null; then
      echo "[mo-steer] ack received in ${i}s — steer delivered + queued" >&2
      exit 0
    fi
    sleep 1
  done
  echo "[mo-steer] WARN: no ack within 30s — message in queue but unconfirmed" >&2
  exit 7
fi
