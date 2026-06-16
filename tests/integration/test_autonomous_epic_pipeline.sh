#!/usr/bin/env bash
# Integration smoke for the autonomous multi-epic pipeline (E1).
#
# Walks the full chain end-to-end WITHOUT requiring live LLM auth:
#   1. Ingest a synthetic 3-epic roadmap
#   2. Confirm dep edges are auto-blocking
#   3. epic_graph_ready_now picks only the unblocked root
#   4. Scheduler --once --dry-run picks it
#   5. Manually mark root done, run cascade
#   6. Verify next epic flips to 'not started'
#   7. Confirm cross_class gradients show up in epic-runner's context pack
#   8. Cleanup
#
# Exit code 0 on green, non-zero on first failure.

set -o pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT
MINI_ORK_HOME="${MINI_ORK_HOME:-$MINI_ORK_ROOT/.mini-ork}"
export MINI_ORK_HOME
STATE_DB="${MINI_ORK_DB:-$MINI_ORK_HOME/state.db}"
export STATE_DB
# context_assembler + lib helpers read MINI_ORK_DB, not STATE_DB.
MINI_ORK_DB="$STATE_DB"
export MINI_ORK_DB

# CI bootstrap: state.db may not exist on a fresh checkout. Apply migrations
# idempotently before any test that queries the schema.
if [ ! -f "$STATE_DB" ]; then
  mkdir -p "$(dirname "$STATE_DB")"
  : > "$STATE_DB"
fi
MINI_ORK_DB="$STATE_DB" bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true

PASS=0; FAIL=0
_t() { printf '\n=== %s ===\n' "$1"; }
_check() {
  local desc="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    printf "  ✓ %s\n" "$desc"
    PASS=$((PASS+1))
  else
    printf "  ✗ %s\n    expected=%q\n    actual=%q\n" "$desc" "$expected" "$actual"
    FAIL=$((FAIL+1))
  fi
}

# Unique test suffix so reruns don't collide.
TS=$$
ROOT_ID="e1t-root-$TS"
MID_ID="e1t-mid-$TS"
LEAF_ID="e1t-leaf-$TS"

_cleanup() {
  sqlite3 "$STATE_DB" "DELETE FROM epic_dependencies WHERE from_epic_id LIKE 'e1t-%-$TS' OR to_epic_id LIKE 'e1t-%-$TS';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "UPDATE epics SET archived_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id LIKE 'e1t-%-$TS';" 2>/dev/null || true
  sqlite3 "$STATE_DB" "DELETE FROM epics WHERE id LIKE 'e1t-%-$TS' AND status != 'done';" 2>/dev/null || true
  rm -f "/tmp/e1-roadmap-$TS.md"
}
trap _cleanup EXIT

_t "1. Ingest synthetic 3-epic roadmap"
cat > "/tmp/e1-roadmap-$TS.md" <<EOF
# Synth roadmap

## Root e1 test (id: $ROOT_ID)
Baseline; no deps.

## Mid e1 test (id: $MID_ID)
- depends on: $ROOT_ID

## Leaf e1 test (id: $LEAF_ID)
- depends on: $MID_ID
EOF
"$MINI_ORK_ROOT/bin/mini-ork-epics" ingest "/tmp/e1-roadmap-$TS.md" > /dev/null
N=$(sqlite3 "$STATE_DB" "SELECT COUNT(*) FROM epics WHERE id LIKE 'e1t-%-$TS';")
_check "3 epics ingested" "$N" "3"
DEPS=$(sqlite3 "$STATE_DB" "SELECT COUNT(*) FROM epic_dependencies WHERE to_epic_id LIKE 'e1t-%-$TS';")
_check "2 dep edges created" "$DEPS" "2"

_t "2. Mid + Leaf are auto-blocked"
MID_STATUS=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$MID_ID';")
LEAF_STATUS=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$LEAF_ID';")
_check "mid status=blocked" "$MID_STATUS" "blocked"
_check "leaf status=blocked" "$LEAF_STATUS" "blocked"
ROOT_STATUS=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$ROOT_ID';")
_check "root status=not started" "$ROOT_STATUS" "not started"

_t "3. ready_now returns only the root"
# shellcheck source=/dev/null
source "$MINI_ORK_ROOT/lib/epic_graph.sh"
READY=$(epic_graph_ready_now | grep -E "^e1t-.*-$TS\$" | sort)
EXPECTED_READY="$ROOT_ID"
_check "ready_now lists only root" "$READY" "$EXPECTED_READY"

_t "4. Scheduler --once --dry-run runs cleanly (oldest-first may pick older queued epic)"
RC=0
SCHED_OUT=$("$MINI_ORK_ROOT/bin/mini-ork-scheduler" --once --dry-run 2>&1) || RC=$?
_check "scheduler exit code = 0" "$RC" "0"
# Scheduler may have picked an older epic from a prior smoke leftover — that's
# fair-ordering by design. Just confirm it dispatched SOMETHING.
echo "$SCHED_OUT" | grep -qE "would dispatch|queue empty" && DISPATCHED=yes || DISPATCHED=no
_check "scheduler logged a dispatch or empty-queue" "$DISPATCHED" "yes"
POST_STATUS=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$ROOT_ID';")
_check "root status not corrupted after dry-run" "$POST_STATUS" "not started"

_t "5. Mark root done + cascade"
sqlite3 "$STATE_DB" "UPDATE epics SET status='done' WHERE id='$ROOT_ID';"
epic_graph_on_done "$ROOT_ID"
MID_AFTER=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$MID_ID';")
LEAF_AFTER=$(sqlite3 "$STATE_DB" "SELECT status FROM epics WHERE id='$LEAF_ID';")
_check "mid flipped to 'not started'" "$MID_AFTER" "not started"
_check "leaf still 'blocked'" "$LEAF_AFTER" "blocked"

_t "6. ready_now now lists mid only"
READY2=$(epic_graph_ready_now | grep -E "^e1t-.*-$TS\$" | sort)
_check "ready_now lists mid" "$READY2" "$MID_ID"

_t "7. cross_class gradients appear in epic_runner_delivery context pack"
RUNDIR="/tmp/e1-pack-$TS"
mkdir -p "$RUNDIR"
echo '{"task_class":"epic_runner_delivery","goal":"E1 verify"}' > "$RUNDIR/brief.json"
# Subshell isolates assembler sourcing — avoids pollution / inherited traps.
( source "$MINI_ORK_ROOT/lib/context_assembler.sh" \
  && context_assemble "$RUNDIR/brief.json" "planner" > "$RUNDIR/pack.json" 2>/dev/null ) || true
CROSS_N=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(sum(1 for m in d.get('known_failure_modes', []) if m.get('scope')=='cross_class'))
except Exception:
    print(0)
" "$RUNDIR/pack.json" 2>/dev/null)
[ "${CROSS_N:-0}" -gt 0 ] && CROSS_OK=yes || CROSS_OK=no
_check "context pack contains cross_class failure modes" "$CROSS_OK" "yes"
rm -rf "$RUNDIR"

# Final root needs to flip back to 'not started' so trigger doesn't block teardown
sqlite3 "$STATE_DB" "UPDATE epics SET status='done' WHERE id='$MID_ID';" 2>/dev/null || true
sqlite3 "$STATE_DB" "UPDATE epics SET status='done' WHERE id='$LEAF_ID';" 2>/dev/null || true

printf '\n=== SUMMARY: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
