"""Unit tests for the durable-DAG checkpoint writer + validity check.

Covers the E1 contract from
``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md``:
crash-safe publication (write artifacts → fsync → verify hashes → commit
the row in one SQLite transaction), and fail-closed validity (``is_node_reusable``
returns False on any mismatch, never raises).

Each test seeds a fresh tmp DB with the canonical 0050 schema and a
fresh tmp run dir; nothing is shared across tests. The conftest fixture
in ``tests/conftest.py`` auto-isolates ``os.environ`` so the test does
not leak ``MINI_ORK_DB``/``MINI_ORK_RUN_DIR`` into siblings.

Cases:
  (a)  write_checkpoint happy path            → row + attempt + manifest
  (b)  write_checkpoint missing db            → rc=1, no crash
  (c)  write_checkpoint with empty artifact list → row commits with empty manifest
  (d)  write_checkpoint with non-existent artifact path → silently omitted
  (e)  write_checkpoint with absolute artifact path → resolved to rel under run_dir
  (f)  write_checkpoint with parent-rel artifact path → rejected (path safety)
  (g)  is_node_reusable happy path            → True on matching hashes + intact files
  (h)  is_node_reusable: missing row          → False
  (i)  is_node_reusable: missing artifact     → False
  (j)  is_node_reusable: mutated bytes        → False (sha256 mismatch)
  (k)  is_node_reusable: input_hash changed   → False
  (l)  is_node_reusable: recipe_version changed → False
  (m)  is_node_reusable: config_hash changed  → False
  (n)  is_node_reusable: manifest corruption  → False (json error)
  (o)  crash window 1: artifacts on disk, no row → is_node_reusable == False
  (p)  crash window 2: row exists, artifact corrupt → is_node_reusable == False
  (q)  is_node_reusable: success with empty manifest → True (reusable)
  (r)  is_node_reusable: failure status row   → False (only success reuses)
  (s)  legacy runs read unchanged: a DB with NO node_checkpoints table
       loads through a SELECT that returns no rows (regression on
       additive migration: SELECT * FROM node_checkpoints must not
       raise on legacy DBs)
  (t)  path-safety: is_node_reusable rejects manifest entries with
       absolute or parent-relative paths (the run_artifacts 0047 convention)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.stores import checkpoints as mc

# Canonical 0050 schema — copy of db/migrations/0050_node_dag_checkpoints.sql
# (the test mirrors it so it does not depend on the migrate.py harness /
# a fully-initialized state.db). If the migration changes, this fixture
# must change too — that is intentional (the test pins the contract).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT    NOT NULL CHECK (status IN ('success','failure','skipped')),
    input_hash            TEXT    NOT NULL,
    recipe_version        TEXT    NOT NULL,
    config_hash           TEXT    NOT NULL,
    artifact_manifest_json TEXT   NOT NULL,
    session_ref           TEXT,
    failure_class         TEXT,
    created_at            INTEGER NOT NULL,
    PRIMARY KEY (run_id, node_id)
);

CREATE TABLE IF NOT EXISTS node_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt_no            INTEGER NOT NULL,
    node_type             TEXT,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER NOT NULL,
    result                TEXT    NOT NULL CHECK (result IN ('success','failure','skipped','error')),
    failure_class         TEXT,
    checkpoint_used       INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced   INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL,
    provider_session_id   TEXT,
    initiator             TEXT
);
"""


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Fresh tmp DB with the 0050 schema applied."""
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()
    return str(p)


@pytest.fixture
def run_dir(tmp_path: Path) -> str:
    """Fresh tmp run dir (where artifacts live)."""
    d = tmp_path / "run"
    d.mkdir()
    return str(d)


def _seed_artifact(run_dir: str, rel: str, body: bytes) -> str:
    p = Path(run_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return rel


def _good_hashes(run_id: str, node_id: str, recipe: str = "test-recipe") -> tuple[str, str, str]:
    return (
        hashlib.sha256(f"{run_id}|{node_id}|{recipe}".encode()).hexdigest(),
        recipe,
        hashlib.sha256(f"tc|{recipe}|{run_id}".encode()).hexdigest(),
    )


# ── (a) happy path ────────────────────────────────────────────────────────
def test_checkpoint_happy_path_commits_row_and_attempt(db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")

    rc = mc.write_checkpoint(
        db_path, "r1", "impl",
        status="success", input_hash=input_hash, recipe_version=recipe_version,
        config_hash=config_hash, artifact_paths=["out.md"], run_dir=run_dir,
        node_type="implementer", started_at=1000, ended_at=2000,
    )
    assert rc == 0, f"write_checkpoint returned {rc}"

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT status, input_hash, recipe_version, config_hash, "
            "artifact_manifest_json FROM node_checkpoints WHERE run_id=? AND node_id=?",
            ("r1", "impl"),
        ).fetchone()
        assert row is not None
        assert row[0] == "success"
        assert row[1] == input_hash
        assert row[2] == recipe_version
        assert row[3] == config_hash
        manifest = json.loads(row[4])
        assert len(manifest) == 1
        assert manifest[0]["path"] == "out.md"
        assert manifest[0]["sha256"] == hashlib.sha256(b"hello").hexdigest()
        assert manifest[0]["bytes"] == 5

        attempts = con.execute(
            "SELECT attempt_no, result, checkpoint_produced, initiator "
            "FROM node_attempts WHERE run_id=? AND node_id=?",
            ("r1", "impl"),
        ).fetchall()
        assert len(attempts) == 1
        assert attempts[0] == (1, "success", 1, "python")
    finally:
        con.close()


# ── (b) missing db ────────────────────────────────────────────────────────
def test_checkpoint_missing_db_returns_nonzero(tmp_path):
    rc = mc.write_checkpoint(
        str(tmp_path / "nope.db"), "r", "n",
        status="success", input_hash="x" * 64, recipe_version="v",
        config_hash="y" * 64, artifact_paths=[], run_dir=str(tmp_path),
        node_type="", started_at=0, ended_at=0,
    )
    assert rc != 0


# ── (c) empty artifact list commits a row with empty manifest ────────────
def test_checkpoint_empty_manifest_still_commits(db_path, run_dir):
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    rc = mc.write_checkpoint(
        db_path, "r", "n", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=[], run_dir=run_dir, node_type="planner",
        started_at=0, ended_at=0,
    )
    assert rc == 0
    con = sqlite3.connect(db_path)
    try:
        manifest = con.execute(
            "SELECT artifact_manifest_json FROM node_checkpoints").fetchone()[0]
        assert json.loads(manifest) == []
    finally:
        con.close()


# ── (d) non-existent artifact path is silently omitted ───────────────────
def test_checkpoint_missing_artifact_path_omitted(db_path, run_dir):
    _seed_artifact(run_dir, "present.md", b"x")
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    rc = mc.write_checkpoint(
        db_path, "r", "n", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["present.md", "absent.md"], run_dir=run_dir,
        node_type="", started_at=0, ended_at=0,
    )
    assert rc == 0
    con = sqlite3.connect(db_path)
    try:
        manifest = json.loads(con.execute(
            "SELECT artifact_manifest_json FROM node_checkpoints").fetchone()[0])
        assert [m["path"] for m in manifest] == ["present.md"]
    finally:
        con.close()


# ── (e) absolute path resolved to rel under run_dir ──────────────────────
def test_checkpoint_absolute_path_normalized_to_rel(db_path, run_dir):
    _seed_artifact(run_dir, "rel.md", b"x")
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    abs_path = os.path.join(run_dir, "rel.md")
    rc = mc.write_checkpoint(
        db_path, "r", "n", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=[abs_path], run_dir=run_dir,
        node_type="", started_at=0, ended_at=0,
    )
    assert rc == 0
    con = sqlite3.connect(db_path)
    try:
        manifest = json.loads(con.execute(
            "SELECT artifact_manifest_json FROM node_checkpoints").fetchone()[0])
        assert manifest[0]["path"] == "rel.md"
    finally:
        con.close()


# ── (f) parent-relative path rejected ────────────────────────────────────
def test_checkpoint_parent_relative_path_rejected(db_path, run_dir, tmp_path):
    sibling = tmp_path / "outside.md"
    sibling.write_bytes(b"x")
    # Use a path that traverses above run_dir.
    bad = os.path.relpath(str(sibling), run_dir)  # "../outside.md" if sibling is sibling
    # Force the parent-relative form
    if not bad.startswith(".."):
        bad = "../outside.md"
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    rc = mc.write_checkpoint(
        db_path, "r", "n", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=[bad], run_dir=run_dir,
        node_type="", started_at=0, ended_at=0,
    )
    # The row still commits (writer is best-effort), but the manifest omits
    # the unsafe path. is_node_reusable will then see an empty manifest and
    # treat it as success-with-no-artifacts → reusable.
    assert rc == 0
    con = sqlite3.connect(db_path)
    try:
        manifest = json.loads(con.execute(
            "SELECT artifact_manifest_json FROM node_checkpoints").fetchone()[0])
        assert all("/" not in m["path"].lstrip(".") or ".." not in m["path"]
                   for m in manifest)
    finally:
        con.close()


# ── (g) is_node_reusable happy path ──────────────────────────────────────
def test_reusable_happy_path(db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    assert mc.write_checkpoint(
        db_path, "r1", "impl", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["out.md"], run_dir=run_dir, node_type="implementer",
        started_at=0, ended_at=0,
    ) == 0
    assert mc.is_node_reusable(
        db_path, "r1", "impl",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is True


# ── (h) missing row → False ──────────────────────────────────────────────
def test_reusable_missing_row(db_path, run_dir):
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    assert mc.is_node_reusable(
        db_path, "r", "n",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (i) missing artifact → False ────────────────────────────────────────
def test_reusable_missing_artifact(db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    mc.write_checkpoint(
        db_path, "r1", "impl", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["out.md"], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    (Path(run_dir) / "out.md").unlink()
    assert mc.is_node_reusable(
        db_path, "r1", "impl",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (j) mutated bytes → False ───────────────────────────────────────────
def test_reusable_mutated_bytes(db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    mc.write_checkpoint(
        db_path, "r1", "impl", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["out.md"], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    (Path(run_dir) / "out.md").write_bytes(b"hello world")
    assert mc.is_node_reusable(
        db_path, "r1", "impl",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (k/l/m) hash mismatches → False ─────────────────────────────────────
@pytest.mark.parametrize("field,new_value", [
    ("input_hash", "f" * 64),
    ("recipe_version", "different-recipe"),
    ("config_hash", "0" * 64),
])
def test_reusable_hash_mismatch(field, new_value, db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    mc.write_checkpoint(
        db_path, "r1", "impl", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["out.md"], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    kwargs = {
        "db": db_path, "run_id": "r1", "node_id": "impl",
        "current_input_hash": input_hash,
        "current_recipe_version": recipe_version,
        "current_config_hash": config_hash,
        "run_dir": run_dir,
    }
    kwargs[f"current_{field}"] = new_value
    assert mc.is_node_reusable(**kwargs) is False


# ── (n) manifest corruption → False ─────────────────────────────────────
def test_reusable_manifest_corruption(db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    mc.write_checkpoint(
        db_path, "r1", "impl", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["out.md"], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    # Corrupt the manifest JSON
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE node_checkpoints SET artifact_manifest_json='{not json' "
            "WHERE run_id='r1' AND node_id='impl'")
        con.commit()
    finally:
        con.close()
    assert mc.is_node_reusable(
        db_path, "r1", "impl",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (o) crash window 1: artifacts exist, no row ─────────────────────────
def test_crash_window1_artifacts_no_row(db_path, run_dir):
    # Simulate a crash AFTER the artifact was written but BEFORE the row
    # committed: artifact is on disk, no node_checkpoints row. The runtime
    # must treat this as "not resumable, rerun".
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    assert mc.is_node_reusable(
        db_path, "r1", "impl",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (p) crash window 2: row exists, artifact corrupt ────────────────────
def test_crash_window2_row_exists_artifact_corrupt(db_path, run_dir):
    _seed_artifact(run_dir, "out.md", b"hello")
    input_hash, recipe_version, config_hash = _good_hashes("r1", "impl")
    mc.write_checkpoint(
        db_path, "r1", "impl", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=["out.md"], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    # Simulate a crash AFTER the row committed but where the artifact
    # bytes were later partially-written (truncated). is_node_reusable
    # must fail-closed on the sha256 mismatch.
    (Path(run_dir) / "out.md").write_bytes(b"h")
    assert mc.is_node_reusable(
        db_path, "r1", "impl",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (q) success with empty manifest → reusable ──────────────────────────
def test_reusable_empty_manifest_is_reusable(db_path, run_dir):
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    mc.write_checkpoint(
        db_path, "r", "n", status="success", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=[], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    assert mc.is_node_reusable(
        db_path, "r", "n",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is True


# ── (r) failure status → not reusable ───────────────────────────────────
def test_reusable_failure_status_not_reusable(db_path, run_dir):
    input_hash, recipe_version, config_hash = _good_hashes("r", "n")
    mc.write_checkpoint(
        db_path, "r", "n", status="failure", input_hash=input_hash,
        recipe_version=recipe_version, config_hash=config_hash,
        artifact_paths=[], run_dir=run_dir, node_type="",
        started_at=0, ended_at=0,
    )
    assert mc.is_node_reusable(
        db_path, "r", "n",
        current_input_hash=input_hash, current_recipe_version=recipe_version,
        current_config_hash=config_hash, run_dir=run_dir,
    ) is False


# ── (s) legacy DB without node_checkpoints reads cleanly ────────────────
def test_legacy_db_reads_unchanged(tmp_path):
    # A DB with NO node_checkpoints / node_attempts table. The additive
    # migration must not break any existing SELECT against it.
    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, x TEXT)")
    con.execute("INSERT INTO llm_calls(x) VALUES ('ok')")
    con.commit()
    con.close()
    con = sqlite3.connect(legacy)
    try:
        rows = con.execute("SELECT * FROM llm_calls").fetchall()
        assert rows == [(1, "ok")]
    finally:
        con.close()
    # And: is_node_reusable on a legacy DB returns False (no row, not an
    # error). This is the legacy-run "fully complete" semantics from §10.
    assert mc.is_node_reusable(
        str(legacy), "r", "n",
        current_input_hash="a" * 64, current_recipe_version="v",
        current_config_hash="b" * 64, run_dir=str(tmp_path),
    ) is False


# ── (t) is_node_reusable rejects unsafe manifest paths ───────────────────
def test_reusable_rejects_unsafe_manifest_path(db_path, run_dir):
    # Write a row whose manifest contains an absolute path. is_node_reusable
    # must return False — the run_artifacts 0047 convention.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO node_checkpoints(run_id, node_id, attempt, status,
                input_hash, recipe_version, config_hash,
                artifact_manifest_json, created_at)
            VALUES (?, ?, 1, 'success', ?, 'v', ?, ?, 0)
            """,
            ("r", "n", "a" * 64, "0" * 64,
             json.dumps([{"path": "/etc/passwd", "sha256": "x", "bytes": 0}])),
        )
        con.commit()
    finally:
        con.close()
    assert mc.is_node_reusable(
        db_path, "r", "n",
        current_input_hash="a" * 64, current_recipe_version="v",
        current_config_hash="0" * 64, run_dir=run_dir,
    ) is False
