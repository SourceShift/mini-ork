"""Tests for the run_artifacts read surface (roadmap Step 2, A2).

Covers ArtifactsRepository + the routes in mini_ork/web/routes/artifacts.py:
list endpoint fields + ordering, ?kind= filter, raw byte serving, path-escape
rejection (403), missing row/file (404), and the old-db no-op (empty list,
never a 500). The tmp db uses the REAL migration 0047 schema; the app is
booted via create_app against a tmp .mini-ork home (TestClient).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# TestClient needs an httpx backend: newer starlette hard-requires httpx2,
# older starlette works with httpx (1). CI installs httpx2; local zero-dep
# runs may have neither — skip cleanly instead of erroring at collection.
try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as _exc:  # RuntimeError: starlette>=httpx2-only
    TestClient = None
    pytestmark = pytest.mark.skip(
        reason=f"starlette TestClient unavailable (needs httpx2 or httpx): {_exc}")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

MIGRATION = REPO / "db/migrations/0047_run_artifacts.sql"

STREAM_BYTES = b'{"turn":0}\n{"turn":1}\n'
TRANSCRIPT_BYTES = b'{"turns":[]}'


def _seed_home(tmp_path: Path, *, with_table: bool = True) -> tuple[Path, dict[str, int]]:
    """Build a tmp .mini-ork home: state.db (+run_artifacts) + runs/run-1 dir.

    Returns (home, ids) where ids maps a label to the run_artifacts rowid.
    """
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "agent-impl.stream.jsonl").write_bytes(STREAM_BYTES)
    (run_dir / "agent-reviewer.transcript.json").write_bytes(TRANSCRIPT_BYTES)
    # A real file OUTSIDE the run dir, targeted by the poisoned escape row.
    (home / "runs" / "escape.txt").write_bytes(b"escaped\n")

    db_path = home / "state.db"
    con = sqlite3.connect(db_path)
    ids: dict[str, int] = {}
    if with_table:
        con.execute(
            "CREATE TABLE schema_migrations(filename TEXT PRIMARY KEY,"
            " applied_at TEXT, checksum TEXT)"
        )
        con.executescript(MIGRATION.read_text(encoding="utf-8"))
        rows = [
            ("run-1", "impl", 42, "turn_jsonl", "agent-impl.stream.jsonl",
             len(STREAM_BYTES), "a" * 64, 100),
            ("run-1", "reviewer", None, "transcript", "agent-reviewer.transcript.json",
             len(TRANSCRIPT_BYTES), "b" * 64, 200),
            # Poisoned legacy row: rel_path escapes the run dir. persist_artifact
            # rejects these at write time; this simulates a hand-written row.
            ("run-1", "impl", None, "escape", "../escape.txt", 8, "c" * 64, 300),
            # Another run's row — must never leak into run-1's surface.
            ("run-2", "impl", None, "turn_jsonl", "agent-impl.stream.jsonl",
             5, "d" * 64, 400),
        ]
        for label, row in zip(("stream", "transcript", "escape", "other_run"), rows):
            cur = con.execute(
                "INSERT INTO run_artifacts (run_id, node_id, call_id, kind, rel_path,"
                " bytes, sha256, created_at) VALUES (?,?,?,?,?,?,?,?)",
                row,
            )
            ids[label] = int(cur.lastrowid)
    con.commit()
    con.close()
    return home, ids


@pytest.fixture
def client(tmp_path: Path):
    from mini_ork.web.app import create_app

    home, ids = _seed_home(tmp_path)
    app = create_app(home=home, dev_cors=False)
    return TestClient(app), ids


def test_list_returns_rows_with_correct_fields(client) -> None:
    c, ids = client
    resp = c.get("/api/v1/task-runs/run-1/artifact-records")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [ids["stream"], ids["transcript"], ids["escape"]]
    first = rows[0]
    assert {
        "id", "run_id", "node_id", "call_id", "kind",
        "rel_path", "bytes", "sha256", "created_at",
    } <= set(first.keys())
    assert first["run_id"] == "run-1"
    assert first["node_id"] == "impl"
    assert first["call_id"] == 42
    assert first["kind"] == "turn_jsonl"
    assert first["rel_path"] == "agent-impl.stream.jsonl"
    assert first["bytes"] == len(STREAM_BYTES)
    assert first["sha256"] == "a" * 64
    # ordered by created_at ascending
    assert [r["created_at"] for r in rows] == [100, 200, 300]
    # another run's rows never leak in
    assert all(r["run_id"] == "run-1" for r in rows)


def test_list_kind_filter(client) -> None:
    c, ids = client
    resp = c.get("/api/v1/task-runs/run-1/artifact-records", params={"kind": "transcript"})
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [ids["transcript"]]
    assert rows[0]["kind"] == "transcript"


def test_raw_serves_file_bytes(client) -> None:
    c, ids = client
    resp = c.get(f"/api/v1/task-runs/run-1/artifact-records/{ids['stream']}/raw")
    assert resp.status_code == 200
    assert resp.content == STREAM_BYTES


def test_raw_path_escape_row_rejected(client) -> None:
    c, ids = client
    resp = c.get(f"/api/v1/task-runs/run-1/artifact-records/{ids['escape']}/raw")
    assert resp.status_code == 403


def test_raw_missing_row_is_404(client) -> None:
    c, ids = client
    assert c.get("/api/v1/task-runs/run-1/artifact-records/99999/raw").status_code == 404
    # run scoping: run-2's row is invisible under run-1
    assert (
        c.get(f"/api/v1/task-runs/run-1/artifact-records/{ids['other_run']}/raw").status_code
        == 404
    )


def test_old_db_without_table_returns_empty_list_not_500(tmp_path: Path) -> None:
    from mini_ork.web.app import create_app

    home, _ids = _seed_home(tmp_path, with_table=False)
    c = TestClient(create_app(home=home, dev_cors=False))
    resp = c.get("/api/v1/task-runs/run-1/artifact-records")
    assert resp.status_code == 200
    assert resp.json() == []
    assert c.get("/api/v1/task-runs/run-1/artifact-records/1/raw").status_code == 404


def test_repository_direct(tmp_path: Path) -> None:
    """Repository-level: has_table guard + kind filter without HTTP."""
    from mini_ork.web.db import StateDB
    from mini_ork.web.repositories import ArtifactsRepository

    home, ids = _seed_home(tmp_path)
    repo = ArtifactsRepository(StateDB(home / "state.db"))
    rows = repo.list_artifacts("run-1")
    assert [r["id"] for r in rows] == [ids["stream"], ids["transcript"], ids["escape"]]
    assert [r["kind"] for r in repo.list_artifacts("run-1", kind="escape")] == ["escape"]
    assert repo.list_artifacts("no-such-run") == []
    assert repo.fetch_artifact("run-1", ids["stream"])["rel_path"] == (
        "agent-impl.stream.jsonl"
    )
    assert repo.fetch_artifact("run-1", ids["other_run"]) is None  # run-scoped

    old_home, _ = _seed_home(tmp_path / "old", with_table=False)
    old_repo = ArtifactsRepository(StateDB(old_home / "state.db"))
    assert old_repo.list_artifacts("run-1") == []
    assert old_repo.fetch_artifact("run-1", 1) is None
