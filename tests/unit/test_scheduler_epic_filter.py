"""Contracts for the scheduler's epic allow-list (``--epic`` / MO_SCHED_EPIC_FILTER).

The filter is a pure post-query intersection of the priority-ordered ready-set:
it must never change the ready-set SQL (priority inheritance, hard-dep gating,
attempt ordering), it must fall back to the env var only when no explicit list
is given, and an empty intersection must be an ordinary idle rather than an
error. These use a minimal temp-SQLite schema — no bash, no runner — so they
exercise pick_ready's contract in isolation.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import scheduler  # noqa: E402

_SCHEMA = """
CREATE TABLE epics (
  id           TEXT PRIMARY KEY,
  title        TEXT,
  status       TEXT,
  priority     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT,
  archived_at  TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER,
  recipe       TEXT,
  held_reason  TEXT,
  last_run_id  TEXT,
  kickoff_path TEXT,
  notes        TEXT
);
CREATE TABLE epic_dependencies (
  from_epic_id TEXT,
  to_epic_id   TEXT,
  kind         TEXT,
  resolved_at  TEXT
);
"""


def _init_db(path: Path) -> str:
    db = str(path)
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return db


def _seed(db: str, epic_id: str, *, status: str = "not started",
          priority: int = 0, created_at: str = "2026-01-01T00:00:00Z") -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epics (id,title,status,priority,created_at) VALUES (?,?,?,?,?)",
                (epic_id, epic_id, status, priority, created_at))
    con.commit()
    con.close()


def _dep(db: str, from_epic_id: str, to_epic_id: str, *, kind: str = "hard",
         resolved_at: str | None = None) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO epic_dependencies (from_epic_id,to_epic_id,kind,resolved_at) "
                "VALUES (?,?,?,?)", (from_epic_id, to_epic_id, kind, resolved_at))
    con.commit()
    con.close()


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    # The filter env var must not leak in from the ambient environment.
    monkeypatch.delenv("MO_SCHED_EPIC_FILTER", raising=False)
    return _init_db(tmp_path / "state.db")


def test_pick_ready_returns_full_ready_set_when_unfiltered(db):
    for i, epic_id in enumerate(("A", "B", "C")):
        _seed(db, epic_id, created_at=f"2026-01-01T00:00:0{i}Z")
    assert scheduler.pick_ready(db=db) == ["A", "B", "C"]


def test_only_intersects_the_ready_set(db):
    for i, epic_id in enumerate(("A", "B", "C")):
        _seed(db, epic_id, created_at=f"2026-01-01T00:00:0{i}Z")
    assert scheduler.pick_ready(db=db, only={"B"}) == ["B"]
    # Order is the ready-set's, not the filter's.
    assert scheduler.pick_ready(db=db, only={"C", "A"}) == ["A", "C"]


def test_only_naming_a_non_ready_epic_yields_empty(db):
    _seed(db, "A")
    _seed(db, "B", status="done")
    assert scheduler.pick_ready(db=db, only={"B"}) == []          # done: not ready
    assert scheduler.pick_ready(db=db, only={"ghost"}) == []      # never existed
    assert scheduler.pick_ready(db=db, only={"A", "ghost"}) == ["A"]


def test_only_does_not_change_ready_set_sql(db):
    # A depends on B (B must reach done first), and B is blocked, so only C is
    # ready; the filter must not unblock A by bypassing the dep gate.
    _seed(db, "A", priority=9)
    _seed(db, "B", status="blocked", priority=9)
    _seed(db, "C", priority=0)
    _dep(db, "B", "A", kind="hard")
    assert scheduler.pick_ready(db=db) == ["C"]
    assert scheduler.pick_ready(db=db, only={"A"}) == []
    assert scheduler.pick_ready(db=db, only={"C"}) == ["C"]


def test_env_filter_honoured_when_only_is_none(db, monkeypatch):
    for i, epic_id in enumerate(("A", "B", "C")):
        _seed(db, epic_id, created_at=f"2026-01-01T00:00:0{i}Z")
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", "A, C")
    assert scheduler.pick_ready(db=db) == ["A", "C"]


def test_env_filter_tolerates_whitespace_and_empty_segments(db, monkeypatch):
    for i, epic_id in enumerate(("A", "B")):
        _seed(db, epic_id, created_at=f"2026-01-01T00:00:0{i}Z")
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", "  A , , B ,")
    assert scheduler.pick_ready(db=db) == ["A", "B"]


def test_explicit_only_overrides_env(db, monkeypatch):
    for i, epic_id in enumerate(("A", "B")):
        _seed(db, epic_id, created_at=f"2026-01-01T00:00:0{i}Z")
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", "A")
    assert scheduler.pick_ready(db=db, only={"B"}) == ["B"]


def test_empty_filter_env_behaves_as_unfiltered(db, monkeypatch):
    _seed(db, "A")
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", "")
    assert scheduler.pick_ready(db=db) == ["A"]
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", " , ")
    assert scheduler.pick_ready(db=db) == ["A"]


def test_pick_ready_does_not_mutate_environ(db, monkeypatch):
    _seed(db, "A")
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", "A")
    before = dict(scheduler.os.environ)
    scheduler.pick_ready(db=db, only={"A"})
    assert dict(scheduler.os.environ) == before


def _cli_world(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "root"
    home = root / ".mini-ork"
    home.mkdir(parents=True)
    db = _init_db(home / "state.db")
    (root / "kickoffs").mkdir()
    (root / "recipes" / "epic-runner").mkdir(parents=True)
    for epic_id in ("A", "B"):
        _seed(db, epic_id)
        (root / "kickoffs" / f"{epic_id}.md").write_text(f"# epic {epic_id}\n", encoding="utf-8")
    return {"root": str(root), "home": str(home), "db": db}


def _run_cli(world: dict[str, str], argv: list[str], capsys) -> str:
    rc = scheduler.main(argv, db=world["db"], root=world["root"], home=world["home"])
    assert rc == 0
    return capsys.readouterr().out


def test_cli_epic_flag_restricts_dispatch(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MO_SCHED_EPIC_FILTER", raising=False)
    world = _cli_world(tmp_path)
    out = _run_cli(world, ["--once", "--dry-run", "--epic", "B"], capsys)
    assert "kickoffs/B.md" in out
    assert "kickoffs/A.md" not in out


def test_cli_epic_flag_repeatable(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MO_SCHED_EPIC_FILTER", raising=False)
    world = _cli_world(tmp_path)
    out = _run_cli(
        world, ["--once", "--dry-run", "--epic", "A", "--epic", "B"], capsys
    )
    # --once dispatches exactly one, and it must be from the allow-list.
    assert ("kickoffs/A.md" in out) ^ ("kickoffs/B.md" in out)


def test_cli_epic_flag_overrides_env(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MO_SCHED_EPIC_FILTER", "A")
    world = _cli_world(tmp_path)
    out = _run_cli(world, ["--once", "--dry-run", "--epic", "B"], capsys)
    assert "kickoffs/B.md" in out
    assert "kickoffs/A.md" not in out

