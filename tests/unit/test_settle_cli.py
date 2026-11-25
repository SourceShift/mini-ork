"""``mini-ork settle`` and the MO_POST_RUN_LEARNING deferral gate.

The gate exists so a caller that dispatches the next deliverable the moment a
run ends does not pay three to five minutes of learning work on the critical
path. These tests pin the three halves of that contract: the gate returns before
touching any database, ``settle`` refuses cleanly when the state is not there to
settle, and ``settle`` actually reaches the learning channels of a real run —
the run registry is ``task_runs`` (``id TEXT``), not the legacy evaluator table
``runs`` (``id INTEGER``, a handful of benchmark rows), and the deferred flag
must not defer a second time.
"""
from __future__ import annotations

import sqlite3

from mini_ork.cli import execute, settle


def _db_with_runs(tmp_path, rows):
    """The legacy ARTIFACT table — kept so a regression back to it is visible."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("create table runs (id integer primary key, run_dir text, "
                "final_verdict text)")
    con.executemany("insert into runs values (?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


def _db_with_task_runs(tmp_path, rows):
    """The real run registry (db/migrations/0013_task_runs.sql, abbreviated)."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("create table task_runs (id text primary key, status text, "
                "verdict text)")
    con.executemany("insert into task_runs values (?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


def _run_dir(tmp_path, run_id, task_class="recursive-validate-impl"):
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(
        '{"task_class": "%s"}' % task_class, encoding="utf-8")
    return run_dir


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


def test_force_bypasses_the_gate(monkeypatch, capsys):
    """``settle`` runs BECAUSE the variable is set; re-reading it here would
    print "[deferred]" and exit 0 having graded nothing."""
    monkeypatch.setenv("MO_POST_RUN_LEARNING", "0")
    # force=True walks into the channels with a db=None; they are all wrapped in
    # try/except, so the point is only that the deferral branch was not taken.
    execute._post_run_learning(None, "/does/not/exist", "run-abc", "",
                               fail_count=0, force=True)
    assert "deferred" not in capsys.readouterr().out


def test_settle_rejects_a_missing_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert "no state db" in capsys.readouterr().err


def test_settle_defaults_to_dot_mini_ork_under_cwd(tmp_path, monkeypatch, capsys):
    """Without MINI_ORK_HOME the home is ``<cwd>/.mini-ork`` (RunContext's
    default), not ``<repo>/state.db`` — reading MINI_ORK_ROOT as the home found
    no database at all."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINI_ORK_HOME", raising=False)
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert str(tmp_path / ".mini-ork" / "state.db") in capsys.readouterr().err


def test_settle_rejects_an_unknown_run(tmp_path, monkeypatch, capsys):
    _db_with_task_runs(tmp_path, [])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert "not found" in capsys.readouterr().err


def test_settle_does_not_read_the_legacy_runs_table(tmp_path, monkeypatch, capsys):
    """runs.id is INTEGER and holds benchmark rows; a real ``run-…`` id never
    matches it. Reading that table answered "not found" for every real run."""
    _db_with_runs(tmp_path, [(1, str(_run_dir(tmp_path, "run-abc")), "pass")])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    # A db that has only the legacy table is not a run registry: the failure
    # names ``task_runs``, the table settle must be reading.
    assert "task_runs" in capsys.readouterr().err


def test_settle_rejects_a_missing_run_dir(tmp_path, monkeypatch, capsys):
    _db_with_task_runs(tmp_path, [("run-abc", "published", "APPROVE")])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    assert settle.main(["run-abc"]) == 2
    assert "missing" in capsys.readouterr().err


def test_settle_resolves_the_run_and_forces_the_channels(tmp_path, monkeypatch,
                                                         capsys):
    run_dir = _run_dir(tmp_path, "run-abc")
    _db_with_task_runs(tmp_path, [("run-abc", "published", "APPROVE")])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setenv("MO_POST_RUN_LEARNING", "0")
    calls = []

    def fake(db, rd, rid, task_class="", fail_count=None, *, force=False):
        calls.append((rd, rid, task_class, fail_count, force))

    monkeypatch.setattr(execute, "_post_run_learning", fake)
    assert settle.main(["run-abc"]) == 0
    assert calls == [(str(run_dir), "run-abc", "recursive-validate-impl", 0, True)]


def test_settle_maps_a_failing_verdict_to_fail_count_one(tmp_path, monkeypatch,
                                                         capsys):
    _run_dir(tmp_path, "run-bad")
    _db_with_task_runs(tmp_path, [("run-bad", "failed", "REQUEST_CHANGES")])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    seen = {}
    monkeypatch.setattr(
        execute, "_post_run_learning",
        lambda db, rd, rid, tc="", fail_count=None, *, force=False:
            seen.update(fail_count=fail_count))
    assert settle.main(["run-bad"]) == 0
    assert seen["fail_count"] == 1


def test_settle_leaves_fail_count_unset_when_the_run_recorded_no_verdict(
        tmp_path, monkeypatch):
    """None keeps the run-verified stamp unwritten rather than guessing pass."""
    _run_dir(tmp_path, "run-open")
    _db_with_task_runs(tmp_path, [("run-open", "executing", None)])
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    seen = {}
    monkeypatch.setattr(
        execute, "_post_run_learning",
        lambda db, rd, rid, tc="", fail_count=None, *, force=False:
            seen.update(fail_count=fail_count))
    assert settle.main(["run-open"]) == 0
    assert seen["fail_count"] is None


def test_settle_help_and_usage():
    assert settle.main(["--help"]) == 0
    assert settle.main([]) == 2
