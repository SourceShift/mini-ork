"""Launch-time lane assignment — ``MO_LANE_OVERRIDES`` into the run snapshot.

``board start --lane <alias>=<lane>`` carries the assignment to the child in
the environment; ``snapshot_run_config`` is the one place that freezes a run's
lane policy, so it is also the one place the pin is applied. These tests pin
both halves: the parse (what reaches the snapshot) and the merge (what the
dispatch-time resolvers then read).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.dispatch import config_resolve


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A home with an effective agents.yaml and an empty run dir."""
    home = tmp_path / "proj" / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes:\n  worker: sonnet\n  reviewer: opus\nbudget:\n  per_run_usd: 9.5\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "no-root"))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    return home, run_dir


def _snapshot(run_dir: Path) -> str:
    assert config_resolve.snapshot_run_config(str(run_dir)) is True
    return (run_dir / "config" / "agents.yaml").read_text(encoding="utf-8")


def test_no_override_freezes_the_effective_file_verbatim(tree) -> None:
    _home, run_dir = tree
    assert "worker: sonnet" in _snapshot(run_dir)


def test_an_override_lands_in_the_runs_own_snapshot(tree, monkeypatch) -> None:
    _home, run_dir = tree
    monkeypatch.setenv(config_resolve.LANE_OVERRIDES_ENV, "worker=glm,reviewer=minimax")
    body = _snapshot(run_dir)
    assert "worker: glm" in body
    assert "reviewer: minimax" in body
    # Unrelated policy rides through untouched: the pin replaces only the
    # aliases it names.
    assert "per_run_usd: 9.5" in body


def test_the_override_never_rewrites_an_existing_snapshot(tree, monkeypatch) -> None:
    """A snapshot is the launch-time policy; a re-entrant execute keeps it."""
    _home, run_dir = tree
    (run_dir / "config").mkdir()
    (run_dir / "config" / "agents.yaml").write_text("lanes:\n  worker: sonnet\n",
                                                    encoding="utf-8")
    monkeypatch.setenv(config_resolve.LANE_OVERRIDES_ENV, "worker=glm")
    body = _snapshot(run_dir)
    assert body == "lanes:\n  worker: sonnet\n"


def test_malformed_pairs_are_dropped_not_raised(monkeypatch) -> None:
    """The CLI refuses a bad ``--lane``; this path must never fail a run that
    is already dispatched."""
    monkeypatch.setenv(config_resolve.LANE_OVERRIDES_ENV, "worker,,=x,reviewer=glm, =z")
    assert config_resolve.lane_overrides() == {"reviewer": "glm"}


def test_no_env_means_no_overrides(monkeypatch) -> None:
    monkeypatch.delenv(config_resolve.LANE_OVERRIDES_ENV, raising=False)
    assert config_resolve.lane_overrides() == {}
