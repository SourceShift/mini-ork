"""Tests for mini_ork.dispatch.run_artifacts — persist_artifact + resolve +
retention. Covers: row existence with correct bytes/sha256, rel_path
rejection of absolute / '..', no-op on old DB (no run_artifacts table),
round-trip resolution, gzip rewrites rel_path, prune preserves
evidence_bundle."""

from __future__ import annotations

import sqlite3
import time


from mini_ork.dispatch.retention import (
    DEFAULT_TTL_DAYS,
    gzip_run_stream,
    prune_old_trajectories,
)
from mini_ork.dispatch.telemetry import persist_artifact, resolve_artifact_abs

RUN_ARTIFACTS_SCHEMA = """
CREATE TABLE run_artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  node_id TEXT,
  call_id INTEGER,
  kind TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  bytes INTEGER,
  sha256 TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(run_id, node_id, kind, rel_path)
);
"""

OLD_SCHEMA = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model_id TEXT NOT NULL, tier TEXT NOT NULL,
  feature_name TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('success','failed')),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _db(tmp_path, schema):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(schema)
    con.commit()
    con.close()
    return p


def _write(path, content=b"hello world\n"):
    path.write_bytes(content)
    return path


def test_persist_artifact_writes_row_with_correct_hash(tmp_path):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    src = _write(run_dir / "agent-impl.stream.jsonl", b"event a\n" + b"event b\n")
    rowid = persist_artifact(
        db, run_id="run-1", node_id="impl", call_id=42,
        kind="turn_jsonl", rel_path="agent-impl.stream.jsonl", abs_path=src,
    )
    assert rowid is not None
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT run_id, node_id, call_id, kind, rel_path, bytes, sha256 "
        "FROM run_artifacts WHERE id=?",
        (rowid,),
    ).fetchone()
    con.close()
    assert row[0] == "run-1"
    assert row[1] == "impl"
    assert row[2] == 42
    assert row[3] == "turn_jsonl"
    assert row[4] == "agent-impl.stream.jsonl"
    assert row[5] == len(b"event a\nevent b\n")
    assert isinstance(row[6], str) and len(row[6]) == 64
    assert src.is_file() and src.stat().st_size > 0


def test_resolve_artifact_abs_round_trips(tmp_path, monkeypatch):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    src = _write(run_dir / "agent-impl.transcript.json", b'{"hello":1}')
    persist_artifact(
        db, run_id="run-2", node_id="impl", call_id=7,
        kind="transcript", rel_path="agent-impl.transcript.json", abs_path=src,
    )
    resolved = resolve_artifact_abs(
        str(db), "run-2", "impl", "transcript", run_dir=str(run_dir),
    )
    assert resolved is not None
    assert resolved.resolve() == src.resolve()
    assert resolved.is_file()


def test_reject_absolute_rel_path(tmp_path, capsys):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    src = _write(tmp_path / "x.jsonl")
    assert persist_artifact(
        db, run_id="run-3", node_id="n", call_id=1,
        kind="turn_jsonl", rel_path="/abs/path/x.jsonl", abs_path=src,
    ) is None
    assert persist_artifact(
        db, run_id="run-3", node_id="n", call_id=1,
        kind="turn_jsonl", rel_path="../escape/x.jsonl", abs_path=src,
    ) is None
    con = sqlite3.connect(db)
    count = con.execute("SELECT COUNT(*) FROM run_artifacts").fetchone()[0]
    con.close()
    assert count == 0


def test_noop_on_old_db_without_run_artifacts_table(tmp_path):
    db = _db(tmp_path, OLD_SCHEMA)
    src = _write(tmp_path / "agent-x.stream.jsonl")
    assert persist_artifact(
        db, run_id="run-4", node_id="x", call_id=1,
        kind="turn_jsonl", rel_path="agent-x.stream.jsonl", abs_path=src,
    ) is None


def test_noop_on_missing_abs_path(tmp_path):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    assert persist_artifact(
        db, run_id="run-5", node_id="x", call_id=1,
        kind="turn_jsonl",
        rel_path="agent-x.stream.jsonl",
        abs_path=tmp_path / "does-not-exist.jsonl",
    ) is None


def test_gzip_updates_rel_path_to_gz(tmp_path, monkeypatch):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    src = _write(run_dir / "agent-impl.stream.jsonl", b"line one\nline two\n")
    persist_artifact(
        db, run_id="run-6", node_id="impl", call_id=3,
        kind="turn_jsonl", rel_path="agent-impl.stream.jsonl", abs_path=src,
    )
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-6")  # real runs export this
    n = gzip_run_stream(run_dir)
    assert n == 1
    gz = run_dir / "agent-impl.stream.jsonl.gz"
    assert gz.is_file()
    con = sqlite3.connect(db)
    rel_path, size, sha = con.execute(
        "SELECT rel_path, bytes, sha256 FROM run_artifacts "
        "WHERE run_id=? AND kind='turn_jsonl'",
        ("run-6",),
    ).fetchone()
    con.close()
    assert rel_path == "agent-impl.stream.jsonl.gz"
    assert size == gz.stat().st_size
    assert isinstance(sha, str) and len(sha) == 64


def test_gzip_scoped_by_run_id(tmp_path, monkeypatch):
    """gzip_run_stream must rewrite ONLY its own run's row — rel_path is a bare
    basename shared across runs, so an unscoped UPDATE would clobber another
    run's same-basename row."""
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    run_dir = tmp_path / "run-me"
    run_dir.mkdir()
    src = _write(run_dir / "agent-impl.stream.jsonl", b"mine\n")
    persist_artifact(
        db, run_id="run-me", node_id="impl", call_id=1,
        kind="turn_jsonl", rel_path="agent-impl.stream.jsonl", abs_path=src,
    )
    # a DIFFERENT run's row, same basename, must survive untouched
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, bytes, sha256, created_at) "
        "VALUES ('run-other','impl','turn_jsonl','agent-impl.stream.jsonl',999,'othersha',1)"
    )
    con.commit(); con.close()
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "run-me")
    gzip_run_stream(run_dir)
    con = sqlite3.connect(db)
    mine = con.execute("SELECT rel_path FROM run_artifacts WHERE run_id='run-me'").fetchone()[0]
    other_rel, other_sha = con.execute(
        "SELECT rel_path, sha256 FROM run_artifacts WHERE run_id='run-other'"
    ).fetchone()
    con.close()
    assert mine == "agent-impl.stream.jsonl.gz"           # own row rewritten
    assert other_rel == "agent-impl.stream.jsonl"         # other run untouched
    assert other_sha == "othersha"


def test_prune_keeps_evidence_bundle(tmp_path):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    con = sqlite3.connect(db)
    now = int(time.time())
    ancient = now - (DEFAULT_TTL_DAYS + 60) * 86400
    con.execute(
        "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("run-old", "impl", "turn_jsonl", "agent-impl.stream.jsonl.gz", ancient),
    )
    con.execute(
        "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("run-old", "impl", "evidence_bundle", "agent-impl.evidence.json", ancient),
    )
    con.execute(
        "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("run-old", "impl", "transcript", "agent-impl.transcript.json", ancient),
    )
    con.commit()
    con.close()
    deleted = prune_old_trajectories(db, ttl_days=DEFAULT_TTL_DAYS)
    assert deleted == 1
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT kind FROM run_artifacts WHERE run_id='run-old' ORDER BY kind"
    ).fetchall()
    con.close()
    assert [r[0] for r in rows] == ["evidence_bundle", "transcript"]


def test_prune_removes_matching_gz_file(tmp_path, monkeypatch):
    db = _db(tmp_path, RUN_ARTIFACTS_SCHEMA)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    gz = _write(run_dir / "agent-x.stream.jsonl.gz", b"\x1f\x8b" + b"x" * 200)
    con = sqlite3.connect(db)
    now = int(time.time())
    con.execute(
        "INSERT INTO run_artifacts(run_id, node_id, kind, rel_path, bytes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("run-prune", "x", "turn_jsonl", gz.name, 200, now - 60 * 86400),
    )
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    deleted = prune_old_trajectories(db, ttl_days=DEFAULT_TTL_DAYS)
    assert deleted == 1
    assert not gz.exists()
    con = sqlite3.connect(db)
    remaining = con.execute("SELECT COUNT(*) FROM run_artifacts").fetchone()[0]
    con.close()
    assert remaining == 0


def test_prune_noop_on_old_db(tmp_path):
    db = _db(tmp_path, OLD_SCHEMA)
    assert prune_old_trajectories(db) == 0