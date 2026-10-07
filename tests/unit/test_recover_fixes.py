"""Recovery fixes found while resuming real runs (2026-10-07).

* ``recover`` takes the recipe from the run's ``task_runs`` row: the
  profiler's ``run_profile.json`` recipe can differ from what ran.
* A ``review-diff-noop.json`` left by an earlier attempt must not block a
  later attempt whose diff is real.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from mini_ork.cli import execute as ex
from mini_ork.recovery import planner as rp


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "B.txt").write_text("B original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def _summary(run_dir: Path, worktree: Path, files: list[str]) -> None:
    (run_dir / "implementer-summary.json").write_text(json.dumps({
        "status": "implemented",
        "worktree_path": str(worktree),
        "files_changed": files,
    }))


def test_recover_takes_the_recipe_from_task_runs_not_the_profile(tmp_path, monkeypatch):
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-x"
    run_dir.mkdir(parents=True)
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": "docs"}))
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, recipe TEXT)")
    con.execute("INSERT INTO task_runs VALUES ('run-x', 'framework-edit')")
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_RECIPE", raising=False)
    monkeypatch.delenv("MINI_ORK_WORKFLOW", raising=False)
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)

    _run_dir, _db, workflow, recipe = rp._resolve_default_paths("run-x")

    assert recipe == "framework-edit"
    assert workflow.endswith("framework-edit/workflow.yaml")


def test_recipe_falls_back_to_the_profile_without_a_ledger_row(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"recipe": "docs"}))
    assert rp._recipe_from_task_runs(str(tmp_path / "missing.db"), "run-x") == ""
    assert rp._recipe_from_run_profile(str(run_dir)) == "docs"


def test_a_stale_noop_marker_is_cleared_when_the_diff_is_real(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    ex._capture_pre_impl_baseline(str(run_dir))
    (repo / "B.txt").write_text("B RESTORED EDIT\n")
    _summary(run_dir, repo, [str(repo / "B.txt")])
    # Left by an earlier attempt that ran before the work was restored.
    (run_dir / "review-diff-noop.json").write_text('{"status": "no_op"}\n')

    ex._assemble_reviewer_inputs(str(run_dir))

    assert "B RESTORED EDIT" in (run_dir / "review-diff.patch").read_text()
    assert not (run_dir / "review-diff-noop.json").exists()
