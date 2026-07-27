"""Unit tests for ``mini_ork.stores.pattern_store``.

Each test allocates a fresh sqlite DB initialised via the native
``mini_ork.stores.migrate.init_db`` so the full migrated schema (0011
``pattern_records`` 5-tuple CHECK + ``execution_traces``) is present, then
drives the Python store and asserts the resulting ``pattern_records`` rows.

Nine cases:
  (1) test_insert_new_pattern_round_trip            — insert path, pid shape
  (2) test_upsert_increments_frequency_and_merges   — merge semantics
  (3) test_query_min_frequency_and_output_type      — filter SQL
  (4) test_mine_from_traces_deterministic_ids       — window + hash + heuristic
  (5) test_invalid_output_type_rejected_by_schema   — 5-tuple CHECK rejects 'other'
  (6) test_non_dict_payload_rejected_no_db_change   — dict API contract
  (7) test_module_imports_clean                     — public-API surface
  (8) test_evidence_string_coerced_via_json         — str→list JSON coercion
  (9) test_on_new_register_module_global            — registry persistence

Case 5 is the schema-drift wart: the lib's private _pattern_ensure_table DDL
includes 'other' in the CHECK, but migration 0011 does NOT. With the migrated
schema applied, the 5-tuple CHECK wins, so output_type='BOGUS' (coerced to
'other') fails to insert — we assert the rejection (not the storage of 'other').
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DB_INIT_ROOT = str(REPO)


@pytest.fixture
def db(tmp_path):
    """Fresh migrated DB per test (pattern_records + execution_traces)."""
    dbp = str(tmp_path / "state.db")
    from mini_ork.stores.migrate import init_db
    rc, out, err = init_db(db=dbp, root=DB_INIT_ROOT)
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"
    con = sqlite3.connect(dbp)
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()
    assert "pattern_records" in tables
    assert "execution_traces" in tables
    return dbp


def _rows(db_path: str) -> list[dict]:
    """SELECT rowid AS rid, * FROM pattern_records, sorted by rid."""
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT rowid AS rid, * FROM pattern_records ORDER BY rid"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _store(payload, db_path):
    from mini_ork.stores import pattern_store as ps
    return ps.store(payload, db_path=db_path)


# ─────────────────────────────────────────────────────────────────────────────
# (1) Canonical insert path
# ─────────────────────────────────────────────────────────────────────────────
def test_insert_new_pattern_round_trip(db):
    payload = {
        "description": "test pattern T",
        "output_type": "adr",
        "evidence_trace_ids": ["tr-1"],
    }

    py_pid, py_is_new = _store(payload, db)

    assert py_pid.startswith("pat-") and len(py_pid) == 16
    assert py_is_new is True

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["pattern_id"] == py_pid
    assert row["description"] == "test pattern T"
    assert row["output_type"] == "adr"
    assert json.loads(row["evidence_trace_ids"]) == ["tr-1"]
    assert row["frequency"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# (2) Upsert merge semantics
# ─────────────────────────────────────────────────────────────────────────────
def test_upsert_increments_frequency_and_merges_evidence(db):
    pid = "pat-fixed-test"

    # First insert.
    _store({
        "pattern_id": pid,
        "description": "T",
        "output_type": "adr",
        "evidence_trace_ids": ["e1", "e2"],
    }, db)

    # Second insert — same pid, new evidence (with overlap on 'e2').
    _, is_new = _store({
        "pattern_id": pid,
        "description": "T",
        "output_type": "adr",
        "evidence_trace_ids": ["e2", "e3"],
    }, db)
    assert is_new is False

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["frequency"] == 2
    # merge with dedup + preserve order: ['e1','e2','e3']
    assert json.loads(rows[0]["evidence_trace_ids"]) == ["e1", "e2", "e3"]


# ─────────────────────────────────────────────────────────────────────────────
# (3) Query filters (min_frequency + output_type)
# ─────────────────────────────────────────────────────────────────────────────
def test_query_min_frequency_and_output_type(db):
    from mini_ork.stores import pattern_store as ps

    seed = [
        {"pattern_id": "p-adr",   "description": "A", "output_type": "adr",
         "evidence_trace_ids": []},
        {"pattern_id": "p-other", "description": "O", "output_type": "best_practice_rule",
         "evidence_trace_ids": []},
        {"pattern_id": "p-extra", "description": "E", "output_type": "adr",
         "evidence_trace_ids": []},
    ]
    for s in seed:
        _store(s, db)

    # Bump p-extra frequency to 3 via raw SQL (frequency defaults to 1; we
    # want a row with frequency=3 to test min_frequency=2 inclusion).
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE pattern_records SET frequency=3 WHERE pattern_id='p-extra'"
        )
        con.commit()
    finally:
        con.close()

    min2 = ps.query(min_frequency=2, db_path=db)
    assert [r["pattern_id"] for r in min2] == ["p-extra"]

    adr = ps.query(output_type="adr", db_path=db)
    # rows ordered by frequency DESC: p-extra (3) before p-adr (1)
    assert [r["pattern_id"] for r in adr] == ["p-extra", "p-adr"]


# ─────────────────────────────────────────────────────────────────────────────
# (4) mine_from_traces deterministic ids
# ─────────────────────────────────────────────────────────────────────────────
def test_mine_from_traces_deterministic_ids(db):
    from mini_ork.stores import pattern_store as ps

    # Seed execution_traces: 3 failures, 1 success.
    # Timestamp must be seeded RELATIVE to the current time — a hardcoded
    # absolute date silently ages out of the `window="7d"` filter and turns
    # this test red days later (relative-window time-bomb). 1 day ago is
    # always well inside the window; the deterministic pattern id hashes
    # task_class|status, not the timestamp, so this stays reproducible.
    import datetime as _dt
    now = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    trace_rows = [
        ("tr-f1", "code_fix", "failure", now),
        ("tr-f2", "code_fix", "failure", now),
        ("tr-f3", "code_fix", "failure", now),
        ("tr-s1", "code_fix", "success", now),
    ]
    con = sqlite3.connect(db)
    try:
        con.executemany(
            "INSERT INTO execution_traces (trace_id, task_class, status, created_at) "
            "VALUES (?,?,?,?)",
            trace_rows,
        )
        con.commit()
    finally:
        con.close()

    # Only (code_fix, failure) cluster meets min_cluster=2.
    assert ps.mine_from_traces(window="7d", min_cluster=2, db_path=db) == 1

    expected_pid = "pat-" + hashlib.sha256(b"code_fix|failure").hexdigest()[:12]

    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["pattern_id"] == expected_pid
    assert row["output_type"] == "verifier_addition"  # failure → verifier_addition
    assert row["frequency"] == 3
    # evidence list contains all 3 failure traces
    assert sorted(json.loads(row["evidence_trace_ids"])) == ["tr-f1", "tr-f2", "tr-f3"]


# ─────────────────────────────────────────────────────────────────────────────
# (5) Invalid output_type rejected by migration 0011 CHECK (5-tuple, no 'other')
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_output_type_rejected_by_schema(db):
    """'BOGUS' coerces to 'other'; migration 0011's 5-tuple CHECK rejects
    'other' — the store raises sqlite3.IntegrityError and no row lands."""
    with pytest.raises(sqlite3.IntegrityError):
        _store({
            "description": "bad type",
            "output_type": "BOGUS",
            "evidence_trace_ids": [],
        }, db)

    assert _rows(db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (6) Non-dict payload rejected, no DB change (dict API contract)
# ─────────────────────────────────────────────────────────────────────────────
def test_non_dict_payload_rejected_no_db_change(db):
    """The store takes an already-parsed DICT by API contract — a raw string
    payload (what the retired bash CLI accepted) is rejected before any
    INSERT, leaving the table empty."""
    from mini_ork.stores import pattern_store as ps
    with pytest.raises((AttributeError, TypeError, ValueError)):
        ps.store("not-json-at-all", db_path=db)
    assert _rows(db) == []


# ─────────────────────────────────────────────────────────────────────────────
# (7) Module import + public API surface smoke
# ─────────────────────────────────────────────────────────────────────────────
def test_module_imports_clean():
    """Confirms the public API surface."""
    from mini_ork.stores.pattern_store import (
        _ON_NEW_HOOKS,
        mine_from_traces,
        on_new_register,
        query,
        store,
    )
    assert callable(store)
    assert callable(query)
    assert callable(mine_from_traces)
    assert callable(on_new_register)

    # Registry starts empty (or as-empty as a previous test left it after
    # clearing — we clear it explicitly here for determinism).
    _ON_NEW_HOOKS.clear()
    on_new_register(lambda *_: None)
    on_new_register(lambda *_: None)
    assert len(_ON_NEW_HOOKS) == 2
    _ON_NEW_HOOKS.clear()


# ─────────────────────────────────────────────────────────────────────────────
# (8) Evidence string→list JSON coercion
# ─────────────────────────────────────────────────────────────────────────────
def test_evidence_string_coerced_via_json(db):
    """A JSON-STRING evidence_trace_ids is coerced via json.loads → list."""
    py_pid, is_new = _store({
        "description": "str-evidence",
        "output_type": "adr",
        "evidence_trace_ids": '["x","y","z"]',  # JSON STRING, not list
    }, db)
    assert py_pid.startswith("pat-")
    assert is_new is True

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["pattern_id"] == py_pid
    assert json.loads(rows[0]["evidence_trace_ids"]) == ["x", "y", "z"]


# ─────────────────────────────────────────────────────────────────────────────
# (9) on_new_register persists across calls (module-global registry)
# ─────────────────────────────────────────────────────────────────────────────
def test_on_new_register_module_global(db):
    """Verifies the module-global _ON_NEW_HOOKS registry persists across
    store() calls. The hooks receive (pid, payload); errors must be
    swallowed."""
    from mini_ork.stores import pattern_store as ps

    ps._ON_NEW_HOOKS.clear()
    fired: list = []

    def hook_a(pid: str, payload: dict) -> None:
        fired.append(("a", pid, payload["description"]))

    def hook_b(*_args) -> None:
        # Raise to confirm error swallowing.
        raise RuntimeError("boom")

    ps.on_new_register(hook_a)
    ps.on_new_register(hook_b)

    pid_a, is_new_a = ps.store(
        {"description": "first", "output_type": "adr", "evidence_trace_ids": []},
        db_path=db,
    )
    pid_b, is_new_b = ps.store(
        {"description": "second", "output_type": "adr", "evidence_trace_ids": []},
        db_path=db,
    )
    assert is_new_a is True and is_new_b is True
    assert pid_a != pid_b

    # hook_a fires on each is_new; hook_b raises but doesn't break the store.
    assert fired == [
        ("a", pid_a, "first"),
        ("a", pid_b, "second"),
    ]

    ps._ON_NEW_HOOKS.clear()
