"""Recursive-orchestration — Python port of lib/recursive_orchestration.sh.

Faithful port of the six bash public functions that implement bounded
parent/child mini-ork lineage. The Python port gives callers an in-process
surface (no `python3` heredoc per call) and gives the parity test a stable
target to byte-diff against the live bash.

Co-existence model (strangler-fig): bash `lib/recursive_orchestration.sh`
is the authoritative source. This module mirrors its surfaces exactly.
Parity is enforced by `tests/unit/test_recursive_orchestration_py.py`
(>=6 cases that drive the LIVE bash subprocess against a temp DB seeded
by `db/init.sh` and diff the resulting `run_spawns` / `run_events` /
`run_artifact_edges` / `merge_decisions` rows against the Python port
byte-for-byte; floats 1e-6 on `authority_level`, epochs 1s tolerance,
event_id stem-equal).

Schema citations:
  - `run_spawns`            — db/migrations/0016_recursive_orchestration.sql
  - `run_events`            — db/migrations/0016_recursive_orchestration.sql
  - `run_artifact_edges`    — db/migrations/0016_recursive_orchestration.sql
  - `merge_decisions`       — db/migrations/0016_recursive_orchestration.sql
  - `task_runs`             — db/migrations/0013_task_runs.sql
                               (UPSERT target for approve_spawn)

Pipeline map (bash function → Python):
  mo_recursive_policy_json    → mo_recursive_policy_json      (env → dict, sort_keys)
  mo_recursive_emit_event     → mo_recursive_emit_event       (validate JSON → INSERT run_events)
  mo_recursive_approve_spawn  → mo_recursive_approve_spawn    (transactional: validate gates → counts → INSERT run_spawns + run_events + task_runs UPSERT)
  mo_recursive_mark_spawn     → mo_recursive_mark_spawn       (UPDATE run_spawns.status, raise on 0 rows)
  mo_recursive_record_artifact→ mo_recursive_record_artifact  (INSERT run_artifact_edges)
  mo_recursive_merge_decision → mo_recursive_merge_decision   (INSERT merge_decisions + conditional UPDATE run_spawns.status)

Internal helpers (mirrors of bash underscore-prefixed functions):
  _mo_recursive_root  → _root         (retained for parity; not consumed)
  _mo_recursive_db    → _resolve_db   (env precedence: MINI_ORK_DB > MINI_ORK_HOME/state.db > .mini-ork/state.db)
  _mo_recursive_uuid  → _build_id     (seconds epoch in middle segment)
  _mo_recursive_bool  → _bool_int     (1|true|yes|on → 1; everything else → 0)

ID format mirror (mirrors bash `_mo_recursive_uuid`):
    f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:12]}"
Seconds resolution is mandatory — `event_id` parity vs bash depends on the
middle segment being `int(time.time())`, not nanoseconds.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

__all__ = [
    "mo_recursive_policy_json",
    "mo_recursive_emit_event",
    "mo_recursive_approve_spawn",
    "mo_recursive_mark_spawn",
    "mo_recursive_record_artifact",
    "mo_recursive_merge_decision",
]


# Mirrors lib/recursive_orchestration.sh:18 (`_mo_recursive_db`)
def _resolve_db() -> str:
    """Return the state.db path the bash script would pick.

    Resolution order (mirrors bash line 19):
      $MINI_ORK_DB → ${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db

    Unlike `_resolve_db` in `mo_node_events.py`, this port always returns a
    path (no `None` branch) because every bash public function in this
    module opens the DB unconditionally — there is no `state.db missing`
    silent no-op contract here.
    """
    env_db = os.environ.get("MINI_ORK_DB")
    if env_db:
        return env_db
    home = os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork")
    return os.path.join(home, "state.db")


# Mirrors lib/recursive_orchestration.sh:22 (`_mo_recursive_uuid`)
def _build_id(prefix: str) -> str:
    """Mirror bash `f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:12]}"`.

    Seconds resolution in the middle segment is required so the bash and
    Python `event_id`s share the `ev-<sec>-` stem during parity diff.
    """
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:12]}"


# Mirrors lib/recursive_orchestration.sh:30 (`_mo_recursive_bool`)
def _bool_int(value: str | int | None) -> int:
    """Mirror bash `_mo_recursive_bool`: 1|true|TRUE|yes|YES|on|ON → 1, else 0."""
    s = "" if value is None else str(value)
    if s in ("1", "true", "TRUE", "yes", "YES", "on", "ON"):
        return 1
    return 0


# Mirrors lib/recursive_orchestration.sh:37 (`mo_recursive_policy_json`)
def mo_recursive_policy_json() -> str:
    """Mirror bash `mo_recursive_policy_json` lines 37-51.

    Reads the same six env vars as the bash heredoc and emits
    `json.dumps(policy, sort_keys=True)` to stdout (well, returns the
    string). The Python port returns the string directly so callers don't
    have to capture subprocess output.
    """
    policy = {
        "max_depth": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DEPTH", "2")),
        "max_children_per_run": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_CHILDREN", "4")),
        "max_total_descendants": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_DESCENDANTS", "16")),
        "max_parallel_children": int(os.environ.get("MINI_ORK_RECURSIVE_MAX_PARALLEL", "4")),
        "default_allow_child_spawn": os.environ.get("MINI_ORK_ALLOW_CHILD_SPAWN", "0").lower()
        in {"1", "true", "yes", "on"},
        "default_authority_level": float(os.environ.get("MINI_ORK_CHILD_AUTHORITY", "0.3")),
    }
    return json.dumps(policy, sort_keys=True)


# Mirrors lib/recursive_orchestration.sh:53 (`mo_recursive_emit_event`)
def mo_recursive_emit_event(
    run_id: str,
    parent_run_id: str = "",
    event_type: str = "",
    payload_json: str = "",
) -> str:
    """Mirror bash `mo_recursive_emit_event <run> <parent> <type> <payload>`.

    Validates the payload parses as JSON (raises `ValueError` with the same
    phrase as bash's `SystemExit(f"invalid event payload JSON: {exc}")`),
    then INSERTs a single `run_events` row. Returns the generated `event_id`.

    Raises:
        ValueError: when `payload_json` is non-empty and not parseable JSON.
                    The error message mirrors bash's `invalid event payload
                    JSON: <exc>` phrasing exactly.
    """
    if not run_id:
        raise ValueError("run_id required")
    if not event_type:
        raise ValueError("event_type required")
    if payload_json:
        try:
            json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid event payload JSON: {exc}") from exc
    else:
        payload_json = "{}"

    db = _resolve_db()
    event_id = _build_id("ev")

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            INSERT INTO run_events(event_id, run_id, parent_run_id, event_type, payload_json)
            VALUES (?, ?, NULLIF(?, ''), ?, ?)
            """,
            (event_id, run_id, parent_run_id, event_type, payload_json),
        )
        con.commit()
    finally:
        con.close()
    return event_id


# Mirrors lib/recursive_orchestration.sh:87 (`mo_recursive_approve_spawn`)
def mo_recursive_approve_spawn(
    parent_run_id: str,
    child_run_id: str,
    recipe: str = "",
    kickoff_path: str = "",
    child_workspace: str = "",
    depth: int | str = 1,
    authority_level: float | str = 0.3,
    allow_child_spawn: int | str = 0,
) -> str:
    """Mirror bash `mo_recursive_approve_spawn` lines 87-230.

    Transactional contract (mirrors bash `BEGIN IMMEDIATE` boundary exactly):
      1. Validate gates: depth<=max_depth, authority in [0,1) with 1.0 raising
         an explicit future-human-approval gate message.
      2. SELECT parent from task_runs; raise if missing.
      3. COUNT children / descendants / running_children against policy caps.
      4. INSERT run_spawns with `status='approved'` + policy_snapshot_json.
      5. INSERT run_events with event_id `ev-<now>-<child_run_id>` (literal
         child_run_id segment — distinct from the uuid-based event_id of
         the generic emit_event helper).
      6. UPSERT task_runs with `task_class = (recipe or "generic").replace("-", "_")`,
         `status='classified'`. ON CONFLICT updates recipe/kickoff_path/updated_at.
      7. COMMIT.

    Returns the generated `spawn_id`.

    Raises:
        ValueError: on gate failure (depth>max, authority>=1.0, authority
                    out of [0,1], parent task_run missing, child cap hit,
                    descendant cap hit, parallel cap hit).
    """
    if not parent_run_id:
        raise ValueError("parent_run_id required")
    if not child_run_id:
        raise ValueError("child_run_id required")
    if not kickoff_path:
        raise ValueError("kickoff_path required")
    if not child_workspace:
        raise ValueError("child_workspace required")

    policy = json.loads(mo_recursive_policy_json())
    depth_i = int(depth)
    authority_f = float(authority_level)
    allow_child_spawn_i = _bool_int(allow_child_spawn)

    if depth_i > int(policy["max_depth"]):
        raise ValueError(
            f"spawn blocked: depth {depth_i} exceeds max_depth {policy['max_depth']}"
        )
    if authority_f >= 1.0:
        raise ValueError(
            "spawn blocked: authority_level 1.0 requires explicit future human approval gate"
        )
    if authority_f < 0.0 or authority_f > 1.0:
        raise ValueError(
            "spawn blocked: authority_level must be between 0.0 and 1.0"
        )

    db = _resolve_db()
    spawn_id = _build_id("sp")
    now = int(time.time())

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        try:
            parent = con.execute(
                "SELECT id FROM task_runs WHERE id=?", (parent_run_id,)
            ).fetchone()
            if parent is None:
                raise ValueError(
                    f"spawn blocked: parent task_run not found: {parent_run_id}"
                )

            parent_child_count = con.execute(
                "SELECT COUNT(*) FROM run_spawns WHERE parent_run_id=?",
                (parent_run_id,),
            ).fetchone()[0]
            if parent_child_count >= int(policy["max_children_per_run"]):
                raise ValueError(
                    f"spawn blocked: parent has {parent_child_count} children; "
                    f"max_children_per_run is {policy['max_children_per_run']}"
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
                raise ValueError(
                    f"spawn blocked: root has {descendant_count} descendants; "
                    f"max_total_descendants is {policy['max_total_descendants']}"
                )

            running_children = con.execute(
                "SELECT COUNT(*) FROM run_spawns WHERE parent_run_id=? AND status='running'",
                (parent_run_id,),
            ).fetchone()[0]
            if running_children >= int(policy["max_parallel_children"]):
                raise ValueError(
                    f"spawn blocked: parent has {running_children} running children; "
                    f"max_parallel_children is {policy['max_parallel_children']}"
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
                    depth_i,
                    recipe,
                    kickoff_path,
                    child_workspace,
                    authority_f,
                    allow_child_spawn_i,
                    mo_recursive_policy_json(),
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
                    json.dumps(
                        {
                            "spawn_id": spawn_id,
                            "depth": depth_i,
                            "recipe": recipe,
                            "authority_level": authority_f,
                        }
                    ),
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
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()
    return spawn_id


# Mirrors lib/recursive_orchestration.sh:232 (`mo_recursive_mark_spawn`)
_VALID_SPAWN_STATUSES = frozenset({
    "requested", "approved", "running", "completed",
    "failed", "blocked", "merged", "rejected",
})


def mo_recursive_mark_spawn(child_run_id: str, status: str) -> None:
    """Mirror bash `mo_recursive_mark_spawn <child> <status>` lines 232-250.

    Validates `status` is in the bash-allowed set; raises on invalid.
    UPDATE run_spawns SET status=?, updated_at=? WHERE child_run_id=?; if
    zero rows are affected, raises with the same phrase as bash.
    """
    if not child_run_id:
        raise ValueError("child_run_id required")
    if not status:
        raise ValueError("status required")
    if status not in _VALID_SPAWN_STATUSES:
        raise ValueError(f"invalid spawn status: {status}")

    db = _resolve_db()
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            "UPDATE run_spawns SET status=?, updated_at=? WHERE child_run_id=?",
            (status, int(time.time()), child_run_id),
        )
        if con.total_changes == 0:
            raise ValueError(f"spawn not found for child_run_id={child_run_id}")
        con.commit()
    finally:
        con.close()


# Mirrors lib/recursive_orchestration.sh:252 (`mo_recursive_record_artifact`)
def mo_recursive_record_artifact(
    producer_run_id: str,
    consumer_run_id: str,
    artifact_path: str,
    artifact_hash: str = "",
    artifact_kind: str = "file",
) -> str:
    """Mirror bash `mo_recursive_record_artifact` lines 252-277.

    INSERT a single row into `run_artifact_edges`. `artifact_hash` empty
    string is stored as NULL (mirrors bash `NULLIF(?, '')`). Returns the
    generated `edge_id`.
    """
    if not producer_run_id:
        raise ValueError("producer_run_id required")
    if not consumer_run_id:
        raise ValueError("consumer_run_id required")
    if not artifact_path:
        raise ValueError("artifact_path required")

    db = _resolve_db()
    edge_id = _build_id("ae")

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            INSERT INTO run_artifact_edges(
              edge_id, producer_run_id, consumer_run_id,
              artifact_path, artifact_hash, artifact_kind
            )
            VALUES (?, ?, ?, ?, NULLIF(?, ''), ?)
            """,
            (edge_id, producer_run_id, consumer_run_id, artifact_path,
             artifact_hash, artifact_kind),
        )
        con.commit()
    finally:
        con.close()
    return edge_id


# Mirrors lib/recursive_orchestration.sh:279 (`mo_recursive_merge_decision`)
_VALID_MERGE_DECISIONS = frozenset({"accepted", "rejected", "needs_changes", "deferred"})


def mo_recursive_merge_decision(
    parent_run_id: str,
    child_run_id: str,
    decision: str,
    reason: str = "",
    decided_by: str = "parent",
) -> str:
    """Mirror bash `mo_recursive_merge_decision` lines 279-311.

    Validates `decision` against the bash allow-list; INSERT into
    `merge_decisions` with `evidence_json='{"source": "mini-ork-spawn"}'`;
    conditional UPDATE of `run_spawns.status` ('merged' on accepted,
    'rejected' on rejected; no update otherwise). `updated_at` is set to
    `strftime('%s','now')` to mirror the bash heredoc exactly.
    Returns the generated `decision_id`.
    """
    if not parent_run_id:
        raise ValueError("parent_run_id required")
    if not child_run_id:
        raise ValueError("child_run_id required")
    if not decision:
        raise ValueError("decision required")
    if decision not in _VALID_MERGE_DECISIONS:
        raise ValueError(f"invalid merge decision: {decision}")

    db = _resolve_db()
    decision_id = _build_id("md")
    now = int(time.time())

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute(
            """
            INSERT INTO merge_decisions(
              decision_id, parent_run_id, child_run_id, decision,
              reason, decided_by, evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (decision_id, parent_run_id, child_run_id, decision, reason,
             decided_by, json.dumps({"source": "mini-ork-spawn"})),
        )
        if decision == "accepted":
            con.execute(
                "UPDATE run_spawns SET status='merged', updated_at=? WHERE child_run_id=?",
                (now, child_run_id),
            )
        elif decision == "rejected":
            con.execute(
                "UPDATE run_spawns SET status='rejected', updated_at=? WHERE child_run_id=?",
                (now, child_run_id),
            )
        con.commit()
    finally:
        con.close()
    return decision_id