"""Reflect lane resolution must read the effective policy, not the raw template.

``_resolve_reflect_model`` read ``<home>/config/agents.yaml`` then
``<root>/config/agents.yaml`` raw, so it ignored both the per-user overlay
(``agents.local.yaml`` / ``$MINI_ORK_AGENTS``) and the run-dir snapshot that
dispatch honours. Gradient extraction then landed on the template's lane even
when the run's effective policy named another one.

Kickoff source: RSI Tier-0 #5 (loop defect D1).
"""
import inspect

import pytest

from mini_ork.cli import reflect as reflect_mod
from mini_ork.cli.reflect import _resolve_reflect_model

BASE_POLICY = "lanes: {reflector: codex, worker: codex}\n"


@pytest.fixture(autouse=True)
def _no_run_dir_or_overlay(monkeypatch):
    """Baseline: no run snapshot and no ambient overlay, as dispatch's own
    lane tests assume."""
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    monkeypatch.delenv("MINI_ORK_AGENTS", raising=False)


def _home(tmp_path, overlay=None, base=BASE_POLICY):
    home = tmp_path / "home"
    cfg = home / "config"
    cfg.mkdir(parents=True)
    (cfg / "agents.yaml").write_text(base)
    if overlay is not None:
        (cfg / "agents.local.yaml").write_text(overlay)
    return str(home)


def test_local_overlay_wins_over_template(tmp_path):
    home = _home(tmp_path, overlay="lanes:\n  reflector: glm\n")
    assert _resolve_reflect_model("reflector", home) == "glm"


def test_mini_ork_agents_env_wins(tmp_path, monkeypatch):
    home = _home(tmp_path)
    env_file = tmp_path / "env-agents.yaml"
    env_file.write_text("lanes:\n  reflector: minimax\n")
    monkeypatch.setenv("MINI_ORK_AGENTS", str(env_file))
    assert _resolve_reflect_model("reflector", home) == "minimax"


def test_run_dir_snapshot_wins(tmp_path, monkeypatch):
    home = _home(tmp_path)
    run_dir = tmp_path / "rd"
    (run_dir / "config").mkdir(parents=True)
    (run_dir / "config" / "agents.yaml").write_text(
        "lanes:\n  reflector: deepseek_flash\n")
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    assert _resolve_reflect_model("reflector", home) == "deepseek_flash"


def test_no_overlay_keeps_todays_answers(tmp_path):
    home = _home(tmp_path)
    assert _resolve_reflect_model("reflector", home) == "codex"
    # Unknown lane falls back to lanes.reflector, as the raw-template reader did.
    assert _resolve_reflect_model("custom_lane", home) == "codex"


def test_no_policy_returns_lane_verbatim(tmp_path, monkeypatch):
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setattr(reflect_mod, "MINI_ORK_ROOT", tmp_path / "missing-root")
    assert _resolve_reflect_model("reflector", str(empty_home)) == "reflector"


def test_broken_overlay_returns_lane_and_does_not_raise(tmp_path, capsys):
    home = _home(tmp_path, overlay="lanes: [unclosed\n")
    assert _resolve_reflect_model("reflector", home) == "reflector"


def test_broken_overlay_announces_on_stderr(tmp_path, capsys):
    home = _home(tmp_path, overlay="lanes: [unclosed\n")
    assert _resolve_reflect_model("reflector", home) == "reflector"
    err = capsys.readouterr().err
    assert "reflect: lane policy unreadable(" in err
    assert "using lane 'reflector'" in err


def test_name_signature_and_all_preserved():
    assert list(inspect.signature(_resolve_reflect_model).parameters) == [
        "lane", "mini_ork_home"]
    assert "_resolve_reflect_model" in reflect_mod.__all__


def test_caller_resolves_lane_through_new_reader(tmp_path, monkeypatch, capsys):
    """With no explicit model, the caller lands on the effective policy's lane."""
    home = _home(tmp_path, overlay="lanes:\n  reflector: glm\n")
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.delenv("MINI_ORK_GRADIENT_MODEL", raising=False)
    rc = reflect_mod.main(["--dry-run", "--lane", "reflector"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lane: reflector -> glm" in out
    assert "lane: reflector -> codex" not in out


def test_explicit_gradient_model_still_wins(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, overlay="lanes:\n  reflector: glm\n")
    monkeypatch.setenv("MINI_ORK_HOME", home)
    monkeypatch.setenv("MINI_ORK_GRADIENT_MODEL", "explicit-model")
    rc = reflect_mod.main(["--dry-run", "--lane", "reflector"])
    assert rc == 0
    assert "lane: reflector -> explicit-model" in capsys.readouterr().out
