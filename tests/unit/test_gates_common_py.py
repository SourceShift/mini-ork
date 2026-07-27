"""Standalone unit tests for ``mini_ork.gates.common``.

Replaces the bash-parity gate (against ``lib/gates_common.sh``) as part of
the bash→Python migration: the Python port is now the sole implementation,
so its coverage no longer runs ``lib/gates_common.sh`` in a subprocess —
it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (tuple_json shape, emit
rc + id shape + inserted row content, rejection rc codes, and the
append-only trigger), now asserted on the port's output.

Seven cases:
  (a) tuple_json shape          — JSON keys + values
  (b) emit valid rejection       — rc, id shape, full-row payload
  (c) invalid verdict rejected   — rc=2
  (d) invalid trace_ids rejected — rc=3 (non-array JSON)
  (e) append-only trigger        — UPDATE of provenance blocked with an
                                   ``immutable`` error
  (f) consumed_by_reflector_ts   — UPDATE of the *non-provenance* column
                                   permitted
  (g) no-table no-op             — DROP TABLE → emit returns 0 with no
                                   row written (warn-and-return-0 path)

Env isolation: the DB is seeded via the Python port of db/init.sh
(``mini_ork.stores.migrate.init_db``) with ``MINI_ORK_HOME``/``MINI_ORK_DB``
redirected into a tmp path; the Python process is monkey-patched to the
same env so ``_resolve_db`` sees the same DB file.
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
from mini_ork.gates import common as gc  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def env_db(tmp_path_factory, monkeypatch):
    """Seed a tmp mini-ork SQLite DB via init_db; point Python env there.

    Returns the db path. monkeypatch.setenv keeps ``_resolve_db``
    returning the tmp DB even when the host shell pytest has real
    MINI_ORK_* vars set.
    """
    home = tmp_path_factory.mktemp("gc_home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    return dbp


def _row(db: str, rid: str) -> dict | None:
    con = sqlite3.connect(db)
    try:
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT * FROM grounded_rejections WHERE id = ?", (rid,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _diff_row(d: dict) -> dict:
    """Drop the random id + wall-clock ts so two rows can be compared."""
    return {
        "gate_name": d["gate_name"],
        "verdict": d["verdict"],
        "concern": d["concern"],
        "evidence_summary": d["evidence_summary"],
        "suggestion": d["suggestion"],
        "evidence_trace_ids": d["evidence_trace_ids"],
        "run_id": d["run_id"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# (a) tuple_json shape
# ─────────────────────────────────────────────────────────────────────────────
def test_tuple_json_shape(env_db):
    args = ("missing input", "evidence span", "wait for upstream")

    py_str = gc.tuple_json(*args)
    py_tuple = json.loads(py_str)

    assert set(py_tuple) == {"concern", "evidence", "suggestion"}
    assert py_tuple == {
        "concern": args[0],
        "evidence": args[1],
        "suggestion": args[2],
    }


# ─────────────────────────────────────────────────────────────────────────────
# (b) emit valid rejection — rc=0, id shape, full-row payload
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_valid(env_db):
    db = env_db
    gate = "coalition"
    verdict = "fail"
    concern = "panel missing third lens family"
    evidence = "trace tr-7d2a1 shows only 2 of 3 families voted"
    suggestion = "wait for kimi-lens retry or escalate to operator"
    trace_ids = '["tr-7d2a1","tr-7d2a2"]'
    run_id = "run-py-test-1"

    py_id = gc.emit(
        gate, verdict, concern, evidence, suggestion,
        evidence_trace_ids=trace_ids, run_id=run_id,
    )
    assert isinstance(py_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", py_id)

    py_row = _row(db, py_id)
    assert py_row is not None
    assert _diff_row(py_row) == {
        "gate_name": gate,
        "verdict": verdict,
        "concern": concern,
        "evidence_summary": evidence,
        "suggestion": suggestion,
        "evidence_trace_ids": trace_ids,
        "run_id": run_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (c) invalid verdict — rc=2
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_verdict_rejected(env_db):
    py_rc = gc.emit("coalition", "catastrophic", "x", "y", "z", "[]", "")
    assert py_rc == 2


# ─────────────────────────────────────────────────────────────────────────────
# (d) invalid evidence_trace_ids JSON — rc=3
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_trace_ids_rejected(env_db):
    py_rc = gc.emit("coalition", "fail", "x", "y", "z", "not-json", "")
    assert py_rc == 3


# ─────────────────────────────────────────────────────────────────────────────
# (e) append-only trigger blocks UPDATE of provenance
# ─────────────────────────────────────────────────────────────────────────────
def test_trigger_blocks_provenance_update(env_db):
    db = env_db

    py_id = gc.emit("coalition", "fail", "concern-original", "ev-sum", "sug")
    assert isinstance(py_id, str)

    con = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError) as ei:
            con.execute(
                "UPDATE grounded_rejections SET concern='hacked' "
                "WHERE id = ?", (py_id,),
            )
        assert "immutable" in str(ei.value).lower()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (f) consumed_by_reflector_ts is updatable — non-provenance column permits
#     UPDATE; no trigger fires.
# ─────────────────────────────────────────────────────────────────────────────
def test_consumed_by_reflector_ts_updatable(env_db):
    db = env_db

    py_id = gc.emit("coalition", "fail", "c", "e", "s")
    assert isinstance(py_id, str)

    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE grounded_rejections "
            "SET consumed_by_reflector_ts = strftime('%s','now') "
            "WHERE id = ?", (py_id,),
        )
        con.commit()
    except sqlite3.IntegrityError as e:
        pytest.fail(
            f"trigger unexpectedly fired on consumed_by_reflector_ts "
            f"UPDATE: {e} (rid={py_id})"
        )
    finally:
        con.close()

    row = _row(db, py_id)
    assert row is not None
    assert row["consumed_by_reflector_ts"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# (g) no-table no-op — DROP grounded_rejections → emit returns 0 without
#     writing a row (warn-and-return-0 path).
# ─────────────────────────────────────────────────────────────────────────────
def test_no_table_noop(env_db):
    db = env_db
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE grounded_rejections")
        con.commit()
    finally:
        con.close()

    py_rc = gc.emit("coalition", "fail", "c", "e", "s")
    assert py_rc == 0

    # Confirm no row was written — table still doesn't exist.
    con = sqlite3.connect(db)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='grounded_rejections' LIMIT 1"
        ).fetchone()
        assert exists is None
    finally:
        con.close()
