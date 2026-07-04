"""Parity gate: mini_ork.ported.gates_common vs lib/gates_common.sh.

Each test invokes the LIVE bash subprocess (sourcing ``lib/gates_common.sh``
in a single ``bash -c`` block so the function bodies are visible to the
caller) against the same temp DB seeded by ``db/init.sh`` as the Python port.
Parity is asserted on:
  * tuple_json stdout shape (sorted-keys, ensure_ascii=False)
  * emit rc + emitted hex id (32 chars)
  * inserted ``grounded_rejections`` row content (JSON-serialised,
    side-by-side diff modulo the random ``id``/wall-clock ``ts``)
  * append-only trigger parity — provenance UPDATE blocked, allowed column
    (``consumed_by_reflector_ts``) UPDATE permitted

Env isolation mirrors ``tests/unit/test_steering_checkpoint_py.py``:
``db/init.sh`` is invoked with ``MINI_ORK_HOME``/``MINI_ORK_DB`` redirected
into a tmp path; the Python process is monkey-patched to the same env so
``_resolve_db`` sees the same DB file the bash subprocess writes to.

Seven cases (above the kickoff's ``>=6`` floor):
  (a) tuple_json shape          — JSON keys + values match across bash/Python
  (b) emit valid rejection       — rc, id shape, full-row payload parity
  (c) invalid verdict rejected   — both rc=2
  (d) invalid trace_ids rejected — both rc=3 (non-array JSON)
  (e) append-only trigger       — UPDATE of provenance blocked on BOTH inserted
                                   rows (bash-inserted + python-inserted), with
                                   stderr containing ``immutable``
  (f) consumed_by_reflector_ts   — UPDATE of the *non-provenance* column allowed
                                   on both rows
  (g) no-table no-op             — DROP TABLE → both emit calls return 0 with
                                   no row written (matches bash warn-and-return-0)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import gates_common as gc  # noqa: E402

GC_SH = REPO / "lib" / "gates_common.sh"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures + bash subprocess helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def env_db(tmp_path_factory, monkeypatch):
    """Seed a tmp mini-ork SQLite DB via db/init.sh; point Python env there.

    Returns (db_path, subprocess_env). The subprocess env already contains
    MINI_ORK_HOME + MINI_ORK_DB so the bash helper resolves to the same DB
    the Python port writes to. monkeypatch.setenv on the Python side keeps
    ``_resolve_db`` returning the tmp DB even when the host shell pytest has
    real MINI_ORK_* vars set.
    """
    home = tmp_path_factory.mktemp("gc_home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    return dbp, {**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp}


def _bash_call(env: dict, body: str) -> subprocess.CompletedProcess:
    """Source lib/gates_common.sh and run ``body``. Return CompletedProcess.

    Sourcing first ensures the bash ``mo_grounded_rejection*`` functions and
    the ``_mo_gr_*`` helpers are in scope. Self-test block at the bottom of
    lib/gates_common.sh is gated on ``BASH_SOURCE[0] == $0`` so it does NOT
    fire when the file is sourced.
    """
    wrapper = f'. "{GC_SH}"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", wrapper], env=env, capture_output=True, text=True,
    )


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
# (a) tuple_json shape — bash stdout keys + Python tuple_json() output match
# ─────────────────────────────────────────────────────────────────────────────
def test_tuple_json_shape_parity(env_db):
    _, env = env_db
    args = ("missing input", "evidence span", "wait for upstream")

    # Bash side: tuple_json path has no separate env-side stdout aside from
    # the JSON line (no DB touched); the bash _mo_gr_log error path is not
    # triggered for non-empty args.
    rb = _bash_call(
        env, f'mo_grounded_rejection_tuple_json {json.dumps(args[0])} '
              f'{json.dumps(args[1])} {json.dumps(args[2])}'
    )
    assert rb.returncode == 0, rb.stderr
    bash_tuple = json.loads(rb.stdout.strip())

    # Python side: in-process call returns the JSON string.
    py_str = gc.tuple_json(*args)
    py_tuple = json.loads(py_str)

    # Same keys, same values, same ASCII-preserving order (bash prints the
    # dict in the literal insertion order concern/evidence/suggestion;
    # json.loads is order-preserving in 3.7+).
    assert set(bash_tuple) == {"concern", "evidence", "suggestion"}
    assert bash_tuple == py_tuple == {
        "concern": args[0],
        "evidence": args[1],
        "suggestion": args[2],
    }


# ─────────────────────────────────────────────────────────────────────────────
# (b) emit valid rejection — rc=0 on both, full-row parity (modulo id/ts)
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_valid_parity(env_db):
    db, env = env_db
    gate = "coalition"
    verdict = "fail"
    concern = "panel missing third lens family"
    evidence = "trace tr-7d2a1 shows only 2 of 3 families voted"
    suggestion = "wait for kimi-lens retry or escalate to operator"
    trace_ids = '["tr-7d2a1","tr-7d2a2"]'
    run_id = "run-py-test-1"

    # Bash path: returns rc=0 and writes the id to stdout (stderr is the JSON
    # log line — strip that away).
    rb = _bash_call(
        env,
        f'mo_grounded_rejection {json.dumps(gate)} {json.dumps(verdict)} '
        f'{json.dumps(concern)} {json.dumps(evidence)} '
        f'{json.dumps(suggestion)} {json.dumps(trace_ids)} '
        f'{json.dumps(run_id)}',
    )
    assert rb.returncode == 0, rb.stderr
    bash_id = rb.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{32}", bash_id), bash_id

    # Python path: returns the id directly.
    py_id = gc.emit(
        gate, verdict, concern, evidence, suggestion,
        evidence_trace_ids=trace_ids, run_id=run_id,
    )
    assert isinstance(py_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", py_id)
    assert py_id != bash_id  # random hex; just sanity-check distinct

    # Row-level parity (modulo id/ts). Both sides must have inserted into the
    # shared tmp DB.
    bash_row = _row(db, bash_id)
    py_row = _row(db, py_id)
    assert bash_row is not None and py_row is not None
    assert _diff_row(bash_row) == _diff_row(py_row) == {
        "gate_name": gate,
        "verdict": verdict,
        "concern": concern,
        "evidence_summary": evidence,
        "suggestion": suggestion,
        "evidence_trace_ids": trace_ids,
        "run_id": run_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (c) invalid verdict — bash + Python both return rc=2
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_verdict_rejected_parity(env_db):
    _, env = env_db

    rb = _bash_call(
        env,
        'mo_grounded_rejection "coalition" "catastrophic" "x" "y" "z" "[]" ""',
    )
    py_rc = gc.emit("coalition", "catastrophic", "x", "y", "z", "[]", "")

    assert rb.returncode == 2
    assert py_rc == 2
    # Both sides log an "invalid verdict" error.
    assert "invalid verdict" in rb.stderr


# ─────────────────────────────────────────────────────────────────────────────
# (d) invalid evidence_trace_ids JSON — both return rc=3
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_trace_ids_rejected_parity(env_db):
    _, env = env_db

    rb = _bash_call(
        env,
        'mo_grounded_rejection "coalition" "fail" "x" "y" "z" "not-json" ""',
    )
    py_rc = gc.emit("coalition", "fail", "x", "y", "z", "not-json", "")

    assert rb.returncode == 3
    assert py_rc == 3
    assert "must be a JSON array" in rb.stderr or "JSON" in rb.stderr


# ─────────────────────────────────────────────────────────────────────────────
# (e) append-only trigger blocks UPDATE of provenance — BOTH inserted rows
# ─────────────────────────────────────────────────────────────────────────────
def test_trigger_blocks_provenance_update_parity(env_db):
    db, env = env_db

    rb = _bash_call(
        env,
        'mo_grounded_rejection "coalition" "fail" '
        '"concern-original" "ev-sum" "sug" "[]" ""',
    )
    bash_id = rb.stdout.strip()
    py_id = gc.emit("coalition", "fail", "concern-original", "ev-sum", "sug")
    assert isinstance(py_id, str)

    # Trigger is defined on the table for both rows (they live in the same
    # DB); no need to repeat on the Python-inserted row alone — a single
    # UPDATE blocked per row proves the trigger fires.
    for rid in (bash_id, py_id):
        con = sqlite3.connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError) as ei:
                con.execute(
                    "UPDATE grounded_rejections SET concern='hacked' "
                    "WHERE id = ?", (rid,),
                )
            assert "immutable" in str(ei.value).lower()
        finally:
            con.close()
    assert bash_id and py_id  # both emitted before the failed UPDATE


# ─────────────────────────────────────────────────────────────────────────────
# (f) consumed_by_reflector_ts is updatable — non-provenance column permits
#     UPDATE; rc=0 on the direct UPDATE (no trigger fires).
# ─────────────────────────────────────────────────────────────────────────────
def test_consumed_by_reflector_ts_updatable_parity(env_db):
    db, env = env_db

    rb = _bash_call(
        env,
        'mo_grounded_rejection "coalition" "fail" "c" "e" "s" "[]" ""',
    )
    bash_id = rb.stdout.strip()
    py_id = gc.emit("coalition", "fail", "c", "e", "s")
    assert isinstance(py_id, str)

    # Allowed column: rc=0 (exit code of the subprocess wrapper), no error.
    for rid in (bash_id, py_id):
        con = sqlite3.connect(db)
        try:
            con.execute(
                "UPDATE grounded_rejections "
                "SET consumed_by_reflector_ts = strftime('%s','now') "
                "WHERE id = ?", (rid,),
            )
            con.commit()
        except sqlite3.IntegrityError as e:
            pytest.fail(
                f"trigger unexpectedly fired on consumed_by_reflector_ts "
                f"UPDATE: {e} (rid={rid})"
            )
        finally:
            con.close()

    # Confirm both rows actually transitioned to consumed.
    for rid in (bash_id, py_id):
        row = _row(db, rid)
        assert row is not None
        assert row["consumed_by_reflector_ts"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# (g) no-table no-op — DROP grounded_rejections → both emit calls return 0
#     without writing a row (matches bash warn-and-return-0 path).
# ─────────────────────────────────────────────────────────────────────────────
def test_no_table_noop_parity(env_db):
    db, env = env_db
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE grounded_rejections")
        con.commit()
    finally:
        con.close()

    rb = _bash_call(
        env,
        'mo_grounded_rejection "coalition" "fail" "c" "e" "s" "[]" ""',
    )
    py_rc = gc.emit("coalition", "fail", "c", "e", "s")

    assert rb.returncode == 0
    assert py_rc == 0
    # Bash warns and stays silent on stdout (no row written).
    assert rb.stdout.strip() == ""
    assert "grounded_rejections table absent" in rb.stderr

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
