"""Role-aware lane routing policies (extracted from cli/execute.py).

Owns the fallback-chain synthesis and the MO_ROUTING_POLICY policy table.
The policy registry (POLICY_REGISTRY) makes routing extensible: register a
new policy callable instead of editing the executor. Re-exported from
mini_ork.cli.execute for backward compatibility.
"""
from __future__ import annotations

import contextvars
import os
import sys
from dataclasses import dataclass
from typing import Callable

_CODING_ROLES = {"implementer", "worker", "spec_author", "healer", "planner", "researcher",
                 "reflector", "replanner", "synthesizer", "bdd_runner"}
_REVIEW_ROLES = {"reviewer", "spec_reviewer", "verifier", "brain"}

# Provenance of the lane currently being routed, published by the policy layer and
# read by the dispatcher. A policy handler returns only a lane string, so the
# reason it chose that lane has nowhere else to travel: without this record a
# learned route, an epsilon-greedy exploration swap, a recipe pin, and a
# trace-governed failover are all just "a lane". EquiRouter and any later
# attribution of an outcome to the decision that produced it need the label.
_ROUTE_PROVENANCE: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "mo_route_provenance", default={})


def _record_route(**fields) -> None:
    """Merge provenance fields for the lane being routed right now."""
    _ROUTE_PROVENANCE.set({**_ROUTE_PROVENANCE.get(), **fields})


def last_route_provenance() -> dict:
    """Provenance recorded by the most recent ``policy_route_lane`` call in this
    node's context. Empty dict when routing never ran (dry-run, non-policy path)."""
    return dict(_ROUTE_PROVENANCE.get())


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
        _record_route(route_source="static", route_explore=False)
        return learning_static_lane(node_type, current_lane)
    task_class = (task_class or os.environ.get("TASK_CLASS")
                  or os.environ.get("MINI_ORK_TASK_CLASS") or "generic")
    objective_domain = (os.environ.get("MINI_ORK_OBJECTIVE_DOMAIN")
                        or os.environ.get("MO_OBJECTIVE_DOMAIN") or "code-delivery")
    try:
        from mini_ork.steering import decision_service
        decision = decision_service.decide(
            node_type, task_class, objective_domain, db=db)
        route = decision.get("route", "")
        if route:
            # Stamp the brain's own account of the decision so the dispatcher can
            # persist it. ``decide`` already distinguishes learned / explore /
            # default; recording it here is what makes the route attributable.
            _record_route(
                route_source=decision.get("route_source") or "learned",
                route_explore=bool(decision.get("route_explore")),
                route_score=decision.get("route_score"),
                route_margin=decision.get("route_margin"),
                predicted_error=decision.get("predicted_error"),
            )
        return route or current_lane
    except Exception:
        _record_route(route_source="fallback", route_explore=False)
        return current_lane


# ── Routing policy registry (OCP) ────────────────────────────────────────────
# A routing policy maps (node_type, current_lane, context) -> lane. Adding a
# policy is ``register_policy(name, fn)`` — no executor edits. Selected via
# MO_ROUTING_POLICY; unknown names warn + fall back to the workflow lane.

_LLM_NODE_TYPES = ("researcher", "implementer", "reviewer")


@dataclass(frozen=True)
class RoutingContext:
    node_type: str
    current_lane: str
    root: str | None = None
    task_class: str | None = None


RoutingPolicy = Callable[[RoutingContext], str]


def _frontier_lane() -> str:
    return os.environ.get("MO_FRONTIER_LANE", "opus_lens")


def _cheap_lane() -> str:
    return os.environ.get("MO_CHEAP_LANE", "kimi_lens")


def _policy_workflow_default(ctx: RoutingContext) -> str:
    return ctx.current_lane


def _policy_frontier_only(ctx: RoutingContext) -> str:
    return _frontier_lane() if ctx.node_type in _LLM_NODE_TYPES else ctx.current_lane


def _policy_cheap_only(ctx: RoutingContext) -> str:
    return _cheap_lane() if ctx.node_type in _LLM_NODE_TYPES else ctx.current_lane


def _policy_static_hybrid(ctx: RoutingContext) -> str:
    return learning_static_lane(ctx.node_type, ctx.current_lane)


def _policy_learning_governed(ctx: RoutingContext) -> str:
    # Router-monoculture fix: a recipe-pinned lane (current_lane != node_type) is a
    # deliberate author choice — cross-family panel diversity (glm/kimi/codex/opus
    # lenses) or a model-strength pin. The governed router must NOT override it with
    # the single global-slice winner: that collapses every same-node-type panel node
    # (4 researchers) onto ONE lane, destroying the diversity the recipe designed.
    # Learning governs only UNPINNED nodes (current_lane == node_type); pinned nodes
    # keep their lane — consistent with learning_static_lane's pin-preservation.
    if ctx.current_lane != ctx.node_type:
        _record_route(route_source="pinned", route_explore=False)
        return ctx.current_lane
    return learning_governed_lane(
        ctx.node_type,
        learning_static_lane(ctx.node_type, ctx.current_lane),
        root=ctx.root,
        task_class=ctx.task_class,
    )


def _trace_escalation(task_class: str | None) -> bool | None:
    """Should this node escalate to the frontier, per the persisted trace record?

    ``True`` / ``False`` when the trace table carries evidence for this task
    class, ``None`` when there is nothing to govern on (no task class, no db, no
    rows, or an unreadable store) so the caller can fall back.

    A task class is required: without one the query would span every task in the
    database, which is exactly the blunt global signal this replaces.

    Rows routed from a recipe pin are excluded. A pinned lane is deliberate
    author intent (cross-family panel diversity, a model-strength pin), so its
    failure says nothing about whether the router's own choice was wrong and
    must not by itself trigger an escalation. Rows written before provenance was
    recorded carry no ``route_source`` and are counted, since silence is not a
    pin.
    """
    if not task_class:
        return None
    db = os.environ.get("MINI_ORK_DB", "")
    if not db or not os.path.isfile(db):
        return None
    try:
        from mini_ork import trace_store
        rows = trace_store.trace_query(task_class=task_class, limit=50, db=db)
    except Exception:
        # Missing table or older schema: no evidence beats a crashed router.
        return None
    governed = [r for r in rows if (r.get("route_source") or "") != "pinned"]
    if not governed:
        return None
    return any((r.get("status") or "success") != "success" for r in governed)


def _policy_trace_governed(ctx: RoutingContext) -> str:
    """Escalate researcher/implementer to the frontier when the trace record for
    this task class shows a failure on a lane the router itself chose.

    Previously the signal was a bare ``FAIL_COUNT`` integer injected into the
    environment — a global counter with no task, no node, and no record of which
    lane actually failed, so no decision's outcome could be attributed to the
    decision. The persisted traces (route_source, status) are that attribution,
    and they are consulted first.

    ``FAIL_COUNT`` still governs when no trace evidence exists — a fresh
    database, a dry run, or a caller that has already counted failures. That
    keeps the documented bash contract and its tests intact.
    """
    if ctx.node_type == "reviewer":
        return _frontier_lane()
    if ctx.node_type in ("researcher", "implementer"):
        escalate = _trace_escalation(ctx.task_class)
        if escalate is None:
            escalate = int(os.environ.get("FAIL_COUNT", "0") or "0") > 0
        _record_route(route_source="trace_governed", route_explore=False)
        return _frontier_lane() if escalate else _cheap_lane()
    return ctx.current_lane


POLICY_REGISTRY: dict[str, RoutingPolicy] = {
    "": _policy_workflow_default,
    "workflow_default": _policy_workflow_default,
    "frontier_only": _policy_frontier_only,
    "cheap_only": _policy_cheap_only,
    "static_hybrid": _policy_static_hybrid,
    "learning_governed": _policy_learning_governed,
    "trace_governed": _policy_trace_governed,
}


def register_policy(name: str, policy: RoutingPolicy) -> None:
    """Register (or replace) a routing policy selectable via MO_ROUTING_POLICY."""
    POLICY_REGISTRY[name] = policy


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
    # Cleared up front: a stale record from the previous node must never be
    # attributed to this one's lane — and a dry-run must leave no record at all.
    _ROUTE_PROVENANCE.set({})
    if dry_run:
        return current_lane
    policy = os.environ.get("MO_ROUTING_POLICY") or "learning_governed"
    handler = POLICY_REGISTRY.get(policy)
    if handler is None:
        sys.stderr.write(f"  [warn] unknown MO_ROUTING_POLICY={policy} — using workflow lane {current_lane}\n")
        return current_lane
    lane = handler(RoutingContext(node_type, current_lane, root=root, task_class=task_class))
    if _ROUTE_PROVENANCE.get():
        _record_route(route_policy=policy)
    else:
        # A policy that never consulted the brain — a recipe pin, or a purely
        # rule-based branch. Attribute it to the policy so no lane is origin-less.
        _record_route(route_source="policy", route_explore=False,
                      route_policy=policy)
    sys.stderr.write(
        f"  [route] policy={policy} node={node_type} "
        f"lane={current_lane}->{lane} "
        f"source={_ROUTE_PROVENANCE.get().get('route_source', '')}\n")
    return lane


