"""Agent-tree assembler.

A 'mini-ork run' (task_run) dispatches N agents — one per recipe node.
Each agent has identity (name, type, model_lane, family), a prompt,
an output artifact, optional verifier log, LLM calls, lifecycle events,
and potentially recursive sub-dispatches.

This module assembles all of that into one navigable tree from
state.db + filesystem, so the UI can answer:
  - which agents did this run dispatch?
  - what prompt did each one see + what did each one emit?
  - how many LLM calls / how much $ / how long per agent?
  - which agents failed and why?
  - what sub-runs did any agent spawn?
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import artifacts, recipes
from .db import StateDB


# Heuristic mapping: recipe node → output artifact filename emitted by
# bin/mini-ork-execute's per-node-type case branches.
def _expected_artifacts(node_id: str, node_type: str) -> list[str]:
    candidates: list[str] = []
    # Researcher / lens nodes write lens-<short>.md per the _lens heuristic
    base = node_id
    for suffix in ("_lens", "-lens"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    if node_type == "researcher":
        candidates.append(f"lens-{base}.md")
        candidates.append(f"lens-{base}.md.stdout.md")
        candidates.append(f"context-{node_id}.json")
    if node_type == "implementer":
        candidates.append(f"impl-{node_id}.log")
    if node_type == "reviewer":
        # synthesizer / reviewer / synth_* — multiple shapes
        if "synth" in node_id:
            candidates.append("synthesis.md")
            candidates.append("synthesis.md.stdout.md")
        candidates.append(f"review-{node_id}.json")
    if node_type == "verifier":
        candidates.append(f"verifier-{node_id}.log")
        candidates.append(f"verifier-result-{node_id}.json")
        # Some recipes write under aliased verifier names
        candidates.append(f"verifier-{base}.log")
        candidates.append(f"verifier-result-{base}.json")
    return candidates


def list_agents(
    db: StateDB,
    home: Path,
    mini_ork_root: Path,
    task_run_id: str,
) -> dict[str, Any]:
    """Return all agents dispatched by a task_run with summary metrics."""
    tr = db.row(
        """
        SELECT id, recipe, trace_id, created_at, ended_at, status, cost_usd
        FROM task_runs WHERE id = ?
        """,
        (task_run_id,),
    )
    if not tr:
        return {"agents": [], "task_run": None, "children": []}

    recipe_name = tr.get("recipe")
    fp = recipes.fingerprint(recipe_name) if recipe_name else {"nodes": [], "edges": []}

    # Per-node lifecycle events (node_start / node_end) keyed by node_id.
    node_events = _collect_node_events(db, task_run_id)

    # Per-node LLM call rollup (cost, count, tokens) — joined via traceparent
    # LIKE trace_id AND metadata_json -> node_id when available, with
    # lane→node_id fallback derived from the recipe DAG.
    llm_per_node = _collect_llm_per_node(
        db,
        tr.get("trace_id"),
        tr.get("created_at"),
        tr.get("ended_at"),
        recipe_name=recipe_name,
    )

    # Filesystem artifacts already on disk for this run
    run_dir = home / "runs" / task_run_id
    fs_files: set[str] = set()
    if run_dir.exists():
        fs_files = {p.name for p in run_dir.iterdir() if p.is_file()}

    agents: list[dict[str, Any]] = []
    for node in fp["nodes"]:
        node_id = node["name"]
        node_type = node["type"]
        ev = node_events.get(node_id, {})
        llm_summary = llm_per_node.get(node_id, {"count": 0, "cost": 0.0, "tokens": 0, "fallback": False})
        # All-node llm aggregate if metadata didn't carry node_id (older runs)
        if llm_summary["count"] == 0 and llm_per_node.get("__unattributed__"):
            llm_summary = {**llm_per_node["__unattributed__"], "fallback": True}

        expected = _expected_artifacts(node_id, node_type)
        artifact_hits = [name for name in expected if name in fs_files]

        agents.append(
            {
                "node_id": node_id,
                "node_type": node_type,
                "model_lane": node.get("lane"),
                "family": node.get("family"),
                "prompt_ref": node.get("prompt_ref"),
                "verifier_ref": node.get("verifier_ref"),
                "dispatch_mode": node.get("dispatch_mode"),
                "gates": node.get("gates") or [],
                "status": ev.get("status", "never_seen"),
                "duration_ms": ev.get("duration_ms"),
                "verdict": ev.get("verdict"),
                "started_at": ev.get("started_at"),
                "ended_at": ev.get("ended_at"),
                "artifact_files": artifact_hits,
                "llm_call_count": llm_summary["count"],
                "llm_cost_usd": llm_summary["cost"],
                "llm_total_tokens": llm_summary["tokens"],
                "llm_attribution_fallback": llm_summary.get("fallback", False),
            }
        )

    # Recursive sub-runs from run_spawns
    children = []
    if db.has_table("run_spawns"):
        children = db.rows(
            """
            SELECT spawn_id, child_run_id, depth, recipe, kickoff_path,
                   authority_level, allow_child_spawn, status, created_at
            FROM run_spawns WHERE parent_run_id = ? ORDER BY created_at ASC
            """,
            (task_run_id,),
        )

    return {
        "task_run": tr,
        "recipe": recipe_name,
        "edges": fp.get("edges", []),
        "agents": agents,
        "children": children,
    }


def load_transcript(home: Path, task_run_id: str, node_id: str) -> dict[str, Any]:
    """Read the per-turn transcript file for a given agent dispatch.

    Prefer <run_dir>/agent-<node_id>.transcript.json. The universal
    llm_dispatch shim writes this stable sidecar even when the model output
    flowed through a temporary stdout file. Older runs may only have
    <output_artifact>.transcript.json beside the final artifact.
    """
    run_dir = home / "runs" / task_run_id
    if not run_dir.exists():
        return {"turns": [], "available": False, "reason": "run dir missing"}

    # Try known output-file shapes for each node_type
    candidates: list[Path] = []
    base = node_id
    for suffix in ("_lens", "-lens"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    for name in (
        f"agent-{node_id}.transcript.json",
        f"lens-{base}.md.transcript.json",
        f"lens-{node_id}.md.transcript.json",
        f"context-{node_id}.json.transcript.json",
        "synthesis.md.transcript.json",
        f"review-{node_id}.json.transcript.json",
        f"impl-{node_id}.log.transcript.json",
    ):
        candidates.append(run_dir / name)
    for path in candidates:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["transcript_path"] = str(path.relative_to(home))
                payload["available"] = True
                payload.setdefault("turns", [])
                return payload
            except (OSError, json.JSONDecodeError) as e:
                return {"turns": [], "available": False, "reason": f"parse error: {e}"}

    fallback_artifacts = [
        run_dir / f"lens-{base}.md",
        run_dir / f"lens-{node_id}.md",
        run_dir / f"context-{node_id}.json",
        run_dir / "synthesis.md",
        run_dir / f"review-{node_id}.json",
        run_dir / f"impl-{node_id}.log",
    ]
    for path in fallback_artifacts:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return {
            "turns": [
                {
                    "turn_index": 0,
                    "model": None,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "text": text,
                    "tool_uses": [],
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "stop_reason": None,
                    "session_id": None,
                }
            ],
            "available": True,
            "fallback": "text-output",
            "transcript_path": str(path.relative_to(home)),
        }
    return {
        "turns": [],
        "available": False,
        "reason": "no transcript.json found (stream-json not captured for this agent)",
    }


def agent_detail(
    db: StateDB,
    home: Path,
    mini_ork_root: Path,
    task_run_id: str,
    node_id: str,
) -> dict[str, Any]:
    """Full per-agent detail: prompt, output, LLM calls, child spawns, transcript."""
    tr = db.row("SELECT recipe, trace_id, created_at, ended_at FROM task_runs WHERE id = ?", (task_run_id,))
    if not tr:
        return {"error": f"task_run {task_run_id} not found"}
    recipe_name = tr.get("recipe")
    fp = recipes.fingerprint(recipe_name) if recipe_name else {"nodes": []}
    node = next((n for n in fp["nodes"] if n["name"] == node_id), None)
    if not node:
        return {"error": f"node {node_id} not in recipe {recipe_name}"}

    ev = _collect_node_events(db, task_run_id).get(node_id, {})

    # Prompt source — recipe-relative path under recipes/<name>/<prompt_ref>
    prompt_text = None
    prompt_path = None
    if recipe_name and node.get("prompt_ref"):
        candidate = mini_ork_root / "recipes" / recipe_name / node["prompt_ref"]
        if candidate.exists():
            prompt_path = str(candidate)
            try:
                prompt_text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                prompt_text = None

    # Output artifacts present on disk
    expected = _expected_artifacts(node_id, node["type"])
    artifact_payloads: list[dict[str, Any]] = []
    for name in expected:
        try:
            payload = artifacts.read_artifact(home, task_run_id, name)
            artifact_payloads.append(payload)
        except (FileNotFoundError, PermissionError):
            continue

    # LLM calls attributable to this agent (with lane→node fallback)
    llm_rows = _collect_llm_rows_for_node(
        db,
        tr.get("trace_id"),
        tr.get("created_at"),
        tr.get("ended_at"),
        node_id,
        recipe_name=recipe_name,
    )

    # Recursive spawns where this agent (by node_id embedded in spawn metadata
    # or by being the dispatcher of a child task_run) is the parent.
    children = []
    if db.has_table("run_spawns"):
        children = db.rows(
            """
            SELECT spawn_id, child_run_id, depth, recipe, status,
                   created_at, updated_at, kickoff_path
            FROM run_spawns
            WHERE parent_run_id = ?
              AND (policy_snapshot_json LIKE ? OR ? IS NULL)
            ORDER BY created_at ASC
            """,
            (task_run_id, f"%{node_id}%", node_id),
        )

    return {
        "task_run_id": task_run_id,
        "node": node,
        "status": ev.get("status", "never_seen"),
        "duration_ms": ev.get("duration_ms"),
        "verdict": ev.get("verdict"),
        "started_at": ev.get("started_at"),
        "ended_at": ev.get("ended_at"),
        "prompt": {"path": prompt_path, "content": prompt_text},
        "artifacts": artifact_payloads,
        "llm_calls": llm_rows,
        "transcript": load_transcript(home, task_run_id, node_id),
        "children": children,
    }


# ── helpers ───────────────────────────────────────────────────────────────


def _collect_node_events(db: StateDB, task_run_id: str) -> dict[str, dict[str, Any]]:
    if not db.has_table("run_events"):
        return {}
    rows = db.rows(
        """
        SELECT event_type, created_at, payload_json
        FROM run_events
        WHERE run_id = ? AND event_type IN ('node_start', 'node_end')
        ORDER BY created_at ASC
        """,
        (task_run_id,),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            p = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except json.JSONDecodeError:
            p = {}
        nid = p.get("node_id")
        if not nid:
            continue
        e = out.setdefault(nid, {"status": "never_seen"})
        if r["event_type"] == "node_start":
            e["status"] = "running"
            e["started_at"] = r["created_at"]
        elif r["event_type"] == "node_end":
            verdict = p.get("verdict")
            e["status"] = "failed" if verdict in ("REQUEST_CHANGES", "ESCALATE", "CRASH") else "done"
            e["verdict"] = verdict
            e["duration_ms"] = p.get("duration_ms")
            e["ended_at"] = r["created_at"]
    return out


def _lane_to_nodes(recipe_name: str | None) -> dict[str, list[str]]:
    """Build lane → [node_id, ...] map from a recipe.

    A lane often maps 1:1 to a node (perf_lens uses minimax_lens), but
    sometimes N nodes share a lane (arch_lens + arxiv_lens + implementer
    all use codex_lens). Attribution must be honest about the ambiguity.
    """
    if not recipe_name:
        return {}
    rec = recipes.load_recipe(recipe_name)
    nodes = (rec.get("workflow") or {}).get("nodes") or []
    out: dict[str, list[str]] = {}
    for n in nodes:
        lane = n.get("model_lane") if isinstance(n, dict) else None
        name = n.get("name") if isinstance(n, dict) else None
        if not lane or not name:
            continue
        out.setdefault(lane, []).append(name)
    return out


def _collect_llm_per_node(
    db: StateDB,
    trace_id: str | None,
    created_at: int | None,
    ended_at: int | None,
    recipe_name: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Group llm_calls by node_id for THIS task_run.

    Attribution priority:
      1. metadata_json.node_id   — canonical, set by MO_NODE_ID env in dispatcher
      2. feature_name suffix as lane → recipe lookup → unique node_id
      3. feature_name suffix as-is (legacy fallback when lane → multiple nodes)
    """
    if not db.has_table("llm_calls"):
        return {}
    rows = _llm_rows_for_run(db, trace_id, created_at, ended_at)
    lane_map = _lane_to_nodes(recipe_name)
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        node_id: str | None = None
        is_fallback = False
        try:
            md = json.loads(r.get("metadata_json") or "{}")
            node_id = md.get("node_id")
        except json.JSONDecodeError:
            pass
        if not node_id:
            feat = r.get("feature_name") or ""
            lane = feat.split(":", 1)[1] if ":" in feat else feat
            # 1:1 lane → node attribution (only when there's exactly one node
            # using that lane). For ambiguous lanes (codex_lens → 3 nodes),
            # leave the row as feature-name-keyed.
            mapped = lane_map.get(lane, [])
            if len(mapped) == 1:
                node_id = mapped[0]
                is_fallback = True
            else:
                node_id = lane
                is_fallback = True
        key = node_id or "__unattributed__"
        bucket = grouped.setdefault(
            key, {"count": 0, "cost": 0.0, "tokens": 0, "fallback": False}
        )
        bucket["count"] += 1
        bucket["cost"] += float(r.get("cost_usd") or 0.0)
        bucket["tokens"] += int(r.get("total_tokens") or 0)
        if is_fallback:
            bucket["fallback"] = True
    return grouped


def _collect_llm_rows_for_node(
    db: StateDB,
    trace_id: str | None,
    created_at: int | None,
    ended_at: int | None,
    node_id: str,
    recipe_name: str | None = None,
) -> list[dict[str, Any]]:
    rows = _llm_rows_for_run(db, trace_id, created_at, ended_at)
    lane_map = _lane_to_nodes(recipe_name)
    out: list[dict[str, Any]] = []
    for r in rows:
        attr_node: str | None = None
        try:
            md = json.loads(r.get("metadata_json") or "{}")
            attr_node = md.get("node_id")
        except json.JSONDecodeError:
            pass
        if not attr_node:
            feat = r.get("feature_name") or ""
            lane = feat.split(":", 1)[1] if ":" in feat else feat
            mapped = lane_map.get(lane, [])
            if len(mapped) == 1:
                attr_node = mapped[0]
            else:
                attr_node = lane
        if attr_node == node_id:
            out.append(r)
    out.sort(key=lambda r: str(r.get("ts") or ""))
    return out


def _llm_rows_for_run(
    db: StateDB,
    trace_id: str | None,
    created_at: int | None,
    ended_at: int | None,
) -> list[dict[str, Any]]:
    """Pull llm_calls correlated to a task_run via trace_id or time-window."""
    if not db.has_table("llm_calls"):
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    if trace_id:
        for r in db.rows(
            """
            SELECT id, provider, model_id, tier, feature_name, actor,
                   input_tokens, output_tokens, total_tokens, cost_usd,
                   duration_ms, status, finish_reason, ts, traceparent,
                   error_message, metadata_json
            FROM llm_calls WHERE traceparent LIKE ?
            """,
            (f"%{trace_id}%",),
        ):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            r["bridge"] = "trace_id"
            out.append(r)
    if created_at:
        import time as _t
        upper = ended_at or int(_t.time())
        for r in db.rows(
            """
            SELECT id, provider, model_id, tier, feature_name, actor,
                   input_tokens, output_tokens, total_tokens, cost_usd,
                   duration_ms, status, finish_reason, ts, traceparent,
                   error_message, metadata_json
            FROM llm_calls
            WHERE strftime('%s', ts) BETWEEN ? AND ?
            """,
            (int(created_at), int(upper)),
        ):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            r["bridge"] = "time-window"
            out.append(r)
    return out
