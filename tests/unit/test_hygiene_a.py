"""RB-35 / RB-42 regression tests — kickoff ``rbf-hygiene-a.md``.

RB-35: a ``verifier_results`` write survives a busy SQLite — bounded retry on
``database is locked``, then a JSONL spool under the run dir that the next
successful write in the same run flushes first.

RB-42: a node dispatch's timeout is ``min(node timeout, remaining run
deadline)`` when a ``--deadline`` is armed, never below 60 s, and the
remaining budget is exposed as ``remaining_seconds()``.

All cases are hermetic: the retry path is driven with an injected sleep and a
shrunken ``busy_timeout`` so exhaustion costs ~1 s instead of ~40 s.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import mini_ork.gates.verifier_rubric as verifier_rubric
from mini_ork.cli.execute_handlers import _cap_dispatch_timeout, remaining_seconds

PENDING_NAME = "verifier_results.pending.jsonl"

_DDL = """
CREATE TABLE IF NOT EXISTS verifier_rubrics (
    rubric_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT,
    task_class    TEXT,
    axes_json     TEXT,
    created_at    INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS verifier_results (
    result_id        TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    verifier_name    TEXT NOT NULL,
    rubric_id        TEXT,
    verdict          TEXT NOT NULL
                      CHECK(verdict IN ('pass', 'fail', 'indeterminate', 'vacuous')),
    confidence       REAL,
    scored_axes_json TEXT,
    created_at       INTEGER NOT NULL DEFAULT 0
);
"""


@pytest.fixture(autouse=True)
def _isolated_run_dir(tmp_path, monkeypatch):
    """Point run-dir resolution at the temp dir and scrub deadline env."""
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    for var in (
        "MINI_ORK_DB",
        "MINI_ORK_RUN_ID",
        "MO_DEADLINE_EPOCH",
        "MO_DEADLINE_SECONDS",
        "MO_DEADLINE_START",
        "MO_NODE_TIMEOUT_S",
        "MO_NODE_TIMEOUT_BASE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _make_db(tmp_path: Path) -> str:
    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    try:
        con.executescript(_DDL)
        con.commit()
    finally:
        con.close()
    return db


def _rows(db: str, run_id: str):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT verifier_name, verdict FROM verifier_results"
                " WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        ]
    finally:
        con.close()


@contextlib.contextmanager
def _exclusive_lock(db: str):
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA busy_timeout=0")
        con.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        try:
            con.rollback()
        finally:
            con.close()


def _fasten_retries(monkeypatch):
    """No-op the declared sleeps and shrink busy_timeout; keep the error class."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    orig_open = verifier_rubric._open

    def _open_fast(db_path):
        con = orig_open(db_path)
        try:
            con.execute("PRAGMA busy_timeout=150")
        except sqlite3.Error:
            pass
        return con

    monkeypatch.setattr(verifier_rubric, "_open", _open_fast)


def _arm_deadline(monkeypatch, remaining: int) -> None:
    now = int(time.time())
    monkeypatch.setenv("MO_DEADLINE_START", str(now - 2400))
    monkeypatch.setenv("MO_DEADLINE_SECONDS", "2700")
    monkeypatch.setenv("MO_DEADLINE_EPOCH", str(now + remaining))


def test_locked_two_seconds_row_lands_after_retries(tmp_path):
    """A DB locked by another connection for 2 s → the row lands."""
    db = _make_db(tmp_path)
    started = threading.Event()

    def _block_for_two_seconds():
        con = sqlite3.connect(db)
        con.execute("PRAGMA busy_timeout=0")
        con.execute("BEGIN EXCLUSIVE")
        started.set()
        time.sleep(2.0)
        con.rollback()
        con.close()

    blocker = threading.Thread(target=_block_for_two_seconds)
    blocker.start()
    assert started.wait(10), "blocker never acquired the EXCLUSIVE lock"
    try:
        t0 = time.monotonic()
        verifier_rubric.verifier_result_record(db, "rb35-2s", "spec-verifier", "pass")
        elapsed = time.monotonic() - t0
    finally:
        blocker.join(timeout=10)

    assert elapsed >= 1.5, f"write returned after {elapsed:.2f}s; it must wait out the lock"
    assert _rows(db, "rb35-2s") == [{"verifier_name": "spec-verifier", "verdict": "pass"}]
    assert not (tmp_path / PENDING_NAME).exists()


def test_exhausted_retries_spool_then_flush_on_next_write(tmp_path, monkeypatch):
    """Locked past the backoff → spooled; next write flushes it first."""
    _fasten_retries(monkeypatch)
    db = _make_db(tmp_path)

    with _exclusive_lock(db):
        verifier_rubric.verifier_result_record(db, "rb35-spool", "verifier-a", "pass")

    pending = tmp_path / PENDING_NAME
    assert pending.is_file(), "the exhausted write must spool, not lose, the row"
    lines = [ln for ln in pending.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines and json.loads(lines[-1])["verdict"] == "pass"
    assert _rows(db, "rb35-spool") == []

    # Lock released: the next successful write flushes the spooled row first.
    verifier_rubric.verifier_result_record(db, "rb35-spool", "verifier-b", "fail")
    assert _rows(db, "rb35-spool") == [
        {"verifier_name": "verifier-a", "verdict": "pass"},
        {"verifier_name": "verifier-b", "verdict": "fail"},
    ]
    assert (not pending.exists()) or pending.read_text(encoding="utf-8").strip() == ""


def test_non_locked_errors_still_raise_and_do_not_spool(tmp_path, monkeypatch):
    """Only ``database is locked`` is retried/spooled; a CHECK violation fails."""
    _fasten_retries(monkeypatch)
    db = _make_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        verifier_rubric.verifier_result_record(db, "rb35-bad", "verifier", "bogus")
    assert not (tmp_path / PENDING_NAME).exists()


def test_remaining_seconds_reads_the_armed_budget(tmp_path, monkeypatch):
    _arm_deadline(monkeypatch, 300)
    assert 295 <= remaining_seconds("rb42") <= 302


def test_dispatch_timeout_capped_to_remaining_deadline(tmp_path, monkeypatch):
    _arm_deadline(monkeypatch, 300)
    assert 295 <= _cap_dispatch_timeout(3000, 300) <= 302
    assert 295 <= _cap_dispatch_timeout(3000) <= 302  # armed env, no explicit remaining


def test_dispatch_timeout_floor_is_sixty_seconds(tmp_path, monkeypatch):
    _arm_deadline(monkeypatch, 10)
    assert _cap_dispatch_timeout(3000, 10) == 60
    assert _cap_dispatch_timeout(3000) == 60


def test_dispatch_timeout_uncapped_without_a_deadline(tmp_path, monkeypatch):
    assert _cap_dispatch_timeout(3000) == 3000
    assert _cap_dispatch_timeout(90) == 90
