"""E3 wiring test: the recover CLI acquires a single-writer lease and
registers an idempotent recovery request before dispatching (scenario 6,
end-to-end through ``recovery_planner.main``).

Reuses the E2 closure harness (linear A→B→C→D, D failed) and adds the E3
schema (run_leases + recovery_requests) so ``lease_tables_present`` is True
and the dispatch path engages the lease. A second ``main`` call — standing in
for a concurrent operator/worker — must see the live lease and return the
safe descriptive result WITHOUT a second dispatch (the node runs once).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.ported import recovery_planner as rp  # noqa: E402

# E2 schema (checkpoints + attempts + task_runs) + E3 schema (leases + requests).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('success','failure','skipped')),
    input_hash TEXT NOT NULL, recipe_version TEXT NOT NULL, config_hash TEXT NOT NULL,
    artifact_manifest_json TEXT NOT NULL, session_ref TEXT, failure_class TEXT,
    created_at INTEGER NOT NULL, PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL, node_type TEXT, started_at INTEGER NOT NULL,
    ended_at INTEGER NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success','failure','skipped','error')),
    failure_class TEXT, checkpoint_used INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced INTEGER NOT NULL DEFAULT 0, cost_usd REAL,
    provider_session_id TEXT, initiator TEXT
);
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY, status TEXT, cost_usd REAL DEFAULT 0,
    created_at INTEGER, updated_at INTEGER, ended_at INTEGER, duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS run_leases (
    run_id TEXT PRIMARY KEY, owner_token TEXT NOT NULL, acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL, renewed_at INTEGER NOT NULL,
    check (expires_at >= acquired_at)
);
CREATE TABLE IF NOT EXISTS recovery_requests (
    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, from_node TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','dispatched','completed','failed')),
    failure_class TEXT, budget_usd REAL NOT NULL DEFAULT 5.00,
    cost_usd REAL NOT NULL DEFAULT 0.0, dispatch_count INTEGER NOT NULL DEFAULT 0,
    owner_token TEXT, created_at INTEGER NOT NULL, last_dispatched_at INTEGER,
    closed_at INTEGER, payload_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_requests_idem
    ON recovery_requests(run_id, from_node, strategy);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()
    return str(p)


def _seed_success(db_path, run_id, node_id, recipe, tc, run_dir):
    import hashlib
    art = Path(run_dir) / f"{node_id}.md"
    art.write_bytes(f"out-{node_id}".encode())
    input_hash = hashlib.sha256(f"{run_id}|{node_id}|{recipe}".encode()).hexdigest()
    config_hash = hashlib.sha256(f"{tc}|{recipe}|{run_id}".encode()).hexdigest()
    manifest = [{"path": f"{node_id}.md",
                 "sha256": hashlib.sha256(art.read_bytes()).hexdigest(),
                 "bytes": os.path.getsize(art)}]
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT OR REPLACE INTO node_checkpoints(run_id,node_id,attempt,status,input_hash,"
        "recipe_version,config_hash,artifact_manifest_json,created_at) "
        "VALUES (?,?,1,'success',?,?,?,?,1000000)",
        (run_id, node_id, input_hash, recipe, config_hash, json.dumps(manifest)),
    )
    con.commit()
    con.close()


def _workflow(path: Path):
    import yaml
    data = {"version": "0.1.0", "task_class": "framework_edit",
            "nodes": [{"name": n, "type": "implementer", "model_lane": "minimax_lens"}
                      for n in ("A", "B", "C", "D")],
            "edges": [{"from": "A", "to": "B", "edge_type": "depends_on"},
                      {"from": "B", "to": "C", "edge_type": "depends_on"},
                      {"from": "C", "to": "D", "edge_type": "depends_on"}]}
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_recover_acquires_lease_and_is_idempotent(tmp_path, db_path, monkeypatch, capsys):
    run_id = "run-e3-wire-1"
    recipe = "framework-edit"
    tc = "framework_edit"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow = tmp_path / "wf.yaml"
    _workflow(workflow)
    for n in ("A", "B", "C"):     # D failed → closure = {D}
        _seed_success(db_path, run_id, n, recipe, tc, str(run_dir))

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RECIPE", recipe)
    for k in ("MINI_ORK_LEASE_TOKEN", "MINI_ORK_RECOVERY_REQUEST"):
        monkeypatch.delenv(k, raising=False)

    # ── first recovery: acquires the lease, registers + dispatches once ──
    rc1 = rp.main([run_id, "--strategy", "resume", "--workflow", str(workflow), "--db", db_path])
    assert rc1 == 0
    out1 = capsys.readouterr().out
    assert "dispatching closure" in out1

    con = sqlite3.connect(db_path)
    lease_owner = con.execute("SELECT owner_token FROM run_leases WHERE run_id=?", (run_id,)).fetchone()
    assert lease_owner is not None, "a lease row must exist after dispatch"
    reqs = con.execute(
        "SELECT status, dispatch_count, owner_token, from_node FROM recovery_requests WHERE run_id=?",
        (run_id,)).fetchall()
    con.close()
    assert len(reqs) == 1
    status, dcount, req_owner, from_node = reqs[0]
    assert status == "dispatched"
    assert dcount == 1
    assert req_owner == lease_owner[0]        # request fenced to the lease token
    assert from_node == "D"                   # closure entry
    assert os.environ.get("MINI_ORK_LEASE_TOKEN") == lease_owner[0]

    # ── second, concurrent recovery: lease still live → safe no-dispatch ──
    rc2 = rp.main([run_id, "--strategy", "resume", "--workflow", str(workflow), "--db", db_path])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "already being recovered" in out2
    assert "dispatching closure" not in out2

    con = sqlite3.connect(db_path)
    reqs2 = con.execute("SELECT COUNT(*), MAX(dispatch_count) FROM recovery_requests WHERE run_id=?",
                        (run_id,)).fetchone()
    con.close()
    assert reqs2[0] == 1, "no duplicate recovery request"
    assert reqs2[1] == 1, "the node was not dispatched a second time"


def test_recover_on_legacy_db_without_lease_tables_still_dispatches(tmp_path, monkeypatch, capsys):
    """A pre-0052 DB (no run_leases/recovery_requests) recovers fence-free —
    E3 wiring must not block a legacy consumer."""
    run_id = "run-legacy-1"
    recipe = "framework-edit"
    tc = "framework_edit"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow = tmp_path / "wf.yaml"
    _workflow(workflow)
    # DB with ONLY the E1/E2 tables (no lease tables)
    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(
        "\n".join(SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS run_leases")[0:1])  # up to E1/E2 only
    )
    con.commit()
    con.close()
    for n in ("A", "B", "C"):
        _seed_success(str(legacy), run_id, n, recipe, tc, str(run_dir))

    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RECIPE", recipe)
    monkeypatch.delenv("MINI_ORK_LEASE_TOKEN", raising=False)

    rc = rp.main([run_id, "--strategy", "resume", "--workflow", str(workflow), "--db", str(legacy)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dispatching closure" in out
    # no lease token emitted on a legacy DB
    assert "MINI_ORK_LEASE_TOKEN" not in os.environ
