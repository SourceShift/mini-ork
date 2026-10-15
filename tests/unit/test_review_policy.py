"""Unit tests: the ``ReviewPolicy`` loader.

Hermetic per test: a tmp ``$MINI_ORK_HOME`` with an isolated config dir, and
``os.environ`` scrubbed of every ``MO_REVIEW_*`` key the loader reads (the
suite-wide fixture in ``tests/conftest.py`` restores the rest).

The loader takes ``home`` and ``env`` as explicit arguments — that is what
makes this hermetic: the ambient process state never leaks into a decision.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.gates.review_policy import (  # noqa: E402
    ReviewPolicy,
    load_policy,
    node_requires_review,
)

_POLICY_KEYS = (
    "MO_REVIEW_BEFORE_MERGE",
    "MO_REVIEW_AFTER_NODES",
    "MO_REVIEW_TIER",
    "MO_REVIEW_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    # Point $MINI_ORK_HOME at tmp so a stray file lookup never reads the
    # real reviewer config (or any repo-local review.yaml a developer left).
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    # Drop every key the loader might read; an unset key reads as "" by
    # _normalize_env_value, and that is the contract we want to assert.
    for key in _POLICY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_home_config(tmp_path: Path, body: str) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "review.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ── DoD 1: file/env/defaults precedence ──────────────────────────────────


def test_defaults_when_no_file_and_no_env(tmp_path):
    policy = load_policy()
    assert policy == ReviewPolicy()  # before_merge="off", after_nodes=(), tier="simple"


def test_yaml_required_before_merge(tmp_path):
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: required\n",
    )
    policy = load_policy()
    assert policy.before_merge == "required"
    assert policy.tier == "simple"  # default


def test_env_overrides_yaml(tmp_path):
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: required\n  tier: secure\n",
    )
    env = {
        "MO_REVIEW_BEFORE_MERGE": "off",  # overrides required above
    }
    policy = load_policy(env=env)
    assert policy.before_merge == "off"
    # tier not in env → falls through to the YAML → "secure".
    assert policy.tier == "secure"


def test_env_after_nodes_all_means_every_node(tmp_path):
    env = {"MO_REVIEW_AFTER_NODES": "all"}
    policy = load_policy(env=env)
    assert policy.after_nodes == ("*",)


def test_env_after_nodes_comma_list(tmp_path):
    env = {"MO_REVIEW_AFTER_NODES": "researcher, verifier"}
    policy = load_policy(env=env)
    assert policy.after_nodes == ("researcher", "verifier")


def test_env_empty_value_is_unset(tmp_path):
    """An accidental `export MO_REVIEW_BEFORE_MERGE=` should not flip the gate."""
    _write_home_config(tmp_path, "review:\n  before_merge: required\n")
    env = {"MO_REVIEW_BEFORE_MERGE": "   "}
    policy = load_policy(env=env)
    assert policy.before_merge == "required"  # YAML still wins.


def test_yaml_after_nodes_list(tmp_path):
    _write_home_config(
        tmp_path,
        "review:\n  after_nodes: [implementer, verifier]\n",
    )
    policy = load_policy()
    assert policy.after_nodes == ("implementer", "verifier")


def test_yaml_comma_string_after_nodes(tmp_path):
    _write_home_config(
        tmp_path,
        'review:\n  after_nodes: "implementer, verifier"\n',
    )
    policy = load_policy()
    assert policy.after_nodes == ("implementer", "verifier")


def test_yaml_unknown_value_for_before_merge_raises(tmp_path):
    """A typo must never silently switch review off."""
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: maybe\n",
    )
    with pytest.raises(ValueError) as ei:
        load_policy()
    assert "before_merge" in str(ei.value)


def test_yaml_unquoted_off_becomes_off_string(tmp_path):
    """``before_merge: off`` (no quotes) is parsed by YAML 1.1 as the bool
    ``False``; the loader must map it to the string ``"off"`` so the
    example's documented value is what the operator gets."""
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: off\n",
    )
    policy = load_policy()
    assert policy.before_merge == "off"


def test_yaml_unquoted_on_raises(tmp_path):
    """``before_merge: on`` parses as ``True`` and is not a documented
    value; the validator must reject it. The point of the bool-coercion
    is the documented ``off`` shortcut, not a free pass on every YAML 1.1
    bool alias."""
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: on\n",
    )
    with pytest.raises(ValueError) as ei:
        load_policy()
    assert "before_merge" in str(ei.value)


def test_yaml_uppercase_OFF_lowercased(tmp_path):
    """YAML scalars are case-sensitive, but the example's value is
    lower-case; an operator's ``OFF`` should be accepted (file values
    follow the env treatment of being normalised)."""
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: OFF\n",
    )
    policy = load_policy()
    assert policy.before_merge == "off"


def test_yaml_uppercase_TIER_lowercased(tmp_path):
    _write_home_config(
        tmp_path,
        "review:\n  tier: SECURE\n",
    )
    policy = load_policy()
    assert policy.tier == "secure"


def test_yaml_unknown_tier_raises(tmp_path):
    _write_home_config(
        tmp_path,
        "review:\n  tier: quantumsafe\n",
    )
    with pytest.raises(ValueError) as ei:
        load_policy()
    assert "tier" in str(ei.value)


def test_env_unknown_value_raises_without_falling_back(tmp_path):
    """The env sits above the file; a typo in the env raises, even if the
    file would have been valid."""
    _write_home_config(
        tmp_path,
        "review:\n  before_merge: required\n",
    )
    env = {"MO_REVIEW_BEFORE_MERGE": "maybe"}
    with pytest.raises(ValueError):
        load_policy(env=env)


def test_yaml_unknown_subkey_raises(tmp_path):
    _write_home_config(
        tmp_path,
        "review:\n  befor_merge: required\n",  # typo
    )
    with pytest.raises(ValueError) as ei:
        load_policy()
    assert "befor_merge" in str(ei.value)


def test_yaml_review_present_but_not_a_mapping_raises(tmp_path):
    _write_home_config(
        tmp_path,
        "review: required\n",  # not a mapping
    )
    with pytest.raises(ValueError):
        load_policy()


def test_yaml_top_level_unknown_key_raises(tmp_path):
    """Like ``vault.yaml`` — typos at the top are caught, not silently dropped."""
    _write_home_config(
        tmp_path,
        "policy: required\n",  # wrong top-level key
    )
    with pytest.raises(ValueError):
        load_policy()


def test_loader_does_not_read_unrelated_mo_review_env():
    """``MO_REVIEW_PANEL`` / ``MO_REVIEW_ROUND_AWARE`` / ``MO_REVIEW_LENS_TIMEOUT_S``
    are lens-mechanic knobs from a different code path. The policy loader must
    not read them; passing them in the env must not change a default."""
    env = {
        "MO_REVIEW_PANEL": "all",
        "MO_REVIEW_ROUND_AWARE": "1",
        "MO_REVIEW_LENS_TIMEOUT_S": "999",
        "MO_FALLBACK_REVIEW": "1",
    }
    policy = load_policy(env=env)
    assert policy.before_merge == "off"
    assert policy.after_nodes == ()


# ── DoD 2: node_requires_review ──────────────────────────────────────────


def test_node_match_in_after_nodes():
    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",), tier="simple")
    assert node_requires_review(policy, "implementer") is True


def test_node_no_match():
    policy = ReviewPolicy(before_merge="off", after_nodes=("implementer",), tier="simple")
    assert node_requires_review(policy, "verifier") is False


def test_star_in_after_nodes_matches_everything():
    policy = ReviewPolicy(before_merge="off", after_nodes=("*",), tier="simple")
    assert node_requires_review(policy, "implementer") is True
    assert node_requires_review(policy, "verifier") is True
    assert node_requires_review(policy, "anything") is True


def test_node_flag_required_overrides_after_nodes():
    policy = ReviewPolicy(before_merge="off", after_nodes=(), tier="simple")
    assert node_requires_review(policy, "verifier", node_flag="required") is True


def test_node_flag_off_turns_off_in_simple_tier():
    policy = ReviewPolicy(before_merge="off", after_nodes=("*",), tier="simple")
    assert node_requires_review(policy, "implementer", node_flag="off") is False


def test_node_flag_off_ignored_in_secure_tier():
    policy = ReviewPolicy(before_merge="off", after_nodes=("*",), tier="secure")
    assert node_requires_review(policy, "implementer", node_flag="off") is True


def test_dataclass_is_frozen():
    """Policy must not mutate after load — a frozen dataclass catches a caller
    that writes ``policy.before_merge = "off"`` at runtime."""
    policy = ReviewPolicy()
    with pytest.raises(Exception):  # FrozenInstanceError
        policy.before_merge = "required"  # type: ignore[misc]
