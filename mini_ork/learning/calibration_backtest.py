"""Calibration backtest over execution_traces — does the router's number hold up?

``decision_service.decide()`` turns a routing margin into a predicted error
probability (``dispatch/calibration.py``) and escalates when it crosses
``target_error()``. Persisting that prediction (migration 0059) is only half the
loop: a stored prediction that no one ever checks is a claim, not a measurement.
This module is the check.

Two numbers, both read from ``execution_traces.predicted_error``:

  * reliability — is the prediction actually calibrated? (reliability diagram,
    ECE, Brier). A calibrated router that predicts 0.3 is wrong 30% of the time;
    one whose 0.3 is wrong 80% of the time is over-confident in a way the
    escalation threshold silently assumes away.
  * blind spot — of the rows the router DECLINED to escalate on
    (``predicted_error <= target``), how many errored anyway? That is the error
    the cheap lane absorbed, and it is invisible to the escalation gate itself:
    the gate only ever sees the rows it escalated.

Pure functions over ``(predicted_error, is_error)`` pairs; DB access lives in one
loader that fails open (an empty list) on a database without the column, so a
pre-0059 database reads as "no predictions yet", not a crash.
"""
from __future__ import annotations

import os
import sqlite3

from mini_ork.dispatch import calibration


def load_prediction_rows(db: str, task_class: str = "") -> list[tuple[float, bool]]:
    """``(predicted_error, is_error)`` for rows that carry a persisted prediction.

    ``is_error`` is ``status != 'success'`` — the same definition
    ``calibration.load_margin_rows`` uses — so a margin row and a prediction row
    describe the same outcome and the two measurements stay comparable.

    Empty list on a missing file, a missing table, or a missing column (a
    database that predates migration 0059) — fail open, exactly as
    ``load_margin_rows`` fails open on a database without ``route_margin``.

    Deliberately NOT windowed the way ``load_margin_rows`` is. The fit must be
    recent, because rows from before a lane's model changed describe a lane that
    no longer exists; this is the check ON that fit, so reading the whole
    history is what lets drift surface as a rising ECE rather than being trimmed
    away before anyone sees it.
    """
    if not db or not os.path.isfile(db):
        return []
    where = ["predicted_error IS NOT NULL"]
    args: list = []
    if task_class:
        where.append("task_class = ?")
        args.append(task_class)
    sql = (f"SELECT predicted_error, status FROM execution_traces "
           f"WHERE {' AND '.join(where)}")
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        # No predicted_error column — this database predates migration 0059.
        con.close()
        return []
    con.close()
    return [(float(p), str(s or "") != "success") for p, s in rows if p is not None]


def reliability(rows: list[tuple[float, bool]], bins: int = 10) -> list[dict]:
    """Equal-width bins over ``[0, 1]``; one entry per NON-empty bin.

    Each entry is ``{"lo", "hi", "n", "mean_predicted", "observed_rate"}``.
    Empty bins are omitted, not emitted with ``n=0`` — an empty bin carries no
    evidence either way, and emitting it would let a caller mistake "no data"
    for "perfectly calibrated".
    """
    if not rows:
        return []
    bins = max(1, int(bins))
    width = 1.0 / bins
    acc: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for p, err in rows:
        p = max(0.0, min(1.0, float(p)))
        idx = min(int(p * bins), bins - 1)
        acc[idx].append((p, 1.0 if err else 0.0))
    out: list[dict] = []
    for i, bucket in enumerate(acc):
        if not bucket:
            continue
        n = len(bucket)
        out.append({
            "lo": round(i * width, 10),
            "hi": round((i + 1) * width, 10),
            "n": n,
            "mean_predicted": sum(p for p, _ in bucket) / n,
            "observed_rate": sum(e for _, e in bucket) / n,
        })
    return out


def ece(rows: list[tuple[float, bool]], bins: int = 10) -> float | None:
    """Sample-weighted mean of ``|observed_rate - mean_predicted|``.

    ``None`` for empty rows. 0 means the router's predicted error equals its
    observed error in every bin; larger means the map is systematically off.
    """
    if not rows:
        return None
    rel = reliability(rows, bins)
    if not rel:
        return None
    total = sum(b["n"] for b in rel)
    return sum(b["n"] * abs(b["observed_rate"] - b["mean_predicted"])
               for b in rel) / total


def brier(rows: list[tuple[float, bool]]) -> float | None:
    """Mean squared error of the prediction against the observed outcome.

    ``None`` for empty rows. 0 is perfect; 1 is the worst case (predict 0,
    always wrong, or predict 1, always wrong).
    """
    if not rows:
        return None
    return sum((float(p) - (1.0 if e else 0.0)) ** 2 for p, e in rows) / len(rows)


def blind_spot(rows: list[tuple[float, bool]], target: float, *,
               include_boundary: bool = True) -> dict:
    """The gate's blind spot: kept rows that errored anyway.

    "Kept" = the router did NOT escalate (``predicted_error <= target`` when
    ``include_boundary``, matching ``should_escalate``'s ``p > target`` rule).
    The escalation gate only ever sees escalated rows, so the error it absorbed
    by staying cheap is exactly the fraction of kept rows that failed — and it
    is invisible to the gate itself.

    ``blind_spot_rate`` is ``None`` (not 0.0) when ``n_kept == 0``: no kept rows
    is "nothing to measure", and 0.0 would read as "perfectly safe" rather than
    "no data".
    """
    if include_boundary:
        kept = [r for r in rows if float(r[0]) <= target]
        escalated = [r for r in rows if float(r[0]) > target]
    else:
        kept = [r for r in rows if float(r[0]) < target]
        escalated = [r for r in rows if float(r[0]) >= target]
    n_kept = len(kept)
    n_kept_errored = sum(1 for _, e in kept if e)
    return {
        "n_kept": n_kept,
        "n_kept_errored": n_kept_errored,
        "blind_spot_rate": (n_kept_errored / n_kept) if n_kept else None,
        "n_escalated": len(escalated),
        "n_escalated_errored": sum(1 for _, e in escalated if e),
    }


def summarize(db: str, task_class: str = "", *, bins: int = 10) -> dict:
    """One call returning the whole backtest. Thin composition, no extra logic.

    ``target`` comes from ``calibration.target_error()`` — the SAME threshold the
    escalation gate uses — so the blind-spot boundary here is the boundary that
    actually governed the decisions being measured.
    """
    rows = load_prediction_rows(db, task_class)
    target = calibration.target_error()
    return {
        "n": len(rows),
        "ece": ece(rows, bins),
        "brier": brier(rows),
        "target": target,
        "blind_spot": blind_spot(rows, target),
        "reliability": reliability(rows, bins),
    }
