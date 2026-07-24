"""Unit tests for the routing policy registry (SOLID M4, OCP)."""
from mini_ork.dispatch import routing


def _route(node_type, lane, monkeypatch, policy, fail_count="0"):
    monkeypatch.setenv("MO_ROUTING_POLICY", policy)
    monkeypatch.setenv("FAIL_COUNT", fail_count)
    return routing.policy_route_lane(node_type, lane)


def test_builtin_policies_unchanged(monkeypatch):
    assert _route("reviewer", "reviewer", monkeypatch, "frontier_only") == "opus_lens"
    assert _route("planner", "planner", monkeypatch, "frontier_only") == "planner"
    assert _route("implementer", "implementer", monkeypatch, "cheap_only") == "kimi_lens"
    assert _route("researcher", "researcher", monkeypatch, "workflow_default") == "researcher"
    assert _route("researcher", "researcher", monkeypatch, "trace_governed", "0") == "kimi_lens"
    assert _route("researcher", "researcher", monkeypatch, "trace_governed", "2") == "opus_lens"
    # pinned lane survives learning_governed (router-monoculture fix)
    assert _route("researcher", "glm_lens", monkeypatch, "learning_governed") == "glm_lens"


def test_unknown_policy_warns_and_falls_back(monkeypatch, capsys):
    lane = _route("reviewer", "opus_lens", monkeypatch, "nope_policy")
    assert lane == "opus_lens"
    assert "unknown MO_ROUTING_POLICY=nope_policy" in capsys.readouterr().err


def test_register_policy_extends_routing(monkeypatch):
    routing.register_policy("always_sonnet", lambda ctx: "sonnet")
    try:
        assert _route("implementer", "implementer", monkeypatch, "always_sonnet") == "sonnet"
    finally:
        routing.POLICY_REGISTRY.pop("always_sonnet", None)


def test_dry_run_preserves_lane(monkeypatch):
    monkeypatch.setenv("MO_ROUTING_POLICY", "frontier_only")
    assert routing.policy_route_lane("reviewer", "kimi_lens", dry_run=True) == "kimi_lens"
