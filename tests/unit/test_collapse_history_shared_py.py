"""The shared ``collapse_history`` writer: per-task-class steps and the read shape.

``test_collapse_history_writer.py`` pins the ``apply`` wiring end to end (cases
1-7 of its kickoff). This file covers what the RSI cycle added when the insert
moved into ``mini_ork.learning.collapse_history``: that two producers do not
share a step counter, and that the reader emits exactly the row shape
``collapse_detector.detect`` consumes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.learning import collapse_detector, collapse_history  # noqa: E402

NOW = 1_760_000_000

DDL = """
CREATE TABLE collapse_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_class TEXT NOT NULL,
    step       INTEGER NOT NULL,
    score      REAL NOT NULL,
    anchor     REAL NOT NULL,
    directives INTEGER NOT NULL DEFAULT 0,
    run_id     TEXT,
    created_at INTEGER NOT NULL
);
"""


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(DDL)
    yield connection
    connection.close()


def test_each_task_class_counts_its_own_steps(con):
    assert collapse_history.append(con, task_class="a", score=1.0, anchor=1.0,
                                   now=NOW) == 0
    assert collapse_history.append(con, task_class="a", score=1.0, anchor=1.0,
                                   now=NOW) == 1
    assert collapse_history.append(con, task_class="b", score=1.0, anchor=1.0,
                                   now=NOW) == 0, "a shared counter would pair the wrong rows"


def test_no_anchor_means_no_row(con):
    assert collapse_history.append(con, task_class="tc", score=1.0, anchor=None,
                                   now=NOW) is None
    assert con.execute("SELECT COUNT(*) FROM collapse_history").fetchone()[0] == 0


def test_a_missing_table_is_a_silent_no_op():
    bare = sqlite3.connect(":memory:")
    try:
        assert collapse_history.append(bare, task_class="tc", score=1.0,
                                       anchor=1.0) is None
        assert collapse_history.history(bare, "tc") == []
    finally:
        bare.close()


def test_history_is_oldest_first_in_detector_shape(con):
    collapse_history.append(con, task_class="tc", score=0.5, anchor=0.9,
                            directives=2, run_id="a", now=NOW)
    collapse_history.append(con, task_class="tc", score=0.6, anchor=0.8,
                            run_id="b", now=NOW)

    rows = collapse_history.history(con, "tc")

    assert [r["step"] for r in rows] == [0, 1]
    assert rows[0]["directives"] == ["d0", "d1"], \
        "the detector counts distinct directive ids, so the count is expanded"
    assert collapse_detector.detect(rows)["n"] == 2


def test_history_limit_keeps_the_most_recent_rows(con):
    for step in range(6):
        collapse_history.append(con, task_class="tc", score=float(step),
                                anchor=1.0, now=NOW)

    rows = collapse_history.history(con, "tc", limit=3)

    assert [r["step"] for r in rows] == [3, 4, 5]
