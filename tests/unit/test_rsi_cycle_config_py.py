"""The cycle config parser: every way a config file can lie, pinned.

A cycle runs unattended and spends money, so the parser is tested like a safety
device rather than a convenience: for each optional knob there is a test that
omitting it does the *safe* thing, and for each required agreement there is a
test that disagreeing is refused at parse time instead of at gate time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mini_ork.rsi.config import (
    ConfigError,
    CycleConfig,
    from_mapping,
    load_config,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "rsi-cycle.example.yaml"

#: A minimal file that parses. Every other test varies exactly one thing, so a
#: failure points at the knob rather than at the fixture drifting.
BASE = {
    "budget": {"daily_cap_usd": 50, "per_cycle_cap_usd": 20},
    "critic": {"lanes": ["glm_lens", "opus_lens"]},
    "gates": {"pytest_paths": ["tests/unit/test_x.py"]},
}


def cfg(**overrides):
    raw = {**BASE, **overrides}
    return from_mapping(raw, env={})


# --------------------------------------------------------------------------
# Unknown keys
# --------------------------------------------------------------------------


def test_an_unknown_top_level_key_is_refused():
    with pytest.raises(ConfigError, match="config: unknown key"):
        cfg(budgte={"daily_cap_usd": 50})


@pytest.mark.parametrize("section", [
    "lease", "queue", "budget", "probes", "gates", "critic", "implement", "merge",
    "decide", "secure",
])
def test_an_unknown_key_in_every_section_is_refused(section):
    with pytest.raises(ConfigError, match=f"{section}: unknown key"):
        cfg(**{section: {"not_a_knob": 1}})


def test_a_merge_typo_cannot_silently_widen_the_gate():
    """The concrete failure: ``testcm`` would leave ``test_cmd`` empty."""
    with pytest.raises(ConfigError, match="merge: unknown key"):
        cfg(merge={"testcm": "python3 -m pytest -q tests/unit/test_x.py"})


def test_a_section_that_is_not_a_mapping_is_refused():
    with pytest.raises(ConfigError, match="budget: expected a mapping"):
        cfg(budget=["50"])


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_a_per_cycle_cap_above_the_daily_cap_is_refused():
    """It could never pass G11, so the file is wrong, not the cycle."""
    with pytest.raises(ConfigError, match="exceeds"):
        cfg(budget={"daily_cap_usd": 10, "per_cycle_cap_usd": 20})


@pytest.mark.parametrize("value", [0, -1, 0.0, "20", True, None])
def test_a_budget_that_is_not_a_positive_number_is_refused(value):
    with pytest.raises(ConfigError, match="budget.per_cycle_cap_usd"):
        cfg(budget={"daily_cap_usd": 50, "per_cycle_cap_usd": value})


def test_the_daily_cap_has_no_default():
    """Omitting it is not 'unlimited' — there is nothing to compare against."""
    with pytest.raises(ConfigError, match="budget.daily_cap_usd"):
        from_mapping({"critic": BASE["critic"]}, env={})


def test_the_environment_can_lower_the_daily_cap():
    out = from_mapping(BASE, env={"MO_DAILY_BUDGET_USD": "30"})
    assert out.budget.daily_cap_usd == 30.0
    assert out.budget.per_cycle_cap_usd == 20.0


def test_lowering_the_daily_cap_below_the_cycle_cap_is_refused():
    """Not 'run anyway': a cycle that can never pass G11 is a broken config."""
    with pytest.raises(ConfigError, match="exceeds"):
        from_mapping(BASE, env={"MO_DAILY_BUDGET_USD": "5"})


def test_the_environment_cannot_raise_the_daily_cap():
    out = from_mapping(BASE, env={"MO_DAILY_BUDGET_USD": "500"})
    assert out.budget.daily_cap_usd == 50.0


@pytest.mark.parametrize("value", ["", "abc", "0", "-3"])
def test_an_unusable_budget_env_var_is_an_error_not_a_shrug(value):
    """``min(cfg, env)`` must never silently become ``min(cfg, 0)``."""
    with pytest.raises(ConfigError, match="MO_DAILY_BUDGET_USD"):
        from_mapping(BASE, env={"MO_DAILY_BUDGET_USD": value})


# --------------------------------------------------------------------------
# decide — the one knob with no default
# --------------------------------------------------------------------------


def test_auto_merge_absent_quarantines_everything():
    assert cfg().decide.auto_merge is False


def test_auto_merge_must_be_a_boolean():
    with pytest.raises(ConfigError, match="decide.auto_merge"):
        cfg(decide={"auto_merge": "yes"})


# --------------------------------------------------------------------------
# Critics
# --------------------------------------------------------------------------


def test_fewer_than_two_critics_is_refused():
    with pytest.raises(ConfigError, match="at least 2 independent critics"):
        cfg(critic={"lanes": ["glm_lens"]})


def test_two_lanes_from_one_family_are_refused():
    """G10 would quarantine every cycle; say so at parse time, not after $7."""
    with pytest.raises(ConfigError, match="share a model family"):
        cfg(critic={"lanes": ["glm_lens", "glm-4.6"]})


def test_an_unknown_lane_counts_as_its_own_family():
    """``model_family`` falls back to the lane string.

    Parse time can only prove the two lanes are *different*; proving a lane
    actually served is the post-run panel check's job (``G10`` /
    ``critic_not_exogenous``). Asserting registry membership here would be
    wrong — lens lanes are aliases resolved by the dispatcher, not registry keys.
    """
    out = cfg(critic={"lanes": ["glm_lens", "totally-made-up-lane"]})
    assert out.critic.lanes == ("glm_lens", "totally-made-up-lane")


def test_majority_merge_is_not_configurable():
    with pytest.raises(ConfigError, match="require_unanimous"):
        cfg(critic={"lanes": ["glm_lens", "opus_lens"], "require_unanimous": False})


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def test_a_registered_probe_id_is_accepted():
    out = cfg(probes={"run": ["garden", "validate", "tests", "db:L3"]})
    assert out.probes.run == ("garden", "validate", "tests", "db:L3")


def test_an_unregistered_probe_is_refused():
    with pytest.raises(ConfigError, match="unregistered probe id"):
        cfg(probes={"run": ["garden", "garfen"]})


def test_a_db_probe_naming_no_rule_is_refused_by_the_shared_registry():
    """``db:L3`` is fine; ``db:L99`` is a typo that would never have a success state."""
    with pytest.raises(ConfigError, match="unregistered probe id"):
        cfg(probes={"run": ["db:L99"]})


def test_lane_preflight_is_refused_instead_of_accepted_then_unrunnable():
    """It used to be accepted here and then crash at startup.

    ``default_specs`` never built it — its health check is a provider network
    call, so it does not belong in a deterministic set. Accepting the id and
    failing later (after HARVEST, before SUGGEST) was the worse of the two
    errors; exit 2 at config time is free.
    """
    with pytest.raises(ConfigError, match="unregistered probe id"):
        cfg(probes={"run": ["garden", "lane-preflight"]})


def test_verifier_audit_is_registered_because_it_runs_in_process():
    assert cfg(probes={"run": ["verifier-audit"]}).probes.run == ("verifier-audit",)


def test_an_unknown_error_policy_is_refused():
    with pytest.raises(ConfigError, match="error_policy"):
        cfg(probes={"error_policy": "ignore"})


# --------------------------------------------------------------------------
# Gates / implement / mode
# --------------------------------------------------------------------------


def test_an_unknown_static_mode_is_refused():
    with pytest.raises(ConfigError, match="static_mode"):
        cfg(gates={"static_mode": "loud"})


def test_an_unknown_runtime_backend_is_refused():
    with pytest.raises(ConfigError, match="runtime_backend"):
        cfg(implement={"runtime_backend": "kubernetes-maybe"})


def test_loop_mode_needs_a_positive_interval():
    with pytest.raises(ConfigError, match="interval_s"):
        from_mapping({**BASE, "mode": "loop", "interval_s": 0}, env={})


def test_once_mode_with_many_cycles_is_contradictory():
    with pytest.raises(ConfigError, match="contradictory"):
        from_mapping({**BASE, "mode": "once", "max_cycles": 3}, env={})


def test_an_unknown_mode_is_refused():
    with pytest.raises(ConfigError, match="mode"):
        from_mapping({**BASE, "mode": "forever"}, env={})


# --------------------------------------------------------------------------
# The merge gate command
# --------------------------------------------------------------------------


def test_the_merge_command_is_built_from_the_scoped_test_set():
    out = cfg()
    assert out.merge_test_cmd.startswith("python3 -m pytest -q")
    assert out.merge_test_cmd.endswith("tests/unit/test_x.py")


def test_the_merge_command_refuses_to_fall_back_to_the_full_suite():
    """No scoped set is not 'run everything' — it is a config error."""
    out = from_mapping({"budget": BASE["budget"], "critic": BASE["critic"]}, env={})
    with pytest.raises(ConfigError, match="no scoped test set"):
        out.merge_test_cmd


def test_an_explicit_merge_test_cmd_wins():
    out = cfg(merge={"test_cmd": "python3 -m pytest -q -k rsi"})
    assert out.merge_test_cmd == "python3 -m pytest -q -k rsi"


# --------------------------------------------------------------------------
# Strict mode: the run path's extra requirements
# --------------------------------------------------------------------------


def test_strict_refuses_a_file_with_no_scoped_tests():
    raw = {"budget": BASE["budget"], "critic": BASE["critic"]}
    from_mapping(raw, env={})  # plan is allowed without them
    with pytest.raises(ConfigError, match="no scoped test set"):
        from_mapping(raw, env={}, strict=True)


def test_strict_refuses_live_probes_requested_by_file_alone():
    raw = {**BASE, "probes": {"run": ["garden"], "allow_live": True}}
    from_mapping(raw, env={})  # parsing alone is not spending
    with pytest.raises(ConfigError, match="allow_live"):
        from_mapping(raw, env={}, strict=True)


# --------------------------------------------------------------------------
# load_config — the file-resolution rules
# --------------------------------------------------------------------------


def test_a_named_path_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_a_named_path_that_is_a_directory_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path, env={})


def test_an_empty_file_is_an_error(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config(path, env={})


def test_a_non_mapping_yaml_document_is_an_error(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- budget\n- decide\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(path, env={})


def test_the_env_var_names_the_file(tmp_path):
    import yaml

    path = tmp_path / "rsi.yaml"
    path.write_text(yaml.safe_dump(BASE), encoding="utf-8")
    out = load_config(env={"MO_RSI_CONFIG": str(path)})
    assert out.budget.daily_cap_usd == 50.0


def test_the_repo_default_path_is_used_when_nothing_is_named(tmp_path):
    import yaml

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rsi-cycle.yaml").write_text(
        yaml.safe_dump(BASE), encoding="utf-8")
    out = load_config(env={"MINI_ORK_ROOT": str(tmp_path)})
    assert isinstance(out, CycleConfig)


def test_no_config_anywhere_is_an_error_with_a_copyable_fix():
    with pytest.raises(ConfigError, match="rsi-cycle.example.yaml"):
        load_config(env={"MINI_ORK_ROOT": "/nonexistent-root"})


def test_a_round_trip_to_dict_keeps_the_safety_knobs():
    out = cfg(decide={"auto_merge": False})
    data = out.to_dict()
    assert data["decide"]["auto_merge"] is False
    assert data["budget"]["per_cycle_cap_usd"] == 20.0
    assert data["critic"]["lanes"] == ["glm_lens", "opus_lens"]


# --------------------------------------------------------------------------
# The shipped example: the file in the repo cannot rot
# --------------------------------------------------------------------------


def test_the_shipped_example_parses_strict_and_passes_every_precondition():
    out = load_config(EXAMPLE, env={}, strict=True)
    assert out.version == 1 and out.enabled is True
    assert out.mode == "once" and out.max_cycles == 1
    assert out.budget.per_cycle_cap_usd <= out.budget.daily_cap_usd
    assert out.decide.auto_merge is True, "the example is the chosen gated-auto-merge policy"
    assert out.probes.allow_live is False
    assert out.gates.static_mode == "differential", "strict is unusable on today's red baseline"
    assert out.merge.require_remote is True
    assert out.critic.lanes == ("glm_lens", "opus_lens")
    assert out.implement.lane == "codex_lens"


def test_the_example_merge_command_is_scoped_and_disables_the_asyncio_plugin():
    out = load_config(EXAMPLE, env={}, strict=True)
    cmd = out.merge_test_cmd
    assert cmd.startswith("python3 -m pytest -q -p no:asyncio ")
    for path in out.scoped_tests:
        assert path in cmd
    assert set(out.scoped_tests) == {"tests/unit/test_rsi_cycle_py.py",
                                     "tests/unit/test_rsi_learn_py.py"}


# --------------------------------------------------------------------------
# Strict mode: the implementer/critic family check
# --------------------------------------------------------------------------


def test_strict_refuses_critics_sharing_the_implementers_family():
    """Without this the cycle pays for a child, then quarantines on G10."""
    raw = {**BASE, "implement": {"lane": "glm_lens"}}
    from_mapping(raw, env={})  # a plan does not spend, so non-strict parses
    with pytest.raises(ConfigError, match="share the implementer's family"):
        from_mapping(raw, env={}, strict=True)


def test_strict_accepts_an_exogenous_panel():
    raw = {**BASE, "implement": {"lane": "codex_lens"}}
    assert from_mapping(raw, env={}, strict=True).implement.lane == "codex_lens"


def test_a_blank_lane_in_the_panel_is_refused():
    """``lane_family`` falls back to the lowercased name; a blank one is not a family."""
    with pytest.raises(ConfigError, match="contains an empty entry"):
        cfg(critic={"lanes": ["glm_lens", "  "]})
    with pytest.raises(ConfigError, match="contains an empty entry"):
        cfg(critic={"lanes": ["glm_lens", ""]})
