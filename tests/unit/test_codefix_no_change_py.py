"""A code-fix implementer that changes nothing must fail, not read "implemented".

Held-out pilot task mo-9a0cf68ccf: the implementer reported success, git saw no
change, and every downstream node ran on the untouched tree. framework-edit got
this guard in its ground-truth harvest; code-fix derives its changed-file list
in ``_write_implementer_summary`` and now fails the node on an empty one.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    return d


def test_summary_returns_empty_list_for_an_untouched_tree(tmp_path):
    repo, run_dir = _repo(tmp_path), _run_dir(tmp_path)

    files = ex._write_implementer_summary(str(run_dir), str(repo), "impl.log")

    assert files == []
    assert json.loads((run_dir / "implementer-summary.json").read_text())["status"] == "no_changes"


def test_summary_returns_the_changed_files(tmp_path):
    repo, run_dir = _repo(tmp_path), _run_dir(tmp_path)
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")

    files = ex._write_implementer_summary(str(run_dir), str(repo), "impl.log")

    assert sorted(Path(f).name for f in files) == ["a.py", "new.py"]
    assert json.loads((run_dir / "implementer-summary.json").read_text())["status"] == "implemented"


def test_summary_returns_none_when_git_cannot_answer(tmp_path):
    """Not a repo: 'could not derive' must never be read as 'changed nothing'."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert ex._write_implementer_summary(str(_run_dir(tmp_path)), str(plain), "impl.log") is None


def _dispatch_implementer(tmp_path, repo, monkeypatch, *, recipe, edit):
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"objective": "o"}))

    def fake_dispatch(task_class, node_type, prompt):
        if edit:
            (repo / "a.py").write_text("x = 3\n")
        return 0, "done"

    return ex.dispatch_node(
        ("impl", "implementer", "fix it", "", "serial", "", "worker", ""),
        root=str(tmp_path), run_dir=str(_run_dir(tmp_path)), plan_path=str(plan),
        task_class="code_fix", db="", run_id="r", dispatch_fn=fake_dispatch,
        recipe=recipe, workflow="")


def test_code_fix_implementer_with_no_changes_fails_the_node(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    rc, reason = _dispatch_implementer(tmp_path, repo, monkeypatch, recipe="code-fix", edit=False)

    assert (rc, reason) == (1, "impl_no_changes")


def test_code_fix_implementer_that_edits_still_succeeds(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    rc, _ = _dispatch_implementer(tmp_path, repo, monkeypatch, recipe="code-fix", edit=True)

    assert rc == 0


def test_other_recipes_are_not_held_to_it(tmp_path, monkeypatch):
    """Recipes whose implementer synthesizes rather than edits keep passing."""
    repo = _repo(tmp_path)

    rc, _ = _dispatch_implementer(tmp_path, repo, monkeypatch, recipe="research-synthesis", edit=False)

    assert rc == 0
