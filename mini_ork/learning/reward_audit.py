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
  equality query, no prose parsing.

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


def enqueue_divergences(db_path: str, divergences) -> int:
    """Surface contradicted-run divergences to the operator inbox.

    One ``mo_inbox_gates`` row per entry with
    ``gate_id='reward_evidence_divergence'``. The run id names the row
    (``feature``), the trace id rides in ``phase``, and the reason is kept in
    ``context_json``.

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
    for entry in divergences:
        if accepted >= cap:
            break
        if not isinstance(entry, dict):
            continue
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            continue
        trace_id = entry.get("trace_id")
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
                feature=f"reward/evidence divergence: {run_id}",
                phase=str(trace_id) if trace_id else "",
                context=context,
                db_path=db_path,
            )
        except Exception:
            continue
        accepted += 1
    return accepted
