"""Auto-register the oracle gates at framework boot — Python port of lib/gate_bootstrap.sh.

Registers the 5 oracle gates (coalition, panel-health, synthesis-promote,
stability, liveness) into the gate_registry table if not already present, plus
the mutation-adversary gate (see ``_MUTATION_GATE_ID``; outside the oracle-*
family, and applicable to every task class). Idempotent. Fail-open — rc=0 even
on partial failures (matches bash semantics).

WS4 (bash-removal): conditions are now ``native:<name>`` sentinels that
``gate_registry`` maps to the in-process evaluators in
``mini_ork.gates.native_gates`` — the production gate path no longer
executes the ``gates/*.sh`` shims. Live DBs whose oracle-* rows still
point at ``<root>/gates/<name>.sh`` keep working: the registry resolves
those script basenames to the same native evaluators (the script is never
executed for a recognized oracle gate), so no DB migration is required.

Two-phase insert-then-rename mirrors the bash sequence exactly:
  (a) INSERT 5 candidate rows with UUID-suffixed gate_ids (matches the bash
      gate_register output: gate-custom-<hex8>), gate_type='custom',
      task_class_filter='' initially, safety per the bash roster
      (coalition/panel-health/synthesis-promote/liveness=1; stability=0),
      condition=native:<name>
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


from mini_ork.gates.native_gates import native_condition

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
    # (native_gate_name, safety_flag)
    ("coalition", 1),
    ("panel-health", 1),
    ("synthesis-promote", 1),
    ("stability", 0),
    ("liveness", 1),
)

_STABLE_IDS = {
    "coalition": "oracle-coalition",
    "panel-health": "oracle-panel-health",
    "synthesis-promote": "oracle-synthesis-promote",
    "stability": "oracle-stability",
    "liveness": "oracle-liveness",
}

# The mutation-adversary gate is seeded OUTSIDE the oracle-* family, and it
# applies to every task class.
#
# It used to be scoped to ``task_class_filter='mutation-adversary'`` for one
# reason: ``gate_run_all`` counted every non-pass verdict as ``all_pass=False``
# and ``cli/verify.py`` read that as a failing gate, so a gate that deferred
# when no campaign ran would push a healthy verify from ``pass`` to
# ``partial``. The 5 oracle gates escaped that only because they defer
# *together* when a run has no panel evidence yet; a sixth deferring alone was
# a regression, not a check.
#
# That conflation is fixed at the source: ``gate_run_all`` now reports
# ``any_fail`` separately from ``any_defer`` and ``verify.py`` reads the failure
# signal, so an unrun check no longer reads as a failed one. With that gone the
# scoping has nothing left to do, and the gate joins real task classes — which
# is the whole point, since a coverage gap in the suite is a property of the
# *recipe's* verifier, not of one task class.
#
# ``safety=0`` is unchanged: the kill rate is a measurement, not a publish
# blocker. A low kill rate surfaces in ``verify``'s verdict without gaining the
# power to refuse a publish.
_MUTATION_GATE_ID = "mutation-adversary-gate"
_MUTATION_GATE_NAME = "mutation-adversary"
#: The filter earlier seeds used. Kept only to repair those rows in place.
_MUTATION_GATE_LEGACY_FILTER = "mutation-adversary"

# The step-rules gate (VPRMs, ``step_rules.py``) is seeded the same way and for
# the same reason: it is a property of the artifact, not of a task class, so it
# is scoped to NULL and joins every run. It is the rule-based counterpart to the
# mutation campaign — where that one measures whether the suite *would* catch a
# wrong patch, this one looks a wrong patch in the face (a diff that will not
# apply, a verifier that names a file the target does not have) with no model in
# the loop and no sample to average over.
#
# ``safety=0`` matches: a rule failing is a finding about the artifact that the
# verdict must show, not a veto over publishing. The rules cover only steps
# somebody wrote a rule for, and a rule with no input defers, so this gate spends
# most of its life unmeasured and says so.
_STEP_RULES_GATE_ID = "step-rules-gate"
_STEP_RULES_GATE_NAME = "step-rules"


def _count(con: sqlite3.Connection, where: str, params: tuple = ()) -> int:
    """Rows in ``gate_registry`` matching ``where`` (0 when the table is absent)."""
    try:
        cur = con.execute(
            f"SELECT COUNT(*) FROM gate_registry WHERE {where}", params
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(cur[0]) if cur else 0


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
            now = int(time.time())
            have_oracle = _count(con, "gate_id LIKE 'oracle-%'") >= 5
            have_mutation = _count(con, "gate_id = ?", (_MUTATION_GATE_ID,)) >= 1
            have_step_rules = _count(
                con, "gate_id = ?", (_STEP_RULES_GATE_ID,)) >= 1

            # Repair a row seeded while defer still counted as failure. This
            # has to run before the early return below: a DB that already has
            # every family present would otherwise take that return and keep
            # the gate inert for every real task class forever.
            con.execute(
                "UPDATE gate_registry SET task_class_filter=NULL "
                "WHERE gate_id=? AND task_class_filter=?",
                (_MUTATION_GATE_ID, _MUTATION_GATE_LEGACY_FILTER),
            )

            if have_oracle and have_mutation and have_step_rules:
                con.commit()
                return 0

            if not have_oracle:
                for name, safety in _ROSTER:
                    cond = native_condition(name)
                    gid = f"gate-custom-{uuid.uuid4().hex[:8]}"
                    con.execute(
                        "INSERT OR IGNORE INTO gate_registry "
                        "(gate_id, gate_type, condition, task_class_filter, "
                        " safety, active, registered_at) "
                        "VALUES (?, 'custom', ?, '', ?, 1, ?)",
                        (gid, cond, int(safety), now),
                    )
                for name in _STABLE_IDS:
                    new_id = _STABLE_IDS[name]
                    cond = native_condition(name)
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

            # Both non-oracle gates: NULL filter (they join every task class)
            # and safety=0 (each is a measurement that belongs in the verdict,
            # not a publish blocker).
            for gid, gname in ((_MUTATION_GATE_ID, _MUTATION_GATE_NAME),
                               (_STEP_RULES_GATE_ID, _STEP_RULES_GATE_NAME)):
                con.execute(
                    "INSERT OR IGNORE INTO gate_registry "
                    "(gate_id, gate_type, condition, task_class_filter, "
                    " safety, active, registered_at) "
                    "VALUES (?, 'custom', ?, NULL, 0, 1, ?)",
                    (gid, native_condition(gname), now),
                )

            con.commit()
        finally:
            con.close()
    except Exception:
        return 0
    return 0