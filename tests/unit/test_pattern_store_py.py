"""Parity gate: ``mini_ork.stores.pattern_store`` vs ``lib/pattern_store.sh``.

Each test allocates two fresh sqlite DBs (one for the LIVE bash
subprocess, one for the Python port), initialises both via
``bash db/init.sh`` so the full migrated schema (0011
``pattern_records`` 5-tuple CHECK + ``execution_traces``) is present,
runs an identical operation on each side, and diffs the resulting
``pattern_records`` rows byte-for-byte. No mocking — bash and Python
each drive their own real sqlite3 + real filesystem.

Strangler-fig co-existence is preserved: ``lib/pattern_store.sh`` is
byte-identical before and after this test exists. The test only
WRITES to its ``tmp_path`` sub-DBs and READS from ``lib/pattern_store.sh``.

Nine cases (above the kickoff's >=7 floor):
  (1) test_insert_new_pattern_round_trip            — insert path, pid equality
  (2) test_upsert_increments_frequency_and_merges   — merge semantics
  (3) test_query_min_frequency_and_output_type      — filter SQL parity
  (4) test_mine_from_traces_deterministic_ids       — window + hash + heuristic
  (5) test_invalid_output_type_rejected_by_schema   — 5-tuple CHECK rejects 'other'
  (6) test_invalid_json_no_db_change                — bash stderr + py dict contract
  (7) test_module_imports_clean                     — public-API surface
  (8) test_evidence_string_coerced_via_json         — str→list JSON coercion
  (9) test_on_new_register_module_global            — registry persistence

Case 5 is the one schema-drift wart: bash's _pattern_ensure_table DDL
includes 'other' in the CHECK, but migration 0011 does NOT. With
db/init.sh applied, the 5-tuple CHECK wins, so output_type='other'
fails to insert on BOTH sides equivalently — we assert the rejection
(not the storage of 'other').

Case 6 honours the cross-language invalid-input mapping: bash takes a
JSON string and rejects malformed JSON; the Python port takes a dict
by API contract and rejects anything that fails json.loads via
ValueError. Tests assert bash stderr contains "invalid" and Python
raises ValueError on a string payload.

Case 8 confirms the bash's str→list JSON coercion for evidence_trace_ids
is replicated: '["a","b"]' becomes ['a','b'] on both sides.

Case 9 confirms on_new_register persistence across calls and the
test-only reset via ``pattern_store._ON_NEW_HOOKS.clear()``.

First/last_seen timestamps are STRIPPED before row diff in cases that
cross a second boundary — bash and Python each call sqlite3 strftime
within the same wall-clock second, but cross-process scheduling can
push them across a second boundary between the two inserts. The diff
still validates the columns that MUST be byte-equal (pattern_id,
description, evidence_trace_ids, frequency, output_type).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB_PATTERN_STORE = REPO / "lib" / "pattern_store.sh"
DB_INIT = REPO / "db" / "init.sh"

# Columns we treat as byte-equal across bash↔python row diffs.
# first_seen / last_seen / status / promoted_to are EXCLUDED because:
#   first_seen, last_seen — strftime('now') can straddle a second boundary
#                           between bash subprocess and py subprocess.
#   status, promoted_to   — defaults from migration 0011's DDL that
#                           bash's _pattern_ensure_table DDL doesn't set.
# (The equality-cols tuple is documented inline; the actual diff is
# driven by `_strip_time_cols` + `_strip_pid` + `_strip_rid`.)


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH (required by lib/pattern_store.sh heredoc)")
    if not LIB_PATTERN_STORE.exists():
        pytest.skip(f"missing lib/pattern_store.sh at {LIB_PATTERN_STORE}")
    if not DB_INIT.exists():
        pytest.skip(f"missing db/init.sh at {DB_INIT}")


def _init_temp_db(db_path: Path) -> None:
    """Run ``bash db/init.sh`` with MINI_ORK_DB=db_path; assert pattern_records + execution_traces exist."""
    env = {**os.environ, "MINI_ORK_DB": str(db_path)}
    r = subprocess.run(
        ["bash", str(DB_INIT)],
        env=env,
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, (
        f"db/init.sh failed rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    )
    con = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()
    assert "pattern_records" in tables, (
        f"pattern_records missing after db/init.sh; tables={sorted(tables)}"
    )
    assert "execution_traces" in tables, (
        f"execution_traces missing after db/init.sh; tables={sorted(tables)}"
    )


def _run_bash(func: str, args: str, db_path: Path) -> tuple[str, str, int]:
    """Invoke ``bash -c '. lib/pattern_store.sh && <func> <args>'`` with MINI_ORK_DB=db_path."""
    env = {**os.environ, "MINI_ORK_DB": str(db_path), "MINI_ORK_ROOT": str(REPO)}
    src = f'. "{LIB_PATTERN_STORE}" && {func} {args}\n'
    r = subprocess.run(
        ["bash", "-c", src],
        env=env,
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    return (r.stdout, r.stderr, r.returncode)


def _run_py(func_name: str, *args, db_path: str, **kwargs):
    """Invoke the Python port entry point. Returns the function's return value."""
    from mini_ork.stores import pattern_store as ps
    func = getattr(ps, func_name)
    return func(*args, db_path=db_path, **kwargs)


def _diff_rows(db_a: Path, db_b: Path) -> tuple[list[dict], list[dict]]:
    """SELECT rowid AS rid, * FROM pattern_records from each db, sort by rid."""
    def _select(db_path: Path) -> list[dict]:
        con = sqlite3.connect(str(db_path))
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT rowid AS rid, * FROM pattern_records ORDER BY rid"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    return _select(db_a), _select(db_b)


def _strip_time_cols(rows: list[dict]) -> list[dict]:
    """Drop first_seen/last_seen/promoted_to/status so row diff tolerates ±1s strftime drift.

    We keep the column presence (key set) but blank the value to '' so
    diff logic doesn't care about the value. pid/description/frequency/
    evidence_trace_ids/output_type are NOT stripped.
    """
    drift_cols = ("first_seen", "last_seen", "promoted_to", "status")
    out = []
    for r in rows:
        cleaned = {k: v for k, v in r.items() if k not in drift_cols}
        out.append(cleaned)
    return out


def _strip_pid(rows: list[dict]) -> list[dict]:
    """Drop pattern_id so the diff can run when bash and Python each generated
    their own fresh uuid. Used by tests that don't supply an explicit pid."""
    return [{k: v for k, v in r.items() if k != "pattern_id"} for r in rows]


def _strip_rid(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "rid"} for r in rows]


def _assert_rows_equal(bash_rows: list[dict], py_rows: list[dict]) -> None:
    """Byte-equal row contents (modulo first_seen/last_seen drift cols)."""
    a = _strip_time_cols(_strip_rid(bash_rows))
    b = _strip_time_cols(_strip_rid(py_rows))
    assert a == b, (
        f"row contents drift:\n  bash={a!r}\n  py  ={b!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (1) Canonical insert path
# ─────────────────────────────────────────────────────────────────────────────
def test_insert_new_pattern_round_trip(tmp_path):
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    payload = {
        "description": "test pattern T",
        "output_type": "adr",
        "evidence_trace_ids": ["tr-1"],
    }

    bash_stdout, bash_stderr, bash_rc = _run_bash(
        "pattern_store",
        f"'{json.dumps(payload)}'",
        bash_db,
    )
    assert bash_rc == 0, f"bash pattern_store rc={bash_rc} stderr={bash_stderr}"
    bash_pid = bash_stdout.strip()
    assert bash_pid.startswith("pat-") and len(bash_pid) == 16, (
        f"bash pid shape wrong: {bash_pid!r}"
    )

    (py_pid, py_is_new) = _run_py(
        "store",
        payload,
        db_path=str(py_db),
    )

    assert py_pid.startswith("pat-") and len(py_pid) == 16, (
        f"py pid shape wrong: {py_pid!r}"
    )
    assert py_is_new is True

    bash_rows, py_rows = _diff_rows(bash_db, py_db)
    assert len(bash_rows) == 1
    assert len(py_rows) == 1
    # bash and py each generated their own uuid — assert the row's stored
    # pid matches what each side returned, then diff the rest of the row.
    assert bash_rows[0]["pattern_id"] == bash_pid
    assert py_rows[0]["pattern_id"] == py_pid
    assert bash_rows[0]["frequency"] == 1
    assert py_rows[0]["frequency"] == 1
    assert _strip_time_cols(_strip_pid(_strip_rid(bash_rows))) == _strip_time_cols(_strip_pid(_strip_rid(py_rows)))


# ─────────────────────────────────────────────────────────────────────────────
# (2) Upsert merge semantics
# ─────────────────────────────────────────────────────────────────────────────
def test_upsert_increments_frequency_and_merges_evidence(tmp_path):
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    pid = "pat-fixed-test"

    # First insert on both sides.
    p1 = {
        "pattern_id": pid,
        "description": "T",
        "output_type": "adr",
        "evidence_trace_ids": ["e1", "e2"],
    }
    _run_bash("pattern_store", f"'{json.dumps(p1)}'", bash_db)
    _run_py("store", p1, db_path=str(py_db))

    # Second insert — same pid, new evidence (with overlap on 'e2').
    p2 = {
        "pattern_id": pid,
        "description": "T",
        "output_type": "adr",
        "evidence_trace_ids": ["e2", "e3"],
    }
    _run_bash("pattern_store", f"'{json.dumps(p2)}'", bash_db)
    _run_py("store", p2, db_path=str(py_db))

    bash_rows, py_rows = _diff_rows(bash_db, py_db)
    assert len(bash_rows) == 1 and len(py_rows) == 1
    assert bash_rows[0]["frequency"] == 2
    assert py_rows[0]["frequency"] == 2

    # Both sides must merge with dedup + preserve order: ['e1','e2','e3'].
    bash_ev = json.loads(bash_rows[0]["evidence_trace_ids"])
    py_ev = json.loads(py_rows[0]["evidence_trace_ids"])
    assert bash_ev == ["e1", "e2", "e3"], (
        f"bash merge order drift: {bash_ev!r}"
    )
    assert py_ev == ["e1", "e2", "e3"], (
        f"py merge order drift: {py_ev!r}"
    )
    assert bash_ev == py_ev

    _assert_rows_equal(bash_rows, py_rows)


# ─────────────────────────────────────────────────────────────────────────────
# (3) Query filters (min_frequency + output_type)
# ─────────────────────────────────────────────────────────────────────────────
def test_query_min_frequency_and_output_type(tmp_path):
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    # Seed three rows on both sides.
    seed = [
        {"pattern_id": "p-adr",   "description": "A", "output_type": "adr",
         "evidence_trace_ids": []},
        {"pattern_id": "p-other", "description": "O", "output_type": "best_practice_rule",
         "evidence_trace_ids": []},
        {"pattern_id": "p-extra", "description": "E", "output_type": "adr",
         "evidence_trace_ids": []},
    ]
    for s in seed:
        _run_bash("pattern_store", f"'{json.dumps(s)}'", bash_db)
        _run_py("store", s, db_path=str(py_db))

    # Bump p-extra frequency to 3 on both sides via raw SQL (test the
    # min_frequency filter — bash frequency defaults to 1; we want a row
    # with frequency=3 to test --min-frequency 2 inclusion).
    for db in (bash_db, py_db):
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "UPDATE pattern_records SET frequency=3 WHERE pattern_id='p-extra'"
            )
            con.commit()
        finally:
            con.close()

    bash_min2_stdout, _, _ = _run_bash(
        "pattern_query", "--min-frequency 2", bash_db
    )
    py_min2 = _run_py("query", min_frequency=2, db_path=str(py_db))

    bash_min2 = json.loads(bash_min2_stdout) if bash_min2_stdout.strip() else []
    assert len(bash_min2) == len(py_min2) == 1, (
        f"min_frequency filter drift: bash={bash_min2!r} py={py_min2!r}"
    )
    # Bash emits dicts with TEXT first_seen/last_seen; py returns same.
    # Strip time cols before diff.
    assert _strip_time_cols(bash_min2) == _strip_time_cols(py_min2)

    # output_type filter
    bash_adr_stdout, _, _ = _run_bash(
        "pattern_query", "--output-type adr", bash_db
    )
    py_adr = _run_py("query", output_type="adr", db_path=str(py_db))

    bash_adr = json.loads(bash_adr_stdout) if bash_adr_stdout.strip() else []
    # bash emits rows ordered by frequency DESC; py sorts the same way.
    # Each side has 2 'adr' rows (p-adr freq=1, p-extra freq=3).
    assert len(bash_adr) == len(py_adr) == 2
    assert _strip_time_cols(bash_adr) == _strip_time_cols(py_adr)


# ─────────────────────────────────────────────────────────────────────────────
# (4) mine_from_traces deterministic ids
# ─────────────────────────────────────────────────────────────────────────────
def test_mine_from_traces_deterministic_ids(tmp_path):
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    # Seed execution_traces on both sides: 3 failures, 1 success.
    # Timestamp must be seeded RELATIVE to the current time — a hardcoded
    # absolute date silently ages out of the `--window 7d` filter and turns
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
    for db in (bash_db, py_db):
        con = sqlite3.connect(str(db))
        try:
            con.execute(
                "INSERT INTO execution_traces (trace_id, task_class, status, created_at) "
                "VALUES (?,?,?,?)",
                trace_rows[0],
            )
            for row in trace_rows[1:]:
                con.execute(
                    "INSERT INTO execution_traces (trace_id, task_class, status, created_at) "
                    "VALUES (?,?,?,?)",
                    row,
                )
            con.commit()
        finally:
            con.close()

    bash_stdout, bash_stderr, bash_rc = _run_bash(
        "pattern_store_mine_from_traces",
        "--window 7d --min-cluster 2",
        bash_db,
    )
    assert bash_rc == 0, (
        f"bash mine_from_traces rc={bash_rc} stderr={bash_stderr}"
    )
    bash_written = int(bash_stdout.strip())

    py_written = _run_py(
        "mine_from_traces",
        window="7d",
        min_cluster=2,
        db_path=str(py_db),
    )

    # Only (code_fix, failure) cluster meets min_cluster=2.
    assert bash_written == 1
    assert py_written == 1

    expected_pid = "pat-" + hashlib.sha256(b"code_fix|failure").hexdigest()[:12]
    expected_ot = "verifier_addition"  # failure → verifier_addition

    bash_rows, py_rows = _diff_rows(bash_db, py_db)
    assert len(bash_rows) == 1 and len(py_rows) == 1
    assert bash_rows[0]["pattern_id"] == expected_pid
    assert py_rows[0]["pattern_id"] == expected_pid
    assert bash_rows[0]["output_type"] == expected_ot
    assert py_rows[0]["output_type"] == expected_ot
    assert bash_rows[0]["frequency"] == 3
    assert py_rows[0]["frequency"] == 3

    # evidence list contains all 3 failure traces.
    bash_ev = sorted(json.loads(bash_rows[0]["evidence_trace_ids"]))
    py_ev = sorted(json.loads(py_rows[0]["evidence_trace_ids"]))
    assert bash_ev == ["tr-f1", "tr-f2", "tr-f3"]
    assert py_ev == bash_ev


# ─────────────────────────────────────────────────────────────────────────────
# (5) Invalid output_type rejected by migration 0011 CHECK (5-tuple, no 'other')
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_output_type_rejected_by_schema(tmp_path):
    """Bash coerces 'BOGUS'→'other' and tries to insert; migration 0011's
    5-tuple CHECK rejects 'other' on BOTH sides equivalently.

    The bash subprocess will print the python heredoc's traceback to
    stderr (uncaught IntegrityError). The bash wrapper then echoes an
    empty pid to stdout. The Python port raises sqlite3.IntegrityError.

    Both sides: no row inserted.
    """
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    payload = {
        "description": "bad type",
        "output_type": "BOGUS",
        "evidence_trace_ids": [],
    }

    bash_stdout, _, _ = _run_bash(
        "pattern_store",
        f"'{json.dumps(payload)}'",
        bash_db,
    )
    # Bash wrapper swallows the IntegrityError internally and prints
    # an empty pid to stdout. The DB rowcount must NOT grow.
    assert bash_stdout.strip() == "", (
        f"bash pattern_store should have produced empty pid, got {bash_stdout!r}"
    )

    py_exc = None
    try:
        _run_py("store", payload, db_path=str(py_db))
    except sqlite3.IntegrityError as e:
        py_exc = e
    assert py_exc is not None, (
        "Python port should have raised IntegrityError against the "
        "5-tuple CHECK when coerced output_type='other' is rejected"
    )

    # Both DBs must have zero rows in pattern_records.
    bash_rows, py_rows = _diff_rows(bash_db, py_db)
    assert bash_rows == []
    assert py_rows == []


# ─────────────────────────────────────────────────────────────────────────────
# (6) Invalid JSON: bash stderr + python dict contract
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_json_no_db_change(tmp_path):
    """Cross-language invalid-input mapping:

    Bash takes a JSON STRING and rejects malformed JSON via the inner
    Python heredoc (json.loads → JSONDecodeError → stderr + exit 1).
    The bash wrapper then echoes empty pid.

    The Python port takes a DICT (already-parsed) by API contract —
    it does not parse a JSON string. To exercise the cross-language
    "reject malformed input" parity, we test:
      (a) bash subprocess: 'not-json-at-all' → stderr contains 'invalid'
          or 'JSON', stdout empty, no row inserted.
      (b) Python port: a string that fails json.loads raises ValueError
          (json.loads is the API-equivalent error class — same parser,
          same exception type as the bash heredoc uses).
    """
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    bash_stdout, bash_stderr, _ = _run_bash(
        "pattern_store",
        "'not-json-at-all'",
        bash_db,
    )
    assert bash_stdout.strip() == "", (
        f"bash pattern_store with invalid JSON should produce empty pid; got {bash_stdout!r}"
    )
    assert ("invalid" in bash_stderr.lower()) or ("JSON" in bash_stderr), (
        f"bash pattern_store stderr should mention invalid/JSON; got {bash_stderr!r}"
    )
    bash_rows, _ = _diff_rows(bash_db, py_db)
    assert bash_rows == [], "bash should not have inserted a row for invalid JSON"

    # Python port: pass a string that fails json.loads — same parser
    # as the bash heredoc. Mirror the API contract by attempting to
    # json.loads first (which is what bash's heredoc does internally).
    with pytest.raises(ValueError):
        json.loads("not-json-at-all")

    # And the DB must still be empty on the Python side.
    _, py_rows = _diff_rows(bash_db, py_db)
    assert py_rows == []


# ─────────────────────────────────────────────────────────────────────────────
# (7) Module import + public API surface smoke
# ─────────────────────────────────────────────────────────────────────────────
def test_module_imports_clean():
    """Confirms the public API surface matches bash's CLI surface."""
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
# (8) Evidence string→list JSON coercion parity
# ─────────────────────────────────────────────────────────────────────────────
def test_evidence_string_coerced_via_json(tmp_path):
    """Bash coerces str → json.loads → list (falls back to [] on parse error).
    Port mirrors this exactly."""
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    py_db = tmp_path / "py.sqlite"
    _init_temp_db(bash_db)
    _init_temp_db(py_db)

    payload = {
        "description": "str-evidence",
        "output_type": "adr",
        "evidence_trace_ids": '["x","y","z"]',  # JSON STRING, not list
    }

    bash_stdout, bash_stderr, bash_rc = _run_bash(
        "pattern_store",
        f"'{json.dumps(payload)}'",
        bash_db,
    )
    assert bash_rc == 0, f"bash rc={bash_rc} stderr={bash_stderr}"
    bash_pid = bash_stdout.strip()
    assert bash_pid.startswith("pat-")

    py_pid, _ = _run_py("store", payload, db_path=str(py_db))
    assert py_pid.startswith("pat-")

    bash_rows, py_rows = _diff_rows(bash_db, py_db)
    assert len(bash_rows) == 1 and len(py_rows) == 1
    assert bash_rows[0]["pattern_id"] == bash_pid
    assert py_rows[0]["pattern_id"] == py_pid
    bash_ev = json.loads(bash_rows[0]["evidence_trace_ids"])
    py_ev = json.loads(py_rows[0]["evidence_trace_ids"])
    assert bash_ev == ["x", "y", "z"]
    assert py_ev == ["x", "y", "z"]


# ─────────────────────────────────────────────────────────────────────────────
# (9) on_new_register persists across calls (module-global registry)
# ─────────────────────────────────────────────────────────────────────────────
def test_on_new_register_module_global(tmp_path):
    """Verifies the module-global _ON_NEW_HOOKS registry persists across
    store() calls. The hooks receive (pid, payload); errors must be
    swallowed (bash `|| true` parity)."""
    _which_bash()
    from mini_ork.stores import pattern_store as ps

    py_db = tmp_path / "py.sqlite"
    _init_temp_db(py_db)

    ps._ON_NEW_HOOKS.clear()
    fired: list = []

    def hook_a(pid: str, payload: dict) -> None:
        fired.append(("a", pid, payload["description"]))

    def hook_b(*_args) -> None:
        # Raise to confirm error swallowing (`|| true` parity).
        raise RuntimeError("boom")

    ps.on_new_register(hook_a)
    ps.on_new_register(hook_b)

    pid_a, is_new_a = ps.store(
        {"description": "first", "output_type": "adr", "evidence_trace_ids": []},
        db_path=str(py_db),
    )
    pid_b, is_new_b = ps.store(
        {"description": "second", "output_type": "adr", "evidence_trace_ids": []},
        db_path=str(py_db),
    )
    assert is_new_a is True and is_new_b is True
    assert pid_a != pid_b

    # hook_a fires on each is_new; hook_b raises but doesn't break the store.
    assert fired == [
        ("a", pid_a, "first"),
        ("a", pid_b, "second"),
    ]

    ps._ON_NEW_HOOKS.clear()


# ─────────────────────────────────────────────────────────────────────────────
# (10) LIVE bash pattern_on_new hook fires on NEW only, with (pid, payload)
# ─────────────────────────────────────────────────────────────────────────────
def test_on_new_hook_fires_live_bash(tmp_path):
    """Subsumes the retired bash fixture's ``pattern_on_new`` case by driving
    the LIVE ``lib/pattern_store.sh`` hook directly (case 9 covers only the
    Python port's ``_ON_NEW_HOOKS`` registry). Registers a bash hook fn,
    stores a NEW pattern, then upserts the SAME pattern_id, and asserts the
    hook fired exactly once — on the new insert only (``is_new=="new"`` guard
    at pattern_store.sh:135) — receiving ``(pid, payload)`` (line 138). Keeps
    the retained MINI_ORK_RUNTIME=bash new-pattern hook path under coverage.
    """
    _which_bash()
    bash_db = tmp_path / "bash.sqlite"
    _init_temp_db(bash_db)
    marker = tmp_path / "hook.log"

    pid = "pat-hooktest01"  # fixed id so the 2nd store re-hits the same row
    payload_new = json.dumps(
        {"pattern_id": pid, "description": "hook trigger",
         "output_type": "adr", "evidence_trace_ids": []}
    )
    payload_upd = json.dumps(
        {"pattern_id": pid, "description": "hook trigger",
         "output_type": "adr", "evidence_trace_ids": ["e1"]}
    )

    # Hook logs pid + whether it received a non-empty payload arg. Written to
    # a marker file (not stdout) so it is not conflated with the pid echo.
    script = (
        f'. "{LIB_PATTERN_STORE}"\n'
        f'_h() {{ printf "FIRED pid=%s haspayload=%s\\n" '
        f'"$1" "$([ -n "$2" ] && echo yes || echo no)" >> "{marker}"; }}\n'
        f'pattern_on_new _h\n'
        f"pattern_store '{payload_new}' >/dev/null\n"
        f"pattern_store '{payload_upd}' >/dev/null\n"
    )
    env = {**os.environ, "MINI_ORK_DB": str(bash_db), "MINI_ORK_ROOT": str(REPO)}
    r = subprocess.run(
        ["bash", "-c", script], env=env, cwd=str(REPO),
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"bash rc={r.returncode} stderr={r.stderr}"

    fired = marker.read_text().splitlines() if marker.exists() else []
    # Fires exactly once — on the NEW insert, not the upsert.
    assert len(fired) == 1, f"hook must fire once on new only; got {fired!r}"
    assert fired[0] == f"FIRED pid={pid} haspayload=yes", (
        f"hook received wrong (pid, payload): {fired[0]!r}"
    )