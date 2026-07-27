"""Unit tests: mini_ork.orchestration.recursive (bash parity halves removed; formerly vs lib/recursive_orchestration.sh).

Each test invokes the Python port against a temp DB seeded by
``db/init.sh`` and asserts the resulting ``run_events`` / ``run_spawns`` /
``run_artifact_edges`` / ``merge_decisions`` rows semantically (event ids
checked by ``<prefix>-`` stem because the sec/uuid suffix is
runtime-generated; ``authority_level`` floats at 1e-6). No mocks.

Cases:
  (a) policy_json stdout JSON shape + env overrides
  (b) emit_event happy-path run_events row
  (c) emit_event invalid payload raises + writes 0 rows
  (d) approve_spawn happy (parent exists) — run_spawns + run_events + task_runs
  (e) approve_spawn blocked by depth>max_depth — raises + writes 0 rows
  (f) mark_spawn UPDATE row (+ invalid status raises)
  (g) record_artifact INSERT
  (h) merge_decision accepted — merge_decisions + run_spawns.status='merged'
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
from mini_ork.orchestration import recursive as py

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Spin up a real mini-ork SQLite DB via db/init.sh.

    Returns ``dbp`` (state.db path). The fixture monkeypatches
    ``MINI_ORK_DB`` and ``MINI_ORK_HOME`` so the Python port's
    ``_resolve_db()`` lands on this DB.
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


def _row_dicts(db: str, table: str) -> list[dict]:
    """Dump all rows of ``table`` as dicts."""
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _event_id_stem(eid: str | None) -> str:
    """Leading ``<prefix>-`` of ids of shape ``<prefix>-<sec>-<hex12>``
    (``ev`` emit_event, ``sp`` spawn, ``ae`` artifact_edge, ``md``
    merge_decision)."""
    assert eid is not None
    return eid.split("-", 1)[0] + "-"


def _seed_parent(db: str, parent_id: str) -> None:
    """Seed a single ``task_runs`` row so ``approve_spawn`` can FK-resolve
    the parent."""
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


def _seed_spawn(db: str, spawn_id: str, parent: str, child: str) -> None:
    spawn_seed = {
        "spawn_id": spawn_id,
        "parent_run_id": parent,
        "child_run_id": child,
        "root_run_id": parent,
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


# ─────────────────────────────────────────────────────────────────────────────
# (a) policy_json stdout JSON shape
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_json(monkeypatch, tmp_path):
    """``mo_recursive_policy_json`` returns ``json.dumps(policy,
    sort_keys=True)`` honoring the env knobs."""
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
    py_out = py.mo_recursive_policy_json()
    doc = json.loads(py_out)
    # Canonical shape: sorted keys.
    expected_keys = sorted([
        "max_depth", "max_children_per_run", "max_total_descendants",
        "max_parallel_children", "default_allow_child_spawn",
        "default_authority_level",
    ])
    assert list(doc.keys()) == expected_keys
    assert doc["max_depth"] == 3
    assert doc["max_children_per_run"] == 5
    assert doc["max_total_descendants"] == 10
    assert doc["max_parallel_children"] == 2
    assert doc["default_allow_child_spawn"] is True
    assert abs(doc["default_authority_level"] - 0.7) <= 1e-6
    # sort_keys=True byte shape
    assert py_out == json.dumps(doc, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────────
# (b) emit_event happy-path run_events row
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_event_happy_path(temp_db):
    """``mo_recursive_emit_event <run> <parent> <type> <payload>`` writes
    one ``run_events`` row."""
    py_event_id = py.mo_recursive_emit_event(
        "run-a", "parent-a", "child.spawned", '{"k":"v","n":1}',
    )

    py_rows = _row_dicts(temp_db, "run_events")
    assert len(py_rows) == 1, f"py wrote {len(py_rows)} rows: {py_rows}"
    row = py_rows[0]
    assert row["run_id"] == "run-a"
    assert row["parent_run_id"] == "parent-a"
    assert row["event_type"] == "child.spawned"
    assert json.loads(row["payload_json"]) == {"k": "v", "n": 1}
    # Stem sanity: event_id has the ``ev-<sec>-`` prefix.
    assert _event_id_stem(row["event_id"]) == "ev-"
    assert _event_id_stem(py_event_id) == "ev-"


# ─────────────────────────────────────────────────────────────────────────────
# (c) emit_event invalid payload raises + writes 0 rows
# ─────────────────────────────────────────────────────────────────────────────
def test_emit_event_invalid_payload(temp_db):
    """Unparseable JSON payload raises ValueError and writes no row."""
    bad_payload = "{not-valid-json"
    raised = False
    try:
        py.mo_recursive_emit_event("run-b", "parent-b", "child.spawned", bad_payload)
    except ValueError as exc:
        raised = True
        assert "invalid event payload JSON" in str(exc), str(exc)
    assert raised, "Python port must raise ValueError on invalid JSON"
    assert _row_dicts(temp_db, "run_events") == []


# ─────────────────────────────────────────────────────────────────────────────
# (d) approve_spawn happy (parent exists) — run_spawns + run_events + task_runs
# ─────────────────────────────────────────────────────────────────────────────
def test_approve_spawn_happy_path(temp_db):
    """Produces rows in all three tables: ``run_spawns`` (1 row),
    ``run_events`` (1 spawn.approved row with the literal
    ``ev-<sec>-<child_run_id>`` event_id), ``task_runs`` (1 UPSERT row)."""
    parent_id = "parent-d"
    _seed_parent(temp_db, parent_id)

    py.mo_recursive_approve_spawn(
        parent_id, "child-d", "code-fix", "/tmp/child-k.md",
        "/tmp/child-ws", 1, 0.4, 1,
    )

    # run_spawns: exactly 1 row
    py_spawns = _row_dicts(temp_db, "run_spawns")
    assert len(py_spawns) == 1, py_spawns
    sp = py_spawns[0]
    assert _event_id_stem(sp["spawn_id"]) == "sp-"
    assert sp["parent_run_id"] == parent_id
    assert sp["child_run_id"] == "child-d"
    assert sp["root_run_id"] == parent_id
    assert sp["depth"] == 1
    assert sp["recipe"] == "code-fix"
    assert sp["kickoff_path"] == "/tmp/child-k.md"
    assert sp["child_workspace"] == "/tmp/child-ws"
    assert abs(float(sp["authority_level"]) - 0.4) <= 1e-6
    assert sp["allow_child_spawn"] == 1
    assert sp["status"] == "approved"
    # policy snapshot is a JSON object with the canonical keys
    snap = json.loads(sp["policy_snapshot_json"])
    assert "max_depth" in snap

    # run_events: exactly 1 row (the spawn.approved row)
    py_events = _row_dicts(temp_db, "run_events")
    assert len(py_events) == 1, py_events
    assert py_events[0]["event_type"] == "spawn.approved"
    # event_id must use the literal ``ev-<sec>-<child_run_id>`` shape.
    assert py_events[0]["event_id"].startswith("ev-")
    assert "child-d" in py_events[0]["event_id"], (
        f"event_id missing child_run_id: {py_events[0]['event_id']!r}"
    )

    # task_runs: exactly 1 row (parent) + 1 UPSERT (child)
    py_trs = _row_dicts(temp_db, "task_runs")
    assert len(py_trs) == 2, f"expected 2 task_runs rows, got {py_trs}"
    py_child = next(r for r in py_trs if r["id"] == "child-d")
    # task_class is the recipe with dashes → underscores.
    assert py_child["task_class"] == "code_fix"
    assert py_child["kickoff_path"] == "/tmp/child-k.md"


# ─────────────────────────────────────────────────────────────────────────────
# (e) approve_spawn blocked by depth>max_depth — raises + writes 0 rows
# ─────────────────────────────────────────────────────────────────────────────
def test_approve_spawn_blocked_by_depth(temp_db):
    """Default ``max_depth=2``. depth=3 raises and writes zero rows in
    ``run_spawns``, ``run_events`` (the spawn.approved row), and
    ``task_runs`` (the UPSERT side effect)."""
    parent_id = "parent-e"
    _seed_parent(temp_db, parent_id)

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
    assert _row_dicts(temp_db, "run_spawns") == []
    py_events = _row_dicts(temp_db, "run_events")
    assert all(r["event_type"] != "spawn.approved" for r in py_events), py_events

    py_trs = {r["id"] for r in _row_dicts(temp_db, "task_runs")}
    assert "child-e" not in py_trs


# ─────────────────────────────────────────────────────────────────────────────
# (f) mark_spawn UPDATE row
# ─────────────────────────────────────────────────────────────────────────────
def test_mark_spawn_update(temp_db):
    """Seed a run_spawns row, then ``mo_recursive_mark_spawn <child>
    <status>`` flips its status."""
    _seed_spawn(temp_db, "sp-seed-f", "parent-f", "child-f")

    py.mo_recursive_mark_spawn("child-f", "running")

    py_rows = _row_dicts(temp_db, "run_spawns")
    assert len(py_rows) == 1
    assert py_rows[0]["status"] == "running"
    # updated_at refreshed to ~now (was 0 in the seed)
    assert py_rows[0]["updated_at"] > 0


def test_mark_spawn_invalid_status_raises(temp_db):
    """Invalid status raises and does not write anything."""
    _seed_spawn(temp_db, "sp-seed-f2", "parent-f2", "child-f2")

    raised = False
    try:
        py.mo_recursive_mark_spawn("child-f2", "BOGUS")
    except ValueError as exc:
        raised = True
        assert "invalid spawn status" in str(exc)
    assert raised
    # seed row untouched
    assert _row_dicts(temp_db, "run_spawns")[0]["status"] == "approved"


# ─────────────────────────────────────────────────────────────────────────────
# (g) record_artifact INSERT
# ─────────────────────────────────────────────────────────────────────────────
def test_record_artifact_insert(temp_db):
    """``mo_recursive_record_artifact`` INSERTs a ``run_artifact_edges``
    row with an ``ae-`` stem edge_id."""
    py.mo_recursive_record_artifact(
        "run-g-prod", "run-g-cons", "/tmp/art.md", "abc123", "file",
    )

    py_rows = _row_dicts(temp_db, "run_artifact_edges")
    assert len(py_rows) == 1
    row = py_rows[0]
    assert _event_id_stem(row["edge_id"]) == "ae-"
    assert row["producer_run_id"] == "run-g-prod"
    assert row["consumer_run_id"] == "run-g-cons"
    assert row["artifact_path"] == "/tmp/art.md"
    assert row["artifact_hash"] == "abc123"
    assert row["artifact_kind"] == "file"


# ─────────────────────────────────────────────────────────────────────────────
# (h) merge_decision accepted — merge_decisions + run_spawns.status='merged'
# ─────────────────────────────────────────────────────────────────────────────
def test_merge_decision_accepted(temp_db):
    """``mo_recursive_merge_decision <parent> <child> accepted <reason>``
    writes a ``merge_decisions`` row AND flips the seeded spawn row's
    status to ``merged``."""
    _seed_spawn(temp_db, "sp-seed-h", "parent-h", "child-h")

    py.mo_recursive_merge_decision("parent-h", "child-h", "accepted", "lgtm", "reviewer")

    # merge_decisions row
    py_decs = _row_dicts(temp_db, "merge_decisions")
    assert len(py_decs) == 1
    dec = py_decs[0]
    assert _event_id_stem(dec["decision_id"]) == "md-"
    assert dec["parent_run_id"] == "parent-h"
    assert dec["child_run_id"] == "child-h"
    assert dec["decision"] == "accepted"
    assert dec["reason"] == "lgtm"
    assert dec["decided_by"] == "reviewer"
    # evidence_json is the literal ``{"source": "mini-ork-spawn"}``.
    assert json.loads(dec["evidence_json"]) == {"source": "mini-ork-spawn"}

    # run_spawns.status flipped to 'merged'.
    py_spawn = _row_dicts(temp_db, "run_spawns")[0]
    assert py_spawn["status"] == "merged"
