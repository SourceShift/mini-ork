"""rev6 R5b — ``mini_ork.stores.revise_rounds`` (rounds-to-pass ledger).

Hermetic: every case builds a migrated state.db with
``mini_ork.stores.migrate.init_db``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mini_ork.stores import migrate as mig
from mini_ork.stores import revise_rounds as rr

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "state.db")
    rc, _out, err = mig.init_db(db=path, root=str(REPO))
    assert rc == 0, err
    return path


def rows(db: str, run_id: str = "run-1", target: str = "implementer"):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT round, passed, cost_usd, created_at FROM revise_rounds"
            " WHERE run_id = ? AND target_node = ? ORDER BY round",
            (run_id, target),
        ).fetchall()


# 1. empty table ---------------------------------------------------------------

def test_next_round_zero_on_empty_table(db):
    assert rr.next_round(db, "run-1", "implementer") == 0


def test_next_round_is_max_plus_one(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 1, cost_usd=0.5, now=100)
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 1, True, cost_usd=0.5, now=101)
    assert rr.next_round(db, "run-1", "implementer") == 2


def test_next_round_scoped_to_run_and_target(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 3, now=100)
    assert rr.next_round(db, "run-2", "implementer") == 0
    assert rr.next_round(db, "run-1", "reviewer") == 0


# 2. first-try pass ------------------------------------------------------------

def test_first_try_pass_writes_one_passed_row(db):
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 0, True,
                      cost_usd=1.25, now=100)
    assert rows(db) == [(0, 1, 1.25, 100)]


def test_outcome_failed_is_zero_not_null(db):
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 0, False, now=100)
    assert rows(db) == [(0, 0, None, 100)]


# 3. grant then outcome --------------------------------------------------------

def test_grant_then_outcome_rows(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 1, cost_usd=0.5, now=100)
    assert [(r[0], r[1]) for r in rows(db)] == [(0, 0), (1, None)]
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 1, True,
                      cost_usd=0.75, now=101)
    assert [(r[0], r[1]) for r in rows(db)] == [(0, 0), (1, 1)]


def test_grant_upserts_prior_round_without_resetting_a_started_one(db):
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO revise_rounds"
            " (run_id, task_class, target_node, round, passed, cost_usd, created_at)"
            " VALUES ('run-1','code-fix','implementer',2,NULL,NULL,100)"
        )
    rr.record_grant(db, "run-1", "code-fix", "implementer", 3, now=200)
    assert [(r[0], r[1]) for r in rows(db)] == [(2, 0), (3, None)]


# 4. recover continues numbering ----------------------------------------------

def test_recover_continues_numbering(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 1, cost_usd=0.5, now=100)
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 1, True, now=101)
    assert rr.next_round(db, "run-1", "implementer") == 2
    rr.record_grant(db, "run-1", "code-fix", "implementer", 3, now=200)
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 3, False, now=201)
    assert [(r[0], r[1]) for r in rows(db)] == [(0, 0), (1, 1), (2, 0), (3, 0)]


# 5. never raise ---------------------------------------------------------------

def _all_calls(path: str) -> None:
    rr.next_round(path, "run-1", "implementer")
    rr.record_grant(path, "run-1", "code-fix", "implementer", 1, now=100)
    rr.record_outcome(path, "run-1", "code-fix", "implementer", 1, True, now=101)


def test_missing_db_file_is_silent(tmp_path):
    _all_calls(str(tmp_path / "nope.db"))
    assert not (tmp_path / "nope.db").exists(), "the store must not create a DB"


def test_empty_db_string_is_silent():
    _all_calls("")


def test_directory_path_is_silent(tmp_path):
    _all_calls(str(tmp_path))


def test_db_without_table_is_silent(tmp_path):
    bare = str(tmp_path / "bare.db")
    with sqlite3.connect(bare) as con:
        con.execute("CREATE TABLE other (x)")
    _all_calls(bare)


def test_non_sqlite_file_is_silent(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("this is definitely not a sqlite database file")
    _all_calls(str(junk))


# 6. upsert cost semantics -----------------------------------------------------

def test_none_cost_on_later_upsert_keeps_earlier_cost(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 1, cost_usd=2.5, now=100)
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 1, True,
                      cost_usd=None, now=101)
    r = rows(db)
    assert r[0][2] == 2.5 and r[1][2] == 2.5


def test_non_null_cost_updates_on_upsert(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 1, cost_usd=1.0, now=100)
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 1, True,
                      cost_usd=2.0, now=101)
    assert rows(db)[1][2] == 2.0


def test_created_at_keeps_first_insert_value(db):
    rr.record_grant(db, "run-1", "code-fix", "implementer", 1, now=111)
    rr.record_outcome(db, "run-1", "code-fix", "implementer", 1, True, now=999)
    r = rows(db)
    assert r[0][3] == 111 and r[1][3] == 111


def test_connect_uses_timeout_and_busy_timeout(db, monkeypatch):
    seen: list[tuple] = []
    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        seen.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    rr.next_round(db, "run-1", "implementer")
    assert seen
    for args, kwargs in seen:
        assert kwargs.get("timeout") == 15 or (len(args) >= 2 and args[1] == 15)
