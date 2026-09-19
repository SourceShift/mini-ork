#!/usr/bin/env python3
"""predicate-cmd for the PERF goal-loop: does chapter <n> of BOOK_UUID meet the
three-axis contract (quality AND cost AND speed)?

Contract (recipes/goal-loop/lib/goal_state.py::evaluate_units): invoked as
``python3 unit_predicate.py <chapter_number>`` (argv, not shell) inside
MO_GOAL_TARGET_CWD. Exit 0 == the unit passes the goal; any non-zero == the
unit still needs work. The FIRST stdout line is captured as the reason.

Three axes, ALL required, evaluated in this order:

1. QUALITY — hard, never relaxable. ``committed_complete = true AND
   rubric_status = 'pass'`` on ``book_chapter_lifecycle``. Evaluated FIRST; a
   quality failure fails the predicate regardless of cost or speed. No env var
   may weaken it.
2. COST — measured, never estimated. ``sum(cost_usd)`` from ``task_runs``
   (recipe ``verified-artifact``) in ``${MO_GOAL_TARGET_CWD}/.mini-ork/state.db``,
   restricted to the current optimization generation by the ``_epoch`` stamped
   in the budget file. Compared against the chapter's cost ceiling.
3. SPEED — measured from lifecycle timestamps: wall-clock of the most recent
   attempt ``started_at -> finished_at`` (fall back to ``committed_at``, then
   ``updated_at``). Compared against the chapter's seconds ceiling.

FAIL CLOSED: a missing budget file, a missing chapter ceiling, a missing
state.db, a missing lifecycle row, or a NULL timestamp where one is required
all FAIL the unit with a reason naming the missing input. Unmeasurable is
never a pass — the same principle as the promotion gate's authority-capture
check.

Quality comes from libpq env vars; cost comes from the researcher target's
sqlite state.db. No secret lives here.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

_BUDGET_FILENAME = "perf-budget.json"
_RECIPE = "verified-artifact"


def _q(sql: str) -> tuple[int, str, str]:
    """Run a psql query. Returns (returncode, stdout, stderr); never raises."""
    try:
        proc = subprocess.run(
            [
                "psql",
                "-h", os.environ.get("PGHOST", "100.74.239.22"),
                "-p", os.environ.get("PGPORT", "5932"),
                "-U", os.environ.get("PGUSER", "researcher_user"),
                "-d", os.environ.get("PGDATABASE", "researcher_db"),
                "-tA", "-F", "|", "-c", sql,
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except OSError as exc:  # psql not installed / not exec — fail closed
        return 127, "", f"psql exec failed: {exc}"


def _target_cwd() -> str:
    return os.environ.get("MO_GOAL_TARGET_CWD", "").strip() or os.getcwd()


def _budget_path() -> str:
    return os.environ.get("MO_GOAL_PERF_BUDGET", "").strip() or os.path.join(
        _target_cwd(), ".mini-ork", _BUDGET_FILENAME
    )


def _state_db_path() -> str:
    return os.path.join(_target_cwd(), ".mini-ork", "state.db")


def _load_budget() -> tuple[dict | None, str]:
    """Load the perf-budget JSON. Returns (data, error); error names the
    missing input when the budget cannot be resolved."""
    path = _budget_path()
    if not os.path.isfile(path):
        return None, f"budget file missing: {path}"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"budget file unreadable: {path} ({exc})"
    if not isinstance(data, dict):
        return None, f"budget file malformed (not an object): {path}"
    return data, ""


def _measure_cost(epoch: int) -> tuple[float | None, str]:
    """Sum verified-artifact spend since `epoch`. Returns (cost, error)."""
    path = _state_db_path()
    if not os.path.isfile(path):
        return None, f"state.db missing: {path}"
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM task_runs "
                "WHERE recipe = ? AND created_at > ?",
                (_RECIPE, int(epoch)),
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — any sqlite problem is unmeasurable
        return None, f"cost measurement failed: {exc}"
    return float(row[0]), ""


def _lifecycle_row(chapter: str, book: str) -> tuple[list[str] | None, str]:
    """One lifecycle row: quality columns + epoch-seconds timestamps for SPEED."""
    sql = (
        "SELECT status, "
        "coalesce(rubric_status,''), "
        "committed_complete, "
        "EXTRACT(EPOCH FROM started_at)::text, "
        "EXTRACT(EPOCH FROM finished_at)::text, "
        "EXTRACT(EPOCH FROM committed_at)::text, "
        "EXTRACT(EPOCH FROM updated_at)::text "
        "FROM book_chapter_lifecycle "
        f"WHERE book_uuid='{book}' AND chapter_number={chapter};"
    )
    rc, out, err = _q(sql)
    if rc != 0:
        return None, f"db-error: {err.strip()[:80]}"
    rows = out.strip().splitlines()
    if not rows:
        return None, "no lifecycle row for this chapter"
    return (rows[0].split("|") + [""] * 7)[:7], ""


def _fmt(value: float) -> str:
    """Render a float for the reason line: 2 decimals, trailing zeros dropped."""
    s = f"{value:.2f}"
    return s.rstrip("0").rstrip(".")


def _epoch_seconds(text: str) -> float | None:
    """Parse an EXTRACT(EPOCH …)::text cell; empty/NULL -> None."""
    t = text.strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: unit_predicate.py <chapter_number>", file=sys.stderr)
        return 2
    chapter = argv[0].strip()
    if not re.fullmatch(r"\d+", chapter):
        print(f"bad chapter id: {chapter!r}", file=sys.stderr)
        return 2
    book = os.environ.get("BOOK_UUID", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", book):
        print(f"BOOK_UUID unset/invalid: {book!r}", file=sys.stderr)
        return 2

    # ── AXIS 1: QUALITY (first, hard) ────────────────────────────────────────
    cols, err = _lifecycle_row(chapter, book)
    if cols is None:
        print(f"ch{chapter} FAIL quality unmeasurable: {err}")
        return 1
    status, rubric, committed = cols[0], cols[1], cols[2]
    if not (committed == "t" and rubric == "pass"):
        print(
            f"ch{chapter} FAIL quality committed={committed} "
            f"rubric={rubric or 'none'} status={status}"
        )
        return 1

    # ── Ceilings (fail closed) ───────────────────────────────────────────────
    budget, err = _load_budget()
    if budget is None:
        print(f"ch{chapter} FAIL cost unmeasurable: {err}")
        return 1
    entry = budget.get(chapter)
    if not isinstance(entry, dict):
        print(f"ch{chapter} FAIL cost unmeasurable: no ceiling for chapter {chapter}")
        return 1
    cost_ceiling = entry.get("cost_usd")
    speed_ceiling = entry.get("seconds")
    raw_epoch = budget.get("_epoch")
    epoch: int | None = None
    if raw_epoch is not None:
        try:
            epoch = int(raw_epoch)
        except (TypeError, ValueError):
            epoch = None
    if cost_ceiling is None:
        print(f"ch{chapter} FAIL cost unmeasurable: cost_usd ceiling missing for chapter {chapter}")
        return 1
    if speed_ceiling is None:
        print(f"ch{chapter} FAIL speed unmeasurable: seconds ceiling missing for chapter {chapter}")
        return 1
    if epoch is None:
        print(f"ch{chapter} FAIL cost unmeasurable: _epoch missing from budget file")
        return 1
    cost_ceiling = float(cost_ceiling)
    speed_ceiling = float(speed_ceiling)

    # ── AXIS 2: COST ─────────────────────────────────────────────────────────
    cost, err = _measure_cost(epoch)
    if cost is None:
        print(f"ch{chapter} FAIL cost unmeasurable: {err}")
        return 1

    # ── AXIS 3: SPEED (timestamps already fetched with the lifecycle row) ───
    started = _epoch_seconds(cols[3])
    finished = _epoch_seconds(cols[4])
    committed_at = _epoch_seconds(cols[5])
    updated_at = _epoch_seconds(cols[6])
    if started is None:
        print(f"ch{chapter} FAIL speed unmeasurable: started_at NULL where required")
        return 1
    end = finished if finished is not None else (
        committed_at if committed_at is not None else updated_at
    )
    if end is None:
        print(f"ch{chapter} FAIL speed unmeasurable: finished_at/committed_at/updated_at all NULL")
        return 1
    speed = max(0.0, end - started)

    cost_ok = cost <= cost_ceiling
    speed_ok = speed <= speed_ceiling
    if cost_ok and speed_ok:
        print(
            f"ch{chapter} PASS cost={_fmt(cost)}<={_fmt(cost_ceiling)} "
            f"speed={_fmt(speed)}s<={_fmt(speed_ceiling)}s status={status}"
        )
        return 0

    print(
        f"ch{chapter} FAIL "
        f"cost={_fmt(cost)}{'<=' if cost_ok else '>'}{_fmt(cost_ceiling)} "
        f"speed={_fmt(speed)}s{'<=' if speed_ok else '>'}{_fmt(speed_ceiling)}s"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
