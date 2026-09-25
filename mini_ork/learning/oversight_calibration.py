"""Rule-by-fatigue calibration over the ``mo_inbox_gates`` oversight channel.

An oversight rule is only as good as the attention behind it. ``mo_inbox_gates``
was wired-and-dead — 0 rows, no writer, no reader — and a channel that nobody
measures fails in one of two directions, both of them fatigue:

  * **Rule-by-fatigue** — the gate fires so often that approving stops being a
    judgment. The signature is high resolution throughput with near-empty
    ``review_note``s: ``resolution_rate`` stays at or above ``FATIGUE_RATE``
    while ``note_rate`` falls to or below ``FATIGUE_NOTE_RATE``. The note stops
    carrying information.
  * **Abandoned oversight** — the gate enqueues and nothing is ever resolved.
    The coverage is imaginary, which is the dangerous state: it looks active.

A third, distinct signal — **stale** — is a growing backlog of still-``pending``
items: the gate is being attended to *and* falling behind.

Pure aggregation over rows; DB access lives in one loader that fails open (an
empty list) on a missing file or table, mirroring
``mini_ork.learning.calibration_backtest.load_prediction_rows``.

``empty_rate_is_none`` contract (the same precedent as
``calibration_backtest.blind_spot``): ``resolution_rate``, ``note_rate`` and the
two durations are ``None`` — never ``0.0`` — when the denominator is zero.
``0.0`` would read as "never resolved" or "fully noted" rather than "no data",
and a fatigue detector fed a fabricated zero would misclassify an empty gate.

The thresholds are module constants with no env knobs, so the rule is
falsifiable and unit-testable rather than tunable at run time.
"""
from __future__ import annotations

import os
import sqlite3
import statistics
import time

__all__ = ["load_gate_rows", "calibrate", "summarize"]

# High throughput, near-empty notes: rule-by-fatigue. MIN_SAMPLE keeps a
# two-item gate from being called fatigued; the rate pair is the signature.
MIN_SAMPLE = 5
FATIGUE_RATE = 0.95
FATIGUE_NOTE_RATE = 0.20

# A backlog item older than this is stale (72 h).
STALE_SECONDS = 72 * 3600

_RESOLVED = ("approved", "rejected")


def _resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> str:
    """The *semantic* DB-path convention — kept in lock-step with
    ``mini_ork.gates.oversight_inbox._resolve_db_path`` so the two modules never
    disagree on which DB a single env pin points at."""
    if db_path is not None:
        return os.fspath(db_path)
    return os.environ.get("MINI_ORK_DB") or ".mini-ork/state.db"


def load_gate_rows(
    db_path: str | os.PathLike[str] | None,
    *,
    since: float | None = None,
) -> list[dict]:
    """Every ``mo_inbox_gates`` row (optionally ``enqueued_at >= since``).

    Fail open — ``[]`` — on a missing file or a missing table, so calibration
    over a fresh tmp DB reads as "no data" rather than raising.
    """
    if not db_path or not os.path.isfile(db_path):
        return []
    con = sqlite3.connect(os.fspath(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    try:
        rows = con.execute(
            """
            SELECT inbox_id, gate_id, feature, phase, context_json, status,
                   review_note, enqueued_at, resolved_at, blocks_dispatch_for
              FROM mo_inbox_gates
            """
        ).fetchall()
    except sqlite3.OperationalError:
        con.close()
        return []
    con.close()
    out = [dict(r) for r in rows]
    if since is not None:
        out = [r for r in out if r["enqueued_at"] >= since]
    return out


def _rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator``, or ``None`` when the denominator is zero."""
    return (numerator / denominator) if denominator else None


def _median(values: list[float]) -> float | None:
    """Median of ``values``, or ``None`` when there are none."""
    return statistics.median(values) if values else None


def calibrate(
    *,
    db_path: str | os.PathLike[str] | None = None,
    since: float | None = None,
    now: float | None = None,
) -> dict:
    """One call returning the whole oversight calibration.

    ``since`` filters on ``enqueued_at`` (unix seconds; ``None`` = all time).
    ``now`` is injectable so ``oldest_pending_seconds`` is deterministic in
    tests; it defaults to ``time.time()``.

    Gates are sorted by ``enqueued`` descending then ``gate_id``, so the busiest
    — the ones most likely to be fatigued — read first.
    """
    if now is None:
        now = time.time()
    rows = load_gate_rows(_resolve_db_path(db_path), since=since)

    by_gate: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        gid = r["gate_id"]
        if gid not in by_gate:
            by_gate[gid] = []
            order.append(gid)
        by_gate[gid].append(r)

    gates: list[dict] = []
    for gid in order:
        rs = by_gate[gid]
        resolved_rows = [r for r in rs if r["status"] in _RESOLVED]
        pending_rows = [r for r in rs if r["status"] == "pending"]
        enqueued = len(rs)
        resolved = len(resolved_rows)
        pending = len(pending_rows)

        resolution_rate = _rate(resolved, enqueued)
        note_rate = _rate(
            sum(1 for r in resolved_rows if (r["review_note"] or "").strip()),
            resolved,
        )
        median_resolve_seconds = _median([
            r["resolved_at"] - r["enqueued_at"]
            for r in resolved_rows
            if r["resolved_at"] is not None
        ])
        oldest_pending_seconds = (
            now - min(r["enqueued_at"] for r in pending_rows)
            if pending_rows else None
        )

        gates.append({
            "gate_id": gid,
            "enqueued": enqueued,
            "resolved": resolved,
            "pending": pending,
            "resolution_rate": resolution_rate,
            "median_resolve_seconds": median_resolve_seconds,
            "note_rate": note_rate,
            "oldest_pending_seconds": oldest_pending_seconds,
            # Rubber-stamp: enough volume, resolved nearly every time, and the
            # notes have stopped carrying information.
            "fatigue": (
                resolved >= MIN_SAMPLE
                and resolution_rate is not None
                and resolution_rate >= FATIGUE_RATE
                and note_rate is not None
                and note_rate <= FATIGUE_NOTE_RATE
            ),
            # Backlog: at least one item has been waiting past the threshold.
            "stale": (
                pending > 0
                and oldest_pending_seconds is not None
                and oldest_pending_seconds > STALE_SECONDS
            ),
            # Never acted on: enqueued repeatedly, zero resolutions. The queue
            # looks active while the gate is off.
            "abandoned": enqueued >= MIN_SAMPLE and resolved == 0,
        })

    gates.sort(key=lambda g: (-g["enqueued"], g["gate_id"]))
    return {"n": len(rows), "since": since, "gates": gates}


def _fmt(value: float | None, spec: str = ".3f") -> str:
    """Render a rate/duration, or ``-`` when it is ``None`` (never ``0.000``)."""
    return "-" if value is None else format(value, spec)


def summarize(report: dict) -> str:
    """Human-readable one-line-per-gate rendering of a ``calibrate()`` report.

    `None` renders as `-`; the empty report renders the literal marker
    `no gate inbox items recorded yet`.
    """
    if report["n"] == 0:
        return "no gate inbox items recorded yet"
    lines: list[str] = []
    for g in report["gates"]:
        flags = ",".join(
            k for k in ("fatigue", "stale", "abandoned") if g[k]
        ) or "-"
        lines.append(
            f"{g['gate_id']}  {g['enqueued']}/{g['resolved']}/{g['pending']}  "
            f"rate {_fmt(g['resolution_rate'])}  notes {_fmt(g['note_rate'])}  "
            f"{flags}"
        )
    return "\n".join(lines)
