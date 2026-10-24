"""resolve_finished walks finished runs newest first.

Hermetic: a temp home and a temp sqlite DB built to match the RSI Tier-0 #1
kickoff. No network, no provider credentials, no operator state.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mini_ork.learning.item_utility import resolve_finished


def _write_ledger(home, run_id: str) -> None:
    learned = home / "runs" / run_id / "learned"
    learned.mkdir(parents=True, exist_ok=True)
    (learned / "planner.json").write_text(
        json.dumps(
            {"held_out": False, "sources": [{"id": f"t:{run_id}"}]}
        ),
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path):
    """20 old failed runs with no ledger, then 10 new published ledger runs."""
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT,"
        " created_at INTEGER)"
    )
    for i in range(20):
        con.execute(
            "INSERT INTO task_runs (id, status, created_at) VALUES (?, ?, ?)",
            (f"old-{i:02d}", "failed", 1000 + i),
        )
    for i in range(10):
        run_id = f"new-{i:02d}"
        con.execute(
            "INSERT INTO task_runs (id, status, created_at) VALUES (?, ?, ?)",
            (run_id, "published", 5000 + i),
        )
        _write_ledger(home, run_id)
    con.commit()
    con.close()
    return home, db


def _connect(db) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def _utility(db) -> dict[str, tuple[int, int]]:
    con = _connect(db)
    try:
        return {
            row["item_id"]: (row["uses"], row["wins"])
            for row in con.execute(
                "SELECT item_id, uses, wins FROM context_item_utility"
            )
        }
    finally:
        con.close()


def _marked(db) -> set[str]:
    con = _connect(db)
    try:
        return {
            row["run_id"]
            for row in con.execute("SELECT run_id FROM context_item_runs")
        }
    finally:
        con.close()


def test_first_window_counts_the_newest_runs(env):
    home, db = env
    assert resolve_finished(str(home), str(db), limit=10) == 10
    assert _utility(db) == {
        f"t:new-{i:02d}": (1, 1) for i in range(10)
    }
    assert not any(
        run_id.startswith("old-") for run_id in _marked(db)
    )


def test_successive_calls_drain_newest_first_without_recounting(env):
    home, db = env

    # The newest 10 are the ledger carriers.
    assert resolve_finished(str(home), str(db), limit=10) == 10
    first = _utility(db)

    # Next 10 newest: old runs, no ledger -> counted, no utility rows.
    assert resolve_finished(str(home), str(db), limit=10) == 10
    assert _utility(db) == first
    assert len(_marked(db)) == 20

    # Remaining 10 old runs.
    assert resolve_finished(str(home), str(db), limit=100) == 10
    assert _utility(db) == first

    # Nothing left.
    assert resolve_finished(str(home), str(db), limit=100) == 0


def test_same_created_at_breaks_ties_by_id_desc(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT,"
        " created_at INTEGER)"
    )
    for run_id in ("tie-a", "tie-b"):
        con.execute(
            "INSERT INTO task_runs (id, status, created_at) VALUES (?, ?, ?)",
            (run_id, "published", 7000),
        )
        _write_ledger(home, run_id)
    con.commit()
    con.close()

    assert resolve_finished(str(home), str(db), limit=1) == 1
    assert _marked(db) == {"tie-b"}
    assert _utility(db) == {"t:tie-b": (1, 1)}
