"""``mini-ork settle`` and the MO_POST_RUN_LEARNING deferral gate.

The gate exists so a caller that dispatches the next deliverable the moment a
run ends does not pay three to five minutes of learning work on the critical
path. These tests pin the two halves of that contract: the gate returns before
touching any database, and ``settle`` refuses cleanly when the state is not
there to settle.
"""
from __future__ import annotations

import sqlite3

from mini_ork.cli import execute, settle


def _db_with_runs(tmp_path, rows):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("create table runs (id text, run_dir text, final_verdict text)")
    con.executemany("insert into runs values (?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


def test_gate_defers_without_touching_the_database(monkeypatch, capsys):
    monkeypatch.setenv("MO_POST_RUN_LEARNING", "0")
    execute._post_run_learning(None, "/does/not/exist", "run-abc", "", fail_count=0)
    out = capsys.readouterr().out
    assert "deferred" in out
    assert "mini-ork settle run-abc" in out


def test_gate_accepts_every_documented_spelling(monkeypatch, capsys):
    for value in ("0", "off", "false", "no", "skip", "  OFF  "):
        monkeypatch.setenv("MO_POST_RUN_LEARNING", value)
        execute._post_run_learning(None, "/does/not/exist", "run-abc", "")
        assert "deferred" in capsys.readouterr().out, value


def test_settle_rejects_a_missing_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert "no state db" in capsys.readouterr().err


def test_settle_rejects_an_unknown_run(tmp_path, monkeypatch, capsys):
    _db_with_runs(tmp_path, [])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert "not found" in capsys.readouterr().err


def test_settle_rejects_a_missing_run_dir(tmp_path, monkeypatch, capsys):
    _db_with_runs(tmp_path, [("run-abc", str(tmp_path / "gone"), "pass")])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert "missing" in capsys.readouterr().err


def test_settle_help_and_usage():
    assert settle.main(["--help"]) == 0
    assert settle.main([]) == 2
