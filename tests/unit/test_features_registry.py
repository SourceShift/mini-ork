"""The controllable-feature registry: tier classification, cost math, and gate."""

from __future__ import annotations

from mini_ork.features import registry as reg


def test_builtins_are_registered() -> None:
    ids = set(reg.CONTROLLABLE_FEATURES)
    assert {"plan_qa_gate", "oracle_gates", "review_panel", "code_arm_scorer", "recursion"} <= ids


def test_premium_classification() -> None:
    # >1.5x on their own -> premium
    assert reg.get_feature("review_panel").tier == "premium"  # 3 lanes -> x3
    assert reg.get_feature("code_arm_scorer").tier == "premium"  # 1 probe -> x2
    assert reg.get_feature("recursion").tier == "premium"  # 3 iters -> x3
    # <=1.5x -> baseline
    for fid in ("plan_qa_gate", "prm_score", "oracle_gates", "assay_relations", "assay_differential"):
        assert reg.get_feature(fid).tier == "baseline", fid


def test_multiplier_math() -> None:
    rp = reg.get_feature("review_panel")
    assert rp.multiplier("codex kimi glm") == 3.0  # 3 lanes in parallel
    assert rp.multiplier("") == 1.0  # off
    assert rp.multiplier("codex") == 1.0  # one lane == baseline, clamped at 1.0

    ca = reg.get_feature("code_arm_scorer")
    assert ca.multiplier("1") == 2.0  # one probe -> baseline + candidate arms
    assert ca.multiplier("2") == 4.0

    rel = reg.get_feature("assay_relations")
    assert round(rel.multiplier("3"), 4) == round(1.0 + 0.07 * 3, 4)  # additive
    assert rel.multiplier("0") == 1.0

    diff = reg.get_feature("assay_differential")
    assert round(diff.multiplier("6"), 4) == round(1.0 + 0.03 * 6, 4)


def test_effective_multiplier_is_a_product() -> None:
    sel = {"review_panel": "codex kimi glm", "prm_score": "1"}
    assert reg.effective_multiplier(sel) == round(3.0 * 1.05, 3)

    baseline_only = {"prm_score": "1", "oracle_gates": "1"}
    assert reg.effective_multiplier(baseline_only) == round(1.05 * 1.2, 3)

    assert reg.effective_multiplier({}) == 1.0


def test_premium_unselected_flags_only_enabled_premium() -> None:
    sel = {"review_panel": "codex kimi glm", "oracle_gates": "1"}
    flagged = {f.id for f in reg.premium_unselected(sel)}
    assert flagged == {"review_panel"}  # oracle_gates is baseline

    # a premium feature that is off is not flagged
    assert reg.premium_unselected({"review_panel": ""}) == []


def test_resolve_selection_gates_premium_without_opt_in() -> None:
    sel = {"review_panel": "codex kimi glm", "oracle_gates": "1"}

    gated = reg.resolve_selection(sel, accept_premium=False)
    assert "MO_REVIEW_PANEL" not in gated  # premium dropped
    assert gated["MO_ORACLE_GATES_AUTO"] == "1"  # baseline kept

    allowed = reg.resolve_selection(sel, accept_premium=True)
    assert allowed["MO_REVIEW_PANEL"] == "codex kimi glm"
    assert allowed["MO_ORACLE_GATES_AUTO"] == "1"


def test_resolve_selection_writes_count_and_co_knobs() -> None:
    env = reg.resolve_selection({"assay_relations": "5"}, accept_premium=True)
    assert env["MO_ASSAY_RELATIONS"] == "1"  # enable knob
    assert env["MO_ASSAY_RELATIONS_K"] == "5"  # the tuned count

    env = reg.resolve_selection({"code_arm_scorer": "2"}, accept_premium=True)
    assert env["MO_APPLY_SCORER"] == "code"
    assert env["MO_APPLY_PROBE_MAX_TASKS"] == "2"


def test_feature_off_contributes_nothing() -> None:
    assert reg.resolve_selection({"oracle_gates": "0"}) == {}
    assert reg.resolve_selection({"oracle_gates": ""}) == {}


def test_catalogue_renders_both_tiers() -> None:
    md = reg.catalogue_markdown()
    assert "Baseline" in md and "Premium" in md
    assert "review_panel" in md and "oracle_gates" in md
    assert "×3" in md  # review panel's multiplier

    js = reg.catalogue_json()
    assert js["premium_threshold"] == 1.5
    ids = {f["id"] for f in js["features"]}
    assert "review_panel" in ids


def test_applies_to_filters_by_recipe() -> None:
    # recursion is scoped to recursive-self-improve
    assert "recursion" in {f.id for f in reg.features_for("recursive-self-improve")}
    assert "recursion" not in {f.id for f in reg.features_for("code-fix")}
    # an unscoped feature is everywhere
    assert "oracle_gates" in {f.id for f in reg.features_for("code-fix")}
    assert "oracle_gates" in {f.id for f in reg.features_for(None)}


def test_gate_env_strips_premium_unless_opted_in() -> None:
    env = {"MO_REVIEW_PANEL": "codex kimi glm", "MO_ORACLE_GATES_AUTO": "1", "MO_PRM_SCORE": "1"}
    filtered, blocked = reg.gate_env(env, accept_premium=False)
    assert blocked == ["review_panel"]
    assert "MO_REVIEW_PANEL" not in filtered  # premium dropped
    assert filtered["MO_ORACLE_GATES_AUTO"] == "1"  # baseline kept

    kept, blocked = reg.gate_env(env, accept_premium=True)
    assert blocked == []
    assert kept["MO_REVIEW_PANEL"] == "codex kimi glm"


def test_gate_env_ignores_baseline_and_off_knobs() -> None:
    # baseline env passes through untouched
    filtered, blocked = reg.gate_env({"MO_PRM_SCORE": "1", "MO_ORACLE_GATES_AUTO": "1"})
    assert blocked == [] and filtered == {"MO_PRM_SCORE": "1", "MO_ORACLE_GATES_AUTO": "1"}
    # a premium knob explicitly off is not blocked
    filtered, blocked = reg.gate_env({"MO_APPLY_SCORER": "0"})
    assert blocked == [] and filtered == {"MO_APPLY_SCORER": "0"}


def test_committed_skill_block_is_current() -> None:
    """The skill's generated block must match the registry — the auto-sync gate.

    A newly registered feature that was never re-rendered turns this red, which
    is what makes "features are added to the skill automatically" a guarantee
    rather than a habit.
    """
    from pathlib import Path

    from mini_ork.cli import features as feat

    skill = Path(__file__).resolve().parents[2] / "skills" / "wizard" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    block = feat._block(None)
    assert feat._replace_block(text, block) == text, (
        "skills/wizard/SKILL.md feature block is stale — run: "
        "mini-ork features render-skill"
    )


def test_wizard_slash_command_is_announced_and_handled() -> None:
    from mini_ork.acp import commands

    assert "wizard" in {c.name for c in commands.COMMANDS}
    assert "wizard" in commands.HANDLERS

