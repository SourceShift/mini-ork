"""spec-tests must pytest the TARGET tree, never the engine that dispatched it.

``bin/mini-ork`` defaults ``MINI_ORK_TARGET_REPO`` to its own cwd, so a run
launched from the checkout hands the verifier the engine path as its target.
``python -m pytest`` then inserts that directory at ``sys.path[0]`` and the
blind spec tests import the engine's modules instead of the worktree's — a
pass on code the run never touched.

``mini_ork.runtime.run_roots`` already excludes engine roots for exactly this
reason. ``spec_cwd()`` did not, so the two disagreed about what "target" means.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIER = REPO / "recipes" / "framework-edit" / "verifiers" / "spec-tests.py"

_ENV_KEYS = (
    "MINI_ORK_ROOT",
    "MINI_ORK_ENGINE_ROOT",
    "MINI_ORK_TARGET_REPO",
    "MO_TARGET_CWD",
)


@pytest.fixture(scope="module")
def spec_tests():
    spec = importlib.util.spec_from_file_location("framework_edit_spec_tests", VERIFIER)
    assert spec is not None and spec.loader is not None, f"cannot load {VERIFIER}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_the_target_cwd_wins_when_it_is_not_the_engine(spec_tests, clean_env, tmp_path):
    engine = tmp_path / "engine"
    target = tmp_path / "target"
    engine.mkdir()
    target.mkdir()
    clean_env.setenv("MINI_ORK_ROOT", str(engine))
    clean_env.setenv("MO_TARGET_CWD", str(target))

    assert os.path.realpath(spec_tests.spec_cwd()) == os.path.realpath(target)


def test_a_target_repo_pointing_at_the_engine_is_skipped(spec_tests, clean_env, tmp_path):
    """The launch-from-checkout case: every env path names the engine."""
    engine = tmp_path / "engine"
    worktree = tmp_path / "worktree"
    engine.mkdir()
    worktree.mkdir()
    clean_env.setenv("MINI_ORK_ROOT", str(engine))
    clean_env.setenv("MINI_ORK_TARGET_REPO", str(engine))
    clean_env.chdir(worktree)

    assert os.path.realpath(spec_tests.spec_cwd()) == os.path.realpath(worktree)


def test_engine_root_env_is_also_excluded(spec_tests, clean_env, tmp_path):
    """MINI_ORK_ENGINE_ROOT names the engine too, even when ROOT is unset."""
    engine = tmp_path / "engine"
    worktree = tmp_path / "worktree"
    engine.mkdir()
    worktree.mkdir()
    clean_env.setenv("MINI_ORK_ENGINE_ROOT", str(engine))
    clean_env.setenv("MO_TARGET_CWD", str(engine))
    clean_env.chdir(worktree)

    assert os.path.realpath(spec_tests.spec_cwd()) == os.path.realpath(worktree)


def test_the_engine_is_the_last_resort_when_everything_points_at_it(
    spec_tests, clean_env, tmp_path
):
    """No other candidate exists; returning the engine beats returning nothing."""
    engine = tmp_path / "engine"
    engine.mkdir()
    clean_env.setenv("MINI_ORK_ROOT", str(engine))
    clean_env.chdir(engine)

    assert os.path.realpath(spec_tests.spec_cwd()) == os.path.realpath(engine)


def test_a_nonexistent_target_repo_is_not_used(spec_tests, clean_env, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    clean_env.setenv("MINI_ORK_TARGET_REPO", str(tmp_path / "gone"))
    clean_env.chdir(worktree)

    assert os.path.realpath(spec_tests.spec_cwd()) == os.path.realpath(worktree)
