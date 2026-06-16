#!/usr/bin/env bash
# epic_graph.sh — cross-epic dependency graph for the autonomous scheduler.
#
# Public API:
#   epic_graph_add_dep    <from_id> <to_id> [kind]   register a dep edge
#   epic_graph_ready_now                              list epic_ids ready to dispatch
#   epic_graph_mark_ready <epic_id>                   flip 'blocked' → 'not started'
#   epic_graph_on_done    <epic_id>                   trigger cascade after a completion
#   epic_graph_deps_met   <epic_id>                   exit 0 if all hard deps resolved
#
# Status vocabulary (per epics table): 'not started', 'in progress', 'in review',
# 'done', 'blocked', 'escalated'.

[ "${0:-}" = "${BASH_SOURCE[0]:-}" ] && set -Eeuo pipefail

MINI_ORK_ROOT="${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DB="${MINI_ORK_DB:-${MINI_ORK_HOME:-.mini-ork}/state.db}"

# Register an edge: from_id must reach 'done' before to_id can dispatch.
# kind ∈ {hard, soft, informational}. Default: hard.
epic_graph_add_dep() {
  local from_id="${1:?from_epic_id required}"
  local to_id="${2:?to_epic_id required}"
  local kind="${3:-hard}"
  sqlite3 "$STATE_DB" \
    "INSERT OR IGNORE INTO epic_dependencies(from_epic_id, to_epic_id, kind)
     VALUES('$from_id','$to_id','$kind');" 2>/dev/null || return 1
}

# True (exit 0) when every hard dep of the given epic is resolved.
# Soft + informational deps do not block.
epic_graph_deps_met() {
  local epic_id="${1:?epic_id required}"
  local unmet
  unmet=$(sqlite3 "$STATE_DB" \
    "SELECT COUNT(*) FROM epic_dependencies
      WHERE to_epic_id='$epic_id'
        AND kind='hard'
        AND resolved_at IS NULL;" 2>/dev/null)
  [ "${unmet:-0}" = "0" ]
}

# Emit epic_ids whose status='not started' AND every hard dep is satisfied.
# Sort by created_at ASC so the oldest queued epic goes first (fairness).
epic_graph_ready_now() {
  python3 - "$STATE_DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
rows = con.execute("""
    SELECT e.id
      FROM epics e
     WHERE e.status = 'not started'
       AND e.archived_at IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM epic_dependencies d
            WHERE d.to_epic_id = e.id
              AND d.kind = 'hard'
              AND d.resolved_at IS NULL
       )
  ORDER BY e.created_at ASC
""").fetchall()
con.close()
for r in rows:
    print(r[0])
PY
}

# Flip a single epic from 'blocked' to 'not started' (idempotent — no-op
# if epic is already in any forward-progress state).
epic_graph_mark_ready() {
  local epic_id="${1:?epic_id required}"
  sqlite3 "$STATE_DB" \
    "UPDATE epics SET status='not started'
      WHERE id='$epic_id' AND status='blocked';" 2>/dev/null || true
}

# Call after an epic reaches 'done'. Resolves outgoing deps and unblocks
# any downstream epic whose remaining hard deps are now all met.
epic_graph_on_done() {
  local done_id="${1:?epic_id required}"
  # 1. Mark this epic's outgoing edges as resolved.
  sqlite3 "$STATE_DB" \
    "UPDATE epic_dependencies
        SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
      WHERE from_epic_id='$done_id' AND resolved_at IS NULL;" 2>/dev/null || true
  # 2. For each downstream epic of that edge, if it's blocked and now has
  #    no unresolved hard deps, flip to 'not started'.
  python3 - "$STATE_DB" "$done_id" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA busy_timeout=5000")
done_id = sys.argv[2]
downstream = [r[0] for r in con.execute(
    "SELECT DISTINCT to_epic_id FROM epic_dependencies WHERE from_epic_id=?",
    (done_id,)
).fetchall()]
for ep in downstream:
    unmet = con.execute(
        """SELECT COUNT(*) FROM epic_dependencies
            WHERE to_epic_id=? AND kind='hard' AND resolved_at IS NULL""",
        (ep,),
    ).fetchone()[0]
    if unmet == 0:
        con.execute(
            "UPDATE epics SET status='not started' WHERE id=? AND status='blocked'",
            (ep,),
        )
con.commit()
con.close()
PY
}

if [[ "${BASH_SOURCE[0]:-}" == "${0:-}" ]]; then
  echo "epic_graph.sh — source me and call epic_graph_add_dep / epic_graph_ready_now / epic_graph_on_done / epic_graph_deps_met / epic_graph_mark_ready"
fi
