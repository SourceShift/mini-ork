"""Cost governance for a single run — the declared ceiling around a live run.

:mod:`mini_ork.cost.budget` is the entry point: it answers whether a run may
spend another node, from a ceiling the operator declared in the environment
(simple tier) or from a registered policy that asks a real cost service
(secure tier). The dispatch gate in ``cli.execute_handlers`` asks it before
every node and parks the run — resumable — instead of letting it fail.

Read-only readers that just report spend stay in :mod:`mini_ork.cost_ledger`.
"""
from __future__ import annotations

from mini_ork.cost.budget import (
    BUDGET_ENV,
    DECLARED_POLICY,
    MARKER_NAME,
    POLICY_ENV,
    BUDGET_POLICIES,
    Verdict,
    declared,
    marker_path,
    read_marker,
    record,
    register_budget_policy,
    verdict,
)

__all__ = [
    "BUDGET_ENV",
    "BUDGET_POLICIES",
    "DECLARED_POLICY",
    "MARKER_NAME",
    "POLICY_ENV",
    "Verdict",
    "declared",
    "marker_path",
    "read_marker",
    "record",
    "register_budget_policy",
    "verdict",
]
