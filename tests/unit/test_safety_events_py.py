"""Standalone unit tests for ``mini_ork.stores.safety_events``.

Replaces the bash-parity gate (against ``lib/safety_events.sh``) as part
of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer runs the LIVE bash subprocess —
it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (rc semantics, id shape,
row contents, idempotent emit, status transitions, table-missing no-op +
warn stderr), now asserted on the port's output.

Eight cases:
  (1) emit valid                — rc=0, 32-hex id, row present with
                                   matching tripwire/severity/run_id.
  (2) invalid severity rejected — rc=2 + "invalid severity" stderr.
  (3) bad JSON rejected          — rc=3 + "failed JSON validation" stderr.
  (4) idempotent emit            — same (tripwire, run_id) within 60s
                                   returns identical id; one row.
  (5) list_open JSONL            — rows with `evidence` parsed to dicts.
  (6) acknowledge transition     — open→acknowledged; only targets
                                   status='open' rows.
  (7) resolve transition         — acknowledged→resolved and open→resolved;
                                   writes resolution_ts + resolution_note.
  (8) table-missing no-op        — drop safety_events table: emit/ack/
                                   resolve/list_open return rc=0 and warn.

DB fixture: every test runs ``mini_ork.stores.migrate.init_db`` (the
Python port of db/init.sh) against a tmp dir so the safety_events table +
both triggers (no_immutable_update, no_delete) are present before any
test fixture writes.

Field shape conventions:
  id        — 32 lowercase hex chars (secrets.token_hex(16)); tests
              assert SHAPE not VALUE because each emit generates a fresh id.
  ts        — UTC epoch seconds at INSERT time.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.stores import safety_events as se  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """Run ``init_db`` against a fresh tmp DB; yield the DB path.

    The migration runner applies all lexicographically-ordered migrations
    including 0036_safety_events.sql, which creates the safety_events
    table AND both triggers (no_immutable_update, no_delete). Tests that
    need a missing-table branch drop the table in-place after this
    fixture resolves.
    """
    dbp = str(tmp_path / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    assert _has_table(dbp, "safety_events"), (
        "safety_events table missing after init_db"
    )
    return dbp


def _has_table(dbp: str, name: str) -> bool:
    con = sqlite3.connect(dbp)
    try:
        cur = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return cur.fetchone() is not None
    finally:
        con.close()


def _row(dbp: str, event_id: str) -> dict:
    """Read a single row by id."""
    con = sqlite3.connect(dbp)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT id, tripwire_id, severity, run_id, recipe, evidence_json, "
            "status, operator_response, resolution_ts, resolution_note "
            "FROM safety_events WHERE id=?",
            (event_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _row_by_tripwire(dbp: str, tripwire_id: str) -> dict:
    """Read the single row for a tripwire_id (used when id isn't known)."""
    con = sqlite3.connect(dbp)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT id, tripwire_id, severity, run_id, recipe, evidence_json, "
            "status, operator_response, resolution_ts, resolution_note "
            "FROM safety_events WHERE tripwire_id=? ORDER BY ts DESC LIMIT 1",
            (tripwire_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _assert_id_shape(s: str) -> None:
    """id must be 32 lowercase hex chars (secrets.token_hex(16))."""
    assert _HEX32.match(s), f"id {s!r} is not 32 lowercase hex chars"


# ─────────────────────────────────────────────────────────────────────────────
# (1) emit valid — 32-hex id and a matching DB row
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_valid(db):
    """``emit('TW-1','high','{"cost_usd":12.50}','run-x')`` returns 32-hex
    id, rc=0, and writes a row with matching
    tripwire_id/severity/run_id."""
    rp = se.emit("TW-1", "high", '{"cost_usd":12.50}', "run-x-py", db=db)
    assert rp["rc"] == 0, f"python rc={rp['rc']}"
    _assert_id_shape(rp["id"])
    rp_row = _row(db, rp["id"])
    assert rp_row["tripwire_id"] == "TW-1"
    assert rp_row["severity"] == "high"
    assert rp_row["run_id"] == "run-x-py"
    assert rp_row["status"] == "open"
    assert rp_row["evidence_json"] == '{"cost_usd":12.50}'


# ─────────────────────────────────────────────────────────────────────────────
# (2) invalid severity rejected — rc=2
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_severity_rejected(db, capsys):
    """Severity='catastrophic' is rejected with rc=2 + "invalid severity"
    stderr. No row is written."""
    rp = se.emit("TW-2", "catastrophic", "{}", db=db)
    assert rp["rc"] == 2
    py_err = capsys.readouterr().err
    assert "invalid severity" in py_err
    assert "catastrophic" in py_err
    assert rp["id"] == ""

    # No row exists for TW-2.
    assert _row_by_tripwire(db, "TW-2") == {}


# ─────────────────────────────────────────────────────────────────────────────
# (3) bad JSON rejected — rc=3
# ─────────────────────────────────────────────────────────────────────────────
def test_bad_json_rejected(db, capsys):
    """Non-JSON evidence returns rc=3 + "failed JSON validation" stderr."""
    rp = se.emit("TW-3", "low", "bad-json", db=db)
    assert rp["rc"] == 3
    py_err = capsys.readouterr().err
    assert "failed JSON validation" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (4) idempotent emit within 60s — same (tripwire, run_id) returns identical id
# ─────────────────────────────────────────────────────────────────────────────
def test_idempotent_emit_within_60s(db):
    """Two emits with the same (tripwire_id, run_id) within the 60s window
    return the SAME id (the first insert is dedup'd by the
    SELECT-then-INSERT logic). Tests run the two emits back-to-back to
    keep the gap under 5s wall-clock drift."""
    rp1 = se.emit("TW-4-py", "medium", '{"x":1}', "run-test-4-py", db=db)
    rp2 = se.emit("TW-4-py", "medium", '{"x":1}', "run-test-4-py", db=db)
    assert rp1["rc"] == 0 and rp2["rc"] == 0
    _assert_id_shape(rp1["id"])
    _assert_id_shape(rp2["id"])
    assert rp1["id"] == rp2["id"], (
        f"idempotency broken: {rp1['id']!r} != {rp2['id']!r}"
    )

    # Exactly ONE row for the (TW-4-py, run-test-4-py) pair.
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "SELECT COUNT(*) FROM safety_events WHERE tripwire_id=?", ("TW-4-py",)
        )
        assert cur.fetchone()[0] == 1
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (5) list_open — rows with `evidence` parsed to dicts
# ─────────────────────────────────────────────────────────────────────────────
def test_list_open_rows(db):
    """Seed two rows via emit, then call list_open. Each row carries the
    `evidence` key parsed into a dict (not the raw JSON string) — that's
    the high-leverage shape that list_open consumers depend on."""
    for tw, sev, ev in (
        ("TW-A-py", "high", '{"cost_usd":12.50}'),
        ("TW-B-py", "low", '{"flagged":true}'),
    ):
        rp = se.emit(tw, sev, ev, "run-listopen-py", db=db)
        assert rp["rc"] == 0

    py_rows = se.list_open(db=db)
    assert len(py_rows) == 2

    by_tw = {r["tripwire_id"]: r for r in py_rows}
    assert set(by_tw) == {"TW-A-py", "TW-B-py"}
    for row in py_rows:
        assert isinstance(row["evidence"], dict), (
            f"evidence not parsed: {row['evidence']!r}"
        )
        assert row["status"] == "open"
        assert row["run_id"] == "run-listopen-py"
    assert by_tw["TW-A-py"]["evidence"]["cost_usd"] == 12.50
    assert by_tw["TW-A-py"]["severity"] == "high"
    assert by_tw["TW-B-py"]["evidence"]["flagged"] is True
    assert by_tw["TW-B-py"]["severity"] == "low"


# ─────────────────────────────────────────────────────────────────────────────
# (6) acknowledge — transitions open → acknowledged
# ─────────────────────────────────────────────────────────────────────────────
def test_acknowledge_status_transition(db):
    """acknowledge(event_id, "investigating") transitions status open →
    acknowledged and writes operator_response. Already-acknowledged rows
    must NOT be re-acked."""
    rp_seed = se.emit("TW-ACK-py", "high", '{"k":1}', "run-ack-py", db=db)
    py_id = rp_seed["id"]
    _assert_id_shape(py_id)
    rp_ack = se.acknowledge(py_id, "investigating", db=db)
    assert rp_ack["rc"] == 0
    assert rp_ack["updated"] == 1

    py_row = _row(db, py_id)
    assert py_row["status"] == "acknowledged"
    assert py_row["operator_response"] == "investigating"

    # Re-ack: WHERE id=? AND status='open' — no rows match → updated=0 and
    # the row stays acknowledged.
    rp_ack2 = se.acknowledge(py_id, "again", db=db)
    assert rp_ack2["updated"] == 0
    assert _row(db, py_id)["status"] == "acknowledged"


# ─────────────────────────────────────────────────────────────────────────────
# (7) resolve — transitions acknowledged → resolved + writes resolution_ts/note
# ─────────────────────────────────────────────────────────────────────────────
def test_resolve_status_transition(db):
    """resolve(event_id, "cap raised") transitions acknowledged → resolved
    and writes resolution_ts (epoch int) + resolution_note. The resolve
    UPDATE also matches status='open', so we confirm that branch here too
    with a separate open-row fixture."""
    # ── ack-then-resolve path ──
    rp_seed = se.emit("TW-RES-py", "critical", '{"k":2}', "run-res-py", db=db)
    py_id = rp_seed["id"]
    se.acknowledge(py_id, "investigating", db=db)
    rp_res = se.resolve(py_id, "cap raised", db=db)
    assert rp_res["rc"] == 0
    assert rp_res["updated"] == 1

    py_row = _row(db, py_id)
    assert py_row["status"] == "resolved"
    assert py_row["resolution_note"] == "cap raised"
    assert isinstance(py_row["resolution_ts"], int)

    # ── open-direct-resolve path (resolve matches status IN ('open','acknowledged')) ──
    rp_seed2 = se.emit("TW-RES2-py", "high", '{"k":3}', "run-res2-py", db=db)
    py_id2 = rp_seed2["id"]
    rp_res2 = se.resolve(py_id2, "no ack needed", db=db)
    assert rp_res2["updated"] == 1

    assert _row(db, py_id2)["status"] == "resolved"


# ─────────────────────────────────────────────────────────────────────────────
# (8) table-missing no-op — rc=0 + warn stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_table_missing_no_op(db, capsys):
    """Drop the safety_events table. emit/ack/resolve/list_open all return
    rc=0 + warn stderr (no row written, no exception). Mirrors the
    table-missing branch in ``_mo_se_table_exists``."""
    # Drop the safety_events table.
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE safety_events")
        con.commit()
    finally:
        con.close()

    # ── emit no-op ──
    rp = se.emit("TW-MISS", "high", '{"k":1}', "run-miss", db=db)
    assert rp["rc"] == 0
    assert rp["id"] == ""
    py_err = capsys.readouterr().err
    assert "table absent" in py_err
    assert "no-op" in py_err

    # ── acknowledge no-op ──
    rp_ack = se.acknowledge("any-id", "response", db=db)
    assert rp_ack["rc"] == 0
    assert rp_ack["updated"] == 0
    py_err = capsys.readouterr().err
    assert "ack is a no-op" in py_err

    # ── resolve no-op ──
    rp_res = se.resolve("any-id", "note", db=db)
    assert rp_res["rc"] == 0
    assert rp_res["updated"] == 0
    py_err = capsys.readouterr().err
    assert "resolve is a no-op" in py_err

    # ── list_open no-op ──
    rp_lo = se.list_open(db=db)
    assert rp_lo == []
    py_err = capsys.readouterr().err
    assert "nothing to list" in py_err
