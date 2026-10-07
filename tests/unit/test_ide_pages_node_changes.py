"""``mini-ork.ide_pages.node_changes.build_changes_view`` — node detail's
``changes`` view: per-node result table, run-cumulative files + diff, run
commits.

The fixture mirrors the kickoff's "Tests" §: a temp home with a five-node
DAG (planner → implementer → verifier_node → reviewer → publisher), an
``acp-diffs.json`` with two files, a verifier JSON + log, a review JSON
with two findings (``file:line``), a ``plan.json``, and a real git
workspace record with two commits on the run branch.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages.node import build_node
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-changes"
T0 = 1_791_000_000


WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: verifier_node, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\n")
    return h


def _seed_git_repo(project: Path) -> tuple[Path, str]:
    """Init a tiny git repo at ``project`` (the run's parent dir). One base
    commit, then a linked worktree at ``project/wt`` with two extra commits.

    Returns ``(worktree_path, base_sha)``. The worktree is a linked worktree
    so ``git log`` runs there; the project's main checkout has the same
    files (because they're tracked), so ``_project_file`` maps diff paths
    back onto ``project`` and returns a non-None ``abs``.
    """
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=project, check=True)
    (project / "README.md").write_text("# demo\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=project, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True
    ).stdout.strip()
    wt_path = project / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt/test", str(wt_path)], cwd=project, check=True)
    (wt_path / "a.py").write_text("def a(): return 1\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a"], cwd=wt_path, check=True)
    (wt_path / "b.py").write_text("def b(): return 2\n")
    subprocess.run(["git", "add", "."], cwd=wt_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add b"], cwd=wt_path, check=True)
    return wt_path, base_sha


def _seed(home: Path, *, repo: Path, base_sha: str) -> Path:
    """One run with five completed nodes + the artefacts the kickoff lists."""
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# demo\n")

    # Workspace record so ``run.workspace`` is populated (the changes view
    # reads ``base_sha`` / ``branch`` / ``path`` from it).
    (home / "worktrees").mkdir(parents=True, exist_ok=True)
    ws_record = home / "worktrees" / f"{RUN}.json"
    ws_record.write_text(
        json.dumps(
            {
                "run_id": RUN,
                "path": str(repo),
                "branch": "wt/test",
                "base_branch": "main",
                "base_sha": base_sha,
                "project": str(home.absolute().parent),
                "adopted": False,
                "clean_at_start": True,
                "home": str(home),
            }
        )
    )

    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            RUN,
            "demo-recipe",
            "executing",
            0.0,
            T0,
            T0 + 100,
            T0 + 90,
            "demo",
            str(kickoff),
            "latest",
            "tr-demo-1",
        ),
    )

    # Five completed nodes (planner, implementer, verifier_node, reviewer,
    # publisher). Each emits node_start then node_end so the run's DAG is
    # fully attributed.
    events = [
        ("planner", "planner", "planner", T0 + 10, T0 + 20),
        ("implementer", "implementer", "worker", T0 + 20, T0 + 50),
        ("verifier_node", "verifier", "verifier", T0 + 50, T0 + 60),
        ("reviewer", "reviewer", "reviewer", T0 + 60, T0 + 70),
        ("publisher", "publisher", "publisher", T0 + 70, T0 + 80),
    ]
    for i, (node, ntype, lane, ts, end) in enumerate(events):
        for event_kind, ts_emit in (("node_start", ts - 1), ("node_end", end)):
            payload = {"node_id": node, "node_type": ntype, "model_lane": lane, "finish_reason": "done"}
            con.execute(
                "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (f"ev-{i}-{event_kind}", RUN, event_kind, json.dumps(payload), ts_emit),
            )
    con.commit()
    con.close()

    # plan.json — the planner's two step entries (objective + a step).
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "objective": "demo planner objective",
                "steps": [
                    {"id": "implementer", "type": "implementer", "lane": "worker"},
                    {"id": "verifier_node", "type": "verifier", "lane": "verifier"},
                ],
            }
        )
    )

    # acp-diffs.json: two files (a.py + b.py) the implementer wrote.
    (run_dir / "acp-diffs.json").write_text(
        json.dumps(
            [
                {"path": str(repo / "a.py"), "old_text": "", "new_text": "def a(): return 1\n"},
                {"path": str(repo / "b.py"), "old_text": "", "new_text": "def b(): return 2\n"},
            ]
        )
    )

    # Verifier JSON + log.
    log_path = run_dir / "verifier_verifier_node.log"
    (run_dir / "verifier_verifier_node.json").write_text(
        json.dumps(
            {
                "verifier": "verifier_node",
                "pass": True,
                "post_rc": 0,
                "evidence_path": str(log_path),
                "error_summary": "",
            }
        )
    )
    log_path.write_text("[verifier] running\n[ok] verifier pass\n")

    # Review JSON: needs_revision + 2 findings with file:line.
    (run_dir / "review-reviewer.json").write_text(
        json.dumps(
            {
                "verdict": "needs_revision",
                "notes": [
                    {"title": "issue one", "file": str(repo / "a.py"), "line": 1, "severity": "high"},
                    {"title": "issue two", "file": str(repo / "b.py"), "line": 1, "severity": "medium"},
                ],
            }
        )
    )

    return run_dir


# ── per-node assertions (kickoff §"Tests") ─────────────────────────────────


def test_changes_view_planner_has_plan_items_no_files(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "planner", view="changes")
    assert out["ok"] is True
    assert out["view"] == "changes"
    items = out["result"]["items"]
    # Objective line plus the two step rows.
    titles = [str(it.get("t") or "") for it in items]
    assert any("demo planner objective" in t for t in titles)
    assert any("implementer" in t for t in titles)
    assert any("verifier_node" in t for t in titles)
    # Planner runs first → no code changes yet.
    assert out["files"] == []
    assert "No code changed" in out["diff_note"]


def test_changes_view_implementer_has_files_diff_commits(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "implementer", view="changes")
    assert out["ok"] is True
    assert len(out["files"]) == 2
    paths = {f["path"] for f in out["files"]}
    assert "wt/a.py" in paths
    assert "wt/b.py" in paths
    # Each file has an absolute path (it exists in the seeded worktree).
    for f in out["files"]:
        assert f["abs"] is not None and Path(f["abs"]).is_file()
    # The diff text is non-empty and contains both file paths.
    assert out["diff"]
    assert "a.py" in out["diff"]
    assert "b.py" in out["diff"]
    assert out["diff_note"] == ""
    # Commits: the two extra commits on the run branch.
    assert len(out["commits"]) == 2
    subjects = [c["subject"] for c in out["commits"]]
    assert "add a" in subjects
    assert "add b" in subjects
    # Each commit carries a sha, when, author, and at least one file entry.
    for c in out["commits"]:
        assert len(c["sha"]) == 40
        assert c["when"]
        assert c["author"]
        assert c["files"]


def test_changes_view_verifier_has_checks_with_log_paths(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "verifier_node", view="changes")
    items = out["result"]["items"]
    # First item is the pass verdict row (with the log path in `sub` or acts).
    first = items[0]
    assert str(first.get("t") or "").startswith("pass")
    blob = json.dumps(items)
    assert "verifier_verifier_node.log" in blob
    # The verifier's OK button points at the log file.
    acts_blob = json.dumps([a for it in items for a in (it.get("acts") or [])])
    assert "verifier_verifier_node.log" in acts_blob


def test_changes_view_reviewer_verdict_first_then_findings_with_paths(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "reviewer", view="changes")
    items = out["result"]["items"]
    # Verdict row first.
    assert "needs_revision" in str(items[0].get("t") or "")
    # Then the two findings.
    titles = [str(items[1].get("t") or ""), str(items[2].get("t") or "")]
    assert any("issue one" in t for t in titles)
    assert any("issue two" in t for t in titles)
    # Each finding has ``file:line`` in `sub`.
    subs = [items[1].get("sub") or "", items[2].get("sub") or ""]
    assert any("a.py:1" in s for s in subs)
    assert any("b.py:1" in s for s in subs)
    # Each finding has an Open action pointing at the absolute file path.
    for it in items[1:3]:
        acts_blob = json.dumps(it.get("acts") or [])
        assert "/a.py" in acts_blob or "/b.py" in acts_blob


def test_changes_view_publisher_has_commits(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "publisher", view="changes")
    assert len(out["commits"]) == 2
    assert out["commits_note"] == ""


# ── error / CLI path ───────────────────────────────────────────────────────


def test_changes_view_unknown_node_returns_ok_false(home: Path) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    out = build_node(home, RUN, "no-such-node", view="changes")
    assert out["ok"] is False
    assert "no node no-such-node" in out["error"]


def test_changes_view_runs_via_cli(home: Path, capsys) -> None:
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    rc = board_cmd.main(["node", RUN, "implementer", "--view", "changes", "--home", str(home)], "")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["view"] == "changes"
    assert len(out["files"]) == 2
    assert len(out["commits"]) == 2


def test_changes_view_diff_capped_when_run_diff_huge(home: Path) -> None:
    """Synthetic diff > 300 KB → ``diff_note`` flags the cap; ``diff`` ≤ cap."""
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    run_dir_path = _seed(home, repo=repo, base_sha=base_sha)
    # Stuff a > 300 KB single file into acp-diffs.json.
    huge = "x\n" * 200_000  # ~400 KB of single-line content
    (run_dir_path / "acp-diffs.json").write_text(
        json.dumps(
            [
                {"path": str(repo / "a.py"), "old_text": "", "new_text": huge},
            ]
        )
    )
    out = build_node(home, RUN, "implementer", view="changes")
    assert len(out["diff"]) <= 300_000
    # diff_note flags the cap when the cap fires.
    if len(out["diff"]) == 300_000:
        assert "capped" in out["diff_note"].lower() or "300" in out["diff_note"]


def test_changes_view_no_workspace_records_commits_note(home: Path) -> None:
    """No workspace + no publish/verdict/run-verdict commit → commits_note."""
    repo, base_sha = _seed_git_repo(home.absolute().parent)
    _seed(home, repo=repo, base_sha=base_sha)
    # Delete the workspace record so the git-log path falls through.
    (home / "worktrees" / f"{RUN}.json").unlink()
    # Reload by deleting the in-process cache would require re-importing
    # _load; the Run was loaded in the seed step but no test in this file
    # shares a Run object across cases (each test is fresh). The fixture
    # recreates everything; we just delete the workspace record before
    # calling build_node so the view reads it as missing.
    out = build_node(home, RUN, "publisher", view="changes")
    assert out["commits"] == []
    assert "No commits" in out["commits_note"]
