"""Tests for the per-turn cost advisor (Epic E3)."""

from __future__ import annotations

import os
import tempfile


from mini_ork.cost_advisor import (
    AdvisorVerdict,
    _apply_budget_floor,
    _classify_difficulty,
    _load_config,
    advise_model,
)


def test_trivial_prompt_returns_cheap_tier():
    verdict = advise_model(
        prompt_text="Fix this typo in the comment",
        task_class="docs",
    )
    assert verdict.tier == "cheap"
    assert verdict.model_id in {"kimi", "glm", "codex"}


def test_hard_prompt_returns_expensive_tier():
    verdict = advise_model(
        prompt_text=(
            "We need to refactor the distributed architecture to fix a "
            "subtle race condition in the worker pool."
        ),
        task_class="refactor_audit",
    )
    assert verdict.tier == "expensive"


def test_long_prompt_returns_expensive_tier():
    long_text = "lorem ipsum " * 500  # ~5500 chars
    verdict = advise_model(
        prompt_text=long_text,
        task_class="generic",
    )
    assert verdict.tier in {"medium", "expensive"}


def test_budget_threshold_forces_cheap_tier():
    """Even on a hard prompt, budget < $5 forces cheap tier."""
    verdict = advise_model(
        prompt_text="refactor the distributed architecture",
        task_class="generic",
        budget_remaining_usd=3.0,
    )
    assert verdict.tier == "cheap"
    assert "budget" in verdict.rationale


def test_budget_threshold_downgrades_expensive_to_medium():
    """budget $10 (< $25) downgrades expensive → medium."""
    verdict = advise_model(
        prompt_text="refactor the distributed architecture",
        task_class="generic",
        budget_remaining_usd=10.0,
    )
    assert verdict.tier == "medium"


def test_classifier_failure_falls_back_to_cheap():
    """When config loading raises, advise_model still returns a
    well-formed AdvisorVerdict pointing at the cheap tier."""
    # Force a config-load failure by pointing at a malformed YAML.
    fd, bad_path = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, b"this is not: valid: yaml: at: all: ::\n")
    os.close(fd)
    try:
        verdict = advise_model(
            prompt_text="short",
            task_class="generic",
            config_path=bad_path,
        )
        assert verdict.tier == "cheap"
        assert verdict.confidence == 0.0
        assert "config_load_failed" in verdict.rationale
    finally:
        os.unlink(bad_path)


def test_advisor_returns_dataclass_with_to_json():
    verdict = advise_model("short", "generic")
    assert isinstance(verdict, AdvisorVerdict)
    j = verdict.to_json()
    assert "model_id" in j
    assert "tier" in j


def test_inline_default_config_when_yaml_missing():
    """When the YAML is absent, _load_config returns a baked-in
    minimal config so the advisor never crashes."""
    verdict = advise_model(
        prompt_text="short",
        task_class="generic",
        config_path="/tmp/does-not-exist-cost-advisor.yaml",
    )
    assert verdict.model_id in {"kimi", "glm", "codex"}
    assert verdict.tier in {"cheap", "medium", "expensive"}


def test_classify_difficulty_recognizes_keywords():
    config = _load_config()
    tier, _, _ = _classify_difficulty(
        "Add a comment to this function", "docs", config
    )
    assert tier == "cheap"
    tier, _, _ = _classify_difficulty(
        "Refactor the distributed pool", "generic", config
    )
    assert tier == "expensive"


def test_apply_budget_floor_idempotent_above_thresholds():
    config = _load_config()
    final_tier, downgrade = _apply_budget_floor("medium", 100.0, config)
    assert final_tier == "medium"
    assert downgrade == ""
