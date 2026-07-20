"""Parity gate: mini_ork.stores.safety_events vs lib/safety_events.sh.

Each test invokes the LIVE ``bash lib/safety_events.sh`` subprocess
(after sourcing) on identical inputs as the Python port and asserts
matching rc + stdout (event id or JSONL rows) + DB row contents. No
mocks, no hardcoded outputs beyond what bash itself emits.

Eight cases (above the kickoff's >=6 floor):
  (1) emit valid                — bash + python each emit('TW-1','high',...);
                                   rc=0, 32-hex id, row present with
                                   matching tripwire/severity/run_id.
  (2) invalid severity rejected — bash exit 2 + "invalid severity" stderr
                                   matches python {"rc": 2}.
  (3) bad JSON rejected          — bash exit 3 + "failed JSON validation"
                                   stderr matches python {"rc": 3}.
  (4) idempotent emit            — same (tripwire, run_id) within 60s
                                   returns identical id on both sides.
  (5) list_open JSONL parity     — after seeding two rows, bash output
                                   and python output are JSONL with
                                   `evidence` parsed identically (ts stripped).
  (6) acknowledge transition     — both sides transition open→acknowledged
                                   and only target status='open' rows.
  (7) resolve transition         — both sides transition acknowledged→resolved
                                   and write resolution_ts + resolution_note.
  (8) table-missing no-op        — drop safety_events table, both sides
                                   return rc=0 on emit/ack/resolve and
                                   both emit a warn line to stderr.

DB fixture: every test runs ``bash db/init.sh`` against a tmp dir so
the safety_events table + both triggers (no_immutable_update,
no_delete) are present before any test fixture writes. bash and python
operate on the SAME migrated DB but each emits a fresh id per call —
bash and python never share an id; parity is asserted on row contents
(stripped of id/ts), not on id equality.

Field shape conventions:
  id        — 32 lowercase hex chars (bash: secrets.token_hex(16));
              tests assert SHAPE not VALUE because each emit generates
              a fresh id per side.
  ts        — UTC epoch seconds at INSERT time; both sides compute it
              within ±1s of each other so tests STRIP ts before diffing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import safety_events as se

SH = REPO / "lib" / "safety_events.sh"
INIT_SH = REPO / "db" / "init.sh"

# Strip keys that drift between bash and python (ts) before diffing
# list_open JSONL rows. id is asserted by SHAPE not equality (each side
# generates a fresh id per emit).
_STRIP_KEYS_FOR_ROW_DIFF = ("ts",)


def _which(*tools: str) -> dict[str, str]:
    out = {}
    for t in tools:
        p = shutil.which(t)
        if not p:
            pytest.skip(f"required tool not on PATH: {t}")
        out[t] = p
    return out


@pytest.fixture
def db(tmp_path):
    """Run ``bash db/init.sh`` against a fresh tmp DB; yield the DB path.

    The migration runner applies all lexicographically-ordered migrations
    including 0036_safety_events.sql, which creates the safety_events
    table AND both triggers (no_immutable_update, no_delete). Tests that
    need a missing-table branch drop the table in-place after this
    fixture resolves.
    """
    _which("bash", "sqlite3")
    dbp = str(tmp_path / "state.db")
    env = {
        **os.environ,
        "MINI_ORK_HOME": str(tmp_path),
        "MINI_ORK_DB": dbp,
        "MINI_ORK_ROOT": str(REPO),
    }
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, (
        f"db/init.sh failed rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    )
    assert _has_table(dbp, "safety_events"), (
        f"safety_events table missing after init.sh\nstdout={r.stdout}"
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
    """Read a single row by id (excludes triggers irrelevant to parity)."""
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


def _bash(fn: str, *args: str, dbp: str) -> subprocess.CompletedProcess:
    """Source ``lib/safety_events.sh`` and call ``fn <args...>``.

    Args:
        fn: bash function name (e.g. ``mo_safety_event_emit``).
        args: positional arguments to the function.
        dbp: path to the SQLite DB (passed as MINI_ORK_DB).

    Returns CompletedProcess with captured stdout/stderr/returncode.
    bash's printf-driven logging writes to stderr — tests assert on
    stdout for rc-shaped output and on stderr for log-line shape.

    Quote handling: args are wrapped in single quotes so JSON braces
    (``{"k":1}``) survive bash word-splitting intact. Double-quoting
    inside ``bash -c '...'`` would mangle them. POSIX-safe single-quote
    escape (``'\\''``) is applied for any embedded apostrophes — none of
    the parity test inputs contain apostrophes today.
    """
    def _sq(s: str) -> str:
        return "'" + s.replace("'", "'\\''") + "'"
    argv = ["bash", "-c",
            f'. "{SH}" && {fn} ' + " ".join(_sq(a) for a in args)]
    env = {**os.environ, "MINI_ORK_DB": dbp, "MINI_ORK_ROOT": str(REPO)}
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, check=False,
    )


_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _assert_id_shape(s: str) -> None:
    """id must be 32 lowercase hex chars (bash + python both use secrets.token_hex(16))."""
    assert _HEX32.match(s), f"id {s!r} is not 32 lowercase hex chars"


# ─────────────────────────────────────────────────────────────────────────────
# (1) emit valid — bash + python both emit a 32-hex id and a matching DB row
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_valid_parity(db):
    """``emit('TW-1','high','{"cost_usd":12.50}','run-x')`` returns 32-hex id, rc=0,
    and writes a row with matching tripwire_id/severity/run_id on both sides.
    """
    # Bash side (independent run_id so python side gets a separate row,
    # letting us compare row CONTENTS — both sides share the same DB).
    rb = _bash("mo_safety_event_emit", "TW-1", "high", '{"cost_usd":12.50}',
               "run-x-bash", dbp=db)
    assert rb.returncode == 0, f"bash rc={rb.returncode} stderr={rb.stderr!r}"
    bash_id = rb.stdout.strip()
    _assert_id_shape(bash_id)
    rb_row = _row(db, bash_id)
    assert rb_row["tripwire_id"] == "TW-1"
    assert rb_row["severity"] == "high"
    assert rb_row["run_id"] == "run-x-bash"
    assert rb_row["status"] == "open"
    assert rb_row["evidence_json"] == '{"cost_usd":12.50}'

    # Python side
    rp = se.emit("TW-1", "high", '{"cost_usd":12.50}', "run-x-py", db=db)
    assert rp["rc"] == 0, f"python rc={rp['rc']}"
    _assert_id_shape(rp["id"])
    rp_row = _row(db, rp["id"])
    assert rp_row["tripwire_id"] == "TW-1"
    assert rp_row["severity"] == "high"
    assert rp_row["run_id"] == "run-x-py"
    assert rp_row["status"] == "open"
    assert rp_row["evidence_json"] == '{"cost_usd":12.50}'

    # Row shape parity (excluding id, run_id — both are side-specific).
    for k in ("tripwire_id", "severity", "recipe", "status", "evidence_json"):
        assert rb_row[k] == rp_row[k], f"row mismatch on {k}: {rb_row[k]!r} vs {rp_row[k]!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (2) invalid severity rejected — rc=2 on both sides
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_severity_rejected_parity(db, capsys):
    """Severity='catastrophic' is rejected with rc=2 + "invalid severity" stderr on both sides.
    No row is written.
    """
    rb = _bash("mo_safety_event_emit", "TW-2", "catastrophic", "{}", dbp=db)
    assert rb.returncode == 2, f"bash rc={rb.returncode} (expected 2)"
    assert "invalid severity" in rb.stderr
    assert "catastrophic" in rb.stderr
    assert rb.stdout.strip() == ""

    rp = se.emit("TW-2", "catastrophic", "{}", db=db)
    assert rp["rc"] == 2
    py_err = capsys.readouterr().err
    assert "invalid severity" in py_err
    assert "catastrophic" in py_err
    assert rp["id"] == ""

    # No row exists for TW-2 on either side.
    assert _row_by_tripwire(db, "TW-2") == {}


# ─────────────────────────────────────────────────────────────────────────────
# (3) bad JSON rejected — rc=3 on both sides
# ─────────────────────────────────────────────────────────────────────────────
def test_bad_json_rejected_parity(db, capsys):
    """Non-JSON evidence returns rc=3 + "failed JSON validation" stderr on both sides.
    """
    rb = _bash("mo_safety_event_emit", "TW-3", "low", "bad-json", dbp=db)
    assert rb.returncode == 3, f"bash rc={rb.returncode} (expected 3)"
    assert "failed JSON validation" in rb.stderr

    rp = se.emit("TW-3", "low", "bad-json", db=db)
    assert rp["rc"] == 3
    py_err = capsys.readouterr().err
    assert "failed JSON validation" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (4) idempotent emit within 60s — same (tripwire, run_id) returns identical id
# ─────────────────────────────────────────────────────────────────────────────
def test_idempotent_emit_within_60s_parity(db):
    """Two emits with the same (tripwire_id, run_id) within the 60s window
    return the SAME id on each side (the first insert is dedup'd by the
    SELECT-then-INSERT logic). Tests run the two emits back-to-back to
    keep the gap under 5s wall-clock drift.
    """
    # Bash side: back-to-back emits with same (tripwire, run_id)
    rb1 = _bash("mo_safety_event_emit", "TW-4", "medium", '{"x":1}',
                 "run-test-4", dbp=db)
    rb2 = _bash("mo_safety_event_emit", "TW-4", "medium", '{"x":1}',
                 "run-test-4", dbp=db)
    assert rb1.returncode == 0 and rb2.returncode == 0
    bash_id1, bash_id2 = rb1.stdout.strip(), rb2.stdout.strip()
    _assert_id_shape(bash_id1)
    _assert_id_shape(bash_id2)
    assert bash_id1 == bash_id2, (
        f"bash idempotency broken: {bash_id1!r} != {bash_id2!r}"
    )

    # Python side: back-to-back emits with same (tripwire, run_id)
    rp1 = se.emit("TW-4-py", "medium", '{"x":1}', "run-test-4-py", db=db)
    rp2 = se.emit("TW-4-py", "medium", '{"x":1}', "run-test-4-py", db=db)
    assert rp1["rc"] == 0 and rp2["rc"] == 0
    _assert_id_shape(rp1["id"])
    _assert_id_shape(rp2["id"])
    assert rp1["id"] == rp2["id"], (
        f"python idempotency broken: {rp1['id']!r} != {rp2['id']!r}"
    )

    # Each side has exactly ONE row for its (TW-4*, run-test-4*) pair.
    for tw in ("TW-4", "TW-4-py"):
        con = sqlite3.connect(db)
        try:
            cur = con.execute(
                "SELECT COUNT(*) FROM safety_events WHERE tripwire_id=?", (tw,)
            )
            assert cur.fetchone()[0] == 1, f"expected 1 row for {tw}"
        finally:
            con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (5) list_open JSONL parity — bash and python emit equivalent JSONL rows
# ─────────────────────────────────────────────────────────────────────────────
def test_list_open_jsonl_parity(db):
    """Seed two rows on each side via emit, then call list_open. Both sides
    emit one JSON object per line (JSONL) with the `evidence` key parsed
    identically. Strip ts before diffing (both sides compute it within
    ±1s wall-clock drift).
    """
    # Seed two rows (one per side via DIFFERENT tripwire ids so both
    # emit fresh inserts without idempotency dedup interference).
    for tw, sev, ev in (
        ("TW-A-bash", "high", '{"cost_usd":12.50}'),
        ("TW-B-bash", "low", '{"flagged":true}'),
    ):
        rb = _bash("mo_safety_event_emit", tw, sev, ev, "run-listopen-bash",
                   dbp=db)
        assert rb.returncode == 0
    for tw, sev, ev in (
        ("TW-A-py", "high", '{"cost_usd":12.50}'),
        ("TW-B-py", "low", '{"flagged":true}'),
    ):
        rp = se.emit(tw, sev, ev, "run-listopen-py", db=db)
        assert rp["rc"] == 0

    # Bash list_open (subprocess invokes mo_safety_event_list_open via source)
    rb = _bash("mo_safety_event_list_open", dbp=db)
    assert rb.returncode == 0
    bash_rows = []
    for line in rb.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for k in _STRIP_KEYS_FOR_ROW_DIFF:
            row.pop(k, None)
        bash_rows.append(row)

    # Python list_open
    py_rows_raw = se.list_open(db=db)
    py_rows = []
    for row in py_rows_raw:
        for k in _STRIP_KEYS_FOR_ROW_DIFF:
            row.pop(k, None)
        py_rows.append(row)

    # Sort both sides by tripwire_id (bash's ORDER BY ts DESC is stable
    # per side but wall-clock drift between emits means ts ordering can
    # differ — normalize on tripwire_id for the diff).
    bash_rows.sort(key=lambda r: r["tripwire_id"])
    py_rows.sort(key=lambda r: r["tripwire_id"])

    assert bash_rows == py_rows, (
        f"list_open parity broken:\n"
        f"  bash: {bash_rows!r}\n"
        f"  py:   {py_rows!r}"
    )

    # `evidence` key was parsed into a dict (not the raw JSON string) — that's
    # the high-leverage shape that list_open consumers depend on.
    for row in bash_rows + py_rows:
        assert isinstance(row["evidence"], dict), (
            f"evidence not parsed: {row['evidence']!r}"
        )
        if row["tripwire_id"] == "TW-A-bash":
            assert row["evidence"]["cost_usd"] == 12.50
        elif row["tripwire_id"] == "TW-B-bash":
            assert row["evidence"]["flagged"] is True


# ─────────────────────────────────────────────────────────────────────────────
# (6) acknowledge — both sides transition open → acknowledged
# ─────────────────────────────────────────────────────────────────────────────
def test_acknowledge_status_transition_parity(db):
    """acknowledge(event_id, "investigating") transitions status open → acknowledged
    and writes operator_response. Already-resolved rows must NOT be re-acked.
    """
    # Bash emits one row, acks it → row is acknowledged.
    rb_seed = _bash("mo_safety_event_emit", "TW-ACK-bash", "high", '{"k":1}',
                    "run-ack-bash", dbp=db)
    bash_id = rb_seed.stdout.strip()
    _assert_id_shape(bash_id)
    rb_ack = _bash("mo_safety_event_acknowledge", bash_id, "investigating",
                   dbp=db)
    assert rb_ack.returncode == 0

    # Python emits a separate row, acks it.
    rp_seed = se.emit("TW-ACK-py", "high", '{"k":1}', "run-ack-py", db=db)
    py_id = rp_seed["id"]
    _assert_id_shape(py_id)
    rp_ack = se.acknowledge(py_id, "investigating", db=db)
    assert rp_ack["rc"] == 0
    assert rp_ack["updated"] == 1

    bash_row = _row(db, bash_id)
    py_row = _row(db, py_id)
    assert bash_row["status"] == "acknowledged"
    assert py_row["status"] == "acknowledged"
    assert bash_row["operator_response"] == "investigating"
    assert py_row["operator_response"] == "investigating"

    # Re-ack: bash WHERE id=? AND status='open' — no rows match → bash row
    # stays acknowledged. Python's port returns updated=0 because the row
    # is no longer in status='open'.
    rb_ack2 = _bash("mo_safety_event_acknowledge", bash_id, "again", dbp=db)
    assert rb_ack2.returncode == 0
    rp_ack2 = se.acknowledge(py_id, "again", db=db)
    assert rp_ack2["updated"] == 0
    assert _row(db, py_id)["status"] == "acknowledged"
    assert _row(db, bash_id)["status"] == "acknowledged"


# ─────────────────────────────────────────────────────────────────────────────
# (7) resolve — both sides transition acknowledged → resolved + write resolution_ts/note
# ─────────────────────────────────────────────────────────────────────────────
def test_resolve_status_transition_parity(db):
    """resolve(event_id, "cap raised") transitions acknowledged → resolved
    and writes resolution_ts (epoch int) + resolution_note. The resolve
    UPDATE also matches status='open' (line 194-195 of bash), so we
    confirm that branch here too with a separate open-row fixture.
    """
    # ── ack-then-resolve path (bash side) ──
    rb_seed = _bash("mo_safety_event_emit", "TW-RES-bash", "critical", '{"k":2}',
                    "run-res-bash", dbp=db)
    bash_id = rb_seed.stdout.strip()
    _assert_id_shape(bash_id)
    _bash("mo_safety_event_acknowledge", bash_id, "investigating", dbp=db)
    rb_res = _bash("mo_safety_event_resolve", bash_id, "cap raised", dbp=db)
    assert rb_res.returncode == 0

    # ── ack-then-resolve path (python side) ──
    rp_seed = se.emit("TW-RES-py", "critical", '{"k":2}', "run-res-py", db=db)
    py_id = rp_seed["id"]
    se.acknowledge(py_id, "investigating", db=db)
    rp_res = se.resolve(py_id, "cap raised", db=db)
    assert rp_res["rc"] == 0
    assert rp_res["updated"] == 1

    bash_row = _row(db, bash_id)
    py_row = _row(db, py_id)
    assert bash_row["status"] == "resolved"
    assert py_row["status"] == "resolved"
    assert bash_row["resolution_note"] == "cap raised"
    assert py_row["resolution_note"] == "cap raised"
    assert isinstance(bash_row["resolution_ts"], int)
    assert isinstance(py_row["resolution_ts"], int)
    assert abs(bash_row["resolution_ts"] - py_row["resolution_ts"]) <= 2

    # ── open-direct-resolve path (resolve matches status IN ('open','acknowledged')) ──
    rb_seed2 = _bash("mo_safety_event_emit", "TW-RES2-bash", "high", '{"k":3}',
                     "run-res2-bash", dbp=db)
    bash_id2 = rb_seed2.stdout.strip()
    _assert_id_shape(bash_id2)
    rb_res2 = _bash("mo_safety_event_resolve", bash_id2, "no ack needed",
                    dbp=db)
    assert rb_res2.returncode == 0

    rp_seed2 = se.emit("TW-RES2-py", "high", '{"k":3}', "run-res2-py", db=db)
    py_id2 = rp_seed2["id"]
    rp_res2 = se.resolve(py_id2, "no ack needed", db=db)
    assert rp_res2["updated"] == 1

    assert _row(db, bash_id2)["status"] == "resolved"
    assert _row(db, py_id2)["status"] == "resolved"


# ─────────────────────────────────────────────────────────────────────────────
# (8) table-missing no-op — both sides return rc=0 + warn stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_table_missing_no_op_parity(db, capsys):
    """Drop the safety_events table. emit/ack/resolve all return rc=0 + warn stderr
    on both sides (no row written, no exception). Mirrors bash's table-missing
    branch in ``_mo_se_table_exists``.
    """
    # Drop the safety_events table.
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE safety_events")
        con.commit()
    finally:
        con.close()

    # ── emit no-op ──
    rb = _bash("mo_safety_event_emit", "TW-MISS", "high", '{"k":1}',
               "run-miss", dbp=db)
    assert rb.returncode == 0, f"bash emit rc={rb.returncode} (expected 0 no-op)"
    assert "table absent" in rb.stderr
    assert "no-op" in rb.stderr
    assert rb.stdout.strip() == ""

    rp = se.emit("TW-MISS", "high", '{"k":1}', "run-miss", db=db)
    assert rp["rc"] == 0
    assert rp["id"] == ""
    py_err = capsys.readouterr().err
    assert "table absent" in py_err
    assert "no-op" in py_err

    # ── acknowledge no-op ──
    rb_ack = _bash("mo_safety_event_acknowledge", "any-id", "response", dbp=db)
    assert rb_ack.returncode == 0
    assert "ack is a no-op" in rb_ack.stderr

    rp_ack = se.acknowledge("any-id", "response", db=db)
    assert rp_ack["rc"] == 0
    assert rp_ack["updated"] == 0
    py_err = capsys.readouterr().err
    assert "ack is a no-op" in py_err

    # ── resolve no-op ──
    rb_res = _bash("mo_safety_event_resolve", "any-id", "note", dbp=db)
    assert rb_res.returncode == 0
    assert "resolve is a no-op" in rb_res.stderr

    rp_res = se.resolve("any-id", "note", db=db)
    assert rp_res["rc"] == 0
    assert rp_res["updated"] == 0
    py_err = capsys.readouterr().err
    assert "resolve is a no-op" in py_err

    # ── list_open no-op ──
    rb_lo = _bash("mo_safety_event_list_open", dbp=db)
    assert rb_lo.returncode == 0
    assert "nothing to list" in rb_lo.stderr
    assert rb_lo.stdout.strip() == ""

    rp_lo = se.list_open(db=db)
    assert rp_lo == []
    py_err = capsys.readouterr().err
    assert "nothing to list" in py_err