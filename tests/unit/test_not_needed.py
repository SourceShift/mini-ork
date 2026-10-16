"""``board not-needed`` — the terminal state for a run the operator closed.

Pins the marker module and the control-plane write together, because they are
one gesture: the marker (``<run_dir>/not-needed.json``) is what makes the
closure *reversible*, and the status column is what makes it *final*. A
regression in either half is invisible from the other, so each case here drives
the public verb and then checks both.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork import not_needed
from mini_ork.stores import migrate as mig
from mini_ork.web import control
from mini_ork.web.db import db_for

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    (h / "runs").mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, run_id: str, status: str, notes: str | None = None) -> None:
    con = sqlite3.connect(home / "state.db")
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "task_class, kickoff_path, workflow_version, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, 0.5, now, now, "code_fix", "", "latest", notes),
    )
    con.commit()
    con.close()
    (home / "runs" / run_id).mkdir(parents=True, exist_ok=True)


def _row(home: Path, run_id: str) -> dict:
    con = sqlite3.connect(home / "state.db")
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())
    con.close()
    return row


def test_close_sets_status_and_leaves_a_reversible_marker(home: Path) -> None:
    _seed(home, "r1", "failed", notes="impl_moved_base: HEAD moved")
    out = control.mark_not_needed(home, db_for(home), "r1")

    assert out["ok"] and out["mode"] == "closed" and out["previous_status"] == "failed"
    row = _row(home, "r1")
    assert row["status"] == "not_needed"
    # An existing reason must survive; the closure appends, never overwrites.
    assert row["notes"].endswith("impl_moved_base: HEAD moved; not-needed")

    marker = not_needed.read(home / "runs" / "r1")
    assert marker is not None
    assert marker["previous_status"] == "failed"  # what undo will restore
    assert marker["status"] == "not_needed"


def test_reclosing_an_already_closed_run_is_unchanged(home: Path) -> None:
    _seed(home, "r1", "failed")
    control.mark_not_needed(home, db_for(home), "r1")
    marker_before = not_needed.read(home / "runs" / "r1")

    again = control.mark_not_needed(home, db_for(home), "r1")

    assert again["mode"] == "unchanged"
    # Re-stamping would have overwritten previous_status with "not_needed",
    # destroying the only record of what to restore.
    assert not_needed.read(home / "runs" / "r1") == marker_before
    assert _row(home, "r1")["status"] == "not_needed"


def test_undo_restores_the_previous_status_and_clears_the_marker(home: Path) -> None:
    _seed(home, "r1", "failed")
    control.mark_not_needed(home, db_for(home), "r1")

    out = control.mark_not_needed(home, db_for(home), "r1", undo=True)

    assert out["ok"] and out["mode"] == "restored" and out["status"] == "failed"
    assert _row(home, "r1")["status"] == "failed"
    assert not_needed.read(home / "runs" / "r1") is None


def test_undo_without_a_marker_refuses(home: Path) -> None:
    _seed(home, "r1", "failed")  # never closed
    out = control.mark_not_needed(home, db_for(home), "r1", undo=True)
    assert out["ok"] is False
    assert _row(home, "r1")["status"] == "failed"


def test_unknown_run_is_not_found(home: Path) -> None:
    out = control.mark_not_needed(home, db_for(home), "no-such-run")
    assert out["ok"] is False and out["error"] == "task_run not found"


def test_a_close_undo_close_cycle_leaves_one_breadcrumb(home: Path) -> None:
    """The reversible verb must not accumulate its own note.

    Undo reverses the status, so it has to reverse the breadcrumb too — else a
    close/undo/close cycle reads ``…; not-needed; not-needed``, two closures
    that never happened. The pre-existing reason is the control: only the
    closure's own trailing segment may be touched.
    """
    _seed(home, "r1", "failed", notes="impl_moved_base: HEAD moved")
    db = db_for(home)

    for _ in range(3):
        control.mark_not_needed(home, db, "r1")
        assert _row(home, "r1")["notes"].count("not-needed") == 1
        control.mark_not_needed(home, db, "r1", undo=True)

    assert _row(home, "r1")["notes"] == "impl_moved_base: HEAD moved"


def test_a_reason_is_recorded_in_both_places(home: Path) -> None:
    _seed(home, "r1", "executing")
    control.mark_not_needed(home, db_for(home), "r1", reason="superseded by r2")

    assert _row(home, "r1")["notes"].endswith("not-needed: superseded by r2")
    marker = not_needed.read(home / "runs" / "r1")
    assert marker is not None and marker["reason"] == "superseded by r2"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, None),
        ("", None),
        ("plain reason", "plain reason"),
        ("plain reason; not-needed", "plain reason"),
        ("plain reason; not-needed; not-needed", "plain reason"),
        ("plain reason; not-needed: why", "plain reason"),
        ("not-needed", None),
        ("not-needed; not-needed", None),
        # Only a *trailing* segment is stripped; an earlier one is history.
        ("plain; not-needed; killed-by-user", "plain; not-needed; killed-by-user"),
    ],
)
def test_strip_trailing_note_honours_the_tag_and_the_end(
    body: str | None, expected: str | None
) -> None:
    assert control._strip_trailing_note(body, "not-needed") == expected


def test_reading_a_malformed_marker_is_absent(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    path = run_dir / not_needed.MARKER_NAME

    assert not_needed.read(run_dir) is None          # missing
    assert not_needed.read(None) is None             # no dir at all
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert not_needed.read(run_dir) is None          # right JSON, wrong shape
    path.write_text("{ not json", encoding="utf-8")
    assert not_needed.read(run_dir) is None          # unparseable

    assert not_needed.clear(run_dir) is True         # one was there
    assert not_needed.clear(run_dir) is False        # …and now it is not
