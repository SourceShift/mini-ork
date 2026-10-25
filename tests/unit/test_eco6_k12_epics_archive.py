"""Unit tests: `mini-ork epics archive` (ECOSYSTEM §6 K12).

The operator verb that closes an epic that will never run: it sets
``epics.archived_at`` — the column the scheduler's ready set, the epic graph,
the supervisor and `epics list` already filter on — and records why in
``notes``, WITHOUT touching ``status`` (so `trg_epics_no_revert_done` stays
satisfied).

Hermetic temp sqlite built straight from the repo migrations (0001_core for
`epics` + its status CHECK/triggers, 0027 for `epic_dependencies`) plus
`scheduler.ensure_retry_schema` for the runtime retry columns. Epics `a`
('not started'), `b` ('in progress') and `c` ('done') are seeded; the command
runs in process via `epics.main(..., db=..., root=...)`.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from mini_ork import scheduler
from mini_ork.cli import epics as ep

REPO = Path(__file__).resolve().parents[2]

# strftime('%Y-%m-%dT%H:%M:%fZ','now') -> 2026-10-25T11:34:56.789Z
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


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
    scheduler.ensure_priority_column(db)
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT INTO epics(id, title, status) VALUES('a', 'Epic A', 'not started')")
        con.execute("INSERT INTO epics(id, title, status, notes) "
                    "VALUES('b', 'Epic B', 'in progress', 'keep')")
        con.execute("INSERT INTO epics(id, title, status, notes) "
                    "VALUES('c', 'Epic C', 'done', 'pre-existing')")
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
        rc = ep.main(["archive", *args], db=db, root=str(tmp_path))
    return rc, out.getvalue(), err.getvalue()


def _row(db: str, epic_id: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT status, notes, archived_at FROM epics WHERE id=?",
            (epic_id,)).fetchone()
    finally:
        con.close()


def _list_ids(db: str, tmp_path) -> set[str]:
    out = StringIO()
    with redirect_stdout(out):
        assert ep.main(["list"], db=db, root=str(tmp_path)) == 0
    return {line.split("|")[0].strip() for line in out.getvalue().splitlines() if "|" in line}


def test_archive_happy_path(db, tmp_path):
    """Sets archived_at, keeps `status`, appends the operator note."""
    rc, out, err = _run(db, tmp_path, "a", "--note", "obsolete")
    assert rc == 0, err
    assert "archived a (status not started)" in out
    status, notes, archived_at = _row(db, "a")
    assert status == "not started"
    assert archived_at is not None and _TS_RE.match(archived_at), archived_at
    assert notes is not None and notes.endswith("[operator: archived: obsolete]"), notes
    assert "None" not in notes  # NULL notes handled, not stringified


def test_archive_hides_epic_from_list(db, tmp_path):
    """`epics list` no longer shows an archived epic."""
    assert "a" in _list_ids(db, tmp_path)
    rc, _out, err = _run(db, tmp_path, "a", "--note", "obsolete")
    assert rc == 0, err
    ids = _list_ids(db, tmp_path)
    assert "a" not in ids
    assert "b" in ids  # only the archived row disappears


def test_archive_removes_epic_from_ready_set(db, tmp_path):
    """The scheduler's ready set stops offering an archived epic.

    The control assertion matters: both ready-set readers swallow DB errors
    and return [], so "a is gone" would also hold for a database whose schema
    the query cannot read at all.
    """
    assert "a" in scheduler.pick_ready(db=db)
    rc, _out, err = _run(db, tmp_path, "a", "--note", "obsolete")
    assert rc == 0, err
    assert "a" not in scheduler.pick_ready(db=db)


def test_archive_done_epic_stays_done(db, tmp_path):
    """A `done` epic keeps its status — the trigger only guards a revert."""
    rc, out, err = _run(db, tmp_path, "c", "--note", "delivered elsewhere")
    assert rc == 0, err
    assert "archived c (status done)" in out
    status, notes, archived_at = _row(db, "c")
    assert status == "done"
    assert archived_at is not None
    assert notes.startswith("pre-existing")  # appended to, not replacing
    assert notes.endswith("[operator: archived: delivered elsewhere]")


def test_archive_refuses_in_progress_epic(db, tmp_path):
    """An epic with a run going is refused; the row is untouched."""
    rc, _out, err = _run(db, tmp_path, "b", "--note", "wont run")
    assert rc == 1
    assert "archive: b is in progress — stop its run first" in err
    assert _row(db, "b") == ("in progress", "keep", None)


def test_archive_requires_a_note(db, tmp_path):
    """A missing or whitespace-only note is refused before the DB is touched."""
    for args in (("a",), ("a", "--note", "   "), ("a", "--note")):
        rc, _out, err = _run(db, tmp_path, *args)
        assert rc == 2, args
        assert "archive: --note is required" in err
        assert _row(db, "a") == ("not started", None, None)


def test_archive_unknown_epic(db, tmp_path):
    """An unknown id is refused with rc 1."""
    rc, _out, err = _run(db, tmp_path, "nope", "--note", "x")
    assert rc == 1
    assert "archive: no such epic: nope" in err


def test_archive_twice_is_a_noop(db, tmp_path):
    """Archiving an archived epic reports the stored timestamp and adds no note."""
    rc, _out, err = _run(db, tmp_path, "a", "--note", "obsolete")
    assert rc == 0, err
    _status, first_notes, first_ts = _row(db, "a")

    rc, out, err = _run(db, tmp_path, "a", "--note", "obsolete")
    assert rc == 0, err
    assert "already archived at" in out
    assert first_ts in out

    status, notes, archived_at = _row(db, "a")
    assert archived_at == first_ts
    assert notes == first_notes
    assert notes.count("[operator: archived:") == 1


def test_archive_rejects_unknown_arguments(db, tmp_path):
    """Unknown extra arguments are refused with rc 2; the row is untouched."""
    for args in (("a", "--note", "x", "--flag"), ("a", "bogus", "--note", "x")):
        rc, _out, err = _run(db, tmp_path, *args)
        assert rc == 2, args
        assert "archive: unknown argument(s):" in err
        assert _row(db, "a") == ("not started", None, None)


def test_archive_requires_an_epic_id(db, tmp_path):
    """`epics archive` with no id is refused with rc 2."""
    err = StringIO()
    with redirect_stderr(err):
        rc = ep.main(["archive"], db=db, root=str(tmp_path))
    assert rc == 2
    assert "epic_id required" in err.getvalue()


def test_usage_documents_archive(db, tmp_path):
    """The usage block lists the new verb."""
    out = StringIO()
    with redirect_stdout(out):
        assert ep.main(["help"], db=db, root=str(tmp_path)) == 0
    text = out.getvalue()
    assert "archive <epic_id> --note TEXT" in text
    assert "Hide an epic that will never run (sets archived_at)" in text
