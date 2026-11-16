"""``mini-ork metrics explore`` — attempt spend by move kind.

The command's job is to keep two false conclusions out of a cost review:

* a table of zeros that reads as "the agent never explored" when in fact the
  ledger was never measured (un-migrated column, pre-0093 rows);
* an ``explore`` share presented as measured behaviour when ``explore`` is
  caller-declared only and no dispatcher ever declares it.

So these tests cover the accounting (counts, shares, cost shares, success
rates), the headline, the two caveats, and every degradation path: no DB, no
``node_attempts``, no ``move_kind``, no ``task_runs``.

Hermetic: a tmp state.db built from a hand-written DDL. No network, no live
mini-ork state.
"""
from __future__ import annotations

import io
import json
import sqlite3
import time

import pytest

from mini_ork.cli import metrics, metrics_explore

_NOW = int(time.time())
_OLD = _NOW - 400 * 86400

_DDL = """
CREATE TABLE node_attempts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,
    node_id               TEXT    NOT NULL,
    attempt_no            INTEGER NOT NULL,
    node_type             TEXT,
    started_at            INTEGER NOT NULL,
    ended_at              INTEGER NOT NULL,
    result                TEXT    NOT NULL,
    failure_class         TEXT,
    checkpoint_used       INTEGER NOT NULL DEFAULT 0,
    checkpoint_produced   INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL,
    provider_session_id   TEXT,
    initiator             TEXT,
    move_kind             TEXT
);
CREATE TABLE task_runs (
    id          TEXT PRIMARY KEY,
    task_class  TEXT
);
"""

_LEGACY_DDL = _DDL.replace("    initiator             TEXT,\n    move_kind             TEXT\n",
                           "    initiator             TEXT\n")


def _make_db(path, ddl=_DDL, *, with_move_kind: bool = True) -> str:
    con = sqlite3.connect(str(path))
    con.executescript(ddl)
    con.execute("INSERT INTO task_runs (id, task_class) VALUES ('r1', 'code-fix')")
    con.execute("INSERT INTO task_runs (id, task_class) VALUES ('r2', 'book')")
    rows = [
        # run, node, attempt_no, result, cost, move_kind, ended_at
        ("r1", "n1", 1, "success", 1.0, "initial", _NOW),
        ("r1", "n1", 2, "failure", 0.5, "retry", _NOW),
        ("r1", "n1", 3, "success", 2.0, "revise", _NOW),
        ("r2", "n1", 1, "failure", 0.25, "initial", _NOW),
        # Outside the 30-day window.
        ("r1", "n1", 4, "success", 9.0, "revise", _OLD),
    ]
    if with_move_kind:
        con.executemany(
            "INSERT INTO node_attempts (run_id, node_id, attempt_no, started_at,"
            " ended_at, result, cost_usd, move_kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run, node, attempt_no, ended_at, ended_at, result, cost, kind)
                for run, node, attempt_no, result, cost, kind, ended_at in rows
            ],
        )
    else:
        con.executemany(
            "INSERT INTO node_attempts (run_id, node_id, attempt_no, started_at,"
            " ended_at, result, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (run, node, attempt_no, ended_at, ended_at, result, cost)
                for run, node, attempt_no, result, cost, _kind, ended_at in rows
            ],
        )
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def db(tmp_path) -> str:
    return _make_db(tmp_path / "state.db")


def _run(argv, db):
    out, err = io.StringIO(), io.StringIO()
    rc = metrics_explore.main(argv + ["--db", db], stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ accounting


def test_counts_shares_and_success_rates(db):
    data = metrics_explore.collect(db, days=30, task_class=None)
    moves = data["moves"]
    kinds = moves["by_task_class"]["code-fix"]
    assert kinds["initial"] == {
        "attempts": 1, "successes": 1, "cost_usd": 1.0,
        "share_of_attempts": pytest.approx(1 / 3),
        "share_of_cost": pytest.approx(1.0 / 3.5),
        "success_rate": 1.0,
    }
    assert kinds["retry"]["successes"] == 0
    assert kinds["retry"]["success_rate"] == 0.0
    assert kinds["revise"]["cost_usd"] == 2.0
    assert kinds["revise"]["share_of_cost"] == pytest.approx(2.0 / 3.5)


def test_window_excludes_older_attempts(db):
    data = metrics_explore.collect(db, days=30, task_class=None)
    h = data["moves"]["headline"]
    assert h["attempts"] == 4
    assert h["cost_usd"] == pytest.approx(3.75)


def test_headline_attempts_per_success_and_cost_shares(db):
    h = metrics_explore.collect(db, days=30, task_class=None)["moves"]["headline"]
    assert h["successes"] == 2
    assert h["attempts_per_success"] == pytest.approx(2.0)
    assert h["share_of_cost"]["revise"] == pytest.approx(2.0 / 3.75)
    assert h["share_of_cost"]["retry"] == pytest.approx(0.5 / 3.75)
    assert h["share_of_cost"]["explore"] == 0.0
    assert h["share_of_attempts"]["initial"] == pytest.approx(2 / 4)


def test_task_class_filter(db):
    data = metrics_explore.collect(db, days=30, task_class="book")
    by_class = data["moves"]["by_task_class"]
    assert list(by_class) == ["book"]
    assert data["moves"]["headline"]["attempts"] == 1


def test_nothing_succeeded_reports_none_not_a_division(tmp_path):
    db = _make_db(tmp_path / "empty-success.db")
    con = sqlite3.connect(db)
    con.execute("UPDATE node_attempts SET result = 'failure'")
    con.commit()
    con.close()
    h = metrics_explore.collect(db, days=30, task_class=None)["moves"]["headline"]
    assert h["successes"] == 0
    assert h["attempts_per_success"] is None


# ------------------------------------------------------------------ degradation


def test_missing_move_kind_is_unavailable_not_a_zero_table(tmp_path):
    db = _make_db(tmp_path / "legacy.db", _LEGACY_DDL, with_move_kind=False)
    data = metrics_explore.collect(db, days=30, task_class=None)
    assert data["moves"] is None
    assert "0093_node_attempts_move_kind.sql" in data["unavailable"]
    text = metrics_explore.render_markdown(data)
    assert "Not measurable" in text
    assert "attempts by move kind" in text
    # An unread table must not be rendered as a row of zeros.
    assert "| code-fix | initial |" not in text


def test_pre_0093_rows_are_counted_as_unclassified(tmp_path):
    db = _make_db(tmp_path / "nulls.db")
    con = sqlite3.connect(db)
    con.execute("UPDATE node_attempts SET move_kind = NULL")
    con.commit()
    con.close()
    data = metrics_explore.collect(db, days=30, task_class=None)
    kinds = data["moves"]["by_task_class"]["code-fix"]
    assert kinds["unclassified"]["attempts"] == 3
    assert data["moves"]["headline"]["share_of_attempts"]["unclassified"] == 1.0


def test_missing_task_runs_still_measures_but_cannot_attribute(tmp_path):
    db = _make_db(tmp_path / "no-runs.db")
    con = sqlite3.connect(db)
    con.execute("DROP TABLE task_runs")
    con.execute("DELETE FROM node_attempts WHERE run_id = 'r1' AND ended_at < ?",
                (_NOW - 86400,))
    con.commit()
    con.close()
    data = metrics_explore.collect(db, days=30, task_class=None)
    assert list(data["moves"]["by_task_class"]) == ["unknown"]
    assert data["moves"]["headline"]["attempts"] > 0


def test_missing_node_attempts_table_is_unavailable(tmp_path):
    path = tmp_path / "bare.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_class TEXT)")
    con.commit()
    con.close()
    data = metrics_explore.collect(str(path), days=30, task_class=None)
    assert data["moves"] is None
    assert "node_attempts" in data["unavailable"]


def test_no_state_db_is_exit_1(tmp_path):
    rc, out, err = _run([], str(tmp_path / "nope.db"))
    assert rc == 1
    assert "no state.db" in err
    assert out == ""


# --------------------------------------------------------------------- surfaces


def test_json_output_parses_and_carries_the_headline(db):
    rc, out, err = _run(["--json"], db)
    assert rc == 0
    payload = json.loads(out)
    assert payload["moves"]["headline"]["attempts"] == 4
    assert err == ""


def test_render_prints_the_two_caveats(db):
    text = metrics_explore.render_markdown(
        metrics_explore.collect(db, days=30, task_class=None))
    assert "caller-declared only" in text
    assert "unclassified" in text
    assert "| code-fix | revise |" in text
    assert "attempts per accepted attempt" in text


def test_metrics_dispatches_explore(db):
    out, err = io.StringIO(), io.StringIO()
    rc = metrics.main(["explore", "--db", db, "--json"], stdout=out, stderr=err)
    assert rc == 0
    assert json.loads(out.getvalue())["moves"]["headline"]["attempts"] == 4
