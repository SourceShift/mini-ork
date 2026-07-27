"""Unit tests: mini_ork.cli.epics (bash parity halves removed; formerly vs bin/mini-ork-epics).

Roadmaps ingested/split through the port on a seeded state.db; epics +
epic_dependencies rows, kickoff files + kickoff_path, and list/priority
behavior are asserted semantically.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import epics as ep

ROADMAP = """# Roadmap

## Build the widget (id: widget-1)
Make the widget in `lib/widget.sh`.
depends on: base-1

## Base setup (id: base-1)
Set up the base.
"""


def _side(tmp_path, name, roadmap=ROADMAP):
    """A repo dir (so split writes into an isolated kickoffs/auto) + fresh db."""
    root = tmp_path / name
    root.mkdir()
    home = root / ".mini-ork"; home.mkdir()
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    (root / "roadmap.md").write_text(roadmap)
    return root, home, db


def _rows(db, sql):
    con = __import__("sqlite3").connect(db)
    r = con.execute(sql).fetchall()
    con.close()
    return r


_QE = "SELECT id, title, status FROM epics ORDER BY id"
_QD = ("SELECT from_epic_id, to_epic_id, kind FROM epic_dependencies "
       "ORDER BY from_epic_id, to_epic_id")


def test_ingest_db(tmp_path):
    rp, hp, db_p = _side(tmp_path, "p")
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    epics = _rows(db_p, _QE)
    assert ("base-1", "Base setup", "not started") in epics
    # widget-1 auto-blocked (unresolved hard dep on base-1)
    assert ("widget-1", "Build the widget", "blocked") in epics
    assert _rows(db_p, _QD) == [("base-1", "widget-1", "hard")]


def test_split_files_and_kickoff_path(tmp_path):
    rp, hp, db_p = _side(tmp_path, "p")
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    ep.main(["split", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    # kickoff files written; content carries the section body
    for eid, needle in (("widget-1", "widget"), ("base-1", "base")):
        fp = rp / "kickoffs" / "auto" / f"{eid}.md"
        assert fp.is_file(), f"missing kickoff {fp}"
        assert needle in fp.read_text().lower()
    q = "SELECT id, kickoff_path FROM epics ORDER BY id"
    rows = _rows(db_p, q)
    assert [r[0] for r in rows] == ["base-1", "widget-1"]
    for eid, path in rows:
        assert path.endswith(f"{eid}.md")
        assert (rp / path).is_file()


def test_priority(tmp_path):
    rp, hp, db_p = _side(tmp_path, "p")
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    buf = io.StringIO()
    with redirect_stdout(buf):
        ep.main(["priority", "base-1", "7"], db=db_p, root=str(rp))
    assert _rows(db_p, "SELECT priority FROM epics WHERE id='base-1'") == [(7,)]
    # non-integer rejected
    assert ep.main(["priority", "base-1", "x"], db=db_p, root=str(rp)) == 2


def test_skip_marker(tmp_path):
    roadmap = """# Roadmap

## Symptom (no-epic)
This context must not become an epic or kickoff.

## Epic A
Do A.

## Epic B
Do B.
"""
    rp, hp, db_p = _side(tmp_path, "p", roadmap)
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    ep.main(["split", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    assert not _rows(db_p, "SELECT id FROM epics WHERE id='symptom'")
    assert not (rp / "kickoffs" / "auto" / "symptom.md").exists()
    # the two real epics landed
    ids = [r[0] for r in _rows(db_p, "SELECT id FROM epics ORDER BY id")]
    assert ids == ["epic-a", "epic-b"]


def test_prose_dep_resolution(tmp_path):
    roadmap = """# Roadmap

## Epic 0 - verify the diagnosis
Verify the diagnosis.

## Epic 1
Implement the fix.
depends on: Epic 0
"""
    rp, hp, db_p = _side(tmp_path, "p", roadmap)
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    assert ("epic-0-verify-the-diagnosis", "epic-1", "hard") in _rows(db_p, _QD)


def test_unresolved_dep_warns_and_skips(tmp_path):
    roadmap = """# Roadmap

## Epic 1
Implement the fix.
depends on: NonexistentEpic
"""
    rp, hp, db_p = _side(tmp_path, "p", roadmap)
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    assert _rows(db_p, _QD) == []
    assert "WARNING" in stderr.getvalue()
    assert "NonexistentEpic" in stderr.getvalue()
    assert "1 dep(s) skipped" in stdout.getvalue()
