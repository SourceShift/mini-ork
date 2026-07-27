"""Unit tests: ``mini_ork.orchestration.topology.aggregate_traces`` (bash parity halves removed; formerly vs ``lib/topology.sh``).

Pure-function tests over small ``execution_traces`` corpora (plus an
optional ``workflow_memory`` join). Each fixture exercises one surface of
the win/loss/tie bucketing contract:

- win:  status='success' AND verdict not in (REJECT/ESCALATE/needs_revision)
- loss: status='failure' OR a rejecting verdict on 'success'
- tie:  status IS NULL or another recognized non-terminal status
- unrecognized statuses contribute 0 to every bucket (and sample_size)
- ``workflow_memory`` rows override topology_id (yaml_hash) + workflow_name

Floats are asserted within ``1e-6``.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from mini_ork.orchestration.topology import aggregate_traces


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures. Each is a (traces, workflow_memory|None, expected rows) triple.
# ─────────────────────────────────────────────────────────────────────────────

def _f01_pure_wins() -> dict:
    """All rows are 'success' with a non-rejecting verdict — pure wins."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.50, "duration_ms": 8000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": None,
             "cost_usd": 0.40, "duration_ms": 7000},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 2, "losses": 0, "ties": 0, "win_rate": 1.0, "sample_size": 2,
             "avg_cost_usd": 0.45, "avg_duration_ms": 7500.0},
        ],
    }


def _f02_pure_losses_via_rejection() -> dict:
    """'success' status with REJECT/ESCALATE/needs_revision verdict — all losses."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "REJECT",
             "cost_usd": 0.20, "duration_ms": 3000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "ESCALATE",
             "cost_usd": 0.30, "duration_ms": 4000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "needs_revision",
             "cost_usd": 0.10, "duration_ms": 2000},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 0, "losses": 3, "ties": 0, "win_rate": 0.0, "sample_size": 3,
             "avg_cost_usd": 0.2, "avg_duration_ms": 3000.0},
        ],
    }


def _f03_mixed_outcomes_two_groups() -> dict:
    """Two (topology, task_class) groups with a full mix of win/loss/tie."""
    return {
        "traces": [
            # Group A: code_fix_v1, code_fix
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.4, "duration_ms": 5000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE", "cost_usd": 0.6, "duration_ms": 6000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "running", "reviewer_verdict": None, "cost_usd": 0.0, "duration_ms": 1000},
            # Group B: refactor_v1, refactor
            {"workflow_version_id": "refactor_v1", "task_class": "refactor",
             "status": "success", "reviewer_verdict": None, "cost_usd": 0.8, "duration_ms": 9000},
            {"workflow_version_id": "refactor_v1", "task_class": "refactor",
             "status": "success", "reviewer_verdict": "REJECT", "cost_usd": 0.2, "duration_ms": 2500},
            {"workflow_version_id": "refactor_v1", "task_class": "refactor",
             "status": "vacuous", "reviewer_verdict": None, "cost_usd": 0.0, "duration_ms": 0},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 1, "losses": 1, "ties": 1, "win_rate": 0.5, "sample_size": 3,
             "avg_cost_usd": 1.0 / 3, "avg_duration_ms": 4000.0},
            {"topology_id": "refactor_v1", "workflow_name": "?", "task_class": "refactor",
             "wins": 1, "losses": 1, "ties": 1, "win_rate": 0.5, "sample_size": 3,
             "avg_cost_usd": 1.0 / 3, "avg_duration_ms": 11500.0 / 3},
        ],
    }


def _f04_unrecognized_status_yields_zero_tally() -> dict:
    """status='completed' is outside the recognized set — must contribute 0 to all 3 buckets.

    The group still appears in the aggregate (matching the GROUP BY contract),
    with win_rate=0.0 (denom=0) and sample_size=0.
    """
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "completed", "reviewer_verdict": None,
             "cost_usd": 0.5, "duration_ms": 5000},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 0, "losses": 0, "ties": 0, "win_rate": 0.0, "sample_size": 0,
             "avg_cost_usd": 0.5, "avg_duration_ms": 5000.0},
        ],
    }


def _f05_workflow_memory_join_overrides() -> dict:
    """The workflow_memory join swaps in yaml_hash + workflow_name."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v3", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.5, "duration_ms": 4000},
            {"workflow_version_id": "code_fix_v3", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.3, "duration_ms": 3000},
        ],
        "workflow_memory": {
            "code_fix_v3": {"yaml_hash": "deadbeefcafe", "workflow_name": "code-fix"},
        },
        "expected": [
            {"topology_id": "deadbeefcafe", "workflow_name": "code-fix", "task_class": "code_fix",
             "wins": 1, "losses": 1, "ties": 0, "win_rate": 0.5, "sample_size": 2,
             "avg_cost_usd": 0.4, "avg_duration_ms": 3500.0},
        ],
    }


def _f06_null_status_counts_as_tie() -> dict:
    """``status IS NULL`` is in the tie branch. Also tests missing cost/duration."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": None, "reviewer_verdict": None,
             "cost_usd": 0.0, "duration_ms": 0},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "blocked", "reviewer_verdict": None,
             "cost_usd": 0.0, "duration_ms": 0},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 0, "losses": 0, "ties": 2, "win_rate": 0.0, "sample_size": 2,
             "avg_cost_usd": 0.0, "avg_duration_ms": 0.0},
        ],
    }


def _f07_avg_cost_and_duration_precision() -> dict:
    """Three rows with different cost/duration — verifies AVG matches exactly.

    avg_cost = (0.10 + 0.30 + 0.50) / 3 = 0.30
    avg_dur  = (1000 + 2000 + 3000) / 3 = 2000.0
    """
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.10, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.30, "duration_ms": 2000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.50, "duration_ms": 3000},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 3, "losses": 0, "ties": 0, "win_rate": 1.0, "sample_size": 3,
             "avg_cost_usd": 0.3, "avg_duration_ms": 2000.0},
        ],
    }


def _f08_win_rate_rounding_boundaries() -> dict:
    """3 wins / 4 (wins+losses) = 0.75 exact. Verifies the round(., 4) contract."""
    return {
        "traces": [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE", "cost_usd": 0.1, "duration_ms": 1000},
        ],
        "workflow_memory": None,
        "expected": [
            {"topology_id": "code_fix_v1", "workflow_name": "?", "task_class": "code_fix",
             "wins": 3, "losses": 1, "ties": 0, "win_rate": 0.75, "sample_size": 4,
             "avg_cost_usd": 0.1, "avg_duration_ms": 1000.0},
        ],
    }


FIXTURES = {
    "01_pure_wins":                       _f01_pure_wins(),
    "02_pure_losses_via_rejection":       _f02_pure_losses_via_rejection(),
    "03_mixed_outcomes_two_groups":       _f03_mixed_outcomes_two_groups(),
    "04_unrecognized_status_zero_tally":  _f04_unrecognized_status_yields_zero_tally(),
    "05_workflow_memory_join_overrides":  _f05_workflow_memory_join_overrides(),
    "06_null_status_counts_as_tie":       _f06_null_status_counts_as_tie(),
    "07_avg_cost_and_duration_precision": _f07_avg_cost_and_duration_precision(),
    "08_win_rate_rounding_boundaries":    _f08_win_rate_rounding_boundaries(),
}


def _assert_rows(expected: list[dict[str, Any]], py_rows: list[dict[str, Any]], label: str) -> None:
    assert len(expected) == len(py_rows), (
        f"[{label}] row-count drift: expected={len(expected)} py={len(py_rows)}\n"
        f"  expected={expected!r}\n  py      ={py_rows!r}"
    )
    for e, p in zip(expected, py_rows):
        # String columns — exact match.
        for k in ("topology_id", "workflow_name", "task_class"):
            assert e[k] == p[k], f"[{label}] {k} drift: expected={e[k]!r} py={p[k]!r}"
        # Int columns — exact match.
        for k in ("wins", "losses", "ties", "sample_size"):
            assert int(e[k]) == int(p[k]), (
                f"[{label}] {k} drift: expected={e[k]!r} py={p[k]!r}"
            )
        # Float columns — close within 1e-6.
        for k in ("win_rate", "avg_cost_usd", "avg_duration_ms"):
            assert math.isclose(float(e[k]), float(p[k]), abs_tol=1e-6), (
                f"[{label}] {k} drift: expected={e[k]!r} py={p[k]!r}"
            )


@pytest.mark.parametrize(
    "fix_id,fix",
    list(FIXTURES.items()),
    ids=list(FIXTURES.keys()),
)
def test_aggregate_traces(fix_id, fix):
    py_rows = aggregate_traces(fix["traces"], fix.get("workflow_memory"))
    _assert_rows(fix["expected"], py_rows, fix_id)


def test_smoke_import_and_aggregate_no_io():
    """Pure-path smoke: import + aggregate a tiny corpus returns the right shape."""
    out = aggregate_traces(
        [
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "success", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.5, "duration_ms": 1000},
            {"workflow_version_id": "code_fix_v1", "task_class": "code_fix",
             "status": "failure", "reviewer_verdict": "APPROVE",
             "cost_usd": 0.3, "duration_ms": 800},
        ],
    )
    assert len(out) == 1
    row = out[0]
    assert row["wins"] == 1
    assert row["losses"] == 1
    assert row["ties"] == 0
    assert row["sample_size"] == 2
    assert math.isclose(row["win_rate"], 0.5, abs_tol=1e-6)
    assert row["topology_id"] == "code_fix_v1"
    assert row["workflow_name"] == "?"
    assert row["task_class"] == "code_fix"
    assert math.isclose(row["avg_cost_usd"], 0.4, abs_tol=1e-6)
    assert math.isclose(row["avg_duration_ms"], 900.0, abs_tol=1e-6)
