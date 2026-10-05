"""Hermetic unit tests for ``mini_ork.acp.race`` (Zed S7a).

The race helpers are pure (or only touch the providers registry + the
filesystem), so these tests run without an agent instance, a run
session, or a real ``bin/mini-ork`` spawn. They cover:

- ``known_lanes`` reads the effective registry (no hard-coding).
- ``parse_race_arg`` matches every shape the kickoff pins: explicit
  lane list, default lanes, ``MO_RACE_LANES`` override, unknown lane,
  single lane, empty task, and a task whose first word merely
  contains a comma.
- ``model_roles`` collects ``model_lane`` values from a temp recipe's
  ``workflow.yaml`` plus the canonical ``implementer``/``worker`` set.
- ``seed_run_config`` writes a snapshot AND the run's dispatch
  resolver reads it (the load-bearing pin through the real resolver).
- ``render_race_table`` formats the table per the kickoff's schema.

The recipes live in ``tmp_path`` so the home-first resolver matches
without touching the engine's working tree.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp import race as race_mod  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare ``.mini-ork`` home with a known providers registry.

    The providers YAML seeds ``sonnet``, ``glm``, and ``minimax`` so
    ``known_lanes`` matches the ``DEFAULT_LANES`` triple. Tests that
    want a different registry write a new file under the same path.
    """
    h = tmp_path / "home"
    (h / "config").mkdir(parents=True)
    (h / "config" / "providers.yaml").write_text(
        "providers:\n"
        "  sonnet: {kind: anthropic-native, family: anthropic}\n"
        "  glm: {kind: openai-compat, family: anthropic}\n"
        "  minimax: {kind: anthropic-native, family: anthropic}\n"
        "  codex: {kind: openai-chat, family: anthropic}\n",
        encoding="utf-8",
    )
    (h / "config" / "agents.yaml").write_text(
        "lanes:\n  worker: sonnet\n  implementer: sonnet\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(h))
    monkeypatch.delenv("MINI_ORK_ROOT", raising=False)
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    monkeypatch.delenv("MO_RACE_LANES", raising=False)
    return h


# ── known_lanes ──────────────────────────────────────────────────────────────


def test_known_lanes_reads_registry_sorted(home: Path) -> None:
    lanes = race_mod.known_lanes(home)
    assert lanes == sorted(lanes)
    assert {"sonnet", "glm", "minimax", "codex"} <= set(lanes)


def test_known_lanes_returns_empty_when_registry_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No providers file at home or root → ``[]``."""
    bare = tmp_path / "bare-home"
    bare.mkdir()
    engine_root = tmp_path / "engine-root"
    engine_root.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(bare))
    monkeypatch.setenv("MINI_ORK_ROOT", str(engine_root))
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    assert race_mod.known_lanes(bare) == []


def test_known_lanes_honours_home_shadow_over_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home with ``config/providers.yaml`` shadows the engine's."""
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    (home / "config" / "providers.yaml").write_text(
        "providers:\n  shadow_only: {kind: anthropic-native}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path / "engine-root"))
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    assert race_mod.known_lanes(home) == ["shadow_only"]


# ── parse_race_arg ───────────────────────────────────────────────────────────


def test_parse_race_arg_explicit_lanes(home: Path) -> None:
    out = race_mod.parse_race_arg("sonnet,glm write tests", home)
    assert out == (["sonnet", "glm"], "write tests")


def test_parse_race_arg_default_lanes_when_no_explicit(home: Path) -> None:
    """``DEFAULT_LANES`` is a triple; filtered to known, kept as-is."""
    out = race_mod.parse_race_arg("write a hello world", home)
    assert isinstance(out, tuple)
    lanes, task = out
    assert lanes == ["sonnet", "glm", "minimax"]
    assert task == "write a hello world"


def test_parse_race_arg_mo_race_lanes_env_overrides_default(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MO_RACE_LANES", "codex,minimax")
    out = race_mod.parse_race_arg("fix the bug", home)
    assert out == (["codex", "minimax"], "fix the bug")


def test_parse_race_arg_unknown_lane_in_explicit(home: Path) -> None:
    out = race_mod.parse_race_arg("sonnet,bogus write code", home)
    assert isinstance(out, str)
    assert "Unknown lane" in out
    assert "bogus" in out


def test_parse_race_arg_single_lane_is_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``MO_RACE_LANES=sonnet`` → one default lane → error."""
    monkeypatch.setenv("MO_RACE_LANES", "sonnet")
    out = race_mod.parse_race_arg("write code", home)
    assert isinstance(out, str)
    assert "at least two lanes" in out


def test_parse_race_arg_empty_task_error(home: Path) -> None:
    out = race_mod.parse_race_arg("", home)
    assert isinstance(out, str)
    assert "What should they do?" in out


def test_parse_race_arg_explicit_lanes_empty_task(
    home: Path,
) -> None:
    out = race_mod.parse_race_arg("sonnet,glm ", home)
    assert isinstance(out, str)
    assert "What should they do?" in out


def test_parse_race_arg_first_word_contains_comma_treats_as_task(
    home: Path,
) -> None:
    """``foo,bar baz`` → ``foo`` and ``bar`` aren't known lanes; the whole
    string is the task. Defaults apply."""
    out = race_mod.parse_race_arg("foo,bar baz qux", home)
    assert isinstance(out, tuple)
    lanes, task = out
    assert "foo" not in lanes and "bar" not in lanes
    assert task == "foo,bar baz qux"


def test_parse_race_arg_dedup_lanes_preserving_order(home: Path) -> None:
    out = race_mod.parse_race_arg("sonnet,glm,sonnet write x", home)
    assert isinstance(out, tuple)
    assert out[0] == ["sonnet", "glm"]


def test_parse_race_arg_caps_at_four_lanes(home: Path) -> None:
    """Explicit 4-lane list passes; explicit 5+ falls back to defaults.

    The kickoff pins ``2–4`` for the explicit lane list. 5 lanes means
    the user typed a comma list longer than the cap; we treat that as
    "not a lane list" and fall through to defaults rather than
    truncating silently.
    """
    out = race_mod.parse_race_arg(
        "sonnet,glm,minimax,codex write x", home
    )
    assert isinstance(out, tuple)
    lanes, task = out
    assert lanes == ["sonnet", "glm", "minimax", "codex"]
    assert task == "write x"
    # Five lanes → not a lane list, fall back to defaults (3 known).
    out = race_mod.parse_race_arg(
        "sonnet,glm,minimax,codex,sonnet write x", home
    )
    assert isinstance(out, tuple)
    lanes2, _ = out
    assert lanes2 == ["sonnet", "glm", "minimax"]


# ── model_roles ──────────────────────────────────────────────────────────────


def test_model_roles_from_temp_recipe(tmp_path: Path, home: Path) -> None:
    recipes = tmp_path / "recipes" / "audit"
    (recipes / "prompts").mkdir(parents=True)
    (recipes / "workflow.yaml").write_text(
        "nodes:\n"
        "  - {name: planner, type: planner, model_lane: opus}\n"
        "  - {name: editor, type: implementer, model_lane: codex}\n"
        "  - {name: checker, type: verifier, model_lane: sonnet}\n"
        "  - {name: editor2, type: implementer, model_lane: codex}\n",
        encoding="utf-8",
    )
    # Drop the recipe next to ``home`` so recipe_plan's home-first walk
    # finds it without a custom root.
    target = home / "recipes" / "audit"
    target.mkdir(parents=True)
    (target / "workflow.yaml").write_text(
        (recipes / "workflow.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    roles = race_mod.model_roles(home, "audit")
    assert roles[:2] == ["implementer", "worker"]
    assert "codex" in roles


def test_model_roles_unknown_recipe_returns_defaults(home: Path) -> None:
    roles = race_mod.model_roles(home, "does-not-exist")
    assert roles == ["implementer", "worker"]


# ── seed_run_config + resolver pin (load-bearing) ───────────────────────────


def test_seed_run_config_pins_the_lane_dispatch_resolves(home: Path, monkeypatch) -> None:
    """The home maps the roles to sonnet; the race pins glm. The pin must hold
    where a node's dispatch resolves its lane — ``llm_dispatch.
    resolve_lane_model``, which reads the home's agents.yaml merged with
    ``$MINI_ORK_AGENTS`` and never the run snapshot. (A real race whose glm
    contestant ran on sonnet is how this was found.)"""
    from mini_ork.dispatch.llm_dispatch import resolve_lane_model

    rid = "run-race-001"
    overlay = race_mod.seed_run_config(home, rid, "audit", "glm")
    assert overlay == home / "runs" / rid / "config" / "agents.race.yaml"
    # Without the overlay, dispatch sees the home's policy …
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)
    assert resolve_lane_model("worker", "", str(home)) != "glm"
    # … with it (the env the race launches each run with), the pin holds.
    monkeypatch.setenv("MINI_ORK_AGENTS", str(overlay))
    for role in race_mod.model_roles(home, "audit"):
        assert resolve_lane_model(role, "", str(home)) == "glm", role
    # The run snapshot carries the same pin for the run-dir-aware resolvers.
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(home / "runs" / rid))
    from mini_ork.steering import decision_service

    assert decision_service.default_lane("implementer") == "glm"


def test_seed_run_config_is_idempotent(home: Path) -> None:
    """A second seed overwrites with the new lane — pin order matters for the race."""
    race_mod.seed_run_config(home, "run-a", "audit", "glm")
    race_mod.seed_run_config(home, "run-a", "audit", "codex")
    text = (home / "runs" / "run-a" / "config" / "agents.yaml").read_text()
    assert "codex" in text


def test_seed_run_config_unknown_recipe_still_pins_defaults(home: Path) -> None:
    """Even without a workflow.yaml, the canonical roles get pinned."""
    race_mod.seed_run_config(home, "run-x", "no-such", "minimax")
    os.environ["MINI_ORK_RUN_DIR"] = str(home / "runs" / "run-x")
    try:
        from mini_ork.steering import decision_service

        assert decision_service.default_lane("implementer") == "minimax"
    finally:
        os.environ.pop("MINI_ORK_RUN_DIR", None)


# ── render_race_table ────────────────────────────────────────────────────────


def test_render_race_table_empty_rows() -> None:
    text = race_mod.render_race_table([])
    assert "model" in text and "result" in text


def test_render_race_table_marks_and_formats() -> None:
    rows = [
        {
            "lane": "sonnet",
            "run_id": "r1",
            "status": "verified",
            "removed": 3,
            "cost_usd": 0.42,
            "added": 12,
            "seconds": 192,
        },
        {
            "lane": "glm",
            "run_id": "r2",
            "status": "failed",
            "added": 0,
            "removed": 0,
            "cost_usd": 0.0,
            "seconds": 12,
        },
        {
            "lane": "minimax",
            "run_id": "r3",
            "status": "rolled back",
            "added": 0,
            "removed": 0,
            "cost_usd": 0.0,
            "seconds": 0,
        },
        {
            "lane": "opus",
            "run_id": "r4",
            "status": "executing",
            "added": 0,
            "removed": 0,
            "cost_usd": 0.0,
            "seconds": 0,
        },
    ]
    text = race_mod.render_race_table(rows)
    lines = text.splitlines()
    assert lines[0].startswith("| | model")
    assert lines[1].startswith("|---")
    # Row order is preserved.
    bodies = [ln for ln in lines if ln.startswith("| ") and "model" not in ln and "---" not in ln]
    assert "sonnet" in bodies[0] and "verified" in bodies[0] and "+12 −3" in bodies[0]
    assert "$0.42" in bodies[0]
    assert "3m 12s" in bodies[0]
    assert "✗" in bodies[1] and "glm" in bodies[1] and "failed" in bodies[1]
    assert "—" in bodies[1]  # no change / no cost
    assert "●" in bodies[3]  # still running


def test_render_race_table_change_dash_when_no_diff() -> None:
    text = race_mod.render_race_table(
        [
            {
                "lane": "sonnet",
                "run_id": "r1",
                "status": "verified",
                "added": 0,
                "removed": 0,
                "cost_usd": 0.0,
                "seconds": 30,
            }
        ]
    )
    assert "—" in text