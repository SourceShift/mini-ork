"""The run's roots are pinned as soon as the profile exists — before planning.

Under ``--placement remote`` the planner lane is a remote spawn, and a remote
spawn without the run's pinned roots refuses to start. Roots used to be pinned
only at the pre-implementer baseline, so every remote run died in its planner
(Daytona e2e, run-1791910667-50438). These tests drive the real ``gen_profile``
+ ``_pin_run_roots_early`` the run lifecycle calls, and the planner-side
``_run_path_map`` lookup that refused."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mini_ork.cli import main as cli
from mini_ork.dispatch import providers


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _profile_for_run(tmp_path: Path, kickoff_dir: Path) -> Path:
    kickoff = kickoff_dir / "kickoff.md"
    kickoff.write_text("# Fix it\n\n## Verification commands\n- `python3 -m unittest`\n",
                       encoding="utf-8")
    agents = tmp_path / "agents.yaml"
    agents.write_text("lanes: {}\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    profile = run_dir / "run_profile.json"
    cli._gen_profile_or_reuse(kickoff, tmp_path, "code-fix", "code_fix", profile, agents, None)
    return profile


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "engine"))
    monkeypatch.delenv("MO_SHARED_DRIVE_BACKEND", raising=False)
    return monkeypatch


def test_roots_are_pinned_before_planning(env, tmp_path):
    target = _repo(tmp_path / "target")
    env.setenv("MO_TARGET_CWD", str(target))
    profile = _profile_for_run(tmp_path, target)
    assert "roots" not in json.loads(profile.read_text())      # gen_profile alone: none

    cli._pin_run_roots_early(profile, cli._prior_run_roots(profile))

    roots = json.loads(profile.read_text())["roots"]
    assert Path(roots["target"]).resolve() == target.resolve()
    assert roots["run_dir"] == str(profile.parent)
    # what the planner's remote dispatch looks up — the call that refused before
    path_map = providers._run_path_map({"MINI_ORK_RUN_DIR": str(profile.parent)})
    assert path_map is not None


def test_regenerated_profile_keeps_the_original_roots(env, tmp_path):
    first = _repo(tmp_path / "first")
    second = _repo(tmp_path / "second")
    env.setenv("MO_TARGET_CWD", str(first))
    profile = _profile_for_run(tmp_path, first)
    cli._pin_run_roots_early(profile, cli._prior_run_roots(profile))

    # a later lifecycle cycle (resume): MO_TARGET_CWD moved and gen_profile
    # rewrites the whole file, dropping the roots key
    env.setenv("MO_TARGET_CWD", str(second))
    prior = cli._prior_run_roots(profile)
    _profile_for_run(tmp_path, first)
    assert "roots" not in json.loads(profile.read_text())
    cli._pin_run_roots_early(profile, prior)

    roots = json.loads(profile.read_text())["roots"]
    assert Path(roots["target"]).resolve() == first.resolve()


def test_missing_profile_is_a_no_op(env, tmp_path):
    ghost = tmp_path / "runs" / "nope" / "run_profile.json"
    cli._pin_run_roots_early(ghost, None)
    assert not ghost.exists()
