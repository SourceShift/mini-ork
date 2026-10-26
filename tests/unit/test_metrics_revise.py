"""rev6 R5b — ``mini-ork metrics revise`` (rounds-to-pass reporting).

Hermetic: every case builds a migrated state.db with
``mini_ork.stores.migrate.init_db``; run dirs live under ``tmp_path``.
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import metrics, metrics_revise
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
NOW = int(time.time())


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "state.db")
    rc, _out, err = mig.init_db(db=path, root=str(REPO))
    assert rc == 0, err
    return path


def seed_round(con, run_id, task_class, target, rnd, passed,
               created_at=NOW, cost=1.0):
    con.execute(
        "INSERT OR REPLACE INTO revise_rounds"
        " (run_id, task_class, target_node, round, passed, cost_usd, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, task_class, target, rnd, passed, cost, created_at),
    )


def run(argv, *, stdout=None, stderr=None):
    out = stdout if stdout is not None else io.StringIO()
    err = stderr if stderr is not None else io.StringIO()
    rc = metrics_revise.main(argv, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def run_json(db_path, *extra, runs_dir=None):
    argv = ["--db", str(db_path), "--json", *extra]
    if runs_dir is not None:
        argv += ["--runs-dir", str(runs_dir)]
    rc, out, err = run(argv)
    assert rc == 0, err
    return json.loads(out)


# 7. distribution --------------------------------------------------------------

def test_three_run_distribution(db):
    with sqlite3.connect(db) as con:
        seed_round(con, "run-A", "code-fix", "implementer", 0, 1)
        for rnd in (0, 1, 2):
            seed_round(con, "run-B", "code-fix", "implementer", rnd, 0)
        seed_round(con, "run-B", "code-fix", "implementer", 2, 1)
        for rnd in (0, 1, 2):
            seed_round(con, "run-C", "code-fix", "implementer", rnd, 0)
    m = run_json(db)["revise_rounds"]["code-fix"]
    assert m["runs"] == 3
    assert m["passed_first_try"] == 1
    assert m["rounds_to_pass"] == {"0": 1, "2": 1}
    assert m["failed"] == 1
    assert m["open"] == 0


def test_open_is_max_round_null(db):
    with sqlite3.connect(db) as con:
        seed_round(con, "run-D", "code-fix", "implementer", 0, 0)
        seed_round(con, "run-D", "code-fix", "implementer", 1, None)
        seed_round(con, "run-E", "code-fix", "implementer", 0, None)
    m = run_json(db)["revise_rounds"]["code-fix"]
    assert m["runs"] == 2 and m["open"] == 2
    assert m["failed"] == 0 and m["passed_first_try"] == 0


def test_task_class_filter(db):
    with sqlite3.connect(db) as con:
        seed_round(con, "run-A", "code-fix", "implementer", 0, 1)
        seed_round(con, "run-R", "research-synthesis", "implementer", 0, 1)
        seed_round(con, "run-R2", "research-synthesis", "implementer", 0, 0)
    obj = run_json(db, "--task-class", "research-synthesis")
    assert set(obj["revise_rounds"]) == {"research-synthesis"}
    assert obj["revise_rounds"]["research-synthesis"]["runs"] == 2


def test_window_defaults_to_30_days(db):
    old = NOW - 40 * 86400
    with sqlite3.connect(db) as con:
        seed_round(con, "run-old", "code-fix", "implementer", 0, 1, created_at=old)
        seed_round(con, "run-new", "code-fix", "implementer", 0, 1)
    obj = run_json(db)
    assert obj["window_days"] == 30
    assert obj["revise_rounds"]["code-fix"]["runs"] == 1
    obj = run_json(db, "--days", "60")
    assert obj["revise_rounds"]["code-fix"]["runs"] == 2


# 8. history -------------------------------------------------------------------

def _seed_history(con):
    for rid in ("run-H", "run-I"):
        con.execute(
            "INSERT OR REPLACE INTO task_runs"
            " (id, task_class, kickoff_path, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (rid, "code-fix", "/k.md", NOW, NOW),
        )
    for n in (1, 2, 3):
        con.execute(
            "INSERT INTO node_attempts"
            " (run_id, node_id, attempt_no, started_at, ended_at, result)"
            " VALUES ('run-H','implementer',?,?,?,'success')",
            (n, NOW, NOW + n),
        )
    for n in (1, 2):
        con.execute(
            "INSERT INTO node_attempts"
            " (run_id, node_id, attempt_no, started_at, ended_at, result)"
            " VALUES ('run-I','implementer',?,?,?,'success')",
            (n, NOW, NOW + n),
        )
    con.execute(
        "INSERT INTO node_attempts"
        " (run_id, node_id, attempt_no, started_at, ended_at, result)"
        " VALUES ('run-H','researcher',1,?,?,'success')",
        (NOW, NOW + 9),
    )


def test_history_counts_implementer_attempts(db):
    with sqlite3.connect(db) as con:
        _seed_history(con)
    history = run_json(db)["history"]
    assert history["by_task_class"]["code-fix"] == {"3": 1, "2": 1}
    assert "node_attempts" in history["label"]
    assert "implementer" in history["label"]


def test_history_omitted_when_table_missing(tmp_path):
    bare = str(tmp_path / "bare.db")
    with sqlite3.connect(bare) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS revise_rounds ("
            " id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, task_class TEXT,"
            " target_node TEXT NOT NULL, round INTEGER NOT NULL, passed INTEGER,"
            " cost_usd REAL, created_at INTEGER NOT NULL,"
            " UNIQUE (run_id, target_node, round))"
        )
        seed_round(con, "run-A", "code-fix", "implementer", 0, 1)
    obj = run_json(bare)
    assert "history" not in obj


# 9. convergence ---------------------------------------------------------------

def test_convergence_sums_findings_ledgers(db, tmp_path):
    runs = tmp_path / "runs"
    (runs / "run-X" / "revise").mkdir(parents=True)
    (runs / "run-X" / "revise" / "findings.json").write_text(json.dumps({
        "rounds": [
            {"new": 2, "reported_again": 1, "fixed": 1,
             "reopened": 0, "outside_delta": 0},
            {"new": 0, "reported_again": 1, "fixed": 2,
             "reopened": 1, "outside_delta": 0},
        ]
    }))
    (runs / "run-Y" / "revise").mkdir(parents=True)
    (runs / "run-Y" / "revise" / "findings.json").write_text("{not json")
    (runs / "run-Z").mkdir()
    conv = run_json(db, runs_dir=runs)["convergence"]
    assert conv["runs_with_ledger"] == 1
    assert conv["new"] == 2 and conv["reported_again"] == 2
    assert conv["fixed"] == 3 and conv["reopened"] == 1
    assert conv["outside_delta"] == 0


# 10. output + exit codes ------------------------------------------------------

def test_json_top_level_shape(db, tmp_path):
    runs = tmp_path / "runs"
    (runs / "run-X" / "revise").mkdir(parents=True)
    (runs / "run-X" / "revise" / "findings.json").write_text(
        json.dumps({"rounds": [{"new": 1, "fixed": 1}]})
    )
    with sqlite3.connect(db) as con:
        seed_round(con, "run-A", "code-fix", "implementer", 0, 1)
        _seed_history(con)
    obj = run_json(db, runs_dir=runs)
    assert set(obj) == {"revise_rounds", "history", "convergence", "window_days"}
    assert obj["window_days"] == 30


def test_markdown_tables_by_default(db):
    with sqlite3.connect(db) as con:
        seed_round(con, "run-A", "code-fix", "implementer", 0, 1)
    rc, out, err = run(["--db", db])
    assert rc == 0, err
    assert "|" in out


def test_missing_db_is_rc1_with_stderr(tmp_path):
    rc, _out, err = run(["--db", str(tmp_path / "nope.db")])
    assert rc == 1
    assert err.strip()


def test_help_exits_zero():
    rc = None
    try:
        rc = metrics.main(["revise", "--help"], stdout=io.StringIO(),
                          stderr=io.StringIO())
    except SystemExit as exc:
        rc = exc.code
    assert rc == 0


def test_metrics_usage_lists_revise():
    assert "revise" in metrics._usage()


def test_metrics_main_dispatches_revise(db):
    with sqlite3.connect(db) as con:
        seed_round(con, "run-A", "code-fix", "implementer", 0, 1)
    out, err = io.StringIO(), io.StringIO()
    rc = metrics.main(["revise", "--db", db, "--json"],
                      stdout=out, stderr=err)
    assert rc == 0, err.getvalue()
    obj = json.loads(out.getvalue())
    assert obj["revise_rounds"]["code-fix"]["passed_first_try"] == 1


def test_feature_flags_documents_the_flag_and_command():
    doc = (REPO / "docs" / "operator" / "feature-flags.md").read_text()
    assert "MO_REVIEW_CONVERGE" in doc
    assert "metrics revise" in doc
