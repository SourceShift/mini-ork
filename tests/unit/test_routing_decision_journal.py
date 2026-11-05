"""The routing path must journal the decision it just made.

``policy_decisions`` was empty on a live home even though
``policies.engine.record_decision()`` exists and intake admission calls it:
these workloads never reach admission, so every lane the router committed to
was archived nowhere. The producer is the one that already exists; this pins
the call.

The second thing pinned here is where it may write. Resolution is an explicit
``MINI_ORK_DB`` / ``MINI_ORK_HOME`` and nothing else — a ``./.mini-ork/state.db``
fallback is what ``MINI_ORK_NO_CWD_DB`` exists to close, after a verifier's
pytest resolved the operator's checkout and wrote ~1,900 fixture events into the
live db (2026-10-13). ``tests/conftest.py`` pops ``MINI_ORK_DB``/``MINI_ORK_HOME``
for every test, so these tests set the variable themselves — the same way any
caller in production must.
"""
from __future__ import annotations

import sqlite3

from mini_ork.dispatch import routing

# Column set and constraints copied from db/migrations/0026_policy_state.sql, so
# a schema change breaks this test instead of letting it pass against a
# hand-rolled table that no longer matches the real one.
_DDL = """
CREATE TABLE policy_decisions (
    decision_id    TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    policy_name    TEXT NOT NULL,
    result         TEXT NOT NULL
                       CHECK(result IN ('ALLOW', 'DENY',
                                        'REQUIRE_APPROVAL', 'LOG_ONLY')),
    reason         TEXT,
    evaluated_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    payload_json   TEXT
);
"""


def _state_db(path):
    con = sqlite3.connect(path)
    con.executescript(_DDL)
    con.commit()
    con.close()
    return path


def _rows(db):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT event_type, policy_name, result, reason FROM policy_decisions"
        ).fetchall()
    finally:
        con.close()


def test_policy_route_lane_journals_the_lane_it_commits_to(tmp_path, monkeypatch):
    db = _state_db(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    lane = routing.policy_route_lane("implementer", "sonnet")

    assert lane == "kimi"
    rows = _rows(db)
    assert len(rows) == 1, "one decision, one journal row"
    event_type, policy_name, result, reason = rows[0]
    assert event_type == "lane_routing"
    assert policy_name == "test_journal"
    assert result == "ALLOW"
    assert "sonnet->kimi" in reason


def test_a_dry_run_leaves_no_journal_row(tmp_path, monkeypatch):
    """A preview must not journal a decision the router never made."""
    db = _state_db(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    assert routing.policy_route_lane("implementer", "sonnet", dry_run=True) == "sonnet"
    assert _rows(db) == []


def test_predicting_a_lane_does_not_journal_it(tmp_path, monkeypatch):
    """``journal=False`` is the preflight contract.

    ``cli/lane_preflight.py`` calls the router to *predict* every node's lane for
    its "waiting on" message. Those are not decisions, and journaling them would
    both fill the table with lanes never dispatched and double-count the node
    when the real dispatch routes it a moment later.
    """
    db = _state_db(tmp_path / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    assert routing.policy_route_lane("implementer", "sonnet", journal=False) == "kimi"
    assert _rows(db) == []


def test_without_an_explicit_db_nothing_is_written_to_the_working_directory(
    tmp_path, monkeypatch
):
    """No ``MINI_ORK_DB``/``MINI_ORK_HOME`` ⇒ no journal row, and no new db file.

    The CWD fallback is the hazard ``MINI_ORK_NO_CWD_DB`` (RBF-27/RB-27) was
    added to close: a pytest or a node run from the operator's checkout resolved
    ``./.mini-ork/state.db`` and wrote its fixtures into the live database. The
    conftest pops both variables for exactly that reason, so this is also the
    configuration every other test in this suite runs under.
    """
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    assert routing.policy_route_lane("implementer", "sonnet") == "kimi"
    assert not (tmp_path / ".mini-ork" / "state.db").exists()


def test_an_unwritable_journal_does_not_change_the_lane(tmp_path, monkeypatch):
    """Journaling is an audit side effect; it must never re-route a node."""
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("MO_ROUTING_POLICY", "test_journal")
    monkeypatch.setitem(routing.POLICY_REGISTRY, "test_journal", lambda ctx: "kimi")

    assert routing.policy_route_lane("implementer", "sonnet") == "kimi"
