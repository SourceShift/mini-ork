"""Unit tests for idempotent recovery requests + budget bound (E3).

Pins design §5 + the kickoff acceptance:
  * two identical recovery requests → ONE row; the node runs once (scenario 6)
  * a different (run, from_node, strategy) tuple → a distinct request
  * dispatch is budget-bounded: once cost would exceed budget_usd, no more
    dispatches (the "never an unbounded auto-retry loop" guard)
  * mark_dispatched refuses a closed request; close_recovery moves to a
    terminal state and records the failure_class
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.stores import lease

RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_requests (
    request_id           TEXT    PRIMARY KEY,
    run_id               TEXT    NOT NULL,
    from_node            TEXT    NOT NULL,
    strategy             TEXT    NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('pending','dispatched','completed','failed')),
    failure_class        TEXT,
    budget_usd           REAL    NOT NULL DEFAULT 5.00,
    cost_usd             REAL    NOT NULL DEFAULT 0.0,
    dispatch_count       INTEGER NOT NULL DEFAULT 0,
    owner_token          TEXT,
    created_at           INTEGER NOT NULL,
    last_dispatched_at   INTEGER,
    closed_at            INTEGER,
    payload_json         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_requests_idem
    ON recovery_requests(run_id, from_node, strategy);
CREATE INDEX IF NOT EXISTS idx_recovery_requests_status
    ON recovery_requests(run_id, status);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(RECOVERY_SCHEMA)
    con.commit()
    con.close()
    return str(p)


# ── scenario 6 (idempotency half): same tuple → one row, one dispatch ──────
def test_duplicate_request_is_idempotent(db_path):
    first = lease.request_recovery(db_path, "run1", "critic", "retry", now=1000)
    second = lease.request_recovery(db_path, "run1", "critic", "retry", now=1001)
    assert first is not None and second is not None
    rid1, created1 = first
    rid2, created2 = second
    assert created1 is True
    assert created2 is False           # the second is a no-op…
    assert rid1 == rid2                 # …and returns the FIRST request's id
    con = sqlite3.connect(db_path)
    n = con.execute("SELECT COUNT(*) FROM recovery_requests WHERE run_id='run1'").fetchone()[0]
    con.close()
    assert n == 1                       # exactly one row → the node runs once


def test_distinct_tuple_makes_distinct_request(db_path):
    r1 = lease.request_recovery(db_path, "run1", "critic", "retry", now=1000)
    r2 = lease.request_recovery(db_path, "run1", "critic", "repair", now=1000)   # different strategy
    r3 = lease.request_recovery(db_path, "run1", "impl", "retry", now=1000)      # different node
    assert r1[0] != r2[0] != r3[0]
    assert all(x[1] is True for x in (r1, r2, r3))


def test_find_and_get_recovery_roundtrip(db_path):
    rid, _ = lease.request_recovery(db_path, "run1", "critic", "retry", budget_usd=3.0, now=1000)
    by_tuple = lease.find_recovery(db_path, "run1", "critic", "retry")
    by_id = lease.get_recovery(db_path, rid)
    assert by_tuple["request_id"] == rid
    assert by_id["status"] == "pending"
    assert by_id["budget_usd"] == 3.0
    assert by_id["cost_usd"] == 0.0


# ── budget bound: dispatch stops before exceeding budget_usd ────────────────
def test_dispatch_is_budget_bounded(db_path):
    rid, _ = lease.request_recovery(db_path, "run1", "critic", "repair", budget_usd=1.00, now=1000)
    # each repair costs 0.40 → two fit (0.80), the third would hit 1.20 > 1.00
    assert lease.mark_dispatched(db_path, rid, owner_token="T", cost_usd=0.40, now=1001) is True
    assert lease.mark_dispatched(db_path, rid, owner_token="T", cost_usd=0.40, now=1002) is True
    assert lease.can_dispatch(db_path, rid, projected_cost_usd=0.40) is False
    assert lease.mark_dispatched(db_path, rid, owner_token="T", cost_usd=0.40, now=1003) is False
    rec = lease.get_recovery(db_path, rid)
    assert rec["dispatch_count"] == 2
    assert abs(rec["cost_usd"] - 0.80) < 1e-9


def test_mark_dispatched_records_owner_token_and_count(db_path):
    rid, _ = lease.request_recovery(db_path, "run1", "critic", "retry", budget_usd=5.0, now=1000)
    lease.mark_dispatched(db_path, rid, owner_token="lease-tok", cost_usd=0.0, now=1001)
    rec = lease.get_recovery(db_path, rid)
    assert rec["owner_token"] == "lease-tok"
    assert rec["dispatch_count"] == 1
    assert rec["status"] == "dispatched"


# ── close: terminal state + failure_class recorded; no more dispatches ──────
def test_close_recovery_blocks_further_dispatch(db_path):
    rid, _ = lease.request_recovery(db_path, "run1", "critic", "retry", budget_usd=5.0, now=1000)
    assert lease.close_recovery(db_path, rid, status="failed", failure_class="terminal", now=1002) is True
    rec = lease.get_recovery(db_path, rid)
    assert rec["status"] == "failed"
    assert rec["failure_class"] == "terminal"
    # a closed request cannot be dispatched again
    assert lease.can_dispatch(db_path, rid, projected_cost_usd=0.0) is False
    assert lease.mark_dispatched(db_path, rid, owner_token="T", cost_usd=0.0, now=1003) is False


def test_close_recovery_completed(db_path):
    rid, _ = lease.request_recovery(db_path, "run1", "critic", "retry", now=1000)
    assert lease.close_recovery(db_path, rid, status="completed", failure_class="infra_interrupt", now=1005) is True
    assert lease.get_recovery(db_path, rid)["status"] == "completed"


def test_close_recovery_rejects_bad_status(db_path):
    rid, _ = lease.request_recovery(db_path, "run1", "critic", "retry", now=1000)
    assert lease.close_recovery(db_path, rid, status="pending", now=1005) is False
