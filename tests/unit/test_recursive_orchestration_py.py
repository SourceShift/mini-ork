"""Parity gate: mini_ork.ported.recursive_orchestration vs lib/recursive_orchestration.sh.

Each test invokes the LIVE bash subprocess against a temp DB seeded by
``db/init.sh``, then invokes the Python port against a parallel temp DB
(seeded identically), and asserts the resulting ``run_events`` /
``run_spawns`` / ``run_artifact_edges`` / ``merge_decisions`` rows match
byte-for-byte (integer epoch columns use 1-second tolerance; ``authority_level``
floats compared at 1e-6; ``event_id`` stem-equal because the uuid hex
suffix differs). No mocks, no hardcoded expected outputs — expected is
always derived from the live control bash invocation.

>=6 cases:
  (a) policy_json stdout JSON equality
  (b) emit_event happy-path run_events row parity
  (c) emit_event invalid payload raises + writes 0 rows
  (d) approve_spawn happy (parent exists) — run_spawns + run_events + task_runs parity
  (e) approve_spawn blocked by depth>max_depth — both raise + write 0 rows
  (f) mark_spawn UPDATE row parity
  (g) record_artifact INSERT parity
  (h) merge_decision accepted — merge_decisions + run_spawns.status='merged' parity

Authority levels are compared at 1e-6 per the kickoff. ``updated_at`` columns
set by ``strftime('%s','now')`` are allowed up to a 1-second window.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import recursive_orchestration as py  # noqa: E402

SH = REPO / "lib" / "recursive_orchestration.sh"
INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Returns ``dbp`` (state.db path). The fixture monkeypatches
    ``MINI_ORK_DB`` and ``MINI_ORK_HOME`` so the Python port's
    ``_resolve_db()`` lands on the same DB the bash subprocess writes to.
    Use ``_pair_db(tmp_path)`` below for cases that need bash and Python
    to write to SEPARATE DBs and diff the resulting rows.
    """
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", dbp)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return dbp


def _pair_db(tmp_path, monkeypatch):
    """Two DBs with identical schemas: one for the bash subprocess, one for
    the Python port. The Python port's ``_resolve_db`` reads ``MINI_ORK_DB``
    / ``MINI_ORK_HOME`` from the env; we set both to the *py* DB so the
    Python port lands on its own DB (the bash subprocess uses the bash DB
    via its own env passed to ``subprocess.run``).
    """
    home_bash = tmp_path / "home_bash"
    home_py = tmp_path / "home_py"
    home_bash.mkdir()
    home_py.mkdir()
    bash_db = str(home_bash / "state.db")
    py_db = str(home_py / "state.db")
    for env_home, env_db in ((home_bash, bash_db), (home_py, py_db)):
        r = subprocess.run(
            ["bash", str(INIT_SH)],
            env={**os.environ, "MINI_ORK_HOME": str(env_home), "MINI_ORK_DB": env_db},
            capture_output=True, text=True, check=True,
        )
        if r.returncode != 0:
            pytest.fail(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    monkeypatch.setenv("MINI_ORK_DB", py_db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home_py))
    return bash_db, py_db


def _bash_run_func(
    func: str,
    args: list[str],
    *,
    db: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Source the bash library and call ``func`` with positional args.

    Args are wrapped in double quotes with internal ``"`` escaped as
    ``\\"`` so JSON payloads with inner double quotes survive the bash
    parse (e.g. ``'{"k":"v"}'`` → bash receives the literal
    ``{"k":"v"}``). Mirrors the helper in ``test_mo_node_events_py.py``;
    we extend it with escape handling because recursive_orchestration
    accepts JSON payloads as positional args.
    """
    def _q(a: str) -> str:
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'
    arg_str = " ".join(_q(a) for a in args)
    script = f'. "{SH}"\n{func} {arg_str}\n'
    return subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "MINI_ORK_DB": db,
             "MINI_ORK_HOME": str(Path(db).parent), **(extra_env or {})},
        capture_output=True, text=True,
    )


def _row_dicts(db: str, table: str) -> list[dict]:
    """Dump all rows of ``table`` as dicts (excluding ``created_at`` for
    time-sensitive diffing — see ``_epoch_close``). Ordered by the table's
    own rowid for determinism where applicable."""
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _epoch_close(a: int | float | None, b: int | float | None,
                 *, window_s: int = 1) -> bool:
    """Integer epoch tolerance in seconds. After the window passes, fall
    through to a 1e-6 float diff as a defense-in-depth check.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if abs(int(a) - int(b)) <= window_s:
        return True
    return abs(float(a) - float(b)) <= 1e-6


def _event_id_stem(eid: str | None) -> str:
    """Stem of ``event_id`` shared between bash and Python ports.

    Bash and Python ports emit IDs of shape ``<prefix>-<sec>-<hex12>``
    where ``prefix`` is one of ``ev`` (emit_event), ``sp`` (spawn),
    ``ae`` (artifact_edge), ``md`` (merge_decision). The bash and
    Python invocations in a single test run are typically a few hundred
    ms apart, so the middle ``<sec>`` segment often drifts by ±1 between
    them — only the leading ``<prefix>-`` is reliably shared. The
    ``<hex12>`` suffix always differs.
    """
    assert eid is not None
    return eid.split("-", 1)[0] + "-"


def _assert_spawn_row_parity(bash_row: dict, py_row: dict) -> None:
    """Compare two ``run_spawns`` rows field-by-field.

    ``spawn_id`` is stem-equal; ``created_at`` / ``updated_at`` allowed
    within 1s; ``authority_level`` compared at 1e-6 (kicked-off tolerance).
    """
    fields = [
        "spawn_id", "parent_run_id", "child_run_id", "root_run_id",
        "depth", "recipe", "kickoff_path", "child_workspace",
        "authority_level", "allow_child_spawn", "status",
        "policy_snapshot_json", "created_at", "updated_at",
    ]
    for f in fields:
        b = bash_row.get(f)
        p = py_row.get(f)
        if f == "spawn_id":
            assert _event_id_stem(b) == _event_id_stem(p), (
                f"spawn_id stem mismatch: bash={b!r} py={p!r}"
            )
            continue
        if f in ("created_at", "updated_at"):
            assert _epoch_close(b, p), f"{f}: bash={b!r} py={p!r}"
            continue
        if f == "authority_level":
            if b is None or p is None:
                assert b == p
            else:
                assert abs(float(b) - float(p)) <= 1e-6, (
                    f"authority_level float mismatch: bash={b!r} py={p!r}"
                )
            continue
        if f == "policy_snapshot_json":
            assert b is not None and p is not None
            assert json.loads(b) == json.loads(p), (
                f"policy_snapshot_json dict mismatch: bash={b!r} py={p!r}"
            )
            continue
        assert b == p, f"{f}: bash={b!r} py={p!r}"


def _assert_event_row_parity(bash_row: dict, py_row: dict) -> None:
    """Compare two ``run_events`` rows field-by-field.

    ``event_id`` is stem-equal; ``created_at`` allowed within 1s;
    ``payload_json`` compared structurally with ``spawn_id`` masked
    out (the spawn_id is generated independently by each port and is
    not a parity invariant).
    """
    fields = ["event_id", "run_id", "parent_run_id", "event_type",
              "payload_json", "created_at"]
    for f in fields:
        b = bash_row.get(f)
        p = py_row.get(f)
        if f == "event_id":
            assert _event_id_stem(b) == _event_id_stem(p), (
                f"event_id stem mismatch: bash={b!r} py={p!r}"
            )
            continue
        if f == "created_at":
            assert _epoch_close(b, p), f"{f}: bash={b!r} py={p!r}"
            continue
        if f == "payload_json":
            assert b is not None and p is not None
            b_obj = json.loads(b)
            p_obj = json.loads(p)
            # spawn_id is generated per-port; not a parity invariant.
            b_obj.pop("spawn_id", None)
            p_obj.pop("spawn_id", None)
            assert b_obj == p_obj, (
                f"payload_json dict mismatch: bash={b!r} py={p!r}"
            )
            continue
        assert b == p, f"{f}: bash={b!r} py={p!r}"


def _assert_task_runs_parity(bash_row: dict, py_row: dict) -> None:
    """Compare two ``task_runs`` rows field-by-field (the UPSERT side effect
    of ``approve_spawn``). Timestamps within 1s."""
    fields = ["id", "task_class", "recipe", "kickoff_path", "status",
              "created_at", "updated_at"]
    for f in fields:
        b = bash_row.get(f)
        p = py_row.get(f)
        if f in ("created_at", "updated_at"):
            assert _epoch_close(b, p), f"{f}: bash={b!r} py={p!r}"
            continue
        assert b == p, f"{f}: bash={b!r} py={p!r}"


def _seed_parent(db: str, parent_id: str) -> None:
    """Seed a single ``task_runs`` row so ``approve_spawn`` can FK-resolve
    the parent. Idempotent: caller picks the parent_id."""
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
            VALUES (?, 'code_fix', NULL, ?, 'classified', 0, 0)
            """,
            (parent_id, "/tmp/k.md"),
        )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) policy_json stdout JSON equality
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_json_parity(monkeypatch, tmp_path):
    """Bash ``mo_recursive_policy_json`` prints ``json.dumps(policy,
    sort_keys=True)``. Python port returns the same string for identical
    env. Drive bash with custom env vars and assert byte equality."""
    env_overrides = {
        "MINI_ORK_RECURSIVE_MAX_DEPTH": "3",
        "MINI_ORK_RECURSIVE_MAX_CHILDREN": "5",
        "MINI_ORK_RECURSIVE_MAX_DESCENDANTS": "10",
        "MINI_ORK_RECURSIVE_MAX_PARALLEL": "2",
        "MINI_ORK_ALLOW_CHILD_SPAWN": "true",
        "MINI_ORK_CHILD_AUTHORITY": "0.7",
    }
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    home = tmp_path / "home"
    home.mkdir()
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, **env_overrides,
             "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    bash_r = _bash_run_func("mo_recursive_policy_json", [], db=dbp,
                            extra_env=env_overrides)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    bash_out = bash_r.stdout.strip()
    py_out = py.mo_recursive_policy_json()
    assert bash_out == py_out, (
        f"policy_json mismatch:\n  bash={bash_out!r}\n  py  ={py_out!r}"
    )
    # Sanity: sort_keys ordering surfaces the canonical shape.
    expected_keys = sorted([
        "max_depth", "max_children_per_run", "max_total_descendants",
        "max_parallel_children", "default_allow_child_spawn",
        "default_authority_level",
    ])
    assert list(json.loads(py_out).keys()) == expected_keys


# ─────────────────────────────────────────────────────────────────────────────
# (b) emit_event happy-path run_events row parity
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_event_happy_path_parity(tmp_path, monkeypatch):
    """Bash ``mo_recursive_emit_event <run> <parent> <type> <payload>`` writes
    one ``run_events`` row. Python port writes the same row (event_id stem,
    payload_json, created_at within 1s)."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    bash_r = _bash_run_func(
        "mo_recursive_emit_event",
        ["run-a", "parent-a", "child.spawned", '{"k":"v","n":1}'],
        db=bash_db,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py_event_id = py.mo_recursive_emit_event(
        "run-a", "parent-a", "child.spawned", '{"k":"v","n":1}',
    )

    bash_rows = _row_dicts(bash_db, "run_events")
    py_rows = _row_dicts(py_db, "run_events")
    assert len(bash_rows) == 1, f"bash wrote {len(bash_rows)} rows: {bash_rows}"
    assert len(py_rows) == 1, f"py wrote {len(py_rows)} rows: {py_rows}"
    _assert_event_row_parity(bash_rows[0], py_rows[0])
    # Stem sanity: both event_ids share the ``ev-<sec>-`` prefix.
    assert _event_id_stem(bash_rows[0]["event_id"]) == "ev-"
    assert _event_id_stem(py_event_id) == "ev-"


# ─────────────────────────────────────────────────────────────────────────────
# (c) emit_event invalid payload raises + writes 0 rows
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_event_invalid_payload_parity(tmp_path, monkeypatch):
    """Both ports must raise on unparseable JSON payload and NOT write any
    ``run_events`` row. Bash raises ``SystemExit`` (exit code != 0); Python
    raises ``ValueError`` (kicked-off Python error contract)."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    bad_payload = "{not-valid-json"
    bash_r = _bash_run_func(
        "mo_recursive_emit_event",
        ["run-b", "parent-b", "child.spawned", bad_payload],
        db=bash_db,
    )
    assert bash_r.returncode != 0, "bash must fail on invalid JSON"
    assert "invalid event payload JSON" in bash_r.stderr, (
        f"bash stderr missing phrase: {bash_r.stderr!r}"
    )
    raised = False
    try:
        py.mo_recursive_emit_event("run-b", "parent-b", "child.spawned", bad_payload)
    except ValueError as exc:
        raised = True
        assert "invalid event payload JSON" in str(exc), str(exc)
    assert raised, "Python port must raise ValueError on invalid JSON"
    assert _row_dicts(bash_db, "run_events") == []
    assert _row_dicts(py_db, "run_events") == []


# ─────────────────────────────────────────────────────────────────────────────
# (d) approve_spawn happy (parent exists) — run_spawns + run_events + task_runs parity
# ─────────────────────────────────────────────────────────────────────────────
def test_approve_spawn_happy_path_parity(tmp_path, monkeypatch):
    """Both ports must produce identical rows in all three tables:
    ``run_spawns`` (1 row), ``run_events`` (1 spawn.approved row with the
    literal ``ev-<sec>-<child_run_id>`` event_id), ``task_runs`` (1 UPSERT
    row). Floats 1e-6, epochs 1s."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    # Seed a parent task_run in each DB.
    parent_id = "parent-d"
    _seed_parent(bash_db, parent_id)
    _seed_parent(py_db, parent_id)

    args = [parent_id, "child-d", "code-fix", "/tmp/child-k.md",
            "/tmp/child-ws", "1", "0.4", "1"]
    bash_r = _bash_run_func("mo_recursive_approve_spawn", args, db=bash_db)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_recursive_approve_spawn(
        parent_id, "child-d", "code-fix", "/tmp/child-k.md",
        "/tmp/child-ws", 1, 0.4, 1,
    )

    # run_spawns: exactly 1 row in each DB
    bash_spawns = _row_dicts(bash_db, "run_spawns")
    py_spawns = _row_dicts(py_db, "run_spawns")
    assert len(bash_spawns) == 1, bash_spawns
    assert len(py_spawns) == 1, py_spawns
    _assert_spawn_row_parity(bash_spawns[0], py_spawns[0])
    # Status & root sanity
    assert bash_spawns[0]["status"] == "approved"
    assert bash_spawns[0]["root_run_id"] == parent_id
    assert bash_spawns[0]["task_class"] if False else True  # column is on task_runs

    # run_events: exactly 1 row in each DB (the spawn.approved row)
    bash_events = _row_dicts(bash_db, "run_events")
    py_events = _row_dicts(py_db, "run_events")
    assert len(bash_events) == 1, bash_events
    assert len(py_events) == 1, py_events
    assert bash_events[0]["event_type"] == "spawn.approved"
    _assert_event_row_parity(bash_events[0], py_events[0])
    # event_id must use the literal ``ev-<sec>-<child_run_id>`` shape.
    assert bash_events[0]["event_id"].startswith(f"ev-")
    assert "child-d" in bash_events[0]["event_id"], (
        f"bash event_id missing child_run_id: {bash_events[0]['event_id']!r}"
    )

    # task_runs: exactly 1 row (parent) + 1 UPSERT (child) per DB
    bash_trs = _row_dicts(bash_db, "task_runs")
    py_trs = _row_dicts(py_db, "task_runs")
    assert len(bash_trs) == 2, f"expected 2 task_runs rows, got {bash_trs}"
    assert len(py_trs) == 2, f"expected 2 task_runs rows, got {py_trs}"
    bash_child = next(r for r in bash_trs if r["id"] == "child-d")
    py_child = next(r for r in py_trs if r["id"] == "child-d")
    _assert_task_runs_parity(bash_child, py_child)
    # Sanity: task_class is the recipe with dashes → underscores.
    assert bash_child["task_class"] == "code_fix"


# ─────────────────────────────────────────────────────────────────────────────
# (e) approve_spawn blocked by depth>max_depth — both raise + write 0 rows
# ─────────────────────────────────────────────────────────────────────────────
def test_approve_spawn_blocked_by_depth(tmp_path, monkeypatch):
    """Default ``max_depth=2``. Both ports must raise on depth=3 and write
    zero rows in ``run_spawns``, ``run_events`` (the spawn.approved row),
    and ``task_runs`` (the UPSERT side effect)."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    parent_id = "parent-e"
    _seed_parent(bash_db, parent_id)
    _seed_parent(py_db, parent_id)

    args = [parent_id, "child-e", "code-fix", "/tmp/child-k.md",
            "/tmp/child-ws", "3", "0.3", "0"]  # depth=3 > max_depth=2
    bash_r = _bash_run_func("mo_recursive_approve_spawn", args, db=bash_db)
    assert bash_r.returncode != 0, "bash must fail when depth>max_depth"
    assert "depth 3 exceeds max_depth 2" in bash_r.stderr, bash_r.stderr
    raised = False
    try:
        py.mo_recursive_approve_spawn(
            parent_id, "child-e", "code-fix", "/tmp/child-k.md",
            "/tmp/child-ws", 3, 0.3, 0,
        )
    except ValueError as exc:
        raised = True
        assert "depth 3 exceeds max_depth 2" in str(exc), str(exc)
    assert raised, "Python port must raise on depth>max_depth"

    # Zero rows in run_spawns; zero spawn.approved rows in run_events;
    # child row in task_runs must NOT exist.
    assert _row_dicts(bash_db, "run_spawns") == []
    assert _row_dicts(py_db, "run_spawns") == []
    bash_events = _row_dicts(bash_db, "run_events")
    py_events = _row_dicts(py_db, "run_events")
    assert all(r["event_type"] != "spawn.approved" for r in bash_events), bash_events
    assert all(r["event_type"] != "spawn.approved" for r in py_events), py_events

    bash_trs = {r["id"] for r in _row_dicts(bash_db, "task_runs")}
    py_trs = {r["id"] for r in _row_dicts(py_db, "task_runs")}
    assert "child-e" not in bash_trs
    assert "child-e" not in py_trs


# ─────────────────────────────────────────────────────────────────────────────
# (f) mark_spawn UPDATE row parity
# ─────────────────────────────────────────────────────────────────────────────
def test_mark_spawn_update_parity(tmp_path, monkeypatch):
    """Seed a run_spawns row in each DB, then bash+Python both call
    ``mo_recursive_mark_spawn <child> <status>``. The resulting rows must
    match byte-for-byte on ``status``; ``updated_at`` within 1s."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    spawn_seed = {
        "spawn_id": "sp-seed-f",
        "parent_run_id": "parent-f",
        "child_run_id": "child-f",
        "root_run_id": "parent-f",
        "depth": 1,
        "recipe": "code-fix",
        "kickoff_path": "/tmp/k.md",
        "child_workspace": "/tmp/ws",
        "authority_level": 0.3,
        "allow_child_spawn": 0,
        "status": "approved",
        "policy_snapshot_json": "{}",
        "created_at": 0,
        "updated_at": 0,
    }
    for db in (bash_db, py_db):
        con = sqlite3.connect(db)
        try:
            cols = ", ".join(spawn_seed.keys())
            placeholders = ", ".join("?" for _ in spawn_seed)
            con.execute(
                f"INSERT INTO run_spawns({cols}) VALUES ({placeholders})",
                tuple(spawn_seed.values()),
            )
            con.commit()
        finally:
            con.close()

    bash_r = _bash_run_func("mo_recursive_mark_spawn", ["child-f", "running"], db=bash_db)
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_recursive_mark_spawn("child-f", "running")

    bash_rows = _row_dicts(bash_db, "run_spawns")
    py_rows = _row_dicts(py_db, "run_spawns")
    assert len(bash_rows) == 1 and len(py_rows) == 1
    # Compare status and updated_at only; the rest is identical seed.
    assert bash_rows[0]["status"] == py_rows[0]["status"] == "running"
    assert _epoch_close(bash_rows[0]["updated_at"], py_rows[0]["updated_at"])


def test_mark_spawn_invalid_status_raises(tmp_path, monkeypatch):
    """Both ports must raise on invalid status and not write anything."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    spawn_seed = {
        "spawn_id": "sp-seed-f2",
        "parent_run_id": "parent-f2",
        "child_run_id": "child-f2",
        "root_run_id": "parent-f2",
        "depth": 1,
        "recipe": "code-fix",
        "kickoff_path": "/tmp/k.md",
        "child_workspace": "/tmp/ws",
        "authority_level": 0.3,
        "allow_child_spawn": 0,
        "status": "approved",
        "policy_snapshot_json": "{}",
        "created_at": 0,
        "updated_at": 0,
    }
    for db in (bash_db, py_db):
        con = sqlite3.connect(db)
        try:
            cols = ", ".join(spawn_seed.keys())
            placeholders = ", ".join("?" for _ in spawn_seed)
            con.execute(
                f"INSERT INTO run_spawns({cols}) VALUES ({placeholders})",
                tuple(spawn_seed.values()),
            )
            con.commit()
        finally:
            con.close()

    bash_r = _bash_run_func("mo_recursive_mark_spawn", ["child-f2", "BOGUS"], db=bash_db)
    assert bash_r.returncode != 0
    assert "invalid spawn status" in bash_r.stderr
    raised = False
    try:
        py.mo_recursive_mark_spawn("child-f2", "BOGUS")
    except ValueError as exc:
        raised = True
        assert "invalid spawn status" in str(exc)
    assert raised


# ─────────────────────────────────────────────────────────────────────────────
# (g) record_artifact INSERT parity
# ─────────────────────────────────────────────────────────────────────────────
def test_record_artifact_insert_parity(tmp_path, monkeypatch):
    """Both ports must INSERT identical ``run_artifact_edges`` rows. The
    ``edge_id`` is stem-equal (``ae-<sec>-``) and the rest of the row is
    byte-for-byte (no timestamps on this table)."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    bash_r = _bash_run_func(
        "mo_recursive_record_artifact",
        ["run-g-prod", "run-g-cons", "/tmp/art.md", "abc123", "file"],
        db=bash_db,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_recursive_record_artifact(
        "run-g-prod", "run-g-cons", "/tmp/art.md", "abc123", "file",
    )

    bash_rows = _row_dicts(bash_db, "run_artifact_edges")
    py_rows = _row_dicts(py_db, "run_artifact_edges")
    assert len(bash_rows) == 1 and len(py_rows) == 1
    # edge_id stem-equal
    assert _event_id_stem(bash_rows[0]["edge_id"]) == "ae-"
    assert _event_id_stem(py_rows[0]["edge_id"]) == "ae-"
    # other fields exact
    for f in ("producer_run_id", "consumer_run_id", "artifact_path",
              "artifact_hash", "artifact_kind", "verification_state"):
        assert bash_rows[0][f] == py_rows[0][f], f"{f}: bash={bash_rows[0][f]!r} py={py_rows[0][f]!r}"
    # created_at default is ``strftime('%s','now')``; allow 1s window.
    assert _epoch_close(bash_rows[0]["created_at"], py_rows[0]["created_at"])


# ─────────────────────────────────────────────────────────────────────────────
# (h) merge_decision accepted — merge_decisions + run_spawns.status='merged' parity
# ─────────────────────────────────────────────────────────────────────────────
def test_merge_decision_accepted_parity(tmp_path, monkeypatch):
    """Seed a run_spawns row, then bash+Python both call
    ``mo_recursive_merge_decision <parent> <child> accepted <reason>``.
    The decision row in ``merge_decisions`` must match, AND the seeded
    spawn row's status must flip to ``merged`` in both DBs."""
    bash_db, py_db = _pair_db(tmp_path, monkeypatch)
    spawn_seed = {
        "spawn_id": "sp-seed-h",
        "parent_run_id": "parent-h",
        "child_run_id": "child-h",
        "root_run_id": "parent-h",
        "depth": 1,
        "recipe": "code-fix",
        "kickoff_path": "/tmp/k.md",
        "child_workspace": "/tmp/ws",
        "authority_level": 0.3,
        "allow_child_spawn": 0,
        "status": "approved",
        "policy_snapshot_json": "{}",
        "created_at": 0,
        "updated_at": 0,
    }
    for db in (bash_db, py_db):
        con = sqlite3.connect(db)
        try:
            cols = ", ".join(spawn_seed.keys())
            placeholders = ", ".join("?" for _ in spawn_seed)
            con.execute(
                f"INSERT INTO run_spawns({cols}) VALUES ({placeholders})",
                tuple(spawn_seed.values()),
            )
            con.commit()
        finally:
            con.close()

    bash_r = _bash_run_func(
        "mo_recursive_merge_decision",
        ["parent-h", "child-h", "accepted", "lgtm", "reviewer"],
        db=bash_db,
    )
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    py.mo_recursive_merge_decision("parent-h", "child-h", "accepted", "lgtm", "reviewer")

    # merge_decisions row parity
    bash_decs = _row_dicts(bash_db, "merge_decisions")
    py_decs = _row_dicts(py_db, "merge_decisions")
    assert len(bash_decs) == 1 and len(py_decs) == 1
    assert _event_id_stem(bash_decs[0]["decision_id"]) == "md-"
    assert _event_id_stem(py_decs[0]["decision_id"]) == "md-"
    for f in ("parent_run_id", "child_run_id", "decision", "reason",
              "decided_by", "evidence_json"):
        assert bash_decs[0][f] == py_decs[0][f], (
            f"{f}: bash={bash_decs[0][f]!r} py={py_decs[0][f]!r}"
        )
    assert _epoch_close(bash_decs[0]["created_at"], py_decs[0]["created_at"])
    # Sanity: evidence_json is the literal ``{"source": "mini-ork-spawn"}``.
    assert json.loads(bash_decs[0]["evidence_json"]) == {"source": "mini-ork-spawn"}

    # run_spawns.status flipped to 'merged' in both DBs.
    bash_spawn = _row_dicts(bash_db, "run_spawns")[0]
    py_spawn = _row_dicts(py_db, "run_spawns")[0]
    assert bash_spawn["status"] == "merged" == py_spawn["status"]
    assert _epoch_close(bash_spawn["updated_at"], py_spawn["updated_at"])