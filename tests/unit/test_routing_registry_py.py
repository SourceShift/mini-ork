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


def test_policy_route_lane_records_provenance(monkeypatch):
    """Every routed lane carries an origin, so an outcome can be attributed to the
    decision that produced it. Before this the policy returned a bare lane string
    and the reason it chose that lane was lost."""
    # A recipe pin is author intent, not a router decision — and it must survive.
    assert _route("researcher", "glm_lens", monkeypatch, "learning_governed") == "glm_lens"
    assert routing.last_route_provenance() == {
        "route_source": "pinned", "route_explore": False,
        "route_policy": "learning_governed",
    }

    # A rule-based policy that never consults the brain still names itself.
    _route("reviewer", "reviewer", monkeypatch, "frontier_only")
    prov = routing.last_route_provenance()
    assert prov["route_source"] == "policy"
    assert prov["route_policy"] == "frontier_only"
    assert prov["route_explore"] is False

    # provenance must not leak from the previous node into this one
    _route("researcher", "researcher", monkeypatch, "workflow_default")
    assert routing.last_route_provenance()["route_policy"] == "workflow_default"


def test_route_provenance_absent_on_dry_run(monkeypatch):
    """Dry-run is a workflow-shape preview, not a policy preview: it must record
    no decision, and must not leave the previous node's decision standing."""
    monkeypatch.setenv("MO_ROUTING_POLICY", "frontier_only")
    _route("reviewer", "reviewer", monkeypatch, "frontier_only")
    assert routing.last_route_provenance()  # a real decision was recorded

    assert routing.policy_route_lane("reviewer", "kimi_lens", dry_run=True) == "kimi_lens"
    assert routing.last_route_provenance() == {}


# ───────────────────────────────────────────────────────────────────────────
# trace_governed: escalate on the persisted trace record, not a global counter.
#
# FAIL_COUNT carried no task, no node, and no record of which lane failed, so
# no outcome could be attributed to the decision that produced it. These tests
# pin the attribution: the escalation follows the traces, and a recipe pin's
# failure is not the router's to answer for.
# ───────────────────────────────────────────────────────────────────────────


def _trace_db(tmp_path, rows, monkeypatch):
    """Minimal execution_traces store; ``rows`` is [(status, route_source), …].

    Also pins the policy: ``policy_route_lane`` defaults MO_ROUTING_POLICY to
    ``learning_governed``, so a test that leaves it unset silently exercises a
    different policy and passes or fails for the wrong reason.
    """
    import sqlite3

    monkeypatch.setenv("MO_ROUTING_POLICY", "trace_governed")
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE execution_traces ("
        " trace_id TEXT PRIMARY KEY, run_id TEXT, task_class TEXT, status TEXT,"
        " route_source TEXT, route_explore INTEGER, route_score REAL,"
        " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S.000Z','now')))")
    for i, (status, route_source) in enumerate(rows):
        con.execute(
            "INSERT INTO execution_traces (trace_id, task_class, status, route_source)"
            " VALUES (?,?,?,?)", (f"t{i}", "code-fix", status, route_source))
    con.commit()
    con.close()
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    return str(db)


def test_trace_governed_escalates_on_a_recorded_failure(tmp_path, monkeypatch):
    """A failed attempt the router chose justifies the frontier — with the
    injected counter saying otherwise, so the traces are what decided."""
    _trace_db(tmp_path, [("success", "learned"), ("failure", "learned")], monkeypatch)
    monkeypatch.setenv("FAIL_COUNT", "0")

    assert routing.policy_route_lane(
        "researcher", "researcher", task_class="code-fix") == "opus_lens"


def test_trace_governed_stays_cheap_when_the_record_is_clean(tmp_path, monkeypatch):
    """All-success traces hold the cheap lane even with FAIL_COUNT raised."""
    _trace_db(tmp_path, [("success", "learned"), ("success", "learned")], monkeypatch)
    monkeypatch.setenv("FAIL_COUNT", "9")

    assert routing.policy_route_lane(
        "researcher", "researcher", task_class="code-fix") == "kimi_lens"


def test_trace_governed_ignores_pinned_failures(tmp_path, monkeypatch):
    """A pinned lane's failure is the author's pin failing, not the router's
    choice — it must not be what escalates the router."""
    _trace_db(tmp_path, [("failure", "pinned"), ("success", "learned")], monkeypatch)
    monkeypatch.setenv("FAIL_COUNT", "0")

    assert routing.policy_route_lane(
        "implementer", "implementer", task_class="code-fix") == "kimi_lens"


def test_trace_governed_falls_back_to_fail_count_without_evidence(tmp_path, monkeypatch):
    """No rows to govern on → the documented FAIL_COUNT contract still holds."""
    _trace_db(tmp_path, [], monkeypatch)
    monkeypatch.setenv("FAIL_COUNT", "2")

    assert routing.policy_route_lane(
        "researcher", "researcher", task_class="code-fix") == "opus_lens"


def test_trace_governed_without_task_class_uses_fail_count(tmp_path, monkeypatch):
    """No task class means no scope to query — a global trace scan is the blunt
    signal this replaced, so the counter governs instead."""
    _trace_db(tmp_path, [("failure", "learned")], monkeypatch)
    monkeypatch.setenv("FAIL_COUNT", "0")

    assert routing.policy_route_lane("researcher", "researcher") == "kimi_lens"
