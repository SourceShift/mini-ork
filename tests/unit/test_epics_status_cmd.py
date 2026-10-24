"""Unit tests: `mini-ork epics status` (RSI Tier-0 #4).

Hermetic temp sqlite built straight from the repo migrations (0001_core for
`epics` + its status CHECK/triggers, 0027 for `epic_dependencies`) plus
``scheduler.ensure_retry_schema`` for the runtime retry columns
(``held_reason``, ...). Epics `a` ('not started') and `b` ('blocked') are
seeded with a hard a -> b edge — the shape ``epic_graph.on_done`` resolves —
and the command runs in process via ``epics.main(..., db=..., root=...)``.
"""
from __future__ import annotations

import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from mini_ork import scheduler
from mini_ork.cli import epics as ep
from mini_ork.orchestration import epic_graph

REPO = Path(__file__).resolve().parents[2]

ALLOWED_STATUSES = (
    "not started", "in progress", "in review", "done", "blocked", "escalated",
)


def _init_db(tmp_path: Path) -> str:
    db = str(tmp_path / "state.db")
    con = sqlite3.connect(db)
    try:
        for name in ("0001_core.sql", "0027_epic_dependencies.sql"):
            con.executescript((REPO / "db" / "migrations" / name).read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()
    scheduler.ensure_retry_schema(db)
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT INTO epics(id, title, status) VALUES('a', 'Epic A', 'not started')")
        con.execute("INSERT INTO epics(id, title, status) VALUES('b', 'Epic B', 'blocked')")
        con.execute("INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind) "
                    "VALUES('a', 'b', 'hard')")
        con.commit()
    finally:
        con.close()
    return db


@pytest.fixture()
def db(tmp_path):
    return _init_db(tmp_path)


def _run(db: str, tmp_path, *args):
    """Invoke the CLI in process, capturing stdout/stderr."""
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ep.main(["status", *args], db=db, root=str(tmp_path))
    return rc, out.getvalue(), err.getvalue()


def _row(db: str, epic_id: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT status, notes, held_reason FROM epics WHERE id=?",
            (epic_id,)).fetchone()
    finally:
        con.close()


def _dep(db: str, from_id: str, to_id: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT resolved_at FROM epic_dependencies "
            "WHERE from_epic_id=? AND to_epic_id=?", (from_id, to_id)).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------- done cascade

def test_done_cascades_and_records_note(db, tmp_path):
    rc, out, _err = _run(db, tmp_path, "a", "done", "--note", "merged 9a698e04")
    assert rc == 0
    assert "a: not started -> done" in out
    status_a, notes_a, _held = _row(db, "a")
    assert status_a == "done"
    assert "operator: not started -> done: merged 9a698e04" in notes_a
    # exactly the scheduler's on_done state: edge resolved, downstream released
    assert _dep(db, "a", "b")[0] is not None
    assert _row(db, "b")[0] == "not started"


def test_done_leaves_held_downstream_blocked(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epics(id, title, status, held_reason) "
                "VALUES('c', 'Epic C', 'blocked', 'needs human')")
    con.execute("INSERT INTO epic_dependencies(from_epic_id, to_epic_id, kind) "
                "VALUES('a', 'c', 'hard')")
    con.commit()
    con.close()
    assert _run(db, tmp_path, "a", "done", "--note", "merged")[0] == 0
    assert _dep(db, "a", "c")[0] is not None
    status_c, _notes_c, held_c = _row(db, "c")
    assert status_c == "blocked" and held_c == "needs human"


def test_done_calls_epic_graph_on_done(db, tmp_path, monkeypatch):
    calls = []
    real = epic_graph.on_done

    def _spy(epic_id, db=None):
        calls.append((epic_id, db))
        real(epic_id, db=db)

    monkeypatch.setattr(epic_graph, "on_done", _spy)
    assert _run(db, tmp_path, "a", "done", "--note", "merged")[0] == 0
    assert calls == [("a", db)]


# ------------------------------------------------------------ no cascade else

def test_non_done_status_does_not_cascade(db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(epic_graph, "on_done", lambda *a, **k: calls.append((a, k)))
    rc, out, _err = _run(db, tmp_path, "a", "escalated", "--note", "rolled back")
    assert rc == 0
    assert "a: not started -> escalated" in out
    assert calls == []
    status_a, notes_a, _held = _row(db, "a")
    assert status_a == "escalated"
    assert "operator: not started -> escalated: rolled back" in notes_a
    assert _dep(db, "a", "b")[0] is None
    assert _row(db, "b")[0] == "blocked"


# ---------------------------------------------------------------- held_reason

def test_blocked_to_not_started_clears_held_reason(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET held_reason='needs human' WHERE id='b'")
    con.commit()
    con.close()
    assert _run(db, tmp_path, "b", "not-started", "--note", "human cleared it")[0] == 0
    status_b, _notes_b, held_b = _row(db, "b")
    assert status_b == "not started"
    assert held_b is None


def test_moving_to_blocked_keeps_held_reason(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epics(id, title, status, held_reason) "
                "VALUES('d', 'Epic D', 'in progress', 'stale hold')")
    con.commit()
    con.close()
    assert _run(db, tmp_path, "d", "blocked", "--note", "parked")[0] == 0
    status_d, _notes_d, held_d = _row(db, "d")
    assert status_d == "blocked"
    assert held_d == "stale hold"


# ------------------------------------------------------------------ spellings

def test_hyphenated_spellings_normalize(db, tmp_path):
    for raw, spaced in (("in-progress", "in progress"),
                        ("in-review", "in review"),
                        ("not-started", "not started")):
        assert _run(db, tmp_path, "a", raw, "--note", "x")[0] == 0
        assert _row(db, "a")[0] == spaced


def test_spaced_status_accepted(db, tmp_path):
    assert _run(db, tmp_path, "a", "in progress", "--note", "x")[0] == 0
    assert _row(db, "a")[0] == "in progress"


# ------------------------------------------------------------ rejected inputs

def test_invalid_status_rejected_unchanged(db, tmp_path):
    before = _row(db, "a")
    rc, _out, err = _run(db, tmp_path, "a", "failed", "--note", "x")
    assert rc == 2
    assert "status: 'failed' is not one of:" in err
    for status in ALLOWED_STATUSES:
        assert status in err
    assert _row(db, "a") == before
    assert _dep(db, "a", "b")[0] is None


def test_missing_note_rejected(db, tmp_path):
    before = _row(db, "a")
    rc, _out, err = _run(db, tmp_path, "a", "in-progress")
    assert rc == 2
    assert "status: --note is required" in err
    assert _row(db, "a") == before


def test_whitespace_note_rejected(db, tmp_path):
    before = _row(db, "a")
    rc, _out, err = _run(db, tmp_path, "a", "in-progress", "--note", "  ")
    assert rc == 2
    assert "status: --note is required" in err
    assert _row(db, "a") == before


def test_note_flag_without_value_rejected(db, tmp_path):
    rc, _out, _err = _run(db, tmp_path, "a", "done", "--note")
    assert rc == 2
    assert _row(db, "a")[0] == "not started"


def test_unknown_epic_rc1(db, tmp_path):
    rc, _out, err = _run(db, tmp_path, "nosuch", "done", "--note", "x")
    assert rc == 1
    assert "unknown epic: nosuch" in err


def test_unknown_extra_argument_rc2(db, tmp_path):
    before = _row(db, "a")
    rc, _out, err = _run(db, tmp_path, "a", "done", "--note", "x", "stray-token")
    assert rc == 2
    assert "stray-token" in err
    rc, _out, err = _run(db, tmp_path, "a", "done", "--note", "x", "--bogus")
    assert rc == 2
    assert "--bogus" in err
    assert _row(db, "a") == before


# --------------------------------------------------------------------- no-op

def test_noop_current_status_changes_nothing(db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(epic_graph, "on_done", lambda *a, **k: calls.append((a, k)))
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET notes='existing note' WHERE id='a'")
    con.commit()
    con.close()
    rc, out, _err = _run(db, tmp_path, "a", "not started", "--note", "no reason")
    assert rc == 0
    assert "a: already not started" in out
    assert _row(db, "a")[1] == "existing note"
    assert calls == []
    assert _dep(db, "a", "b")[0] is None
    assert _row(db, "b")[0] == "blocked"


# --------------------------------------------------------------------- notes

def test_notes_append_preserves_existing(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET notes='seed' WHERE id='a'")
    con.commit()
    con.close()
    assert _run(db, tmp_path, "a", "in progress", "--note", "because")[0] == 0
    assert _row(db, "a")[1] == "seed [operator: not started -> in progress: because]"


# --------------------------------------------------------------------- usage

def test_help_lists_status(db, tmp_path):
    out, _err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(_err):
        rc = ep.main(["help"], db=db, root=str(tmp_path))
    assert rc == 0
    text = out.getvalue()
    assert "retry" in text
    assert "status <epic_id>" in text
    assert "--note" in text
