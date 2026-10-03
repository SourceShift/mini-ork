"""Unit tests: ``mini-ork concord admit`` (Concord P2b — scope-overlap admission).

Drives ``concord.main(["admit", ...])`` in-process against a minimal temp-SQLite
``epics`` table and temp kickoff files, mirroring the pattern in
``test_concord_cli.py``. The table is the minimal compatible subset of the real
schema (``db/migrations/0001_core.sql``) — only the columns ``admit`` touches.
"""
from __future__ import annotations

import sqlite3

from mini_ork.orchestration import concord
from mini_ork.orchestration import concord_admission as ca


def _make_epics_table(db: str) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE epics ("
        " id TEXT PRIMARY KEY,"
        " title TEXT NOT NULL,"
        " status TEXT NOT NULL,"
        " kickoff_path TEXT,"
        " archived_at TEXT"
        ")"
    )
    con.commit()
    con.close()


def _seed_epic(db: str, epic_id: str, status: str, kickoff_path: str | None = None) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO epics (id, title, status, kickoff_path) VALUES (?,?,?,?)",
        (epic_id, epic_id, status, kickoff_path),
    )
    con.commit()
    con.close()


def _kickoff(paths: list[str]) -> str:
    lines = ["# epic", "", "## Files in scope"]
    for p in paths:
        lines.append(f"- `{p}`")
    return "\n".join(lines) + "\n"


# --- required test 1: absolute + repo-relative paths normalize to equal ---

def test_defer_when_scopes_overlap(tmp_path, monkeypatch, capsys):
    wt_dir = tmp_path / "worktrees"
    wt_root = wt_dir / "concord-admission"
    (wt_root / "mini_ork").mkdir(parents=True)
    monkeypatch.setenv("MINI_ORK_WORKTREES_DIR", str(wt_dir))

    db = str(tmp_path / "state.db")
    _make_epics_table(db)

    # In-progress epic B (blocks): repo-relative scope.
    kick_b = tmp_path / "b.md"
    kick_b.write_text(_kickoff(["mini_ork/foo.py"]), encoding="utf-8")
    _seed_epic(db, "B", "in progress", kickoff_path=str(kick_b))

    # Epic A (own, claimed 'in progress'): the same file as an absolute path.
    abs_path = str(wt_root / "mini_ork" / "foo.py")
    kick_a = tmp_path / "a.md"
    kick_a.write_text(_kickoff([abs_path]), encoding="utf-8")
    _seed_epic(db, "A", "in progress", kickoff_path=str(kick_a))

    monkeypatch.setenv("MO_EPIC_ID", "A")
    monkeypatch.setenv("MO_EPIC_KICKOFF", str(kick_a))

    rc = concord.main(["admit", "--db", db])
    out = capsys.readouterr().out

    assert rc == 75
    assert "scope overlap with in-progress B" in out
    assert "mini_ork/foo.py" in out


# --- required test 2: disjoint scopes admitted ---

def test_disjoint_scopes_admitted(tmp_path, monkeypatch):
    db = str(tmp_path / "state.db")
    _make_epics_table(db)

    kick_b = tmp_path / "b.md"
    kick_b.write_text(_kickoff(["mini_ork/bar.py"]), encoding="utf-8")
    _seed_epic(db, "B", "in progress", kickoff_path=str(kick_b))

    kick_a = tmp_path / "a.md"
    kick_a.write_text(_kickoff(["mini_ork/foo.py"]), encoding="utf-8")
    monkeypatch.setenv("MO_EPIC_ID", "A")
    monkeypatch.setenv("MO_EPIC_KICKOFF", str(kick_a))

    assert concord.main(["admit", "--db", db]) == 0


# --- required test 3: own id not compared against itself ---

def test_own_epic_not_compared_to_itself(tmp_path, monkeypatch):
    db = str(tmp_path / "state.db")
    _make_epics_table(db)

    kick_a = tmp_path / "a.md"
    kick_a.write_text(_kickoff(["mini_ork/foo.py"]), encoding="utf-8")
    # The only in-progress row is A itself (claimed by the scheduler).
    _seed_epic(db, "A", "in progress", kickoff_path=str(kick_a))

    monkeypatch.setenv("MO_EPIC_ID", "A")
    monkeypatch.setenv("MO_EPIC_KICKOFF", str(kick_a))

    assert concord.main(["admit", "--db", db]) == 0


# --- required test 4: component-aligned prefix overlap ---

def test_overlaps_directory_vs_file():
    assert ca.overlaps("mini_ork/", "mini_ork/foo.py") is True
    assert ca.overlaps("mini_ork2/x", "mini_ork/x") is False


# --- required test 5: glob overlap ---

def test_overlaps_glob():
    assert ca.overlaps("tests/unit/test_concord_*.py", "tests/unit/test_concord_cli.py") is True
    assert ca.overlaps("tests/unit/test_concord_*.py", "tests/unit/test_scheduler.py") is False


# --- required test 6: empty scope admits silently; missing other kickoff skipped ---

def test_empty_scope_admits_silently(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "state.db")
    _make_epics_table(db)

    kick_a = tmp_path / "a.md"
    kick_a.write_text("# epic\n\nno scope section here\n", encoding="utf-8")
    monkeypatch.setenv("MO_EPIC_ID", "A")
    monkeypatch.setenv("MO_EPIC_KICKOFF", str(kick_a))

    assert concord.main(["admit", "--db", db]) == 0
    assert capsys.readouterr().out == ""


def test_missing_other_kickoff_is_skipped(tmp_path, monkeypatch):
    db = str(tmp_path / "state.db")
    _make_epics_table(db)

    # In-progress epic B's kickoff file no longer exists.
    _seed_epic(db, "B", "in progress", kickoff_path=str(tmp_path / "gone.md"))

    kick_a = tmp_path / "a.md"
    kick_a.write_text(_kickoff(["mini_ork/foo.py"]), encoding="utf-8")
    monkeypatch.setenv("MO_EPIC_ID", "A")
    monkeypatch.setenv("MO_EPIC_KICKOFF", str(kick_a))

    assert concord.main(["admit", "--db", db]) == 0


# --- required test 7: corrupt DB path fails open ---

def test_corrupt_db_fails_open(tmp_path, monkeypatch, capsys):
    bad_db = tmp_path / "state.db"
    bad_db.write_text("this is not a sqlite database", encoding="utf-8")

    kick_a = tmp_path / "a.md"
    kick_a.write_text(_kickoff(["mini_ork/foo.py"]), encoding="utf-8")
    monkeypatch.setenv("MO_EPIC_ID", "A")
    monkeypatch.setenv("MO_EPIC_KICKOFF", str(kick_a))

    rc = concord.main(["admit", "--db", str(bad_db)])

    assert rc == 0
    assert "warning" in capsys.readouterr().err


# --- required test 8: parse_scope stops at next heading, ignores prose ---

def test_parse_scope_stops_at_next_heading_and_ignores_prose():
    text = (
        "# epic\n"
        "\n"
        "## Files in scope\n"
        "\n"
        "- `mini_ork/foo.py`\n"
        "- `mini_ork/bar.py`\n"
        "\n"
        "Some prose without paths in it.\n"
        "\n"
        "## Next section\n"
        "\n"
        "- `should_not_appear.py`\n"
    )
    assert ca.parse_scope(text) == ["mini_ork/foo.py", "mini_ork/bar.py"]


# --- direct unit coverage of normalize ---

def test_normalize_strips_repo_root():
    roots = ["/worktrees/slug"]
    assert ca.normalize("/worktrees/slug/mini_ork/foo.py", roots) == "mini_ork/foo.py"
    assert ca.normalize("mini_ork/foo.py", roots) == "mini_ork/foo.py"


def test_missing_db_path_is_never_created(tmp_path, monkeypatch, capsys):
    """Admission is read-only: a missing state DB path fails open and must NOT
    leave an empty database file behind."""
    from mini_ork.orchestration import concord_admission as ca
    k = tmp_path / "k.md"
    k.write_text("## Files in scope\n- `mini_ork/foo.py` — x\n")
    missing = tmp_path / "nope" / "state.db"
    ok, reason = ca.admit(str(missing), "E1", str(k), repo_roots=[])
    assert ok is True and reason == ""
    assert not missing.exists()
    assert "warning" in capsys.readouterr().err


def test_relative_kickoff_paths_resolve_against_mini_ork_root(tmp_path, monkeypatch):
    """Epics store repo-relative kickoff paths; they must resolve against
    MINI_ORK_ROOT even when the hook runs from another cwd."""
    import sqlite3
    from mini_ork.orchestration import concord_admission as ca
    root = tmp_path / "root"
    (root / "kickoffs" / "auto").mkdir(parents=True)
    (root / "kickoffs" / "auto" / "a.md").write_text("## Files in scope\n- `mini_ork/foo.py`\n")
    mine = tmp_path / "b.md"
    mine.write_text("## Files in scope\n- `mini_ork/foo.py`\n")
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE epics (id TEXT PRIMARY KEY, status TEXT, kickoff_path TEXT, archived_at TEXT)")
    con.execute("INSERT INTO epics VALUES ('A', 'in progress', 'kickoffs/auto/a.md', NULL)")
    con.commit(); con.close()
    monkeypatch.setenv("MINI_ORK_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    ok, reason = ca.admit(str(db), "B", str(mine), repo_roots=[])
    assert ok is False and "in-progress A" in reason
