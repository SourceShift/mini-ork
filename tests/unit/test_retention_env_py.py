"""Unit tests for trajectory retention env wiring (roadmap Step 2 / A2)."""
import sqlite3
import time

from mini_ork.dispatch import retention

_DDL = """
CREATE TABLE run_artifacts (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  node_id TEXT,
  call_id TEXT,
  kind TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(run_id,node_id,kind,rel_path)
);
"""


def _seed(db, age_days, kind="turn_jsonl"):
    con = sqlite3.connect(str(db))
    con.execute(_DDL)
    con.execute(
        "INSERT INTO run_artifacts (run_id,node_id,kind,rel_path,bytes,created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("r1", "n1", kind, f"agent-n1.{kind}.jsonl.gz", 10,
         int(time.time()) - age_days * 86400))
    con.commit()
    con.close()


def _count(db):
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM run_artifacts").fetchone()[0]
    con.close()
    return n


def test_prune_from_env_respects_ttl(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _seed(db, age_days=45)
    monkeypatch.setenv("MO_TRAJECTORY_TTL_DAYS", "30")
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    assert retention.prune_from_env(db) == 1
    assert _count(db) == 0


def test_prune_from_env_keeps_fresh_rows(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _seed(db, age_days=3)
    monkeypatch.setenv("MO_TRAJECTORY_TTL_DAYS", "30")
    assert retention.prune_from_env(db) == 0
    assert _count(db) == 1


def test_prune_from_env_zero_disables(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _seed(db, age_days=365)
    monkeypatch.setenv("MO_TRAJECTORY_TTL_DAYS", "0")
    assert retention.prune_from_env(db) == 0
    assert _count(db) == 1


def test_prune_from_env_missing_table_noop(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setenv("MO_TRAJECTORY_TTL_DAYS", "30")
    assert retention.prune_from_env(db) == 0


def test_prune_from_env_db_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setenv("MO_TRAJECTORY_TTL_DAYS", "30")
    # MINI_ORK_HOME is the .mini-ork dir itself (RunContext contract).
    db = tmp_path / "state.db"
    _seed(db, age_days=45)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    assert retention.prune_from_env() == 1
