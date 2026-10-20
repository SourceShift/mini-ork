"""RB-34: dangling node events pair in time order and keep a legal finish_reason.

Kickoff: mo-kickoffs/run-bugs-20261020/rbf-event-pairing.md

Two independent defects, both hermetic (a fresh sqlite state.db built from the
repo's shipped migrations in ``tmp_path``; the operator's live
``.mini-ork/state.db`` is never resolved or touched):

1. ``mini_ork.web.control._close_dangling_node_events`` paired starts and ends
   as SETS of node ids. A node that ran twice (start, end, start) has an end
   for its id, so the second start stayed open forever; the synthetic INSERT
   also omitted the indexed ``finish_reason`` column.
2. ``mini_ork.observability.node_events`` wrote NULL into that column whenever
   the emitted reason fell outside migration 0021's CHECK list
   (``impl_moved_base``, ``publish_abstain``, ``config``, …).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.observability import node_events  # noqa: E402
from mini_ork.web import control  # noqa: E402

RUN = "rb34-hermetic"


def _make_state_db(tmp_path: Path) -> Path:
    """A fresh state.db carrying the repo's real schema (incl. 0021's CHECK)."""
    from mini_ork.stores.migrate import init_db

    db = tmp_path / "state.db"
    db.touch()
    rc, _out, err = init_db(str(db), root=str(REPO))
    assert rc == 0, f"hermetic init_db failed: {err}"
    return db


def _seed(con, event_id, event_type, node_id, created_at, finish_reason=None):
    payload = {"node_id": node_id, "node_type": "implementer"}
    if finish_reason is not None:
        payload["finish_reason"] = finish_reason
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json,"
        " created_at, finish_reason) VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, RUN, event_type, json.dumps(payload), created_at,
         finish_reason),
    )


def _ends_for(con, node_id):
    """All node_end rows naming ``node_id``, as (event_id, finish_reason)."""
    out = []
    for eid, fr, pj in con.execute(
        "SELECT event_id, finish_reason, payload_json FROM run_events"
        " WHERE run_id = ? AND event_type = 'node_end'",
        (RUN,),
    ).fetchall():
        if (json.loads(pj or "{}") or {}).get("node_id") == node_id:
            out.append((eid, fr))
    return out


def _legal_finish_reasons(con) -> set[str]:
    """The CHECK vocabulary parsed from the live run_events DDL."""
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'"
    ).fetchone()[0]
    m = re.search(r"finish_reason[\s\S]{0,400}?CHECK\s*\(", ddl)
    assert m, "run_events DDL must carry the finish_reason CHECK (migration 0021)"
    start = m.end() - 1
    depth = 0
    for i in range(start, len(ddl)):
        if ddl[i] == "(":
            depth += 1
        elif ddl[i] == ")":
            depth -= 1
            if depth == 0:
                return set(re.findall(r"'([^']+)'", ddl[start + 1:i]))
    raise AssertionError("unbalanced CHECK parens in run_events DDL")


def _close(db, run_id=RUN):
    from mini_ork.web.db import StateDB

    control._close_dangling_node_events(StateDB(db), run_id)


def test_start_end_start_adds_exactly_one_synthetic_end(tmp_path):
    """A node that ran twice with one end is closed with one synthetic end."""
    db = _make_state_db(tmp_path)
    con = sqlite3.connect(db)
    _seed(con, "x-start-1", "node_start", "X", 1001)
    _seed(con, "x-end-1", "node_end", "X", 1002, finish_reason="done")
    _seed(con, "x-start-2", "node_start", "X", 1003)
    con.commit()
    con.close()

    _close(db)

    con = sqlite3.connect(db)
    try:
        ends = _ends_for(con, "X")
        synthetic = [(eid, fr) for eid, fr in ends if eid != "x-end-1"]
        assert len(ends) == 2, f"real end plus one synthetic end expected: {ends}"
        assert len(synthetic) == 1, (
            f"start, end, start must be closed by exactly one synthetic end: {ends}"
        )
        assert synthetic[0][1] is not None, (
            "the synthetic end must write the finish_reason column"
        )
        assert synthetic[0][1] == "interrupted"
    finally:
        con.close()


def test_balanced_events_add_no_synthetic_end(tmp_path):
    """Two starts matched by two ends are left alone."""
    db = _make_state_db(tmp_path)
    con = sqlite3.connect(db)
    _seed(con, "x-start-1", "node_start", "X", 1001)
    _seed(con, "x-end-1", "node_end", "X", 1002, finish_reason="done")
    _seed(con, "x-start-2", "node_start", "X", 1003)
    _seed(con, "x-end-2", "node_end", "X", 1004, finish_reason="error")
    con.commit()
    con.close()

    _close(db)

    con = sqlite3.connect(db)
    try:
        assert {eid for eid, _ in _ends_for(con, "X")} == {"x-end-1", "x-end-2"}
        total = con.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ?", (RUN,)
        ).fetchone()[0]
        assert total == 4, f"no rows may be added, run has {total} events"
    finally:
        con.close()


def test_impl_moved_base_end_keeps_legal_column_and_exact_payload(tmp_path):
    """An emitted impl_moved_base end holds a legal column value and the exact
    reason in the payload (the old code wrote NULL into the indexed column)."""
    db = _make_state_db(tmp_path)
    node_events.mo_node_end(
        RUN, "impl-node", "implementer", 1500,
        verdict="FAIL", finish_reason="impl_moved_base", db=str(db),
    )

    con = sqlite3.connect(db)
    try:
        ends = _ends_for(con, "impl-node")
        assert len(ends) == 1
        eid, fr = ends[0]
        legal = _legal_finish_reasons(con)
        assert fr is not None, "finish_reason column must not be NULL"
        assert fr in legal, f"{fr!r} is outside the schema's CHECK list {sorted(legal)}"
        payload = con.execute(
            "SELECT payload_json FROM run_events WHERE event_id = ?", (eid,)
        ).fetchone()[0]
        assert json.loads(payload)["finish_reason"] == "impl_moved_base"
    finally:
        con.close()


@pytest.mark.parametrize("reason", ["publish_abstain", "config"])
def test_out_of_enum_reason_maps_into_the_schema_vocabulary(tmp_path, reason):
    """Abstain-like reasons map to done (0021 has no 'abstain'); failure-like
    reasons map to error — never NULL, always legal."""
    db = _make_state_db(tmp_path)
    node_events.mo_node_end(
        RUN, "n-x", "publisher", 10, verdict="", finish_reason=reason,
        db=str(db),
    )

    con = sqlite3.connect(db)
    try:
        _eid, fr = _ends_for(con, "n-x")[0]
        assert fr in _legal_finish_reasons(con)
        assert fr == ("done" if reason == "publish_abstain" else "error")
    finally:
        con.close()
