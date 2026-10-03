"""ACP plan entries for a run — project ``plan.json`` decomposition + lifecycle
events into ``PlanEntry``-shaped dicts.

Pure functions: no ACP imports, no async, no module-level state. Mirrors the
``mini_ork.acp.diffs`` posture so ``agent.py`` is the only place that touches
wire-format types.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Node types whose ``priority`` field is "high" in the rendered plan. Keep
# literal so a future ``low`` priority (e.g. a teaser node) lands without
# changing this module.
_PRIORITY_HIGH: frozenset[str] = frozenset({"implementer", "reviewer"})


def plan_entries(
    run_dir: Path,
    events: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return ``PlanEntry``-shaped dicts (content/status/priority) in decomposition order.

    Reads ``<run_dir>/plan.json`` → ``decomposition``. A missing/unparseable
    file or a missing ``decomposition`` list returns ``[]`` — the caller
    treats that as "no plan to render" and skips the emit. Status per node:

    - ``node_end`` for that node seen → ``completed``
    - ``node_start`` (and no end) → ``in_progress``
    - no events for the node, yet some node that lists it in its own
      ``depends_on`` has started → ``completed`` (the planner under a static
      plan emits no events but its successors still fire — the planner is
      marked completed by the start of its first dependent)
    - else → ``pending``

    ``content`` = ``"<id> (<node_type>)"``; ``priority`` = ``"high"`` for
    ``implementer`` / ``reviewer`` else ``"medium"``. Order matches the
    decomposition list (the canonical DAG authoring order).
    """
    nodes = _load_decomposition(run_dir)
    if not nodes:
        return []

    started, ended = _partition_events(events)
    dependents = _dependents_index(nodes)

    entries_out: list[dict[str, Any]] = []
    for node in nodes:
        nid = str(node.get("id") or "")
        ntype = str(node.get("node_type") or "")
        if not nid:
            continue
        entries_out.append(
            {
                "content": f"{nid} ({ntype})" if ntype else nid,
                "status": _resolve_status(nid, started, ended, dependents),
                "priority": "high" if ntype in _PRIORITY_HIGH else "medium",
            }
        )
    return entries_out


def _load_decomposition(run_dir: Path) -> list[dict[str, Any]]:
    """Return the ``decomposition`` list from ``<run_dir>/plan.json``; ``[]`` on miss."""
    plan_path = run_dir / "plan.json"
    try:
        raw = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    decomposition = data.get("decomposition")
    if not isinstance(decomposition, list):
        return []
    out: list[dict[str, Any]] = []
    for node in decomposition:
        if isinstance(node, dict):
            out.append(node)
    return out


def _partition_events(
    events: list[dict[str, Any]] | None,
) -> tuple[set[str], set[str]]:
    """Walk lifecycle events and return (started_node_ids, ended_node_ids).

    Defensive on ``payload_json``: it may be a dict or a JSON-encoded string
    (``acp.agent._build_event_updates`` already swallows both shapes). Any
    un-decodable payload is treated as "no node id" so the event is skipped.
    """
    started: set[str] = set()
    ended: set[str] = set()
    for ev in events or []:
        et = ev.get("event_type")
        if et not in ("node_start", "node_end"):
            continue
        nid = _payload_node_id(ev)
        if not nid:
            continue
        if et == "node_start":
            started.add(nid)
        else:
            ended.add(nid)
    return started, ended


def _payload_node_id(ev: dict[str, Any]) -> str:
    """Read ``node_id`` from ``payload_json`` (dict or JSON string); ``""`` on miss."""
    payload = ev.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = None
    if isinstance(payload, dict):
        nid = payload.get("node_id")
        if nid:
            return str(nid)
    direct = ev.get("node_id")
    return str(direct or "")


def _dependents_index(nodes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map ``node_id`` → list of nodes that depend on it (the inverse of ``depends_on``)."""
    out: dict[str, list[str]] = {}
    for node in nodes:
        nid = str(node.get("id") or "")
        if not nid:
            continue
        for dep in node.get("depends_on") or []:
            if isinstance(dep, str) and dep:
                out.setdefault(dep, []).append(nid)
    return out


def _resolve_status(
    nid: str,
    started: set[str],
    ended: set[str],
    dependents: dict[str, list[str]],
) -> str:
    """Apply the four-rule status table documented on ``plan_entries``."""
    if nid in ended:
        return "completed"
    if nid in started:
        return "in_progress"
    # Skipped: a dependent has started but this node emitted no events.
    for dependent in dependents.get(nid, ()):
        if dependent in started:
            return "completed"
    return "pending"