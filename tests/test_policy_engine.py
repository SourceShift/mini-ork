"""Tests for the stateful policy engine.

Covers the four fixture cases the Epic E2 kickoff requires:
    1. Simple ALLOW path
    2. Simple DENY path
    3. Stateful REQUIRE_APPROVAL (npm_install → git_push)
    4. Audit row written per evaluation + decision_id round-trip
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from mini_ork.policies import (
    PolicyEvent,
    PolicyResponse,
    evaluate_policies,
    record_decision,
    register_policy,
)
from mini_ork.policies.engine import (
    clear_registry,
    cost_threshold_pause,
    network_egress_check,
    verifier_failure_escalation,
)


ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "db" / "migrations" / "0026_policy_state.sql"


@pytest.fixture
def fresh_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript(MIGRATION.read_text())
    con.commit()
    con.close()
    yield path
    os.unlink(path)


def test_default_allow_when_no_policies_registered(fresh_registry):
    event: PolicyEvent = {"type": "tool_call", "data": {"tool": "ls"}}
    response = evaluate_policies(event)
    assert response["result"] == "ALLOW"
    assert response["policy_name"] == "<default>"


def test_deny_policy_short_circuits(fresh_registry):
    def deny_ls(event: PolicyEvent, config=None) -> PolicyResponse | None:
        if event.get("data", {}).get("tool") == "ls":
            return {"result": "DENY", "reason": "ls is forbidden"}
        return None

    register_policy("deny_ls", deny_ls)
    response = evaluate_policies(
        {"type": "tool_call", "data": {"tool": "ls"}}
    )
    assert response["result"] == "DENY"
    assert response["policy_name"] == "deny_ls"


def test_stateful_require_approval_after_npm_install(fresh_registry):
    """The classic Omnigent example: deny git push after npm install."""
    state = {"recent_npm_install": False}

    def npm_install_tracker(
        event: PolicyEvent, config=None
    ) -> PolicyResponse | None:
        data = event.get("data") or {}
        if event.get("type") == "tool_call" and data.get("tool") == "npm_install":
            state["recent_npm_install"] = True
            return {"result": "LOG_ONLY", "reason": "tracking npm_install"}
        return None

    def block_git_push_after_npm(
        event: PolicyEvent, config=None
    ) -> PolicyResponse | None:
        data = event.get("data") or {}
        if (
            event.get("type") == "tool_call"
            and data.get("tool") == "git_push"
            and state["recent_npm_install"]
        ):
            return {
                "result": "REQUIRE_APPROVAL",
                "reason": "git push after npm install requires approval",
            }
        return None

    register_policy("track_npm", npm_install_tracker)
    register_policy("block_git", block_git_push_after_npm)

    # Step 1: npm install → tracked
    response = evaluate_policies(
        {"type": "tool_call", "data": {"tool": "npm_install"}}
    )
    assert response["result"] == "LOG_ONLY"
    assert state["recent_npm_install"] is True

    # Step 2: git push → REQUIRE_APPROVAL
    response = evaluate_policies(
        {"type": "tool_call", "data": {"tool": "git_push"}}
    )
    assert response["result"] == "REQUIRE_APPROVAL"


def test_record_decision_audit_row(fresh_registry, temp_db):
    event: PolicyEvent = {
        "type": "verifier_result",
        "data": {"verifier": "tsc", "verdict": "fail"},
        "run_id": "run-test-001",
    }
    response: PolicyResponse = {
        "result": "DENY",
        "reason": "verifier failed",
        "policy_name": "test_policy",
    }
    decision_id = record_decision(event, response, temp_db)
    assert decision_id.startswith("pd-")

    # Round-trip: read back the row + verify shape.
    con = sqlite3.connect(temp_db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM policy_decisions WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    con.close()

    assert row is not None
    assert row["run_id"] == "run-test-001"
    assert row["event_type"] == "verifier_result"
    assert row["policy_name"] == "test_policy"
    assert row["result"] == "DENY"


def test_buggy_policy_does_not_crash_engine(fresh_registry):
    def buggy_policy(event, config=None):
        raise RuntimeError("intentional bug")

    register_policy("buggy", buggy_policy)
    response = evaluate_policies({"type": "tool_call", "data": {}})
    # Engine catches + downgrades to LOG_ONLY rather than crashing.
    assert response["result"] == "LOG_ONLY"
    assert "intentional bug" in response["reason"]


def test_builtin_cost_threshold_pause(fresh_registry):
    register_policy(
        "cost_pause",
        cost_threshold_pause,
        config={"threshold_usd": 10.0},
    )
    # Under threshold — abstains, default allow.
    response = evaluate_policies(
        {"type": "cost_threshold", "data": {"spent_usd": 5.0}}
    )
    assert response["result"] == "ALLOW"
    # Over threshold — REQUIRE_APPROVAL.
    response = evaluate_policies(
        {"type": "cost_threshold", "data": {"spent_usd": 15.0}}
    )
    assert response["result"] == "REQUIRE_APPROVAL"


def test_builtin_network_egress_check(fresh_registry):
    register_policy(
        "egress",
        network_egress_check,
        config={"allowed_hosts": ["api.anthropic.com"]},
    )
    # Allowlisted host — abstains, default allow.
    response = evaluate_policies(
        {"type": "network_request", "data": {"host": "api.anthropic.com"}}
    )
    assert response["result"] == "ALLOW"
    # Not allowlisted — DENY.
    response = evaluate_policies(
        {"type": "network_request", "data": {"host": "evil.com"}}
    )
    assert response["result"] == "DENY"


def test_builtin_verifier_failure_escalation(fresh_registry):
    register_policy(
        "vf_escalate",
        verifier_failure_escalation,
        config={"max_consecutive_failures": 3},
    )
    response = evaluate_policies(
        {"type": "verifier_result", "data": {"consecutive_failures": 2}}
    )
    assert response["result"] == "ALLOW"
    response = evaluate_policies(
        {"type": "verifier_result", "data": {"consecutive_failures": 3}}
    )
    assert response["result"] == "REQUIRE_APPROVAL"
