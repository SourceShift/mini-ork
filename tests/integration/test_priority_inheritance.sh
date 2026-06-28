#!/usr/bin/env bash
# Integration regression for Track B5 — priority inheritance.
#
# Scenario:
#   * E_LOW  (base prio 10) is the holder — no deps.
#   * E_HIGH (base prio 100) is blocked on E_LOW — the high-priority waiter.
#   * E_MED  (base prio 50)  is an independent requester, ready to dispatch.
#
# Acceptance:
#   1. While E_HIGH is blocked on E_LOW, E_LOW's *effective* priority is 100
#      (it inherits from the highest-priority waiter). It falls back to its
#      own base (10) after release.
#   2. The scheduler's next-pick never returns E_MED before E_HIGH once E_LOW
#      flips to 'done' and the cascade unblocks E_HIGH.
#   3. A medium-priority requester with no blocked ancestors cannot outrun a
#      high-priority waiter — the inheritance + tiebreak guarantee this.
#
# Uses an isolated state.db under $TMPDIR so the test never touches the real
# mini-ork home. The schema is bootstrapped via db/init.sh then priority +
# priority-inheritance logic is exercised through bin/mini-ork-epics and the
# scheduler's _pick_next_epic equivalent CTE.

set -o pipefail

MINI_ORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export MINI_ORK_ROOT

# Isolated DB — never read/write the real .mini-ork home.
TMPDIR_B5="$(mktemp -d -t b5-prio-XXXXXX)"
trap 'rm -rf "$TMPDIR_B5"' EXIT
export MINI_ORK_HOME="$TMPDIR_B5"
STATE_DB="$TMPDIR_B5/state.db"
export MINI_ORK_DB="$STATE_DB"
export STATE_DB

mkdir -p "$TMPDIR_B5"
: > "$STATE_DB"
MINI_ORK_DB="$STATE_DB" bash "$MINI_ORK_ROOT/db/init.sh" >/dev/null 2>&1 || true

# Trigger the idempotent priority-column migration in bin/mini-ork-epics before
# any query reads it. The migration is also run on every scheduler/epics
# invocation in production, so this mirrors real boot order.
"$MINI_ORK_ROOT/bin/mini-ork-epics" list >/dev/null 2>&1 || true

PASS=0; FAIL=0
_t() { printf '\n=== %s ===\n' "$1"; }
_check() {
  local desc="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    printf "  PASS  %s\n" "$desc"
    PASS=$((PASS+1))
  else
    printf "  FAIL  %s\n    expected=%s\n    actual=%s\n" "$desc" "$expected" "$actual"
    FAIL=$((FAIL+1))
  fi
}

# Insert fixture epics directly — gives us total control over created_at order
# so the created_at tiebreaker can be exercised deterministically.
sqlite3 "$STATE_DB" <<'SQL'
INSERT INTO epics(id, title, status, priority, created_at) VALUES
  ('e5-low',  'low priority holder',  'not started', 10,
     strftime('%Y-%m-%dT%H:%M:%fZ','now','-3 seconds')),
  ('e5-high', 'high priority waiter','blocked',     100,
     strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 seconds')),
  ('e5-med',  'medium priority requester','not started', 50,
     strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 seconds'));
INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind) VALUES
  ('e5-low', 'e5-high', 'hard');
SQL

# The inline ALTER inside bin/mini-ork-epics is what installs the priority
# column on real schemas that pre-date Track B5 — exercise that path here so
# the test fails loudly if the migration drifts.
"$MINI_ORK_ROOT/bin/mini-ork-epics" list >/dev/null

# CTE mirroring the scheduler's _pick_next_epic, callable from the test.
_effective_priority() {
  python3 - "$STATE_DB" "$1" <<'PY' 2>/dev/null
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
epic_id = sys.argv[2]
row = con.execute("""
    WITH RECURSIVE inheritors(node) AS (
        SELECT id FROM epics WHERE id = ?
        UNION
        SELECT d.to_epic_id
          FROM inheritors i
          JOIN epic_dependencies d ON d.from_epic_id = i.node
         WHERE d.kind = 'hard' AND d.resolved_at IS NULL
    )
    SELECT COALESCE(MAX(e.priority), 0) AS eff
      FROM inheritors i JOIN epics e ON e.id = i.node
""", (epic_id,)).fetchone()
print(int(row[0]) if row and row[0] is not None else 0)
PY
}

_pick_next() {
  python3 - "$STATE_DB" <<'PY' 2>/dev/null
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
rows = con.execute("""
    WITH RECURSIVE inheritors(root, node) AS (
        SELECT e.id, e.id FROM epics e
         WHERE e.status = 'not started'
           AND e.archived_at IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM epic_dependencies d
                WHERE d.to_epic_id = e.id
                  AND d.kind = 'hard'
                  AND d.resolved_at IS NULL
           )
        UNION
        SELECT i.root, d.to_epic_id
          FROM inheritors i
          JOIN epic_dependencies d ON d.from_epic_id = i.node
         WHERE d.kind = 'hard' AND d.resolved_at IS NULL
    ),
    effective(root, eff) AS (
        SELECT root, COALESCE(MAX(e.priority), 0)
          FROM inheritors i JOIN epics e ON e.id = i.node
         GROUP BY root
    )
    SELECT e.id
      FROM epics e
      JOIN effective ef ON ef.root = e.id
     WHERE e.status = 'not started'
       AND e.archived_at IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM epic_dependencies d
            WHERE d.to_epic_id = e.id
              AND d.kind = 'hard'
              AND d.resolved_at IS NULL
       )
     ORDER BY ef.eff DESC, e.created_at ASC
     LIMIT 1
""").fetchall()
for r in rows:
    print(r[0])
PY
}

_t "1. effective priority while waiter is blocked"
EFF_LOW=$(_effective_priority e5-low)
EFF_HIGH=$(_effective_priority e5-high)
EFF_MED=$(_effective_priority e5-med)
_check "low holder inherits 100 from blocked high waiter"  "$EFF_LOW"  "100"
_check "high waiter effective priority is its own base"    "$EFF_HIGH" "100"
_check "medium requester effective priority is its own"    "$EFF_MED"  "50"

_t "2. set/inspect via bin/mini-ork-epics priority"
PRI=$("$MINI_ORK_ROOT/bin/mini-ork-epics" priority e5-low | tr -d ' ')
_check "epics priority subcommand returns current value" \
      "$(printf '%s' "$PRI" | tr '|' ' ' | awk '{print $2}')" "priority=10"
"$MINI_ORK_ROOT/bin/mini-ork-epics" priority e5-low 25 >/dev/null
PRI_AFTER=$("$MINI_ORK_ROOT/bin/mini-ork-epics" priority e5-low | tr '|' ' ' | awk '{print $2}')
_check "epics priority subcommand sets value"  "$PRI_AFTER" "priority=25"
# Reset so the rest of the test sees the original scenario.
"$MINI_ORK_ROOT/bin/mini-ork-epics" priority e5-low 10 >/dev/null

_t "3. inheritance clears once the waiter is released"
# Resolving the hard dep (cascade) is what releases the holder's inheritance
# — flipping the waiter's status alone does not, the edge is still open.
sqlite3 "$STATE_DB" "
  UPDATE epic_dependencies
     SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
   WHERE from_epic_id='e5-low' AND to_epic_id='e5-high';
  UPDATE epics SET status='not started' WHERE id='e5-high';
"
EFF_LOW_AFTER=$(_effective_priority e5-low)
EFF_HIGH_AFTER=$(_effective_priority e5-high)
_check "low holder falls back to its own base (10)"  "$EFF_LOW_AFTER"  "10"
_check "high waiter effective priority is its own"    "$EFF_HIGH_AFTER" "100"

_t "4. medium-priority requester cannot outrun high-priority waiter"
# Reset fixture: re-open the dep so the inheritance re-applies for this check.
sqlite3 "$STATE_DB" "
  UPDATE epic_dependencies SET resolved_at = NULL
   WHERE from_epic_id='e5-low' AND to_epic_id='e5-high';
  UPDATE epics SET status='blocked' WHERE id='e5-high';
  UPDATE epics SET status='not started' WHERE id='e5-low';
"
# Both e5-low and e5-med are ready. The pick should choose e5-low first
# because e5-low's effective priority (100, inherited) beats e5-med's 50.
PICK=$(_pick_next)
_check "priority-aware pick chooses the holder over medium requester" \
      "$PICK" "e5-low"

_t "5. after release, the highest-priority waiter is dispatched first"
# Simulate the holder finishing + cascade. Resolve the dep + flip downstream.
sqlite3 "$STATE_DB" "
  UPDATE epic_dependencies
     SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
   WHERE from_epic_id='e5-low' AND to_epic_id='e5-high';
  UPDATE epics SET status='done' WHERE id='e5-low';
"
sqlite3 "$STATE_DB" "UPDATE epics SET status='not started' WHERE id='e5-high';"
PICK2=$(_pick_next)
_check "cascade pick returns the high-priority waiter" \
      "$PICK2" "e5-high"

_t "6. priority CLI refuses non-integer"
set +e
OUT=$("$MINI_ORK_ROOT/bin/mini-ork-epics" priority e5-low notanint 2>&1)
RC=$?
set -e
_check "non-integer priority rejected"  "$RC" "2"

printf '\n=== SUMMARY: %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
