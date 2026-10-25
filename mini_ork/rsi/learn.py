"""LEARN — what one finished cycle teaches the next one.

Two deterministic records, both written after the ledger has already decided the
cycle's status:

* ``rsi_credit`` — one row per cycle that reached a candidate, so merge rate by
  ``(source, lens)`` can reorder the next queue. ``queue`` ignores a group until
  ``credit_n_min`` observations exist, so the first cycles cannot be gamed by
  noise: one merge out of one is not evidence.
* ``collapse_history`` — one row per measured cycle, ``score`` = the rolling
  merge rate over the last ``WINDOW`` cycles, ``anchor`` = the fraction of this
  cycle's deterministic probes that came back green. This pair is the point:
  the collapse signature is a score that climbs while an independent anchor
  decays, and a loop watching only its merge rate cannot see that.

The detector needs ``collapse_detector.MIN_STEPS`` (4) rows, so **the halt
cannot fire before the fifth cycle**. That is not a bug to work around — a
loop that can panic on its first measurement is a loop nobody can trust — and
a test pins it so a future ``MIN_STEPS`` change is a deliberate one.

No model call, and no lesson authoring. A lesson drawn from a single cycle is a
loop learning its own noise; lessons are only considered once the same failure
signature has been seen twice and has cleared ``lesson_conflict``.

Failure policy: every writer here is fail-open at the *cycle* level. A merged
cycle whose bookkeeping failed is still merged — ``_finish`` in ``cycle.py``
swallows the exception on purpose. The collapse trip is the one write whose
failure is *reported* rather than dropped: it comes back in the returned report
as ``collapse_error`` so the caller can shout about it on stderr. It cannot fail
the cycle, because the cycle is already finished by then and rewriting a
``merged`` row to ``error`` would misreport work that landed.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mini_ork.learning import collapse_detector
from mini_ork.learning import collapse_history

from . import ledger

#: Task class the RSI cycle writes under. Shared with the queue's operator
#: tooling and pinned by tests — a rename silently orphans every prior row.
TASK_CLASS = "rsi_cycle"

#: Rolling window for the score. Bounded by the detector: it needs
#: ``MIN_STEPS`` rows (4) and reads halves, so a longer history just makes the
#: halves overlap more of one another.
WINDOW = 10

#: Probe statuses that count as a healthy anchor. ``findings`` is green: the
#: probe ran and reported, which is the measurement succeeding. ``error`` and
#: ``timeout`` are what the anchor is meant to catch, ``skipped`` measured
#: nothing and is excluded from the denominator entirely.
GREEN = ("ok", "findings")


def merge_rate(con: sqlite3.Connection, *, window: int = WINDOW) -> float | None:
    """Fraction of the last ``window`` finished cycles that merged.

    ``None`` when no cycle has finished yet — an empty history has no rate, and
    writing ``0.0`` would tell the detector the loop never merges.
    """
    rows = con.execute(
        "SELECT status FROM rsi_cycles WHERE status <> 'running'"
        " ORDER BY started_at DESC, cycle_id DESC LIMIT ?",
        (int(window),),
    ).fetchall()
    if not rows:
        return None
    merged = sum(1 for row in rows if row[0] == "merged")
    return merged / len(rows)


def probe_anchor(con: sqlite3.Connection, cycle_id: str) -> float | None:
    """Fraction of this cycle's probes that were green. ``None`` if none ran."""
    rows = con.execute(
        "SELECT status, COUNT(*) FROM rsi_probe_runs WHERE cycle_id = ?"
        " GROUP BY status",
        (cycle_id,),
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row[0])
        if status == "skipped":
            continue
        counts[status] = counts.get(status, 0) + int(row[1])
    total = sum(counts.values())
    if total == 0:
        return None
    green = sum(n for status, n in counts.items() if status in GREEN)
    return green / total


def record(
    con: sqlite3.Connection,
    *,
    cycle_id: str,
    status: str,
    candidate_id: str | None = None,
    lens: str | None = None,
    home: Path | str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Write this cycle's credit + collapse rows. Returns a small report.

    ``home`` enables the collapse trip: when the freshly-appended row completes
    a collapse signature, ``<home>/rsi/DISABLED`` is written so the *next*
    cycle halts. The cycle that tripped it keeps the status it earned — rewriting
    a merged cycle's row to ``halted`` would be a lie about what happened.
    """
    when = int(time.time() if now is None else now)
    report: dict[str, Any] = {
        "credit": _record_credit(
            con, cycle_id=cycle_id, status=status, candidate_id=candidate_id,
            lens=lens, now=when,
        ),
        "step": None,
        "score": None,
        "anchor": None,
        "collapse": False,
    }

    score = merge_rate(con)
    anchor = probe_anchor(con, cycle_id)
    report["score"] = score
    report["anchor"] = anchor
    if score is None or anchor is None:
        # A row needs both halves of the signature. Measuring one and
        # inventing the other is how a detector learns a false trend.
        return report

    step = _step_for_run(con, cycle_id)
    if step is None:
        step = collapse_history.append(
            con, task_class=TASK_CLASS, score=score, anchor=anchor,
            run_id=cycle_id, now=when,
        )
    report["step"] = step
    if step is None or home is None:
        return report

    detector = collapse_detector.detect(collapse_history.history(con, TASK_CLASS))
    report["collapse_report"] = detector
    if not detector["collapse"]:
        return report

    report["collapse"] = True
    try:
        report["disabled_path"] = str(_trip(home, detector))
    except OSError as exc:
        # The conclusion stands even when arming the stop failed; the caller
        # reports it. Never let this rewrite the status of a finished cycle.
        report["collapse_error"] = f"{type(exc).__name__}: {exc}"
    return report


def _record_credit(
    con: sqlite3.Connection,
    *,
    cycle_id: str,
    status: str,
    candidate_id: str | None,
    lens: str | None,
    now: int,
) -> bool:
    """One credit row per cycle. Idempotent: a replayed ``_finish`` does not
    double-count, which would quietly shift every ``credit_stats`` rate."""
    if not candidate_id:
        return False
    if con.execute("SELECT 1 FROM rsi_credit WHERE cycle_id = ?", (cycle_id,)).fetchone():
        return False
    cand = ledger.get_candidate(con, candidate_id)
    if cand is None:
        return False
    cycle = ledger.get_cycle(con, cycle_id)
    ledger.record_credit(
        con,
        cycle_id=cycle_id,
        now=now,
        candidate_id=candidate_id,
        source=str(cand["source"]),
        lens=lens,
        domain=str(cand["domain"]),
        outcome=status,
        severity=int(cand["severity"] or 0),
        utility=1.0 if status == "merged" else 0.0,
        cost_usd=float((cycle or {}).get("cost_usd") or 0.0),
    )
    return True


def _step_for_run(con: sqlite3.Connection, cycle_id: str) -> int | None:
    """The step already written for this cycle, if any (``run_id`` is the id).

    ``collapse_history`` may be absent on an un-updated DB; that is the same
    fail-open condition ``append`` honours, so it reads as "not written yet".
    """
    try:
        row = con.execute(
            "SELECT step FROM collapse_history WHERE task_class = ? AND run_id = ?",
            (TASK_CLASS, cycle_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None else None


def _trip(home: Path | str, report: Mapping[str, Any]) -> Path:
    """Write the durable stop. Deliberately NOT swallowed by the caller."""
    path = Path(home) / "rsi" / "DISABLED"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"collapse: score_rise={report['score_rise']} "
        f"anchor_drop={report['anchor_drop']} — the merge rate is rising while"
        " probe health falls.\n"
        "Inspect `mini-ork rsi-cycle list --limit 10` and the recent "
        "quarantines, then delete this file to resume.\n",
        encoding="utf-8",
    )
    return path
