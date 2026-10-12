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
from pathlib import Path

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
