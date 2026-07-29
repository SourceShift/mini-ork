"""Unit tests for ``RubricPrescreenConfig`` (M8 ISP refactor).

Pins the env-fallback contract that used to live inline in
``mo_run_rubric_prescreen``:
  (a) built-in defaults match the historical inline ones
      (lane=kimi, budget=0.60, effort=low, tokens=2000, timeout=480,
      home=.mini-ork).
  (b) from_env picks up every env var the orchestrator read, with the
      same float()/int() coercion.
  (c) resolution precedence is unchanged: explicit parameter > config
      field (env var) > built-in default — verified end-to-end through
      ``mo_run_rubric_prescreen`` for both MINI_ORK_DB and
      MO_RUBRIC_LANE without spawning Claude (the registry fixture uses a
      non-Claude transport, so the run bails at the provider-capability gate).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates.rubric_prescreen import (
    RubricPrescreenConfig,
    mo_run_rubric_prescreen,
)

ALL_ENV = {
    "MINI_ORK_HOME": "/tmp/home-x",
    "MINI_ORK_DB": "/tmp/home-x/custom.db",
    "MO_RUBRIC_LANE": "glm",
    "MO_RUBRIC_BUDGET_USD": "1.25",
    "MO_RUBRIC_EFFORT": "high",
    "MO_RUBRIC_MAX_OUTPUT_TOKENS": "4096",
    "MO_RUBRIC_TIMEOUT_SEC": "900",
}


# ── config object: defaults + from_env ──────────────────────────────────────

def test_builtin_defaults_match_historical_inline_ones():
    cfg = RubricPrescreenConfig()
    assert cfg.resolve_lane() == "kimi"
    assert cfg.resolve_budget_usd() == 0.60
    assert cfg.resolve_effort() == "low"
    assert cfg.resolve_max_output_tokens() == 2000
    assert cfg.resolve_timeout_sec() == 480
    assert cfg.resolve_db() == ".mini-ork/state.db"


def test_from_env_picks_up_every_var():
    cfg = RubricPrescreenConfig.from_env(ALL_ENV)
    assert cfg.mini_ork_home == "/tmp/home-x"
    assert cfg.mini_ork_db == "/tmp/home-x/custom.db"
    assert cfg.lane == "glm"
    assert cfg.rubric_budget_usd == 1.25
    assert isinstance(cfg.rubric_budget_usd, float)
    assert cfg.rubric_effort == "high"
    assert cfg.rubric_max_output_tokens == 4096
    assert isinstance(cfg.rubric_max_output_tokens, int)
    assert cfg.rubric_timeout_sec == 900
    assert cfg.resolve_db() == "/tmp/home-x/custom.db"


def test_from_env_empty_mapping_yields_defaults():
    cfg = RubricPrescreenConfig.from_env({})
    assert cfg.mini_ork_home is None
    assert cfg.mini_ork_db is None
    assert cfg.lane is None
    assert cfg.resolve_lane() == "kimi"
    assert cfg.resolve_budget_usd() == 0.60
    assert cfg.resolve_effort() == "low"
    assert cfg.resolve_max_output_tokens() == 2000
    assert cfg.resolve_timeout_sec() == 480


def test_from_env_malformed_numeric_raises_like_inline_code():
    with pytest.raises(ValueError):
        RubricPrescreenConfig.from_env({"MO_RUBRIC_BUDGET_USD": "not-a-float"})
    with pytest.raises(ValueError):
        RubricPrescreenConfig.from_env({"MO_RUBRIC_TIMEOUT_SEC": "soon"})


# ── resolve_db precedence (mirrors the old inline os.environ.get chain) ─────

def test_resolve_db_precedence():
    # env MINI_ORK_DB wins over everything (even an explicit home param)
    cfg = RubricPrescreenConfig.from_env(
        {"MINI_ORK_DB": "/env.db", "MINI_ORK_HOME": "/envhome"})
    assert cfg.resolve_db("/paramhome") == "/env.db"
    # no MINI_ORK_DB: explicit param home wins over env home
    cfg = RubricPrescreenConfig.from_env({"MINI_ORK_HOME": "/envhome"})
    assert cfg.resolve_db("/paramhome") == "/paramhome/state.db"
    # no param: env home used
    assert cfg.resolve_db() == "/envhome/state.db"
    # neither: built-in default
    assert RubricPrescreenConfig.from_env({}).resolve_db() == ".mini-ork/state.db"


def test_resolve_db_env_set_to_empty_string_still_wins():
    # os.environ.get("MINI_ORK_DB", default) returns "" when the var is
    # set-but-empty; the config must preserve that (no truthiness
    # collapse on the DB var itself).
    cfg = RubricPrescreenConfig.from_env({"MINI_ORK_DB": ""})
    assert cfg.resolve_db("/paramhome") == ""
    # Same for a set-but-empty MINI_ORK_HOME: os.environ.get('MINI_ORK_HOME',
    # '.mini-ork') returned "" → "/state.db".
    cfg = RubricPrescreenConfig.from_env({"MINI_ORK_HOME": ""})
    assert cfg.resolve_db() == "/state.db"


# ── end-to-end precedence through mo_run_rubric_prescreen ───────────────────

def _mk_db(path: Path, kickoff_rel: str | None) -> str:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE epics (id TEXT PRIMARY KEY, kickoff_path TEXT)")
    if kickoff_rel is not None:
        con.execute("INSERT INTO epics (id, kickoff_path) VALUES ('ep1', ?)",
                    (kickoff_rel,))
    con.commit()
    con.close()
    return str(path)


@pytest.fixture
def stage(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    prompts = tmp_path / "prompts"
    scripts = tmp_path / "scripts"
    for d in (home, repo, prompts, scripts):
        d.mkdir()
    providers = tmp_path / "providers.yaml"
    providers.write_text(
        "providers:\n"
        "  kimi: {kind: codex-native, family: openai}\n"
        "  glm: {kind: codex-native, family: openai}\n"
        "  opus: {kind: codex-native, family: openai}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(providers))
    (repo / "kickoff.md").write_text("# kickoff\n", encoding="utf-8")
    db_with_kickoff = _mk_db(tmp_path / "a.db", "kickoff.md")
    db_without_kickoff = _mk_db(tmp_path / "b.db", None)
    return {
        "home": str(home), "repo": str(repo),
        "prompts": str(prompts), "scripts": str(scripts), "providers": str(providers),
        "db_a": db_with_kickoff, "db_b": db_without_kickoff,
    }


def _iter_dir(stage: dict) -> Path:
    return Path(stage["home"]) / "runs" / "ep1" / "iter-7"


def _run(stage: dict, **overrides):
    args = dict(
        epic="ep1", worktree=stage["repo"], iter=7,
        repo_root=stage["repo"], prompts_dir=stage["prompts"],
        scripts_dir=stage["scripts"], mini_ork_home=stage["home"],
    )
    args.update(overrides)
    mo_run_rubric_prescreen(**args)


def test_env_db_used_when_param_omitted(stage, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", stage["db_a"])
    _run(stage)
    # db_a has a kickoff row → flow reaches prompt write + the
    # env-script-missing bail (default lane kimi).
    assert (_iter_dir(stage) / "rubric-prompt.md").is_file()
    rub = json.loads((_iter_dir(stage) / "rubric.json").read_text())
    assert rub["parse_error"] is True


def test_param_db_overrides_env_db(stage, monkeypatch):
    monkeypatch.setenv("MINI_ORK_DB", stage["db_a"])
    _run(stage, mini_ork_db=stage["db_b"])
    # db_b has NO kickoff row → kickoff-miss bail happens BEFORE the
    # prompt is written. Absence of the prompt file proves the param
    # db (not the env db) was consulted.
    assert not (_iter_dir(stage) / "rubric-prompt.md").exists()
    rub = json.loads((_iter_dir(stage) / "rubric.json").read_text())
    assert rub["parse_error"] is True


def test_env_lane_feeds_provider_registry_lookup(stage, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_DB", stage["db_a"])
    monkeypatch.setenv("MO_RUBRIC_LANE", "glm")
    _run(stage)
    assert "lane=glm" in capsys.readouterr().err


def test_param_lane_overrides_env_lane(stage, monkeypatch, capsys):
    monkeypatch.setenv("MINI_ORK_DB", stage["db_a"])
    monkeypatch.setenv("MO_RUBRIC_LANE", "glm")
    _run(stage, lane="opus")
    assert "lane=opus" in capsys.readouterr().err
