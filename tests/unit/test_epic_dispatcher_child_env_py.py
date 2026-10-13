"""Unit tests: the epic-runner dispatcher's child environment.

A spawned epic child is a *different* run in a *different* recipe. The
dispatcher used to hand it ``os.environ.copy()``, so the parent node's identity
leaked in — including ``MINI_ORK_RECOVERY_ACTIVE`` / ``MINI_ORK_RECOVERY_CLOSURE``.
``cli/execute.py`` reads those back to compute a closure, so a framework-edit
child recomputed an *epic-runner* closure, resolved it to ``epic_dispatcher`` (a
node its recipe does not have), and ran a two-node closure that skipped its own
implementer. The child then had no diff to publish and failed
(run-1791788217-61320's child). These tests pin the scrub.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_DISPATCHER = REPO / "recipes" / "epic-runner" / "lib" / "epic_dispatcher.py"


def _load():
    spec = importlib.util.spec_from_file_location("epic_dispatcher_under_test", _DISPATCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LEAKED_INTO_CHILD = (
    "MINI_ORK_RECOVERY_ACTIVE",
    "MINI_ORK_RECOVERY_CLOSURE",
    "MINI_ORK_RECOVERY_FROM",
    "MINI_ORK_RECIPE",
    "MINI_ORK_WORKFLOW",
    "MINI_ORK_TASK_CLASS",
    "MINI_ORK_RUN_DIR",
    "MO_NODE_ID",
    "MO_NODE_TYPE",
)


def test_child_env_drops_the_parent_node_and_recovery_context(monkeypatch, tmp_path):
    for name in _LEAKED_INTO_CHILD:
        monkeypatch.setenv(name, "leaked")

    env = _load()._child_env(tmp_path, tmp_path, "run-parent", False)

    for leaked in _LEAKED_INTO_CHILD:
        assert leaked not in env, f"{leaked} leaked into the child environment"


def test_child_env_keeps_repo_policy_and_sets_child_identity(monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_ORK_DB", raising=False)
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "225")
    monkeypatch.setenv("MO_SCHED_MAX_PARALLEL", "1")

    env = _load()._child_env(tmp_path, tmp_path, "run-parent", True)

    assert env["MO_DAILY_BUDGET_USD"] == "225"
    assert env["MO_SCHED_MAX_PARALLEL"] == "1"
    assert env["MINI_ORK_HOME"] == str(tmp_path)
    assert env["MINI_ORK_ROOT"] == str(tmp_path)
    assert env["MINI_ORK_DB"] == str(tmp_path / "state.db")
    assert env["MINI_ORK_EPIC_PUBLISH"] == "true"
    assert env["MINI_ORK_PARENT_RUN_ID"] == "run-parent"


def test_child_env_reflects_publish_gating(tmp_path):
    env = _load()._child_env(tmp_path, tmp_path, "run-parent", False)
    assert env["MINI_ORK_EPIC_PUBLISH"] == "false"


def test_clear_stale_child_baseline_drops_only_the_baseline(tmp_path):
    """A re-dispatched child must re-snapshot its baseline, not inherit the
    previous attempt's.

    ``execute._snapshot_pre_impl_ref`` is idempotent, so a child re-spawned into
    the same deterministic run dir kept an old ``pre-implementer-ref`` and failed
    ``impl_moved_base`` for a base that never moved. Only the two baseline files
    go; the child's checkpoints and artifacts survive the clear.
    """
    child = tmp_path / "runs" / "run-parent-epic"
    child.mkdir(parents=True)
    (child / "pre-implementer-ref").write_text("e28d17be3765\n")
    (child / "pre-implementer-untracked").write_text("stray.md\n")
    (child / "plan.json").write_text("{}\n")

    _load()._clear_stale_child_baseline(child)

    assert not (child / "pre-implementer-ref").exists()
    assert not (child / "pre-implementer-untracked").exists()
    assert (child / "plan.json").exists(), "checkpoints must survive the clear"


def test_clear_stale_child_baseline_is_a_noop_for_a_first_dispatch(tmp_path):
    """A first dispatch has no run dir yet; clearing must not raise."""
    _load()._clear_stale_child_baseline(tmp_path / "runs" / "run-parent-epic")


def test_prior_passed_verdict_reuses_a_child_that_already_passed(tmp_path):
    """Re-entering the dispatcher must not restart a child that already passed.

    ``recover --from-node epic_dispatcher`` replays every child in the wave. A
    re-spawned child re-implements into a target tree that already holds its own
    output, so its diff cannot apply and the run false-fails
    (impl_diff_unusable, run-1791785128-75074-wit-2-documents 2026-10-12). A
    prior verdict carrying ``pass: true`` is the evidence that it is reusable.
    """
    child = tmp_path / "runs" / "run-parent-epic"
    child.mkdir(parents=True)
    (child / "verdict.json").write_text(json.dumps({"pass": True, "files_changed": ["a.py"]}))

    assert _load()._prior_passed_verdict(child) == {"pass": True, "files_changed": ["a.py"]}


def test_prior_passed_verdict_is_none_without_a_verdict_file(tmp_path):
    """A missing verdict must NOT read as pass.

    ``_load_child_verdict`` infers ``pass=True`` from exit code 0, so reusing it
    here would skip a child that never ran. The helper reads the file instead.
    """
    child = tmp_path / "runs" / "run-parent-epic"
    child.mkdir(parents=True)

    assert _load()._prior_passed_verdict(child) is None


def test_prior_passed_verdict_is_none_when_the_prior_attempt_failed(tmp_path):
    child = tmp_path / "runs" / "run-parent-epic"
    child.mkdir(parents=True)
    (child / "verdict.json").write_text(json.dumps({"pass": False, "error": "impl_diff_unusable"}))

    assert _load()._prior_passed_verdict(child) is None


def test_prior_passed_verdict_is_none_when_the_file_is_unreadable(tmp_path):
    """An unreadable verdict is not evidence of a pass — and must not raise."""
    child = tmp_path / "runs" / "run-parent-epic"
    child.mkdir(parents=True)
    (child / "verdict.json").write_text("{not json")

    assert _load()._prior_passed_verdict(child) is None


# ── which repo a spawned child edits ────────────────────────────────────────
#
# run-1791785967-43108 declared the intake-delegation worktree as its target,
# but the dispatcher never passed it on. The child resolved its target from its
# kickoff copy under <home>/runs/, i.e. the engine checkout, wrote the Linear
# adapter there, and the parent's publisher found no authored patch.


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_child_target_prefers_the_declared_epic_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_EPIC_TARGET_REPO", str(tmp_path))
    plan = {"target_repo": "/somewhere/else"}

    assert _load()._child_target(plan, tmp_path / "run") == str(tmp_path.resolve())


def test_child_target_falls_back_to_the_plan_when_the_env_is_gone(monkeypatch, tmp_path):
    """A `recover` launched from another shell no longer carries the variable;
    the plan recorded what the planner was told."""
    monkeypatch.delenv("MINI_ORK_EPIC_TARGET_REPO", raising=False)

    assert _load()._child_target({"target_repo": str(tmp_path)}, tmp_path) == str(tmp_path.resolve())


def test_child_target_ignores_an_unexpanded_placeholder(monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_ORK_EPIC_TARGET_REPO", raising=False)
    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run_profile.json").write_text(json.dumps({"roots": {"target": str(tmp_path)}}))

    target = _load()._child_target({"target_repo": "$MINI_ORK_EPIC_TARGET_REPO"}, run_dir)

    assert target == str(tmp_path), "with nothing declared the child follows its parent"


def test_child_target_keeps_an_operator_pin(monkeypatch, tmp_path):
    monkeypatch.delenv("MINI_ORK_EPIC_TARGET_REPO", raising=False)
    monkeypatch.setenv("MO_TARGET_CWD", str(tmp_path))

    module = _load()
    assert module._child_target({}, tmp_path) == ""
    assert module._child_env(tmp_path, tmp_path, "p", False)["MO_TARGET_CWD"] == str(tmp_path)


def test_child_target_refuses_a_declared_repo_that_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_EPIC_TARGET_REPO", str(tmp_path / "gone"))

    with pytest.raises(ValueError, match="gone"):
        _load()._child_target({}, tmp_path)


def test_a_child_resolves_the_declared_worktree_not_the_engine(monkeypatch, tmp_path):
    """End to end through the ladder the child's nodes use (`resolve_run_roots`)."""
    from mini_ork.runtime.run_roots import resolve_run_roots

    monkeypatch.delenv("MO_TARGET_CWD", raising=False)
    engine = _git_repo(tmp_path / "engine")
    worktree = _git_repo(tmp_path / "worktree")
    home = engine / ".mini-ork"
    kickoff = home / "runs" / "run-p" / "children" / "run-p-epic" / "kickoff.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# epic\n")
    child_dir = home / "runs" / "run-p-epic"
    child_dir.mkdir(parents=True)
    (child_dir / "run_profile.json").write_text(json.dumps({"kickoff_path": str(kickoff)}))
    module = _load()

    unpinned = module._child_env(home, engine, "run-p", False)
    assert Path(resolve_run_roots(str(child_dir), env=unpinned).target).resolve() == engine

    monkeypatch.setenv("MINI_ORK_EPIC_TARGET_REPO", str(worktree))
    pinned = module._child_env(home, engine, "run-p", False,
                               target=module._child_target({}, child_dir))
    assert Path(resolve_run_roots(str(child_dir), env=pinned).target).resolve() == worktree
