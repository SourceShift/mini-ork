"""Parity gate: mini_ork.ported.mini_ork_epics vs bin/mini-ork-epics.

Same roadmap ingested/split through the LIVE bash CLI and the port on separate
seeded state.dbs; epics + epic_dependencies rows, kickoff files + kickoff_path,
and list/priority output must match.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_epics as ep  # noqa: E402

BIN = REPO / "bin" / "mini-ork-epics"

ROADMAP = """# Roadmap

## Build the widget (id: widget-1)
Make the widget in `lib/widget.sh`.
depends on: base-1

## Base setup (id: base-1)
Set up the base.
"""


def _env(root, home, db):
    return {**os.environ, "MINI_ORK_ROOT": str(root), "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db}


def _side(tmp_path, name, roadmap=ROADMAP):
    """A repo copy (so split writes into an isolated kickoffs/auto) + fresh db."""
    root = tmp_path / name
    root.mkdir()
    (root / "lib").mkdir()
    # libs that bin/mini-ork-epics sources
    import shutil
    shutil.copy(REPO / "lib" / "epic_graph.sh", root / "lib" / "epic_graph.sh")
    shutil.copy(REPO / "lib" / "paths.sh", root / "lib" / "paths.sh")
    (root / "bin").mkdir()
    shutil.copy(BIN, root / "bin" / "mini-ork-epics")
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


def test_ingest_db_parity(tmp_path):
    rb, hb, db_b = _side(tmp_path, "b")
    rp, hp, db_p = _side(tmp_path, "p")
    subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "ingest", str(rb / "roadmap.md")],
                   env=_env(rb, hb, db_b), capture_output=True, text=True)
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    q_e = "SELECT id, title, status FROM epics ORDER BY id"
    q_d = "SELECT from_epic_id, to_epic_id, kind FROM epic_dependencies ORDER BY from_epic_id, to_epic_id"
    assert _rows(db_b, q_e) == _rows(db_p, q_e)
    assert _rows(db_b, q_d) == _rows(db_p, q_d)
    # widget-1 auto-blocked (unresolved hard dep on base-1)
    assert ("widget-1", "Build the widget", "blocked") in _rows(db_p, q_e)


def test_split_files_and_kickoff_path_parity(tmp_path):
    rb, hb, db_b = _side(tmp_path, "b")
    rp, hp, db_p = _side(tmp_path, "p")
    for r, h, d in ((rb, hb, db_b), (rp, hp, db_p)):
        subprocess.run(["bash", str(r / "bin" / "mini-ork-epics"), "ingest", str(r / "roadmap.md")],
                       env=_env(r, h, d), capture_output=True, text=True)
    subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "split", str(rb / "roadmap.md")],
                   env=_env(rb, hb, db_b), capture_output=True, text=True)
    ep.main(["split", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    # same kickoff files, byte-identical content
    for eid in ("widget-1", "base-1"):
        fb = (rb / "kickoffs" / "auto" / f"{eid}.md").read_text()
        fp = (rp / "kickoffs" / "auto" / f"{eid}.md").read_text()
        assert fb == fp
    q = "SELECT id, kickoff_path FROM epics ORDER BY id"
    assert _rows(db_b, q) == _rows(db_p, q)


def test_priority_parity(tmp_path):
    rb, hb, db_b = _side(tmp_path, "b")
    rp, hp, db_p = _side(tmp_path, "p")
    for r, h, d in ((rb, hb, db_b), (rp, hp, db_p)):
        subprocess.run(["bash", str(r / "bin" / "mini-ork-epics"), "ingest", str(r / "roadmap.md")],
                       env=_env(r, h, d), capture_output=True, text=True)
    ob = subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "priority", "base-1", "7"],
                        env=_env(rb, hb, db_b), capture_output=True, text=True).stdout
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        ep.main(["priority", "base-1", "7"], db=db_p, root=str(rp))
    assert ob.strip() == buf.getvalue().strip()
    assert _rows(db_b, "SELECT priority FROM epics WHERE id='base-1'") == \
        _rows(db_p, "SELECT priority FROM epics WHERE id='base-1'") == [(7,)]
    # non-integer rejected on both
    rb_rc = subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "priority", "base-1", "x"],
                           env=_env(rb, hb, db_b), capture_output=True, text=True).returncode
    assert rb_rc == ep.main(["priority", "base-1", "x"], db=db_p, root=str(rp)) == 2


def test_skip_marker_parity(tmp_path):
    roadmap = """# Roadmap

## Symptom (no-epic)
This context must not become an epic or kickoff.

## Epic A
Do A.

## Epic B
Do B.
"""
    rb, hb, db_b = _side(tmp_path, "b", roadmap)
    rp, hp, db_p = _side(tmp_path, "p", roadmap)
    subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "ingest", str(rb / "roadmap.md")],
                   env=_env(rb, hb, db_b), capture_output=True, text=True)
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "split", str(rb / "roadmap.md")],
                   env=_env(rb, hb, db_b), capture_output=True, text=True)
    ep.main(["split", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    q_e = "SELECT id, title, status FROM epics ORDER BY id"
    assert _rows(db_b, q_e) == _rows(db_p, q_e)
    assert not _rows(db_p, "SELECT id FROM epics WHERE id='symptom'")
    assert not (rb / "kickoffs" / "auto" / "symptom.md").exists()
    assert not (rp / "kickoffs" / "auto" / "symptom.md").exists()


def test_prose_dep_resolution_parity(tmp_path):
    roadmap = """# Roadmap

## Epic 0 - verify the diagnosis
Verify the diagnosis.

## Epic 1
Implement the fix.
depends on: Epic 0
"""
    rb, hb, db_b = _side(tmp_path, "b", roadmap)
    rp, hp, db_p = _side(tmp_path, "p", roadmap)
    subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "ingest", str(rb / "roadmap.md")],
                   env=_env(rb, hb, db_b), capture_output=True, text=True)
    ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    q_d = "SELECT from_epic_id, to_epic_id, kind FROM epic_dependencies ORDER BY from_epic_id, to_epic_id"
    assert _rows(db_b, q_d) == _rows(db_p, q_d)
    assert ("epic-0-verify-the-diagnosis", "epic-1", "hard") in _rows(db_p, q_d)


def test_unresolved_dep_warns_and_skips_parity(tmp_path):
    roadmap = """# Roadmap

## Epic 1
Implement the fix.
depends on: NonexistentEpic
"""
    rb, hb, db_b = _side(tmp_path, "b", roadmap)
    rp, hp, db_p = _side(tmp_path, "p", roadmap)
    ob = subprocess.run(["bash", str(rb / "bin" / "mini-ork-epics"), "ingest", str(rb / "roadmap.md")],
                        env=_env(rb, hb, db_b), capture_output=True, text=True)
    import io
    from contextlib import redirect_stderr, redirect_stdout
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        ep.main(["ingest", str(rp / "roadmap.md")], db=db_p, root=str(rp))
    q_d = "SELECT from_epic_id, to_epic_id, kind FROM epic_dependencies ORDER BY from_epic_id, to_epic_id"
    assert _rows(db_b, q_d) == _rows(db_p, q_d) == []
    assert "WARNING" in ob.stderr
    assert "NonexistentEpic" in ob.stderr
    assert "WARNING" in stderr.getvalue()
    assert "NonexistentEpic" in stderr.getvalue()
    assert "1 dep(s) skipped" in ob.stdout
    assert "1 dep(s) skipped" in stdout.getvalue()
