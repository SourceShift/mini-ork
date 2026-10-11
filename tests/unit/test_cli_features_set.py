"""``mini-ork features set|unset|set-env|unset-env`` — the write-side verbs.

These verbs take no ``--home``: the IDE action path resolves the project from
``$MINI_ORK_HOME`` in the environment, not from argv (see ``board.rs``
``run_action``). ``list`` / ``render-skill`` / ``check-skill`` read the registry
only and must stay byte-identical to what they printed before the verbs existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_ork.cli import features as feat
from mini_ork.features import store


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / ".mini-ork"
    home.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.delenv("MINI_ORK_FEATURES", raising=False)
    return home


def _store(home: Path) -> dict:
    return store.load(home / "config" / "features.yaml")


# ── set ─────────────────────────────────────────────────────────────────────


def test_set_persists_a_baseline_value(_home: Path, capsys) -> None:
    assert feat.main(["set", "assay_relations", "5"]) == 0
    assert _store(_home)["features"] == {"assay_relations": "5"}
    assert store.effective_env(_home / "config" / "features.yaml")["MO_ASSAY_RELATIONS_K"] == "5"
    assert "assay_relations = 5" in capsys.readouterr().out


def test_set_off_materializes_the_engine_off_value(_home: Path) -> None:
    assert feat.main(["set", "plan_qa_gate", "off"]) == 0
    assert store.effective_env(_home / "config" / "features.yaml") == {"MINI_ORK_EXECUTE_GATE": "0"}


def test_set_rejects_an_unknown_id(_home: Path, capsys) -> None:
    assert feat.main(["set", "no_such_feature", "1"]) == 2
    assert "unknown feature" in capsys.readouterr().err
    assert _store(_home) == {"features": {}, "env": {}}


def test_set_rejects_a_bad_value_for_the_kind(_home: Path, capsys) -> None:
    assert feat.main(["set", "assay_relations", "many"]) == 2
    assert "whole number" in capsys.readouterr().err


def test_set_needs_a_value(_home: Path, capsys) -> None:
    assert feat.main(["set", "oracle_gates"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_turning_on_a_premium_feature_needs_the_flag_then_records_the_opt_in(
    _home: Path, capsys
) -> None:
    assert feat.main(["set", "review_panel", "codex kimi glm"]) == 2
    assert "premium" in capsys.readouterr().err
    assert _store(_home) == {"features": {}, "env": {}}  # refused before writing

    assert feat.main(["set", "review_panel", "codex kimi glm", "--accept-premium"]) == 0
    data = _store(_home)
    assert data["features"]["review_panel"] == "codex kimi glm"
    assert data["env"][store.ACCEPT_PREMIUM_ENV] == "1"


# ── unset ───────────────────────────────────────────────────────────────────


def test_unset_removes_a_value_and_is_idempotent(_home: Path, capsys) -> None:
    feat.main(["set", "oracle_gates", "off"])
    assert feat.main(["unset", "oracle_gates"]) == 0
    assert _store(_home)["features"] == {}
    assert feat.main(["unset", "oracle_gates"]) == 1  # second time: nothing set
    assert "is not set" in capsys.readouterr().err


def test_unset_needs_exactly_one_id(_home: Path, capsys) -> None:
    assert feat.main(["unset"]) == 2
    assert feat.main(["unset", "a", "b"]) == 2
    assert "usage: mini-ork features unset <id>" in capsys.readouterr().err


def test_unset_drops_the_premium_opt_in_when_no_premium_remains(_home: Path) -> None:
    feat.main(["set", "review_panel", "codex kimi glm", "--accept-premium"])
    feat.main(["set", "recursion", "4", "--accept-premium"])
    feat.main(["unset", "review_panel"])
    assert _store(_home)["env"][store.ACCEPT_PREMIUM_ENV] == "1"  # recursion still premium
    feat.main(["unset", "recursion"])
    assert store.ACCEPT_PREMIUM_ENV not in _store(_home)["env"]


# ── set-env / unset-env ─────────────────────────────────────────────────────


def test_set_env_persists_a_raw_setting(_home: Path, capsys) -> None:
    assert feat.main(["set-env", "MO_DAILY_BUDGET_USD", "20"]) == 0
    assert _store(_home)["env"] == {"MO_DAILY_BUDGET_USD": "20"}
    assert "MO_DAILY_BUDGET_USD" in capsys.readouterr().out


def test_set_env_refuses_a_credential(_home: Path, capsys) -> None:
    assert feat.main(["set-env", "ACME_API_KEY", "hunter2"]) == 2
    assert "credential" in capsys.readouterr().err
    assert _store(_home)["env"] == {}


def test_set_env_needs_a_value(_home: Path, capsys) -> None:
    assert feat.main(["set-env", "MO_DAILY_BUDGET_USD"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_unset_env_reports_absence(_home: Path, capsys) -> None:
    feat.main(["set-env", "MO_DAILY_BUDGET_USD", "20"])
    assert feat.main(["unset-env", "MO_DAILY_BUDGET_USD"]) == 0
    assert feat.main(["unset-env", "MO_DAILY_BUDGET_USD"]) == 1
    assert "is not set" in capsys.readouterr().err


# ── the read-side verbs that must not have moved ────────────────────────────


def test_list_prints_the_catalogue_unchanged(capsys) -> None:
    assert feat.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "review_panel" in out and "Baseline" in out and "Premium" in out

    assert feat.main(["list", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {f["id"] for f in doc["features"]} >= {"review_panel", "oracle_gates"}


def test_check_skill_passes_on_the_repo_block() -> None:
    repo = Path(__file__).resolve().parents[2]
    assert feat.main(["check-skill", "--path", str(repo / feat.DEFAULT_SKILL)]) == 0


def test_render_skill_round_trips_a_skill_file(tmp_path: Path, capsys) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text(f"before\n{feat.BEGIN}\nstale\n{feat.END}\nafter\n", encoding="utf-8")
    assert feat.main(["render-skill", "--path", str(skill)]) == 0
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("before\n") and text.endswith("\nafter\n")
    assert "review_panel" in text  # the generated block is in place
    assert "rendered feature block" in capsys.readouterr().out
    # and now it is no longer stale
    assert feat.main(["check-skill", "--path", str(skill)]) == 0


def test_a_skill_without_the_block_fails_render(tmp_path: Path, capsys) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("no markers here\n", encoding="utf-8")
    assert feat.main(["render-skill", "--path", str(skill)]) == 1
    assert "failed" in capsys.readouterr().err


def test_help_returns_zero_and_no_argv_returns_two(capsys) -> None:
    assert feat.main(["help"]) == 0
    assert "Usage:" in capsys.readouterr().out
    assert feat.main([]) == 2
