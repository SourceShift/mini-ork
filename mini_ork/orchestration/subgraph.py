"""Subgraph nodes: one recipe runs as a node of another.

A ``type: subgraph`` node names a recipe (``recipe:``) and, optionally, the
kickoff it runs on (``kickoff:``; the parent run's kickoff by default). The
handler runs that recipe as a child ``mini-ork run`` under its own run id,
``<parent>--<node>``, approved and bounded by the recursive-orchestration spawn
policy (``MINI_ORK_RECURSIVE_MAX_DEPTH``, the child and descendant caps), and
the node passes exactly when the child run passes.

The child gets a clean environment: run- and node-scoped variables of the
parent (its run dir, node id, tool grants, capability envelope, sidecar paths)
are dropped, so the child can neither write into the parent's run nor inherit
a grant its own recipe did not make. ``subgraph-<node>.json`` records the link,
the child's output goes to ``subgraph-<node>.log``, and the child's source
artifact — when its contract names one — is copied into the parent run dir as
``subgraph-<node>-<artifact>``.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

DEPTH_ENV = "MINI_ORK_SPAWN_DEPTH"
_DROPPED_EXACT = frozenset({
    "MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MO_WORKFLOW_YAML", "MO_RESOLVED_NODE_TOOLS",
    "MO_SKILLS", "MO_MCP_SERVERS", "MO_AGENT_DOC", "MO_USAGE_FILE", "MO_COST_FILE",
    "MO_TRACEPARENT",
})
_DROPPED_PREFIXES = ("MO_NODE_",)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def node_fields(workflow_path: str, node_id: str) -> dict:
    """The raw ``workflow.yaml`` entry of ``node_id`` (``{}`` when absent)."""
    try:
        document = yaml.safe_load(Path(workflow_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    for node in document.get("nodes") or []:
        if isinstance(node, dict) and node.get("name") == node_id:
            return node
    return {}


def child_env(base: Mapping[str, str], *, child_run_id: str, depth: int) -> dict[str, str]:
    """``base`` without the parent's run- and node-scoped variables."""
    env = {key: value for key, value in base.items()
           if key not in _DROPPED_EXACT and not key.startswith(_DROPPED_PREFIXES)}
    env["MINI_ORK_RUN_ID"] = child_run_id
    env["MINI_ORK_PARENT_RUN_ID"] = base.get("MINI_ORK_RUN_ID", "")
    env[DEPTH_ENV] = str(depth)
    # The child is `python -m mini_ork.cli.main` started from the target repo:
    # the engine is importable only if its package root is on the path.
    package_root = str(Path(__file__).resolve().parents[2])
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{package_root}{os.pathsep}{inherited}" if inherited else package_root
    return env


def _parent_kickoff(db: str, run_id: str) -> str:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT kickoff_path FROM task_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


def _resolve_kickoff(kickoff: str, *, run_dir: str, target: str, db: str, run_id: str) -> str:
    # task_runs keeps the kickoff as given on the command line, often relative.
    kickoff = kickoff or _parent_kickoff(db, run_id)
    if not kickoff:
        return ""
    path = Path(kickoff)
    if path.is_absolute():
        return str(path)
    for base in (run_dir, target):
        candidate = Path(base) / path
        if base and candidate.is_file():
            return str(candidate)
    return str(Path(target or ".") / path)


def _source_artifact(recipe: str, home: str) -> str:
    from mini_ork.recipes_catalog import find_recipe

    info = find_recipe(recipe, Path(home) if home else None)
    contract_path = getattr(info, "path", None)
    if contract_path is None:
        return ""
    try:
        contract = yaml.safe_load((Path(contract_path) / "artifact_contract.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""
    return str(contract.get("source_artifact") or "")


def run_subgraph(*, parent_run_id: str, node_id: str, recipe: str, kickoff: str,
                 run_dir: str, home: str, target: str, db: str, base_env: Mapping[str, str],
                 timeout_s: float, runner: Runner = subprocess.run) -> dict:
    """Run ``recipe`` as a child of ``parent_run_id``; return the link record."""
    from mini_ork.orchestration import recursive

    child_run_id = f"{parent_run_id}--{node_id}"
    depth = int(base_env.get(DEPTH_ENV) or 0) + 1
    record: dict = {"node": node_id, "recipe": recipe, "child_run_id": child_run_id,
                    "depth": depth, "rc": None, "artifact": None, "error": ""}
    kickoff_path = _resolve_kickoff(kickoff, run_dir=run_dir, target=target, db=db, run_id=parent_run_id)
    record["kickoff"] = kickoff_path
    if not recipe:
        record["error"] = "subgraph node has no recipe"
        return record
    if not kickoff_path or not Path(kickoff_path).is_file():
        record["error"] = f"no kickoff for the child run ({kickoff_path or 'none'})"
        return record
    try:
        record["spawn_id"] = recursive.mo_recursive_approve_spawn(
            parent_run_id, child_run_id, recipe=recipe, kickoff_path=kickoff_path,
            child_workspace=target or ".", depth=depth)
    except ValueError as exc:
        record["error"] = str(exc)
        return record
    log_path = Path(run_dir) / f"subgraph-{node_id}.log"
    try:
        proc = runner(
            [sys.executable, "-m", "mini_ork.cli.main", "run", recipe, kickoff_path],
            env=child_env(base_env, child_run_id=child_run_id, depth=depth),
            cwd=target or None, capture_output=True, text=True, timeout=timeout_s,
        )
        record["rc"] = proc.returncode
        log_path.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
    except subprocess.TimeoutExpired:
        record["rc"] = 124
        record["error"] = f"child run exceeded {timeout_s:g}s"
    recursive.mo_recursive_mark_spawn(child_run_id, "completed" if record["rc"] == 0 else "failed")
    source = _source_artifact(recipe, home)
    produced = Path(home) / "runs" / child_run_id / source if source else None
    if produced is not None and produced.is_file():
        copied = Path(run_dir) / f"subgraph-{node_id}-{produced.name}"
        shutil.copyfile(produced, copied)
        record["artifact"] = str(copied)
    return record


def handle(ctx) -> tuple[int, str]:
    """Executor handler for ``type: subgraph`` nodes."""
    from mini_ork.context import context_env_snapshot

    env = context_env_snapshot()
    fields = node_fields(ctx.workflow, ctx.node_id)
    run_dir = ctx.run_dir_eff or ctx.run_dir
    home = env.get("MINI_ORK_HOME") or str(Path(run_dir).parent.parent)
    record = run_subgraph(
        parent_run_id=ctx.run_id, node_id=ctx.node_id, recipe=str(fields.get("recipe") or ""),
        kickoff=str(fields.get("kickoff") or ""), run_dir=run_dir, home=home,
        target=env.get("MO_TARGET_CWD") or os.getcwd(), db=ctx.db, base_env=env,
        timeout_s=float(env.get("MO_SUBGRAPH_TIMEOUT_S") or 3600),
    )
    Path(run_dir, f"subgraph-{ctx.node_id}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if record["error"] or record["rc"] != 0:
        print(f"  [subgraph] {ctx.node_id} -> {record['recipe']} failed: "
              f"{record['error'] or 'child rc=' + str(record['rc'])}", file=sys.stderr)
        return int(record["rc"] or 1), "subgraph_failed"
    print(f"  [subgraph] {ctx.node_id} -> {record['recipe']} passed (child {record['child_run_id']})")
    return 0, "done"


__all__ = ["DEPTH_ENV", "child_env", "handle", "node_fields", "run_subgraph"]
