"""Auto-register the 5 oracle gates at framework boot — Python port of lib/gate_bootstrap.sh.

Faithful port of mo_bootstrap_oracle_gates. Registers the 5 oracle gates
(coalition, panel-health, synthesis-promote, stability, liveness) into the
gate_registry table if not already present. Idempotent. Fail-open — rc=0
even on partial failures (matches bash semantics).

Two-phase insert-then-rename mirrors the bash sequence exactly so row-diff
parity can be asserted against the live bash via sha256 row-dump equality:
  (a) INSERT 5 candidate rows with UUID-suffixed gate_ids (matches the bash
      gate_register output: gate-custom-<hex8>), gate_type='custom',
      task_class_filter='' initially, safety per the bash roster
      (coalition/panel-health/synthesis-promote/liveness=1; stability=0),
      condition=<root>/gates/<name>.sh
  (b) UPDATE OR IGNORE the 5 newly-inserted rows to stable oracle-* IDs,
      DELETE the UUID rows
  (c) UPDATE task_class_filter=NULL for all oracle-* rows (so gate_list's
      "task_class_filter IS NULL OR task_class_filter=?" treats NULL as
      "applies to ALL task_classes")
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid


_DDL = """
    CREATE TABLE IF NOT EXISTS gate_registry (
        gate_id             TEXT PRIMARY KEY,
        gate_type           TEXT NOT NULL,
        condition           TEXT NOT NULL,
        task_class_filter   TEXT,
        safety              INTEGER NOT NULL DEFAULT 0,
        active              INTEGER NOT NULL DEFAULT 1,
        registered_at       INTEGER NOT NULL
    )
"""

_ROSTER = (
    # (gate_script_basename, safety_flag)
    ("coalition.sh", 1),
    ("panel-health.sh", 1),
    ("synthesis-promote.sh", 1),
    ("stability.sh", 0),
    ("liveness.sh", 1),
)

_STABLE_IDS = {
    "coalition.sh": "oracle-coalition",
    "panel-health.sh": "oracle-panel-health",
    "synthesis-promote.sh": "oracle-synthesis-promote",
    "stability.sh": "oracle-stability",
    "liveness.sh": "oracle-liveness",
}


def bootstrap_oracle_gates(db: str | None = None,
                           root: str | None = None) -> int:
    """mo_bootstrap_oracle_gates — register the 5 oracle gates if missing.

    Args:
        db:  Path to SQLite state DB. Falls back to $MINI_ORK_DB.
        root: Path to mini-ork repo root. Falls back to $MINI_ORK_ROOT.

    Returns:
        Always 0 (fail-open). Reads $MINI_ORK_DB and $MINI_ORK_ROOT from
        the env when not provided explicitly.
    """
    if db is None:
        db = os.environ.get("MINI_ORK_DB", "")
    if root is None:
        root = os.environ.get("MINI_ORK_ROOT", "")
    try:
        if not db or not os.path.isfile(db):
            return 0
        if not root:
            return 0
        con = sqlite3.connect(db)
        try:
            con.execute(_DDL)
            cur = con.execute(
                "SELECT COUNT(*) FROM gate_registry WHERE gate_id LIKE 'oracle-%'"
            ).fetchone()
            if (cur[0] if cur else 0) >= 5:
                return 0
            now = int(time.time())
            for basename, safety in _ROSTER:
                cond = f"{root}/gates/{basename}"
                gid = f"gate-custom-{uuid.uuid4().hex[:8]}"
                con.execute(
                    "INSERT OR IGNORE INTO gate_registry "
                    "(gate_id, gate_type, condition, task_class_filter, "
                    " safety, active, registered_at) "
                    "VALUES (?, 'custom', ?, '', ?, 1, ?)",
                    (gid, cond, int(safety), now),
                )
            for basename in _STABLE_IDS:
                new_id = _STABLE_IDS[basename]
                cond = f"{root}/gates/{basename}"
                rows = con.execute(
                    "SELECT gate_id FROM gate_registry WHERE condition=? "
                    "AND gate_id NOT LIKE 'oracle-%'", (cond,)
                ).fetchall()
                for (old_id,) in rows:
                    con.execute(
                        "UPDATE OR IGNORE gate_registry SET gate_id=? "
                        "WHERE gate_id=?", (new_id, old_id)
                    )
                    con.execute(
                        "DELETE FROM gate_registry WHERE gate_id=?",
                        (old_id,),
                    )
            con.execute(
                "UPDATE gate_registry SET task_class_filter=NULL "
                "WHERE gate_id LIKE 'oracle-%' AND task_class_filter=''"
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        return 0
    return 0