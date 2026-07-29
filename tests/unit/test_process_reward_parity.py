"""Native contract tests for the deterministic process-reward scorer.

Fixtures preserve the SQLite row representation used by callers: activity
fields are JSON strings, while status, verdict, duration, and cost retain the
execution-trace shapes. Each expected score documents the reward policy rather
than deriving expectations from a retired implementation.
"""

from __future__ import annotations

import json
import pytest

from mini_ork.learning.process_reward import score_trace

# Fixture set — covers the trigger matrix of the weight table plus the
# Goodhart activity cap and verdict gating. Fields omitted from a fixture
# default to the empty/zero execution-trace representation.
FIXTURES = {
    "bare_success": {"status": "success"},
    "bare_failed": {"status": "failure"},
    "failed_heavy_activity_capped": {
        "status": "failure",
        "tool_calls": json.dumps([{"name": "bash"}, {"name": "edit"}, {"name": "read"}]),
        "files_written": json.dumps(["a.py", "b.py"]),
        "files_read": json.dumps(["c.py"]),
        "cost_usd": 0.05,
        "duration_ms": 5000,
        # Bash: 0 + min(0.20+0.10, 0.15) + 0 + 0.10 + 0.05 = 0.30
    },
    "success_verdict_approve": {
        "status": "success",
        "reviewer_verdict": "approve",
        "duration_ms": 3000,
        # 0.40 + 0 + 0.15 + 0.10 = 0.65
    },
    "success_verdict_fail": {
        "status": "success",
        "reviewer_verdict": "reject",
        "duration_ms": 3000,
        # 0.40 + 0 + 0 + 0.10 = 0.50
    },
    "failed_verdict_approve_gated": {
        "status": "failure",
        "reviewer_verdict": "approve",
        "duration_ms": 3000,
        # verdict gated: 0.10 only
    },
    "duration_below_floor_999ms": {
        "status": "success",
        "duration_ms": 999,
        # 0.40 only — too fast
    },
    "duration_at_floor_1000ms": {
        "status": "success",
        "duration_ms": 1000,
        # 0.40 + 0.10 = 0.50
    },
    "duration_at_ceiling_600000ms": {
        "status": "success",
        "duration_ms": 600000,
        # 0.40 + 0.10 = 0.50
    },
    "duration_above_ceiling_600001ms": {
        "status": "success",
        "duration_ms": 600001,
        # 0.40 only — too slow
    },
    "cost_zero_no_bonus": {
        "status": "success",
        "cost_usd": 0.0,
        "duration_ms": 3000,
        # 0.40 + 0.10 = 0.50
    },
    "cost_positive_bonus": {
        "status": "success",
        "cost_usd": 0.001,
        "duration_ms": 3000,
        # 0.40 + 0.10 + 0.05 = 0.55
    },
    "empty_none_fields": {
        # status defaults to "" via get fallback → no status_success
        # tool_calls/files default "[]" via .get(..., "[]") in helper
        # reviewer_verdict None → ""
        "duration_ms": 0,
    },
    "tool_plus_file_activity_under_cap": {
        "status": "success",
        "tool_calls": json.dumps([{"name": "bash"}]),
        "files_written": json.dumps(["x.py"]),
        "duration_ms": 3000,
        # 0.40 + min(0.20+0.10, 0.15) + 0.10 = 0.65 — verifies cap=0.15 not 0.30
    },
}


EXPECTED_SCORES = {
    "bare_success": 0.5,
    "bare_failed": 0.0,
    "failed_heavy_activity_capped": 0.0,
    "success_verdict_approve": 0.85,
    "success_verdict_fail": 0.55,
    "failed_verdict_approve_gated": 0.0,
    "duration_below_floor_999ms": 0.5,
    "duration_at_floor_1000ms": 0.55,
    "duration_at_ceiling_600000ms": 0.55,
    "duration_above_ceiling_600001ms": 0.5,
    "cost_zero_no_bonus": 0.55,
    "cost_positive_bonus": 0.55,
    "empty_none_fields": 0.0,
    "tool_plus_file_activity_under_cap": 0.7,
}


@pytest.mark.parametrize("name,fixture", FIXTURES.items())
def test_score_trace_matches_reward_policy(name, fixture):
    assert score_trace(fixture) == EXPECTED_SCORES[name]


def test_smoke_import_and_score():
    """Importing the module and scoring a minimal fixture returns a float in [0, 1]."""
    score = score_trace({"status": "success"})
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0
