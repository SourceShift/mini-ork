"""Shared ledger reader for the rolling-24h USD budget guards.

Every budget guard in the runtime (``scheduler.today_cost_usd``,
``dispatch.llm_dispatch.cost_circuit_open``, ``cli.main._spent_last_24h``,
``cli.self_improve.pre_iter_cost_check``,
``orchestration.conductor._today_cost``) used to sum ``task_runs.cost_usd``,
which only carries what a node handler explicitly charged. Stage rows
(provider records the profile answerer, rubric judge, gradient-extract, jury, lens
panel) live only in ``llm_calls``. Measured 2026-10-03 on run ``1791012731``:
``task_runs`` said $0.394 of spend, ``llm_calls`` summed $1.75 — the meter
read roughly a third of real spend.

This module is the one definition of "rolling-24h USD spend" the runtime
reads from. Callers delegate; the SQL lives here.

The same flat-connection rule applies to ``run_cost`` / ``reconcile_run_cost``
(added 2026-10-03, run-cost-1791018816): a ``mode=ro`` reader cannot create
the ``-shm`` file of a WAL database, so an idle db would read $0 and silently
leave ``task_runs.cost_usd`` unchanged.
"""
from __future__ import annotations

import os
import sqlite3
import time


def spent_last_24h(db: str | os.PathLike | None) -> float:
    """Rolling-24h USD spend summed from the per-dispatch ledger.

    Reads ``llm_calls.cost_usd`` with a ``task_runs`` fallback for DBs behind
    the migrator. Returns ``0.0`` on a missing/None path, a non-existent file,
    a non-SQLite file, or any other ``sqlite3.Error`` — the budget guards all
    treat absent spend as zero, not as an exception.

    Callers resolve the db path themselves; this function is a pure reader.
    """
    if not db:
        return 0.0
    path = os.fspath(db)
    if not os.path.isfile(path):
        return 0.0
    # A plain connection, NOT ``mode=ro``: a read-only connection cannot create
    # the ``-shm`` file of a WAL database, so on an idle db (no writer has it
    # open, no sidecar files) every query fails and the guard would read $0 —
    # a budget circuit that never trips. SELECTs never write the db.
    try:
        con = sqlite3.connect(path, timeout=5)
    except sqlite3.Error:
        return 0.0
    try:
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM llm_calls "
                "WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S','now','-24 hours')"
            ).fetchone()
            return float(row[0] or 0)
        except sqlite3.OperationalError:
            # No llm_calls table — DB behind the migrator. Fall back to the
            # task_runs sum so a stale DB degrades to the old estimate rather
            # than to a blind zero.
            try:
                row = con.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) FROM task_runs "
                    "WHERE created_at >= ?",
                    (int(time.time()) - 86400,),
                ).fetchone()
                return float(row[0] or 0)
            except sqlite3.Error:
                return 0.0
        except sqlite3.Error:
            return 0.0
    finally:
        con.close()


def run_cost(db: str | os.PathLike | None, run_id: str) -> float | None:
    """Total USD spend for a single run, summed from the per-dispatch ledger.

    Returns ``None`` when the db path is empty, the file does not exist, the
    table or column is missing, the run has no ``llm_calls`` rows, or any
    ``sqlite3.Error`` fires — callers treat "cannot read" as "do nothing",
    same as ``spent_last_24h`` does for the rolling-24h gates.

    Plain connection (never ``mode=ro``); the idle-WAL trap documented at the
    top of this module applies.
    """
    if not db or not run_id:
        return None
    path = os.fspath(db)
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(path, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM llm_calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None or row[0] == 0:
        return None
    return float(row[1])


def reconcile_run_cost(db: str | os.PathLike | None, run_id: str) -> float | None:
    """Raise ``task_runs.cost_usd`` to the ledger total for ``run_id``.

    Reads ``task_runs.cost_usd``; when the ledger sum is not ``None`` and is
    strictly greater than the stored value, writes the new total plus an
    ``updated_at`` bump. Never lowers the stored value (a node handler may
    charge spend the ledger lacks). Returns the value the column holds after
    the call (or ``None`` for an unknown run, a missing table/column, or any
    ``sqlite3.Error`` — the caller treats that as "could not reconcile").

    Plain connection; commits and closes on every code path.
    """
    if not db or not run_id:
        return None
    path = os.fspath(db)
    if not os.path.isfile(path):
        return None
    ledger = run_cost(db, run_id)
    try:
        con = sqlite3.connect(path, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT cost_usd FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        stored = row[0]
        if ledger is not None and (stored is None or ledger > float(stored)):
            con.execute(
                "UPDATE task_runs SET cost_usd = ?, updated_at = ? WHERE id = ?",
                (float(ledger), int(time.time()), run_id),
            )
            con.commit()
            return float(ledger)
        return None if stored is None else float(stored)
    except sqlite3.Error:
        return None
    finally:
        con.close()
