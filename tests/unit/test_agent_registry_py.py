"""Unit tests for mini_ork.registries.agent_registry.

In-process coverage of the four public functions (register / get / current /
performance) against fresh temp DBs, plus the error paths and a
migrated-schema bootstrap via the native ``mini_ork.stores.migrate.init_db``.
Float rounding uses Python's stdlib round() — asserted exactly where the
values are deterministic.

Cases:
  1. register returns version_id; get returns row
  2. register retires previous active
  3. current returns active or None
  4. get unknown version returns None
  5. performance aggregates traces
  6. register errors (invalid JSON + missing 'model')
  7. init_db-bootstrapped DB works on the real-migration schema
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.registries import agent_registry as ar  # noqa: E402
from mini_ork.stores.migrate import init_db  # noqa: E402

# Minimal execution_traces schema for the perf case (columns mirror what
# agent_performance's join needs; the real canonical schema lives in
# db/migrations/0010_benchmarks.sql).
TRACES_DDL = """
    CREATE TABLE IF NOT EXISTS execution_traces (
        trace_id         TEXT PRIMARY KEY,
        run_id           INTEGER NOT NULL DEFAULT 0,
        agent_version_id TEXT NOT NULL DEFAULT '',
        task_class       TEXT NOT NULL DEFAULT '',
        cost_usd         REAL NOT NULL DEFAULT 0.0,
        duration_ms      INTEGER NOT NULL DEFAULT 0,
        status           TEXT NOT NULL CHECK (status IN ('success','failure'))
    );
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh temp DB path pinned via MINI_ORK_DB."""
    dbp = str(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    return dbp


# ───────────────────────── Case 1: register + get round-trip ──────────────────

def test_register_returns_version_id_and_get_returns_row(db):
    payload = {"model": "claude-sonnet-4-5",
               "provider": "anthropic",
               "tools": ["read", "write"],
               "task_classes": ["code-review"]}

    vid = ar.register("planner", payload)
    assert vid.startswith("av-plann") and len(vid) > len("av-plann")

    row = ar.get("planner", vid)
    assert row is not None
    assert row["version_id"] == vid
    assert row["model"] == "claude-sonnet-4-5"
    assert row["provider"] == "anthropic"
    assert row["role"] == "planner"
    assert row["status"] == "active"


# ─────────────────── Case 2: register retires previous active ──────────────────

def test_register_retires_previous_active(db):
    vid_a = ar.register("planner", {"model": "claude-sonnet-4-5"})
    vid_b = ar.register("planner", {"model": "claude-opus-4-5"})

    with sqlite3.connect(db) as con:
        statuses = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT version_id, status FROM agent_registry "
                "WHERE role='planner' ORDER BY registered_at"
            ).fetchall()
        }

    # exactly one active + one retired
    assert statuses[vid_a] == "retired"
    assert statuses[vid_b] == "active"
    assert sorted(statuses.values()) == ["active", "retired"]


# ─────────────────────── Case 3: current returns active / None ─────────────────

def test_current_returns_active_or_none(db):
    ar.register("planner", {"model": "m1"})

    cur = ar.current("planner")
    assert cur is not None
    assert cur["status"] == "active"
    assert cur["model"] == "m1"

    # Unknown role → None
    assert ar.current("role-no-such-xyz") is None


# ───────────────────────── Case 4: get unknown version → None ──────────────────

def test_get_unknown_version_returns_none(db):
    # Bootstrap so the table exists.
    with sqlite3.connect(db) as con:
        con.executescript(ar.SCHEMA_SQL)
        con.commit()

    assert ar.get("planner", "av-doesnotexist") is None


# ─────────────────────── Case 5: performance aggregates traces ────────────────

def test_performance_aggregates_traces(db):
    vid = ar.register("planner", {"model": "m1"})

    # Seed execution_traces: 1 success + 1 failure for the version.
    with sqlite3.connect(db) as con:
        con.executescript(TRACES_DDL)
        con.execute(
            "INSERT INTO execution_traces "
            "(trace_id, agent_version_id, task_class, cost_usd, duration_ms, status) "
            "VALUES (?, ?, 'code-review', 0.05, 1500, 'success')",
            (f"tr-{vid}-1", vid),
        )
        con.execute(
            "INSERT INTO execution_traces "
            "(trace_id, agent_version_id, task_class, cost_usd, duration_ms, status) "
            "VALUES (?, ?, 'code-review', 0.02, 800, 'failure')",
            (f"tr-{vid}-2", vid),
        )
        con.commit()

    perf = ar.performance("planner")

    assert perf["role"] == "planner"
    assert perf["version_count"] == 1
    assert len(perf["versions"]) == 1

    v = perf["versions"][0]
    assert v["version_id"] == vid
    assert v["total_runs"] == 2
    assert v["success_runs"] == 1
    assert abs(v["success_rate"] - 0.5) < 1e-6
    # avg_cost = (0.05 + 0.02)/2 = 0.035
    assert abs(v["avg_cost_usd"] - 0.035) < 1e-6
    # avg_duration = (1500 + 800)/2 = 1150.0
    assert abs(v["avg_duration_ms"] - 1150.0) < 0.1


# ─────────────────────────── Case 6: error paths ───────────────────────────────

def test_register_rejects_invalid_json(db):
    with pytest.raises(ValueError, match="agent_register.*invalid JSON"):
        ar.register("executor", "not-json")


def test_register_rejects_missing_model(db):
    with pytest.raises(ValueError, match="agent_register.*model"):
        ar.register("executor", {"provider": "anthropic"})


# ───────────── Case 7: init_db-bootstrapped DB (slow, opt-in skip) ────────────

def test_init_db_bootstrapped_db(tmp_path, monkeypatch):
    """Run the native init_db against a temp DB, then register/get/current on
    THAT DB (proves the module works on a real-migration schema, not just its
    own lazy CREATE TABLE). Slow — 80+ migrations.
    Set MO_FAST_PARITY=1 to skip."""
    if os.environ.get("MO_FAST_PARITY"):
        pytest.skip("MO_FAST_PARITY=1 set; skipping slow init_db bootstrap")

    dbp = str(tmp_path / "state.db")
    rc, out, err = init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    monkeypatch.setenv("MINI_ORK_DB", dbp)

    vid = ar.register("planner", {"model": "m1"})

    with sqlite3.connect(dbp) as con:
        row = con.execute(
            "SELECT model, role, status FROM agent_registry WHERE version_id=?",
            (vid,),
        ).fetchone()
        assert row == ("m1", "planner", "active")

        # The migrated schema's agent_registry covers the module's DDL columns.
        migrated_cols = {r[1] for r in con.execute("PRAGMA table_info(agent_registry)").fetchall()}
        # And execution_traces exists from the real migration.
        t = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_traces'"
        ).fetchone()
        assert t is not None

    tmp_ddl_db = str(tmp_path / "ddl.db")
    with sqlite3.connect(tmp_ddl_db) as con:
        con.executescript(ar.SCHEMA_SQL)
        ddl_cols = {r[1] for r in con.execute("PRAGMA table_info(agent_registry)").fetchall()}
    assert ddl_cols <= migrated_cols

    # current() round-trips on the migrated schema too.
    cur = ar.current("planner")
    assert cur is not None and cur["version_id"] == vid
