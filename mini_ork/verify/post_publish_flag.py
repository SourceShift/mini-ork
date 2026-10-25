"""Make a post-run verify failure after publish visible instead of silent.

A run is marked ``published`` before the post-run verify runs. If that verify
then ends ``fail`` or ``partial``, the run was already delivered: nothing
reopens it, ``retry_hint`` returns ``None`` for a ``published`` row (no
NEEDS-CHANGE.md, no inbox item), and a ``partial`` even exits 0. The failure
existed only in the verify's own stdout.

This module closes that hole without adding a pre-publish gate. The publish is
left alone; afterwards, the run's verdict is escalated to ``ESCALATE`` (the
status stays ``published``), a note records why, a run-local record is written,
and one non-blocking oversight item is enqueued. ``active_state_index`` reads
only ``verdict='APPROVE'``, so an escalated run drops out of the planner's
facts while remaining delivered.

Everything is best-effort: a missing table, a locked database or a broken inbox
must not turn a post-run verify into a crash. ``mark`` returns ``None`` in every
one of those cases.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

__all__ = ["GATE_ID", "FLAG_FILE", "FLAGGED_VERDICTS", "mark"]

GATE_ID = "post_verify_after_publish"
FLAG_FILE = "post-verify-after-publish.json"
FLAGGED_VERDICTS = frozenset({"fail", "partial"})

#: Row fields carried into the run-local record for each failing verifier.
_FAILING_FIELDS = ("verifier", "detail", "evidence_path", "rc")


def _connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _failing_records(failing) -> list[dict]:
    out: list[dict] = []
    for row in failing or []:
        if not isinstance(row, dict):
            continue
        out.append({key: row[key] for key in _FAILING_FIELDS if key in row})
    return out


def _failing_names(failing) -> list[str]:
    names = {
        str(row["verifier"])
        for row in failing or []
        if isinstance(row, dict) and row.get("verifier") is not None
    }
    return sorted(names)


def mark(db, run_id, run_dir, verdict, *, trace_id: str = "",
         failing=None, now=None) -> dict | None:
    """Escalate a published run whose post-run verify failed. Never raises.

    Returns the record written to ``<run_dir>/FLAG_FILE`` (also the function's
    value when no run dir exists), or ``None`` when nothing was flagged.
    """
    try:
        if verdict not in FLAGGED_VERDICTS:
            return None
        if not db or not run_id:
            return None
        if not os.path.isfile(db):
            return None

        try:
            conn = _connect(db)
        except sqlite3.Error:
            return None

        try:
            try:
                row = conn.execute(
                    "SELECT status, verdict FROM task_runs WHERE id = ?", (run_id,)
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is None or row["status"] != "published":
                return None

            names = _failing_names(failing)
            note = (f"post-run verify {verdict} after publish ({trace_id}): "
                    f"{', '.join(names) or 'no failing row recorded'}")
            flagged_at = now if now is not None else int(time.time())

            try:
                cur = conn.execute(
                    "UPDATE task_runs SET verdict = 'ESCALATE', "
                    "notes = COALESCE(notes || '; ', '') || ?, updated_at = ? "
                    "WHERE id = ? AND status = 'published' "
                    "AND (verdict IS NULL OR verdict = 'APPROVE')",
                    (note, flagged_at, run_id),
                )
                conn.commit()
                escalated = cur.rowcount == 1
            except sqlite3.Error:
                return None

            feature = f"post-run verify failed after publish: {run_id}"
            inbox_id = None
            try:
                try:
                    existing = conn.execute(
                        "SELECT inbox_id FROM mo_inbox_gates"
                        " WHERE gate_id = ? AND feature = ?",
                        (GATE_ID, feature),
                    ).fetchone()
                except sqlite3.Error:
                    existing = None
                if existing is not None:
                    inbox_id = int(existing["inbox_id"])
                else:
                    from mini_ork.gates.oversight_inbox import enqueue

                    # No ``blocks_dispatch_for``: an oversight item, never a
                    # dispatch blocker — the run was already delivered.
                    inbox_id = enqueue(
                        GATE_ID,
                        feature,
                        phase="verify",
                        context={
                            "run_id": run_id,
                            "verdict": verdict,
                            "trace_id": trace_id,
                            "failing": names,
                            "run_dir": run_dir,
                        },
                        db_path=db,
                    )
            except Exception:
                inbox_id = None

            record = {
                "run_id": run_id,
                "verdict": verdict,
                "trace_id": trace_id,
                "status": "published",
                "task_runs_verdict": "ESCALATE" if escalated else row["verdict"],
                "failing": _failing_records(failing),
                "inbox_id": inbox_id,
                "flagged_at": flagged_at,
            }

            if run_dir and os.path.isdir(run_dir):
                try:
                    with open(os.path.join(run_dir, FLAG_FILE), "w",
                              encoding="utf-8") as fh:
                        json.dump(record, fh, indent=2)
                except OSError:
                    pass
            return record
        finally:
            conn.close()
    except Exception:
        return None
