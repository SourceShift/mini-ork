"""Deterministic transforms for the registry-audit recipe.

``registry_parse`` turns the registry document into a stable item list — no
LLM, so the ids the per-item checkpoints key on do not change between runs.

``audit_plan`` selects the items still lacking a checkpointed result and writes
the wave's work list. That filter is what makes a re-run (or the revise edge)
cheap: only the unfinished items are planned, not all of them.

Both register with ``@register_transform`` so workflow.yaml can name them in a
``transform:`` field. They run in the mini-ork Python process, not a harness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mini_ork.orchestration.item_fanout import result_path
from mini_ork.planning.registry_parse import RegistryItem, parse_registry_file
from mini_ork.workflow.artifacts import ArtifactContractError, ArtifactLedger
from mini_ork.workflow.compiler import CompiledWorkflow
from mini_ork.workflow.transforms import register_transform


def _run_dir() -> Path:
    run_dir = os.environ.get("MINI_ORK_RUN_DIR")
    if not run_dir:
        raise ArtifactContractError("registry-audit transforms require MINI_ORK_RUN_DIR")
    return Path(run_dir)


def results_dir_for(run_dir: str | Path) -> Path:
    """Where the fan-out checkpoints per-item results. One definition, used by
    the plan transform, the driver, and the verifier — a second copy of this
    path is how a verifier silently grades the wrong directory."""
    return Path(run_dir) / "results"


def _write(ledger: ArtifactLedger, workflow: CompiledWorkflow, node_id: str, name: str, payload) -> Path:
    node = workflow.nodes[node_id]
    if name not in node.outputs:
        raise ArtifactContractError(f"{node_id} requires a {name} output")
    out_path = ledger.output_path(workflow, node_id, name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


@register_transform("registry_parse")
def registry_parse(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Parse ``MO_REGISTRY_PATH`` into ``registry-items.json``.

    Refuses an empty parse: a work list of zero items would let every downstream
    node "succeed" while auditing nothing, which is the vacuous-pass shape the
    probe-validity rules exist to catch. A registry that parses to nothing is a
    broken parser or a broken path, never a legitimately empty audit.
    """
    registry = os.environ.get("MO_REGISTRY_PATH", "")
    if not registry or not Path(registry).is_file():
        raise ArtifactContractError(
            f"registry_parse requires MO_REGISTRY_PATH to name an existing file (got {registry!r})"
        )
    items: list[RegistryItem] = parse_registry_file(registry)
    if not items:
        raise ArtifactContractError(
            f"registry_parse found 0 items in {registry} — refusing a vacuous work list"
        )
    payload = {
        "source": registry,
        "count": len(items),
        "items": [item.to_dict() for item in items],
    }
    return _write(ledger, workflow, node_id, "registry_items", payload)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


@register_transform("audit_plan")
def audit_plan(workflow: CompiledWorkflow, ledger: ArtifactLedger, node_id: str) -> Path:
    """Select the still-unaudited items into ``audit-plan.json``.

    An item counts as done when its checkpoint file already holds a result, so
    this filter is the resume mechanism. ``MO_REGISTRY_MAX_ITEMS`` (0 = all)
    bounds one wave; ``MO_REGISTRY_QUARANTINED`` (newline-delimited ids) drops
    items a prior wave gave up on, mirroring goal-loop's quarantine.
    """
    prepared = ledger.prepared_inputs(node_id)
    item_paths = prepared.paths.get("registry_items", ())
    if not item_paths:
        raise ArtifactContractError("audit_plan requires the registry_items input")
    registry = json.loads(item_paths[0].read_text(encoding="utf-8"))

    results_dir = results_dir_for(_run_dir())
    quarantined = {
        line.strip()
        for line in os.environ.get("MO_REGISTRY_QUARANTINED", "").splitlines()
        if line.strip()
    }

    pending: list[dict] = []
    for item in registry.get("items", []):
        item_id = item["id"]
        if item_id in quarantined:
            continue
        if result_path(results_dir, item_id).is_file():
            continue
        pending.append(item)

    max_items = _int_env("MO_REGISTRY_MAX_ITEMS", 0)
    if max_items > 0:
        pending = pending[:max_items]

    plan = {
        "registry_path": registry.get("source", ""),
        "total_items": registry.get("count", 0),
        "planned": len(pending),
        "pending": [
            {
                "id": item["id"],
                "cluster": item.get("cluster", ""),
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "evidence": item.get("evidence", ""),
                "line": item.get("line", 0),
                # Recorded here so the verifier never re-derives the slug rule:
                # two copies of a path convention is how a gate ends up
                # checking the wrong file and passing.
                "result_path": str(result_path(results_dir, item["id"])),
            }
            for item in pending
        ],
    }
    return _write(ledger, workflow, node_id, "audit_plan", plan)
