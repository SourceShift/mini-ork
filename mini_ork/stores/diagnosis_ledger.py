"""diagnosis_ledger — the ledger behind the calibrated escalation bar (SDE-3b2).

One row per diagnosis the self-driving loop **acted on**: escalated to a
``framework-edit`` fix epic, or refused and handed to a human. Each row can be
labelled right or wrong once the outcome is known, and ``labelled`` is what
``mini_ork.recovery.escalation.gather_inputs`` feeds to the Learn-Then-Test
threshold (``mini_ork.gates.abstain_gate._ltt_min_confidence``) to produce the
calibrated bar the frozen escalation policy compares a model diagnosis against.

    record(db, run_id, diagnosis, action=...) -> ledger id   (idempotent)
    set_label(db, ledger_id, correct, reason)                (first label wins)
    labelled(db, source="model") -> [(confidence, is_wrong)] (the LTT input)

Why a ledger and not the diagnosis itself: a diagnosis is an *opinion* the loop
acts on; whether it was right is only knowable later (did the fix epic merge AND
did the revived run get past the node that failed). The ledger is the join
between the two, keyed so a re-recorded diagnosis never inflates the counts.

**Row orientation is load-bearing.** ``_ltt_min_confidence`` takes
``(confidence, is_wrong)`` pairs, while the ``label`` column stores 1 = the
diagnosis was right, 0 = it was wrong. :func:`labelled` inverts the flag — a
flipped orientation here would silently calibrate the bar backwards.

Schema: this module speaks sqlite the way its siblings do
(``mini_ork/stores/safety_events.py``, ``pattern_store.py``) — plain
``sqlite3.connect`` with ``PRAGMA busy_timeout=5000``, and the DDL from
``db/migrations/0073_diagnosis_ledger.sql`` re-run at connect time via
``CREATE TABLE IF NOT EXISTS`` so a home that has not migrated still works.

DB path resolution (when ``db`` is omitted) mirrors the siblings:
``${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}``.

Reads fail closed and quiet: a caller asking for labels from a home with no
ledger table yet gets ``[]``, never an exception — ``gather_inputs`` runs on
every escalate, including ones from homes that predate this migration.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["record", "set_label", "labelled", "_db_path", "_ensure_table"]

_TABLE = "diagnosis_ledger"

# The table DDL, mirroring db/migrations/0073_diagnosis_ledger.sql. Kept as one
# text so the migration and the runtime healing cannot drift: a home that never
# ran 0073 gets the identical shape here.
_DDL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    layer        TEXT,
    routes_to    TEXT,
    confidence   REAL,
    model_lane   TEXT,
    signature    TEXT,
    action       TEXT,
    fix_epic_id  TEXT,
    created_at   INTEGER NOT NULL,
    label        INTEGER,
    labelled_at  INTEGER,
    label_reason TEXT
)
"""

# Mirrors the migration's UNIQUE INDEX (0061/0062 use an index rather than a
# table constraint for a non-PK unique key).
_INDEXES = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{_TABLE}_unique "
    f"ON {_TABLE}(run_id, source, signature)",
    f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_source_label "
    f"ON {_TABLE}(source, label)",
    f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_epic "
    f"ON {_TABLE}(fix_epic_id)",
)

# The diagnosis fields that identify a failure, hashed into a signature when the
# diagnosis carries none of its own. Deliberately the *stable* fields: a
# re-diagnosis of the same failure must land on the same ledger row, so
# ``computed_at`` / ``evidence`` excerpts (which drift run to run) are excluded.
_SIGNATURE_FIELDS = ("rule_id", "layer", "stage", "node", "source", "summary")


def _db_path(db: str | Path | None = None) -> str:
    """Resolve the SQLite DB path. Mirrors the sibling stores' ``_db_path``.

    Args:
        db: explicit override. When supplied, returned verbatim (as ``str``).

    Falls back identically to the siblings:
        ``${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}``
    """
    if db:
        return str(db)
    return os.environ.get("MINI_ORK_DB") or os.path.join(
        os.environ.get("MINI_ORK_HOME") or os.path.join(os.getcwd(), ".mini-ork"),
        "state.db",
    )


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5.0)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _table_exists(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
    ).fetchone()
    return row is not None


def _ensure_table(con: sqlite3.Connection) -> None:
    """Create the ledger (and its indexes) if absent — idempotent, no guard.

    Unlike ``pattern_store``'s once-per-process flag this runs the
    ``IF NOT EXISTS`` DDL every time. The read is a single ``sqlite_master``
    lookup and the cost is negligible, while a cached flag would go stale the
    way a fresh/replaced state.db surprises a long-lived process. Writers call
    this; :func:`labelled` deliberately does NOT — a read must never take a
    write lock, and must not create a DB file merely to answer "no labels".
    """
    if _table_exists(con):
        return
    for ddl in (_DDL, *_INDEXES):
        con.execute(ddl)
    con.commit()


def _signature(run_id: str, diagnosis: dict[str, Any]) -> str:
    """The diagnosis signature — the third half of the dedupe key.

    A diagnosis that already carries its own ``signature`` (a caller that knows
    the GRAO fingerprint, say) keeps it verbatim; otherwise one is derived from
    the identifying fields plus the run id. The writer never leaves this NULL:
    SQLite treats NULLs as distinct in a UNIQUE index, so a NULL signature would
    let the same diagnosis be recorded twice.
    """
    explicit = diagnosis.get("signature")
    if explicit:
        return str(explicit)
    seed = {"run_id": str(run_id)}
    for key in _SIGNATURE_FIELDS:
        seed[key] = diagnosis.get(key)
    blob = json.dumps(seed, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _as_float(value: Any) -> float | None:
    """A confidence as a float, or ``None`` when it is absent/unparseable.

    ``None`` is meaningful: a diagnosis that stated no confidence cannot
    calibrate a confidence threshold, so :func:`labelled` skips it rather than
    coercing a misleading 0.0.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record(
    db: str | Path | None,
    run_id: str,
    diagnosis: dict,
    *,
    action: str,
    fix_epic_id: str | None = None,
) -> int:
    """Record one acted-on diagnosis; idempotent on ``(run_id, source, signature)``.

    Args:
        db: state.db path (``None`` → the env fallback chain).
        run_id: the run the diagnosis was made for.
        diagnosis: the diagnosis dict (``layer`` / ``source`` / ``confidence`` /
            ``routes_to`` / ``model_lane`` / ``signature`` are read; anything
            else — ``evidence``, ``computed_at`` — is not stored).
        action: what the loop did with it — ``"escalate"`` when a fix epic was
            opened, ``"human"`` when the policy refused or the fix could not be
            filed. Recording refusals is the point: SDE-4 can only learn the
            diagnoser's error rate from both outcomes.
        fix_epic_id: the epic an escalation opened, when there is one.

    Returns:
        The ledger row id. Re-recording the same diagnosis returns the id of the
        row already there — it is **not** rewritten. The first write is the one
        that saw the outcome first, and a later duplicate (a sweep re-deciding
        the same failure) must not silently mutate it.

    Raises:
        sqlite3.Error / OSError: a broken DB is the caller's to swallow. The
            production caller (``escalation.escalate``) wraps this so a ledger
            failure can never fail an escalation.
    """
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    run_id = str(run_id)
    source = str(diagnosis.get("source") or "")
    signature = _signature(run_id, diagnosis)
    row = (
        run_id,
        source,
        diagnosis.get("layer"),
        diagnosis.get("routes_to"),
        _as_float(diagnosis.get("confidence")),
        diagnosis.get("model_lane") or diagnosis.get("lane"),
        signature,
        action,
        fix_epic_id,
        int(time.time()),
    )

    con = _connect(_db_path(db))
    try:
        _ensure_table(con)
        existing = con.execute(
            f"SELECT id FROM {_TABLE} WHERE run_id=? AND source=? AND signature=?",
            (run_id, source, signature),  # noqa: S608 — _TABLE is a module constant
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        try:
            cur = con.execute(
                f"INSERT INTO {_TABLE} (run_id, source, layer, routes_to, confidence, "
                "model_lane, signature, action, fix_epic_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            con.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # A concurrent writer inserted the same key between the SELECT and
            # the INSERT. The uniqueness constraint did its job; adopt the
            # winner's id so both callers agree on one row.
            winner = con.execute(
                f"SELECT id FROM {_TABLE} WHERE run_id=? AND source=? AND signature=?",
                (run_id, source, signature),
            ).fetchone()
            if winner is None:  # pragma: no cover — the row must be there
                raise
            return int(winner[0])
    finally:
        con.close()


def set_label(
    db: str | Path | None,
    ledger_id: int,
    correct: bool,
    reason: str = "",
) -> bool:
    """Label a ledger row right or wrong. **First label wins.**

    The UPDATE only matches rows where ``label IS NULL``, so a second call for
    the same row changes nothing — an outcome, once observed, is not revised by
    a later guess. Legacy column semantics: ``label`` is 1 when the diagnosis was
    right and 0 when it was wrong; ``correct`` is the caller's bool in that
    orientation (``labelled`` inverts it back into ``_ltt_min_confidence``'s
    ``is_wrong``).

    Args:
        ledger_id: the id :func:`record` returned.
        correct: ``True`` when the diagnosis was right — the fix epic it opened
            merged *and* the revived run got past the node that failed.
        reason: the labeller's note. Evidence for the label, kept verbatim.

    Returns:
        ``True`` iff this call wrote the label. ``False`` when the row is
        already labelled (refused, first label kept) or no such row exists.
    """
    con = _connect(_db_path(db))
    try:
        _ensure_table(con)
        cur = con.execute(
            f"UPDATE {_TABLE} SET label=?, labelled_at=?, label_reason=? "
            "WHERE id=? AND label IS NULL",
            (1 if correct else 0, int(time.time()), str(reason or ""), int(ledger_id)),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def labelled(db: str | Path | None, source: str = "model") -> list[tuple[float, bool]]:
    """Labelled rows of one source, as ``(confidence, is_wrong)`` — the LTT input.

    The shape ``abstain_gate._ltt_min_confidence`` expects. Two filters are
    load-bearing:

      * ``label IS NOT NULL`` — an unlabelled row has no outcome, so it has
        nothing to teach the bar. (An unlabelled *and* a labelled-wrong row are
        exactly the two states a bare ``label`` default would have merged.)
      * ``confidence IS NOT NULL`` — a diagnosis that stated no confidence
        cannot contribute to a *confidence* threshold.

    A source with no ledger table (a home that never ran 0073) returns ``[]``:
    the caller then reports ``labelled_count=0`` and the frozen policy uses its
    bootstrap bar — the same degradation a missing DB gives, never an exception.
    """
    con = _connect(_db_path(db))
    try:
        if not _table_exists(con):
            return []
        rows = con.execute(
            f"SELECT confidence, label FROM {_TABLE} "
            "WHERE source=? AND label IS NOT NULL AND confidence IS NOT NULL",
            (str(source),),  # noqa: S608 — _TABLE is a module constant
        ).fetchall()
    finally:
        con.close()
    # Column semantics are 1 = right, 0 = wrong; the helper wants is_wrong.
    return [(float(conf), not bool(label)) for conf, label in rows]
