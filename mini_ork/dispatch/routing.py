"""Role-aware lane routing policies (extracted from cli/execute.py).

Owns the fallback-chain synthesis and the MO_ROUTING_POLICY policy table.
The policy registry (POLICY_REGISTRY) makes routing extensible: register a
new policy callable instead of editing the executor. Re-exported from
mini_ork.cli.execute for backward compatibility.
"""
from __future__ import annotations

import os
import sys

_CODING_ROLES = {"implementer", "worker", "spec_author", "healer", "planner", "researcher",
                 "reflector", "replanner", "synthesizer", "bdd_runner"}
_REVIEW_ROLES = {"reviewer", "spec_reviewer", "verifier", "brain"}


def dispatch_chain(node_type: str, lead: str) -> str:
    """Lead lane + role-category fallback tail, comma-joined, order-preserving dedup."""
    tail = ""
    if node_type in _CODING_ROLES:
        tail = os.environ.get("MO_FALLBACK_CODING", "minimax,codex,sonnet")
    elif node_type in _REVIEW_ROLES:
        tail = os.environ.get("MO_FALLBACK_REVIEW", "opus,kimi,sonnet")
    if not tail:
        return lead
    seen = set()
    out = []
    for x in (lead + "," + tail).split(","):
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return ",".join(out)


def learning_static_lane(node_type: str, current_lane: str) -> str:
    frontier = os.environ.get("MO_FRONTIER_LANE", "opus_lens")
    cheap = os.environ.get("MO_CHEAP_LANE", "kimi_lens")
    # A recipe-pinned lane (current_lane != node_type) is explicit author intent
    # + the learning loop's exploration arm — keep it.
    if current_lane != node_type:
        return current_lane
    if node_type == "reviewer":
        return frontier
    if node_type in ("researcher", "implementer"):
        return cheap
    return current_lane


def learning_governed_lane(
    node_type: str,
    current_lane: str,
    *,
    root=None,
    task_class: str | None = None,
) -> str:
    """Port of bash `_mo_learning_governed_lane`: delegate the routing read to the
    canonical NATIVE `decide()` in ``mini_ork.steering.decision_service`` — the same
    brain every consumer uses. No state DB → static fallback (decide can't consult
    GRPO tables). Byte-parity with bash `decide` (verified deterministic, EPSILON=0);
    the .route field carries the lane, empty falls back to current_lane.

    2026-07-18: rewired from `bash -c 'source decision_service.sh; decide'` to the
    in-process native port — routing no longer shells out per dispatch."""
    db = os.environ.get("MINI_ORK_DB", "")
    if not db or not os.path.isfile(db):
        return learning_static_lane(node_type, current_lane)
    task_class = (task_class or os.environ.get("TASK_CLASS")
                  or os.environ.get("MINI_ORK_TASK_CLASS") or "generic")
    objective_domain = (os.environ.get("MINI_ORK_OBJECTIVE_DOMAIN")
                        or os.environ.get("MO_OBJECTIVE_DOMAIN") or "code-delivery")
    try:
        from mini_ork.steering import decision_service
        route = decision_service.decide(
            node_type, task_class, objective_domain, db=db).get("route", "")
        return route or current_lane
    except Exception:
        return current_lane


def policy_route_lane(
    node_type: str,
    current_lane: str,
    *,
    dry_run=False,
    root=None,
    task_class: str | None = None,
) -> str:
    """Port of bash `_mo_policy_route_lane`. Applied to every live node BEFORE dispatch
    so the routed lane (not the raw node_type/workflow lane) reaches --node-type. Dry-run
    preserves the recipe's explicit lane (workflow-shape preview, not a policy preview)."""
    if dry_run:
        return current_lane
    policy = os.environ.get("MO_ROUTING_POLICY") or "learning_governed"
    frontier = os.environ.get("MO_FRONTIER_LANE", "opus_lens")
    cheap = os.environ.get("MO_CHEAP_LANE", "kimi_lens")
    llm_types = ("researcher", "implementer", "reviewer")
    if policy in ("", "workflow_default"):
        return current_lane
    if policy == "frontier_only":
        return frontier if node_type in llm_types else current_lane
    if policy == "cheap_only":
        return cheap if node_type in llm_types else current_lane
    if policy == "static_hybrid":
        return learning_static_lane(node_type, current_lane)
    if policy == "learning_governed":
        # Router-monoculture fix: a recipe-pinned lane (current_lane != node_type) is a
        # deliberate author choice — cross-family panel diversity (glm/kimi/codex/opus
        # lenses) or a model-strength pin. The governed router must NOT override it with
        # the single global-slice winner: that collapses every same-node-type panel node
        # (4 researchers) onto ONE lane, destroying the diversity the recipe designed.
        # Learning governs only UNPINNED nodes (current_lane == node_type); pinned nodes
        # keep their lane — consistent with learning_static_lane's pin-preservation.
        if current_lane != node_type:
            return current_lane
        return learning_governed_lane(
            node_type,
            learning_static_lane(node_type, current_lane),
            root=root,
            task_class=task_class,
        )
    if policy == "trace_governed":
        fail_count = int(os.environ.get("FAIL_COUNT", "0") or "0")
        if node_type == "reviewer":
            return frontier
        if node_type in ("researcher", "implementer"):
            return frontier if fail_count > 0 else cheap
        return current_lane
    sys.stderr.write(f"  [warn] unknown MO_ROUTING_POLICY={policy} — using workflow lane {current_lane}\n")
    return current_lane


