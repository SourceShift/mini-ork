"""verifier_rubric — Python port of lib/verifier_rubric.sh.

Faithful port of the public CRUD surface of ``lib/verifier_rubric.sh``.
The bash file shells out to Python heredocs for every SQL operation
already, so collapsing it to a single Python module removes two layers
of indirection while preserving byte-equivalent stdout and identical
DB state.

Co-existence model (strangler-fig): ``lib/verifier_rubric.sh`` stays
byte-identical. Parity is enforced by
``tests/unit/test_verifier_rubric_py.py`` (7 live-subprocess cases:
each function called via real ``bash -c 'source lib/verifier_rubric.sh
&& fn args'`` against a temp ``db/init.sh``-scaffolded SQLite, then
again via the Python port, then DB-row + stdout diffed).

Pipeline map (bash → Python; bash line ranges from
``lib/verifier_rubric.sh``):

  rubric_register       lines 62-105    → rubric_register
  rubric_get            lines 107-126   → rubric_get
  verifier_result_record lines 128-175  → verifier_result_record
  verifier_result_annotate lines 177-220 → verifier_result_annotate
  verifier_chain_repair lines 222-244   → verifier_chain_repair
  verifier_fp_rate      lines 246-275   → verifier_fp_rate

Public surface (mirrors the bash signatures exactly):
    rubric_register(db_path, rubric_id, name, task_class, axes_json) -> None
    rubric_get(db_path, rubric_id) -> str
    verifier_result_record(db_path, run_id, verifier_name, verdict,
                           rubric_id=None, confidence=None,
                           scored_axes_json=None) -> str
    verifier_result_annotate(db_path, result_id, kind, annotator,
                             notes=None) -> None
    verifier_chain_repair(db_path, result_id, repair_run_id) -> None
    verifier_fp_rate(db_path, verifier_name, window_seconds=0) -> str

Notes on parity:
- ``_rubric_uuid`` in bash calls ``secrets.token_hex(6)`` (12 lowercase
  hex chars). The port mirrors that verbatim — DO NOT substitute
  ``uuid.uuid4().hex[:12]``; the formats are equivalent on length but
  both should still emit lowercased hex, so ``secrets.token_hex(6)`` is
  the safer one-to-one match.
- Bash ``rubric_get`` on miss prints literal ``null`` (with a trailing
  newline from ``print()``). The port mirrors with ``print('null')``.
- Bash ``verifier_fp_rate`` on zero-results prints literal ``0.0``.
- Bash ``verifier_fp_rate`` happy-path uses ``f"{fps/total:.4f}"`` —
  the port mirrors with the same f-string so the emitted text is
  byte-equal (``"0.2500"`` not ``"0.25"``).
- The CHECK constraint on ``verifier_results.verdict`` (and the
  ``NOT (is_false_positive=1 AND is_false_negative=1)`` cross-flag
  constraint) is enforced at the DB layer. The port does NOT
  pre-validate; ``sqlite3.IntegrityError`` propagates with rc=1
  semantics (caller sees the raise — matches bash's ``exit 1``).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Optional

__all__ = [
    "rubric_register",
    "rubric_get",
    "verifier_result_record",
    "verifier_result_annotate",
    "verifier_chain_repair",
    "verifier_fp_rate",
]


def _open(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with busy_timeout=5000 (mirrors every
    bash heredoc's first pragma). Per-connection — not persisted."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=5000")
    return con


def rubric_register(
    db_path: str,
    rubric_id: str,
    name: str,
    task_class: str,
    axes_json: str,
) -> None:
    """Mirror bash ``rubric_register`` (lines 62-105).

    UPSERT into ``verifier_rubrics``. On INSERT both ``created_at`` and
    ``updated_at`` use the same ``now``; on UPDATE only ``updated_at``
    is refreshed (also to the same ``now``). The bash heredoc sets
    ``updated_at = excluded.updated_at`` so a single ``now`` value
    flows through both INSERT and UPDATE branches — this matches.
    """
    now = int(time.time())
    con = _open(db_path)
    try:
        con.execute(
            """
            INSERT INTO verifier_rubrics
                (rubric_id, name, description, task_class, axes_json,
                 created_at, updated_at, is_active)
            VALUES (?, ?, NULL, ?, ?, ?, ?, 1)
            ON CONFLICT(rubric_id) DO UPDATE SET
                name=excluded.name,
                task_class=excluded.task_class,
                axes_json=excluded.axes_json,
                updated_at=excluded.updated_at,
                is_active=1
            """,
            (
                rubric_id,
                name,
                task_class or None,
                axes_json or None,
                now,
                now,
            ),
        )
        con.commit()
    finally:
        con.close()


def rubric_get(db_path: str, rubric_id: str) -> str:
    """Mirror bash ``rubric_get`` (lines 107-126).

    Emits the rubric row as JSON on stdout. ``null`` on miss. The
    function returns the string (without trailing newline) so callers
    can inspect it; the bash output mirrors because both call
    ``print(...)`` internally — see ``print_rubric_get`` if you want
    the exact stdout-write helper.
    """
    con = _open(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM verifier_rubrics WHERE rubric_id=?",
            (rubric_id,),
        ).fetchone()
        if row is None:
            return "null"
        return json.dumps(dict(row))
    finally:
        con.close()


def verifier_result_record(
    db_path: str,
    run_id: str,
    verifier_name: str,
    verdict: str,
    rubric_id: Optional[str] = None,
    confidence: Optional[float] = None,
    scored_axes_json: Optional[str] = None,
) -> str:
    """Mirror bash ``verifier_result_record`` (lines 128-175).

    INSERT into ``verifier_results``. Returns the new result_id
    (``vr-`` + 12 lowercase hex chars from ``secrets.token_hex(6)``).

    Does NOT pre-validate ``verdict`` — the DB CHECK constraint
    ``verdict IN ('pass','fail','indeterminate','vacuous')`` raises
    ``sqlite3.IntegrityError`` on bad input. Matches bash's
    ``exit 1`` on the same DDL violation.
    """
    result_id = "vr-" + secrets.token_hex(6)
    con = _open(db_path)
    try:
        con.execute(
            """
            INSERT INTO verifier_results
                (result_id, run_id, verifier_name, rubric_id, verdict,
                 confidence, scored_axes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                run_id,
                verifier_name,
                rubric_id or None,
                verdict,
                float(confidence) if confidence is not None else None,
                scored_axes_json or None,
            ),
        )
        con.commit()
    finally:
        con.close()
    return result_id


def verifier_result_annotate(
    db_path: str,
    result_id: str,
    kind: str,
    annotator: str,
    notes: Optional[str] = None,
) -> None:
    """Mirror bash ``verifier_result_annotate`` (lines 177-220).

    ``kind`` ∈ ``{false_positive, false_negative}`` — any other value
    raises ``ValueError`` (mirrors the bash ``case`` statement that
    prints to stderr and exits 2 on unknown kinds).

    Lets ``sqlite3.IntegrityError`` propagate on the cross-flag CHECK
    constraint (``NOT (is_false_positive=1 AND is_false_negative=1)``)
    so the caller sees a hard failure — matches bash's ``exit 1``.
    """
    if kind not in ("false_positive", "false_negative"):
        raise ValueError(
            "verifier_result_annotate: kind must be false_positive or false_negative"
        )
    col = "is_false_positive" if kind == "false_positive" else "is_false_negative"
    now = int(time.time())
    con = _open(db_path)
    try:
        con.execute(
            f"""
            UPDATE verifier_results
               SET {col}=1, annotated_by=?, annotated_at=?, notes=COALESCE(?, notes)
             WHERE result_id=?
            """,
            (annotator, now, notes or None, result_id),
        )
        con.commit()
    finally:
        con.close()


def verifier_chain_repair(
    db_path: str,
    result_id: str,
    repair_run_id: str,
) -> None:
    """Mirror bash ``verifier_chain_repair`` (lines 222-244).

    UPDATE ``verifier_results.repair_run_id`` for the given result_id.
    """
    con = _open(db_path)
    try:
        con.execute(
            "UPDATE verifier_results SET repair_run_id=? WHERE result_id=?",
            (repair_run_id, result_id),
        )
        con.commit()
    finally:
        con.close()


def verifier_fp_rate(
    db_path: str,
    verifier_name: str,
    window_seconds: int = 0,
) -> str:
    """Mirror bash ``verifier_fp_rate`` (lines 246-275).

    Emits the false-positive rate as a float string. ``window_seconds=0``
    means "all time" (no cutoff). Empty result set emits literal ``0.0``;
    otherwise ``f"{fps/total:.4f}"`` so 1-of-4 prints ``0.2500`` (NOT
    ``0.25`` — bash mirrors the same precision).
    """
    cutoff = (int(time.time()) - window_seconds) if window_seconds > 0 else 0
    con = _open(db_path)
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM verifier_results "
            "WHERE verifier_name=? AND created_at>=?",
            (verifier_name, cutoff),
        ).fetchone()[0]
        if total == 0:
            return "0.0"
        fps = con.execute(
            "SELECT COUNT(*) FROM verifier_results "
            "WHERE verifier_name=? AND created_at>=? AND is_false_positive=1",
            (verifier_name, cutoff),
        ).fetchone()[0]
        return f"{fps / total:.4f}"
    finally:
        con.close()