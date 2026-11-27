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


# ── the pin must survive the resolvers, not just the snapshot write ───────


def _resolver_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   overlay: str) -> tuple[Path, Path]:
    """Home with template + personal overlay, and one run dir."""
    home = tmp_path / "proj" / ".mini-ork"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agents.yaml").write_text(
        "lanes:\n  kimi_lens: minimax\n  minimax_lens: minimax\n", encoding="utf-8")
    (home / "config" / "agents.local.yaml").write_text(overlay, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(home))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    return home, run_dir


def test_snapshot_records_the_pinned_aliases(tree, monkeypatch) -> None:
    """The snapshot names what was pinned, so a resolver can re-apply it."""
    _home, run_dir = tree
    monkeypatch.setenv(config_resolve.LANE_OVERRIDES_ENV, "worker=glm")
    body = _snapshot(run_dir)
    assert "lane_pins:" in body
    assert "worker: glm" in body


def test_env_override_beats_the_personal_overlay(tmp_path, monkeypatch) -> None:
    """THE regression: the overlay used to win over the run-scoped pin.

    ``_effective_lanes`` merges ``agents.local.yaml`` over the run snapshot, so
    a pin that only landed in ``lanes`` was reverted — lane preflight then
    probed the overlay's (dead) lane and blocked a run whose lane had been
    overridden. The pin is the most specific layer and must win both here and
    in ``resolve_lane_family`` (preflight reads the same function).
    """
    from mini_ork.dispatch import llm_dispatch

    _home, run_dir = _resolver_tree(
        tmp_path, monkeypatch,
        "lanes:\n  kimi_lens: minimax\n  minimax_lens: minimax\n")
    monkeypatch.setenv(config_resolve.LANE_OVERRIDES_ENV,
                       "kimi_lens=opus,minimax_lens=deepseek_flash")
    config_resolve.snapshot_run_config(str(run_dir))

    lanes = llm_dispatch._effective_lanes("", "") or {}
    assert lanes["kimi_lens"] == "opus", lanes
    assert lanes["minimax_lens"] == "deepseek_flash", lanes
    # resolve_lane_family is the preflight's entry point — same answer.
    assert llm_dispatch.resolve_lane_family("kimi_lens", "", "") == "opus"


def test_snapshot_pins_survive_without_the_env(tmp_path, monkeypatch) -> None:
    """A resume/recover process does not carry ``MO_LANE_OVERRIDES``.

    The pins recorded in the snapshot are what keeps the re-dispatched run on
    the lanes it was launched with, instead of falling back to the overlay.
    """
    from mini_ork.dispatch import llm_dispatch

    _home, run_dir = _resolver_tree(
        tmp_path, monkeypatch,
        "lanes:\n  kimi_lens: minimax\n  minimax_lens: minimax\n")
    monkeypatch.setenv(config_resolve.LANE_OVERRIDES_ENV, "kimi_lens=opus")
    config_resolve.snapshot_run_config(str(run_dir))
    monkeypatch.delenv(config_resolve.LANE_OVERRIDES_ENV, raising=False)

    lanes = llm_dispatch._effective_lanes("", "") or {}
    assert lanes["kimi_lens"] == "opus", lanes
    # Unpinned aliases still come from the overlay.
    assert lanes["minimax_lens"] == "minimax", lanes


def test_no_pin_leaves_the_overlay_in_charge(tmp_path, monkeypatch) -> None:
    """Without a run-scoped pin the overlay keeps winning — unchanged."""
    from mini_ork.dispatch import llm_dispatch

    _home, run_dir = _resolver_tree(
        tmp_path, monkeypatch,
        "lanes:\n  kimi_lens: deepseek_flash\n")
    monkeypatch.delenv(config_resolve.LANE_OVERRIDES_ENV, raising=False)
    config_resolve.snapshot_run_config(str(run_dir))

    lanes = llm_dispatch._effective_lanes("", "") or {}
    assert lanes["kimi_lens"] == "deepseek_flash", lanes
    assert lanes["minimax_lens"] == "minimax", lanes
