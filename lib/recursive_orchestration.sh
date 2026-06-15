#!/usr/bin/env bash
# lib/recursive_orchestration.sh — bounded parent/child mini-ork control plane.
#
# Public functions:
#   mo_recursive_policy_json
#   mo_recursive_emit_event <run_id> <parent_run_id> <event_type> <payload_json>
#   mo_recursive_approve_spawn <parent_run_id> <child_run_id> <recipe> <kickoff> <workspace> <depth> <authority> <allow_child_spawn>
#   mo_recursive_mark_spawn <child_run_id> <status>
#   mo_recursive_record_artifact <producer_run_id> <consumer_run_id> <path> [hash] [kind]
#   mo_recursive_merge_decision <parent_run_id> <child_run_id> <decision> <reason> [decided_by]

set -Eeuo pipefail

_mo_recursive_root() {
  printf '%s\n' "${MINI_ORK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
}

_mo_recursive_db() {
  printf '%s\n' "${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}"
}

_mo_recursive_uuid() {
  local prefix="${1:-id}"
  python3 - "$prefix" <<'PY'
import sys, time, uuid
print(f"{sys.argv[1]}-{int(time.time())}-{uuid.uuid4().hex[:12]}")
PY
}

_mo_recursive_bool() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES|on|ON) printf '1\n' ;;
    *) printf '0\n' ;;
  esac
}

mo_recursive_policy_json() {
  python3 - <<'PY'
import json, os

policy = {
    "max_depth": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DEPTH", "2")),
    "max_children_per_run": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_CHILDREN", "4")),
    "max_total_descendants": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DESCENDANTS", "16")),
    "max_parallel_children": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_PARALLEL", "4")),
    "default_allow_child_spawn": os.environ.get("MINI_ORK_ALLOW_CHILD_SPAWN", "0").lower() in {"1", "true", "yes", "on"},
    "default_authority_level": float(os.environ.get("MINI_ORK_CHILD_AUTHORITY", "0.3")),
}
print(json.dumps(policy, sort_keys=True))
PY
}

mo_recursive_emit_event() {
  local run_id="${1:?run_id required}"
  local parent_run_id="${2:-}"
  local event_type="${3:?event_type required}"
  local payload_json="${4:-}"
  [ -n "$payload_json" ] || payload_json="{}"
  local db="$(_mo_recursive_db)"
  local event_id
  event_id="$(_mo_recursive_uuid ev)"

  python3 - "$db" "$event_id" "$run_id" "$parent_run_id" "$event_type" "$payload_json" <<'PY'
import json, sqlite3, sys

db, event_id, run_id, parent_run_id, event_type, payload_json = sys.argv[1:7]
try:
    json.loads(payload_json)
except Exception as exc:
    raise SystemExit(f"invalid event payload JSON: {exc}")

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.execute(
    """
    INSERT INTO run_events(event_id, run_id, parent_run_id, event_type, payload_json)
    VALUES (?, ?, NULLIF(?, ''), ?, ?)
    """,
    (event_id, run_id, parent_run_id, event_type, payload_json),
)
con.commit()
con.close()
print(event_id)
PY

  # Outbound event-callback hook. Best-effort; cannot block dispatch.
  if ! declare -F _mo_emit_hook >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]}")/mo_emit_hook.sh" 2>/dev/null || return 0
  fi
  _mo_emit_hook "$event_type" "$run_id" "$payload_json"
}

mo_recursive_approve_spawn() {
  local parent_run_id="${1:?parent_run_id required}"
  local child_run_id="${2:?child_run_id required}"
  local recipe="${3:-}"
  local kickoff_path="${4:?kickoff_path required}"
  local child_workspace="${5:?child_workspace required}"
  local depth="${6:?depth required}"
  local authority_level="${7:-0.3}"
  local allow_child_spawn
  allow_child_spawn="$(_mo_recursive_bool "${8:-0}")"
  local db="$(_mo_recursive_db)"
  local policy_json
  policy_json="$(mo_recursive_policy_json)"
  local spawn_id
  spawn_id="$(_mo_recursive_uuid sp)"

  python3 - "$db" "$spawn_id" "$parent_run_id" "$child_run_id" "$recipe" "$kickoff_path" "$child_workspace" "$depth" "$authority_level" "$allow_child_spawn" "$policy_json" <<'PY'
import json, sqlite3, sys, time

(
    db,
    spawn_id,
    parent_run_id,
    child_run_id,
    recipe,
    kickoff_path,
    child_workspace,
    depth_raw,
    authority_raw,
    allow_child_spawn_raw,
    policy_json,
) = sys.argv[1:12]

policy = json.loads(policy_json)
depth = int(depth_raw)
authority = float(authority_raw)
allow_child_spawn = int(allow_child_spawn_raw)
now = int(time.time())

if depth > int(policy["max_depth"]):
    raise SystemExit(f"spawn blocked: depth {depth} exceeds max_depth {policy['max_depth']}")
if authority >= 1.0:
    raise SystemExit("spawn blocked: authority_level 1.0 requires explicit future human approval gate")
if authority < 0.0 or authority > 1.0:
    raise SystemExit("spawn blocked: authority_level must be between 0.0 and 1.0")

con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.execute("PRAGMA foreign_keys=ON")
con.execute("BEGIN IMMEDIATE")
try:
    parent = con.execute("SELECT id FROM task_runs WHERE id=?", (parent_run_id,)).fetchone()
    if parent is None:
        raise SystemExit(f"spawn blocked: parent task_run not found: {parent_run_id}")

    parent_child_count = con.execute(
        "SELECT COUNT(*) FROM run_spawns WHERE parent_run_id=?",
        (parent_run_id,),
    ).fetchone()[0]
    if parent_child_count >= int(policy["max_children_per_run"]):
        raise SystemExit(
            f"spawn blocked: parent has {parent_child_count} children; max_children_per_run is {policy['max_children_per_run']}"
        )

    root_row = con.execute(
        "SELECT root_run_id FROM run_spawns WHERE child_run_id=?",
        (parent_run_id,),
    ).fetchone()
    root_run_id = root_row[0] if root_row else parent_run_id
    descendant_count = con.execute(
        "SELECT COUNT(*) FROM run_spawns WHERE root_run_id=?",
        (root_run_id,),
    ).fetchone()[0]
    if descendant_count >= int(policy["max_total_descendants"]):
        raise SystemExit(
            f"spawn blocked: root has {descendant_count} descendants; max_total_descendants is {policy['max_total_descendants']}"
        )

    running_children = con.execute(
        "SELECT COUNT(*) FROM run_spawns WHERE parent_run_id=? AND status='running'",
        (parent_run_id,),
    ).fetchone()[0]
    if running_children >= int(policy["max_parallel_children"]):
        raise SystemExit(
            f"spawn blocked: parent has {running_children} running children; max_parallel_children is {policy['max_parallel_children']}"
        )

    con.execute(
        """
        INSERT INTO run_spawns(
          spawn_id, parent_run_id, child_run_id, root_run_id, depth, recipe,
          kickoff_path, child_workspace, authority_level, allow_child_spawn,
          status, policy_snapshot_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, 'approved', ?, ?, ?)
        """,
        (
            spawn_id,
            parent_run_id,
            child_run_id,
            root_run_id,
            depth,
            recipe,
            kickoff_path,
            child_workspace,
            authority,
            allow_child_spawn,
            policy_json,
            now,
            now,
        ),
    )
    con.execute(
        """
        INSERT INTO run_events(event_id, run_id, parent_run_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, 'spawn.approved', ?, ?)
        """,
        (
            f"ev-{now}-{child_run_id}",
            child_run_id,
            parent_run_id,
            json.dumps({"spawn_id": spawn_id, "depth": depth, "recipe": recipe, "authority_level": authority}),
            now,
        ),
    )
    task_class = (recipe or "generic").replace("-", "_")
    con.execute(
        """
        INSERT INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
        VALUES (?, ?, NULLIF(?, ''), ?, 'classified', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          recipe=COALESCE(excluded.recipe, task_runs.recipe),
          kickoff_path=excluded.kickoff_path,
          updated_at=excluded.updated_at
        """,
        (child_run_id, task_class, recipe, kickoff_path, now, now),
    )
    con.commit()
finally:
    con.close()

print(spawn_id)
PY
}

mo_recursive_mark_spawn() {
  local child_run_id="${1:?child_run_id required}"
  local status="${2:?status required}"
  local db="$(_mo_recursive_db)"
  python3 - "$db" "$child_run_id" "$status" <<'PY'
import sqlite3, sys, time
db, child_run_id, status = sys.argv[1:4]
valid = {"requested", "approved", "running", "completed", "failed", "blocked", "merged", "rejected"}
if status not in valid:
    raise SystemExit(f"invalid spawn status: {status}")
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.execute("UPDATE run_spawns SET status=?, updated_at=? WHERE child_run_id=?", (status, int(time.time()), child_run_id))
if con.total_changes == 0:
    raise SystemExit(f"spawn not found for child_run_id={child_run_id}")
con.commit()
con.close()
PY
}

mo_recursive_record_artifact() {
  local producer_run_id="${1:?producer_run_id required}"
  local consumer_run_id="${2:?consumer_run_id required}"
  local artifact_path="${3:?artifact_path required}"
  local artifact_hash="${4:-}"
  local artifact_kind="${5:-file}"
  local db="$(_mo_recursive_db)"
  local edge_id
  edge_id="$(_mo_recursive_uuid ae)"
  python3 - "$db" "$edge_id" "$producer_run_id" "$consumer_run_id" "$artifact_path" "$artifact_hash" "$artifact_kind" <<'PY'
import sqlite3, sys
db, edge_id, producer, consumer, path, digest, kind = sys.argv[1:8]
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.execute(
    """
    INSERT INTO run_artifact_edges(edge_id, producer_run_id, consumer_run_id, artifact_path, artifact_hash, artifact_kind)
    VALUES (?, ?, ?, ?, NULLIF(?, ''), ?)
    """,
    (edge_id, producer, consumer, path, digest, kind),
)
con.commit()
con.close()
print(edge_id)
PY
}

mo_recursive_merge_decision() {
  local parent_run_id="${1:?parent_run_id required}"
  local child_run_id="${2:?child_run_id required}"
  local decision="${3:?decision required}"
  local reason="${4:-}"
  local decided_by="${5:-parent}"
  local db="$(_mo_recursive_db)"
  local decision_id
  decision_id="$(_mo_recursive_uuid md)"
  python3 - "$db" "$decision_id" "$parent_run_id" "$child_run_id" "$decision" "$reason" "$decided_by" <<'PY'
import json, sqlite3, sys
db, decision_id, parent, child, decision, reason, decided_by = sys.argv[1:8]
valid = {"accepted", "rejected", "needs_changes", "deferred"}
if decision not in valid:
    raise SystemExit(f"invalid merge decision: {decision}")
con = sqlite3.connect(db)
con.execute("PRAGMA busy_timeout=5000")
con.execute(
    """
    INSERT INTO merge_decisions(decision_id, parent_run_id, child_run_id, decision, reason, decided_by, evidence_json)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
    (decision_id, parent, child, decision, reason, decided_by, json.dumps({"source": "mini-ork-spawn"})),
)
if decision == "accepted":
    con.execute("UPDATE run_spawns SET status='merged', updated_at=strftime('%s','now') WHERE child_run_id=?", (child,))
elif decision == "rejected":
    con.execute("UPDATE run_spawns SET status='rejected', updated_at=strftime('%s','now') WHERE child_run_id=?", (child,))
con.commit()
con.close()
print(decision_id)
PY
}
