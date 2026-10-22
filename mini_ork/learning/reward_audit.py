"""Pair GRPO rewards with independent verifier evidence.

``write_grpo_advantages`` learns from every trace whose ``validity`` says the
agent controlled the outcome. That says nothing about whether the run actually
*passed* anything: a ``status='success'`` trace can carry an approving reviewer
verdict while the run's ``verifier_results`` rows are all failures. Learning
from that row trains the loop on the claim instead of the measurement.

This module is the measurement half:

* :func:`classify_run` reads ``verifier_results`` for one run id and returns
  ``"backed"`` (at least one genuine pass), ``"contradicted"`` (rows exist but
  none is a genuine pass), or ``"unmeasured"`` (no rows — or a database that
  predates migration 0025). Only ``"contradicted"`` is evidence of
  disagreement; absence of evidence is never treated as evidence of failure.
* :func:`pair` applies that rule to a reward: a positive reward
  (``>= DEFAULT_POSITIVE``) with contradicted evidence is dropped (``None``);
  everything else passes through unchanged.
* :func:`audit` reports the same classification over recent traces.
* :func:`enqueue_divergences` surfaces drops to the operator through
  ``mo_inbox_gates`` under ``gate_id='reward_evidence_divergence'`` — a column
  equality query, no prose parsing. Enqueueing is idempotent per
  ``(feature, phase)``: a divergence the operator already saw — in any status —
  is never written twice.
* :func:`dedupe_divergence_rows` collapses duplicates that earlier writebacks
  already wrote, keeping the smallest ``inbox_id`` per ``(feature, phase)`` and
  never touching a resolved row or another gate's rows.

All queries are read-only. Run ids are opaque TEXT keys — never int-cast.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

__all__ = [
    "DEFAULT_POSITIVE",
    "UNMEASURED",
    "BACKED",
    "CONTRADICTED",
    "CONTRADICTED_REASON",
    "DIVERGENCE_GATE_ID",
    "DEFAULT_ENQUEUE_LIMIT",
    "classify_run",
    "pair",
    "audit",
    "enqueue_divergences",
    "dedupe_divergence_rows",
]

#: Rewards at or above this are positive and need evidence of success.
DEFAULT_POSITIVE = 0.5

UNMEASURED = "unmeasured"
BACKED = "backed"
CONTRADICTED = "contradicted"

#: Why a run classified as contradicted.
CONTRADICTED_REASON = "run has verifier rows and none is a pass"

#: ``mo_inbox_gates.gate_id`` for an evidence divergence — queryable by equality.
DIVERGENCE_GATE_ID = "reward_evidence_divergence"

#: Per-call cap, overridable with ``MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT``.
DEFAULT_ENQUEUE_LIMIT = 50


def classify_run(con: sqlite3.Connection, run_id: Any) -> str:
    """Classify one run's independent verifier evidence.

    ``"backed"`` — at least one row has ``verdict='pass'`` AND
    ``is_false_positive=0``. An operator-marked false positive overrides the
    verdict, so such a row is not a pass.
    ``"contradicted"`` — rows exist for the run, but none is a genuine pass.
    ``"unmeasured"`` — no rows for the run, or no ``verifier_results`` table
    (legacy DBs predate migration 0025). This never raises: a writeback running
    against a pre-0025 database must keep working.

    Read-only. The caller owns ``con``; this function neither commits nor
    closes it.
    """
    if run_id is None:
        return UNMEASURED
    key = str(run_id)
    if not key:
        return UNMEASURED
    try:
        row = con.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN verdict='pass' AND is_false_positive=0 "
            "THEN 1 ELSE 0 END), 0) AS passes "
            "FROM verifier_results WHERE run_id=?",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return UNMEASURED
    if row is None:
        return UNMEASURED
    total = int(row[0] or 0)
    passes = int(row[1] or 0)
    if passes > 0:
        return BACKED
    if total > 0:
        return CONTRADICTED
    return UNMEASURED


def pair(reward: float | None, evidence: str) -> tuple[float | None, str]:
    """Return ``(reward, evidence)`` after the evidence filter.

    ``reward is None`` is passed through untouched. A reward below
    ``DEFAULT_POSITIVE`` needs no evidence of success, so it is never dropped —
    the threshold is inclusive, so exactly ``0.5`` is positive. A positive
    reward with ``"contradicted"`` evidence becomes ``None`` (the row is
    dropped); ``"backed"`` and ``"unmeasured"`` keep the reward.
    """
    if reward is None:
        return None, evidence
    if reward < DEFAULT_POSITIVE:
        return reward, evidence
    if evidence == CONTRADICTED:
        return None, evidence
    return reward, evidence


def audit(db_path: str, *, limit: int = 500) -> dict:
    """Classify recent run-carrying traces and summarize the result.

    Reads up to ``limit`` ``execution_traces`` rows that carry a non-empty
    ``run_id``, newest first (``created_at DESC``), and classifies each distinct
    run. Returns::

        {"checked": int, "backed": int, "contradicted": int,
         "unmeasured": int,
         "divergences": [{"run_id", "trace_id", "status",
                          "reviewer_verdict", "reason"}, ...]}

    Read-only.
    """
    report = {
        "checked": 0,
        "backed": 0,
        "contradicted": 0,
        "unmeasured": 0,
        "divergences": [],
    }
    con = sqlite3.connect(db_path, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        try:
            rows = con.execute(
                "SELECT trace_id, run_id, status, reviewer_verdict "
                "FROM execution_traces "
                "WHERE run_id IS NOT NULL AND TRIM(run_id) <> '' "
                "ORDER BY created_at DESC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        except sqlite3.OperationalError:
            return report
        for row in rows:
            report["checked"] += 1
            evidence = classify_run(con, row["run_id"])
            report[evidence] += 1
            if evidence == CONTRADICTED:
                report["divergences"].append(
                    {
                        "run_id": row["run_id"],
                        "trace_id": row["trace_id"],
                        "status": row["status"],
                        "reviewer_verdict": row["reviewer_verdict"],
                        "reason": CONTRADICTED_REASON,
                    }
                )
    finally:
        con.close()
    return report


def _enqueue_limit() -> int:
    raw = os.environ.get(
        "MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT", str(DEFAULT_ENQUEUE_LIMIT)
    )
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_ENQUEUE_LIMIT


def _divergence_row_exists(db_path: str, feature: str, phase: str) -> bool:
    """Whether a divergence row already exists for ``(feature, phase)``.

    Matches ``gate_id``, ``feature`` and ``phase`` in **any** status, so a
    divergence the operator already approved or rejected never comes back. A
    missing ``mo_inbox_gates`` table means the inbox has never been written
    here: there is no row. Read failures propagate; the caller falls back to
    the old enqueue-always behaviour for that entry.
    """
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        try:
            row = con.execute(
                "SELECT 1 FROM mo_inbox_gates WHERE gate_id = ? "
                "AND feature = ? AND COALESCE(phase, '') = ? LIMIT 1",
                (DIVERGENCE_GATE_ID, feature, phase),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None
    finally:
        con.close()


def enqueue_divergences(db_path: str, divergences) -> int:
    """Surface contradicted-run divergences to the operator inbox.

    One ``mo_inbox_gates`` row per distinct ``(feature, phase)`` with
    ``gate_id='reward_evidence_divergence'``. The run id names the row
    (``feature``), the trace id rides in ``phase``, and the reason is kept in
    ``context_json``.

    Enqueueing is idempotent: an entry whose row already exists in **any**
    status — or whose ``(feature, phase)`` key already appeared earlier in this
    call — is skipped and neither counted nor charged against the per-call cap.
    If the existence check itself fails, the entry falls back to being
    enqueued, exactly as before the check existed.

    Malformed entries are skipped, never raised on. A per-entry enqueue failure
    is swallowed for the same reason: oversight is a side channel and must
    never break the learning write that produced the entries. Returns the
    number of rows actually accepted, capped at
    ``MO_REWARD_DIVERGENCE_ENQUEUE_LIMIT`` (default 50) per call.
    """
    if not divergences:
        return 0
    # Imported lazily so this module stays stdlib-only at import time; the
    # dependency is one-way (learning -> gates.oversight_inbox).
    from mini_ork.gates import oversight_inbox

    cap = _enqueue_limit()
    accepted = 0
    seen: set[tuple[str, str]] = set()
    for entry in divergences:
        if not isinstance(entry, dict):
            continue
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            continue
        trace_id = entry.get("trace_id")
        feature = f"reward/evidence divergence: {run_id}"
        phase = str(trace_id) if trace_id else ""
        key = (feature, phase)
        if key in seen:
            continue
        seen.add(key)
        try:
            if _divergence_row_exists(db_path, feature, phase):
                continue
        except Exception:
            # Fail open: oversight must never be lost to a flaky read. The
            # entry falls through to the pre-idempotent enqueue path.
            pass
        if accepted >= cap:
            break
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = CONTRADICTED_REASON
        context = {
            "run_id": run_id,
            "trace_id": trace_id,
            "status": entry.get("status"),
            "reviewer_verdict": entry.get("reviewer_verdict"),
            "reason": reason,
        }
        try:
            oversight_inbox.enqueue(
                gate_id=DIVERGENCE_GATE_ID,
                feature=feature,
                phase=phase,
                context=context,
                db_path=db_path,
            )
        except Exception:
            continue
        accepted += 1
    return accepted


#: Smallest ``inbox_id`` per ``(feature, phase)`` group within one gate.
_DEDUPE_KEEPER_SQL = (
    "SELECT MIN(inbox_id) FROM mo_inbox_gates "
    "WHERE gate_id = ? GROUP BY feature, phase"
)

_DEDUPE_COUNT_SQL = (
    "SELECT COUNT(*) FROM mo_inbox_gates "
    "WHERE gate_id = ? AND status = 'pending' "
    f"AND inbox_id NOT IN ({_DEDUPE_KEEPER_SQL})"
)

_DEDUPE_DELETE_SQL = (
    "DELETE FROM mo_inbox_gates "
    "WHERE gate_id = ? AND status = 'pending' "
    f"AND inbox_id NOT IN ({_DEDUPE_KEEPER_SQL})"
)


def _dedupe_divergence_rows(db_path: str, *, dry_run: bool) -> int:
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.OperationalError:
        # An unopenable path holds no rows to dedupe.
        return 0
    try:
        try:
            if dry_run:
                row = con.execute(
                    _DEDUPE_COUNT_SQL,
                    (DIVERGENCE_GATE_ID, DIVERGENCE_GATE_ID),
                ).fetchone()
                return int(row[0] or 0) if row else 0
            cur = con.execute(
                _DEDUPE_DELETE_SQL,
                (DIVERGENCE_GATE_ID, DIVERGENCE_GATE_ID),
            )
        except sqlite3.OperationalError:
            # No mo_inbox_gates table: nothing has ever been enqueued.
            return 0
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def dedupe_divergence_rows(db_path: str) -> int:
    """Collapse duplicate divergence rows already written to the inbox.

    Within ``gate_id = DIVERGENCE_GATE_ID`` only, each ``(feature, phase)``
    group keeps the row with the smallest ``inbox_id``; every other row in the
    group whose ``status = 'pending'`` is deleted. Resolved rows
    (``approved``/``rejected``) are never deleted, and no other gate's rows are
    touched. Returns the number of rows deleted.
    """
    return _dedupe_divergence_rows(db_path, dry_run=False)


def main(argv=None) -> int:
    """Module CLI: ``dedupe [--db PATH] [--dry-run]``.

    ``--db`` defaults to ``MINI_ORK_DB``, then ``.mini-ork/state.db``. Prints
    the number of duplicate divergence rows deleted (or, with ``--dry-run``,
    that would be deleted, without deleting).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m mini_ork.learning.reward_audit",
        description="Reward/evidence audit utilities.",
    )
    sub = parser.add_subparsers(dest="command")
    dedupe_parser = sub.add_parser(
        "dedupe", help="delete duplicate reward/evidence divergence rows"
    )
    dedupe_parser.add_argument(
        "--db", default=None, help="state DB path (default: MINI_ORK_DB or "
        ".mini-ork/state.db)"
    )
    dedupe_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many rows would be deleted without deleting them",
    )
    args = parser.parse_args(argv)
    if args.command != "dedupe":
        parser.print_help()
        return 2
    db_path = args.db or os.environ.get("MINI_ORK_DB") or ".mini-ork/state.db"
    deleted = _dedupe_divergence_rows(db_path, dry_run=bool(args.dry_run))
    print(deleted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
