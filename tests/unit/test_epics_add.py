"""Unit tests: `mini-ork epics add` and the no-fallback `resolve_kickoff` change.

Covers the acceptance matrix from the kickoff:
  1. `add` inserts one 'not started' row whose kickoff_path file exists and
     matches the source kickoff byte-for-byte.
  2. `add` refuses (rc=2, no row) for a missing --kickoff, a nonexistent path,
     and an empty/whitespace-only file.
  3. Two adds with the same title produce two distinct ids (the second is
     suffixed `-2`).
  4. `resolve_kickoff` returns None for an epic with no kickoff even when
     recipes/<recipe>/example-kickoff.md exists at the root.
  5. `dispatch_epic` on such an epic marks it 'escalated' with 'no kickoff'
     in its notes and never calls the runner.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import epics as ep
from mini_ork import scheduler


KICKOFF_BODY = "# Investigate the bug\n\nGoal: find root cause.\n"


def _init_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    scheduler.ensure_priority_column(db)
    return db


def _seed(db: str, epic_id: str, title: str | None = None,
          status: str = "not started", kickoff_path: str | None = None,
          priority: int = 0) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO epics(id, title, status, kickoff_path, priority, "
        "created_at) VALUES (?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (epic_id, title or epic_id, status, kickoff_path, priority),
    )
    con.commit(); con.close()


def _row(db: str, sql: str, args=()):
    con = sqlite3.connect(db)
    try:
        r = con.execute(sql, args).fetchone()
    finally:
        con.close()
    return r


def _rows(db: str, sql: str, args=()):
    con = sqlite3.connect(db)
    try:
        r = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return r


def _world(tmp_path: Path) -> dict[str, str]:
    """A repo dir + fresh db + a real source kickoff file."""
    root = tmp_path / "root"
    home = root / ".mini-ork"
    root.mkdir()
    home.mkdir()
    kickoff_src = root / "src-kickoff.md"
    kickoff_src.write_text(KICKOFF_BODY, encoding="utf-8")
    return {
        "root": str(root),
        "home": str(home),
        "db": _init_db(home),
        "kickoff_src": str(kickoff_src),
    }


# ── acceptance #1: add inserts one row and copies the kickoff ────────────────

def test_add_inserts_row_and_copies_kickoff(tmp_path: Path) -> None:
    w = _world(tmp_path)
    rc = ep.main(["add", "--title", "Investigate the bug",
                  "--kickoff", w["kickoff_src"]],
                 db=w["db"], root=w["root"])
    assert rc == 0
    rows = _rows(w["db"], "SELECT id, title, status, kickoff_path FROM epics")
    assert rows == [("investigate-the-bug", "Investigate the bug",
                     "not started", "kickoffs/auto/investigate-the-bug.md")]
    dest = Path(w["root"]) / "kickoffs" / "auto" / "investigate-the-bug.md"
    assert dest.is_file(), f"kickoff was not copied to {dest}"
    assert dest.read_text(encoding="utf-8") == KICKOFF_BODY


# ── acceptance #2: add refuses with rc=2 and inserts no row ──────────────────

def test_add_refuses_when_kickoff_flag_missing(tmp_path: Path) -> None:
    """No --kickoff at all: the row still lands (the dispatcher will escalate)."""
    w = _world(tmp_path)
    rc = ep.main(["add", "--title", "Plan only"], db=w["db"], root=w["root"])
    assert rc == 0  # add allows skipping kickoff; dispatch is what escalates
    # but the row was still inserted, and the kickoff column is NULL
    row = _row(w["db"], "SELECT id, kickoff_path FROM epics WHERE id='plan-only'")
    assert row == ("plan-only", None)
    assert not (Path(w["root"]) / "kickoffs" / "auto" / "plan-only.md").exists()


def test_add_refuses_when_kickoff_is_nonexistent(tmp_path: Path) -> None:
    w = _world(tmp_path)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = ep.main(["add", "--title", "Doomed",
                      "--kickoff", "/no/such/file.md"],
                     db=w["db"], root=w["root"])
    assert rc == 2
    assert "not found" in stderr.getvalue()
    assert _rows(w["db"], "SELECT id FROM epics WHERE id='doomed'") == []


def test_add_refuses_when_kickoff_is_empty(tmp_path: Path) -> None:
    w = _world(tmp_path)
    empty = Path(w["root"]) / "empty.md"; empty.write_text("", encoding="utf-8")
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = ep.main(["add", "--title", "Empty kickoff",
                      "--kickoff", str(empty)], db=w["db"], root=w["root"])
    assert rc == 2
    assert "empty" in stderr.getvalue()
    assert _rows(w["db"], "SELECT id FROM epics WHERE id='empty-kickoff'") == []


def test_add_refuses_when_kickoff_is_whitespace_only(tmp_path: Path) -> None:
    w = _world(tmp_path)
    blank = Path(w["root"]) / "blank.md"; blank.write_text("   \n\n  \n", encoding="utf-8")
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = ep.main(["add", "--title", "Whitespace kickoff",
                      "--kickoff", str(blank)], db=w["db"], root=w["root"])
    assert rc == 2
    assert _rows(w["db"], "SELECT id FROM epics WHERE id='whitespace-kickoff'") == []


def test_add_refuses_missing_title(tmp_path: Path) -> None:
    w = _world(tmp_path)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = ep.main(["add", "--kickoff", w["kickoff_src"]],
                     db=w["db"], root=w["root"])
    assert rc == 2
    assert "--title" in stderr.getvalue()
    assert _rows(w["db"], "SELECT 1 FROM epics") == []


def test_add_refuses_non_integer_priority(tmp_path: Path) -> None:
    w = _world(tmp_path)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        rc = ep.main(["add", "--title", "Bad prio", "--priority", "high"],
                     db=w["db"], root=w["root"])
    assert rc == 2
    assert "--priority" in stderr.getvalue()
    assert _rows(w["db"], "SELECT id FROM epics WHERE id='bad-prio'") == []


# ── acceptance #3: two adds with the same title get distinct ids ─────────────

def test_add_unique_ids_with_dash_suffix(tmp_path: Path) -> None:
    w = _world(tmp_path)
    title_args = ["--title", "Investigate the bug", "--kickoff", w["kickoff_src"]]
    rc1 = ep.main(["add", *title_args], db=w["db"], root=w["root"]); assert rc1 == 0
    rc2 = ep.main(["add", *title_args], db=w["db"], root=w["root"]); assert rc2 == 0
    rc3 = ep.main(["add", *title_args], db=w["db"], root=w["root"]); assert rc3 == 0

    ids = [r[0] for r in _rows(w["db"], "SELECT id FROM epics ORDER BY id")]
    assert ids == ["investigate-the-bug",
                   "investigate-the-bug-2",
                   "investigate-the-bug-3"]
    # Each one has its own kickoff file under the auto dir
    for eid in ids:
        assert (Path(w["root"]) / "kickoffs" / "auto" / f"{eid}.md").is_file()


def test_add_picks_up_existing_clash_in_db(tmp_path: Path) -> None:
    """A pre-existing row (from a previous run) must be respected too."""
    w = _world(tmp_path)
    _seed(w["db"], "investigate-the-bug")
    rc = ep.main(["add", "--title", "Investigate the bug",
                  "--kickoff", w["kickoff_src"]],
                 db=w["db"], root=w["root"])
    assert rc == 0
    ids = [r[0] for r in _rows(w["db"], "SELECT id FROM epics ORDER BY id")]
    assert ids == ["investigate-the-bug", "investigate-the-bug-2"]


# ── JSON output + bonus flags ────────────────────────────────────────────────

def test_add_with_priority_recipe_and_json(tmp_path: Path) -> None:
    w = _world(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ep.main(["add", "--title", "Investigate the bug",
                      "--kickoff", w["kickoff_src"],
                      "--priority", "5",
                      "--recipe", "framework-edit",
                      "--json"], db=w["db"], root=w["root"])
    assert rc == 0
    payload = json.loads(buf.getvalue().strip())
    assert payload["id"] == "investigate-the-bug"
    assert payload["status"] == "not started"
    assert payload["priority"] == 5
    assert payload["recipe"] == "framework-edit"
    assert payload["kickoff_path"] == "kickoffs/auto/investigate-the-bug.md"


# ── acceptance #4: resolve_kickoff has no example-kickoff fallback ────────────

def test_resolve_kickoff_ignores_recipe_example(tmp_path: Path) -> None:
    """The bash-order rung ``recipes/<recipe>/example-kickoff.md`` is gone.

    Without that fallback, an epic with no kickoff_path AND no on-disk
    kickoffs/<id>.md resolves to None — even when the recipe's example
    kickoff is sitting right there. The dispatch path is what escalates.
    """
    w = _world(tmp_path)
    # Seed an epic with no kickoff_path, then lay down a recipe example that
    # would have been the old third rung.
    _seed(w["db"], "bare-epic")
    recipe_dir = Path(w["root"]) / "recipes" / "framework-edit"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "example-kickoff.md").write_text(
        "# example\n\nbody\n", encoding="utf-8")

    assert scheduler.resolve_kickoff("bare-epic", w["root"], "framework-edit",
                                     db=w["db"]) is None


def test_resolve_kickoff_finds_kickoffs_dir(tmp_path: Path) -> None:
    """The ``kickoffs/<id>.md`` rung is kept — only the recipe example was removed."""
    w = _world(tmp_path)
    _seed(w["db"], "split-epic")
    kickoff = Path(w["root"]) / "kickoffs" / "split-epic.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# split\n\nbody\n", encoding="utf-8")

    assert scheduler.resolve_kickoff("split-epic", w["root"], "framework-edit",
                                     db=w["db"]) == str(kickoff)


def test_resolve_kickoff_prefers_explicit_kickoff_path(tmp_path: Path) -> None:
    """Column wins over on-disk ``kickoffs/<id>.md`` (matches the old order)."""
    w = _world(tmp_path)
    _seed(w["db"], "explicit-epic", kickoff_path="kickoffs/auto/explicit-epic.md")
    auto = Path(w["root"]) / "kickoffs" / "auto" / "explicit-epic.md"
    auto.parent.mkdir(parents=True)
    auto.write_text("# explicit\n\nbody\n", encoding="utf-8")

    assert scheduler.resolve_kickoff("explicit-epic", w["root"], "framework-edit",
                                     db=w["db"]) == str(auto)


# ── acceptance #5: dispatch escalates a kickoff-less epic and skips the runner ─

def test_dispatch_escalates_no_kickoff_without_launching(tmp_path: Path) -> None:
    """The runner_cmd test seam is irrelevant here: dispatch_epic returns at
    ``scheduler.py:657`` before _claim, before any hook, before any runner
    call. A non-empty runner_cmd survives the signature unchanged.
    """
    w = _world(tmp_path)
    _seed(w["db"], "no-kickoff")

    verdict, rc = scheduler.dispatch_epic(
        "no-kickoff", w["root"], w["home"], "framework-edit",
        db=w["db"], runner_cmd=["/bin/true"],  # never reached; tripwire only
    )

    assert (verdict, rc) == ("no_kickoff", 1)
    row = _row(w["db"], "SELECT status, notes FROM epics WHERE id='no-kickoff'")
    assert row[0] == "escalated"
    assert "no kickoff" in (row[1] or "")
