"""IDE page ``changes`` — run worktrees and the latest pre-push review."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from mini_ork import workspaces
from mini_ork.ide_pages import build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MO_WORKTREE_DIR", str(tmp_path / "wts"))
    monkeypatch.delenv("MO_WORKSPACE_MODE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q", "-b", "main")
    _git(proj, "config", "user.email", "t@example.com")
    _git(proj, "config", "user.name", "t")
    (proj / "app.py").write_text("x = 1\n")
    (proj / ".gitignore").write_text(".mini-ork/\n")
    _git(proj, "add", ".")
    _git(proj, "commit", "-qm", "init")
    home = proj / ".mini-ork"
    home.mkdir()
    rc, _out, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, err
    return proj


def _section(p: dict, kind: str, title: str) -> dict:
    return next(s for s in p["sections"] if s["type"] == kind and s["title"] == title)


def test_worktrees_lists_run_workspaces_with_merge_and_discard(project: Path, tmp_path: Path) -> None:
    home = project / ".mini-ork"
    ws = workspaces.create(project, home, "run-1791000000-aaaaaa")
    (ws.path / "app.py").write_text("x = 2\ny = 3\n")
    _git(project, "worktree", "add", "-q", "-b", "scratch", str(tmp_path / "scratch"))

    p = build_page(home, "changes")
    assert p["ok"] and p["title"] == "Changes & worktrees"
    assert [t["key"] for t in p["tabs"]] == ["worktrees", "review"]
    assert p["chips"] == [{"t": "2 open", "c": "sub"}]
    items = _section(p, "list", "Worktrees")["items"]
    run_item = items[0]
    assert run_item["t"] == ws.branch and run_item["mono"]
    assert "+2 −1" in run_item["sub"]
    labels = [a["label"] for a in run_item["acts"]]
    assert labels == ["Merge into main", "Discard", "Open run"]
    assert run_item["acts"][0]["do"]["cli"] == ["board", "merge", "run-1791000000-aaaaaa"]
    assert run_item["acts"][0]["do"]["confirm"] == "Merge this run's branch into main?"
    assert run_item["acts"][1]["do"]["cli"] == ["board", "discard", "run-1791000000-aaaaaa"]
    assert run_item["acts"][1]["do"]["confirm"]
    other = items[1]
    assert other["t"] == "scratch" and "not a mini-ork run" in other["sub"]
    assert all(a["label"] == "Reveal" for a in other["acts"])
    setup = _section(p, "list", "Setup")["items"]
    assert setup[0]["mc"] == "yellow" and "worktree-setup.sh is missing" in setup[0]["t"]
    assert "new worktree per task" in setup[1]["t"]
    json.dumps(p)


def test_no_worktrees_reads_as_an_empty_state(project: Path) -> None:
    p = build_page(project / ".mini-ork", "changes")
    assert p["chips"] == [{"t": "0 open", "c": "sub"}]
    assert _section(p, "list", "Worktrees")["items"][0]["t"] == "No open worktrees"


def test_review_tab_reads_the_latest_stored_review(project: Path) -> None:
    home = project / ".mini-ork"
    empty = build_page(home, "changes", "review")
    assert empty["sections"][0]["items"][0]["t"] == "No review yet"

    con = sqlite3.connect(home / "state.db")
    cur = con.execute(
        "INSERT INTO pre_push_reviews (reviewed_at, source_sha, target_branch, reviewer_mode, "
        "files_changed, lines_added, lines_removed, verdict, issues_open, rationale) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()) - 120, "abcdef1234567890", "main", "hybrid", 4, 412, 88, "warn", 3, "target=main"))
    rid = cur.lastrowid
    for lens, sev, title in [("heuristic.migration_safety", "high", "Migration 0042 cannot be rolled back"),
                             ("heuristic.todo_marker", "low", "TODO in upload.ts"),
                             ("llm.glm", "medium", "cors.ts changed without a paired test")]:
        con.execute("INSERT INTO pre_push_review_issues (review_id, lens, severity, file_path, title) "
                    "VALUES (?,?,?,?,?)", (rid, lens, sev, "src/x.ts", title))
    con.commit()
    con.close()

    p = build_page(home, "changes", "review")
    summary = next(s for s in p["sections"] if s["type"] == "kv")
    assert summary["title"] == f"main…abcdef12 · review #{rid}"
    kv = {i["k"]: i for i in summary["items"]}
    assert kv["Verdict"]["v"] == "warn" and kv["Verdict"]["c"] == "yellow"
    assert kv["Open issues"]["v"] == "3" and kv["Diff"]["v"] == "+412 −88"
    lenses = {i["t"]: i for i in _section(p, "list", "Lenses")["items"]}
    assert lenses["migration safety"]["mc"] == "red"
    assert lenses["added TODOs"]["mc"] == "yellow"
    assert lenses["bash syntax"]["mc"] == "green"
    assert lenses["LLM panel · glm"]["mc"] == "green"
    issues = _section(p, "table", "Issues")
    assert issues["head"] == ["severity", "issue"]
    assert [r["cells"][0]["t"] for r in issues["rows"]] == ["high", "medium", "low"]
