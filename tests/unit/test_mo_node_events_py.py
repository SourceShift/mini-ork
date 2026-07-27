"""Unit tests: mini_ork.observability.node_events (bash parity halves removed; formerly vs lib/mo_node_events.sh).

Each test invokes the Python port against a temp DB seeded by `db/init.sh`
and asserts the resulting `run_events` rows semantically (integer epoch
columns checked against wall-clock bounds; the nanosecond/PID suffix on
`event_id` is checked by stem only). No mocks.

Cases:
  (a) mo_node_emit happy path node_start         — payload merge + finish_reason
  (b) mo_node_end w/ explicit args               — full payload (verdict/artifact/finish_reason)
  (c) mo_emit_node_heartbeat                     — populates last_heartbeat_at (migration 0023)
  (d) mo_node_start with/without model_lane      — extra = {} vs {model_lane}
  (e) _build_extra_json                          — payload shaping contract
  (f) missing required arg                       — rc 0 + stderr phrase, no row
  (g) missing state.db silent no-op              — 0 rows, exit 0, no file created
  (h) mo_node_emit_end_trap                      — guards + happy path + rc!=0 default
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.observability import node_events as py

INIT_SH = REPO / "db" / "init.sh"


@pytest.fixture
def temp_db(tmp_path_factory, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Returns (db_path, home_dir) tuple. The fixture monkeypatches
    `MINI_ORK_DB` and `MINI_ORK_HOME` in the parent pytest env so the
    Python port's `_resolve_db()` lands on this DB.
    """
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return dbp, home


def _seed_db(home: Path) -> str:
    """Materialize a fresh DB under `home`. Returns db path."""
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp


def _row_dict(con: sqlite3.Connection, table: str = "run_events") -> list[dict]:
    """Dump rows as dicts, ordered by `created_at` then `event_id` for
    determinism."""
    cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM {table} ORDER BY created_at, event_id"
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _event_id_stem(eid: str | None) -> str:
    """Return the `evt-<event_type>-<node_id>-` prefix (everything up to and
    including the 3rd dash-segment); the trailing `<timestamp>-<pid>`
    segments are runtime-specific."""
    assert eid is not None, "event_id must not be None"
    parts = eid.split("-")
    return "-".join(parts[:3]) + "-"


# ─────────────────────────────────────────────────────────────────────────────
# (a) mo_node_emit happy path node_start
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_emit_node_start(temp_db):
    """`mo_node_emit <run> <node> <type> node_start '<json>'` writes one
    `run_events` row with `event_id` stem `evt-node_start-<node>-` and
    payload_json merging `node_id`/`node_type` with the parsed extra dict."""
    db, _ = temp_db
    t0 = int(time.time())
    py.mo_node_emit("run-a", "n-a", "researcher", "node_start", '{"model_lane":"minimax"}')

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    row = rows[0]
    assert row["event_id"].startswith("evt-node_start-n-a-")
    assert row["run_id"] == "run-a"
    assert row["event_type"] == "node_start"
    payload = json.loads(row["payload_json"])
    assert payload["node_id"] == "n-a"
    assert payload["node_type"] == "researcher"
    assert payload["model_lane"] == "minimax"
    assert abs(int(row["created_at"]) - t0) <= 2
    assert row["finish_reason"] is None
    # node_start populates last_heartbeat_at (migration 0023)
    assert row["last_heartbeat_at"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# (b) mo_node_end w/ explicit verdict/artifact/finish_reason
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_emit_node_end_full_payload(temp_db):
    """`mo_node_end <run> <node> <type> <dur> <verdict> <artifact> <finish>`
    builds a JSON payload with `duration_ms` + optional fields."""
    db, _ = temp_db
    py.mo_node_end("run-b", "n-b", "implementer", 1234, "pass", "/tmp/art.md", "done")

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"].startswith("evt-node_end-n-b-")
    assert row["event_type"] == "node_end"
    assert row["finish_reason"] == "done"
    payload = json.loads(row["payload_json"])
    assert payload["duration_ms"] == 1234
    assert payload["verdict"] == "pass"
    assert payload["artifact_path"] == "/tmp/art.md"
    assert payload["finish_reason"] == "done"


# ─────────────────────────────────────────────────────────────────────────────
# (c) mo_emit_node_heartbeat populates last_heartbeat_at
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_heartbeat_writes_last_heartbeat_at(temp_db, monkeypatch):
    """Migration 0023 adds `last_heartbeat_at` to `run_events`; it is
    populated when `event_type in ('node_start', 'node_heartbeat')`.

    The port reads `MO_NODE_TYPE` from the env (defaulting to
    `'heartbeat'`); we override it to `'reviewer'` so the test exercises
    the env-override path.
    """
    monkeypatch.setenv("MO_NODE_TYPE", "reviewer")
    db, _ = temp_db
    py.mo_emit_node_heartbeat("n-c", "run-c")

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "node_heartbeat"
    assert r["last_heartbeat_at"] is not None
    assert r["finish_reason"] is None
    payload = json.loads(r["payload_json"])
    assert payload["node_type"] == "reviewer"  # MO_NODE_TYPE=reviewer


# ─────────────────────────────────────────────────────────────────────────────
# (d) mo_node_start with/without model_lane
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_start_with_and_without_model_lane(temp_db):
    """`mo_node_start <run> <node> <type> [<lane>]` builds
    `extra = {"model_lane": <lane>}` when non-empty, else the empty-object
    literal `{}`."""
    db, _ = temp_db

    # (i) with model_lane
    py.mo_node_start("run-d1", "n-d1", "verifier", "opus")
    # (ii) without model_lane
    py.mo_node_start("run-d2", "n-d2", "verifier")

    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 2
    by_run = {r["run_id"]: r for r in rows}
    lane_payload = json.loads(by_run["run-d1"]["payload_json"])
    no_lane_payload = json.loads(by_run["run-d2"]["payload_json"])
    assert lane_payload["model_lane"] == "opus"
    assert "model_lane" not in no_lane_payload
    for r in rows:
        assert r["event_type"] == "node_start"


# ─────────────────────────────────────────────────────────────────────────────
# (e) _build_extra_json payload shaping contract
# ─────────────────────────────────────────────────────────────────────────────
def test_build_extra_json_contract():
    """`_build_extra_json` shapes the node_end extra payload: duration_ms
    always present (int, empty→0); verdict/artifact_path/finish_reason only
    when non-empty; compact JSON key order is insertion order."""
    cases = [
        (("1000", "", "", ""), {"duration_ms": 1000}),
        (("1000", "pass", "", ""), {"duration_ms": 1000, "verdict": "pass"}),
        (("1000", "pass", "/tmp/art", "done"),
         {"duration_ms": 1000, "verdict": "pass",
          "artifact_path": "/tmp/art", "finish_reason": "done"}),
        (("0", "", "/tmp/x", "error"),
         {"duration_ms": 0, "artifact_path": "/tmp/x", "finish_reason": "error"}),
        (("42", "verdict-with-spaces and |pipe|", "/path/with spaces", "max_steps"),
         {"duration_ms": 42, "verdict": "verdict-with-spaces and |pipe|",
          "artifact_path": "/path/with spaces", "finish_reason": "max_steps"}),
    ]
    for args, expected_dict in cases:
        actual = py._build_extra_json(*args)
        assert json.loads(actual) == expected_dict, (
            f"build_extra_json mismatch ({args!r}): {actual!r}"
        )
        # byte-shape: json.dumps default separators, insertion order
        assert actual == json.dumps(expected_dict)


# ─────────────────────────────────────────────────────────────────────────────
# (f) missing required arg (stderr phrase + return 0)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("missing_arg,py_kwargs,expected_phrase", [
    ("run_id",
     {"run_id": "", "node_id": "n-f", "node_type": "researcher",
      "event_type": "node_start", "extra_json": "{}"},
     "mo_node_emit: run_id required"),
    ("node_id",
     {"run_id": "run-f", "node_id": "", "node_type": "researcher",
      "event_type": "node_start", "extra_json": "{}"},
     "mo_node_emit: node_id required"),
    ("event_type",
     {"run_id": "run-f", "node_id": "n-f", "node_type": "researcher",
      "event_type": "", "extra_json": "{}"},
     "mo_node_emit: event_type required"),
])
def test_missing_required_arg(temp_db, capsys, missing_arg, py_kwargs,
                              expected_phrase):
    """Guard miss emits the canonical `mo_node_emit: <arg> required` stderr
    phrase and returns 0 (silent). No row is written."""
    db, _ = temp_db
    rc_py = py.mo_node_emit(**py_kwargs)
    captured = capsys.readouterr()
    assert rc_py == 0, f"[{missing_arg}] port returned {rc_py} (must be 0)"
    assert expected_phrase in captured.err, (
        f"[{missing_arg}] stderr missing phrase {expected_phrase!r}: {captured.err!r}"
    )
    con = sqlite3.connect(db)
    try:
        count = con.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    finally:
        con.close()
    assert count == 0, f"missing-arg case must NOT write a row; got {count}"


def test_stderr_exact_phrase(temp_db, capsys):
    """The port's stderr phrase is exactly `mo_node_emit: run_id required`
    (modulo the trailing newline print adds)."""
    db, _ = temp_db
    rc_py = py.mo_node_emit("", "n", "t", "node_start")
    captured = capsys.readouterr()
    py_stderr = captured.err.rstrip("\n")
    assert rc_py == 0
    assert py_stderr == "mo_node_emit: run_id required"


# ─────────────────────────────────────────────────────────────────────────────
# (g) missing state.db silent no-op
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_state_db_silent_noop(tmp_path, monkeypatch):
    """The port silently no-ops when the resolved state.db does not exist:
    exit 0 and no phantom DB file created."""
    missing_db = str(tmp_path / "nonexistent" / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", missing_db)
    rc_py = py.mo_node_emit("run-g", "n-g", "t", "node_start", "{}")
    assert rc_py == 0
    assert not os.path.exists(missing_db), (
        f"silent no-op must not create {missing_db}"
    )


def test_missing_state_db_noop_does_not_create_file(tmp_path, monkeypatch):
    """Same contract with `MINI_ORK_HOME` resolved but state.db not yet
    seeded: return 0, no file created."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    assert not os.path.exists(dbp)
    rc_py = py.mo_node_emit("r", "n", "t", "node_start", "{}")
    assert rc_py == 0
    assert not os.path.exists(dbp), "state.db must not be auto-created"


# ─────────────────────────────────────────────────────────────────────────────
# (h) mo_node_emit_end_trap: guards + happy path + rc!=0 default finish_reason
# ─────────────────────────────────────────────────────────────────────────────
def test_mo_node_emit_end_trap_early_return(temp_db):
    """Early-returns 0 on empty `_mo_run_id`/`node_id`/`node_type`."""
    db, _ = temp_db
    # Empty _run_id
    assert py.mo_node_emit_end_trap("", "n-h", "t", 1000, 0) == 0
    # Empty node_id
    assert py.mo_node_emit_end_trap("r-h", "", "t", 1000, 0) == 0
    # Empty node_type
    assert py.mo_node_emit_end_trap("r-h", "n-h", "", 1000, 0) == 0
    # None should be written.
    con = sqlite3.connect(db)
    try:
        count = con.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    finally:
        con.close()
    assert count == 0


def test_mo_node_emit_end_trap_happy_path(temp_db):
    """The end-trap computes `duration_ms = end_ms - start_ms` at runtime and
    emits a full node_end row with the explicit verdict/artifact/finish."""
    db, _ = temp_db
    start_ms = py._now_ms() - 5000
    rc = py.mo_node_emit_end_trap(
        "run-h", "n-h", "implementer", start_ms, 0,
        context_path="/tmp/art.md", verdict="pass", finish_reason="done",
    )
    assert rc == 0
    con = sqlite3.connect(db)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "node_end"
    assert row["finish_reason"] == "done"
    payload = json.loads(row["payload_json"])
    assert payload["duration_ms"] >= 4000  # started 5000ms ago (allow jitter)
    assert payload["verdict"] == "pass"
    assert payload["artifact_path"] == "/tmp/art.md"
    assert payload["finish_reason"] == "done"


def test_mo_node_emit_end_trap_rc_nonzero_defaults_to_error(tmp_db):
    """Default finish_reason on rc != 0 is 'error'."""
    home = tmp_db
    dbp = _seed_db(home)
    start_ms = py._now_ms() - 1000
    rc = py.mo_node_emit_end_trap(
        "run-err", "n-err", "implementer", start_ms, 1,
    )
    assert rc == 0
    con = sqlite3.connect(dbp)
    try:
        rows = _row_dict(con)
    finally:
        con.close()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["finish_reason"] == "error"
    assert payload["duration_ms"] >= 0


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """A separate fixture for tests that need their own DB distinct from
    `temp_db`. Returns the home dir; call `_seed_db` to materialize the DB.
    The env vars are monkeypatched in advance so subsequent Python port
    calls resolve to the same DB once `_seed_db` is called."""
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home
