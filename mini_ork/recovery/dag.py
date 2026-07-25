"""Recovery DAG — pure workflow.yaml adjacency data structure.

Parity port: moved verbatim from ``mini_ork/recovery/planner.py`` (SOLID
SRP split). This module is a pure data structure: it parses
``workflow.yaml`` (nodes + edges) into adjacency lists and answers
reachability questions (``descendants``). It has NO env, NO subprocess,
NO lease, and NO checkpoint-DB concerns — those stay in
``planner.py`` / ``plan.py``.

Public API (re-exported from ``mini_ork.recovery.planner``):

    load_dag(workflow_yaml_path) -> DAG
        Parses ``workflow.yaml`` (nodes + edges) into adjacency lists.
        Returns a ``DAG`` namedtuple with ``node_ids``, ``parents``
        (id → list of upstream ids), ``children`` (id → list of
        downstream ids), and ``topo`` (a topo-sorted list).

Topology convention (moved from the planner module docstring):

  Edges in workflow.yaml follow the convention ``from → to`` with
  ``edge_type`` in ``{depends_on, supplies_context_to, verifies,
  escalates_to}``. ALL edges contribute to the dependency relation
  for the closure — ``escalates_to`` and ``verifies`` are still a
  "this node's output flows into the next node's input" relation at
  the level the planner needs. ``rollback`` edges are excluded
  because they are control-flow only (the operator path, not data
  flow) — including them would mark the WHOLE DAG as the closure
  whenever a verifier fires.
"""
from __future__ import annotations

import dataclasses
import os

__all__ = ["DAG", "load_dag"]


@dataclasses.dataclass(frozen=True)
class DAG:
    """Parsed workflow.yaml. All adjacency lists are dicts keyed by node id.

    ``parents[node]`` lists nodes that produce data flowing into ``node``.
    ``children[node]`` lists nodes that consume data produced by ``node``.
    ``topo`` is a stable topological sort (Kahn's algorithm; ties broken
    by workflow.yaml declaration order)."""

    node_ids: tuple[str, ...]
    parents: dict[str, tuple[str, ...]]
    children: dict[str, tuple[str, ...]]
    topo: tuple[str, ...]

    def descendants(self, root: str) -> set[str]:
        """All nodes transitively downstream of ``root`` (incl. ``root``)."""
        if root not in self.children:
            return {root}
        seen: set[str] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.children.get(cur, ()))
        return seen


def _yaml_load(path: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "recovery_planner: PyYAML is required to parse workflow.yaml"
        ) from e
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    return data


def load_dag(workflow_yaml_path: str) -> DAG:
    """Parse ``workflow.yaml`` into a ``DAG``.

    Edges with ``edge_type == "escalates_to"`` are EXCLUDED from the
    dependency relation: they are operator-path edges, not data flow
    edges. A failed verifier escalating to rollback must not pull the
    whole DAG into the closure (that would defeat the point of E2).
    All other edge_types (``depends_on``, ``supplies_context_to``,
    ``verifies``) are treated as data-flow deps — see module docstring
    for the topology convention.
    """
    if not workflow_yaml_path or not os.path.isfile(workflow_yaml_path):
        raise FileNotFoundError(
            f"recovery_planner: workflow.yaml not found: {workflow_yaml_path!r}"
        )
    wf = _yaml_load(workflow_yaml_path)
    nodes = wf.get("nodes") or []
    if not isinstance(nodes, list):
        raise ValueError(
            f"recovery_planner: workflow.yaml nodes must be a list, got {type(nodes).__name__}"
        )
    declared_order: list[str] = []
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("name") or "").strip()
        if not nid:
            continue
        declared_order.append(nid)
        parents.setdefault(nid, [])
        children.setdefault(nid, [])

    for e in (wf.get("edges") or []):
        if not isinstance(e, dict):
            continue
        src = str(e.get("from") or "").strip()
        dst = str(e.get("to") or "").strip()
        if not src or not dst:
            continue
        if src not in children or dst not in parents:
            # Edge references an unknown node — ignore (workflow.yaml
            # validation belongs to plan, not the recovery planner).
            continue
        if str(e.get("edge_type") or "").strip() == "escalates_to":
            continue
        # Dedup per-node adjacency.
        if dst not in children[src]:
            children[src].append(dst)
        if src not in parents[dst]:
            parents[dst].append(src)

    # Stable topo sort: Kahn's algorithm with declared-order tiebreak.
    topo: list[str] = []
    indeg: dict[str, int] = {nid: len(parents.get(nid, ())) for nid in declared_order}
    # Use a sorted "ready" queue so ties break by declaration order.
    ready: list[str] = [nid for nid in declared_order if indeg[nid] == 0]
    ready.sort(key=declared_order.index)
    while ready:
        ready.sort(key=declared_order.index)
        cur = ready.pop(0)
        topo.append(cur)
        for child in children.get(cur, ()):
            indeg[child] -= 1
            if indeg[child] == 0 and child not in topo:
                ready.append(child)
    # Cycle guard: anything left in `indeg > 0` after Kahn's is a cycle
    # (workflow.yaml is a DAG per E1 contract; surface the error rather
    # than silently mis-computing the closure).
    if len(topo) != len(declared_order):
        leftover = [nid for nid in declared_order if nid not in topo]
        raise ValueError(
            "recovery_planner: workflow.yaml has a cycle through "
            f"{leftover}; cannot compute a dependency closure"
        )
    return DAG(
        node_ids=tuple(declared_order),
        parents={k: tuple(v) for k, v in parents.items()},
        children={k: tuple(v) for k, v in children.items()},
        topo=tuple(topo),
    )
