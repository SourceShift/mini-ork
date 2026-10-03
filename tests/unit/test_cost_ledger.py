"""Hermetic tests for ``mini_ork.cost_ledger.spent_last_24h``.

The ledger reader is the one definition of "rolling-24h USD spend" the budget
guards read. These tests build temp sqlite files with the minimal ``llm_calls``
and ``task_runs`` columns the reader touches, then verify the fallback ladder
(llm_calls → task_runs → 0.0) and the rolling-window predicate.

Timestamps are now-offset, never absolute: a literal-date test reds out once
that date ages past 24h.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork import cost_ledger  # noqa: E402


def _init_db(path: Path, *, with_llm_calls: bool = True, with_task_runs: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        if with_llm_calls:
            con.execute(
                "CREATE TABLE llm_calls("
                "id INTEGER PRIMARY KEY,"
                "run_id TEXT,"
                "cost_usd REAL,"
                "ts TEXT"
                ")"
            )
        if with_task_runs:
            con.execute(
                "CREATE TABLE task_runs("
                "id TEXT,"
                "cost_usd REAL,"
                "created_at INTEGER"
                ")"
            )
        con.commit()
    finally:
        con.close()
    return path


def _insert_llm_call(db: Path, *, run_id: str, cost: float, ts: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO llm_calls (id, run_id, cost_usd, ts) VALUES (?, ?, ?, ?)",
            (gen_id(), run_id, cost, ts),
        )
        con.commit()
    finally:
        con.close()


def _insert_task_run(db: Path, *, cost: float, created_at: int) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (id, cost_usd, created_at) VALUES (?, ?, ?)",
            (f"tr-{created_at}-{cost}", cost, created_at),
        )
        con.commit()
    finally:
        con.close()


def _iso(secs: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))


_ID_COUNTER = [0]


def gen_id() -> int:
    _ID_COUNTER[0] += 1
    return _ID_COUNTER[0]


# ── ledger-vs-task_runs divergence (the root bug) ──────────────────────────────────


def test_ledger_includes_stage_rows_that_task_runs_misses(tmp_path: Path) -> None:
    """Measured on docs run 1791012731: task_runs said $0.39, llm_calls summed
    $1.04 across the profile answerer, worker, and rubric judge. The ledger
    must read the larger figure (1.04), not the smaller (0.39)."""
    db = _init_db(tmp_path / "ledger-stage")
    now = time.time()
    # Stage rows: only llm_calls has them; task_runs does not.
    _insert_llm_call(db, run_id="r1", cost=0.26, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=0.39, ts=_iso(now - 1200))
    _insert_llm_call(db, run_id="r1", cost=0.39, ts=_iso(now - 1800))
    # What task_runs reports (the old under-counting meter):
    _insert_task_run(db, cost=0.39, created_at=int(now - 1200))

    assert cost_ledger.spent_last_24h(db) == pytest.approx(1.04)


# ── rolling-window predicate ──────────────────────────────────────────────────────


def test_rows_older_than_24h_are_excluded(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "ledger-window")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=0.50, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=99.0, ts=_iso(now - 30 * 3600))

    assert cost_ledger.spent_last_24h(db) == pytest.approx(0.50)


# ── fallback ladder ───────────────────────────────────────────────────────────────


def test_falls_back_to_task_runs_when_llm_calls_missing(tmp_path: Path) -> None:
    """A DB behind the migrator degrades to the old estimate, not a blind zero."""
    db = _init_db(tmp_path / "ledger-fallback", with_llm_calls=False)
    now = int(time.time())
    _insert_task_run(db, cost=1.25, created_at=now - 3600)

    assert cost_ledger.spent_last_24h(db) == pytest.approx(1.25)


def test_no_db_path_returns_zero() -> None:
    assert cost_ledger.spent_last_24h(None) == 0.0
    assert cost_ledger.spent_last_24h("") == 0.0


def test_missing_file_returns_zero(tmp_path: Path) -> None:
    assert cost_ledger.spent_last_24h(tmp_path / "does-not-exist.db") == 0.0


def test_garbage_file_returns_zero(tmp_path: Path) -> None:
    """A non-SQLite file at the path yields 0.0, not a crash."""
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database at all")
    assert cost_ledger.spent_last_24h(garbage) == 0.0


# ── budget guards see the ledger sum (not task_runs) ──────────────────────────────


def test_cost_circuit_open_reads_ledger_sum(tmp_path: Path) -> None:
    """A db whose task_runs is under budget but whose llm_calls is over budget
    trips the guard — exactly the run-1791012731 bug class."""
    from mini_ork.dispatch.llm_dispatch import cost_circuit_open

    db = _init_db(tmp_path / "circuit-trip")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=12.5, ts=_iso(now - 600))
    # task_runs reports $0 — old meter would NOT trip.

    assert cost_circuit_open(str(db), "10") is True
    assert cost_circuit_open(str(db), "20") is False


def test_pre_iter_cost_check_reads_ledger_sum(tmp_path: Path) -> None:
    """pre_iter_cost_check mirrors the circuit. llm_calls over budget trips it."""
    from mini_ork.cli import self_improve

    db = _init_db(tmp_path / "pre-iter")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=7.0, ts=_iso(now - 600))

    # Env switch default is "1"; bypass the missing-file guard with a real file.
    assert self_improve.pre_iter_cost_check(str(db), "5") is True
    assert self_improve.pre_iter_cost_check(str(db), "10") is False


def test_today_cost_reads_ledger_sum(tmp_path: Path) -> None:
    """scheduler.today_cost_usd now delegates to cost_ledger.spent_last_24h."""
    from mini_ork import scheduler

    db = _init_db(tmp_path / "today")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=0.5, ts=_iso(now - 600))
    _insert_llm_call(db, run_id="r1", cost=0.3, ts=_iso(now - 1200))

    assert scheduler.today_cost_usd(str(db)) == pytest.approx(0.8)


def test_conductor_today_cost_reads_ledger_sum(tmp_path: Path) -> None:
    """orchestration.conductor._today_cost delegates to cost_ledger."""
    from mini_ork.orchestration import conductor

    db = _init_db(tmp_path / "conductor")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=2.0, ts=_iso(now - 600))

    assert conductor._today_cost(str(db)) == pytest.approx(2.0)


def test_cli_spent_last_24h_reads_ledger_sum(tmp_path: Path) -> None:
    """cli.main._spent_last_24h is the display mirror."""
    from mini_ork.cli import main as cli_main

    db = _init_db(tmp_path / "display")
    now = time.time()
    _insert_llm_call(db, run_id="r1", cost=1.5, ts=_iso(now - 600))

    assert cli_main._spent_last_24h(str(db)) == pytest.approx(1.5)

def test_idle_wal_db_without_sidecar_files_is_still_read(tmp_path: Path) -> None:
    """A WAL db nobody has open has no -shm/-wal files. A ``mode=ro`` reader
    cannot create the -shm and fails, which read as $0 — the guard never trips."""
    import os

    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE llm_calls(id INTEGER PRIMARY KEY, run_id TEXT, cost_usd REAL, ts TEXT)")
    con.execute("INSERT INTO llm_calls(run_id, cost_usd, ts) VALUES ('r1', 3.5, ?)", (_iso(time.time() - 60),))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for suffix in ("-shm", "-wal"):
        side = tmp_path / f"state.db{suffix}"
        if side.exists():
            os.remove(side)

    assert cost_ledger.spent_last_24h(db) == pytest.approx(3.5)
