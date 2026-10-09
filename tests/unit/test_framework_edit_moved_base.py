"""framework-edit: an implementer that moves HEAD fails with `impl_moved_base`.

The ground-truth harvest diffs the target against `pre-implementer-ref`. When
the implementer commits, rebases, resets or checks out another ref, the delta
carries every commit in between: sdd-i5-evidence-ledger-20261008122008 rebased
onto origin/main mid-run and shipped a 54-file diff for a 6-file change. The
implementer node now fails with the cause instead.
"""
from __future__ import annotations

import contextvars
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute_handlers as exh  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo_with_baseline(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pre-implementer-ref").write_text(base + "\n")
    return repo, run_dir, base


def test_head_on_the_starting_commit_with_uncommitted_edits_passes(tmp_path) -> None:
    repo, run_dir, _ = _repo_with_baseline(tmp_path)
    (repo / "a.txt").write_text("two\n")  # the normal framework-edit shape
    assert exh._implementer_moved_base(str(run_dir), str(repo)) == ""


def test_a_commit_by_the_implementer_is_a_moved_base(tmp_path) -> None:
    repo, run_dir, base = _repo_with_baseline(tmp_path)
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-qam", "WIP: implementer diff staged for rebase")
    why = exh._implementer_moved_base(str(run_dir), str(repo))
    assert "is not the run's starting commit" in why and base[:12] in why


def test_a_reset_onto_another_commit_is_a_moved_base(tmp_path) -> None:
    # The I5 shape: rebase onto a newer upstream, then soft-reset to it.
    repo, run_dir, _ = _repo_with_baseline(tmp_path)
    (repo / "b.txt").write_text("upstream\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "someone else's commit")
    assert "is not the run's starting commit" in exh._implementer_moved_base(str(run_dir), str(repo))


def test_a_stash_snapshot_baseline_with_head_unchanged_passes(tmp_path) -> None:
    # The dirty-tree shape _snapshot_pre_impl_ref creates: `git stash create`
    # records a WIP commit (2 parents: HEAD + index) as pre-implementer-ref.
    # HEAD stays on the starting commit, so the base never moved (2026-10-09,
    # run-1791543223-76795 failed impl_moved_base on exactly this).
    repo, run_dir, _ = _repo_with_baseline(tmp_path)
    (repo / "a.txt").write_text("pre-existing uncommitted dirt\n")  # tracked mod
    stash = _git(repo, "stash", "create")
    assert stash  # a dirty tree yields a WIP commit
    (run_dir / "pre-implementer-ref").write_text(stash + "\n")
    assert exh._implementer_moved_base(str(run_dir), str(repo)) == ""


def test_a_backward_reset_off_a_normal_baseline_is_a_moved_base(tmp_path) -> None:
    # Control for the stash rule: a NORMAL (1-parent) baseline whose first
    # parent is HEAD is a backward reset, not a dirt snapshot — still a moved
    # base. Only the 2-parent stash shape resolves to its first parent.
    repo, run_dir, _ = _repo_with_baseline(tmp_path)
    (repo / "b.txt").write_text("second commit\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "the baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    (run_dir / "pre-implementer-ref").write_text(baseline + "\n")
    first = _git(repo, "rev-parse", "HEAD~1")
    _git(repo, "reset", "-q", "--hard", first)  # HEAD == baseline's first parent
    why = exh._implementer_moved_base(str(run_dir), str(repo))
    assert "is not the run's starting commit" in why and baseline[:12] in why


def test_no_baseline_or_no_repo_defers(tmp_path) -> None:
    repo, run_dir, _ = _repo_with_baseline(tmp_path)
    (run_dir / "pre-implementer-ref").unlink()
    assert exh._implementer_moved_base(str(run_dir), str(repo)) == ""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pre-implementer-ref").write_text("0" * 40 + "\n")
    assert exh._implementer_moved_base(str(plain), str(plain)) == ""


def _ctx(tmp_path: Path, run_dir: Path, traces: list, dispatch) -> SimpleNamespace:
    return SimpleNamespace(
        node_id="implementer", node_type="implementer", task_class="framework_edit",
        recipe_eff="framework-edit", run_dir=str(run_dir), run_dir_eff=str(run_dir),
        root=str(tmp_path), node_desc="do it", learned="", plan_content="{}",
        artifact_context="", db="", run_id="r1",
        declared_output_path=lambda p: p, scope_guard=lambda: "", prepend=lambda: "",
        dispatch=dispatch, trace=lambda *a: traces.append(a), charge=lambda: None,
        publish_declared_outputs=lambda: True,
    )


def test_the_implementer_node_fails_impl_moved_base_before_the_harvest(tmp_path, monkeypatch) -> None:
    repo, run_dir, _ = _repo_with_baseline(tmp_path)
    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    harvested: list = []
    monkeypatch.setattr(exh, "_harvest_framework_edit_ground_truth",
                        lambda *a: harvested.append(a) or (True, ""))

    def dispatch(_prompt):
        (repo / "a.txt").write_text("two\n")
        _git(repo, "commit", "-qam", "implementer commits anyway")
        return 0, "done"

    traces: list = []
    # The handler publishes run-scoped vars (contextvar + os.environ) that
    # monkeypatch does not restore; run it in a copied context and put
    # os.environ back so later tests do not inherit this run's target.
    saved_env = dict(os.environ)
    try:
        rc, fr = contextvars.copy_context().run(
            exh._handle_implementer, _ctx(tmp_path, run_dir, traces, dispatch))
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    assert (rc, fr) == (1, "impl_moved_base")
    assert harvested == []  # refused before the harvest could ship a polluted diff
    assert traces and traces[-1][-1] == "impl_moved_base"
