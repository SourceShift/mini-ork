"""Pure-logic port of ``lib/topology.sh::topology_recompute_win_rates``.

Faithful port of the deterministic aggregation logic in
``lib/topology.sh``. The bash function reads from ``execution_traces`` (and
left-joins ``workflow_memory``) and upserts aggregated rows into
``topology_win_rates``. This module lifts the classification rules and the
win-rate / sample-size math into a regular import so callers (and the
parity test) can exercise the deterministic core without going through
SQLite I/O.

The bash function's full pipeline is::

    READ execution_traces + LEFT JOIN workflow_memory
        ON wm.workflow_version_id = et.workflow_version_id
    GROUP BY (topology_id, workflow_name, task_class)
    CLASSIFY each row as win / loss / tie / none via (status, reviewer_verdict)
    AGGREGATE wins, losses, ties, AVG(cost_usd), AVG(duration_ms)
    COMPUTE win_rate = wins / (wins + losses), guarded against zero denom
    COMPUTE sample_size = wins + losses + ties

Public API::

    from mini_ork.orchestration.topology import (
        classify_outcome,       # (status, reviewer_verdict) -> 'win'|'loss'|'tie'|None
        compute_win_rate,       # (wins, losses) -> float (rounded to 4 decimals)
        aggregate_traces,       # (traces[, workflow_memory]) -> list[aggregate_row]
    )

    rows = aggregate_traces(
        [
            {"workflow_version_id": "code_fix_v3", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": None,
             "cost_usd": 0.42, "duration_ms": 8000, "created_at": "..."},
            ...
        ],
        workflow_memory={
            "code_fix_v3": {"yaml_hash": "abc123", "workflow_name": "code-fix"},
        },
    )

Each returned ``aggregate_row`` mirrors the columns bash would upsert into
``topology_win_rates``::

    {
      "topology_id":      str,
      "workflow_name":    str,
      "task_class":       str,
      "wins":             int,
      "losses":           int,
      "ties":             int,
      "win_rate":         float,    # round(_, 4)
      "sample_size":      int,      # wins + losses + ties
      "avg_cost_usd":     float,    # AVG(COALESCE(cost_usd, 0))
      "avg_duration_ms":  float,    # AVG(COALESCE(duration_ms, 0))
    }

The result list is sorted by ``(topology_id, task_class)`` to match a
deterministic ``SELECT ... ORDER BY topology_id, task_class`` read of the
bash-populated table.

``tests/unit/test_topology_parity.py`` enforces exact parity (floats
within ``1e-6``, strings exact) between ``aggregate_traces`` and the live
bash function over a corpus of representative trace sets.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, cast

# ─────────────────────────────────────────────────────────────────────────────
# Classification rules — verbatim mirror of the CASE expressions in
# lib/topology.sh::topology_recompute_win_rates. A row contributes to
# exactly one of (wins, losses, ties) or to NONE of them; the aggregation
# below handles "none" by counting zeros across all three buckets.
# ─────────────────────────────────────────────────────────────────────────────

_REJECTING_VERDICTS: frozenset[str] = frozenset({"REJECT", "ESCALATE", "needs_revision"})

_TIE_STATUSES: frozenset[str] = frozenset({"running", "vacuous", "blocked", "unknown"})


def classify_outcome(
    status: str | None,
    reviewer_verdict: str | None,
) -> str | None:
    """Map a single ``(status, reviewer_verdict)`` to ``'win'/'loss'/'tie'/None``.

    Mirrors the three mutually exclusive ``CASE`` branches in
    ``lib/topology.sh::topology_recompute_win_rates``:

    * ``win``  — ``status == 'success'`` AND ``reviewer_verdict`` is ``None``
                 or not in ``{REJECT, ESCALATE, needs_revision}``.
    * ``loss`` — ``status == 'failure'`` OR (``status == 'success'`` AND
                 ``reviewer_verdict`` in the rejecting set).
    * ``tie``  — ``status`` is ``None`` or in
                 ``{running, vacuous, blocked, unknown}``.
    * ``None`` — every other case (status outside the recognized set with
                 a non-rejecting verdict). The bash function tallies zero
                 for all three buckets; the port must do the same.

    A ``None`` classification is NOT an error — it is preserved so the
    aggregation reproduces bash's exact zero-tally rows.
    """
    if status == "success":
        if reviewer_verdict is None or reviewer_verdict not in _REJECTING_VERDICTS:
            return "win"
        return "loss"
    if status == "failure":
        return "loss"
    if status is None or status in _TIE_STATUSES:
        return "tie"
    return None


def compute_win_rate(wins: int, losses: int, ndigits: int = 4) -> float:
    """``wins / (wins + losses)`` rounded to ``ndigits`` decimals, ``0.0`` if denom == 0.

    Mirrors ``round(wr, 4)`` in ``lib/topology.sh``.
    """
    denom = (wins or 0) + (losses or 0)
    if denom <= 0:
        return 0.0
    return round((wins or 0) / denom, ndigits)


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation — pure pipeline over an iterable of trace rows. Mirrors the
# SQL aggregation in lib/topology.sh::topology_recompute_win_rates end to
# end (modulo the workflow_memory join, which is provided as a dict).
# ─────────────────────────────────────────────────────────────────────────────

# Required trace fields for the aggregation. Mirrors the WHERE clause in bash:
# rows missing any of these are dropped (matching SQL's NULL-fail filter).
_REQUIRED_TRACE_FIELDS = ("workflow_version_id", "task_class")


def _is_qualifying_trace(t: Mapping[str, object]) -> bool:
    for k in _REQUIRED_TRACE_FIELDS:
        v = t.get(k)
        if v is None or v == "":
            return False
    return True


def _group_key(
    t: Mapping[str, object],
    workflow_memory: Mapping[str, Mapping[str, str]] | None,
) -> tuple[str, str, str]:
    """Resolve ``(topology_id, workflow_name, task_class)`` for one trace.

    Mirrors the bash join::

        COALESCE(wm.yaml_hash, et.workflow_version_id) AS topology_id,
        COALESCE(wm.workflow_name, '?')                AS workflow_name,

    A missing ``workflow_memory`` row falls back to the trace's
    ``workflow_version_id`` and the literal ``'?'``.
    """
    wvid = cast(str, t["workflow_version_id"])
    tclass = cast(str, t["task_class"])
    wm = workflow_memory.get(wvid) if workflow_memory else None
    topology_id = (wm or {}).get("yaml_hash") or wvid
    workflow_name = (wm or {}).get("workflow_name") or "?"
    return str(topology_id), str(workflow_name), str(tclass)


def aggregate_traces(
    traces: Iterable[Mapping[str, object]],
    workflow_memory: Mapping[str, Mapping[str, str]] | None = None,
    win_rate_ndigits: int = 4,
) -> list[dict]:
    """Aggregate ``traces`` by ``(topology_id, task_class)`` per bash's rules.

    Parameters
    ----------
    traces:
        Iterable of trace rows. Each row is a Mapping carrying (at minimum)
        ``workflow_version_id`` and ``task_class``; the function reads
        ``status``, ``reviewer_verdict``, ``cost_usd`` and ``duration_ms``
        for the aggregation. ``created_at`` is NOT consulted here — bash
        filters by it via the ``--since`` parameter, which the caller
        already accounts for before handing rows to the port.
    workflow_memory:
        Optional ``{workflow_version_id: {yaml_hash, workflow_name}}``
        mapping. Equivalent to the bash ``LEFT JOIN workflow_memory``.
    win_rate_ndigits:
        Decimal places for the win-rate rounding. Mirrors bash's
        ``round(wr, 4)``; expose only for tests.

    Returns
    -------
    list[dict]
        Aggregate rows, one per ``(topology_id, task_class)`` group, sorted
        by ``(topology_id, task_class)``. Schema matches the columns bash
        upserts into ``topology_win_rates``.
    """
    grouped: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "_cost_sum": 0.0,
            "_cost_n": 0,
            "_dur_sum": 0.0,
            "_dur_n": 0,
        }
    )

    for t in traces:
        if not _is_qualifying_trace(t):
            continue
        key = _group_key(t, workflow_memory)
        bucket = grouped[key]
        status = cast(object | None, t.get("status"))
        verdict = cast(object | None, t.get("reviewer_verdict"))
        outcome = classify_outcome(
            cast(str | None, status),
            cast(str | None, verdict),
        )
        if outcome == "win":
            bucket["wins"] += 1
        elif outcome == "loss":
            bucket["losses"] += 1
        elif outcome == "tie":
            bucket["ties"] += 1
        # outcome == None: contributes zero to all three buckets, matches bash.

        cost = t.get("cost_usd")
        if cost is not None:
            bucket["_cost_sum"] += float(cast(float, cost))
            bucket["_cost_n"] += 1
        dur = t.get("duration_ms")
        if dur is not None:
            bucket["_dur_sum"] += float(cast(float, dur))
            bucket["_dur_n"] += 1

    rows: list[dict] = []
    for (topology_id, workflow_name, task_class), b in grouped.items():
        wins = b["wins"]
        losses = b["losses"]
        ties = b["ties"]
        avg_cost = (b["_cost_sum"] / b["_cost_n"]) if b["_cost_n"] else 0.0
        avg_dur = (b["_dur_sum"] / b["_dur_n"]) if b["_dur_n"] else 0.0
        rows.append(
            {
                "topology_id": topology_id,
                "workflow_name": workflow_name,
                "task_class": task_class,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "win_rate": compute_win_rate(wins, losses, win_rate_ndigits),
                "sample_size": wins + losses + ties,
                "avg_cost_usd": avg_cost,
                "avg_duration_ms": avg_dur,
            }
        )

    rows.sort(key=lambda r: (r["topology_id"], r["task_class"]))
    return rows
