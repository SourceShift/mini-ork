"""Unit tests for ``CheckpointRecord`` + the record call shape of
``write_checkpoint`` (M8 ISP refactor).

Pins the backward-compat contract:
  (a) CheckpointRecord defaults == the historical write_checkpoint
      keyword defaults (node_type="", cost_usd=None,
      provider_session_id="", initiator="python", failure_class=None,
      attempt=None, owner_token=None).
  (b) record path == kwargs path: both write byte-identical rows to
      node_checkpoints and node_attempts in a fresh tmp sqlite DB.
  (c) record + any explicit kwarg → ValueError (ambiguous source).
  (d) legacy kwargs path unchanged: the eight historically required
      keyword args still raise TypeError when omitted.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.stores.checkpoints import CheckpointRecord, write_checkpoint

# Canonical 0050 schema — same fixture copy as tests/test_node_checkpoints.py.
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
def run_dir(tmp_path: Path) -> str:
    d = tmp_path / "run"
    d.mkdir()
    (d / "out.md").write_bytes(b"# hello\n")
    return str(d)


def _fresh_db(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    con = sqlite3.connect(p)
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()
    return str(p)


def _rows(db: str) -> tuple[list, list]:
    con = sqlite3.connect(db)
    try:
        cp = con.execute(
            "SELECT run_id, node_id, attempt, status, input_hash,"
            " recipe_version, config_hash, artifact_manifest_json,"
            " session_ref, failure_class, created_at"
            " FROM node_checkpoints"
        ).fetchall()
        at = con.execute(
            "SELECT run_id, node_id, attempt_no, node_type, started_at,"
            " ended_at, result, failure_class, checkpoint_used,"
            " checkpoint_produced, cost_usd, provider_session_id, initiator"
            " FROM node_attempts"
        ).fetchall()
    finally:
        con.close()
    return cp, at


FULL_KWARGS = dict(
    status="success",
    input_hash="ih",
    recipe_version="rv1",
    config_hash="ch",
    artifact_paths=["out.md"],
    run_dir=None,  # filled per-test
    node_type="implementer",
    started_at=100,
    ended_at=200,
    cost_usd=0.42,
    provider_session_id="",
    initiator="python",
    failure_class=None,
    attempt=3,
)


def test_record_defaults_match_historical_signature():
    r = CheckpointRecord()
    # Fields that were required kwargs default to empty/None sentinels
    # (writer validation rejects them with rc=1, fail-closed).
    assert r.status == ""
    assert r.input_hash == ""
    assert r.recipe_version == ""
    assert r.config_hash == ""
    assert tuple(r.artifact_paths) == ()
    assert r.run_dir == ""
    assert r.started_at is None
    assert r.ended_at is None
    # Fields with real historical defaults keep them verbatim.
    assert r.node_type == ""
    assert r.cost_usd is None
    assert r.provider_session_id == ""
    assert r.initiator == "python"
    assert r.failure_class is None
    assert r.attempt is None
    assert r.owner_token is None


def test_record_is_frozen():
    r = CheckpointRecord()
    with pytest.raises(Exception):
        r.status = "success"  # type: ignore[misc]


def test_record_path_matches_kwargs_path(tmp_path: Path, run_dir: str):
    db_kwargs = _fresh_db(tmp_path, "kwargs.db")
    db_record = _fresh_db(tmp_path, "record.db")

    kw = dict(FULL_KWARGS, run_dir=run_dir)
    rc1 = write_checkpoint(db_kwargs, "run1", "n1", **kw)
    assert rc1 == 0

    rec = CheckpointRecord(**kw)
    rc2 = write_checkpoint(db_record, "run1", "n1", record=rec)
    assert rc2 == 0

    assert _rows(db_kwargs) == _rows(db_record)


def test_record_path_matches_kwargs_path_defaults_only(tmp_path: Path, run_dir: str):
    """Minimal call: only the historically required args, on both paths."""
    db_kwargs = _fresh_db(tmp_path, "kwargs_min.db")
    db_record = _fresh_db(tmp_path, "record_min.db")

    minimal = dict(
        status="failure",
        input_hash="ih",
        recipe_version="rv1",
        config_hash="ch",
        artifact_paths=["out.md"],
        run_dir=run_dir,
        started_at=100,
        ended_at=200,
        failure_class="timeout",
    )
    assert write_checkpoint(db_kwargs, "run1", "n1", **minimal) == 0
    assert write_checkpoint(db_record, "run1", "n1", record=CheckpointRecord(**minimal)) == 0
    assert _rows(db_kwargs) == _rows(db_record)


def test_record_plus_kwargs_raises_value_error(tmp_path: Path, run_dir: str):
    db = _fresh_db(tmp_path, "mix.db")
    rec = CheckpointRecord(status="success", input_hash="ih",
                           recipe_version="rv", config_hash="ch",
                           run_dir=run_dir, started_at=1, ended_at=2)
    with pytest.raises(ValueError):
        write_checkpoint(db, "run1", "n1", record=rec, status="success")
    with pytest.raises(ValueError):
        write_checkpoint(db, "run1", "n1", record=rec, attempt=2)
    # record=None is "no record" — pure kwargs path, no ValueError.
    with pytest.raises(TypeError):
        write_checkpoint(db, "run1", "n1", record=None, status="success")


def test_kwargs_path_still_requires_historical_required_args(tmp_path: Path):
    db = _fresh_db(tmp_path, "req.db")
    with pytest.raises(TypeError):
        write_checkpoint(db, "run1", "n1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        write_checkpoint(
            db, "run1", "n1",
            status="success", input_hash="ih", recipe_version="rv",
            config_hash="ch", artifact_paths=[], run_dir=".",
            # started_at / ended_at omitted — historically required
        )


def test_record_with_default_sentinels_fails_closed(tmp_path: Path):
    """A bare CheckpointRecord() fails the same writer validation a bad
    kwargs call would — rc=1, no rows, no exception."""
    db = _fresh_db(tmp_path, "bare.db")
    rc = write_checkpoint(db, "run1", "n1", record=CheckpointRecord())
    assert rc == 1
    assert _rows(db) == ([], [])


def test_owner_token_fencing_via_record(tmp_path: Path, run_dir: str):
    """record.owner_token flows into the E3 fence exactly like the kwarg
    did: an unknown token on a run with no lease is rejected with rc=2
    (fence fails closed when the holder check errors)."""
    db = _fresh_db(tmp_path, "fence.db")
    rec = CheckpointRecord(
        status="success", input_hash="ih", recipe_version="rv",
        config_hash="ch", artifact_paths=["out.md"], run_dir=run_dir,
        started_at=1, ended_at=2, owner_token="tok-stale",
    )
    rc = write_checkpoint(db, "run1", "n1", record=rec)
    assert rc == 2
    assert _rows(db) == ([], [])
