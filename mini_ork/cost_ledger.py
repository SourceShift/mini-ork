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