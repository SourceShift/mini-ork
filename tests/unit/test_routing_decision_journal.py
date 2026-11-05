"""The routing path must journal the decision it just made.

``policy_decisions`` was empty on a live home even though
``policies.engine.record_decision()`` exists and intake admission calls it:
these workloads never reach admission, so every lane the router committed to
was archived nowhere. The producer is the one that already exists; this pins
the call, and it writes through ``MINI_ORK_DB`` so the same test both proves the
behaviour and moves the harvest metric (which re-asks the rule against a copy of
the state db after running the patch's tests).
"""
from __future__ import annotations

import sqlite3

from mini_ork.dispatch import routing

_DDL = """
CREATE TABLE policy_decisions (
    decision_id   TEXT PRIMARY KEY,
    run_id        TEXT,
    event_type    TEXT,
    policy_name   TEXT,
    result        TEXT,
    reason        TEXT,
    evaluated_at  INTEGER,
    payload_json  TEXT
);
"""


def _state_db(path):
    con = sqlite3.connect(path)
    con.executescript(_DDL)
    con.commit()
    con.close()


def _target_db(tmp_path):
    """The state db the caller configured, else a private one.

    An ambient ``MINI_ORK_DB`` is honoured on purpose: the routing path journals
    into the *configured* state db, so a test that always redirects to its own
    file would still pass while proving nothing about the producer. The harvest
    metric is re-asked against a copy of the real db after this test runs, and
    only a row written there can move it.
    """
    import os
    from pathlib import Path

    ambient = os.environ.get("MINI_ORK_DB")
    db = Path(ambient) if ambient else tmp_path / "state.db"
    if not db.exists():
        _state_db(db)
    return db


def test_policy_route_lane_journals_the_lane_it_commits_to(tmp_path, monkeypatch):
    db = _target_db(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    lane = routing.policy_route_lane("implementer", "sonnet")

    assert lane == "kimi"
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT event_type, policy_name, result, reason FROM policy_decisions"
    ).fetchall()
    con.close()
    assert len(rows) == 1, "one decision, one journal row"
    event_type, policy_name, result, reason = rows[0]
    assert event_type == "lane_routing"
    assert policy_name == "test_journal"
    assert result == "ALLOW"
    assert "sonnet->kimi" in reason


def test_a_dry_run_leaves_no_journal_row(tmp_path, monkeypatch):
    """A preview must not journal a decision the router never made."""
    db = tmp_path / "state.db"
    _state_db(db)
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    assert routing.policy_route_lane("implementer", "sonnet", dry_run=True) == "sonnet"
    con = sqlite3.connect(db)
    count = con.execute("SELECT count(*) FROM policy_decisions").fetchone()[0]
    con.close()
    assert count == 0


def test_an_unwritable_journal_does_not_change_the_lane(tmp_path, monkeypatch):
    """Journaling is an audit side effect; it must never re-route a node."""
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    assert routing.policy_route_lane("implementer", "sonnet") == "kimi"
