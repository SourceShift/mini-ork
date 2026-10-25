"""LEARN: the two rows a finished cycle leaves behind, and when it stops itself.

The load-bearing assertions are about *absence* as much as presence: a cycle
that measured no probes writes no collapse row, a cycle with no candidate
writes no credit row, and running ``record`` twice does not double-count. Each
of those, done wrong, teaches the detector a trend that never happened.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.learning import collapse_detector, collapse_history  # noqa: E402
from mini_ork.rsi import learn, ledger  # noqa: E402
from mini_ork.rsi.candidates import Candidate  # noqa: E402
from mini_ork.stores import migrate  # noqa: E402

NOW = 1_760_000_000
CAND = Candidate.build(
    domain="verification",
    source="db:V1",
    kind="data_path_dead",
    title="confidence is never persisted",
    evidence_key="table:verifier_results:confidence-null",
    files=("mini_ork/verify/persist.py",),
    success_command="db:V1",
    now=NOW,
)


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "state.db"
    migdir = tmp_path / "migrations"
    migdir.mkdir()
    for name in ("0084_rsi_cycles.sql", "0060_collapse_history.sql"):
        shutil.copy(REPO / "db" / "migrations" / name, migdir / name)
    rc, out = migrate.migrate_apply(str(migdir), dry_run=False, db=str(path),
                                    root=str(REPO))
    assert rc == 0, out
    return path


@pytest.fixture
def con(db):
    connection = ledger.connect(str(db))
    yield connection
    connection.close()


def _cycle(con, cycle_id: str, status: str, *, started: int = NOW,
           cost: float = 0.0) -> None:
    ledger.start_cycle(con, cycle_id=cycle_id, now=started, mode="once", dry_run=False)
    ledger.finish_cycle(con, cycle_id, now=started + 1, status=status, cost_usd=cost)


def _probe(con, cycle_id: str, probe_id: str, status: str) -> None:
    ledger.record_probe_run(con, cycle_id=cycle_id, now=NOW, probe_id=probe_id,
                            cmd=probe_id, status=status, rc=0, duration_ms=1)


def _history(con, task_class: str = learn.TASK_CLASS) -> list[dict]:
    rows = con.execute(
        "SELECT step, score, anchor, directives, run_id FROM collapse_history"
        " WHERE task_class = ? ORDER BY step",
        (task_class,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# score and anchor
# --------------------------------------------------------------------------


def test_an_empty_history_has_no_rate(con):
    assert learn.merge_rate(con) is None


def test_the_rate_counts_finished_cycles_only(con):
    _cycle(con, "a", "merged", started=NOW - 3)
    _cycle(con, "b", "quarantined", started=NOW - 2)
    _cycle(con, "c", "no_candidate", started=NOW - 1)
    ledger.start_cycle(con, cycle_id="d", now=NOW, mode="once", dry_run=False)

    assert learn.merge_rate(con) == pytest.approx(1 / 3)


def test_the_rate_respects_its_window(con):
    for i in range(12):
        _cycle(con, f"old{i}", "merged", started=NOW - 100 + i)
    _cycle(con, "new", "quarantined", started=NOW)

    assert learn.merge_rate(con, window=1) == 0.0
    assert learn.merge_rate(con, window=20) == pytest.approx(12 / 13)


def test_the_anchor_counts_green_probes_and_ignores_skipped(con):
    _cycle(con, "c", "merged")
    _probe(con, "c", "garden", "ok")
    _probe(con, "c", "validate", "findings")
    _probe(con, "c", "tests", "error")
    _probe(con, "c", "calibrate", "skipped")

    assert learn.probe_anchor(con, "c") == pytest.approx(2 / 3)


def test_no_probe_means_no_anchor(con):
    _cycle(con, "c", "merged")
    assert learn.probe_anchor(con, "c") is None

    _probe(con, "c", "garden", "skipped")
    assert learn.probe_anchor(con, "c") is None, "a skipped probe measured nothing"


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------


def _seed_candidate(con) -> None:
    ledger.upsert_candidate(con, CAND, now=NOW)


def test_a_merged_cycle_writes_credit_and_one_collapse_row(con, tmp_path):
    _seed_candidate(con)
    _cycle(con, "rsi-x", "merged", cost=2.5)
    _probe(con, "rsi-x", "garden", "ok")

    report = learn.record(con, cycle_id="rsi-x", status="merged",
                          candidate_id=CAND.candidate_id, home=tmp_path, now=NOW)

    credit = [dict(r) for r in con.execute("SELECT * FROM rsi_credit")]
    assert len(credit) == 1
    assert credit[0]["source"] == "db:V1"
    assert credit[0]["domain"] == "verification"
    assert credit[0]["outcome"] == "merged"
    assert credit[0]["utility"] == 1.0
    assert credit[0]["cost_usd"] == pytest.approx(2.5)
    assert credit[0]["lens"] is None

    rows = _history(con)
    assert len(rows) == 1
    assert rows[0]["step"] == 0
    assert rows[0]["score"] == pytest.approx(1.0)
    assert rows[0]["anchor"] == pytest.approx(1.0)
    assert rows[0]["run_id"] == "rsi-x"

    assert report["credit"] is True
    assert report["step"] == 0
    assert report["collapse"] is False


def test_a_quarantined_cycle_scores_zero_utility(con, tmp_path):
    _seed_candidate(con)
    _cycle(con, "rsi-q", "quarantined")
    _probe(con, "rsi-q", "garden", "ok")

    learn.record(con, cycle_id="rsi-q", status="quarantined",
                 candidate_id=CAND.candidate_id, home=tmp_path, now=NOW)

    credit = [dict(r) for r in con.execute("SELECT * FROM rsi_credit")]
    assert [c["utility"] for c in credit] == [0.0]
    assert [c["outcome"] for c in credit] == ["quarantined"]


def test_a_cycle_without_a_candidate_writes_no_credit(con, tmp_path):
    _cycle(con, "rsi-n", "no_candidate")
    _probe(con, "rsi-n", "garden", "ok")

    report = learn.record(con, cycle_id="rsi-n", status="no_candidate",
                          home=tmp_path, now=NOW)

    assert report["credit"] is False
    assert [dict(r) for r in con.execute("SELECT * FROM rsi_credit")] == []
    assert len(_history(con)) == 1, "the anchor was still measured"


def test_record_is_idempotent(con, tmp_path):
    _seed_candidate(con)
    _cycle(con, "rsi-x", "merged")
    _probe(con, "rsi-x", "garden", "ok")

    learn.record(con, cycle_id="rsi-x", status="merged",
                 candidate_id=CAND.candidate_id, home=tmp_path, now=NOW)
    second = learn.record(con, cycle_id="rsi-x", status="merged",
                          candidate_id=CAND.candidate_id, home=tmp_path, now=NOW)

    assert second["credit"] is False
    assert second["step"] == 0
    assert len([r for r in con.execute("SELECT * FROM rsi_credit")]) == 1
    assert len(_history(con)) == 1


def test_a_cycle_that_ran_no_probes_writes_no_row(con, tmp_path):
    _cycle(con, "rsi-n", "no_candidate")

    report = learn.record(con, cycle_id="rsi-n", status="no_candidate",
                          home=tmp_path, now=NOW)

    assert report["anchor"] is None
    assert report["step"] is None
    assert _history(con) == [], "a row needs both halves of the signature"


def test_steps_increment_within_a_task_class_only(con, tmp_path):
    for i, cycle in enumerate(("a", "b", "c")):
        _cycle(con, cycle, "merged", started=NOW + i)
        _probe(con, cycle, "garden", "ok")
        learn.record(con, cycle_id=cycle, status="merged", home=None, now=NOW)

    collapse_history.append(con, task_class="other", score=1.0, anchor=1.0, now=NOW)

    assert [r["step"] for r in _history(con)] == [0, 1, 2]
    assert len(_history(con, "other")) == 1


# --------------------------------------------------------------------------
# the collapse trip
# --------------------------------------------------------------------------


def _seed_trend(con) -> None:
    """Four cycles that never merged, every probe green.

    The next cycle merges once, so its merge rate (1.0) rises against the 0.0
    baseline. Whether that is a collapse depends entirely on what happens to
    the anchor — which is the property under test.
    """
    for step in range(4):
        collapse_history.append(con, task_class=learn.TASK_CLASS, score=0.0,
                                anchor=1.0, run_id=f"seed-{step}",
                                now=NOW - 10 + step)


def test_a_rising_score_against_a_falling_anchor_trips_the_stop(con, tmp_path):
    _seed_trend(con)
    _seed_candidate(con)
    _cycle(con, "rsi-x", "merged")
    _probe(con, "rsi-x", "garden", "error")  # the anchor collapses to 0.0

    report = learn.record(con, cycle_id="rsi-x", status="merged",
                          candidate_id=CAND.candidate_id, home=tmp_path, now=NOW)

    assert report["collapse"] is True
    assert report["score"] == pytest.approx(1.0)
    assert report["anchor"] == 0.0
    stop = tmp_path / "rsi" / "DISABLED"
    assert stop.exists()
    assert stop.read_text(encoding="utf-8").startswith("collapse: score_rise=")


def test_the_same_rise_keeps_the_anchor_green_and_does_not_trip(con, tmp_path):
    _seed_trend(con)
    _cycle(con, "rsi-x", "merged")
    _probe(con, "rsi-x", "garden", "ok")

    report = learn.record(con, cycle_id="rsi-x", status="merged",
                          home=tmp_path, now=NOW)

    assert report["score"] == pytest.approx(1.0)
    assert report["anchor"] == 1.0
    assert report["collapse"] is False, "a rising score with a healthy anchor is progress"
    assert not (tmp_path / "rsi" / "DISABLED").exists()


def test_fewer_than_four_rows_can_never_trip(con, tmp_path):
    _seed_candidate(con)
    for cycle in ("a", "b", "c"):
        _cycle(con, cycle, "merged")
        _probe(con, cycle, "garden", "error")
        learn.record(con, cycle_id=cycle, status="merged", home=tmp_path, now=NOW)

    assert len(_history(con)) == 3
    assert not (tmp_path / "rsi" / "DISABLED").exists(), \
        f"collapse_detector.MIN_STEPS is {collapse_detector.MIN_STEPS}"


def test_an_unwritable_home_reports_instead_of_raising(con, tmp_path):
    _seed_trend(con)
    _cycle(con, "rsi-x", "merged")
    _probe(con, "rsi-x", "garden", "error")
    blocker = tmp_path / "home"
    blocker.write_text("", encoding="utf-8")  # mkdir will fail under a file

    report = learn.record(con, cycle_id="rsi-x", status="merged", home=blocker,
                          now=NOW)

    assert report["collapse"] is True
    assert "disabled_path" not in report
    # macOS raises NotADirectoryError where Linux raises FileExistsError or
    # PermissionError; all three are the OSError branch this test is about.
    assert report["collapse_error"].split(":")[0] in (
        "NotADirectoryError", "FileExistsError", "PermissionError", "OSError")


# --------------------------------------------------------------------------
# what the detector actually consumes
# --------------------------------------------------------------------------


def test_history_shapes_rows_for_the_detector(con):
    collapse_history.append(con, task_class="rsi_cycle", score=0.5, anchor=0.9,
                            directives=2, run_id="a", now=NOW)
    collapse_history.append(con, task_class="rsi_cycle", score=0.6, anchor=0.8,
                            run_id="b", now=NOW)

    rows = collapse_history.history(con, "rsi_cycle")

    assert [r["step"] for r in rows] == [0, 1], "oldest first"
    assert rows[0]["directives"] == ["d0", "d1"]
    assert collapse_detector.detect(rows)["n"] == 2


def test_an_unmigrated_db_is_fail_open():
    """A home that never ran migration 0060 must not fail a cycle."""
    bare = sqlite3.connect(":memory:")
    try:
        assert collapse_history.history(bare, learn.TASK_CLASS) == []
        assert collapse_history.append(bare, task_class=learn.TASK_CLASS,
                                       score=1.0, anchor=1.0, now=NOW) is None
    finally:
        bare.close()
